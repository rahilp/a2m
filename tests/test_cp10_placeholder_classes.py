"""CP10 adversarial round 2: two classes of credential the placeholders showed the AI provider, closed default-deny.

* Quoted object keys of custom code (JavaScript, Python): an allowlist keyed by the credential itself,
  ``var clients = {'partner-secret-123456': 'partner'}``, sent the key verbatim (codex-correctness X1).
* Numbers in the data positions of an Apigee policy or endpoint: a JavaCallout
  ``<Property name="password">123456789012</Property>``, a header or query value, an AssignVariable value, and the
  numbers a2m copies from them into a Mule attribute or a JSON payload (codex-standards X1).

A number stays visible only where it is syntax (code) or a structural setting (``<StatusCode>``, ``timeLimit``,
a2m's ``entryTtl`` and status variable). Only public entry points are used: ``Translator``, ``Place``,
``CalloutSource``, ``ItemKind`` and ``Placeholders``.

Case IDs:

* CP10-X12 - a JavaScript callout with a credential as a quoted object key: it reaches no AiRequest field.
* CP10-X13 - a Python callout with a credential as a quoted dictionary key: it reaches no AiRequest field.
* CP10-X14 - a placeholder of a hidden key written back into the answer's DataWeave comes back as the exact key.
* CP10-X15 - a JavaCallout with a numeric password Property: the number reaches no AiRequest field, its name does.
* CP10-X16 - numeric credentials in the data positions of every policy type and of a target endpoint are hidden;
  structural numbers stay.
* CP10-X17 - numbers a2m copied into a Mule attribute or a JSON payload are hidden; a2m's structural numbers stay,
  and an echoed Mule file is restored byte for byte.
"""

from __future__ import annotations

import json
import re
from dataclasses import fields
from typing import Any

import pytest

from a2m.ai.placeholders import Placeholders
from a2m.ai.provider import ItemKind
from a2m.ai.sources import CalloutSource
from a2m.ai.translate import CalloutTranslated, Place, Translator

TOKEN = re.compile(r"«v\d+»")
ANY_TOKEN = re.compile(r"«[vn]\d+»")  # a text («vN») or a number («nN») placeholder
NUMBER_TOKEN = re.compile(r"«n\d+»")
PLACE = Place("canary-proxy", "default PreFlow request", "request", "AM-Before", "AM-After")
KEY_SECRET = "partner-secret-123456"
NUMERIC_PASSWORD = "123456789012"


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


def _texts(request: Any) -> dict[str, str]:
    return {item.name: str(getattr(request, item.name)) for item in fields(request)}


def _assert_absent(provider: Recorder, *values: str) -> Any:
    assert len(provider.requests) == 1
    request = provider.requests[0]
    for field, text in _texts(request).items():
        for value in values:
            assert value not in text, f"{value} reached the request field {field!r}:\n{text}"
    return request


@pytest.mark.parametrize(
    "code",
    [
        pytest.param(f"var clients = {{'{KEY_SECRET}': 'partner'}};\n", id="single-quoted"),
        pytest.param(f'var clients = {{"{KEY_SECRET}": "partner", other: 1}};\n', id="double-quoted"),
        pytest.param(f"var clients = {{first: 1, '{KEY_SECRET}': 'partner'}};\n", id="after-comma"),
    ],
)
def test_CP10_X12_javascript_quoted_object_key_never_reaches_the_request(code: str) -> None:
    """[CP10-X12] A credential written as a quoted object key of a JavaScript callout reaches no AiRequest field."""
    provider = Recorder()
    source = CalloutSource(ItemKind.JAVASCRIPT, code + "context.setVariable('n', clients.length);\n", "allow.js")

    Translator(provider).callout(source, "JS-Allow", "Javascript", '<Javascript name="JS-Allow"/>', PLACE)

    request = _assert_absent(provider, KEY_SECRET)
    assert TOKEN.search(str(request.original)) is not None
    assert "var clients = {" in str(request.original)


