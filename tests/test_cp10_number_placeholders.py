"""CP10 adversarial round 4: number placeholders are typed, so a number keeps its kind through the placeholder.

Round 3 hid every number of a condition and of custom code behind the same ``«vN»`` form a text gets. The AI could
not tell a number from a text, so it could not decline a numeric comparison, a2m restored the number as a TEXT
comparison CP5 refuses, and a number written bare came back exactly as the source spelled it (``5000L``, ``0xFF``,
``1_000_000``, a digit-led string), which is not DataWeave. Now a number literal gets ``«nN»`` and a text ``«vN»``:

* a condition that compares with a number is refused, as CP5 refuses it, whatever the AI answers;
* a number placeholder written where a number goes is the same number in a form DataWeave and JSON read, a negative
  one in parentheses in DataWeave; one that cannot be written exactly is refused;
* a text placeholder outside any string literal of code is refused, and so is a number placeholder inside a string
  literal or as data of the answer.

Only public entry points are used: ``Translator``, ``Place``, ``CalloutSource``, ``ItemKind`` and ``Placeholders``.

Case IDs:

* CP10-X29 - a condition shows a number as a number placeholder and a text (also a quoted "2") as a text placeholder.
* CP10-X30 - a condition comparing a number (mixed with a StartsWith) is refused whatever the AI answers: the number
  as the tree's value, or the number dropped.
* CP10-X31 - the legacy DataWeave condition field: a sign-carrying number placeholder written bare restores as a
  DataWeave number, and the translation is refused for the number, not for the placeholder.
* CP10-X32 - a number of JavaScript, Python or Java written bare in DataWeave comes back as the same number in
  DataWeave's form (L suffix, hexadecimal, octal, separators, BigInt, exponent with its sign).
* CP10-X33 - refused: a digit-led text written bare, a number inside a string literal, a number that cannot be
  written exactly, a number as data of the answer.
* CP10-X34 - a signed and an exponent JSON number written bare in a fix's DataWeave come back as those numbers.
* CP10-X35 - a number placeholder written as an attribute value comes back as the number.
* CP10-X36 - the same digits as a number and as a text get different placeholders, in code and in DataWeave.
* CP10-X37 - a callout answer that writes a number placeholder where a number goes gets the normalised number, and
  one that writes it inside quotes is refused.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from a2m.ai.placeholders import PlaceholderError, Placeholders
from a2m.ai.provider import ItemKind
from a2m.ai.sources import CalloutSource
from a2m.ai.translate import CalloutTranslated, ExpressionTranslated, Place, Translator

ANY_TOKEN = re.compile(r"«[vn]\d+»")
PLACE = Place("number-proxy", "default PreFlow request", "request", "AM-Before", "AM-After")


class Recorder:
    """A provider that records every request and answers with ``answer(request)``."""

    def __init__(self, answer: Any) -> None:
        self.requests: list[Any] = []
        self.answer = answer

    def complete(self, request: Any) -> str:
        self.requests.append(request)
        return str(self.answer(request))


def _translated(**fields: Any) -> str:
    return json.dumps({"status": "translated", "confidence": "high", "notes": "n", **fields})


def test_CP10_X29_a_condition_shows_a_number_as_a_number_placeholder_and_text_as_a_text_placeholder() -> None:
    """[CP10-X29] In a condition a number (as CP5 reads one: a digit or a sign first) is a number placeholder; a bare
    word and a quoted "2" are text placeholders, and the quoted "2" does not share the number's placeholder."""
    table = Placeholders()

    shown = table.condition(
        '(request.header.x = 2) and (request.header.y = abc) and (request.header.z = "2") and (request.header.w = -7.5)'
    )

    found = re.fullmatch(
        r'\(request\.header\.x = («n\d+»)\) and \(request\.header\.y = («v\d+»)\) and '
        r'\(request\.header\.z = "(«v\d+»)"\) and \(request\.header\.w = («n\d+»)\)',
        shown,
    )
    assert found is not None, shown
    number, word, text, signed = found.groups()
    assert table.values[number] == "2" and table.values[text] == "2" and number != text
    assert table.values[word] == "abc" and table.values[signed] == "-7.5"


