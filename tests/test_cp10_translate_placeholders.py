"""CP10: the AI translation of custom code and conditions never sends a literal value to the provider.

Rahil's decision (option A, CP10 adversarial round 1, host-alt A1 findings): a2m.ai.translate shows the AI a callout's
code, its included scripts and its policy XML, and a condition, through the same placeholder machinery as the fix
loop (default deny, ``«vN»`` placeholders), and writes the exact values back into the AI's answer before CP6's checks
run. Only public entry points are used: ``Translator``, ``Place``, ``CalloutSource`` and ``ItemKind``.

Case IDs:

* CP10-X05 .. X07 - a JavaScript, Python or Java callout with a credential in its code, an included script and its
  policy XML: no planted value reaches any field of the AiRequest.
* CP10-X08 - a condition with a credential literal, and a refusal reason that repeats it: neither reaches the request.
* CP10-X09 - a canned translation written with the placeholders is restored byte for byte: an attribute value, a
  DataWeave string literal and the declared writes.
* CP10-X10 - a structured condition answer with a placeholder is restored to the exact value.
* CP10-X11 - an answer with a placeholder a2m never showed is refused (needs review, never used).
"""

from __future__ import annotations

import json
import re
from dataclasses import fields
from typing import Any

import pytest

from a2m.ai.provider import ItemKind
from a2m.ai.sources import CalloutSource
from a2m.ai.translate import CalloutTranslated, ExpressionTranslated, NotTranslated, Place, Translator

CODE_KEY = "CANARY-CODEKEY-3H8J2K6L"
INCLUDE_KEY = "CANARY-INCKEY-9Q4W7E1R"
POLICY_KEY = "CANARY-POLKEY-5T2Y8U3I"
COND_KEY = "CANARY-CONDKEY-6O1P4A9S"
PLANTED = (CODE_KEY, INCLUDE_KEY, POLICY_KEY, COND_KEY)
TOKEN = re.compile(r"«v\d+»")
PLACE = Place("canary-proxy", "default PreFlow request", "request", "AM-Before", "AM-After")

SOURCES = {
    ItemKind.JAVASCRIPT: (
        f'var apiKey = "{CODE_KEY}";\ncontext.setVariable("corr.key", apiKey); // key {CODE_KEY}\n',
        f"function key() {{ return '{INCLUDE_KEY}'; }}\n",
        "Javascript",
    ),
    ItemKind.PYTHON: (
        f'secret = "{CODE_KEY}"\nflow.setVariable("corr.key", secret)  # {CODE_KEY}\n',
        f"KEY = '{INCLUDE_KEY}'\n",
        "Script",
    ),
    ItemKind.JAVA: (
        (
            'public class Sign implements Execution {\n'
            f'  private static final String KEY = "{CODE_KEY}";\n'
            '  public ExecutionResult execute(MessageContext c, ExecutionContext e) {\n'
            '    c.setVariable("corr.key", KEY); return ExecutionResult.SUCCESS; }\n}\n'
        ),
        f'class Helper {{ static String k() {{ return "{INCLUDE_KEY}"; }} }}\n',
        "JavaCallout",
    ),
}


class Recorder:
    """A provider that records every request and answers with ``answer(request)``."""

    def __init__(self, answer: Any = None) -> None:
        self.requests: list[Any] = []
        self.answer = answer

    def complete(self, request: Any) -> str:
        self.requests.append(request)
        if self.answer is None:
            return json.dumps({"status": "cannot_translate", "reason": "test"})
        return str(self.answer(request))


def _policy_xml(policy_type: str) -> str:
    return (
        f'<{policy_type} name="CB-Sign"><Properties><Property name="apiKey">{POLICY_KEY}</Property></Properties>'
        f"<ResourceURL>x://sign</ResourceURL></{policy_type}>"
    )


def _texts(request: Any) -> dict[str, str]:
    return {item.name: str(getattr(request, item.name)) for item in fields(request)}


def _callout(kind: ItemKind, provider: Recorder) -> CalloutTranslated | NotTranslated:
    code, include, policy_type = SOURCES[kind]
    source = CalloutSource(kind, code, "sign", (("helper", include),))
    return Translator(provider).callout(source, "CB-Sign", policy_type, _policy_xml(policy_type), PLACE)


def _token_for(text: str, before: str) -> str:
    """The placeholder that follows ``before`` in ``text`` (as shown to the AI)."""
    match = re.search(re.escape(before) + r"\s*[\"']?(«v\d+»)", text)
    assert match is not None, f"no placeholder after {before!r} in:\n{text}"
    return match.group(1)