@pytest.mark.parametrize(
    "code",
    [
        pytest.param(f"clients = {{'{KEY_SECRET}': 'partner'}}\n", id="single-quoted"),
        pytest.param(f'clients = {{"{KEY_SECRET}": "partner"}}\n', id="double-quoted"),
        pytest.param(f"clients = dict(a=1) | {{'x': 1, '{KEY_SECRET}': 2}}\n", id="after-comma"),
    ],
)
def test_CP10_X13_python_quoted_dictionary_key_never_reaches_the_request(code: str) -> None:
    """[CP10-X13] A credential written as a quoted dictionary key of a Python callout reaches no AiRequest field."""
    provider = Recorder()
    source = CalloutSource(ItemKind.PYTHON, code + "flow.setVariable('n', str(len(clients)))\n", "allow.py")

    Translator(provider).callout(source, "PY-Allow", "Script", '<Script name="PY-Allow"/>', PLACE)

    request = _assert_absent(provider, KEY_SECRET)
    assert "clients = " in str(request.original)


def test_CP10_X14_a_hidden_object_key_is_restored_into_the_answer() -> None:
    """[CP10-X14] The AI writes the placeholder of a hidden key into a DataWeave object key; a2m puts back the exact
    key before CP6's checks, and the prompt never held it."""
    code = f"var clients = {{'{KEY_SECRET}': 'partner'}};\ncontext.setVariable('allow.list', clients);\n"

    def answer(request: Any) -> str:
        shown = str(request.original)
        match = re.search(r"var clients = \{'(«v\d+»)': '(«v\d+»)'\}", shown)
        assert match is not None, shown
        key, value = match.groups()
        name = re.search(r"context\.setVariable\('(«v\d+»)'", shown)
        assert name is not None, shown
        mule = f"<set-variable variableName=\"{name.group(1)}\" value=\"#[{{'{key}': '{value}'}}]\"/>"
        return json.dumps(
            {"status": "translated", "confidence": "high", "notes": "n", "mule": mule,
             "writes": {"variables": [name.group(1)]}}
        )

    provider = Recorder(answer)
    source = CalloutSource(ItemKind.JAVASCRIPT, code, "allow.js")

    result = Translator(provider).callout(source, "JS-Allow", "Javascript", '<Javascript name="JS-Allow"/>', PLACE)

    assert isinstance(result, CalloutTranslated), result
    (processor,) = result.processors
    assert processor.get("variableName") == "allow.list"
    assert processor.get("value") == f"#[{{'{KEY_SECRET}': 'partner'}}]"
    assert KEY_SECRET not in str(provider.requests[0].prompt)


def test_CP10_X15_java_callout_numeric_password_property_never_reaches_the_request() -> None:
    """[CP10-X15] The complete AiRequest for a JavaCallout whose password Property is a number holds no copy of it;
    the Property's name stays visible."""
    provider = Recorder()
    java = (
        "public class Auth implements Execution {\n"
        "  public ExecutionResult execute(MessageContext c, ExecutionContext e) {\n"
        "    c.setVariable(\"auth.ok\", \"yes\"); return ExecutionResult.SUCCESS; }\n}\n"
    )
    policy = (
        '<JavaCallout name="JC-Auth"><Properties>'
        f'<Property name="password">{NUMERIC_PASSWORD}</Property>'
        '<Property name="retries">3</Property><Property name="strict">true</Property></Properties>'
        "<ClassName>com.example.Auth</ClassName><ResourceURL>java://auth.jar</ResourceURL></JavaCallout>"
    )
    source = CalloutSource(ItemKind.JAVA, java, "java/com/example/Auth.java")

    Translator(provider).callout(source, "JC-Auth", "JavaCallout", policy, PLACE)

    request = _assert_absent(provider, NUMERIC_PASSWORD)
    prompt = str(request.prompt)
    assert 'name="password">«v' in prompt, prompt
    assert ">3<" not in prompt and ">true<" not in prompt, "a Property value is data: every value is a placeholder"