MIXED = '(request.header.accept =| "application/json") and (request.queryparam.version = 2)'


def _mixed_tokens(request: Any) -> tuple[str, str]:
    found = re.search(r'=\| "(«[vn]\d+»)"\) and \(request\.queryparam\.version = («[vn]\d+»)\)', str(request.original))
    assert found is not None, request.original
    return found.group(1), found.group(2)


def _value_answer(request: Any) -> str:
    accept, version = _mixed_tokens(request)
    tree = {
        "and": [
            {"variable": "request.header.accept", "operator": "starts-with", "value": accept},
            {"variable": "request.queryparam.version", "operator": "equals", "value": version},
        ]
    }
    return _translated(condition=tree)


def _dropped_answer(request: Any) -> str:
    accept, _ = _mixed_tokens(request)
    return _translated(condition={"variable": "request.header.accept", "operator": "starts-with", "value": accept})


@pytest.mark.parametrize(
    "answer", [pytest.param(_value_answer, id="number-as-value"), pytest.param(_dropped_answer, id="number-dropped")]
)
def test_CP10_X30_a_condition_comparing_a_number_is_refused_whatever_the_ai_answers(answer: Any) -> None:
    """[CP10-X30] CP5 refuses this condition for its StartsWith, so the refusal does not name the number. Whether the
    AI writes the number's placeholder as a value (which would be a TEXT comparison, "2") or drops the comparison, a2m
    does not use the answer: the condition compares with a number, which is not translated. The number never reached
    the request."""
    provider = Recorder(answer)

    result = Translator(provider).expression("Flow v2", "v2", MIXED, PLACE, "StartsWith (=|) is not translated")

    assert not isinstance(result, ExpressionTranslated), result
    assert "number" in result.reason and "not translated" in result.reason, result.reason
    assert provider.requests == []  # CP10 round 5: never sent, so the number never reached a request


def test_CP10_X31_the_legacy_dataweave_field_restores_a_signed_number_and_is_refused_for_the_number() -> None:
    """[CP10-X31] A condition's number carries its sign inside its placeholder. Written bare in the legacy DataWeave
    field it restores as a DataWeave number (in parentheses), and Translator refuses the answer because the condition
    compares with a number, not because of where the placeholder stands."""
    table = Placeholders()
    shown = table.condition("request.header.x-pin = -265018394712")
    token = ANY_TOKEN.search(shown)
    assert token is not None, shown

    restored = table.restore_code(f"attributes.headers['x-pin'] == {token.group(0)}")

    assert restored == "attributes.headers['x-pin'] == (-265018394712)"

    def answer(request: Any) -> str:
        placeholder = ANY_TOKEN.search(str(request.original))
        assert placeholder is not None, request.original
        return _translated(dataweave=f"attributes.headers['x-pin'] == {placeholder.group(0)}")

    result = Translator(Recorder(answer)).expression(
        "Step RF-Pin", "RF-Pin", "request.header.x-pin = -265018394712", PLACE, "r"
    )

    assert not isinstance(result, ExpressionTranslated), result
    assert "number" in result.reason and "inside quotes" not in result.reason, result.reason


