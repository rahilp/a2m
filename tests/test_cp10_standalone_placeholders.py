"""CP10 adversarial round 6: one rule for where a placeholder may stand, honest records for a condition not sent.

* A placeholder outside a string literal (and a number placeholder anywhere) must stand alone as a whole token. One
  joined to a letter, a digit, ".", "_", "$", a quote or another placeholder is refused, whatever language its value
  came from: the fix loop's own DataWeave numbers (``.«n2»`` for 5 was written back as 0.5, ``«n2»«n3»`` for 5 and 7
  as 57) and a policy's JSON numbers in a callout's Mule answer, as much as a number of custom code.
* A placeholder inside a DataWeave string's ``$( )`` interpolation is code: a text placeholder there must stand in
  quotes of its own (spelled for those quotes), a number placeholder goes back as a number, and a bare text placeholder
  is refused, never pasted in as code.
* A number placeholder in a plain Mule attribute (not a ``#[...]`` expression) is written in plain digits, as Mule reads
  an attribute number (``5e3`` and ``5000.0`` are ``5000``), and refused when that needs too many digits.
* A condition a2m refuses without asking the AI (it compares with a number) is recorded as ``skipped``, with CP5's
  reason and the comparison named, and run.log says it was not sent to the AI.

Case IDs:

* CP10-X45 - the fix loop's path (``Placeholders.mule`` and ``restore``): a joined placeholder is refused.
* CP10-X46 - the translation path (``Translator.callout``): a joined placeholder of the policy's JSON is refused.
* CP10-X47 - a placeholder in a DataWeave ``$( )`` interpolation is restored as code, never as raw code of a text.
* CP10-X48 - a number placeholder in a plain Mule attribute is written in plain digits.
* CP10-X49 - a condition with a number: no request, recorded ``skipped``, run.log says it was not sent.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from a2m.ai.placeholders import PlaceholderError, Placeholders
from a2m.ai.provider import ItemKind
from a2m.ai.sources import CalloutSource
from a2m.ai.translate import CalloutTranslated, NotTranslated, Place, Translator

TESTS = Path(__file__).resolve().parent
ODD_BUNDLE = TESTS / "fixtures" / "apigee" / "cp6" / "odd-condition"
ODD_CONDITION = "request.header.User-Agent =| &quot;curl&quot;"
MIXED_CONDITION = '(request.header.User-Agent =| "curl") and (request.queryparam.v = 2)'
PLACE = Place("glue-proxy", "default PreFlow request", "request", "AM-Before", "AM-After")
ANY_TOKEN = re.compile(r"«[vn]\d+»")


class Recorder:
    """A provider that records every request and answers with ``answer(request)``."""

    def __init__(self, answer: Any) -> None:
        self.requests: list[Any] = []
        self.answer = answer

    def complete(self, request: Any) -> str:
        self.requests.append(request)
        return str(self.answer(request))


def _tokens(shown: str) -> list[str]:
    return ANY_TOKEN.findall(shown)


# ------------------------------------------------------------------------------------------------ X45: fix loop


DW_FILE = '<set-variable variableName="r" value="#[vars.a * 5 + 7]"/>'
DW_DOT_FILE = '<set-variable variableName="r" value="#[vars.a * .07]"/>'
JSON_FILE = """<set-payload value='{"a": 5, "b": NaN}'/>"""


@pytest.mark.parametrize(
    ("xml", "shown_expression", "joined"),
    [
        pytest.param(DW_FILE, "{n0} + {n1}", ".{n0}", id="dot-before-dataweave-integer"),
        pytest.param(DW_FILE, "{n0} + {n1}", "{n0}{n1}", id="two-numbers-joined"),
        pytest.param(DW_FILE, "{n0} + {n1}", "{n0}abc", id="word-after"),
        pytest.param(DW_FILE, "{n0} + {n1}", "{n0}e3", id="exponent-after"),
        pytest.param(DW_FILE, "{n0} + {n1}", "{n0}_0", id="underscore-after"),
        pytest.param(DW_FILE, "{n0} + {n1}", "x{n0}", id="word-before"),
        pytest.param(DW_FILE, "{n0} + {n1}", "{n0}$", id="dollar-after"),
        pytest.param(DW_DOT_FILE, "{n0}", ".{n0}", id="dot-before-leading-dot-number"),
        pytest.param(JSON_FILE, "{n1}}}", "{n1}x}}", id="json-text-placeholder-word-after"),
        pytest.param(JSON_FILE, "{n0},", "{n0}{n1},", id="json-number-and-word-joined"),
    ],
)
def test_CP10_X45_the_fix_loop_refuses_a_placeholder_that_does_not_stand_alone(
    xml: str, shown_expression: str, joined: str
) -> None:
    """[CP10-X45] In the fix loop the numbers of the app's own DataWeave and JSON are restored into the same language.
    A placeholder written joined to a letter, a digit, ".", "_", "$" or another placeholder would make another value
    of it (``.«n2»`` for 5 is 0.5, ``«n2»«n3»`` for 5 and 7 is 57, ``«v3»x`` for NaN is NaNx): refused, not written
    back silently."""
    table = Placeholders()
    shown = table.mule({"flow.xml": xml})["flow.xml"]
    assert shown is not None
    numbers = [token for token in _tokens(shown) if token.startswith("«n")]
    # n0, n1: the numbers of the DataWeave; in the JSON text the number and the text placeholder of NaN.
    names = dict(zip(("n0", "n1"), numbers + _tokens(shown)[-1:] if xml == JSON_FILE else numbers, strict=False))
    before = shown_expression.format(**names)
    assert before in shown, (before, shown)
    answer = shown.replace(before, joined.format(**names))

    with pytest.raises(PlaceholderError, match="alone"):
        table.restore("flow.xml", answer)


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        pytest.param("({n0})", "(5)", id="in-parentheses"),
        pytest.param("{n1} - {n0}", "7 - 5", id="operator-between"),
        pytest.param("[{n0},{n1}]", "[5,7]", id="list"),
        pytest.param("-{n0}", "-5", id="unary-minus"),
    ],
)
def test_CP10_X45_a_placeholder_that_stands_alone_still_restores(expression: str, expected: str) -> None:
    """[CP10-X45] (guard) A number placeholder with a space, an operator or a bracket next to it is written back as its
    number: the rule refuses only a joined placeholder."""
    table = Placeholders()
    shown = table.mule({"flow.xml": DW_FILE})["flow.xml"]
    assert shown is not None
    n0, n1 = [token for token in _tokens(shown) if token.startswith("«n")]
    answer = shown.replace(f"{n0} + {n1}", expression.format(n0=n0, n1=n1))

    assert table.restore("flow.xml", answer) == DW_FILE.replace("5 + 7", expected)


# ------------------------------------------------------------------------------------------------ X46: translation


POLICY = (
    '<Javascript name="JS-Cfg"><Properties><Property name="cfg">{"limit": 5, "rate": 7, "on": NaN}</Property>'
    "</Properties><ResourceURL>jsc://cfg.js</ResourceURL></Javascript>"
)


@pytest.mark.parametrize(
    "joined",
    [
        pytest.param(".{limit}", id="dot-before"),
        pytest.param("{limit}{rate}", id="two-numbers-joined"),
        pytest.param("{limit}abc", id="word-after"),
        pytest.param("{limit}e3", id="exponent-after"),
        pytest.param("{on}x", id="text-placeholder-word-after"),
    ],
)
def test_CP10_X46_a_callout_answer_with_a_placeholder_that_does_not_stand_alone_is_not_used(joined: str) -> None:
    """[CP10-X46] The translation path: a callout's policy holds JSON whose numbers and words are shown as
    placeholders. The AI's Mule answer writes them back into JSON text, the language they came from; one joined to
    something that changes its value (57 for 5 and 7, 5e3 for 5) makes the answer unusable, never used as written."""
    seen: dict[str, str] = {}

    def answer(request: Any) -> str:
        policy = str(request.prompt)
        found = re.search(r"\{\"«v\d+»\": («n\d+»), \"«v\d+»\": («n\d+»), \"«v\d+»\": («v\d+»)\}", policy)
        assert found is not None, policy
        seen.update(limit=found.group(1), rate=found.group(2), on=found.group(3))
        value = "{&quot;limit&quot;: " + joined.format(**seen) + "}"
        mule = f'<set-variable variableName="a2mCfg" value="{value}"/>'
        return json.dumps(
            {"status": "translated", "confidence": "high", "notes": "n", "mule": mule,
             "writes": {"variables": ["a2mCfg"]}}
        )

    provider = Recorder(answer)
    source = CalloutSource(ItemKind.JAVASCRIPT, "var cfg = context.getVariable('cfg');\n", "cfg.js")

    result = Translator(provider).callout(source, "JS-Cfg", "Javascript", POLICY, PLACE)

    assert len(provider.requests) == 1
    assert isinstance(result, NotTranslated), result
    assert "alone" in result.reason, result.reason


# ------------------------------------------------------------------------------------------------ X47: $( )


def test_CP10_X47_a_bare_text_placeholder_in_an_interpolation_is_refused() -> None:
    """[CP10-X47] The code of a DataWeave string's ``$( )`` is code: a text placeholder written there bare would be
    pasted in as code (``"$(Bearer) x"``, an unresolved reference), so it is refused."""
    table = Placeholders()
    shown = table.code("var a = 'Bearer';", ItemKind.JAVASCRIPT)
    (token,) = _tokens(shown)

    with pytest.raises(PlaceholderError, match="outside any string literal"):
        table.restore_code(f'"$({token}) x"')


def test_CP10_X47_a_bare_text_placeholder_in_an_interpolation_is_refused_in_a_mule_file() -> None:
    """[CP10-X47] The same in the fix loop's whole-file restore: a text placeholder bare in ``$( )`` is refused."""
    table = Placeholders()
    xml = "<set-variable variableName=\"r\" value=\"#['k']\"/>"
    shown = table.mule({"flow.xml": xml})["flow.xml"]
    assert shown is not None
    token = _tokens(shown)[-1]
    answer = shown.replace(f"'{token}'", f"&quot;$({token})&quot;")

    with pytest.raises(PlaceholderError, match="outside any string literal"):
        table.restore("flow.xml", answer)