# One numeric credential per data position, for every policy type a2m reads, and the policy XML that holds it.
NUMERIC_POSITIONS: dict[str, tuple[str, str]] = {
    "AssignMessage header": (
        "518302749163",
        (
            '<AssignMessage name="AM-Pin"><Set><Headers><Header name="X-Partner-Pin">518302749163</Header></Headers>'
            "</Set></AssignMessage>"
        ),
    ),
    "AssignMessage query parameter": (
        "927461038254",
        (
            '<AssignMessage name="AM-Pin"><Set><QueryParams><QueryParam name="pin">927461038254</QueryParam>'
            "</QueryParams></Set></AssignMessage>"
        ),
    ),
    "AssignMessage AssignVariable Value": (
        "381947205639",
        (
            '<AssignMessage name="AM-Pin"><AssignVariable><Name>backend.pin</Name><Value>381947205639</Value>'
            "</AssignVariable></AssignMessage>"
        ),
    ),
    "AssignMessage JSON payload": (
        "265018394712",
        (
            '<AssignMessage name="AM-Pin"><Set><Payload contentType="application/json">{"pin": 265018394712}'
            "</Payload></Set></AssignMessage>"
        ),
    ),
    "AssignMessage Add header": (
        "730481926354",
        (
            '<AssignMessage name="AM-Pin"><Add><Headers><Header name="X-Pin">730481926354</Header></Headers></Add>'
            "</AssignMessage>"
        ),
    ),
    "Javascript Property": (
        "604183927561",
        (
            '<Javascript name="JS-Pin" timeLimit="200"><Properties><Property name="pin">604183927561</Property>'
            "</Properties><ResourceURL>jsc://pin.js</ResourceURL></Javascript>"
        ),
    ),
    "Python Script Property": (
        "153862094718",
        (
            '<Script name="PY-Pin"><Properties><Property name="pin">153862094718</Property></Properties>'
            "<ResourceURL>py://pin.py</ResourceURL></Script>"
        ),
    ),
    "JavaCallout Property": (
        "739218465017",
        (
            '<JavaCallout name="JC-Pin"><Properties><Property name="password">739218465017</Property></Properties>'
            "<ClassName>com.example.Pin</ClassName></JavaCallout>"
        ),
    ),
    "BasicAuthentication Password": (
        "846203917528",
        (
            '<BasicAuthentication name="BA-Pin"><Operation>Encode</Operation><User>svc</User>'
            "<Password>846203917528</Password><AssignTo>request.header.Authorization</AssignTo></BasicAuthentication>"
        ),
    ),
    "KeyValueMapOperations entry": (
        "193750284617",
        (
            '<KeyValueMapOperations name="KVM-Pin" mapIdentifier="backend"><InitialEntries><Entry><Key>'
            "<Parameter>pin</Parameter></Key><Value>193750284617</Value></Entry></InitialEntries>"
            "</KeyValueMapOperations>"
        ),
    ),
    "ServiceCallout request header": (
        "658230194735",
        (
            '<ServiceCallout name="SC-Pin"><Request variable="r"><Set><Headers><Header name="X-Geo-Pin">658230194735'
            "</Header></Headers></Set></Request><HTTPTargetConnection><URL>https://geo.example.com/x</URL>"
            "</HTTPTargetConnection></ServiceCallout>"
        ),
    ),
    "ServiceCallout URL query value": (
        "472819360528",
        (
            '<ServiceCallout name="SC-Pin"><HTTPTargetConnection><URL>https://geo.example.com/x?address=a&amp;'
            "pin=472819360528</URL></HTTPTargetConnection></ServiceCallout>"
        ),
    ),
    "RaiseFault header": (
        "314928570361",
        (
            '<RaiseFault name="RF-Pin"><FaultResponse><Set><Headers><Header name="X-Deny-Pin">314928570361</Header>'
            "</Headers><StatusCode>403</StatusCode></Set></FaultResponse></RaiseFault>"
        ),
    ),
    "target endpoint Property": (
        "920384756102",
        (
            '<TargetEndpoint name="default"><HTTPTargetConnection><Properties>'
            '<Property name="keystore.password">920384756102</Property></Properties>'
            "<URL>https://backend.example.test/x</URL></HTTPTargetConnection></TargetEndpoint>"
        ),
    ),
    "target endpoint URL query value": (
        "587106392841",
        (
            '<TargetEndpoint name="default"><HTTPTargetConnection>'
            "<URL>https://backend.example.test/x?pin=587106392841</URL></HTTPTargetConnection></TargetEndpoint>"
        ),
    ),
    "a policy element text off the name allowlist": (
        "402918375610",
        (
            '<VerifyJWT name="JWT-Pin"><Algorithm>HS256</Algorithm><SecretKey><Value ref="k"/></SecretKey>'
            "<Audience>402918375610</Audience></VerifyJWT>"
        ),
    ),
}


@pytest.mark.parametrize("position", sorted(NUMERIC_POSITIONS), ids=lambda p: "CP10-X16-" + p.replace(" ", "-"))
def test_CP10_X16_numeric_credentials_in_policy_data_positions_are_placeholders(position: str) -> None:
    """[CP10-X16] A number in a data position of every policy type and of a target endpoint is a placeholder."""
    secret, xml = NUMERIC_POSITIONS[position]

    shown = Placeholders().apigee(xml)

    assert shown is not None, xml
    assert secret not in shown, shown
    assert TOKEN.search(shown) is not None, shown