@pytest.mark.parametrize(
    ("kind", "code", "expected"),
    [
        pytest.param(ItemKind.JAVA, "long t = 5000L;", "5000", id="java-long"),
        pytest.param(ItemKind.JAVA, "int m = 0xFFFFFFFF;", "(-1)", id="java-hex-int"),
        pytest.param(ItemKind.JAVA, "double d = 2.5e-3d;", "2.5e-3", id="java-double-exponent"),
        pytest.param(ItemKind.JAVASCRIPT, "var m = 0xFF;", "255", id="javascript-hex"),
        pytest.param(ItemKind.JAVASCRIPT, "var o = 010;", "8", id="javascript-legacy-octal"),
        pytest.param(ItemKind.JAVASCRIPT, "var b = 9007199254740993n;", "9007199254740993", id="javascript-bigint"),
        pytest.param(ItemKind.JAVASCRIPT, "var e = 1e-5;", "1e-5", id="javascript-exponent"),
        pytest.param(ItemKind.PYTHON, "n = 1_000_000", "1000000", id="python-separators"),
        pytest.param(ItemKind.PYTHON, "n = 0o17", "15", id="python-octal"),
    ],
)
def test_CP10_X32_a_code_number_written_bare_in_dataweave_is_the_same_number_in_dataweaves_form(
    kind: ItemKind, code: str, expected: str
) -> None:
    """[CP10-X32] A number literal of custom code is one number placeholder (an exponent with its sign included); the
    AI writes it bare in DataWeave and a2m writes the same number as DataWeave reads it, never the source spelling."""
    table = Placeholders()
    shown = table.code(code, kind)

    tokens = ANY_TOKEN.findall(shown)
    assert len(tokens) == 1 and tokens[0].startswith("«n"), shown
    assert re.search(r"\d", ANY_TOKEN.sub("", shown)) is None and re.search(r"»-|-«", shown) is None, shown
    assert table.restore_code(f"vars.x == {tokens[0]}") == f"vars.x == {expected}"


def _bare_text(table: Placeholders) -> None:
    shown = table.code('String k = "9f3a";', ItemKind.JAVA)
    token = ANY_TOKEN.search(shown)
    assert token is not None, shown
    table.restore_code(f"vars.k == {token.group(0)}")


def _number_in_string(table: Placeholders) -> None:
    shown = table.code("long t = 5000L;", ItemKind.JAVA)
    token = ANY_TOKEN.search(shown)
    assert token is not None, shown
    table.restore_code(f"vars.t == '{token.group(0)}'")


def _inexact_float(table: Placeholders) -> None:
    shown = table.code("float f = 1.1f;", ItemKind.JAVA)
    token = ANY_TOKEN.search(shown)
    assert token is not None, shown
    table.restore_code(f"vars.f == {token.group(0)}")


def _inexact_double_integer(table: Placeholders) -> None:
    shown = table.code("var big = 9007199254740993;", ItemKind.JAVASCRIPT)
    token = ANY_TOKEN.search(shown)
    assert token is not None, shown
    table.restore_code(f"vars.big == {token.group(0)}")


def _number_as_data(table: Placeholders) -> None:
    shown = table.code("var t = 5000;", ItemKind.JAVASCRIPT)
    token = ANY_TOKEN.search(shown)
    assert token is not None, shown
    table.restore_plain(token.group(0))


@pytest.mark.parametrize(
    "attempt",
    [
        pytest.param(_bare_text, id="digit-led-text-bare"),
        pytest.param(_number_in_string, id="number-inside-a-string"),
        pytest.param(_inexact_float, id="java-float-not-exact"),
        pytest.param(_inexact_double_integer, id="javascript-integer-past-a-double"),
        pytest.param(_number_as_data, id="number-as-data"),
    ],
)
def test_CP10_X33_placeholders_that_cannot_stand_where_written_are_refused(attempt: Any) -> None:
    """[CP10-X33] Refused with a PlaceholderError, never written back as the source spelled it: a text that starts
    with a digit written bare in DataWeave, a number inside a DataWeave string literal (a text of it), a Java float or
    a JavaScript integer whose value is not the decimal written, and a number as data of the answer."""
    with pytest.raises(PlaceholderError):
        attempt(Placeholders())


def test_CP10_X34_a_signed_and_an_exponent_json_number_written_bare_in_a_fixs_dataweave_are_those_numbers() -> None:
    """[CP10-X34] JSON numbers keep their sign and exponent inside their placeholder; written bare in a fix's
    DataWeave they come back as those numbers (the negative one in parentheses), not refused or quoted."""
    table = Placeholders(["main", "x"])
    policy = table.apigee(
        '<AssignMessage name="AM-Json"><Set><Payload contentType="application/json">'
        '{"offset": -829406175302, "rate": 1.5e-3}</Payload></Set></AssignMessage>'
    )
    assert policy is not None
    found = re.search(r'\{"«v\d+»": («[vn]\d+»), "«v\d+»": («[vn]\d+»)\}', policy)
    assert found is not None, policy
    mule = '<mule>\n  <flow name="main">\n    <set-variable variableName="x" value="#[vars.base]"/>\n  </flow>\n</mule>\n'
    shown = table.mule({"f.xml": mule})["f.xml"]
    assert shown is not None and "#[vars.base]" in shown, shown

    edited = shown.replace("#[vars.base]", f"#[vars.base + {found.group(1)} * {found.group(2)}]")

    assert table.restore("f.xml", edited) == mule.replace("#[vars.base]", "#[vars.base + (-829406175302) * 1.5e-3]")