def test_CP10_X47_a_quoted_text_placeholder_in_an_interpolation_is_spelled_for_its_own_quotes() -> None:
    """[CP10-X47] A text placeholder in quotes of its own inside ``$( )`` is spelled for those quotes (the inner
    ``'``), not for the outer string's: ``O'Brien`` becomes ``'O\\'Brien'``."""
    table = Placeholders()
    shown = table.code('var a = "O\'Brien";', ItemKind.JAVASCRIPT)
    (token,) = _tokens(shown)

    assert table.restore_code(f"\"$('{token}') x\"") == "\"$('O\\'Brien') x\""


def test_CP10_X47_a_number_placeholder_in_an_interpolation_is_restored_as_a_number() -> None:
    """[CP10-X47] A number placeholder in the code of ``$( )`` goes back as the number, as anywhere in code."""
    table = Placeholders()
    shown = table.code("var t = 0x10;", ItemKind.JAVASCRIPT)
    (token,) = _tokens(shown)

    assert table.restore_code(f'"total: $(vars.a * {token})"') == '"total: $(vars.a * 16)"'


# ------------------------------------------------------------------------------------------------ X48: attributes


@pytest.mark.parametrize(
    ("kind", "code", "expected"),
    [
        pytest.param(ItemKind.JAVASCRIPT, "var t = 5e3;", "5000", id="javascript-exponent"),
        pytest.param(ItemKind.JAVASCRIPT, "var t = 5000.0;", "5000", id="javascript-trailing-zero"),
        pytest.param(ItemKind.PYTHON, "t = 5_000.00", "5000", id="python-separators"),
        pytest.param(ItemKind.JAVASCRIPT, "var t = 2.5e-3;", "0.0025", id="javascript-fraction-exponent"),
    ],
)
def test_CP10_X48_a_number_in_a_plain_mule_attribute_is_written_in_plain_digits(
    kind: ItemKind, code: str, expected: str
) -> None:
    """[CP10-X48] ``responseTimeout="«n1»"`` is a plain Mule attribute, which Mule reads as an integer: the number is
    written in plain digits (``5e3`` and ``5000.0`` are ``5000``), never in DataWeave's form, which fails to deploy.
    In a ``#[...]`` expression of the same file it keeps DataWeave's form."""
    table = Placeholders()
    (token,) = _tokens(table.code(code, kind))
    answer = f'<http:request path="/x" responseTimeout="{token}" doc:name="#[vars.a * {token}]"/>'

    restored = table.restore("flow.xml", answer)

    assert f'responseTimeout="{expected}"' in restored, restored