def test_CP10_X16_structural_numbers_and_names_stay_visible() -> None:
    """[CP10-X16] The structural numbers of a policy (a status code, a time limit, a quota) and the names it declares
    stay as they are next to the hidden data."""
    table = Placeholders()
    raise_fault = table.apigee(
        '<RaiseFault name="RF-Deny"><FaultResponse><Set><Headers><Header name="X-Deny-Pin">314928570361</Header>'
        "</Headers><StatusCode>401</StatusCode></Set></FaultResponse></RaiseFault>"
    )
    script = table.apigee(
        '<Javascript name="JS-Pin" timeLimit="200"><Properties><Property name="pin">604183927561</Property>'
        "</Properties></Javascript>"
    )
    quota = table.apigee('<Quota name="Q-Pin"><Allow count="2000"/><Interval>1</Interval><TimeUnit>minute</TimeUnit></Quota>')

    assert raise_fault is not None and script is not None and quota is not None
    assert "<StatusCode>401</StatusCode>" in raise_fault and 'name="X-Deny-Pin"' in raise_fault
    assert 'timeLimit="200"' in script and 'name="pin"' in script
    assert 'count="2000"' in quota and "<Interval>1</Interval>" in quota and "<TimeUnit>minute</TimeUnit>" in quota


MULE = (
    '<mule xmlns:doc="http://www.mulesoft.org/schema/mule/documentation" '
    'xmlns:os="http://www.mulesoft.org/schema/mule/os">\n'
    '  <os:object-store name="cache" persistent="false" entryTtl="3600000" entryTtlUnit="MILLISECONDS"/>\n'
    '  <flow name="main">\n'
    '    <set-variable variableName="backend.pin" value="381947205639" doc:name="AM-Pin"/>\n'
    '    <set-payload value="{&quot;pin&quot;: 265018394712, &quot;ok&quot;: true}" mimeType="application/json"/>\n'
    '    <set-variable variableName="a2mPin">730481926354</set-variable>\n'
    '    <set-variable variableName="httpStatus" value="500"/>\n'
    '    <set-variable variableName="httpStatus" value="#[output application/java --- 403]"/>\n'
    '    <set-variable variableName="first" value="#[payload.items[0] + 1]"/>\n'
    "  </flow>\n"
    "</mule>\n"
)


def test_CP10_X17_numbers_a2m_copied_into_mule_are_placeholders_and_structural_numbers_stay() -> None:
    """[CP10-X17] A number a2m copied from a policy into a Mule attribute, element text or JSON payload is a
    placeholder; a2m's structural numbers (entryTtl, its status variable, status codes and indexes in DataWeave) stay;
    an echoed file comes back byte for byte."""
    table = Placeholders(["main", "cache", "AM-Pin", "backend.pin", "a2mPin", "httpStatus", "first"])

    shown = table.mule({"proxy.xml": MULE})["proxy.xml"]

    assert shown is not None
    for secret in ("381947205639", "265018394712", "730481926354"):
        assert secret not in shown, shown
    for kept in (
        'entryTtl="3600000"', 'variableName="httpStatus" value="500"', "--- 403]", "payload.items[0] + «n",
        'persistent="false"', 'mimeType="application/json"', 'variableName="backend.pin"',
    ):
        assert kept in shown, (kept, shown)
    assert "payload.items[0] + 1" not in shown, shown
    assert table.restore("proxy.xml", shown) == MULE


# ---------------------------------------------------------------- CP10-X18 .. X19: quoted keys of JSON and DataWeave
#
# Orchestrator decision (CP10 round 2, option 1): every quoted key is a placeholder, in Apigee JSON payloads and in
# DataWeave as well, and is restored byte for byte.

JSON_KEY_SECRET = "partner-secret-654321"


def test_CP10_X18_a_json_payload_key_is_a_placeholder_and_restores_exactly() -> None:
    """[CP10-X18] A credential written as a quoted key of a JSON payload, in an Apigee policy and in the set-payload
    a2m generates from it, is a placeholder; the echoed Mule file restores byte for byte."""
    table = Placeholders(["main"])
    policy = table.apigee(
        '<AssignMessage name="AM-Allow"><Set><Payload contentType="application/json">'
        f'{{"{JSON_KEY_SECRET}": "partner", "tier": "gold"}}</Payload></Set></AssignMessage>'
    )
    mule = (
        '<mule xmlns:doc="http://www.mulesoft.org/schema/mule/documentation">\n  <flow name="main">\n'
        f'    <set-payload value="{{&quot;{JSON_KEY_SECRET}&quot;: &quot;partner&quot;}}" mimeType="application/json"/>\n'
        "  </flow>\n</mule>\n"
    )

    shown = table.mule({"proxy.xml": mule})["proxy.xml"]

    assert policy is not None and shown is not None
    assert JSON_KEY_SECRET not in policy and '"tier"' not in policy, policy
    assert JSON_KEY_SECRET not in shown, shown
    assert table.restore("proxy.xml", shown) == mule


