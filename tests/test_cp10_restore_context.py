"""CP10 adversarial round 7: a placeholder is restored by what its place is, and a text placeholder in code stands in
a string literal.

* The place decides how a placeholder is written back, never what the answer happens to write there: the text of a
  Transform Message part (``ee:set-payload``, ``ee:set-variable``, ``ee:set-attributes``) is DataWeave whether or not it
  starts with ``%dw``, every ``#[...]`` is DataWeave, a value that is a JSON object or array as a whole is JSON text,
  any other attribute or element text is plain text, and code is code of its language.
* In DataWeave, JSON text and code a text placeholder must stand inside a string literal, where it is spelled for
  that literal's quotes. Written bare it is refused, whatever its value (``"true"`` written bare in JSON would be the
  boolean ``true``, ``"active"`` would be no JSON at all), and so is one in a regular expression or in a stretch a
  ``/`` may start one. A bare text placeholder is allowed only in plain text.

Case IDs:

* CP10-X50 - a text placeholder written bare in JSON text is refused (fix loop and translation paths); quoted, it is
  the exact value, JSON-escaped.
* CP10-X51 - a headerless DataWeave script (a Transform Message part) is restored with DataWeave rules: backslashes,
  an apostrophe and ``$`` escaped; the fix loop shows such a script as DataWeave too.
* CP10-X52 - a text placeholder in a regular expression or in a ``/`` stretch that may be one is refused; number
  placeholders there are restored as numbers.
* CP10-X53 - a generator over places x placeholder kinds: each restores to the exact spelled value or is refused.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from typing import Any

import pytest

from a2m.ai.placeholders import JAVASCRIPT, PYTHON, PlaceholderError, Placeholders
from a2m.ai.provider import ItemKind
from a2m.ai.sources import CalloutSource
from a2m.ai.translate import CalloutTranslated, NotTranslated, Place, Translator

CORE = "http://www.mulesoft.org/schema/mule/core"
EE = "http://www.mulesoft.org/schema/mule/ee/core"
WRAP = f'<mule xmlns="{CORE}" xmlns:ee="{EE}"><flow name="f">{{}}</flow></mule>'
PLACE = Place("ctx-proxy", "default PreFlow request", "request", "AM-Before", "AM-After")
POLICY = '<Javascript name="JS-Ctx"><ResourceURL>jsc://ctx.js</ResourceURL></Javascript>'
ANY_TOKEN = re.compile(r"«[vn]\d+»")


class Recorder:
    """A provider that records every request and answers with ``answer(request)``."""

    def __init__(self, answer: Callable[[Any], str]) -> None:
        self.requests: list[Any] = []
        self.answer = answer

    def complete(self, request: Any) -> str:
        self.requests.append(request)
        return self.answer(request)


def _callout_answer(mule: str, variables: list[str]) -> str:
    return json.dumps(
        {"status": "translated", "confidence": "high", "notes": "n", "mule": mule, "writes": {"variables": variables}}
    )


def _callout(code: str, build: Callable[[list[str]], tuple[str, list[str]]]) -> CalloutTranslated | NotTranslated:
    """``Translator.callout`` on the JavaScript ``code``; ``build`` gets the placeholders of the code as shown in the
    prompt (in order) and returns the Mule answer and its written variables."""

    def answer(request: Any) -> str:
        shown = re.search(r"```javascript\n(.*?)```", str(request.prompt), re.DOTALL)
        assert shown is not None, request.prompt
        tokens = list(dict.fromkeys(ANY_TOKEN.findall(shown.group(1))))
        mule, variables = build(tokens)
        return _callout_answer(mule, variables)

    provider = Recorder(answer)
    result = Translator(provider).callout(CalloutSource(ItemKind.JAVASCRIPT, code, "ctx.js"), "JS-Ctx", "Javascript",
                                          POLICY, PLACE)
    assert len(provider.requests) == 1
    return result


def _script_text(result: CalloutTranslated, local: str) -> str:
    for processor in result.processors:
        for element in processor.iter():
            if element.tag == f"{{{EE}}}{local}":
                return element.text or ""
    raise AssertionError(f"no ee:{local} in the translation")


# ------------------------------------------------------------------------------------------------ X50: JSON words


@pytest.mark.parametrize("value", ["true", "false", "null", "active"])
def test_CP10_X50_a_text_placeholder_written_bare_in_json_is_refused_in_the_fix_loop(value: str) -> None:
    """[CP10-X50] The reviewer's repro: a Mule file holds the JSON string "true" (or "false", "null", a word). The AI
    drops the quotes around its placeholder; restoring it bare would turn the string into a boolean or null, or into
    text that is no JSON. It is refused; quoted, it is the same string."""
    mule = WRAP.format(f"""<set-payload value='{{"enabled": "{value}", "note": "hi"}}'/>""")
    table = Placeholders()
    table.learn_mule([mule])
    shown = table.mule({"flow.xml": mule})["flow.xml"]
    assert shown is not None
    token = next(t for t, v in table.values.items() if v == value)
    assert f'"{token}"' in shown, shown

    with pytest.raises(PlaceholderError, match="outside any string literal"):
        table.restore("flow.xml", shown.replace(f'"{token}"', token).replace('"note"', '"note2"'))
    edited = table.restore("flow.xml", shown.replace("}'/>", ', "x": 1}\'/>'))
    payload = ET.fromstring(edited)[0][0].get("value", "")
    assert json.loads(payload)["enabled"] == value


def test_CP10_X50_a_text_placeholder_written_bare_in_json_makes_a_callout_unusable() -> None:
    """[CP10-X50] The translation path: the code's string 'true' written bare in the JSON payload of the AI's Mule
    answer makes the answer unusable; written in quotes it is the JSON string "true", escaped for JSON."""

    def bare(tokens: list[str]) -> tuple[str, list[str]]:
        return f"<set-payload value='{{\"enabled\": {tokens[0]}}}' mimeType=\"application/json\"/>", []

    def quoted(tokens: list[str]) -> tuple[str, list[str]]:
        mule = (
            f"<set-payload value='{{\"enabled\": \"{tokens[0]}\", \"dir\": \"{tokens[1]}\"}}' "
            "mimeType=\"application/json\"/>"
        )
        return mule, []

    code = "var s = 'true'; var d = 'C:\\\\temp';\n"
    refused = _callout(code, bare)
    assert isinstance(refused, NotTranslated), refused
    assert "outside any string literal" in refused.reason, refused.reason

    used = _callout(code, quoted)
    assert isinstance(used, CalloutTranslated), used
    payload = used.processors[0].get("value", "")
    assert json.loads(payload) == {"enabled": "true", "dir": "C:\\temp"}


# ------------------------------------------------------------------------------------------------ X51: headerless DW


def test_CP10_X51_a_headerless_transform_script_in_a_callout_is_restored_as_dataweave() -> None:
    """[CP10-X51] The reviewer's end-to-end repro: JavaScript sets 'C:\\\\temp\\\\new' and 'Can\\'t'; the AI's
    Transform Message scripts have no %dw header (the form the prompts teach). Each value is spelled for its
    DataWeave string: backslashes doubled, the apostrophe escaped, and '$' escaped in "Price: $(amount)"."""
    code = (
        "context.setVariable('backupDir', 'C:\\\\temp\\\\new');\n"
        "var p = 'Can\\'t';\n"
        "var m = 'Price: $(amount)';\n"
    )

    def build(tokens: list[str]) -> tuple[str, list[str]]:
        name, path, cant, price = tokens
        mule = (
            "<ee:transform><ee:message>"
            f"<ee:set-payload><![CDATA[{{ msg: \"{price}\", who: '{cant}' }}]]></ee:set-payload>"
            "</ee:message><ee:variables>"
            f"<ee:set-variable variableName=\"{name}\"><![CDATA['{path}']]></ee:set-variable>"
            "</ee:variables></ee:transform>"
        )
        return mule, [name]

    result = _callout(code, build)

    assert isinstance(result, CalloutTranslated), result
    assert _script_text(result, "set-variable") == "'C:\\\\temp\\\\new'"
    assert _script_text(result, "set-payload") == "{ msg: \"Price: \\$(amount)\", who: 'Can\\'t' }"


def test_CP10_X51_a_headerless_transform_script_in_a_mule_file_is_shown_and_restored_as_dataweave() -> None:
    """[CP10-X51] The fix loop: a headerless Transform Message script is shown as DataWeave (its literal hidden, its
    structure visible), an echo is the file byte for byte, and a diff value written into its string literal is
    spelled for DataWeave."""
    script = "payload ++ { msg: 'old-value', dir: \"C:\\\\temp\" }"
    mule = WRAP.format(f"<ee:transform><ee:message><ee:set-payload><![CDATA[{script}]]></ee:set-payload>"
                       "</ee:message></ee:transform>")
    table = Placeholders()
    shown = table.mule({"flow.xml": mule})["flow.xml"]
    assert shown is not None
    old = next(t for t, v in table.values.items() if v == "old-value")
    assert f"payload ++ {{ msg: '{old}', dir: \"" in shown, shown
    assert table.restore("flow.xml", shown) == mule

    diff = table.diff("header X-Value: expected \"Can't $(x) C:\\\\new\", actual 'zz'")
    found = re.search(r'expected "(«v\d+»)"', diff)
    assert found is not None, diff
    restored = table.restore("flow.xml", shown.replace(f"'{old}'", f"'{found.group(1)}'"))
    text = ET.fromstring(restored).find(f".//{{{EE}}}set-payload")
    assert text is not None
    assert text.text == "payload ++ { msg: 'Can\\'t \\$(x) C:\\\\new', dir: \"C:\\\\temp\" }"


# ------------------------------------------------------------------------------------------------ X52: regex spans


def test_CP10_X52_a_text_placeholder_in_an_unsure_slash_stretch_is_refused() -> None:
    """[CP10-X52] The reviewer's repro: a "/" right after a placeholder may start a regular expression, so the
    stretch to the next "/" was read as one and its text placeholder pasted in as code. It is refused; number
    placeholders in such a stretch still come back as the same numbers."""
    table = Placeholders()
    shown = table.code("var a = 'Bearer'; var n = 5e3; var m = 7;", ItemKind.JAVASCRIPT)
    text, five, seven = ANY_TOKEN.findall(shown)

    with pytest.raises(PlaceholderError, match="outside any string literal"):
        table.restore_code(f"{five} / {text} / 2")
    assert table.restore_code(f"vars.a * {five} / {seven} / 2") == "vars.a * 5e3 / 7 / 2"


def test_CP10_X52_a_text_placeholder_in_a_regular_expression_is_refused_in_a_mule_file() -> None:
    """[CP10-X52] The fix loop's whole-file restore: a text placeholder in an unsure "/" stretch of a ``#[...]`` and
    one in a regular expression for sure are refused, never pasted in."""
    table = Placeholders()
    shown = table.code("var a = 'Bearer'; var n = 5e3;", ItemKind.JAVASCRIPT)
    text, five = ANY_TOKEN.findall(shown)

    for expression in (f"{five} / {text} / 2", f"payload matches /{text}/", f"payload splitBy /a{text}/"):
        answer = WRAP.format(f'<set-variable variableName="r" value="#[{expression}]"/>')
        with pytest.raises(PlaceholderError, match="regular expression"):
            table.restore("flow.xml", answer)


# ------------------------------------------------------------------------------------------------ X53: generator


def _dw(value: str, quote: str) -> str:
    return value.replace("\\", "\\\\").replace(quote, "\\" + quote).replace("$", "\\$")


def _json(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _code(value: str, quote: str) -> str:
    return value.replace("\\", "\\\\").replace(quote, "\\" + quote)


def _same(value: str) -> str:
    return value


TEXT_VALUES = ("true", "null", "active", "C:\\temp\\new", "Can't", 'say "hi"', "$(a)", "x < y & z")
# The number placeholder stands for the JavaScript literal 5e3; N0 for 7.
NUMBER_CODE = "var n = 5e3; var m = 7;"


def _ee_message(inner: str) -> str:
    return f"<ee:transform><ee:message>{inner}</ee:message></ee:transform>"


def _ee_variable(inner: str) -> str:
    return f'<ee:transform><ee:variables><ee:set-variable variableName="v">{inner}</ee:set-variable></ee:variables></ee:transform>'


# name -> (Mule fragment or code with {T}, where: an attribute "value", the text of an element (local name) or code
# in a language, the restored value with {T} in it, how a text value is spelled (None: refused), what the number is
# written as (None: refused)).
PLACES: dict[str, tuple[str, str, str, Callable[[str], str] | None, str | None]] = {
    "plain-attribute": ('<set-variable variableName="v" value="{T}"/>', "@value", "{T}", _same, "5000"),
    "plain-text": ("<description>{T}</description>", "description", "{T}", _same, "5000"),
    "dw-expression-single": (
        "<set-variable variableName=\"v\" value=\"#['{T}']\"/>", "@value", "#['{T}']", lambda v: _dw(v, "'"), None
    ),
    "dw-expression-double": (
        "<set-variable variableName=\"v\" value='#[\"{T}\"]'/>", "@value", '#["{T}"]', lambda v: _dw(v, '"'), None
    ),
    "dw-expression-bare": ('<set-variable variableName="v" value="#[vars.a + {T}]"/>', "@value", "#[vars.a + {T}]",
                           None, "5e3"),
    "ee-headerless-single-cdata": (
        _ee_message("<ee:set-payload><![CDATA[payload ++ {msg: '{T}'}]]></ee:set-payload>"), "set-payload",
        "payload ++ {msg: '{T}'}", lambda v: _dw(v, "'"), None,
    ),
    "ee-headerless-object-double": (
        _ee_variable('<![CDATA[{ msg: "{T}" }]]>'), "set-variable", '{ msg: "{T}" }', lambda v: _dw(v, '"'), None,
    ),
    "ee-headerless-bare": (_ee_variable("<![CDATA[vars.a + {T}]]>"), "set-variable", "vars.a + {T}", None, "5e3"),
    "ee-headerless-element-text": (
        _ee_message("<ee:set-attributes>'{T}'</ee:set-attributes>"), "set-attributes", "'{T}'",
        lambda v: _dw(v, "'"), None,
    ),
    "dw-script": (
        _ee_message("<ee:set-payload><![CDATA[%dw 2.0\noutput application/json\n---\n{ msg: '{T}' }]]>"
                    "</ee:set-payload>"),
        "set-payload", "%dw 2.0\noutput application/json\n---\n{ msg: '{T}' }", lambda v: _dw(v, "'"), None,
    ),
    "json-string": ("<set-payload value='{\"k\": \"{T}\"}'/>", "@value", '{"k": "{T}"}', _json, None),
    "json-bare": ("<set-payload value='{\"k\": {T}}'/>", "@value", '{"k": {T}}', None, "5e3"),
    "dw-regex": ('<set-variable variableName="v" value="#[payload matches /{T}/]"/>', "@value",
                 "#[payload matches /{T}/]", None, "5000"),
    "dw-unsure-slash": ('<set-variable variableName="v" value="#[{N0} / {T} / 2]"/>', "@value", "#[7 / {T} / 2]",
                        None, "5e3"),
    "js-single": ("x = '{T}';", JAVASCRIPT, "x = '{T}';", lambda v: _code(v, "'"), None),
    "js-bare": ("x = {T};", JAVASCRIPT, "x = {T};", None, "5e3"),
    "js-regex": ("x = /{T}/;", JAVASCRIPT, "x = /{T}/;", None, "5000"),
    "python-double": ('x = "{T}"', PYTHON, 'x = "{T}"', lambda v: _code(v, '"'), None),
    "dataweave-code-bare": ("vars.a ++ {T}", "dataweave", "vars.a ++ {T}", None, "5e3"),
}


def _restored(table: Placeholders, template: str, where: str, token: str, seven: str) -> str:
    """The place ``template`` with ``token`` (and N0) restored, read back from the restored text."""
    text = template.replace("{T}", token).replace("{N0}", seven)
    if where in (JAVASCRIPT, PYTHON, "dataweave"):
        return table.restore_code(text, where)
    restored = table.restore("flow.xml", WRAP.format(text))
    root = ET.fromstring(restored)
    if where.startswith("@"):
        element = root[0][0]
        return element.get(where[1:], "")
    found = next(element for element in root.iter() if element.tag.rpartition("}")[2] == where)
    return found.text or ""


def test_CP10_X53_every_place_restores_each_placeholder_kind_exactly_or_refuses_it() -> None:
    """[CP10-X53] Places (plain attribute and text, DataWeave expressions, headerless and full Transform Message
    scripts, JSON strings and bare JSON, regular expressions, an unsure "/" stretch, JavaScript, Python and DataWeave
    code) x placeholder kinds (text values that read as JSON words, backslashes, quotes, '$', XML specials; a number):
    each restores to exactly the value spelled for its place, or is refused. Nothing is restored any other way."""
    failures: list[str] = []
    for name, (template, where, logical, spell, number) in PLACES.items():
        for value in (*TEXT_VALUES, None):
            table = Placeholders()
            five, seven = ANY_TOKEN.findall(table.code(NUMBER_CODE, ItemKind.JAVASCRIPT))
            token = five if value is None else table.token(value)
            expected_piece = number if value is None else (spell(value) if spell is not None else None)
            try:
                got = _restored(table, template, where, token, seven)
            except PlaceholderError as exc:
                if expected_piece is not None:
                    failures.append(f"{name} {value!r}: refused ({exc}), expected {expected_piece!r}")
                continue
            if expected_piece is None:
                failures.append(f"{name} {value!r}: restored as {got!r}, expected a refusal")
            elif got != logical.replace("{T}", expected_piece):
                failures.append(f"{name} {value!r}: {got!r} != {logical.replace('{T}', expected_piece)!r}")
    assert not failures, "\n".join(failures)
