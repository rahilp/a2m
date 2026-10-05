"""CP10 adversarial round 11: a restore never falls back to reading a part as plain text when it cannot tell the
part's context for sure, and siblings that tie on their attributes pair by content.

* CP10-X68 - a value that starts with "#[", ends with "]" and holds no other "#[" is one DataWeave expression (as
  Mule reads it), whatever a "/" or a "//" comment in it does to a2m's guess of where it ends: an added division
  with a string holding "/", the same on lines of their own, and a trailing comment restore byte for byte, and a
  new placeholder written there is spelled for its string. In a template whose expression end a2m cannot find for
  sure, a placeholder after it is refused with a reason that names the expression.
* CP10-X69 - a value that is JSON text as a whole stays JSON text when the answer adds a ``${property}`` (the
  values' ``\\\\`` and ``\\"`` kept); a new ``#[...]`` in such a value makes it a template whose text a2m cannot
  spell for JSON, so it is refused with that reason.
* CP10-X70 - generated across contexts (DataWeave strings of both quotes in an expression and a script, JSON text,
  a mixed template) and edits (division with a "/" string, inline and on its own lines, a trailing comment, a
  ``${property}``, a new placeholder, a control rename), with values holding quotes, backslashes, ``$`` and
  ``$(``: every restore either refuses or gives text whose string literals decode exactly to the values, read by
  an independent decoder; the edits a2m can read for sure restore.
* CP10-X71 - an added line with an unsure "/" whose quotes and "//" comment close on that line hides only that
  line: placeholders after it in unchanged code restore. When such a line leaves a quote open, a placeholder below
  is refused with a reason that names that line, not "write the code on a line of its own".
* CP10-X72 - a new sibling inserted before an edited one that no attribute tells apart pairs by content: the
  edited ``<when>`` keeps its regex. Two answer parts exactly as like the shown one are refused with a reason that
  says a2m cannot tell which stands for it.
* CP10-X73 - the other fallbacks: a plain value that would make a ``${property}`` is refused, and a value that
  starts like XML but is not XML a2m can read refuses a placeholder that was not left as shown.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable

import pytest

from a2m.ai.placeholders import PlaceholderError, Placeholders

CORE = "http://www.mulesoft.org/schema/mule/core"
EE = "http://www.mulesoft.org/schema/mule/ee/core"
HEAD = f'<mule xmlns="{CORE}" xmlns:ee="{EE}"><flow name="f">'
TAIL = "</flow></mule>"
NAMES = ("f", "greeting", "INFO")


def _show(original: str) -> tuple[Placeholders, str]:
    table = Placeholders(list(NAMES))
    shown = table.mule({"a.xml": original})["a.xml"]
    assert shown is not None
    assert table.restore("a.xml", shown) == original
    return table, shown


# ------------------------------------------------------------------------------------------------ X68: expression


D3 = (
    HEAD + '<set-variable variableName="greeting" value="#[\'O\\\'Reilly says \' ++ (vars.name default \'a\\\' ++ '
    "p(\\'secure::db.password\\') ++ \\'b')]\"/>" + TAIL
)


@pytest.mark.parametrize(
    ("label", "old", "new"),
    [
        ("division", "(vars.name", "((vars.count default 0) / 2) ++ '/page ' ++ (vars.name"),
        ("own lines", "(vars.name", "\n((vars.count default 0) / 2) ++ '/page ' ++\n(vars.name"),
        ("comment", ')]"', ') // greet the caller]"'),
    ],
)
def test_CP10_X68_a_whole_value_expression_restores_with_a_slash_or_comment_in_it(label: str, old: str, new: str) -> None:
    """[CP10-X68] The reviewer's d3: the edits touch neither placeholder. Mule reads the whole value as one
    expression, so both stay DataWeave strings with their original bytes (they were written raw, the second as
    code reading a secure property)."""
    table, shown = _show(D3)
    answer = shown.replace(old, new, 1)
    assert answer != shown, label
    assert table.restore("a.xml", answer) == D3.replace(old, new, 1), label


def test_CP10_X68_a_new_placeholder_in_such_an_expression_is_spelled_for_its_string() -> None:
    """[CP10-X68] A new placeholder from a diff written into that expression is spelled for its string literal
    (quote and backslash escaped), never written raw."""
    table, shown = _show(D3)
    diff = table.diff("body $.greeting: expected 'it\\'s a \\\\ b', actual 'x'")
    found = re.search(r"expected '(«v\d+»)'", diff)
    assert found is not None, diff
    new = found.group(1)
    answer = shown.replace("(vars.name", f"((vars.count default 0) / 2) ++ '/page ' ++ '{new}' ++ (vars.name", 1)
    restored = table.restore("a.xml", answer)
    assert "++ '/page ' ++ 'it\\'s a \\\\ b' ++ (vars.name default 'a\\' ++ p(\\'secure" in restored, restored


def test_CP10_X68_a_placeholder_after_a_template_expression_with_an_unsure_end_is_refused() -> None:
    """[CP10-X68] In a mixed template a2m finds each expression's end with the DataWeave lexer; a "//" comment
    holding the "]" makes that end unsure, so the placeholder after it is refused (it was read as plain text), and
    the reason names the expression."""
    original = HEAD + '<logger level="INFO" message="Hi #[vars.who] and #[\'it\\\'s\']"/>' + TAIL
    table, shown = _show(original)
    answer = shown.replace("#[vars.who]", "#[vars.who // the caller]", 1)
    assert answer != shown
    with pytest.raises(PlaceholderError) as caught:
        table.restore("a.xml", answer)
    reason = str(caught.value)
    assert "whose end a2m cannot find for sure" in reason and "#[vars.who // the caller]" in reason, reason


# ------------------------------------------------------------------------------------------------ X69: JSON


D1 = (
    HEAD + """<set-payload value='{"path": "C:\\\\temp\\\\new", "msg": "say \\"hi\\"", "n": 5}' """
    'mimeType="application/json"/>' + TAIL
)


def test_CP10_X69_a_json_value_gaining_a_property_stays_json_text() -> None:
    """[CP10-X69] The reviewer's d1: a ``${property}`` added as a field, or at the end, leaves every value's bytes
    as they were (it restored ``"C:\\temp\\new"`` and ``"say "hi""``)."""
    table, shown = _show(D1)
    field = shown.replace(', "«', ', "env": "${app.env}", "«', 1)
    assert table.restore("a.xml", field) == D1.replace(', "msg"', ', "env": "${app.env}", "msg"', 1)
    end = shown.replace("}' mime", ', "env": "${app.env}"}\' mime')
    restored = table.restore("a.xml", end)
    assert restored == D1.replace("}' mime", ', "env": "${app.env}"}\' mime'), restored
    payload = re.search(r"value='([^']*)'", restored)
    assert payload is not None
    assert json.loads(payload.group(1))["msg"] == 'say "hi"'


def test_CP10_X69_a_new_expression_in_a_json_value_is_refused() -> None:
    """[CP10-X69] A ``#[...]`` added inside a JSON string makes the value a template: Mule writes the text around
    it as it is, so a2m cannot spell the values for JSON there and refuses (they were written raw)."""
    table, shown = _show(D1)
    answer = shown.replace("}' mime", ', "ts": "#[now()]"}\' mime')
    with pytest.raises(PlaceholderError) as caught:
        table.restore("a.xml", answer)
    assert "JSON text as a whole" in str(caught.value), str(caught.value)


# ------------------------------------------------------------------------------------------------ X70: generated


def _xml(value: str, quote: str = '"') -> str:
    out = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\t", "&#9;")
    return out.replace(quote, "&quot;" if quote == '"' else "&apos;")


def _unxml(raw: str) -> str:
    names = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'"}

    def entity(match: re.Match[str]) -> str:
        name = match.group(1)
        if name.startswith("#x"):
            return chr(int("0x" + name[2:], 0))
        if name.startswith("#"):
            return chr(int(name[1:]))
        return names[name]

    return re.sub(r"&(#x[0-9A-Fa-f]+|#[0-9]+|amp|lt|gt|quot|apos);", entity, raw)


def _dw(value: str, quote: str) -> str:
    """``value`` spelled in a DataWeave string quoted with ``quote``."""
    out = value.replace("\\", "\\\\").replace(quote, "\\" + quote).replace("$", "\\$")
    return out.replace("\t", "\\t").replace("\n", "\\n")


def _js(value: str) -> str:
    return json.dumps(value)[1:-1]


_DW_DECODE = {"\\": "\\", "'": "'", '"': '"', "$": "$", "/": "/", "n": "\n", "t": "\t", "r": "\r", "`": "`"}


def _dw_strings(code: str) -> list[str]:
    """The decoded string literals of DataWeave ``code`` with no regular expression in it (an independent reader:
    quotes with backslash escapes, ``//`` and ``/* */`` comments). Fails on a string that is never closed and on an
    unescaped ``$(``, which would be an interpolation, never the text it spells."""
    found: list[str] = []
    index = 0
    while index < len(code):
        char = code[index]
        if code.startswith("//", index):
            end = code.find("\n", index)
            index = len(code) if end < 0 else end
            continue
        if code.startswith("/*", index):
            end = code.find("*/", index + 2)
            assert end >= 0, code
            index = end + 2
            continue
        if char in "'\"":
            out: list[str] = []
            index += 1
            while True:
                assert index < len(code), f"string never closed: {code}"
                here = code[index]
                if here == "\\":
                    escape = code[index + 1]
                    if escape == "u":
                        out.append(chr(int(code[index + 2 : index + 6], 16)))
                        index += 6
                        continue
                    assert escape in _DW_DECODE, f"unknown escape in {code}"
                    out.append(_DW_DECODE[escape])
                    index += 2
                    continue
                if here == char:
                    index += 1
                    break
                assert not code.startswith("$(", index), f"interpolation in a string: {code}"
                out.append(here)
                index += 1
            found.append("".join(out))
            continue
        index += 1
    return found


def _expressions(template: str) -> list[str]:
    """The bodies of the ``#[...]`` expressions of a Mule template whose code holds no regular expression and no
    bracket inside a string or comment (independent of a2m's reader)."""
    bodies: list[str] = []
    index = template.find("#[")
    while index >= 0:
        depth, at, quote = 1, index + 2, ""
        while depth:
            assert at < len(template), template
            char = template[at]
            if quote:
                if char == "\\":
                    at += 1
                elif char == quote:
                    quote = ""
            elif char in "'\"":
                quote = char
            elif char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
            at += 1
        bodies.append(template[index + 2 : at - 1])
        index = template.find("#[", at)
    return bodies


VALUE_PAIRS = [
    ("O'Reilly", 'say "hi"'),
    ("C:\\temp\\new", "cost $5 $(vars.secret)"),
    ("a' ++ p('secure::db.password') ++ 'b", 'x" ++ vars.secret ++ "y'),
    ("tab\there", "back\\slash 'and' \"both\""),
]


def _attr(name: str, raw: str) -> Callable[[str], str]:
    def read(restored: str) -> str:
        found = re.search(name + r'="([^"]*)"', restored)
        assert found is not None, restored
        return _unxml(found.group(1))

    return read


def _contexts(a: str, b: str) -> dict[str, tuple[str, Callable[[str], list[str]], str]]:
    """Each context: the original Mule file holding values ``a`` and ``b``, how to read the decoded strings of the
    restored file, and the anchor the edits insert code before."""
    single = "#['" + _dw(a, "'") + "' ++ (vars.name default '" + _dw(b, "'") + "')]"
    double = '#["' + _dw(a, '"') + '" ++ (vars.name default "' + _dw(b, '"') + '")]'
    script = "%dw 2.0\noutput application/json\n---\n{\n  first: '" + _dw(a, "'") + "',\n  second: vars.name default \"" \
        + _dw(b, '"') + '"\n}'
    payload = '{"first": "' + _js(a) + '", "second": "' + _js(b) + '", "n": 5}'
    template = "Hi #['" + _dw(a, "'") + "' ++ vars.name] and #[\"" + _dw(b, '"') + '"]'

    def value_strings(restored: str) -> list[str]:
        found = re.search(r'variableName="greeting" value="([^"]*)"', restored)
        assert found is not None, restored
        body = _unxml(found.group(1))
        assert body.startswith("#[") and body.endswith("]"), body
        return _dw_strings(body[2:-1])

    def script_strings(restored: str) -> list[str]:
        found = re.search(r"<!\[CDATA\[(.*?)\]\]>", restored, re.DOTALL)
        assert found is not None, restored
        return _dw_strings(found.group(1).split("---", 1)[1])

    def json_strings(restored: str) -> list[str]:
        found = re.search(r"value='([^']*)'", restored)
        assert found is not None, restored
        data = json.loads(_unxml(found.group(1)))
        return [key for key in data] + [item for item in data.values() if isinstance(item, str)]

    def template_strings(restored: str) -> list[str]:
        found = re.search(r'message="([^"]*)"', restored)
        assert found is not None, restored
        text = _unxml(found.group(1))
        assert text.startswith("Hi #[") and "] and #[" in text, text
        return [string for body in _expressions(text) for string in _dw_strings(body)]

    return {
        "single": (
            HEAD + f'<set-variable variableName="greeting" value="{_xml(single)}"/>' + TAIL, value_strings, "(vars.name"
        ),
        "double": (
            HEAD + f'<set-variable variableName="greeting" value="{_xml(double)}"/>' + TAIL, value_strings, "(vars.name"
        ),
        "script": (
            HEAD + "<ee:transform><ee:message><ee:set-payload><![CDATA[" + script
            + "]]></ee:set-payload></ee:message></ee:transform>" + TAIL,
            script_strings,
            "vars.name",
        ),
        "json": (
            HEAD + f"<set-payload value='{_xml(payload, chr(39))}' mimeType=\"application/json\"/>" + TAIL,
            json_strings,
            "}' mime",
        ),
        "template": (
            HEAD + f'<logger level="INFO" message="{_xml(template)}"/>' + TAIL, template_strings, "vars.name]"
        ),
    }


# Edits: (label, code inserted before the anchor in DataWeave, the literals it adds). Each touches no placeholder.
DW_EDITS = [
    ("division", "((vars.count default 0) / 2) ++ '/page ' ++ ", ["/page "]),
    ("own lines", "\n((vars.count default 0) / 2) ++ '/page ' ++\n", ["/page "]),
    ("property", "'${app.env}' ++ ", ["${app.env}"]),
    ("control", "", []),
]
# The edits a2m reads for sure, which must restore (refusing them would be a regression).
MUST_RESTORE = {
    ("single", "division"), ("single", "own lines"), ("single", "comment"), ("single", "property"),
    ("single", "control"), ("double", "division"), ("double", "own lines"), ("double", "comment"),
    ("double", "property"), ("double", "control"), ("script", "division"), ("script", "own lines"),
    ("script", "property"), ("script", "control"), ("json", "property"), ("json", "control"),
    ("template", "division"), ("template", "property"), ("template", "control"),
}


def _edits(context: str, shown: str, anchor: str, new: str) -> list[tuple[str, str, list[str]]]:
    """The answers for one context: each edit applied to the shown file, with the literals it adds."""
    answers: list[tuple[str, str, list[str]]] = []
    if context == "json":
        answers.append(("property", shown.replace(anchor, ', "env": "${app.env}"' + anchor, 1), ["env", "${app.env}"]))
        answers.append(("control", shown.replace(anchor, ', "on": "yes"' + anchor, 1), ["on", "yes"]))
        answers.append(("new", shown.replace(anchor, f', "extra": "{new}"' + anchor, 1), ["extra", "NEW"]))
        return answers
    for label, code, added in DW_EDITS:
        if label == "control":
            answers.append((label, shown.replace("vars.name", "vars.nick", 1), added))
        else:
            answers.append((label, shown.replace(anchor, code + anchor, 1), added))
    answers.append(("new", shown.replace(anchor, f"'{new}' ++ " + anchor, 1), ["NEW"]))
    if context in ("single", "double"):
        answers.append(("comment", shown.replace(')]"', ') // greet the caller]"', 1), []))
    if context == "script":
        answers.append(("comment", shown.replace("\n}]]>", " // greet the caller\n}]]>", 1), []))
    if context == "template":
        answers.append(("comment", shown.replace("vars.name]", "vars.name // greet]", 1), []))
    return answers


NEW_VALUE = "it's \"new\" \\ $(vars.x) $5"


@pytest.mark.parametrize("pair", VALUE_PAIRS, ids=[f"pair{index}" for index in range(len(VALUE_PAIRS))])
def test_CP10_X70_no_restore_writes_a_value_unescaped_into_a_string(pair: tuple[str, str]) -> None:
    """[CP10-X70] Every context and edit: the restore refuses, or every string literal of the restored part decodes
    exactly to the values it should hold (the two original values, the edit's own literals and a new placeholder's
    value), read by an independent decoder. The edits a2m reads for sure restore."""
    a, b = pair
    restored_count = 0
    for context, (original, strings, anchor) in _contexts(a, b).items():
        table, shown = _show(original)
        diff = table.diff(f"body $.x: expected {json.dumps(NEW_VALUE)}, actual 'z'")
        found = re.search(r'expected "(«v\d+»)"', diff)
        assert found is not None, diff
        expected_base = ["first", "second", "n", a, b] if context == "json" else [a, b]
        for label, answer, added in _edits(context, shown, anchor, found.group(1)):
            assert answer != shown or label == "control", (context, label)
            try:
                restored = table.restore("a.xml", answer)
            except PlaceholderError:
                assert (context, label) not in MUST_RESTORE, (context, label, answer)
                continue
            restored_count += 1
            expected = expected_base + [NEW_VALUE if item == "NEW" else item for item in added]
            assert Counter(strings(restored)) == Counter(expected), (context, label, restored)
    assert restored_count >= len(MUST_RESTORE)


# ------------------------------------------------------------------------------------------------ X71: line


D4_SCRIPT = """%dw 2.0
output application/json
---
{
  count: sizeOf(payload.items default []),
  status: 'ok',
  phone: (payload.phone default '') replace /[^0-9]/ with ''
}"""


def _d4() -> tuple[Placeholders, str, str]:
    original = (
        HEAD + "<ee:transform><ee:message><ee:set-payload><![CDATA[" + D4_SCRIPT
        + "]]></ee:set-payload></ee:message></ee:transform>" + TAIL
    )
    table, shown = _show(original)
    return table, shown, original


@pytest.mark.parametrize(
    "line",
    [
        'pages: ceil(sizeOf(payload.items default []) / 10) ++ " pages", // 10 per page',
        "pages: ceil(sizeOf(payload.items default []) / 10) ++ ' pages', // 10 per page",
    ],
)
def test_CP10_X71_an_added_line_whose_slash_closes_on_it_keeps_later_placeholders(line: str) -> None:
    """[CP10-X71] The reviewer's d4: the added line has a "/" a2m cannot read for sure, with a quote and a "//"
    comment after it that close on the line. The untouched status and regex below restore byte for byte (they were
    refused with "write the code you add on a line of its own", which the AI had done)."""
    table, shown, original = _d4()
    answer = shown.replace("{\n", "{\n  " + line + "\n", 1)
    assert table.restore("a.xml", answer) == original.replace("{\n", "{\n  " + line + "\n", 1)


def test_CP10_X71_a_quote_left_open_after_such_a_slash_names_the_line_above() -> None:
    """[CP10-X71] When the added line leaves a quote open after its unsure "/", what follows cannot be read for
    sure: the untouched placeholder below is refused, and the reason names that line and says it is above."""
    table, shown, _ = _d4()
    line = 'pages: ceil(sizeOf(payload.items default []) / 10) ++ " pages a/b'
    answer = shown.replace("{\n", "{\n  " + line + "\n", 1)
    with pytest.raises(PlaceholderError) as caught:
        table.restore("a.xml", answer)
    reason = str(caught.value)
    assert "below a line" in reason and line in reason, reason
    assert "on a line of its own" not in reason, reason


def test_CP10_X71_a_placeholder_on_the_added_line_after_such_a_slash_is_refused_naming_the_line() -> None:
    """[CP10-X71] A placeholder written on that line after the unsure "/" is refused: a2m cannot tell what it would
    be in, and the reason quotes that line and says to write the code on a line of its own."""
    table, shown, _ = _d4()
    status = re.search(r"status: '(«v\d+»)'", shown)
    assert status is not None
    line = f"pages: ceil(sizeOf(payload.items default []) / 10) ++ '{status.group(1)}', // 10 per page"
    answer = shown.replace("{\n", "{\n  " + line + "\n", 1)
    with pytest.raises(PlaceholderError) as caught:
        table.restore("a.xml", answer)
    reason = str(caught.value)
    assert '"/"' in reason and "line of its own" in reason and f"`{line}`" in reason, reason


# ------------------------------------------------------------------------------------------------ X72: pairing


WHEN = (
    '<when expression="#[(attributes.maskedRequestPath default &quot;&quot;) matches /\\/orders\\/.*/]">'
    '<logger level="INFO" message="#[vars.a]"/></when>'
)
CHOICE = HEAD + "<choice>" + WHEN + '<otherwise><logger level="INFO" message="#[vars.b]"/></otherwise></choice>' + TAIL
NEW_WHEN = "<when expression=\"#[attributes.method == 'OPTIONS']\"><logger level=\"INFO\" message=\"#[vars.c]\"/></when>"


def test_CP10_X72_a_new_when_before_an_edited_generated_when_keeps_its_regex() -> None:
    """[CP10-X72] The reviewer's r2: a new ``<when>`` inserted before a2m's generated one, whose expression is
    edited with the regex left as shown. Nothing in the start tags tells them apart, so they pair by content: the
    edited one restores byte for byte (it was refused, saying the AI moved the regex)."""
    table, shown = _show(CHOICE)
    answer = shown.replace("<choice>", "<choice>" + NEW_WHEN, 1).replace(
        "(attributes.maskedRequestPath", "(attributes.requestPath", 1
    )
    expected = CHOICE.replace("<choice>", "<choice>" + NEW_WHEN, 1).replace(
        "(attributes.maskedRequestPath", "(attributes.requestPath", 1
    )
    assert table.restore("a.xml", answer) == expected


def test_CP10_X72_two_parts_exactly_as_like_the_shown_one_are_refused_as_ambiguous() -> None:
    """[CP10-X72] Two ``<when>`` written where a2m showed one, each the shown one with one word changed: a2m cannot
    tell which stands for it, and the reason says so (not that a placeholder was moved)."""
    table, shown = _show(CHOICE)
    shown_when = shown[shown.index("<when") : shown.index("</when>") + len("</when>")]
    first = shown_when.replace("maskedRequestPath", "requestPath")
    second = shown_when.replace("maskedRequestPath", "rawRequestPath")
    answer = shown.replace(shown_when, first + second, 1)
    with pytest.raises(PlaceholderError) as caught:
        table.restore("a.xml", answer)
    reason = str(caught.value)
    assert "cannot tell which of them stands for the element it showed" in reason, reason


# ------------------------------------------------------------------------------------------------ X73: fallbacks


def test_CP10_X73_a_plain_value_that_would_make_a_property_placeholder_is_refused() -> None:
    """[CP10-X73] A value a2m showed as a placeholder, written inside a new ``${...}`` of a plain attribute, would
    make Mule read it as a property name (here a secure one): refused."""
    original = HEAD + '<set-variable variableName="greeting" value="secure::db.password"/>' + TAIL
    table, shown = _show(original)
    token = re.search(r'value="(«v\d+»)"', shown)
    assert token is not None
    with pytest.raises(PlaceholderError) as caught:
        table.restore("a.xml", shown.replace(f'value="{token.group(1)}"', f'value="${{{token.group(1)}}}"'))
    assert "property placeholder" in str(caught.value), str(caught.value)


def test_CP10_X73_a_value_that_starts_like_xml_but_is_not_refuses_a_new_placeholder() -> None:
    """[CP10-X73] A placeholder written into a value that starts with "<" but is not XML a2m can read: a2m cannot
    tell whether it is XML text or plain text, so it refuses (it was written as plain text)."""
    original = HEAD + '<set-payload value="&lt;a&gt;x &amp; y&lt;/a&gt;"/><logger level="INFO" message="a&lt;b"/>' + TAIL
    table, shown = _show(original)
    token = re.search(r'message="(«v\d+»)"', shown)
    assert token is not None
    answer = shown.replace('<set-payload value="', f'<set-payload value="&lt;b x=&quot;{token.group(1)}&quot;', 1)
    with pytest.raises(PlaceholderError) as caught:
        table.restore("a.xml", answer)
    assert "starts like XML" in str(caught.value), str(caught.value)