@pytest.mark.parametrize(
    "number",
    [
        pytest.param("1e300", id="too-many-integer-digits"),
        pytest.param("1e-300", id="too-many-fraction-digits"),
    ],
)
def test_CP10_X48_a_number_plain_digits_cannot_write_is_refused(number: str) -> None:
    """[CP10-X48] A number (here of a JSON payload, which keeps its exact spelling) that needs hundreds of digits in
    plain form is refused in a plain attribute, not written with an exponent Mule cannot read there."""
    table = Placeholders()
    shown = table.mule({"flow.xml": f"<set-payload value='{{\"a\": {number}}}'/>"})["flow.xml"]
    assert shown is not None
    token = _tokens(shown)[-1]
    assert token.startswith("«n"), shown

    with pytest.raises(PlaceholderError, match="plain digits"):
        table.restore("flow.xml", f'<http:request path="/x" responseTimeout="{token}"/>')


def test_CP10_X48_a_number_joined_to_text_in_a_plain_attribute_is_refused() -> None:
    """[CP10-X48] In a plain attribute too a number placeholder must stand alone: ``«n1»0`` would be another
    number."""
    table = Placeholders()
    (token,) = _tokens(table.code("var t = 5;", ItemKind.JAVASCRIPT))

    with pytest.raises(PlaceholderError, match="alone"):
        table.restore("flow.xml", f'<http:request path="/x" responseTimeout="{token}0"/>')