def test_CP10_X19_a_dataweave_key_is_a_placeholder_and_an_edited_answer_restores_it() -> None:
    """[CP10-X19] A credential written as a quoted key of a DataWeave object in a Mule file is a placeholder; an
    answer that echoes the file restores byte for byte, and one that changes only the value keeps the exact key."""
    table = Placeholders(["main", "partnerAllow"])
    mule = (
        '<mule xmlns:doc="http://www.mulesoft.org/schema/mule/documentation">\n  <flow name="main">\n'
        f"    <set-variable variableName=\"partnerAllow\" value=\"#[{{'{JSON_KEY_SECRET}': 'partner'}}]\"/>\n"
        "  </flow>\n</mule>\n"
    )

    shown = table.mule({"proxy.xml": mule})["proxy.xml"]

    assert shown is not None
    assert JSON_KEY_SECRET not in shown, shown
    keyed = re.search(r"\{'(«v\d+»)': '(«v\d+»)'\}", shown)
    assert keyed is not None, shown
    key, _ = keyed.groups()
    assert table.restore("proxy.xml", shown) == mule
    edited = shown.replace(keyed.group(0), f"{{'{key}': 'gold'}}")
    assert table.restore("proxy.xml", edited) == mule.replace("'partner'", "'gold'")


# ---------------------------------------------------------------- CP10-X20 .. X28: numbers in conditions and code
#
# CP10 adversarial round 3 (host-alt-correctness A1, A2), closed default-deny: every number of a condition and of
# custom code is a placeholder on both channels (the translation of conditions and callouts, and the fix loop), a
# number a2m copied into DataWeave is one too (only a2m's own syntax stays: a selector index, a status code, the
# %dw version and directive options), and a JSON number's sign, fraction and exponent belong to its placeholder.
# Restoring stays byte-exact, and the AI can write a number's placeholder where the number goes.

import importlib.util  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

from a2m.ai.translate import ExpressionTranslated  # noqa: E402

PIN = "265018394712"
REPO = Path(__file__).resolve().parents[1]
CANARY_SCRIPT = REPO / "tools" / "checks" / "credential_canary.py"


@pytest.mark.parametrize(
    ("condition", "variable"),
    [
        pytest.param(f"request.header.x-pin = {PIN}", "request.header.x-pin", id="equals-symbol"),
        pytest.param(f"request.header.x-pin == {PIN}", "request.header.x-pin", id="double-equals"),
        pytest.param(f"request.header.x-pin Equals {PIN}", "request.header.x-pin", id="equals-word"),
        pytest.param(f"request.header.x-pin != {PIN}", "request.header.x-pin", id="not-equals"),
        pytest.param(f"request.queryparam.n > {PIN}", "request.queryparam.n", id="greater-than"),
        pytest.param(f"(request.verb = \"GET\") and (request.header.x-pin = -{PIN})", "request.verb", id="negative"),
        pytest.param(f"request.header.x-pin = {PIN[:6]}.{PIN[6:]}", "request.header.x-pin", id="decimal"),
        pytest.param(f"{PIN} = request.header.x-pin", "", id="number-first"),
    ],
)
def test_CP10_X20_a_number_in_a_condition_never_reaches_the_expression_request(condition: str, variable: str) -> None:
    """[CP10-X20] A number compared in an Apigee condition (any operator, either side, signed or decimal) reaches no
    AiRequest field of the condition's translation, the refusal reason that names it included; the variable stays."""
    provider = Recorder()
    written = re.search(r"-?\d[\d.]*", condition)
    assert written is not None
    refusal = f"comparing with the number {written.group(0)} is not translated"  # CP5 names the number as written

    Translator(provider).expression("Step RF-Pin", "RF-Pin", condition, PLACE, refusal)

    assert provider.requests == []  # CP10 round 5: a condition with a number placeholder is never sent
    shown = Placeholders().condition(condition)
    assert PIN not in shown and PIN[:6] + "." + PIN[6:] not in shown, shown
    assert NUMBER_TOKEN.search(shown) is not None, shown
    assert variable in shown, shown