def test_CP10_X35_a_number_placeholder_written_as_an_attribute_value_comes_back_as_the_number() -> None:
    """[CP10-X35] A Java long written as a Mule attribute value (a number setting) comes back as the number DataWeave
    and Mule read, not with its L suffix."""
    table = Placeholders(["main", "t"])
    code = table.code("long timeout = 5000L;", ItemKind.JAVA)
    token = ANY_TOKEN.search(code)
    assert token is not None, code
    mule = '<mule>\n  <flow name="main">\n    <set-variable variableName="t" value="#[vars.t]"/>\n  </flow>\n</mule>\n'
    shown = table.mule({"f.xml": mule})["f.xml"]
    assert shown is not None

    edited = shown.replace('value="#[vars.t]"', f'value="{token.group(0)}"')

    assert table.restore("f.xml", edited) == mule.replace('value="#[vars.t]"', 'value="5000"')


def test_CP10_X36_the_same_digits_as_a_number_and_as_a_text_get_different_placeholders() -> None:
    """[CP10-X36] The digits 5000 as a number and as a quoted text get different placeholders (a number one and a
    text one), in custom code and in a Mule file's DataWeave, so the AI sees which is which."""
    table = Placeholders(["main", "ok"])
    code = table.code('var a = 5000; var b = "5000";', ItemKind.JAVASCRIPT)
    mule = '<mule><flow name="main"><set-variable variableName="ok" value="#[vars.a == 5000 or vars.b == \'5000\']"/></flow></mule>'
    shown = table.mule({"f.xml": mule})["f.xml"]

    found = re.fullmatch(r'var a = («n\d+»); var b = "(«v\d+»)";', code)
    assert found is not None, code
    assert found.group(1) != found.group(2)
    assert shown is not None
    dataweave = re.search(r"vars\.a == («n\d+») or vars\.b == '(«v\d+»)'", shown)
    assert dataweave is not None, shown
    assert table.values[dataweave.group(1)] == "5000" and table.values[dataweave.group(2)] == "5000"


@pytest.mark.parametrize(
    ("template", "translated"),
    [
        pytest.param("#[vars.count + {token}]", True, id="where-a-number-goes"),
        pytest.param("#[vars.count ++ '{token}']", False, id="inside-quotes"),
    ],
)
def test_CP10_X37_a_callout_answer_writes_a_code_number_where_a_number_goes(template: str, translated: bool) -> None:
    """[CP10-X37] A JavaScript hexadecimal number shown as a number placeholder: an answer that writes it where a
    number goes gets 255 back; one that writes it inside quotes (a text of the number) is not used."""
    code = "var mask = 0xFF;\ncontext.setVariable('masked', mask);\n"

    def answer(request: Any) -> str:
        shown = str(request.original)
        number = re.search(r"var mask = («[vn]\d+»);", shown)
        name = re.search(r"context\.setVariable\('(«v\d+»)'", shown)
        assert number is not None and name is not None, shown
        value = template.format(token=number.group(1))
        mule = f'<set-variable variableName="{name.group(1)}" value="{value}"/>'
        return _translated(mule=mule, writes={"variables": [name.group(1)]})

    result = Translator(Recorder(answer)).callout(
        CalloutSource(ItemKind.JAVASCRIPT, code, "mask.js"), "JS-Mask", "Javascript", '<Javascript name="JS-Mask"/>',
        PLACE,
    )

    if translated:
        assert isinstance(result, CalloutTranslated), result
        (processor,) = result.processors
        assert processor.get("value") == "#[vars.count + 255]"
    else:
        assert not isinstance(result, CalloutTranslated), result
        assert "number" in result.reason, result.reason