@pytest.mark.parametrize(
    "kind",
    [
        pytest.param(ItemKind.JAVASCRIPT, id="CP10-X05-javascript"),
        pytest.param(ItemKind.PYTHON, id="CP10-X06-python"),
        pytest.param(ItemKind.JAVA, id="CP10-X07-java"),
    ],
)
def test_CP10_X05_X07_callout_literals_never_reach_the_request(kind: ItemKind) -> None:
    """[CP10-X05..X07] No credential of the code, an included script or the policy XML reaches any AiRequest field."""
    provider = Recorder()

    _callout(kind, provider)

    assert len(provider.requests) == 1
    request = provider.requests[0]
    for field, text in _texts(request).items():
        for value in PLANTED:
            assert value not in text, f"{value} reached the request field {field!r}"
    assert TOKEN.search(str(request.prompt)) is not None
    assert "CB-Sign" in str(request.prompt) and "AM-Before" in str(request.prompt)


def test_CP10_X08_condition_literal_never_reaches_the_request() -> None:
    """[CP10-X08] A condition's literal, also repeated in a2m's refusal reason, never reaches the AiRequest."""
    provider = Recorder()
    original = f'request.header.x-client-id =| "{COND_KEY}"'

    Translator(provider).expression(
        "Step RF-Odd", "RF-Odd", original, PLACE, f"the operator =| on '{COND_KEY}' is not supported"
    )

    assert len(provider.requests) == 1
    request = provider.requests[0]
    for field, text in _texts(request).items():
        assert COND_KEY not in text, f"the condition literal reached the request field {field!r}"
    assert "request.header.x-client-id" in str(request.original)


def test_CP10_X09_canned_translation_with_placeholders_is_restored_exactly() -> None:
    """[CP10-X09] Placeholders in the answer's attribute values, DataWeave string literals and declared writes are
    written back as the exact values the code holds."""
    tricky = "O'Re&lly <k> \"q\""
    code = f"var apiKey = '{CODE_KEY}';\nvar odd = \"O'Re&lly <k> \\\"q\\\"\";\ncontext.setVariable('corr.key', apiKey);\n"

    def answer(request: Any) -> str:
        shown = str(request.original)
        key = _token_for(shown, "var apiKey =")
        odd = _token_for(shown, "var odd =")
        name = _token_for(shown, "context.setVariable(")
        mule = (
            f'<set-variable variableName="{name}" value="#[\'{key}\']"/>'
            f'<set-variable variableName="a2mOdd" value="{odd}"/>'
        )
        return json.dumps(
            {"status": "translated", "confidence": "high", "notes": "n", "mule": mule,
             "writes": {"variables": [name, "a2mOdd"]}}
        )

    provider = Recorder(answer)
    source = CalloutSource(ItemKind.JAVASCRIPT, code, "sign.js")

    result = Translator(provider).callout(source, "JS-Sign", "Javascript", '<Javascript name="JS-Sign"/>', PLACE)

    assert isinstance(result, CalloutTranslated), result
    first, second = result.processors
    assert first.get("variableName") == "corr.key"
    assert first.get("value") == f"#['{CODE_KEY}']"
    assert second.get("value") == tricky
    assert result.writes is not None, result.writes_note
    assert result.writes.variables == frozenset({"corr.key", "a2mOdd"})
    assert CODE_KEY not in str(provider.requests[0].prompt)


def test_CP10_X10_structured_condition_placeholder_is_restored() -> None:
    """[CP10-X10] A placeholder as the value of a structured condition comes back as the exact literal."""
    original = f'request.header.User-Agent =| "{COND_KEY}"'

    def answer(request: Any) -> str:
        token = _token_for(str(request.original), "=|")
        condition = {"variable": "request.header.User-Agent", "operator": "starts-with", "value": token}
        return json.dumps({"status": "translated", "confidence": "high", "notes": "n", "condition": condition})

    provider = Recorder(answer)

    result = Translator(provider).expression("Flow curl", "curl", original, PLACE, "unsupported operator")

    assert isinstance(result, ExpressionTranslated), result
    assert f'"{COND_KEY}"' in result.dataweave
    assert TOKEN.search(result.dataweave) is None
    assert COND_KEY not in str(provider.requests[0].prompt)


@pytest.mark.parametrize(
    "mule",
    [
        pytest.param('<set-variable variableName="x" value="«v999»"/>', id="attribute"),
        pytest.param("<set-variable variableName=\"x\" value=\"#['«v999»']\"/>", id="dataweave-literal"),
    ],
)
def test_CP10_X11_unknown_placeholder_is_refused(mule: str) -> None:
    """[CP10-X11] An answer using a placeholder a2m did not show is unusable: needs review, never used."""
    code = f"var apiKey = '{CODE_KEY}';\n"

    def answer(request: Any) -> str:
        assert TOKEN.search(str(request.original)) is not None, "the code was sent without placeholders"
        return json.dumps({"status": "translated", "confidence": "high", "notes": "n", "mule": mule})

    provider = Recorder(answer)
    source = CalloutSource(ItemKind.JAVASCRIPT, code, "sign.js")

    result = Translator(provider).callout(source, "JS-Sign", "Javascript", '<Javascript name="JS-Sign"/>', PLACE)

    assert isinstance(result, NotTranslated), result
    assert "«v999»" in result.reason and "could not be used" in result.reason