def test_CP10_X21_a_number_in_a_condition_never_reaches_the_fix_prompt() -> None:
    """[CP10-X21] In the fix loop's view of a proxy endpoint, a number compared in a Step or Flow condition is a
    placeholder; the variable and the operator stay."""
    table = Placeholders(["default", "RF-Pin", "Pinned"])
    endpoint = (
        '<ProxyEndpoint name="default"><PreFlow name="PreFlow"><Request><Step><Name>RF-Pin</Name>'
        f"<Condition>request.header.x-pin = {PIN}</Condition></Step></Request></PreFlow><Flows>"
        f'<Flow name="Pinned"><Condition>(request.queryparam.pin Equals -{PIN})</Condition></Flow></Flows>'
        "</ProxyEndpoint>"
    )

    shown = table.apigee(endpoint)

    assert shown is not None
    assert PIN not in shown, shown
    assert re.search(r"request\.header\.x-pin = «n\d+»<", shown), shown
    assert re.search(r"\(request\.queryparam\.pin Equals «n\d+»\)", shown), shown


def test_CP10_X22_a_condition_numbers_placeholder_written_as_the_trees_value_is_refused_as_cp5_refuses_it() -> None:
    """[CP10-X22] (CP10 round 4: was "gets the exact value", which wrote a TEXT comparison of a number.) The AI sees
    the hidden condition number as a number placeholder and writes it as the tree's value; a2m does not turn it into
    a text comparison: the condition compares with a number, so the answer is refused as CP5 refuses it, and the
    prompt never held the number."""

    def answer(request: Any) -> str:
        token = re.search(r"request\.header\.x-pin = («n\d+»)", str(request.original))
        assert token is not None, request.original
        tree = {"variable": "request.header.x-pin", "operator": "equals", "value": token.group(1)}
        return json.dumps({"status": "translated", "confidence": "high", "notes": "n", "condition": tree})

    provider = Recorder(answer)

    result = Translator(provider).expression("Step RF-Pin", "RF-Pin", f"request.header.x-pin = {PIN}", PLACE, "r")

    assert not isinstance(result, ExpressionTranslated), result
    assert "number" in result.reason and "not translated" in result.reason, result.reason
    assert PIN not in result.reason
    assert provider.requests == []  # CP10 round 5: never sent, so the prompt never held the number


CODE_NUMBERS: dict[str, tuple[ItemKind, str, str, str]] = {
    "javascript": (
        ItemKind.JAVASCRIPT,
        f"var pin = {PIN};\ncontext.setVariable('pin.ok', String(pin === {PIN}));\nvar half = -{PIN} / 2;\n",
        "pin.js",
        "var pin = «n",
    ),
    "python": (
        ItemKind.PYTHON,
        f"pin = {PIN}\nflow.setVariable('pin.ok', str(pin == {PIN} and pin > 1.5e3))\n",
        "pin.py",
        "pin = «n",
    ),
    "java": (
        ItemKind.JAVA,
        (
            "public class Pin implements Execution {\n"
            "  public ExecutionResult execute(MessageContext c, ExecutionContext e) {\n"
            f"    long pin = {PIN}L; int mask = 0x{PIN[:4]};\n"
            '    c.setVariable("pin.ok", String.valueOf(pin > 0)); return ExecutionResult.SUCCESS; }\n}\n'
        ),
        "java/Pin.java",
        "long pin = «n",
    ),
}


@pytest.mark.parametrize("language", sorted(CODE_NUMBERS))
def test_CP10_X23_a_numeric_literal_of_custom_code_never_reaches_the_callout_request(language: str) -> None:
    """[CP10-X23] A numeric literal of JavaScript, Python or Java custom code (an int, a long, a hex number, a
    negative or an exponent) reaches no AiRequest field of the callout's translation; the code's structure stays."""
    kind, code, file, kept = CODE_NUMBERS[language]
    provider = Recorder()

    Translator(provider).callout(CalloutSource(kind, code, file), "CO-Pin", "Javascript", "<Javascript name='CO-Pin'/>", PLACE)

    request = _assert_absent(provider, PIN, PIN[:4])
    assert kept in str(request.original), request.original
    if kind is ItemKind.PYTHON:
        assert "1.5e3" not in str(request.original), request.original