# ------------------------------------------------------------------------------------------------ X49: not sent


def _mixed_bundle(tmp_path: Path) -> Path:
    exports = tmp_path / "in"
    bundle = exports / "odd-condition"
    shutil.copytree(ODD_BUNDLE, bundle)
    proxy = bundle / "apiproxy" / "proxies" / "default.xml"
    text = proxy.read_text(encoding="utf-8")
    mixed = MIXED_CONDITION.replace('"', "&quot;")
    assert f"<Condition>{ODD_CONDITION}</Condition>" in text
    proxy.write_text(text.replace(f"<Condition>{ODD_CONDITION}</Condition>", f"<Condition>{mixed}</Condition>"),
                     encoding="utf-8")
    return exports


def test_CP10_X49_a_condition_with_a_number_is_recorded_skipped_not_ai(tmp_path: Path) -> None:
    """[CP10-X49] The generator records a condition it refuses without asking the AI as ``skipped`` (not ``ai``), with
    CP5's reason and the comparison it refused named (never a placeholder), and needs no provider request."""
    from a2m.generator import generate_project
    from a2m.parser import read_bundle

    exports = _mixed_bundle(tmp_path)
    provider = Recorder(lambda request: json.dumps({"status": "declined", "reason": "never asked"}))

    result = generate_project(
        read_bundle(exports / "odd-condition"), tmp_path / "out" / "odd-condition" / "mule-app", shared_flows=(),
        results_root=tmp_path / "out", provider=provider,
    )

    (record,) = [r for r in result.conditions if r.name == "curl-clients"]
    assert [r.kind for r in provider.requests if r.kind is ItemKind.EXPRESSION] == []
    assert record.method.value == "skipped", record
    assert record.ok is False and record.dw is None
    assert "StartsWith" in str(record.reason), record.reason
    assert "not sent to the AI" in str(record.reason), record.reason
    assert "request.queryparam.v = 2" in str(record.reason), record.reason
    assert ANY_TOKEN.search(str(record.reason)) is None, record.reason


def test_CP10_X49_run_log_says_the_condition_was_not_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_cli: Any
) -> None:
    """[CP10-X49] Through the CLI (``a2m migrate --llm fake --no-runtime``): the fake provider gets no expression
    request, and run.log logs the condition on the "can't be translated" line, saying it was not sent to the AI, never
    "condition sent to the AI"."""
    from a2m.ai.fake import FakeProvider

    asked: list[Any] = []
    complete = FakeProvider.complete

    def recording(self: FakeProvider, request: Any) -> str:
        asked.append(request)
        return complete(self, request)

    monkeypatch.setattr(FakeProvider, "complete", recording)
    monkeypatch.delenv("A2M_FAKE_LLM_DIR", raising=False)
    exports = _mixed_bundle(tmp_path)
    results = tmp_path / "results"

    res = run_cli(["migrate", str(exports), "--out", str(results), "--llm", "fake", "--no-runtime"])

    assert res.code == 0, res.err
    assert [r for r in asked if r.kind is ItemKind.EXPRESSION] == []
    lines = [line for line in (results / "run.log").read_text(encoding="utf-8").splitlines() if "curl-clients" in line]
    assert len(lines) == 1, lines
    assert "condition sent to the AI" not in lines[0], lines[0]
    assert "can't be translated" in lines[0] and "not sent to the AI" in lines[0], lines[0]
    assert "request.queryparam.v = 2" in lines[0], lines[0]


def test_CP10_X49_a_translator_refusal_without_a_request_says_it_was_not_sent() -> None:
    """[CP10-X49] ``Translator.expression`` marks the result of a condition it does not send (``sent`` is False), so
    no caller can record it as an AI result; one it sends is marked sent."""
    provider = Recorder(lambda request: json.dumps({"status": "declined", "reason": "no"}))
    translator = Translator(provider)

    skipped = translator.expression("Flow v", "v", "request.queryparam.v = 2", PLACE, "comparing with a number")
    sent = translator.expression("Flow ua", "ua", 'request.header.ua =| "curl"', PLACE, "StartsWith")

    assert isinstance(skipped, NotTranslated) and skipped.sent is False, skipped
    assert isinstance(sent, NotTranslated) and sent.sent is True, sent
    assert len(provider.requests) == 1
    assert not isinstance(sent, CalloutTranslated)