def test_CP10_X23_the_fix_loop_shows_no_numeric_literal_of_custom_code() -> None:
    """[CP10-X23] The fix loop's view of custom code (Placeholders.code) hides every numeric literal of each
    language, an index and a small number too."""
    for kind, code, _, kept in CODE_NUMBERS.values():
        shown = Placeholders().code(code + "x = items[0] + 1;\n", kind)
        assert PIN not in shown and PIN[:4] not in shown, shown
        assert kept in shown and "items[«n" in shown, shown
        assert re.search(r"\d", ANY_TOKEN.sub("", shown)) is None, shown


def test_CP10_X24_the_ai_writes_a_code_numbers_placeholder_bare_and_gets_the_exact_number() -> None:
    """[CP10-X24] The AI writes the placeholder of a hidden numeric literal where a number goes in its DataWeave
    (outside any string literal); a2m puts back the exact number, and the prompt never held it."""
    code = f"var pin = {PIN};\ncontext.setVariable('pin.ok', String(context.getVariable('pin') == pin));\n"

    def answer(request: Any) -> str:
        shown = str(request.original)
        number = re.search(r"var pin = («n\d+»);", shown)
        name = re.search(r"context\.setVariable\('(«v\d+»)'", shown)
        assert number is not None and name is not None, shown
        mule = (
            f'<set-variable variableName="{name.group(1)}" '
            f"value=\"#[if (vars.pin == {number.group(1)}) 'true' else 'false']\"/>"
        )
        return json.dumps(
            {"status": "translated", "confidence": "high", "notes": "n", "mule": mule,
             "writes": {"variables": [name.group(1)]}}
        )

    provider = Recorder(answer)

    result = Translator(provider).callout(
        CalloutSource(ItemKind.JAVASCRIPT, code, "pin.js"), "JS-Pin", "Javascript", '<Javascript name="JS-Pin"/>', PLACE
    )

    assert isinstance(result, CalloutTranslated), result
    (processor,) = result.processors
    assert processor.get("value") == f"#[if (vars.pin == {PIN}) 'true' else 'false']"
    assert PIN not in str(provider.requests[0].prompt)


MULE_NUMBERS = (
    '<mule xmlns:http="http://www.mulesoft.org/schema/mule/http" '
    'xmlns:ee="http://www.mulesoft.org/schema/mule/ee/core">\n'
    '  <flow name="main">\n'
    '    <http:listener path="/"><http:response statusCode="#[vars.httpStatus default 200]"/></http:listener>\n'
    f'    <set-variable variableName="pinOk" value="#[vars.pin == {PIN}]"/>\n'
    f"    <set-variable variableName=\"headerOk\" value=\"#[attributes.headers['x-pin'] == -{PIN}]\"/>\n"
    '    <set-variable variableName="first" value="#[payload.items[0]]"/>\n'
    '    <set-variable variableName="user" value="#[trim((vars.auth as String)[6 to -1])]"/>\n'
    '    <set-variable variableName="httpStatus" value="#[output application/java --- 401]"/>\n'
    "    <ee:transform><ee:message><ee:set-payload><![CDATA[%dw 2.0\n"
    "output text/plain; charset=UTF-8\n---\n"
    f"payload.total * {PIN}\n"
    "]]></ee:set-payload></ee:message></ee:transform>\n"
    "  </flow>\n"
    "</mule>\n"
)


def test_CP10_X25_numbers_copied_into_dataweave_are_placeholders_and_a2ms_syntax_stays() -> None:
    """[CP10-X25] In a Mule file, a number of DataWeave that may come from the bundle (a comparison, arithmetic,
    signed) is a placeholder; a2m's own syntax stays (a status code where one belongs, a selector index, the %dw
    version, a directive's charset). An echo restores byte for byte, and an edit that writes the placeholder bare
    gets the exact number back."""
    table = Placeholders(["main", "pinOk", "headerOk", "first", "user", "httpStatus"])

    shown = table.mule({"proxy.xml": MULE_NUMBERS})["proxy.xml"]

    assert shown is not None
    assert PIN not in shown, shown
    for kept in (
        'statusCode="#[vars.httpStatus default 200]"', "payload.items[0]", "[6 to -1]",
        "--- 401]", "%dw 2.0", "charset=UTF-8",
    ):
        assert kept in shown, (kept, shown)
    assert table.restore("proxy.xml", shown) == MULE_NUMBERS
    compared = re.search(r"vars\.pin == («n\d+»)", shown)
    assert compared is not None, shown
    edited = shown.replace(compared.group(0), f"vars.pin != {compared.group(1)}")
    assert table.restore("proxy.xml", edited) == MULE_NUMBERS.replace(f"vars.pin == {PIN}", f"vars.pin != {PIN}")


@pytest.mark.parametrize(
    "number",
    [f"-{PIN}", f"+{PIN}", f"-{PIN[:6]}.{PIN[6:]}e-3", f"{PIN[:6]}.{PIN[6:]}E+10", f"-{PIN}e7"],
    ids=["negative", "plus", "negative-fraction-exponent", "exponent", "negative-exponent"],
)
def test_CP10_X26_a_json_numbers_sign_fraction_and_exponent_are_part_of_its_placeholder(number: str) -> None:
    """[CP10-X26] In JSON text (an Apigee payload and the set-payload a2m copies it into) a number's sign, fraction
    and exponent are inside its placeholder; the echo restores byte for byte, and the placeholder written elsewhere
    in the JSON gets the exact signed number back."""
    table = Placeholders(["main"])
    policy = table.apigee(
        '<AssignMessage name="AM-J"><Set><Payload contentType="application/json">'
        f'{{"pin": {number}, "list": [1, {number}]}}</Payload></Set></AssignMessage>'
    )
    mule = (
        '<mule>\n  <flow name="main">\n'
        f'    <set-payload value="{{&quot;pin&quot;: {number}}}" mimeType="application/json"/>\n'
        "  </flow>\n</mule>\n"
    )

    shown = table.mule({"proxy.xml": mule})["proxy.xml"]

    assert policy is not None and shown is not None
    for text in (policy, shown):
        assert PIN[:6] not in text and PIN[6:] not in text, text
        assert re.search(r"[-+.eE0-9]«[vn]|»[-+.eE0-9]", text) is None, text
    assert table.restore("proxy.xml", shown) == mule
    value = re.search(r"&quot;: («n\d+»)\}", shown)
    assert value is not None, shown
    edited = shown.replace(f"{value.group(0)}", f"{value.group(0)[:-1]}, &quot;copy&quot;: {value.group(1)}}}")
    assert table.restore("proxy.xml", edited) == mule.replace(
        f"{number}}}", f"{number}, &quot;copy&quot;: {number}}}"
    )


def _canary_module() -> Any:
    spec = importlib.util.spec_from_file_location("cp10_credential_canary", CANARY_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_CP10_X27_the_check_plants_numeric_canaries_and_detects_a_shown_sign(tmp_path: Path) -> None:
    """[CP10-X27] The check's full-pipeline proxy plants a number as a condition's compared value, a numeric literal
    in its JavaScript, Python and Java callouts and a negative JSON number; and it reports a JSON sign shown outside
    its placeholder."""
    check = _canary_module()

    check.write_policies_proxy(tmp_path, basic_auth_step=True)

    files = {path.name: path.read_text(encoding="utf-8") for path in tmp_path.rglob("*") if path.is_file()}
    every = "\n".join(files.values())
    numbers = {label: check.PROVIDER_CANARIES[label][0] for label in ("cond-num", "js-num", "py-num", "java-num")}
    assert re.search(rf"<Condition>[^<]*= {numbers['cond-num']}</Condition>", every), "numeric condition value"
    assert re.search(rf"var pin = {numbers['js-num']};", files["sign.js"]), "JavaScript numeric literal"
    assert re.search(rf"pin = {numbers['py-num']}\n", files["hash.py"]), "Python numeric literal"
    assert re.search(rf"long pin = {numbers['java-num']}L;", files["Auth.java"]), "Java numeric literal"
    assert f": -{check.PROVIDER_CANARIES['json-neg-num'][0]}" in every, "negative JSON number"
    assert check.signed_number_shown('{"«v1»": -«v2»}')
    assert check.signed_number_shown("{&quot;«v3»&quot;: +«v4»}")
    assert not check.signed_number_shown('{"«v1»": «v2»}')


def test_CP10_X28_a2m_passes_the_check_with_the_numeric_canaries() -> None:
    """[CP10-X28] The a2m package under test passes the credential canary check, numeric canaries included."""
    import a2m

    env = {key: value for key, value in os.environ.items() if key not in ("FORCE_COLOR", "PY_COLORS")}
    result = subprocess.run(
        [sys.executable, str(CANARY_SCRIPT), str(Path(a2m.__file__).resolve().parent)],
        capture_output=True, text=True, timeout=300, env=env, check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "expression" in result.stdout, result.stdout
