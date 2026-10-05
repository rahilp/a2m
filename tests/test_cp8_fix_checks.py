"""CP8 adversarial round 1: the AI fix loop's checks on real generated Mule XML (default suite, no Mule).

The locked CP8 tests (tests/test_cp8_fix_loop.py) simulate a fix with an XML comment, which the XML parser drops,
so they never reach the element-level checks. Every test here feeds a fix that changes real elements of a project
the real a2m generator wrote, through :func:`a2m.verify.fix_loop.run_with_fixes`, with a fake runner that records
the Mule configuration as it was each time the app was started (so a fix that was written and re-tested is seen even
when it is undone afterwards). The AI is a local fake provider; nothing reaches the network.

Pinned here (findings of round 1):

* a fix that removes, moves or duplicates an element a2m generated, changes a template step, or is a shortened
  whole-file answer is refused: nothing is written and the app is never re-tested for it (CP8-X01..X04, X09);
* a line the AI echoes as it was shown, masked, is put back to the original; a mask anywhere else is refused, so a
  mask never reaches the project (CP8-X05, X06);
* literal credentials in policy XML and Mule files never reach the AI's prompt, and an echoed file gets them back
  (CP8-X07);
* a guard an AI fix adds is checked for the side of the flow it sits on (CP8-X08);
* an AI-translated step may be changed in place; a kept fix records the steps it changed and the AI's confidence,
  and gets a review flag when the confidence is low (CP8-X09, X10);
* an answer that changes nothing is never credited as help (CP8-X11);
* a missing fix prompt is reported before any attempt and is not counted as one (CP8-X12);
* a cut-off or timed-out fix answer from the Claude provider ends the loop, and fix requests get a longer timeout and
  no client retries (CP8-X13).
"""

from __future__ import annotations

import http.client
import json
import re
import shutil
import sys
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from xml.sax.saxutils import quoteattr

import pytest

REPO = Path(__file__).resolve().parents[1]
CP4_ORDERS = REPO / "tests" / "fixtures" / "apigee" / "cp4" / "orders-api"
FLOW_REL = "src/main/mule/proxy.xml"
XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
JSON_HEADERS = {"Content-Type": "application/json"}
SHOWN = re.compile(r"### src/main/mule/proxy\.xml\n\n```xml\n(.*?)\n```", re.DOTALL)


# ---------------------------------------------------------------- the project and its text


@dataclass
class App:
    bundle: Any
    app_dir: Path
    text: str  # the generated proxy.xml
    generated: Any = None  # GeneratedSteps, when the test generated with an AI provider


def generate(bundle_dir: Path, app_dir: Path, provider: Any = None) -> App:
    from a2m.generator import generate_project
    from a2m.parser import read_bundle
    from a2m.verify.generated import GeneratedSteps

    bundle = read_bundle(bundle_dir)
    result = generate_project(bundle, app_dir, provider=provider) if provider is not None else generate_project(
        bundle, app_dir
    )
    text = (app_dir / FLOW_REL).read_text(encoding="utf-8")
    return App(bundle, app_dir, text, GeneratedSteps.from_result(result))


def orders_app(tmp_path: Path) -> App:
    bundle_dir = tmp_path / "in" / "orders-api"
    shutil.copytree(CP4_ORDERS, bundle_dir)
    return generate(bundle_dir, tmp_path / "orders-api" / "mule-app")


def flow_text(app: App) -> str:
    return (app.app_dir / FLOW_REL).read_text(encoding="utf-8")


def block(text: str, start: str) -> tuple[int, int]:
    """The first and last line index of the element whose start line contains ``start`` (pretty-printed XML)."""
    lines = text.splitlines()
    first = next(i for i, line in enumerate(lines) if start in line)
    line = lines[first]
    if line.rstrip().endswith("/>"):
        return first, first
    indent = line[: len(line) - len(line.lstrip())]
    last = next(i for i in range(first + 1, len(lines)) if lines[i].startswith(indent + "</"))
    return first, last


def lines_of(text: str) -> list[str]:
    return text.splitlines(keepends=True)


def without(text: str, start: str) -> str:
    first, last = block(text, start)
    lines = lines_of(text)
    return "".join(lines[:first] + lines[last + 1 :])


def element(text: str, start: str) -> str:
    first, last = block(text, start)
    return "".join(lines_of(text)[first : last + 1])


def insert_after(text: str, start: str, new: str) -> str:
    _, last = block(text, start)
    lines = lines_of(text)
    return "".join(lines[: last + 1] + [new] + lines[last + 1 :])


def insert_before(text: str, start: str, new: str) -> str:
    first, _ = block(text, start)
    lines = lines_of(text)
    return "".join(lines[:first] + [new] + lines[first:])


LOGGER = '        <logger level="INFO" message="cp8 fix check" />\n'


# ---------------------------------------------------------------- the fake runner and AI


class RecordingRunner:
    """A Runner that records the app's proxy.xml at every start; its app answers every call with 500 (so every
    battery case fails, whatever the fix wrote)."""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.snapshots: list[str] = []

    def start(self, app: Any, *, backend_url: str) -> Any:
        from a2m.verify import HttpResponse

        self.started.append(app.name)
        self.snapshots.append((Path(app.app_dir) / FLOW_REL).read_text(encoding="utf-8"))

        class _Handle:
            running = True
            base_url = "http://fake-fail.invalid/app"

            def send(self, request: Any) -> Any:
                return HttpResponse(500, dict(JSON_HEADERS), b'{"fail":true}')

            def stop(self) -> None:
                pass

        return _Handle()


@dataclass
class ScriptedProvider:
    """A Provider whose answers are built from the request (so an answer can echo exactly what the AI was shown)."""

    answers: list[Callable[[Any], str]] = field(default_factory=list)
    requests: list[Any] = field(default_factory=list)

    def complete(self, request: Any) -> str:
        self.requests.append(request)
        assert self.answers, "the fix loop asked the AI more times than this test expected"
        return self.answers.pop(0)(request)


def fixed(text: str, *, confidence: str | None = None) -> str:
    data: dict[str, Any] = {"status": "fixed", "files": {FLOW_REL: text}, "notes": "cp8 fix check"}
    if confidence is not None:
        data["confidence"] = confidence
    return json.dumps(data)


def shown_flow(request: Any) -> str:
    """The proxy.xml exactly as the fix prompt showed it to the AI."""
    match = SHOWN.search(request.prompt)
    assert match is not None, request.prompt[:2000]
    return match.group(1) + "\n"


def run(app: App, provider: Any, runner: Any, *, max_fix_attempts: int = 1, golden: Path | None = None) -> Any:
    from a2m.verify.fix_loop import run_with_fixes

    return run_with_fixes(
        app.bundle, app.app_dir, runner=runner, provider=provider, max_fix_attempts=max_fix_attempts,
        golden=golden, generated=app.generated,
    )


def assert_refused(app: App, loop: Any, runner: RecordingRunner, *words: str) -> None:
    """The one attempt was refused: not helped, nothing written, the app never re-tested for it."""
    assert len(loop.attempts) == 1
    attempt = loop.attempts[0]
    assert attempt.helped is False
    assert attempt.changed_files == ()
    assert "refused" in attempt.reason, attempt.reason
    for word in words:
        assert word in attempt.reason, attempt.reason
    assert len(runner.started) == 1, "the refused fix was written and re-tested"
    assert flow_text(app) == app.text
    assert loop.result.type.value == "failed"


# ---------------------------------------------------------------- CP8-X01..X04: structural bypasses


def test_CP8_X01_a_fix_that_deletes_a_generated_auth_step_is_refused(tmp_path: Path) -> None:
    """[CP8-X01] Removing the generated Verify-Key step (the API key check) from orders-api's proxy.xml is refused,
    never written and never re-tested."""
    app = orders_app(tmp_path)
    runner = RecordingRunner()
    provider = ScriptedProvider([lambda request: fixed(without(app.text, '<try doc:name="Verify-Key">'))])

    loop = run(app, provider, runner)

    assert_refused(app, loop, runner, "Verify-Key")


def test_CP8_X02_a_fix_that_moves_an_auth_step_after_the_backend_call_is_refused(tmp_path: Path) -> None:
    """[CP8-X02] Moving the Verify-Key step after the backend http:request is refused."""
    app = orders_app(tmp_path)
    verify_key = element(app.text, '<try doc:name="Verify-Key">')
    moved = insert_after(without(app.text, '<try doc:name="Verify-Key">'), "<http:request ", verify_key)
    runner = RecordingRunner()
    provider = ScriptedProvider([lambda request: fixed(moved)])

    loop = run(app, provider, runner)

    assert_refused(app, loop, runner, "Verify-Key")


def test_CP8_X03_a_fix_that_duplicates_the_backend_call_or_adds_a_connector_is_refused(tmp_path: Path) -> None:
    """[CP8-X03] A second copy of the generated backend http:request is refused, and so is an added flow-ref."""
    app = orders_app(tmp_path)
    call = element(app.text, "<http:request ")
    duplicated = insert_after(app.text, "<http:request ", call)
    flow_ref = insert_before(app.text, "<http:request ", '        <flow-ref name="proxy-default" />\n')
    runner = RecordingRunner()
    provider = ScriptedProvider([lambda request: fixed(duplicated), lambda request: fixed(flow_ref)])

    loop = run(app, provider, runner, max_fix_attempts=2)

    assert len(loop.attempts) == 2
    assert all(not a.helped and a.changed_files == () and "refused" in a.reason for a in loop.attempts)
    assert "request" in loop.attempts[0].reason
    assert len(runner.started) == 1
    assert flow_text(app) == app.text


def test_CP8_X04_a_shortened_whole_file_answer_is_refused(tmp_path: Path) -> None:
    """[CP8-X04] An answer that keeps the listener, writes '<!-- rest unchanged -->' and closes the file (dropping
    every step, the backend call and the error handler) is refused."""
    app = orders_app(tmp_path)
    lines = lines_of(app.text)
    _, listener_end = block(app.text, "<http:listener ")
    short = "".join(lines[: listener_end + 1]) + "        <!-- rest unchanged -->\n" + LOGGER + "    </flow>\n</mule>\n"
    runner = RecordingRunner()
    provider = ScriptedProvider([lambda request: fixed(short)])

    loop = run(app, provider, runner)

    assert_refused(app, loop, runner, "removes")


# ---------------------------------------------------------------- CP8-X05..X07: masking round trip and secrets


def test_CP8_X05_an_echoed_masked_line_is_put_back_and_no_mask_is_ever_written(tmp_path: Path) -> None:
    """[CP8-X05] orders-api's Encode-Basic-Auth step reaches the prompt with a placeholder ('Basic ' shows as
    '«vN»'). An answer that echoes the shown file plus one real change (an added logger) is written with the original
    'Basic ' line, never with a placeholder or a mask."""
    app = orders_app(tmp_path)
    runner = RecordingRunner()

    def answer(request: Any) -> str:
        shown = shown_flow(request)
        assert "'Basic '" not in shown and "'«v" in shown  # the AI really was shown a placeholder
        return fixed(insert_after(shown, "<http:listener ", LOGGER))

    loop = run(app, ScriptedProvider([answer]), runner)

    assert len(runner.started) == 2, loop.attempts[0].reason
    tested = runner.snapshots[1]
    assert "cp8 fix check" in tested
    assert "{'authorization': 'Basic ' ++" in tested
    assert "***" not in tested and "«v" not in tested
    assert loop.attempts[0].changed_files == (FLOW_REL,)


def test_CP8_X06_a_mask_in_a_changed_line_is_refused(tmp_path: Path) -> None:
    """[CP8-X06] A placeholder a2m never showed stands for nothing a2m can put back, and a2m's own mask format
    ('*** (5 chars)') is no value: a fix that writes either into a changed line is refused and nothing is written."""
    app = orders_app(tmp_path)
    runner = RecordingRunner()

    def answer(request: Any) -> str:
        shown = shown_flow(request)
        changed = re.sub(r"'«v\d+»' \+\+ dw::core", "'«v999999»' ++ dw::core", shown, count=1)
        assert changed != shown
        return fixed(changed)

    loop = run(app, ScriptedProvider([answer]), runner)

    assert_refused(app, loop, runner, "placeholder", "«v999999»")

    masked_app = orders_app(tmp_path / "masked")
    masked_runner = RecordingRunner()

    def masked(request: Any) -> str:
        shown = shown_flow(request)
        changed = re.sub(r"'«v\d+»' \+\+ dw::core", "'*** (5 chars) ' ++ dw::core", shown, count=1)
        assert changed != shown
        return fixed(changed)

    loop = run(masked_app, ScriptedProvider([masked]), masked_runner)

    assert_refused(masked_app, loop, masked_runner, "mask")


SECRETS = (
    "s3cr3t-backend-partner-key-998877",
    "qp-secret-token-55443322",
    "payload-secret-11223344",
    "var-secret-pass-667788",
)


def write_secret_bundle(parent: Path, name: str = "secret-proxy") -> Path:
    """One AssignMessage that sets literal backend credentials: a header, a query parameter, a JSON payload field
    and a variable."""
    root = parent / name / "apiproxy"
    for sub in ("policies", "proxies", "targets"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    policy = (
        '<AssignMessage name="Set-Backend-Auth"><Set>'
        f'<Headers><Header name="X-Partner-Api-Key">{SECRETS[0]}</Header></Headers>'
        f'<QueryParams><QueryParam name="access_token">{SECRETS[1]}</QueryParam></QueryParams>'
        f'<Payload contentType="application/json">{{"client_secret": "{SECRETS[2]}"}}</Payload></Set>'
        f"<AssignVariable><Name>backend.password</Name><Value>{SECRETS[3]}</Value></AssignVariable>"
        '<AssignTo createNew="false" transport="http" type="request"/></AssignMessage>\n'
    )
    (root / f"{name}.xml").write_text(
        XML_HEAD + f'<APIProxy revision="1" name="{name}"><Policies><Policy>Set-Backend-Auth</Policy></Policies>'
        "<ProxyEndpoints><ProxyEndpoint>default</ProxyEndpoint></ProxyEndpoints>"
        "<TargetEndpoints><TargetEndpoint>default</TargetEndpoint></TargetEndpoints></APIProxy>\n",
        encoding="utf-8",
    )
    (root / "policies" / "Set-Backend-Auth.xml").write_text(XML_HEAD + policy, encoding="utf-8")
    (root / "proxies" / "default.xml").write_text(
        XML_HEAD + '<ProxyEndpoint name="default"><PreFlow name="PreFlow"><Request><Step><Name>Set-Backend-Auth'
        "</Name></Step></Request><Response/></PreFlow><Flows/>"
        '<PostFlow name="PostFlow"><Request/><Response/></PostFlow>'
        "<HTTPProxyConnection><BasePath>/secret</BasePath><VirtualHost>default</VirtualHost></HTTPProxyConnection>"
        '<RouteRule name="default"><TargetEndpoint>default</TargetEndpoint></RouteRule></ProxyEndpoint>\n',
        encoding="utf-8",
    )
    (root / "targets" / "default.xml").write_text(
        XML_HEAD + '<TargetEndpoint name="default"><PreFlow name="PreFlow"><Request/><Response/></PreFlow><Flows/>'
        '<PostFlow name="PostFlow"><Request/><Response/></PostFlow>'
        "<HTTPTargetConnection><URL>http://backend.example/secret</URL></HTTPTargetConnection></TargetEndpoint>\n",
        encoding="utf-8",
    )
    return parent / name


def test_CP8_X07_literal_credentials_never_reach_the_prompt_and_an_echo_gets_them_back(tmp_path: Path) -> None:
    """[CP8-X07] A proxy whose AssignMessage sets literal backend credentials (a header, a query parameter, a JSON
    payload field, a variable): none of the four values is in the prompt, neither from the Apigee policy XML nor
    from the generated Mule file; an answer that echoes the shown file plus a logger is written with every real
    value back and no mask."""
    app = generate(write_secret_bundle(tmp_path / "in"), tmp_path / "secret-proxy" / "mule-app")
    assert all(secret in app.text for secret in SECRETS)
    runner = RecordingRunner()

    def answer(request: Any) -> str:
        return fixed(insert_after(shown_flow(request), "<http:listener ", LOGGER))

    provider = ScriptedProvider([answer])
    loop = run(app, provider, runner)

    prompt = provider.requests[0].prompt
    assert "X-Partner-Api-Key" in prompt and "<AssignVariable>" in prompt  # the policy XML was sent
    for secret in SECRETS:
        assert secret not in prompt, secret
        assert secret not in provider.requests[0].original, secret
    assert len(runner.started) == 2, loop.attempts[0].reason
    tested = runner.snapshots[1]
    assert all(secret in tested for secret in SECRETS)
    assert "***" not in tested and "cp8 fix check" in tested


# ---------------------------------------------------------------- CP8-X08: the side of an added guard


def write_response_guard_bundle(parent: Path, name: str = "response-guard-proxy") -> Path:
    """AM-SetHeader on the ProxyEndpoint PostFlow Response, guarded by a request header (a2m reads it from the sent
    request snapshot there), and SA-Limit on the PreFlow (so the battery has cases that fail)."""
    root = parent / name / "apiproxy"
    for sub in ("policies", "proxies", "targets"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    policy = (
        '<AssignMessage name="AM-SetHeader"><Set><Headers><Header name="X-Env">prod</Header></Headers></Set>'
        '<AssignTo createNew="false" transport="http" type="response"/></AssignMessage>\n'
    )
    spike = '<SpikeArrest name="SA-Limit"><Rate>600pm</Rate></SpikeArrest>\n'
    (root / "policies" / "SA-Limit.xml").write_text(XML_HEAD + spike, encoding="utf-8")
    (root / f"{name}.xml").write_text(
        XML_HEAD + f'<APIProxy revision="1" name="{name}"><Policies><Policy>AM-SetHeader</Policy>'
        "<Policy>SA-Limit</Policy></Policies>"
        "<ProxyEndpoints><ProxyEndpoint>default</ProxyEndpoint></ProxyEndpoints>"
        "<TargetEndpoints><TargetEndpoint>default</TargetEndpoint></TargetEndpoints></APIProxy>\n",
        encoding="utf-8",
    )
    (root / "policies" / "AM-SetHeader.xml").write_text(XML_HEAD + policy, encoding="utf-8")
    (root / "proxies" / "default.xml").write_text(
        XML_HEAD + '<ProxyEndpoint name="default"><PreFlow name="PreFlow"><Request><Step><Name>SA-Limit</Name></Step>'
        "</Request><Response/></PreFlow><Flows/>"
        '<PostFlow name="PostFlow"><Request/><Response><Step><Name>AM-SetHeader</Name>'
        '<Condition>request.header.X-Tag = "yes"</Condition></Step></Response></PostFlow>'
        "<HTTPProxyConnection><BasePath>/guard</BasePath><VirtualHost>default</VirtualHost></HTTPProxyConnection>"
        '<RouteRule name="default"><TargetEndpoint>default</TargetEndpoint></RouteRule></ProxyEndpoint>\n',
        encoding="utf-8",
    )
    (root / "targets" / "default.xml").write_text(
        XML_HEAD + '<TargetEndpoint name="default"><PreFlow name="PreFlow"><Request/><Response/></PreFlow><Flows/>'
        '<PostFlow name="PostFlow"><Request/><Response/></PostFlow>'
        "<HTTPTargetConnection><URL>http://backend.example/guard</URL></HTTPTargetConnection></TargetEndpoint>\n",
        encoding="utf-8",
    )
    return parent / name


def test_CP8_X08_an_added_guard_is_checked_for_the_side_of_the_flow_it_sits_on(tmp_path: Path) -> None:
    """[CP8-X08] A new choice guarded exactly as a2m guards a response-side step (reading the sent request snapshot)
    is accepted after the backend call, where that snapshot exists, and refused before it, where it does not."""
    import xml.etree.ElementTree as ET

    app = generate(write_response_guard_bundle(tmp_path / "in"), tmp_path / "response-guard-proxy" / "mule-app")
    root = ET.fromstring(app.text)
    guards = [
        when.get("expression", "")
        for when in root.iter("{http://www.mulesoft.org/schema/mule/core}when")
        if "a2mSentRequest" in when.get("expression", "")
    ]
    assert len(guards) == 1, app.text  # a2m's own response-side guard
    added = (
        f"        <choice>\n            <when expression={quoteattr(guards[0])}>\n"
        '                <set-variable variableName="cp8Seen" value="#[\'yes\']" />\n'
        "            </when>\n        </choice>\n"
    )
    response_side = insert_before(app.text, '<set-variable variableName="httpStatus" value="#[attributes', added)
    request_side = insert_before(app.text, "<http:request ", added)
    assert response_side.index("cp8Seen") > response_side.index("<http:request ")

    runner = RecordingRunner()
    provider = ScriptedProvider([lambda request: fixed(request_side), lambda request: fixed(response_side)])
    loop = run(app, provider, runner, max_fix_attempts=2)

    first, second = loop.attempts
    assert first.changed_files == () and "refused" in first.reason and "request side" in first.reason, first.reason
    assert second.changed_files == (FLOW_REL,), second.reason
    assert len(runner.started) == 2
    assert "cp8Seen" in runner.snapshots[1]


# ---------------------------------------------------------------- CP8-X09..X10: AI steps, template steps, records


JS_APP = "js-header-proxy"
JS_SOURCE = "context.setVariable('response.header.X-Api-Version', '2');\n"
WRONG_STEP = (
    '<set-variable xmlns="http://www.mulesoft.org/schema/mule/core" variableName="responseHeaders" '
    "value=\"#[vars.responseHeaders default {} ++ {'X-Api-Version': '1'}]\"/>"
)
VERSION = re.compile(r"'X-Api-Version': '(\d)'")


def write_js_bundle(parent: Path) -> Path:
    """One PostFlow response JavaScript step, JS-SetVersion (as in CP8-T18)."""
    root = parent / JS_APP / "apiproxy"
    for sub in ("policies", "proxies", "targets", "resources/jsc"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    (root / f"{JS_APP}.xml").write_text(
        XML_HEAD + f'<APIProxy revision="1" name="{JS_APP}"><Policies><Policy>JS-SetVersion</Policy></Policies>'
        "<ProxyEndpoints><ProxyEndpoint>default</ProxyEndpoint></ProxyEndpoints>"
        "<TargetEndpoints><TargetEndpoint>default</TargetEndpoint></TargetEndpoints></APIProxy>\n",
        encoding="utf-8",
    )
    (root / "policies" / "JS-SetVersion.xml").write_text(
        XML_HEAD + '<Javascript name="JS-SetVersion" timeLimit="200"><ResourceURL>jsc://set-version.js</ResourceURL>'
        "</Javascript>\n",
        encoding="utf-8",
    )
    (root / "resources" / "jsc" / "set-version.js").write_text(JS_SOURCE, encoding="utf-8")
    (root / "proxies" / "default.xml").write_text(
        XML_HEAD + '<ProxyEndpoint name="default"><PreFlow name="PreFlow"><Request/><Response/></PreFlow><Flows/>'
        '<PostFlow name="PostFlow"><Request/><Response><Step><Name>JS-SetVersion</Name></Step></Response></PostFlow>'
        "<HTTPProxyConnection><BasePath>/js-header</BasePath><VirtualHost>default</VirtualHost></HTTPProxyConnection>"
        '<RouteRule name="default"><TargetEndpoint>default</TargetEndpoint></RouteRule></ProxyEndpoint>\n',
        encoding="utf-8",
    )
    (root / "targets" / "default.xml").write_text(
        XML_HEAD + '<TargetEndpoint name="default"><PreFlow name="PreFlow"><Request/><Response/></PreFlow><Flows/>'
        '<PostFlow name="PostFlow"><Request/><Response/></PostFlow>'
        "<HTTPTargetConnection><URL>http://backend.example/js-header</URL></HTTPTargetConnection></TargetEndpoint>\n",
        encoding="utf-8",
    )
    return parent / JS_APP


def write_js_golden(golden_root: Path) -> Path:
    folder = golden_root / JS_APP
    folder.mkdir(parents=True)
    exchange = {
        "name": "version-header",
        "calls": [
            {
                "after_ms": 0,
                "request": {"method": "GET", "path": "/js-header", "headers": {}, "body": ""},
                "response": {
                    "status": 200,
                    "headers": {"X-Api-Version": "2", "Content-Type": "application/json"},
                    "body": '{"ok":true}',
                },
            }
        ],
        "backend_calls": [
            {
                "method": "GET",
                "path": "/js-header",
                "headers": {},
                "body": "",
                "response": {"status": 200, "headers": {"Content-Type": "application/json"}, "body": '{"ok":true}'},
            }
        ],
    }
    (folder / "version-header.json").write_text(json.dumps(exchange), encoding="utf-8")
    return golden_root


def js_app(tmp_path: Path) -> App:
    from a2m.ai.fake import FakeProvider

    llm = tmp_path / "llm"
    llm.mkdir(parents=True)
    answer = {"status": "translated", "confidence": "high", "notes": "cp8-x", "mule": WRONG_STEP, "writes": None}
    (llm / "javascript.JS-SetVersion.json").write_text(json.dumps(answer), encoding="utf-8")
    app = generate(write_js_bundle(tmp_path / "in"), tmp_path / JS_APP / "mule-app", provider=FakeProvider(llm))
    assert "'X-Api-Version': '1'" in app.text and 'doc:name="JS-SetVersion"' in app.text
    return app


def forward(backend_url: str, request: Any, extra: dict[str, str]) -> Any:
    from a2m.verify import HttpResponse

    target = urlsplit(backend_url)
    sent = {
        k: v
        for k, v in dict(request.headers or {}).items()
        if str(k).lower() not in ("host", "content-length", "connection", "transfer-encoding")
    }
    conn = http.client.HTTPConnection(target.hostname or "127.0.0.1", target.port or 80, timeout=10)
    try:
        conn.request(request.method, request.path, body=request.body or None, headers=sent)
        got = conn.getresponse()
        body = got.read()
        return HttpResponse(got.status, {**dict(got.getheaders()), **extra}, body)
    finally:
        conn.close()


class VersionRunner(RecordingRunner):
    """The JS proxy's app: forwards each call to the backend and answers with the X-Api-Version header its
    proxy.xml sets now (re-read on every call)."""

    def start(self, app: Any, *, backend_url: str) -> Any:
        self.started.append(app.name)
        flow = Path(app.app_dir) / FLOW_REL
        self.snapshots.append(flow.read_text(encoding="utf-8"))

        class _Handle:
            running = True
            base_url = "http://fake-version.invalid/app"

            def send(self, request: Any) -> Any:
                found = VERSION.search(flow.read_text(encoding="utf-8"))
                return forward(backend_url, request, {"X-Api-Version": found.group(1) if found else "none"})

            def stop(self) -> None:
                pass

        return _Handle()


def test_CP8_X09_an_ai_step_may_change_in_place_but_a_template_step_may_not(tmp_path: Path) -> None:
    """[CP8-X09] Changing a value inside a template step (orders-api's Spike-Arrest bucket size) is refused; changing
    the AI-translated JS-SetVersion step in place (on the response side) is written, re-tested and kept."""
    orders = orders_app(tmp_path / "orders")
    template_change = orders.text.replace("/ 2000)]", "/ 4000)]", 1)
    assert template_change != orders.text
    runner = RecordingRunner()
    loop = run(orders, ScriptedProvider([lambda request: fixed(template_change)]), runner)
    assert_refused(orders, loop, runner, "Spike-Arrest")

    app = js_app(tmp_path / "js")
    golden = write_js_golden(tmp_path / "golden")
    version_runner = VersionRunner()
    corrected = app.text.replace("'X-Api-Version': '1'", "'X-Api-Version': '2'")
    loop = run(app, ScriptedProvider([lambda request: fixed(corrected, confidence="high")]), version_runner,
               golden=golden)

    assert loop.result.type.value == "golden", loop.attempts
    assert loop.attempts[0].helped is True
    assert "'X-Api-Version': '2'" in flow_text(app)


def test_CP8_X10_a_kept_fix_records_its_steps_and_confidence_and_low_confidence_is_flagged(tmp_path: Path) -> None:
    """[CP8-X10] A kept fix of the AI-translated JS-SetVersion step with confidence 'low' records the step it changed
    and the confidence (also in its JSON data), and the result carries a review flag for that step."""
    app = js_app(tmp_path)
    golden = write_js_golden(tmp_path / "golden")
    corrected = app.text.replace("'X-Api-Version': '1'", "'X-Api-Version': '2'")

    loop = run(app, ScriptedProvider([lambda request: fixed(corrected, confidence="low")]), VersionRunner(),
               golden=golden)

    attempt = loop.attempts[0]
    assert attempt.helped is True
    assert attempt.changed_steps == ("JS-SetVersion",)
    assert attempt.confidence == "low"
    data = json.loads(json.dumps(attempt.to_json_data()))
    assert data["changed_steps"] == ["JS-SetVersion"] and data["confidence"] == "low"
    flags = [flag for flag in loop.result.review_flags if flag.policy == "JS-SetVersion"]
    assert flags and "low" in flags[0].reason


# ---------------------------------------------------------------- CP8-X11: an answer that changes nothing


class FlakyHandle:
    """The rate-limit-proxy app of tests/test_cp8_fix_loop.py: wrong (no 429, no X-Env header) on the first start,
    right on every later start, whatever its files hold."""

    def __init__(self, backend_url: str, right: bool) -> None:
        self.backend_url = backend_url
        self.right = right
        self.calls = 0
        self.running = True
        self.base_url = "http://fake-flaky.invalid/app"

    def send(self, request: Any) -> Any:
        from a2m.verify import HttpResponse

        self.calls += 1
        if self.calls == 3 and self.right:
            body = json.dumps(
                {
                    "fault": {
                        "faultstring": "Spike arrest violation. Allowed rate : 600pm",
                        "detail": {"errorcode": "policies.ratelimit.SpikeArrestViolation"},
                    }
                }
            ).encode()
            return HttpResponse(429, dict(JSON_HEADERS), body)
        headers = {str(k).lower() for k in dict(request.headers or {})}
        return forward(self.backend_url, request, {"X-Env": "prod"} if "x-tag" in headers and self.right else {})

    def stop(self) -> None:
        pass


class FlakyRunner(RecordingRunner):
    def start(self, app: Any, *, backend_url: str) -> Any:
        self.started.append(app.name)
        return FlakyHandle(backend_url, right=len(self.started) > 1)


def test_CP8_X11_an_answer_that_changes_nothing_is_never_credited_as_help(tmp_path: Path) -> None:
    """[CP8-X11] The app fails its first run and passes every later run with nothing changed (a flaky first run).
    Neither an empty fix nor a fix that sends the file back unchanged is credited: both attempts are not helped, say
    the AI changed nothing, and the result stays failed (for review), never battery."""
    from test_cp8_fix_loop import write_rate_limit_bundle

    app = generate(write_rate_limit_bundle(tmp_path / "in"), tmp_path / "rate-limit-proxy" / "mule-app")
    empty = json.dumps({"status": "fixed", "files": {}, "notes": "nothing to do"})
    provider = ScriptedProvider([lambda request: empty, lambda request: fixed(app.text)])

    loop = run(app, provider, FlakyRunner(), max_fix_attempts=2)

    assert len(loop.attempts) == 2
    for attempt in loop.attempts:
        assert attempt.helped is False
        assert attempt.changed_files == ()
        assert "changed nothing" in attempt.reason, attempt.reason
    assert loop.result.type.value == "failed"
    assert "passed after" not in loop.result.message


# ---------------------------------------------------------------- CP8-X12: the fix prompt is missing


def prompts_without_fix(tmp_path: Path) -> Path:
    from importlib import resources

    from a2m.ai.prompts import PROMPT_FILES

    folder = tmp_path / "prompts"
    folder.mkdir()
    for name in PROMPT_FILES.values():
        text = resources.files("a2m").joinpath("prompts", name).read_text(encoding="utf-8")
        (folder / name).write_text(text, encoding="utf-8")
    return folder


def test_CP8_X12_a_missing_fix_prompt_is_reported_before_any_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_cli: Any
) -> None:
    """[CP8-X12] With A2M_PROMPTS_DIR holding only the four translation prompts: the fix loop asks the AI nothing,
    records no attempt and says it did not run because of fix.md; a migrate run that could run the fix loop stops
    up front with one clear line naming fix.md."""
    from test_cp8_fix_loop import AlwaysFailRunner, write_rate_limit_bundle

    monkeypatch.setenv("A2M_PROMPTS_DIR", str(prompts_without_fix(tmp_path)))
    app = generate(write_rate_limit_bundle(tmp_path / "in"), tmp_path / "rate-limit-proxy" / "mule-app")
    provider = ScriptedProvider([])

    loop = run(app, provider, RecordingRunner(), max_fix_attempts=3)

    assert provider.requests == []
    assert loop.attempts == ()
    assert "did not run" in loop.result.message and "fix.md" in loop.result.message
    assert loop.result.type.value == "failed"

    from a2m.engine import generate as generate_stage
    from a2m.engine import parse
    from a2m.verify import make_verify_stage

    out = tmp_path / "out"
    result = run_cli(
        ["migrate", str(tmp_path / "in"), "--out", str(out), "--mock-backends", "--llm", "fake"],
        stages=[parse, generate_stage, make_verify_stage(runner=AlwaysFailRunner())],
    )
    assert result.code != 0
    assert "fix.md" in result.err
    assert len(result.err.strip().splitlines()) == 1
    assert "Traceback" not in result.err


# ---------------------------------------------------------------- CP8-X13: the Claude provider's limits


def fake_sdk(*, stop_reason: str = "end_turn", raise_timeout: bool = False) -> tuple[types.ModuleType, Any]:
    """A stand-in for the anthropic package: records every client copy's options and every create call."""
    calls = types.SimpleNamespace(options=[], creates=[])
    module = types.ModuleType("anthropic")

    class APIError(Exception):
        pass

    class APIConnectionError(APIError):
        pass

    class APITimeoutError(APIConnectionError):
        pass

    class Messages:
        def create(self, **kwargs: Any) -> Any:
            calls.creates.append(kwargs)
            if raise_timeout:
                raise APITimeoutError("Request timed out.")
            text = json.dumps({"status": "fixed", "files": {}})
            return types.SimpleNamespace(
                content=[types.SimpleNamespace(type="text", text=text)], stop_reason=stop_reason
            )

    class Anthropic:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.messages = Messages()

        def with_options(self, **kwargs: Any) -> Anthropic:
            calls.options.append(kwargs)
            return self

    module.APIError = APIError  # type: ignore[attr-defined]
    module.APIConnectionError = APIConnectionError  # type: ignore[attr-defined]
    module.APITimeoutError = APITimeoutError  # type: ignore[attr-defined]
    module.Anthropic = Anthropic  # type: ignore[attr-defined]
    return module, calls


@pytest.mark.parametrize("case", ["cut-off", "timeout"])
def test_CP8_X13_a_cut_off_or_timed_out_fix_answer_ends_the_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """[CP8-X13] A fix answer cut off at the token limit, or a fix request that times out, is one failed attempt that
    ends the loop (the same request would end the same way); fix requests use a client copy with a timeout longer
    than the default and no client retries."""
    from test_cp8_fix_loop import write_rate_limit_bundle

    from a2m.ai.claude import REQUEST_TIMEOUT_SECONDS, ClaudeProvider

    module, calls = fake_sdk(stop_reason="max_tokens", raise_timeout=case == "timeout")
    monkeypatch.setitem(sys.modules, "anthropic", module)
    provider = ClaudeProvider.from_environment({"ANTHROPIC_API_KEY": "sk-cp8-x13-test-key"})
    app = generate(write_rate_limit_bundle(tmp_path / "in"), tmp_path / "rate-limit-proxy" / "mule-app")
    runner = RecordingRunner()

    loop = run(app, provider, runner, max_fix_attempts=3)

    assert len(loop.attempts) == 1, [a.reason for a in loop.attempts]
    assert len(calls.creates) == 1
    assert loop.attempts[0].helped is False
    assert ("cut off" if case == "cut-off" else "timed out") in loop.attempts[0].reason.lower()
    assert len(runner.started) == 1
    assert calls.options and calls.options[0]["max_retries"] == 0
    assert calls.options[0]["timeout"] > REQUEST_TIMEOUT_SECONDS


# ---------------------------------------------------------------- CP8 adversarial round 2 (CP8-X14..)
# Pinned here (findings of round 2):
#
# * one funnel for secrets: every literal credential of the proxy (policy XML, Mule files, properties) and every
#   value of a credential-like header or query parameter the run sees is masked in every field of every request the
#   provider gets, whatever text path it comes through (CP8-X14, X15);
# * a rate limit or overload is waited out with a bounded backoff that honours retry-after, and is not an attempt;
#   a timeout is still never sent again (CP8-X16, X17);
# * a re-indented echo changes nothing and is never credited (CP8-X18);
# * only a2m's own mask format is refused, so a card mask of literal asterisks can be fixed (CP8-X19);
# * the prompt names the steps the AI may change in place and why the previous attempt was not kept (CP8-X20);
# * fix.md is not required when the fix loop cannot run (CP8-X21).

import dataclasses  # noqa: E402

CANARY_PROPERTY = "prop-canary-ZQ81-secret-value"
CANARY_UPSTREAM = "upstream-canary-TK44-auth-value"
CANARY_RECORDED = "recorded-canary-RP09-token-value"
CANARY_RESPONSE = "response-canary-HX27-refresh-value"
CANARIES = (*SECRETS, CANARY_PROPERTY, CANARY_UPSTREAM, CANARY_RECORDED, CANARY_RESPONSE)


def request_texts(request: Any) -> dict[str, str]:
    """Every text field of an AiRequest (whatever fields it has)."""
    return {
        item.name: str(getattr(request, item.name))
        for item in dataclasses.fields(request)
        if isinstance(getattr(request, item.name), str)
    }


class CanaryRunner(RecordingRunner):
    """The secret proxy's app: really calls the mock backend (path without the query a2m's policy adds, without the
    partner key header, with an extra X-Upstream-Auth header), and answers 200 with a body that echoes a value it
    read from the app's properties."""

    def start(self, app: Any, *, backend_url: str) -> Any:
        from a2m.verify import HttpResponse

        self.started.append(app.name)
        self.snapshots.append((Path(app.app_dir) / FLOW_REL).read_text(encoding="utf-8"))
        target = urlsplit(backend_url)

        class _Handle:
            running = True
            base_url = "http://fake-canary.invalid/app"

            def send(self, request: Any) -> Any:
                conn = http.client.HTTPConnection(target.hostname or "127.0.0.1", target.port or 80, timeout=10)
                try:
                    conn.request(
                        request.method, request.path.partition("?")[0], headers={"X-Upstream-Auth": CANARY_UPSTREAM}
                    )
                    conn.getresponse().read()
                finally:
                    conn.close()
                body = json.dumps({"ok": True, "echo": CANARY_PROPERTY}).encode()
                return HttpResponse(200, dict(JSON_HEADERS), body)

            def stop(self) -> None:
                pass

        return _Handle()


def secret_app(tmp_path: Path) -> App:
    app = generate(write_secret_bundle(tmp_path / "in"), tmp_path / "secret-proxy" / "mule-app")
    props = app.app_dir / "src" / "main" / "resources" / "config.properties"
    assert props.is_file()
    with props.open("a", encoding="utf-8") as handle:
        handle.write(f"\npartner.api.secret={CANARY_PROPERTY}\n")
    return app


def write_secret_golden(golden_root: Path) -> Path:
    folder = golden_root / "secret-proxy"
    folder.mkdir(parents=True)
    exchange = {
        "name": "partner-call",
        "calls": [
            {
                "after_ms": 0,
                "request": {"method": "GET", "path": "/secret", "headers": {}, "body": ""},
                "response": {
                    "status": 200,
                    "headers": {"Content-Type": "application/json", "X-Refresh-Token": CANARY_RESPONSE},
                    "body": '{"ok":true}',
                },
            }
        ],
        "backend_calls": [
            {
                "method": "GET",
                "path": f"/secret?access_token={SECRETS[1]}",
                "headers": {"X-Partner-Api-Key": SECRETS[0], "X-Client-Token": CANARY_RECORDED},
                "body": "",
                "response": {"status": 200, "headers": {"Content-Type": "application/json"}, "body": '{"ok":true}'},
            }
        ],
    }
    (folder / "partner-call.json").write_text(json.dumps(exchange), encoding="utf-8")
    return golden_root


def assert_no_canary(provider: ScriptedProvider, loop: Any, canaries: tuple[str, ...]) -> None:
    assert provider.requests, "the AI was never asked"
    for request in provider.requests:
        for field_name, text in request_texts(request).items():
            for canary in canaries:
                assert canary not in text, (field_name, canary)
    for attempt in loop.attempts:
        for canary in canaries:
            assert canary not in json.dumps(attempt.to_json_data()), canary


def test_CP8_X14_a_failing_header_set_case_never_sends_the_literal_key_to_the_provider(tmp_path: Path) -> None:
    """[CP8-X14] Battery run of the secret proxy whose app reaches the mock backend without the partner key header:
    the header-set case fails with a header diff that holds the literal key, and no literal credential is in any
    field of the request the provider gets."""
    app = secret_app(tmp_path)
    runner = CanaryRunner()
    provider = ScriptedProvider([lambda request: json.dumps({"status": "cannot_fix", "reason": "cp8-x14"})])

    loop = run(app, provider, runner)

    diffs = "\n".join(case.diff for case in loop.result.cases if not case.passed)
    assert "X-Partner-Api-Key" in diffs and "expected" in diffs, diffs  # the header diff path really ran
    assert "X-Partner-Api-Key" in provider.requests[0].prompt
    assert_no_canary(provider, loop, CANARIES)


def test_CP8_X15_no_canary_from_any_source_reaches_any_field_of_the_fix_request(tmp_path: Path) -> None:
    """[CP8-X15] Golden run of the secret proxy, with distinct canary secrets in every source: policy XML literals
    (header, query parameter, payload field, variable), a credential in config.properties the app echoes in its body,
    a header the backend received that the recording lacks, recorded backend and response header values the app does
    not reproduce. Every canary's diff line reaches the prompt, and no canary is in any field of any request the
    provider gets, nor in the attempt's record."""
    app = secret_app(tmp_path)
    golden = write_secret_golden(tmp_path / "golden")
    runner = CanaryRunner()
    provider = ScriptedProvider([lambda request: json.dumps({"status": "cannot_fix", "reason": "cp8-x15"})])

    loop = run(app, provider, runner, golden=golden)

    assert loop.result.type.value == "failed", loop.result.message
    prompt = provider.requests[0].prompt
    lowered = prompt.lower()
    for shown in ("x-upstream-auth", "x-client-token", "x-refresh-token", "x-partner-api-key", "access_token", "echo"):
        assert shown in lowered, shown  # each canary's diff line was really in the prompt (masked)
    assert_no_canary(provider, loop, CANARIES)


# ---------------------------------------------------------------- CP8-X16..X17: rate limits are waited out


def rate_sdk(errors: list[Any]) -> tuple[types.ModuleType, Any]:
    """A stand-in for the anthropic package whose create raises each of ``errors`` in turn (an error factory taking
    the module, or None to answer with a fix that adds one logger to the shown proxy.xml)."""
    calls = types.SimpleNamespace(options=[], creates=[])
    module = types.ModuleType("anthropic")

    class APIError(Exception):
        pass

    class APIConnectionError(APIError):
        pass

    class APITimeoutError(APIConnectionError):
        pass

    class APIStatusError(APIError):
        def __init__(self, message: str, status_code: int, headers: dict[str, str] | None = None) -> None:
            super().__init__(message)
            self.status_code = status_code
            self.response = types.SimpleNamespace(headers=dict(headers or {}))

    class Messages:
        def create(self, **kwargs: Any) -> Any:
            calls.creates.append(kwargs)
            factory = errors.pop(0) if errors else None
            if factory is not None:
                raise factory(module)
            prompt = kwargs["messages"][0]["content"]
            shown = shown_flow(types.SimpleNamespace(prompt=prompt))
            text = fixed(insert_after(shown, "<http:listener ", LOGGER))
            return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)], stop_reason="end_turn")

    class Anthropic:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.messages = Messages()

        def with_options(self, **kwargs: Any) -> Anthropic:
            calls.options.append(kwargs)
            return self

    module.APIError = APIError  # type: ignore[attr-defined]
    module.APIConnectionError = APIConnectionError  # type: ignore[attr-defined]
    module.APITimeoutError = APITimeoutError  # type: ignore[attr-defined]
    module.APIStatusError = APIStatusError  # type: ignore[attr-defined]
    module.Anthropic = Anthropic  # type: ignore[attr-defined]
    return module, calls


def status_error(status: int, headers: dict[str, str] | None = None) -> Callable[[Any], BaseException]:
    return lambda module: module.APIStatusError(f"Error code: {status}", status, headers)


def claude_with(module: types.ModuleType, sleeps: list[float]) -> Any:
    from a2m.ai.claude import ClaudeProvider

    client = module.Anthropic()  # type: ignore[attr-defined]
    return ClaudeProvider(
        client,
        "claude-test",
        (module.APIError,),  # type: ignore[attr-defined]
        "sk-cp8-x16-test-key",
        (module.APITimeoutError,),  # type: ignore[attr-defined]
        connection_types=(module.APIConnectionError,),  # type: ignore[attr-defined]
        sleep=sleeps.append,
    )


def test_CP8_X16_a_rate_limited_fix_request_is_waited_out_and_is_one_attempt(tmp_path: Path) -> None:
    """[CP8-X16] The first fix request gets a 429 with retry-after 7, the second an answer: a2m waits 7 seconds, sends
    it again, writes and re-tests the fix, and records exactly 1 attempt (the client copy still has no retries)."""
    from test_cp8_fix_loop import write_rate_limit_bundle

    module, calls = rate_sdk([status_error(429, {"retry-after": "7"})])
    sleeps: list[float] = []
    app = generate(write_rate_limit_bundle(tmp_path / "in"), tmp_path / "rate-limit-proxy" / "mule-app")
    runner = RecordingRunner()

    loop = run(app, claude_with(module, sleeps), runner, max_fix_attempts=1)

    assert len(calls.creates) == 2
    assert sleeps == [7.0]
    assert len(loop.attempts) == 1
    assert loop.attempts[0].changed_files == (FLOW_REL,), loop.attempts[0].reason
    assert "provider failed" not in loop.attempts[0].reason
    assert len(runner.started) == 2 and "cp8 fix check" in runner.snapshots[1]
    assert calls.options and calls.options[0]["max_retries"] == 0


def test_CP8_X17_backoff_is_bounded_and_only_for_errors_that_may_pass_later() -> None:
    """[CP8-X17] A 529 that never passes is sent 5 times with growing waits; a retry-after above the cap waits the cap;
    a connection error is sent again; a 400 and a timeout are sent once."""
    from a2m.ai.claude import MAX_BACKOFF_SECONDS
    from a2m.ai.provider import AiRequest, ItemKind, ProviderError, ProviderLimitError

    flow = '<mule>\n    <http:listener path="/x" />\n</mule>'
    request = AiRequest(ItemKind.FIX, "p", "o", f"### src/main/mule/proxy.xml\n\n```xml\n{flow}\n```")

    module, calls = rate_sdk([status_error(529)] * 10)
    sleeps: list[float] = []
    with pytest.raises(ProviderError) as raised:
        claude_with(module, sleeps).complete(request)
    assert not isinstance(raised.value, ProviderLimitError)
    assert len(calls.creates) == 5
    assert sleeps == sorted(sleeps) and len(sleeps) == 4 and max(sleeps) <= MAX_BACKOFF_SECONDS and sleeps[0] > 0

    module, calls = rate_sdk([status_error(429, {"retry-after": "600"})] * 2)
    sleeps = []
    assert '"status": "fixed"' in claude_with(module, sleeps).complete(request)
    assert sleeps == [MAX_BACKOFF_SECONDS, MAX_BACKOFF_SECONDS] and len(calls.creates) == 3

    module, calls = rate_sdk([lambda m: m.APIConnectionError("Connection error.")])
    sleeps = []
    assert '"status": "fixed"' in claude_with(module, sleeps).complete(request)
    assert len(calls.creates) == 2 and len(sleeps) == 1

    for factory, expected in ((status_error(400), ProviderError), (lambda m: m.APITimeoutError("timed out"), ProviderLimitError)):
        module, calls = rate_sdk([factory])
        sleeps = []
        with pytest.raises(expected):
            claude_with(module, sleeps).complete(request)
        assert len(calls.creates) == 1 and sleeps == []


# ---------------------------------------------------------------- CP8-X18: a re-indented echo changes nothing


def test_CP8_X18_a_re_indented_echo_is_never_credited_as_help(tmp_path: Path) -> None:
    """[CP8-X18] With the flaky app of CP8-X11 (fails its first run, passes later runs), an answer that is the
    generated proxy.xml re-indented (4 spaces to 2) is not helped, says the AI changed nothing, writes nothing, and
    the result stays failed."""
    from test_cp8_fix_loop import write_rate_limit_bundle

    app = generate(write_rate_limit_bundle(tmp_path / "in"), tmp_path / "rate-limit-proxy" / "mule-app")
    reindented = "".join(
        " " * ((len(line) - len(line.lstrip(" "))) // 2) + line.lstrip(" ") for line in lines_of(app.text)
    )
    assert reindented != app.text
    provider = ScriptedProvider([lambda request: fixed(reindented, confidence="high")])

    loop = run(app, provider, FlakyRunner(), max_fix_attempts=1)

    attempt = loop.attempts[0]
    assert attempt.helped is False, attempt.reason
    assert attempt.changed_files == ()
    assert "changed nothing" in attempt.reason
    assert flow_text(app) == app.text
    assert loop.result.type.value == "failed"


# ---------------------------------------------------------------- CP8-X19: literal asterisks are not a mask


def test_CP8_X19_a_fix_with_literal_asterisks_in_an_ai_step_is_not_refused(tmp_path: Path) -> None:
    """[CP8-X19] A fix of the AI-translated JS-SetVersion step that also sets a card mask of literal asterisks
    ('****-****-****-' ++ ...) is written, re-tested and kept; a2m's own mask format stays refused (CP8-X06)."""
    app = js_app(tmp_path)
    golden = write_js_golden(tmp_path / "golden")
    corrected = app.text.replace(
        "{'X-Api-Version': '1'}", "{'X-Api-Version': '2', 'X-Card-Shown': '****-****-****-' ++ '4242'}"
    )
    assert corrected != app.text

    loop = run(app, ScriptedProvider([lambda request: fixed(corrected, confidence="high")]), VersionRunner(),
               golden=golden)

    attempt = loop.attempts[0]
    assert attempt.helped is True, attempt.reason
    assert "****-****-****-" in flow_text(app)
    assert loop.result.type.value == "golden"


# ---------------------------------------------------------------- CP8-X20: the prompt's context


def test_CP8_X20_the_prompt_names_the_ai_steps_and_why_the_previous_attempt_was_refused(tmp_path: Path) -> None:
    """[CP8-X20] The fix prompt lists the steps the AI may change in place (JS-SetVersion for the JS proxy; none for
    orders-api, whose steps are all templates) and, from the second attempt, why the previous one was not kept."""
    app = js_app(tmp_path / "js")
    golden = write_js_golden(tmp_path / "golden")
    refused = without(app.text, "<http:request ")
    provider = ScriptedProvider(
        [lambda request: fixed(refused), lambda request: json.dumps({"status": "cannot_fix", "reason": "cp8-x20"})]
    )

    loop = run(app, provider, VersionRunner(), max_fix_attempts=2, golden=golden)

    assert len(provider.requests) == 2
    section = re.compile(r"change in place.*?^- JS-SetVersion$", re.DOTALL | re.MULTILINE)
    assert section.search(provider.requests[0].prompt), provider.requests[0].prompt
    assert "first attempt" in provider.requests[0].prompt
    second = provider.requests[1].prompt
    reason = loop.attempts[0].reason
    assert "refused" in reason
    assert "Attempt 1 was not kept" in second and reason[:80] in second

    orders = orders_app(tmp_path / "orders")
    orders_provider = ScriptedProvider([lambda request: json.dumps({"status": "cannot_fix", "reason": "cp8-x20"})])
    run(orders, orders_provider, RecordingRunner())
    assert re.search(r"\n\s*none: every step of this proxy was generated from a template", orders_provider.requests[0].prompt)


# ---------------------------------------------------------------- CP8-X21: fix.md only when the loop can run


@pytest.mark.parametrize(
    "flags",
    [
        ["--no-runtime", "--mock-backends"],
        [],
        ["--mock-backends", "--max-fix-attempts", "0"],
    ],
    ids=["no-runtime", "no-mock-or-golden", "max-fix-attempts-0"],
)
def test_CP8_X21_fix_md_is_not_required_when_the_fix_loop_cannot_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_cli: Any, flags: list[str]
) -> None:
    """[CP8-X21] With fix.md missing, a migrate run that can never run the fix loop (--no-runtime, neither
    --mock-backends nor --golden, or --max-fix-attempts 0) is not stopped for fix.md: it ends exactly as the same run
    with every prompt file present."""
    from test_cp8_fix_loop import AlwaysFailRunner, write_rate_limit_bundle

    from a2m.engine import generate as generate_stage
    from a2m.engine import parse
    from a2m.verify import make_verify_stage

    write_rate_limit_bundle(tmp_path / "in")

    def migrate(out: Path) -> Any:
        return run_cli(
            ["migrate", str(tmp_path / "in"), "--out", str(out), "--llm", "fake", *flags],
            stages=[parse, generate_stage, make_verify_stage(runner=AlwaysFailRunner())],
        )

    baseline = migrate(tmp_path / "out-baseline")
    monkeypatch.setenv("A2M_PROMPTS_DIR", str(prompts_without_fix(tmp_path)))
    without_fix = migrate(tmp_path / "out")

    assert "fix.md" not in without_fix.err
    assert without_fix.code == baseline.code, without_fix.err
    assert (tmp_path / "out" / "rate-limit-proxy").is_dir()


# ---------------------------------------------------------------- CP8 adversarial round 3 (CP8-X22..)
# Pinned here (findings of round 3):
#
# * the fix prompt holds only the policies a fix can be about (those of the generated steps a failing test is about,
#   and the AI-translated ones), each masked by structure; every other policy is listed by name and type only, and a
#   golden failure tied to no step says so (CP8-X22, X23);
# * a canary planted in every credential position of every policy type a2m parses never reaches any field of a fix
#   request, verification.json or run.log, on a golden or a battery run (CP8-X22, X23);
# * credential-like names are matched as whole words, so keyword, author, a keycloak target and WWW-Authenticate stay
#   visible (CP8-X22, X24);
# * masking a diff line once gives the final text: the word "actual" stays, both values are masked (CP8-X25);
# * re-indenting or reflowing a DataWeave script is "changed nothing", never help (CP8-X26).

SWEEP = "sweep-proxy"
SWEEP_CANARIES = {
    "AM header": "cnry-am-header-7Q1X",
    "AM query": "cnry-am-query-8W2Y",
    "AM variable": "cnry-am-variable-9E3Z",
    "AM form": "cnry-am-form-1R4A",
    "AM payload": "cnry-am-payload-2T5B",
    "SC header": "cnry-sc-header-3Y6C",
    "SC userinfo": "cnry-sc-userinfo-4U7D",
    "SC query": "cnry-sc-query-5I8E",
    "KVM value": "cnry-kvm-value-6O9F",
    "BA user": "cnry-ba-user-7P0G",
    "BA password": "cnry-ba-password-8A1H",
    "VerifyAPIKey": "cnry-verify-key-9S2J",
    "OAuthV2": "cnry-oauth-token-1D3K",
    "RF header": "cnry-rf-header-2F4L",
    "JS source": "cnry-js-source-3G5M",
    "target query": "cnry-target-query-4H6N",
    "target userinfo": "cnry-target-user-5J7P",
}
# Look-alikes of credentials that are not secrets: each must stay visible.
LOOK_ALIKES = ("electronics", "Hemingway", "/realms/acme", 'Basic realm="orders"')


def _sweep_policies() -> dict[str, str]:
    c = SWEEP_CANARIES
    return {
        "AM-Creds": (
            '<AssignMessage name="AM-Creds"><Set><Headers>'
            f'<Header name="X-Partner-Api-Key">{c["AM header"]}</Header><Header name="X-Author">Hemingway</Header>'
            f'</Headers><QueryParams><QueryParam name="access_token">{c["AM query"]}</QueryParam>'
            '<QueryParam name="keyword">electronics</QueryParam></QueryParams></Set>'
            f'<AssignVariable><Name>backend.password</Name><Value>{c["AM variable"]}</Value></AssignVariable>'
            '<AssignTo createNew="false" transport="http" type="request"/></AssignMessage>'
        ),
        "AM-Form": (
            '<AssignMessage name="AM-Form"><Set><FormParams>'
            f'<FormParam name="client_secret">{c["AM form"]}</FormParam></FormParams></Set>'
            '<AssignTo createNew="false" transport="http" type="request"/></AssignMessage>'
        ),
        "AM-Payload": (
            '<AssignMessage name="AM-Payload"><Set>'
            f'<Payload contentType="application/json">{{"password": "{c["AM payload"]}", "keyword": "electronics"}}'
            '</Payload></Set><AssignTo createNew="false" transport="http" type="request"/></AssignMessage>'
        ),
        "SC-Geo": (
            '<ServiceCallout name="SC-Geo"><Request variable="geoRequest"><Set><Headers>'
            f'<Header name="x-api-key">{c["SC header"]}</Header></Headers></Set></Request>'
            '<Response>geoResponse</Response><HTTPTargetConnection>'
            f'<URL>https://geo-user:{c["SC userinfo"]}@maps.example.com/geocode/json?address=x&amp;key={c["SC query"]}'
            "</URL></HTTPTargetConnection></ServiceCallout>"
        ),
        "KVM-Init": (
            '<KeyValueMapOperations name="KVM-Init" mapIdentifier="backend"><InitialEntries><Entry><Key>'
            f'<Parameter>backend_password</Parameter></Key><Value>{c["KVM value"]}</Value></Entry></InitialEntries>'
            '<Get assignTo="private.backend_password"><Key><Parameter>backend_password</Parameter></Key></Get>'
            "<Scope>environment</Scope></KeyValueMapOperations>"
        ),
        "BA-Encode": (
            '<BasicAuthentication name="BA-Encode"><Operation>Encode</Operation>'
            "<IgnoreUnresolvedVariables>false</IgnoreUnresolvedVariables>"
            f'<User>{c["BA user"]}</User><Password>{c["BA password"]}</Password>'
            '<AssignTo createNew="false">request.header.Authorization</AssignTo></BasicAuthentication>'
        ),
        "VK-Key": f'<VerifyAPIKey name="VK-Key"><APIKey>{c["VerifyAPIKey"]}</APIKey></VerifyAPIKey>',
        "OA-Verify": (
            '<OAuthV2 name="OA-Verify"><Operation>VerifyAccessToken</Operation>'
            f'<ExternalAccessToken>{c["OAuthV2"]}</ExternalAccessToken></OAuthV2>'
        ),
        "RF-Unauthorized": (
            '<RaiseFault name="RF-Unauthorized"><FaultResponse><Set><Headers>'
            '<Header name="WWW-Authenticate">Basic realm="orders"</Header>'
            f'<Header name="X-Debug-Token">{c["RF header"]}</Header></Headers>'
            "<StatusCode>401</StatusCode></Set></FaultResponse></RaiseFault>"
        ),
        "JS-Sign": '<Javascript name="JS-Sign" timeLimit="200"><ResourceURL>jsc://sign.js</ResourceURL></Javascript>',
    }


def write_sweep_bundle(parent: Path) -> Path:
    """Every policy type a2m parses that can carry a literal credential, each with a distinct canary in each
    credential position, a target named keycloak whose URL carries a userinfo and a query secret, and non-secret
    look-alikes (keyword, author, keycloak, WWW-Authenticate)."""
    root = parent / SWEEP / "apiproxy"
    for sub in ("policies", "proxies", "targets", "resources/jsc"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    policies = _sweep_policies()
    for name, text in policies.items():
        (root / "policies" / f"{name}.xml").write_text(XML_HEAD + text + "\n", encoding="utf-8")
    (root / "resources" / "jsc" / "sign.js").write_text(
        f'var apiKey = "{SWEEP_CANARIES["JS source"]}";\ncontext.setVariable("request.header.X-Sig", apiKey);\n',
        encoding="utf-8",
    )
    listed = "".join(f"<Policy>{name}</Policy>" for name in policies)
    (root / f"{SWEEP}.xml").write_text(
        XML_HEAD + f'<APIProxy revision="1" name="{SWEEP}"><Policies>{listed}</Policies>'
        "<ProxyEndpoints><ProxyEndpoint>default</ProxyEndpoint></ProxyEndpoints>"
        "<TargetEndpoints><TargetEndpoint>keycloak</TargetEndpoint></TargetEndpoints></APIProxy>\n",
        encoding="utf-8",
    )
    # Only steps the battery can predict run in the flow; the other policies are in the bundle, used by no step.
    request = "".join(f"<Step><Name>{name}</Name></Step>" for name in ("AM-Creds", "AM-Payload"))
    response = "<Step><Name>JS-Sign</Name></Step>"
    (root / "proxies" / "default.xml").write_text(
        XML_HEAD + f'<ProxyEndpoint name="default"><PreFlow name="PreFlow"><Request>{request}'
        "<Step><Name>RF-Unauthorized</Name><Condition>request.header.fail = \"yes\"</Condition></Step>"
        "</Request><Response/></PreFlow><Flows/>"
        f'<PostFlow name="PostFlow"><Request/><Response>{response}</Response></PostFlow>'
        "<HTTPProxyConnection><BasePath>/sweep</BasePath><VirtualHost>default</VirtualHost></HTTPProxyConnection>"
        '<RouteRule name="default"><TargetEndpoint>keycloak</TargetEndpoint></RouteRule></ProxyEndpoint>\n',
        encoding="utf-8",
    )
    (root / "targets" / "keycloak.xml").write_text(
        XML_HEAD + '<TargetEndpoint name="keycloak"><PreFlow name="PreFlow"><Request/><Response/></PreFlow><Flows/>'
        '<PostFlow name="PostFlow"><Request/><Response/></PostFlow><HTTPTargetConnection>'
        f'<URL>https://kc-admin:{SWEEP_CANARIES["target userinfo"]}@sso.example.com/realms/acme'
        f'?client_secret={SWEEP_CANARIES["target query"]}</URL>'
        "</HTTPTargetConnection></TargetEndpoint>\n",
        encoding="utf-8",
    )
    return parent / SWEEP


def write_sweep_golden(golden_root: Path) -> Path:
    """One recorded exchange the sweep app does not reproduce: a 401 challenge, a body with an author and a category,
    and a backend call to the keycloak realm's token path with a keyword query."""
    folder = golden_root / SWEEP
    folder.mkdir(parents=True)
    exchange = {
        "name": "search",
        "calls": [
            {
                "after_ms": 0,
                "request": {"method": "GET", "path": "/sweep?keyword=electronics", "headers": {}, "body": ""},
                "response": {
                    "status": 401,
                    "headers": {"Content-Type": "application/json", "WWW-Authenticate": 'Basic realm="orders"'},
                    "body": '{"author":"Hemingway","category":"electronics"}',
                },
            }
        ],
        "backend_calls": [
            {
                "method": "GET",
                "path": "/realms/acme/protocol/openid-connect/token?keyword=electronics",
                "headers": {},
                "body": "",
                "response": {"status": 200, "headers": {"Content-Type": "application/json"}, "body": '{"ok":true}'},
            }
        ],
    }
    (folder / "search.json").write_text(json.dumps(exchange), encoding="utf-8")
    return golden_root


class SweepRunner(RecordingRunner):
    """The sweep app: calls the backend at the realm's short token path, and answers 200 with a body whose author and
    category differ from the recording only in a few letters."""

    def start(self, app: Any, *, backend_url: str) -> Any:
        from a2m.verify import HttpResponse

        self.started.append(app.name)
        target = urlsplit(backend_url)

        class _Handle:
            running = True
            base_url = "http://fake-sweep.invalid/app"

            def send(self, request: Any) -> Any:
                conn = http.client.HTTPConnection(target.hostname or "127.0.0.1", target.port or 80, timeout=10)
                try:
                    conn.request("GET", "/realms/acme/token")
                    conn.getresponse().read()
                finally:
                    conn.close()
                body = json.dumps({"author": "E. Hemingway", "category": "Electronics"}).encode()
                return HttpResponse(200, dict(JSON_HEADERS), body)

            def stop(self) -> None:
                pass

        return _Handle()


def _no_canary(where: str, text: str) -> None:
    for label, canary in SWEEP_CANARIES.items():
        assert canary not in text, (where, label)


def test_CP8_X22_a_golden_failure_sends_no_canary_from_any_credential_position_and_keeps_look_alikes(
    tmp_path: Path, run_cli: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[CP8-X22] migrate --golden --llm fake of the sweep proxy (a canary in every credential position of every
    policy type a2m parses, of the custom code and of the target URL): the golden failure gets a fix request, and no
    canary is in any field of it, in verification.json or in run.log. The prompt shows the policy of every generated
    step (the template steps too, placeholdered) and lists the others (unsupported, used by no step) by name and
    type. The non-secret look-alikes (keyword's electronics, author's Hemingway, the keycloak realm path, the
    WWW-Authenticate challenge) stay visible in verification.json; in the prompt every value is a placeholder."""
    from a2m.ai.fake import FakeProvider
    from a2m.engine import generate as generate_stage
    from a2m.engine import parse
    from a2m.verify import make_verify_stage

    exports = tmp_path / "exports"
    write_sweep_bundle(exports)
    golden = write_sweep_golden(tmp_path / "golden")
    requests: list[Any] = []
    original_complete = FakeProvider.complete

    def recording(self: Any, request: Any) -> str:
        if request.kind.value == "fix":
            requests.append(request)
        return original_complete(self, request)

    monkeypatch.setattr(FakeProvider, "complete", recording)
    out = tmp_path / "out"
    run_cli(
        ["migrate", str(exports), "--out", str(out), "--golden", str(golden), "--llm", "fake",
         "--max-fix-attempts", "1"],
        stages=[parse, generate_stage, make_verify_stage(runner=SweepRunner())],
    )

    assert len(requests) == 1, "the golden failure got no fix request"
    request = requests[0]
    for field_name, text in request_texts(request).items():
        _no_canary(f"AiRequest.{field_name}", text)
    verification = (out / SWEEP / "verification.json").read_text(encoding="utf-8")
    _no_canary("verification.json", verification)
    _no_canary("run.log", (out / "run.log").read_text(encoding="utf-8"))
    assert json.loads(verification)["type"] == "failed"

    prompt = request.prompt
    assert "No step could be tied" not in prompt and "no failing test is about them" not in prompt
    for name in ("KVM-Init", "SC-Geo", "BA-Encode", "OA-Verify", "AM-Form"):
        assert f"- {name} (" in prompt, name  # used by no flow step: listed by name and type only
    for tag in ("<KeyValueMapOperations", "<ServiceCallout", "<OAuthV2", "<BasicAuthentication"):
        assert tag not in prompt, tag
    for name in ("AM-Creds", "AM-Payload", "RF-Unauthorized"):
        assert f"policy {name} (" in prompt, name  # the template steps' policies are shown (placeholdered)
    assert 'name="X-Partner-Api-Key"' in prompt and 'name="WWW-Authenticate"' in prompt
    assert "<Javascript" in prompt and "var apiKey" in prompt  # the AI-translated step and its source
    for shown in LOOK_ALIKES:
        assert shown in verification.replace('\\"', '"'), shown
    assert re.search(r"header WWW-Authenticate: expected '«v\d+»'", prompt), prompt
    assert re.search(r"query keyword: expected '«v\d+»', actual absent", prompt), prompt


def test_CP8_X23_policies_a_failing_test_is_about_are_sent_masked_by_structure(tmp_path: Path) -> None:
    """[CP8-X23] Battery run of the sweep proxy whose app answers 500 to everything: the policies of the generated
    steps the failing tests are about are in the prompt with their structure (header and parameter names, the
    WWW-Authenticate header name) and every value a placeholder; unsupported policies (ServiceCallout,
    KeyValueMapOperations, OAuthV2) are listed by name only. Each policy placeholdered on its own holds no canary
    either, and keeps its name, its header names and its operation."""
    from a2m.ai.fake import FakeProvider
    from a2m.verify.placeholders import Placeholders

    app = generate(write_sweep_bundle(tmp_path / "in"), tmp_path / SWEEP / "mule-app", provider=FakeProvider())
    provider = ScriptedProvider([lambda request: json.dumps({"status": "cannot_fix", "reason": "cp8-x23"})])

    loop = run(app, provider, RecordingRunner())

    assert provider.requests, "the AI was never asked"
    tied = {case.policy for case in loop.result.cases if not case.passed and case.policy}
    assert tied, [case.name for case in loop.result.cases]
    prompt = provider.requests[0].prompt
    for request in provider.requests:
        for field_name, text in request_texts(request).items():
            _no_canary(f"AiRequest.{field_name}", text)
    for attempt in loop.attempts:
        _no_canary("attempt", json.dumps(attempt.to_json_data()))
    for name in tied:
        assert f"policy {name} (" in prompt, name
    for tag in ("<KeyValueMapOperations", "<ServiceCallout", "<OAuthV2"):
        assert tag not in prompt, tag
    assert "- KVM-Init (KeyValueMapOperations)" in prompt and "- SC-Geo (ServiceCallout)" in prompt

    table = Placeholders()
    shown_policies: dict[str, str] = {}
    for policy in app.bundle.policies:
        shown = table.apigee(policy.raw_xml)
        assert shown is not None, policy.name
        _no_canary(policy.name, shown)
        assert f'name="{policy.name}"' in shown
        shown_policies[policy.name] = shown
    assert 'name="X-Partner-Api-Key"' in shown_policies["AM-Creds"] and 'name="keyword"' in shown_policies["AM-Creds"]
    assert "<Name>backend.password</Name>" in shown_policies["AM-Creds"]
    assert 'name="WWW-Authenticate"' in shown_policies["RF-Unauthorized"]
    assert "<StatusCode>401</StatusCode>" in shown_policies["RF-Unauthorized"]
    assert "<Operation>Encode</Operation>" in shown_policies["BA-Encode"]
    assert "?address=«v" in shown_policies["SC-Geo"] and "&amp;key=«v" in shown_policies["SC-Geo"]


def test_CP8_X24_non_secret_look_alikes_stay_visible_and_credentials_stay_masked() -> None:
    """[CP8-X24] Run outputs are masked by CP7's rules only: after the run learned a query keyword=electronics and
    author=Hemingway, an X-Author header and a Bearer Authorization header, a keyword, an author, a keycloak realm
    path, a WWW-Authenticate challenge and a primaryKey body field stay visible in diff lines; the Authorization
    token stays masked."""
    from a2m.verify.masking import Masker

    masker = Masker()
    masker.learn_query("keyword=electronics&author=Hemingway&access_token=tok-canary-112233")
    masker.learn_headers({"Authorization": "Bearer hdr-canary-445566", "X-Author": "Hemingway-Header"})
    rows = (
        "body field category: expected 'electronics', actual 'Electronics'",
        "body field author: expected 'Hemingway', actual 'E. Hemingway'",
        "backend call: expected POST /realms/acme/protocol/openid-connect/token, got POST /realms/acme/token",
        "header WWW-Authenticate: expected 'Basic realm=\"orders\"', actual missing",
        "body field items[0].primaryKey: expected 'SKU-12345', actual 'SKU-12346'",
        "header X-Author: expected 'Hemingway-Header', actual missing",
        "backend call 1 header authorization: expected 'Bearer hdr-canary-445566', actual missing",
        "body field echo: expected 'hdr-canary-445566', actual missing",
    )
    text = masker.mask("\n".join(rows))
    for visible in (
        "'electronics'", "'Electronics'", "'Hemingway'", "'E. Hemingway'", "/realms/acme/protocol",
        "/realms/acme/token", "'Basic realm=\"orders\"'", "'SKU-12345'", "'Hemingway-Header'",
    ):
        assert visible in text, (visible, text)
    assert "hdr-canary-445566" not in text


def test_CP8_X25_one_mask_pass_over_a_diff_line_is_final_and_keeps_the_line_readable() -> None:
    """[CP8-X25] A credential header diff ('Bearer ...' expected, the bare token actual): one CP7 mask pass (the run
    learned the Authorization header) keeps the word 'actual' and masks both values, and a second pass gives the
    same text; the fix request's placeholdered diff keeps the words and shows both values as placeholders."""
    from a2m.verify import compare
    from a2m.verify.masking import Masker
    from a2m.verify.placeholders import Placeholders

    line = "\n".join(
        compare.body_diffs(b'{"authorization":"Bearer 9f8e7d6c5b4a3210zz"}', b'{"authorization":"9f8e7d6c5b4a3210zz"}')
    )
    masker = Masker()
    masker.learn_headers({"Authorization": "Bearer 9f8e7d6c5b4a3210zz"})

    once = masker.mask(line)

    assert "9f8e7d6c5b4a3210zz" not in once, once
    assert ", actual '" in once, once
    assert masker.mask(once) == once
    shown = Placeholders().diff(line)
    assert re.fullmatch(r"body field authorization: expected '«v1»', actual '«v2»'", shown), shown


class FlakyVersionRunner(RecordingRunner):
    """The JS proxy's app: forwards each call to the backend and answers X-Api-Version 'none' on the first start and
    '2' on every later start, whatever its files hold (a flaky first run)."""

    def start(self, app: Any, *, backend_url: str) -> Any:
        self.started.append(app.name)
        flow = Path(app.app_dir) / FLOW_REL
        self.snapshots.append(flow.read_text(encoding="utf-8"))
        version = "none" if len(self.started) == 1 else "2"

        class _Handle:
            running = True
            base_url = "http://fake-flaky-version.invalid/app"

            def send(self, request: Any) -> Any:
                return forward(backend_url, request, {"X-Api-Version": version})

            def stop(self) -> None:
                pass

        return _Handle()


def test_CP8_X26_a_re_indented_or_reflowed_dataweave_script_is_never_credited_as_help(tmp_path: Path) -> None:
    """[CP8-X26] With an app that fails its first run and passes later runs, two echoes of the AI-translated
    JS-SetVersion step whose DataWeave expression is only reflowed are "changed nothing": one spreads the expression
    over indented lines, one drops the blanks around its punctuation (string literals unchanged). Neither is helped,
    nothing is written and the result stays failed. The same holds for a multi-line Transform Message script
    re-indented or reflowed, while a change inside a string literal or to a token is a change."""
    from a2m.verify.fix_loop import _same_document

    app = js_app(tmp_path)
    golden = write_js_golden(tmp_path / "golden")
    expression = "#[vars.responseHeaders default {} ++ {'X-Api-Version': '1'}]"
    assert expression in app.text
    spread = app.text.replace(
        expression, "#[vars.responseHeaders default {}\n            ++ {\n                'X-Api-Version': '1'\n            }]"
    )
    tight = app.text.replace(expression, "#[vars.responseHeaders default{}++{'X-Api-Version':'1'}]")
    assert spread != app.text and tight != app.text
    provider = ScriptedProvider(
        [lambda request: fixed(spread, confidence="high"), lambda request: fixed(tight, confidence="high")]
    )

    loop = run(app, provider, FlakyVersionRunner(), max_fix_attempts=2, golden=golden)

    assert len(loop.attempts) == 2
    for attempt in loop.attempts:
        assert attempt.helped is False, attempt.reason
        assert attempt.changed_files == () and attempt.changed_steps == ()
        assert "changed nothing" in attempt.reason, attempt.reason
    assert flow_text(app) == app.text
    assert loop.result.type.value == "failed"

    script = (
        '<t xmlns:ee="http://www.mulesoft.org/schema/mule/ee/core"><ee:set-variable variableName="h"><![CDATA[%dw 2.0'
        "\n---\n{{\n{indent}a: 1,\n{indent}b: 'x  y' // note\n}}]]></ee:set-variable></t>"
    )
    four, two = script.format(indent="    "), script.format(indent="  ")
    assert _same_document(four, two)
    assert _same_document(four, four.replace("{\n    a: 1,\n    b:", "{ a: 1, b:"))
    assert not _same_document(four, four.replace("'x  y'", "'x y'"))
    assert not _same_document(four, four.replace("a: 1", "a: 2"))
    assert not _same_document(four, four.replace("// note\n}", "// note }"))


# ---------------------------------------------------------------- CP8 adversarial round 4 (CP8-X27..)
# Pinned here (findings of round 4; the AI is shown placeholders instead of searched-for secrets):
#
# * no literal value of the proxy reaches any field of a fix request, whatever its position: a condition literal, a
#   namespaced WS-Security password in a CDATA SOAP payload, a key query parameter named code, sig or appid, a KVM
#   entry, a ServiceCallout URL, AssignMessage headers and payloads, BasicAuthentication literals, a properties value,
#   custom code, recorded values and headers the backend received (CP8-X27);
# * run outputs (verification.json, run.log) are masked by CP7's rules only, so look-alikes of credentials
#   (api-version, region, keyword, author, keycloak, tokenEndpoint, 'unauthorized') stay visible, exactly as a run
#   without the fix loop shows them (CP8-X28);
# * the AI fixes a value mismatch by writing an existing placeholder, and a2m writes the exact original value
#   (CP8-X29);
# * a golden failure, tied to no step, still gets the policies of the template steps (CP8-X30).

LEAK = "leak-proxy"
LEAK_CANARIES = {
    label: f"cnry27-{label}-{index:02d}Zq"
    for index, label in enumerate(
        (
            "am-header", "am-code", "am-sig", "am-appid", "soap-password", "json-secret", "ba-user", "ba-password",
            "kvm-value", "sc-userinfo", "sc-query", "js-source", "step-condition", "flow-condition", "property",
            "recorded-header", "recorded-response", "recorded-query", "backend-received", "target-userinfo",
            "target-query", "am-variable",
        )
    )
}


def write_leak_bundle(parent: Path, *, basic_auth_step: bool = True) -> Path:
    """A distinct canary in every position a literal value can hold: AssignMessage header, key query parameters
    (code, sig, appid), a CDATA SOAP payload with a namespaced wsse:Password, a JSON payload, a variable,
    BasicAuthentication literals, a KVM entry, a ServiceCallout URL (user information and query), custom code, a Step
    condition on a credential header, a Flow condition on a credential query parameter and the target URL. Without
    ``basic_auth_step`` the BasicAuthentication policy is in no step (its unset variables would stop the battery)."""
    c = LEAK_CANARIES
    root = parent / LEAK / "apiproxy"
    for sub in ("policies", "proxies", "targets", "resources/jsc"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    soap = (
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">'
        "<soapenv:Header><wsse:Security><wsse:UsernameToken><wsse:Username>svc-orders</wsse:Username>"
        f'<wsse:Password>{c["soap-password"]}</wsse:Password></wsse:UsernameToken></wsse:Security></soapenv:Header>'
        "<soapenv:Body><GetOrder/></soapenv:Body></soapenv:Envelope>"
    )
    policies = {
        "AM-Creds": (
            '<AssignMessage name="AM-Creds"><Set><Headers>'
            f'<Header name="X-Partner-Api-Key">{c["am-header"]}</Header></Headers><QueryParams>'
            f'<QueryParam name="code">{c["am-code"]}</QueryParam><QueryParam name="sig">{c["am-sig"]}</QueryParam>'
            f'<QueryParam name="appid">{c["am-appid"]}</QueryParam></QueryParams>'
            f'<Payload contentType="text/xml"><![CDATA[{soap}]]></Payload></Set>'
            f'<AssignVariable><Name>session.region</Name><Value>{c["am-variable"]}</Value></AssignVariable>'
            '<AssignTo createNew="false" transport="http" type="request"/></AssignMessage>'
        ),
        "AM-Json": (
            '<AssignMessage name="AM-Json"><Set><Payload contentType="application/json">'
            f'{{"client_secret": "{c["json-secret"]}", "keyword": "electronics"}}</Payload></Set>'
            '<AssignTo createNew="false" transport="http" type="request"/></AssignMessage>'
        ),
        "BA-Encode": (
            '<BasicAuthentication name="BA-Encode"><Operation>Encode</Operation>'
            f'<IgnoreUnresolvedVariables>false</IgnoreUnresolvedVariables><User>{c["ba-user"]}</User>'
            f'<Password>{c["ba-password"]}</Password><AssignTo createNew="false">request.header.Authorization</AssignTo>'
            "</BasicAuthentication>"
        ),
        "KVM-Init": (
            '<KeyValueMapOperations name="KVM-Init" mapIdentifier="backend"><InitialEntries><Entry><Key>'
            f'<Parameter>backend_password</Parameter></Key><Value>{c["kvm-value"]}</Value></Entry></InitialEntries>'
            '<Get assignTo="private.backend_password"><Key><Parameter>backend_password</Parameter></Key></Get>'
            "<Scope>environment</Scope></KeyValueMapOperations>"
        ),
        "SC-Geo": (
            '<ServiceCallout name="SC-Geo"><Request variable="geoRequest"/><Response>geoResponse</Response>'
            f'<HTTPTargetConnection><URL>https://geo-user:{c["sc-userinfo"]}@maps.example.com/geocode/json'
            f'?address=x&amp;key={c["sc-query"]}</URL></HTTPTargetConnection></ServiceCallout>'
        ),
        "RF-Deny": (
            '<RaiseFault name="RF-Deny"><FaultResponse><Set><StatusCode>403</StatusCode></Set></FaultResponse>'
            "</RaiseFault>"
        ),
        "JS-Sign": '<Javascript name="JS-Sign" timeLimit="200"><ResourceURL>jsc://sign.js</ResourceURL></Javascript>',
    }
    for name, text in policies.items():
        (root / "policies" / f"{name}.xml").write_text(XML_HEAD + text + "\n", encoding="utf-8")
    (root / "resources" / "jsc" / "sign.js").write_text(
        f'var apiKey = "{c["js-source"]}";\ncontext.setVariable("request.header.X-Sig", apiKey);\n', encoding="utf-8"
    )
    listed = "".join(f"<Policy>{name}</Policy>" for name in policies)
    (root / f"{LEAK}.xml").write_text(
        XML_HEAD + f'<APIProxy revision="1" name="{LEAK}"><Policies>{listed}</Policies>'
        "<ProxyEndpoints><ProxyEndpoint>default</ProxyEndpoint></ProxyEndpoints>"
        "<TargetEndpoints><TargetEndpoint>default</TargetEndpoint></TargetEndpoints></APIProxy>\n",
        encoding="utf-8",
    )
    pre = (
        "<Step><Name>AM-Creds</Name></Step>"
        + ("<Step><Name>BA-Encode</Name></Step>" if basic_auth_step else "")
        + f'<Step><Name>RF-Deny</Name><Condition>request.header.x-api-key != "{c["step-condition"]}"</Condition></Step>'
    )
    flows = (
        f'<Flows><Flow name="Keyed"><Condition>request.queryparam.apikey = "{c["flow-condition"]}"</Condition>'
        "<Request><Step><Name>AM-Json</Name></Step></Request><Response/></Flow></Flows>"
    )
    post = "<Request><Step><Name>KVM-Init</Name></Step><Step><Name>SC-Geo</Name></Step></Request>"
    (root / "proxies" / "default.xml").write_text(
        XML_HEAD + f'<ProxyEndpoint name="default"><PreFlow name="PreFlow"><Request>{pre}</Request><Response/>'
        f'</PreFlow>{flows}<PostFlow name="PostFlow">{post}<Response><Step><Name>JS-Sign</Name></Step></Response>'
        "</PostFlow><HTTPProxyConnection><BasePath>/leak</BasePath><VirtualHost>default</VirtualHost>"
        '</HTTPProxyConnection><RouteRule name="default"><TargetEndpoint>default</TargetEndpoint></RouteRule>'
        "</ProxyEndpoint>\n",
        encoding="utf-8",
    )
    (root / "targets" / "default.xml").write_text(
        XML_HEAD + '<TargetEndpoint name="default"><PreFlow name="PreFlow"><Request/><Response/></PreFlow><Flows/>'
        '<PostFlow name="PostFlow"><Request/><Response/></PostFlow><HTTPTargetConnection>'
        f'<URL>https://kc-admin:{c["target-userinfo"]}@sso.example.com/realms/acme?client_secret={c["target-query"]}'
        "</URL></HTTPTargetConnection></TargetEndpoint>\n",
        encoding="utf-8",
    )
    return parent / LEAK


def write_leak_golden(golden_root: Path) -> Path:
    """One recorded exchange whose response header and backend call (a recorded header, a key query value) the
    leak app does not reproduce."""
    c = LEAK_CANARIES
    folder = golden_root / LEAK
    folder.mkdir(parents=True)
    exchange = {
        "name": "leak-call",
        "calls": [
            {
                "after_ms": 0,
                "request": {"method": "GET", "path": "/leak", "headers": {}, "body": ""},
                "response": {
                    "status": 200,
                    "headers": {"Content-Type": "application/json", "X-Refresh-Token": c["recorded-response"]},
                    "body": '{"ok":true}',
                },
            }
        ],
        "backend_calls": [
            {
                "method": "GET",
                "path": f"/realms/acme?access_token={c['recorded-query']}",
                "headers": {"X-Client-Token": c["recorded-header"]},
                "body": "",
                "response": {"status": 200, "headers": {"Content-Type": "application/json"}, "body": '{"ok":true}'},
            }
        ],
    }
    (folder / "leak-call.json").write_text(json.dumps(exchange), encoding="utf-8")
    return golden_root


class LeakRunner(RecordingRunner):
    """The leak app: calls the mock backend with an X-Upstream-Auth header the recording lacks, and answers 200 with
    a body that echoes the credential it read from the app's properties."""

    def __init__(self, status: int = 200) -> None:
        super().__init__()
        self.status = status

    def start(self, app: Any, *, backend_url: str) -> Any:
        from a2m.verify import HttpResponse

        self.started.append(app.name)
        self.snapshots.append((Path(app.app_dir) / FLOW_REL).read_text(encoding="utf-8"))
        target = urlsplit(backend_url)
        status = self.status

        class _Handle:
            running = True
            base_url = "http://fake-leak.invalid/app"

            def send(self, request: Any) -> Any:
                conn = http.client.HTTPConnection(target.hostname or "127.0.0.1", target.port or 80, timeout=10)
                try:
                    conn.request("GET", "/realms/acme", headers={"X-Upstream-Auth": LEAK_CANARIES["backend-received"]})
                    conn.getresponse().read()
                finally:
                    conn.close()
                body = json.dumps({"ok": True, "echo": LEAK_CANARIES["property"]}).encode()
                return HttpResponse(status, dict(JSON_HEADERS), body)

            def stop(self) -> None:
                pass

        return _Handle()


def leak_app(tmp_path: Path, *, basic_auth_step: bool = True) -> App:
    from a2m.ai.fake import FakeProvider

    bundle = write_leak_bundle(tmp_path / "in", basic_auth_step=basic_auth_step)
    app = generate(bundle, tmp_path / LEAK / "mule-app", provider=FakeProvider())
    props = app.app_dir / "src" / "main" / "resources" / "config.properties"
    with props.open("a", encoding="utf-8") as handle:
        handle.write(f"\npartner.api.secret={LEAK_CANARIES['property']}\n")
    return app


def _no_leak(where: str, text: str) -> None:
    for label, canary in LEAK_CANARIES.items():
        assert canary not in text, (where, label)


def test_CP8_X27_no_literal_value_from_any_position_reaches_any_field_of_a_fix_request(tmp_path: Path) -> None:
    """[CP8-X27] A golden run and a battery run of the leak proxy (a distinct canary in every position listed in the
    module comment above, and in the properties, the recording and the headers the backend received): every canary
    really is in the generated app or the run's diffs, the fix request shows the template steps' policies, the
    Mule file and the diffs (names kept), and no canary is in any field of any request the provider gets."""
    app = leak_app(tmp_path / "golden-run")
    for label in ("am-header", "am-code", "am-sig", "am-appid", "soap-password", "json-secret", "step-condition",
                  "flow-condition", "am-variable"):
        assert LEAK_CANARIES[label] in app.text, label  # the generated Mule file really holds it
    golden = write_leak_golden(tmp_path / "golden")
    provider = ScriptedProvider([lambda request: json.dumps({"status": "cannot_fix", "reason": "cp8-x27"})])

    loop = run(app, provider, LeakRunner(), golden=golden)

    for request in provider.requests:
        for field_name, text in request_texts(request).items():
            _no_leak(f"golden AiRequest.{field_name}", text)
    diffs = "\n".join(case.diff for case in loop.result.cases if not case.passed)
    for label in ("recorded-response", "recorded-header", "property", "backend-received"):
        assert LEAK_CANARIES[label] in diffs, (label, diffs)  # the run's diffs really show it
    prompt = provider.requests[0].prompt
    for shown in (
        "policy AM-Creds (", "policy AM-Json (", 'name="X-Partner-Api-Key"', 'name="code"', 'name="sig"',
        'name="appid"', "<wsse:Password>«v", "&quot;client_secret&quot;", "<Javascript", "var apiKey = \"«v",
        "attributes.headers['«v", "- KVM-Init (KeyValueMapOperations)", "- SC-Geo (ServiceCallout)",
        "x-refresh-token", "x-client-token", "x-upstream-auth", "echo",
    ):
        assert shown in prompt or shown in prompt.lower(), shown
    # A quoted selector is a placeholder even when it holds a known header name (CP8 round 8).
    assert "attributes.headers['x-api-key']" not in prompt.lower(), "a quoted selector was shown by value"

    battery_app = leak_app(tmp_path / "battery-run", basic_auth_step=False)
    battery_provider = ScriptedProvider([lambda request: json.dumps({"status": "cannot_fix", "reason": "cp8-x27"})])

    run(battery_app, battery_provider, LeakRunner(status=500))

    assert battery_provider.requests, "the battery failure got no fix request"
    for request in battery_provider.requests:
        for field_name, text in request_texts(request).items():
            _no_leak(f"battery AiRequest.{field_name}", text)


LOOKS = "looks-proxy"
# Values that look like credentials by name or shape but are not: each must stay visible in run outputs.
LOOK_ALIKE_VALUES = (
    "api-version=2023-05-01", "region=westeurope", "'westeurope'", "'2023-05-01T10:00:00Z'", "'unauthorized'",
    "'Hemingway'", "'electronics'", "'https://sso.example.com/realms/keycloak/token'", "'us-east-1'", "'orders-api'",
)


def write_looks_bundle(parent: Path) -> Path:
    """A proxy whose policies hold look-alikes of credentials: a target URL with api-version and region query values,
    a config KVM (region, tokenEndpoint), AssignVariables named oauth.tokenEndpoint and session.region, a VerifyJWT
    audience and a JavaScript step setting auth.status to 'authorized'."""
    root = parent / LOOKS / "apiproxy"
    for sub in ("policies", "proxies", "targets", "resources/jsc"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    policies = {
        "AM-Config": (
            '<AssignMessage name="AM-Config"><AssignVariable><Name>oauth.tokenEndpoint</Name>'
            "<Value>https://sso.example.com/realms/keycloak/token</Value></AssignVariable>"
            "<AssignVariable><Name>session.region</Name><Value>us-east-1</Value></AssignVariable>"
            '<AssignTo createNew="false" transport="http" type="request"/></AssignMessage>'
        ),
        "KVM-Config": (
            '<KeyValueMapOperations name="KVM-Config" mapIdentifier="config"><InitialEntries><Entry><Key>'
            "<Parameter>region</Parameter></Key><Value>us-east-1</Value></Entry><Entry><Key><Parameter>tokenEndpoint"
            "</Parameter></Key><Value>https://sso.example.com/realms/keycloak/token</Value></Entry></InitialEntries>"
            "<Scope>environment</Scope></KeyValueMapOperations>"
        ),
        "JWT-Verify": (
            '<VerifyJWT name="JWT-Verify"><Algorithm>RS256</Algorithm><Source>request.header.jwt</Source>'
            "<Issuer>https://sso.example.com/realms/keycloak</Issuer><Audience>orders-api</Audience></VerifyJWT>"
        ),
        "JS-Status": '<Javascript name="JS-Status" timeLimit="200"><ResourceURL>jsc://status.js</ResourceURL></Javascript>',
    }
    for name, text in policies.items():
        (root / "policies" / f"{name}.xml").write_text(XML_HEAD + text + "\n", encoding="utf-8")
    (root / "resources" / "jsc" / "status.js").write_text(
        'context.setVariable("auth.status", "authorized");\n', encoding="utf-8"
    )
    listed = "".join(f"<Policy>{name}</Policy>" for name in policies)
    (root / f"{LOOKS}.xml").write_text(
        XML_HEAD + f'<APIProxy revision="1" name="{LOOKS}"><Policies>{listed}</Policies>'
        "<ProxyEndpoints><ProxyEndpoint>default</ProxyEndpoint></ProxyEndpoints>"
        "<TargetEndpoints><TargetEndpoint>default</TargetEndpoint></TargetEndpoints></APIProxy>\n",
        encoding="utf-8",
    )
    (root / "proxies" / "default.xml").write_text(
        XML_HEAD + '<ProxyEndpoint name="default"><PreFlow name="PreFlow"><Request><Step><Name>AM-Config</Name>'
        "</Step></Request><Response/></PreFlow><Flows/>"
        '<PostFlow name="PostFlow"><Request/><Response><Step><Name>JS-Status</Name></Step></Response></PostFlow>'
        "<HTTPProxyConnection><BasePath>/looks</BasePath><VirtualHost>default</VirtualHost></HTTPProxyConnection>"
        '<RouteRule name="default"><TargetEndpoint>default</TargetEndpoint></RouteRule></ProxyEndpoint>\n',
        encoding="utf-8",
    )
    (root / "targets" / "default.xml").write_text(
        XML_HEAD + '<TargetEndpoint name="default"><PreFlow name="PreFlow"><Request/><Response/></PreFlow><Flows/>'
        '<PostFlow name="PostFlow"><Request/><Response/></PostFlow><HTTPTargetConnection>'
        "<URL>https://backend.example.test/orders?api-version=2023-05-01&amp;region=westeurope</URL>"
        "</HTTPTargetConnection></TargetEndpoint>\n",
        encoding="utf-8",
    )
    return parent / LOOKS


def write_looks_golden(golden_root: Path) -> Path:
    folder = golden_root / LOOKS
    folder.mkdir(parents=True)
    body = {
        "status": "unauthorized", "author": "Hemingway", "keyword": "electronics", "region": "us-east-1",
        "createdAt": "2023-05-01T10:00:00Z", "tokenEndpoint": "https://sso.example.com/realms/keycloak/token",
        "audience": "orders-api",
    }
    exchange = {
        "name": "looks-call",
        "calls": [
            {
                "after_ms": 0,
                "request": {"method": "GET", "path": "/looks/42", "headers": {}, "body": ""},
                "response": {"status": 200, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body)},
            }
        ],
        "backend_calls": [
            {
                "method": "GET",
                "path": "/orders/42?api-version=2023-05-01&region=westeurope",
                "headers": {},
                "body": "",
                "response": {"status": 200, "headers": {"Content-Type": "application/json"}, "body": '{"ok":true}'},
            }
        ],
    }
    (folder / "looks-call.json").write_text(json.dumps(exchange), encoding="utf-8")
    return golden_root


class LooksRunner(RecordingRunner):
    """The looks app: calls the backend without the region query and answers a body whose every field differs."""

    def start(self, app: Any, *, backend_url: str) -> Any:
        from a2m.verify import HttpResponse

        self.started.append(app.name)
        target = urlsplit(backend_url)

        class _Handle:
            running = True
            base_url = "http://fake-looks.invalid/app"

            def send(self, request: Any) -> Any:
                conn = http.client.HTTPConnection(target.hostname or "127.0.0.1", target.port or 80, timeout=10)
                try:
                    conn.request("GET", "/orders/42?api-version=2023-05-01")
                    conn.getresponse().read()
                finally:
                    conn.close()
                return HttpResponse(200, dict(JSON_HEADERS), b'{"status":"ok"}')

            def stop(self) -> None:
                pass

        return _Handle()


def test_CP8_X28_look_alikes_stay_visible_in_verification_json_and_run_log_as_without_the_fix_loop(
    tmp_path: Path, run_cli: Any
) -> None:
    """[CP8-X28] migrate --golden --llm fake of the looks proxy with --max-fix-attempts 1 (the fix loop runs) and 0
    (it does not): every look-alike (api-version, region, a date, 'unauthorized', an author, a keyword, a keycloak
    tokenEndpoint, a KVM region, a JWT audience) is visible in verification.json and run.log, and the failing test's
    diff is exactly the same text with and without the fix loop."""
    from a2m.engine import generate as generate_stage
    from a2m.engine import parse
    from a2m.verify import make_verify_stage

    exports = tmp_path / "exports"
    write_looks_bundle(exports)
    golden = write_looks_golden(tmp_path / "golden")
    diffs: dict[str, list[str]] = {}
    for attempts in ("1", "0"):
        out = tmp_path / f"out-{attempts}"
        run_cli(
            ["migrate", str(exports), "--out", str(out), "--golden", str(golden), "--llm", "fake",
             "--max-fix-attempts", attempts],
            stages=[parse, generate_stage, make_verify_stage(runner=LooksRunner())],
        )
        data = json.loads((out / LOOKS / "verification.json").read_text(encoding="utf-8"))
        assert data["type"] == "failed", data
        assert len(data["attempts"]) == int(attempts), data["attempts"]
        diffs[attempts] = [case["diff"] for case in data["cases"]]
        verification = json.dumps(data, ensure_ascii=False)
        run_log = (out / "run.log").read_text(encoding="utf-8")
        for shown in LOOK_ALIKE_VALUES:
            assert shown in verification, (attempts, shown, verification)
            assert shown in run_log, (attempts, shown)
        assert "***" not in "\n".join(diffs[attempts])
    assert diffs["1"] == diffs["0"]


def test_CP8_X29_the_ai_fixes_a_value_mismatch_by_writing_an_existing_placeholder(tmp_path: Path) -> None:
    """[CP8-X29] The JS proxy's golden recording expects X-Api-Version '2' and the AI-translated step sets '1'. The
    fake AI reads the placeholders of the expected and actual values from the diff and writes the expected one in
    place of the actual one in the shown proxy.xml, nothing else. a2m writes the exact original value: the file is
    byte for byte the generated one with '1' turned into '2', the re-test passes and the fix is kept."""
    app = js_app(tmp_path)
    golden = write_js_golden(tmp_path / "golden")
    seen: dict[str, str] = {}

    def answer(request: Any) -> str:
        found = re.search(r"(?i)x-api-version: expected '(«v\d+»)', actual '(«v\d+»)'", request.prompt)
        assert found is not None, request.prompt
        expected, actual = found.groups()
        shown = shown_flow(request)
        assert f"'X-Api-Version': '{actual}'" in shown and "'1'" not in shown
        seen.update(expected=expected, actual=actual)
        return fixed(shown.replace(f"'X-Api-Version': '{actual}'", f"'X-Api-Version': '{expected}'"), confidence="high")

    loop = run(app, ScriptedProvider([answer]), VersionRunner(), golden=golden)

    assert seen and seen["expected"] != seen["actual"]
    assert loop.attempts[0].helped is True, loop.attempts[0].reason
    assert loop.result.type.value == "golden"
    assert flow_text(app) == app.text.replace("'X-Api-Version': '1'", "'X-Api-Version': '2'")
    assert "«v" not in flow_text(app)


def write_orders_golden(golden_root: Path) -> Path:
    """One recorded orders-api exchange whose X-Served-By header the app does not reproduce."""
    folder = golden_root / "orders-api"
    folder.mkdir(parents=True)
    exchange = {
        "name": "served-by",
        "calls": [
            {
                "after_ms": 0,
                "request": {"method": "GET", "path": "/orders/42?apikey=a2m-battery-key", "headers": {}, "body": ""},
                "response": {
                    "status": 200,
                    "headers": {"Content-Type": "application/json", "X-Served-By": "apigee-edge"},
                    "body": '{"ok":true}',
                },
            }
        ],
        "backend_calls": [],
    }
    (folder / "served-by.json").write_text(json.dumps(exchange), encoding="utf-8")
    return golden_root


class ServedByRunner(RecordingRunner):
    """The orders-api app: answers 200 with X-Served-By 'a2m' and calls no backend."""

    def start(self, app: Any, *, backend_url: str) -> Any:
        from a2m.verify import HttpResponse

        self.started.append(app.name)

        class _Handle:
            running = True
            base_url = "http://fake-served-by.invalid/app"

            def send(self, request: Any) -> Any:
                return HttpResponse(200, {**JSON_HEADERS, "X-Served-By": "a2m"}, b'{"ok":true}')

            def stop(self) -> None:
                pass

        return _Handle()


def test_CP8_X30_a_golden_failure_gets_the_policies_of_the_template_steps(tmp_path: Path) -> None:
    """[CP8-X30] orders-api (template steps only) with one recorded exchange whose X-Served-By differs, through
    run_with_fixes with golden: the fix prompt holds the XML of Set-Response-Header (the AssignMessage that sets that
    header, placeholdered) and of every other template step, and never says no failing test is about them."""
    app = orders_app(tmp_path)
    golden = write_orders_golden(tmp_path / "golden")
    provider = ScriptedProvider([lambda request: json.dumps({"status": "cannot_fix", "reason": "cp8-x30"})])

    loop = run(app, provider, ServedByRunner(), golden=golden)

    assert loop.result.type.value == "failed", loop.result.message
    prompt = provider.requests[0].prompt
    assert "### AssignMessage policy Set-Response-Header (" in prompt, prompt
    assert '<Header name="X-Served-By">«v' in prompt
    for name in ("Verify-Key", "Spike-Arrest", "Check-IP", "Hourly-Quota", "Extract-Order-Id", "Encode-Basic-Auth",
                 "Add-Target-Headers", "Strip-Internal"):
        assert f"policy {name} (" in prompt, name
    assert "<Rate>30pm</Rate>" in prompt and "<TimeUnit>hour</TimeUnit>" in prompt
    assert "no failing test is about them" not in prompt
    assert re.search(r"(?i)x-served-by: expected '«v\d+»', actual '«v\d+»'", prompt), prompt


# ---------------------------------------------------------------- CP8 adversarial round 5: default-deny placeholders
#
# Round 5 findings: a name-shaped string in a root JSON array or a DataWeave list literal was shown as a selector
# (codex X1); a query item with no '=' (a bare token) was shown as a parameter name (host-alt-correctness A1); a diff
# value written into a DataWeave string literal was put back unescaped for that string (codex X1, O'Reilly); and
# placeholders.py redeclared ItemKind's language values (host-alt-standards A1). Every quoted string literal is now a
# placeholder unless it is an object key or a known name from the IR (default deny), and a placeholder is written
# back spelled for the string literal it stands in.

import xml.etree.ElementTree as ET  # noqa: E402
from xml.sax.saxutils import escape as xml_escape  # noqa: E402

SHAPES = "shapes-proxy"
SHAPES_REL = "src/main/mule/shapes.xml"
SHAPES_SHOWN = re.compile(r"### src/main/mule/shapes\.xml\n\n```xml\n(.*?)\n```", re.DOTALL)
SHAPES_CANARIES = {
    "host": "cnry31host01Zq", "bare": "cnry31bare02Zq", "query": "cnry31qv03Zq", "fragment": "cnry31frag04Zq",
    "quote": "cnry31quote05Zq", "root": "cnry31root06Zq", "condition": "cnry31cond07Zq", "target": "cnry31target08Zq",
}


def _attr(value: str) -> str:
    """``value`` as a double-quoted XML attribute value (quotes included)."""
    return '"' + xml_escape(value, {'"': "&quot;", "\n": "&#10;", "\r": "&#13;", "\t": "&#09;"}) + '"'


def _dw(value: str, quote: str = "'") -> str:
    """``value`` as a DataWeave string literal quoted with ``quote``."""
    spelled = {"\\": "\\\\", "$": "\\$", "\n": "\\n", "\t": "\\t", quote: "\\" + quote}
    return quote + "".join(spelled.get(char, char) for char in value) + quote


def _dw_value(content: str) -> str:
    """The value of a DataWeave string literal whose content is ``content`` (escapes read; an unescaped $ fails, it
    would be interpolation)."""
    out: list[str] = []
    index = 0
    while index < len(content):
        char = content[index]
        assert char != "$", f"an unescaped $ in a DataWeave string: {content!r}"
        if char == "\\":
            following = content[index + 1]
            if following == "u":
                out.append(chr(int(content[index + 2 : index + 6], 16)))
                index += 6
                continue
            out.append({"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}.get(following, following))
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def write_shapes_bundle(parent: Path) -> Path:
    """One AssignMessage step (a header holding a URL with a bare query token and a fragment, a header with quotes and
    a backslash, a JSON payload that is a root array of one name-shaped string) under a condition on a header, and a
    target URL with a bare query token."""
    c = SHAPES_CANARIES
    root = parent / SHAPES / "apiproxy"
    for sub in ("policies", "proxies", "targets"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    (root / "policies" / "AM-Shapes.xml").write_text(
        XML_HEAD + '<AssignMessage name="AM-Shapes"><Set><Headers>'
        f'<Header name="X-Url">https://{c["host"]}.example.com/p?{c["bare"]}&amp;k={c["query"]}#{c["fragment"]}</Header>'
        f'<Header name="X-Quote">O\'Reilly \\ "{c["quote"]}"</Header></Headers>'
        f'<Payload contentType="application/json">["{c["root"]}"]</Payload></Set>'
        '<AssignTo createNew="false" transport="http" type="request"/></AssignMessage>\n',
        encoding="utf-8",
    )
    (root / f"{SHAPES}.xml").write_text(
        XML_HEAD + f'<APIProxy revision="1" name="{SHAPES}"><Policies><Policy>AM-Shapes</Policy></Policies>'
        "<ProxyEndpoints><ProxyEndpoint>default</ProxyEndpoint></ProxyEndpoints>"
        "<TargetEndpoints><TargetEndpoint>default</TargetEndpoint></TargetEndpoints></APIProxy>\n",
        encoding="utf-8",
    )
    (root / "proxies" / "default.xml").write_text(
        XML_HEAD + '<ProxyEndpoint name="default"><PreFlow name="PreFlow"><Request><Step><Name>AM-Shapes</Name>'
        f'<Condition>request.header.x-shape-key = "{c["condition"]}"</Condition></Step></Request><Response/>'
        '</PreFlow><Flows/><PostFlow name="PostFlow"><Request/><Response/></PostFlow>'
        "<HTTPProxyConnection><BasePath>/shapes</BasePath><VirtualHost>default</VirtualHost></HTTPProxyConnection>"
        '<RouteRule name="default"><TargetEndpoint>default</TargetEndpoint></RouteRule></ProxyEndpoint>\n',
        encoding="utf-8",
    )
    (root / "targets" / "default.xml").write_text(
        XML_HEAD + '<TargetEndpoint name="default"><PreFlow name="PreFlow"><Request/><Response/></PreFlow><Flows/>'
        '<PostFlow name="PostFlow"><Request/><Response/></PostFlow><HTTPTargetConnection>'
        f'<URL>https://backend.example.com/orders?api-version=2023-05-01&amp;{c["target"]}</URL>'
        "</HTTPTargetConnection></TargetEndpoint>\n",
        encoding="utf-8",
    )
    return parent / SHAPES


def shapes_mule(fragments: list[str]) -> str:
    """A Mule configuration file holding ``fragments`` (processors, one per line) in one flow."""
    body = "".join(f"        {fragment}\n" for fragment in fragments)
    return (
        XML_HEAD + '<mule xmlns="http://www.mulesoft.org/schema/mule/core">\n    <flow name="shapes-flow">\n'
        f"{body}    </flow>\n</mule>\n"
    )


def shapes_app(tmp_path: Path, extra: str) -> App:
    """The shapes proxy generated by a2m, with ``extra`` written as a second Mule configuration file."""
    app = generate(write_shapes_bundle(tmp_path / "in"), tmp_path / SHAPES / "mule-app")
    (app.app_dir / SHAPES_REL).write_text(extra, encoding="utf-8")
    return app


class ShapesRunner(RecordingRunner):
    """Records every Mule configuration file of the app at every start; its app answers 500 to every call."""

    def __init__(self) -> None:
        super().__init__()
        self.files: list[dict[str, str]] = []

    def start(self, app: Any, *, backend_url: str) -> Any:
        folder = Path(app.app_dir) / "src" / "main" / "mule"
        self.files.append({path.name: path.read_text(encoding="utf-8") for path in sorted(folder.glob("*.xml"))})
        return super().start(app, backend_url=backend_url)


def echo_with_logger(request: Any) -> str:
    """An answer that echoes both shown files, with one logger added to proxy.xml."""
    shapes = SHAPES_SHOWN.search(request.prompt)
    assert shapes is not None, request.prompt[:2000]
    files = {FLOW_REL: insert_after(shown_flow(request), "<http:listener ", LOGGER), SHAPES_REL: shapes.group(1) + "\n"}
    return json.dumps({"status": "fixed", "files": files, "notes": "cp8-x31"})


def _absent(where: str, text: str, canaries: Any) -> None:
    for canary in canaries:
        assert canary not in text, (where, canary)


def test_CP8_X31_a_root_array_or_list_literal_of_one_name_shaped_string_is_a_placeholder(tmp_path: Path) -> None:
    """[CP8-X31] A JSON payload that is a root array of one name-shaped string (["cnry..."]) and a Mule expression
    #[['cnry...']] never reach any field of a fix request, in the policy, the generated set-payload or an expression;
    an echo of the shown files (plus a logger) is written back with each value byte for byte."""
    c = SHAPES_CANARIES
    dw_list = "cnry31dwList09Zq"
    json_list = "cnry31jsonList10Zq"
    extra = shapes_mule(
        [
            f"<set-variable variableName=\"s\" value=\"#[['{dw_list}']]\"/>",
            f'<set-payload value="[&quot;{json_list}&quot;]" mimeType="application/json"/>',
        ]
    )
    app = shapes_app(tmp_path, extra)
    assert f'[&quot;{c["root"]}&quot;]' in app.text  # the generated set-payload holds the root array
    provider = ScriptedProvider([echo_with_logger])
    runner = ShapesRunner()

    loop = run(app, provider, runner)

    for request in provider.requests:
        for field_name, text in request_texts(request).items():
            _absent(f"AiRequest.{field_name}", text, (c["root"], dw_list, json_list))
    prompt = provider.requests[0].prompt
    assert '["«v' in prompt and "#[['«v" in prompt and "[&quot;«v" in prompt
    assert len(runner.files) == 2, loop.attempts[0].reason
    written = runner.files[1]
    assert written["shapes.xml"] == extra
    assert written["proxy.xml"] == insert_after(app.text, "<http:listener ", LOGGER)


def test_CP8_X32_a_query_item_with_no_equals_sign_is_hidden_whole(tmp_path: Path) -> None:
    """[CP8-X32] A bare query token (no '=') in a TargetEndpoint URL, an AssignMessage header URL and a diff's
    request target is a placeholder, never shown as a parameter name; a named parameter keeps its name."""
    from a2m.verify.placeholders import Placeholders

    table = Placeholders()
    xml = (
        '<TargetEndpoint name="default"><HTTPTargetConnection><URL>https://backend.example.com/orders?'
        "api-version=2023-05-01&amp;SECRETTOKENABCDEF123456</URL></HTTPTargetConnection></TargetEndpoint>"
    )
    shown = table.apigee(xml)
    assert shown is not None and "SECRETTOKENABCDEF123456" not in shown, shown
    assert re.search(r"\?api-version=«v\d+»&amp;«v\d+»</URL>", shown), shown
    line = "backend call: expected path /orders?api-version=2023-05-01&SECRETTOKENABCDEF123456, got path /orders"
    diff = table.diff(line)
    assert "SECRETTOKENABCDEF123456" not in diff and "api-version=«v" in diff, diff
    for url in ("/p?SECRETTOKENABCDEF123456", "/p?a=1&SECRETTOKENABCDEF123456&b=2", "https://h.example/p?x#SECRETTOKENABCDEF123456"):
        assert "SECRETTOKENABCDEF123456" not in table.diff(f"backend call: expected path {url}, got path /p"), url

    c = SHAPES_CANARIES
    app = shapes_app(tmp_path, shapes_mule(["<logger level=\"INFO\" message=\"x\"/>"]))
    provider = ScriptedProvider([lambda request: json.dumps({"status": "cannot_fix", "reason": "cp8-x32"})])

    run(app, provider, RecordingRunner())

    for request in provider.requests:
        for field_name, text in request_texts(request).items():
            _absent(f"AiRequest.{field_name}", text, (c["bare"], c["host"], c["query"], c["fragment"], c["target"]))
    assert re.search(r"<Header name=\"X-Url\">«v\d+»\?«v\d+»&amp;k=«v\d+»#«v\d+»</Header>", provider.requests[0].prompt)


QUOTED_VERSION = "O'Reilly \\ \"2\""
DW_VERSION = re.compile(r"'X-Api-Version': '((?:[^'\\]|\\.)*)'")


class QuotedVersionRunner(RecordingRunner):
    """The JS proxy's app: forwards each call to the backend and answers with the X-Api-Version header its proxy.xml
    sets now, read as DataWeave reads the string literal (re-read on every call)."""

    def start(self, app: Any, *, backend_url: str) -> Any:
        from xml.sax.saxutils import unescape

        self.started.append(app.name)
        flow = Path(app.app_dir) / FLOW_REL
        self.snapshots.append(flow.read_text(encoding="utf-8"))

        class _Handle:
            running = True
            base_url = "http://fake-quoted.invalid/app"

            def send(self, request: Any) -> Any:
                text = unescape(flow.read_text(encoding="utf-8"), {"&quot;": '"', "&apos;": "'"})
                found = DW_VERSION.search(text)
                return forward(backend_url, request, {"X-Api-Version": _dw_value(found.group(1)) if found else "none"})

            def stop(self) -> None:
                pass

        return _Handle()


def test_CP8_X33_a_diff_value_with_quotes_and_a_backslash_written_into_a_dataweave_string_keeps_its_value(
    tmp_path: Path,
) -> None:
    """[CP8-X33] The JS proxy's golden recording expects X-Api-Version O'Reilly \\ "2" and the AI step sets '1'. The
    fake AI writes the diff's expected placeholder in place of the actual one inside the single-quoted DataWeave
    string. a2m spells the value for that string ('O\\'Reilly \\\\ "2"', XML-escaped): the expression keeps the
    expected value, the re-test passes and the fix is kept."""
    app = js_app(tmp_path)
    golden = write_js_golden(tmp_path / "golden")
    exchange_file = golden / JS_APP / "version-header.json"
    exchange = json.loads(exchange_file.read_text(encoding="utf-8"))
    exchange["calls"][0]["response"]["headers"]["X-Api-Version"] = QUOTED_VERSION
    exchange_file.write_text(json.dumps(exchange), encoding="utf-8")

    def answer(request: Any) -> str:
        found = re.search(r"(?i)x-api-version: expected '(«v\d+»)', actual '(«v\d+»)'", request.prompt)
        assert found is not None, request.prompt
        expected, actual = found.groups()
        shown = shown_flow(request)
        return fixed(shown.replace(f"'X-Api-Version': '{actual}'", f"'X-Api-Version': '{expected}'"), confidence="high")

    loop = run(app, ScriptedProvider([answer]), QuotedVersionRunner(), golden=golden)

    assert loop.attempts[0].helped is True, loop.attempts[0].reason
    assert loop.result.type.value == "golden", loop.result.message
    assert flow_text(app) == app.text.replace(
        "'X-Api-Version': '1'", "'X-Api-Version': 'O\\'Reilly \\\\ &quot;2&quot;'"
    )


RESTORE_VALUES = (
    "O'Reilly",
    'say "hi"',
    "back\\slash \\n not a newline",
    "two\nlines",
    "tab\there",
    "unicode é✓ 漢字",
    "dollar $(vars.x) and $5",
    "«v1» is text of the bundle",
    "all '\" \\ \n «v2» $",
)
RESTORE_FILE = shapes_mule(
    [
        "<set-variable variableName=\"a\" value=\"#[{k: 'old-a'}]\"/>",
        "<set-variable variableName=\"b\" value='#[{k: \"old-b\"}]'/>",
        '<set-payload><![CDATA[{"k": "old-c"}]]></set-payload>',
        '<set-variable variableName="d" value="old-d"/>',
        "<set-variable variableName=\"e\" value='old-e'/>",
        "<description>old-f</description>",
        "<set-payload><![CDATA[%dw 2.0\noutput application/json\n---\n{k: 'old-g'}\n]]></set-payload>",
        "<set-variable variableName=\"h\" value=\"#[&quot;pre $(vars.a) post&quot; ++ 'old-h']\"/>",
        '<set-variable variableName="i" value="«v1» stays «v2»"/>',
    ]
)


def test_CP8_X34_a_placeholder_is_written_back_spelled_for_where_it_stands(tmp_path: Path) -> None:
    """[CP8-X34] For values with an apostrophe, double quotes, backslashes, a newline, a tab, unicode, a DataWeave
    $( and the placeholder mark itself: a diff placeholder written into a single- or double-quoted DataWeave string
    (in #[...] and in a %dw script), a JSON string in CDATA, an attribute quoted either way and element text gives
    back exactly that value. An echo is byte for byte the file, also when the bundle's own text holds '«v1»'. A
    placeholder outside any string of an expression, and a DataWeave string with $(...) moved into another string,
    are refused with a reason."""
    from a2m.verify.placeholders import PlaceholderError, Placeholders

    name = SHAPES_REL
    for value in RESTORE_VALUES:
        table = Placeholders()
        shown = table.mule({name: RESTORE_FILE})[name]
        assert shown is not None
        assert table.restore(name, shown) == RESTORE_FILE, value
        diff = table.diff(f"header X-Value: expected {value!r}, actual 'zz'")
        found = re.search(r"expected (['\"])(«v\d+»)\1", diff)
        assert found is not None, diff
        token = found.group(2)
        answer = shown
        for old in ("old-a", "old-b", "old-c", "old-d", "old-e", "old-f", "old-g"):
            old_token = next(t for t, v in table.values.items() if v == old)
            assert answer.count(old_token) == 1, (old, answer)
            answer = answer.replace(old_token, token)
        restored = table.restore(name, answer)
        flow = ET.fromstring(restored.encode("utf-8"))[0]
        by_name = {child.get("variableName"): child for child in flow if child.get("variableName")}
        single = re.fullmatch(r"#\[\{k: '((?:[^'\\]|\\.)*)'\}\]", by_name["a"].get("value", ""))
        assert single is not None and _dw_value(single.group(1)) == value, (value, by_name["a"].get("value"))
        double = re.fullmatch(r'#\[\{k: "((?:[^"\\]|\\.)*)"\}\]', by_name["b"].get("value", ""))
        assert double is not None and _dw_value(double.group(1)) == value, (value, by_name["b"].get("value"))
        payloads = [child for child in flow if child.tag.endswith("set-payload")]
        assert json.loads(payloads[0].text or "")["k"] == value
        script = re.search(r"\{k: '((?:[^'\\]|\\.)*)'\}", payloads[1].text or "")
        assert script is not None and _dw_value(script.group(1)) == value, (value, payloads[1].text)
        assert by_name["d"].get("value") == value and by_name["e"].get("value") == value
        assert next(child for child in flow if child.tag.endswith("description")).text == value
        assert by_name["i"].get("value") == "«v1» stays «v2»"

    table = Placeholders()
    shown = table.mule({name: RESTORE_FILE})[name]
    assert shown is not None
    old_a = next(t for t, v in table.values.items() if v == "old-a")
    with pytest.raises(PlaceholderError, match="outside any string literal"):
        table.restore(name, shown.replace(f"'{old_a}'", old_a))
    interpolated = re.search(r"#\[&quot;(«v\d+»)&quot; \+\+", shown)
    assert interpolated is not None, shown
    with pytest.raises(PlaceholderError, match="cannot be written into as it was"):
        table.restore(name, shown.replace(f"'{old_a}'", f"'{interpolated.group(1)}'"))
    marked = next(t for t, v in table.values.items() if v == "«v1» stays «v2»")
    restored = table.restore(name, shown.replace(f'value="{marked}"', f'value="{marked} new"'))
    assert restored == RESTORE_FILE.replace('value="«v1» stays «v2»"', 'value="«v1» stays «v2» new"')


STRING_SHAPES: dict[str, Callable[[str], str]] = {
    "dw-root-list": lambda v: f"<set-variable variableName=\"s\" value={_attr('#[[' + _dw(v) + ']]')}/>",
    "dw-nested": lambda v: f"<set-variable variableName=\"s\" value={_attr('#[[[' + _dw(v) + '], {k: ' + _dw(v, chr(34)) + '}]]')}/>",
    "dw-selector": lambda v: f"<set-variable variableName=\"s\" value={_attr('#[vars.table[' + _dw(v) + ']]')}/>",
    "dw-header-selector": lambda v: f"<set-variable variableName=\"s\" value={_attr('#[attributes.headers[' + _dw(v, chr(34)) + ']]')}/>",
    "dw-call": lambda v: f"<set-variable variableName=\"s\" value={_attr('#[p(' + _dw(v) + ')]')}/>",
    "dw-script-cdata": lambda v: (
        "<set-payload><![CDATA[%dw 2.0\noutput application/json\n---\n[" + _dw(v) + ', {"k": ' + _dw(v, chr(34))
        + "}]\n]]></set-payload>"
    ),
    "json-cdata": lambda v: "<set-payload><![CDATA[" + json.dumps({"k": [v], "n": {"m": v}}, ensure_ascii=False) + "]]></set-payload>",
    "json-root-array-attribute": lambda v: f"<set-payload value={_attr(json.dumps([v], ensure_ascii=False))}/>",
    "escaped-xml-attribute": lambda v: f"<set-payload value={_attr('<a b=' + quoteattr(v) + '>' + xml_escape(v) + '</a>')}/>",
    "attribute-text": lambda v: f"<logger message={_attr(v)}/>",
    "element-text": lambda v: f"<description>{xml_escape(v)}</description>",
    "dw-interpolated": lambda v: '<set-variable variableName="s" value=' + _attr('#["pre $(vars.a) ' + _dw(v, '"')[1:] + "]") + "/>",
}
URL_SHAPES: dict[str, Callable[[str], str]] = {
    "url-bare-token": lambda c: f"<set-variable variableName=\"u\" value={_attr('https://h.example.com/p?' + c + '&a=1')}/>",
    "url-bare-last": lambda c: f"<set-variable variableName=\"u\" value={_attr('https://h.example.com/p?a=1&' + c)}/>",
    "url-fragment": lambda c: f"<set-variable variableName=\"u\" value={_attr('https://h.example.com/p?x=1#' + c)}/>",
    "url-path": lambda c: f"<set-variable variableName=\"u\" value={_attr('/p/' + c + '?a=1')}/>",
    "url-userinfo": lambda c: f"<set-variable variableName=\"u\" value={_attr('https://u:' + c + '@h.example.com/p?a=1')}/>",
    "url-query-value": lambda c: f"<set-variable variableName=\"u\" value={_attr('https://h.example.com/p?k=' + c)}/>",
    "dw-url-literal": lambda c: f"<set-variable variableName=\"u\" value=\"#['https://h.example.com/p?{c}']\"/>",
    "form-bare": lambda c: f"<set-payload value={_attr('a=1&' + c)}/>",
    "form-value": lambda c: f"<set-payload value={_attr('a=' + c + '&b=2')}/>",
    "dw-regex": lambda c: f"<set-variable variableName=\"u\" value=\"#[payload matches /{c}/]\"/>",
}
STYLES: tuple[Callable[[str], str], ...] = (
    lambda c: c,
    lambda c: f"O'{c}",
    lambda c: f'{c} \\ "q"',
    lambda c: f"{c}\nline 2",
    lambda c: f"é✓ {c}",
    lambda c: f"«v1» {c}",
)


def sweep_shapes() -> tuple[list[str], list[str]]:
    """Every Mule literal shape of the sweep (each string shape in every style, each URL shape once) and the distinct
    canary each one holds."""
    fragments: list[str] = []
    canaries: list[str] = []
    for shape_index, shape in enumerate(STRING_SHAPES.values()):
        for style_index, style in enumerate(STYLES):
            canary = f"cnry35s{shape_index:02d}x{style_index}Zq"
            fragments.append(shape(style(canary)))
            canaries.append(canary)
    for shape_index, shape in enumerate(URL_SHAPES.values()):
        canary = f"cnry35u{shape_index:02d}Zq"
        fragments.append(shape(canary))
        canaries.append(canary)
    return fragments, canaries


def test_CP8_X35_no_literal_shape_reaches_a_fix_request_and_an_echo_restores_every_byte(tmp_path: Path) -> None:
    """[CP8-X35] Property sweep: 12 string literal shapes (root and nested lists, selectors, call arguments, %dw in
    CDATA, JSON in CDATA and in an attribute, escaped XML in an attribute, attribute and element text, DataWeave
    interpolation) in 6 styles (plain, apostrophe, quotes and backslash, newline, unicode, '«v1»'
    text), and 10 URL and form shapes (bare query tokens, a fragment, a path, user information, a query value, a URL
    in a DataWeave string, form text, a regular expression), each with its own canary, in a Mule file of the shapes
    proxy. No canary reaches any field of the fix request; an echo of every shown file (plus a logger) writes each
    file back byte for byte. Apigee policies, custom code and diffs with the same shapes hold no canary either. (A
    Mule file there when the loop starts is taken as a2m generated it, so its name attributes are known names: CP8-X36
    covers name attributes.)"""
    from a2m.ai.provider import ItemKind
    from a2m.verify.placeholders import Placeholders

    fragments, canaries = sweep_shapes()
    extra = shapes_mule(fragments)
    ET.fromstring(extra.encode("utf-8"))  # the shapes really are well-formed XML
    for canary in canaries:
        assert canary in extra, canary
    app = shapes_app(tmp_path, extra)
    provider = ScriptedProvider([echo_with_logger])
    runner = ShapesRunner()

    loop = run(app, provider, runner)

    for request in provider.requests:
        for field_name, text in request_texts(request).items():
            _absent(f"AiRequest.{field_name}", text, canaries)
    assert len(runner.files) == 2, loop.attempts[0].reason
    assert runner.files[1]["shapes.xml"] == extra
    assert runner.files[1]["proxy.xml"] == insert_after(app.text, "<http:listener ", LOGGER)

    table = Placeholders()
    files = {SHAPES_REL: extra, FLOW_REL: app.text}
    shown_files = table.mule(files)
    for rel, text in files.items():
        shown = shown_files[rel]
        assert shown is not None, rel
        _absent(rel, shown, canaries)
        assert table.restore(rel, shown) == text, rel

    for style_index, style in enumerate(STYLES):
        canary = f"cnry35a{style_index}Zq"
        value = style(canary)
        policies = (
            (f'<AssignMessage name="AM"><Set><Payload contentType="application/json">{xml_escape(json.dumps([value]))}'
             "</Payload></Set></AssignMessage>"),
            ('<AssignMessage name="AM"><Set><Payload contentType="application/json">'
             f"{xml_escape(json.dumps({'a': [[value], {'b': value}]}))}</Payload></Set></AssignMessage>"),
            ('<AssignMessage name="AM"><Set><Payload contentType="application/json"><![CDATA['
             f"{json.dumps({'k': [value]})}]]></Payload></Set></AssignMessage>"),
            ('<AssignMessage name="AM"><Set><Payload contentType="text/xml">'
             f"{xml_escape('<a b=' + quoteattr(value) + '>' + xml_escape(value) + '</a>')}</Payload></Set></AssignMessage>"),
            (f'<AssignMessage name="AM"><Set><Headers><Header name="X-U">https://h.example/p?{canary}&amp;a=1#{canary}'
             "</Header></Headers></Set></AssignMessage>"),
            ('<ProxyEndpoint name="default"><PreFlow name="PreFlow"><Request><Step><Name>AM</Name>'
             f"<Condition>request.header.x = {xml_escape(json.dumps(value))}</Condition></Step></Request></PreFlow>"
             "</ProxyEndpoint>"),
        )
        for policy in policies:
            shown_policy = Placeholders().apigee(policy)
            assert shown_policy is not None, policy
            _absent(policy, shown_policy, (canary,))
        for code, kind in (
            (f"var a = [{_dw(value)}]; x[{_dw(value)}]; context.getVariable({_dw(value)}); f({_dw(value, chr(34))});",
             ItemKind.JAVASCRIPT),
            (f"a = [{_dw(value)}]\nx[{_dw(value)}]\nflow.getVariable({_dw(value)})\n", ItemKind.PYTHON),
            (f"String a = {_dw(value, chr(34))}; m.get({_dw(value, chr(34))});", ItemKind.JAVA),
        ):
            _absent(kind.value, Placeholders().code(code, kind), (canary,))
        for line in (
            f"backend call: expected path /p?{canary}&a=1, got path /p",
            f"header X-Value: expected {value!r}, actual 'x'",
            f"body field k: expected [{value!r}], actual []",
        ):
            _absent(line, Placeholders().diff(line), (canary,))


def test_CP8_X36_a_mule_name_attribute_is_shown_only_when_it_is_a_known_name() -> None:
    """[CP8-X36] In a Mule file, a name attribute (variableName, name, doc:name, config-ref) is shown only when its
    value is a known name (from the IR, the Apigee XML or the Mule files as a2m generated them), never because it is
    written like one; a string literal is a placeholder even when it holds a known name (CP8 round 8)."""
    from a2m.verify.placeholders import Placeholders

    table = Placeholders(names=("proxy-default", "JS-SetVersion"))
    table.learn_apigee('<ProxyEndpoint name="default"><Flows><Flow name="Keyed"><Condition>request.header.X-Api-Key '
                       '= "v"</Condition></Flow></Flows></ProxyEndpoint>')
    table.learn_mule(['<mule><set-variable variableName="a2mRequestHeaders" value="1"/></mule>'])
    text = (
        '<mule xmlns:doc="http://www.mulesoft.org/schema/mule/documentation"><flow name="proxy-default">'
        '<set-variable variableName="a2mRequestHeaders" doc:name="JS-SetVersion" '
        "value=\"#[attributes.headers['x-api-key'] ++ vars.t['cnry36sel01Zq']]\"/>"
        '<set-variable variableName="cnry36slot02Zq" doc:name="cnry36label03Zq" value="1"/></flow></mule>'
    )
    shown = table.mule({"f.xml": text})["f.xml"]
    assert shown is not None
    for visible in ('name="proxy-default"', 'variableName="a2mRequestHeaders"', 'doc:name="JS-SetVersion"',
                    "attributes.headers['«v"):
        assert visible in shown, (visible, shown)
    _absent("f.xml", shown, ("cnry36sel01Zq", "cnry36slot02Zq", "cnry36label03Zq", "x-api-key"))
    assert table.restore("f.xml", shown) == text


def test_CP8_X37_the_code_languages_are_itemkind_values_not_a_second_vocabulary() -> None:
    """[CP8-X37] placeholders.py takes the custom code languages from a2m.ai.provider.ItemKind and Placeholders.code
    takes an ItemKind: for each custom code kind, a string literal is read with that language's escapes (so it gets
    the placeholder of the same value in a diff) and a comment and the literal never show."""
    from a2m.ai.provider import ItemKind
    from a2m.verify import fix_loop, placeholders

    assert placeholders.JAVASCRIPT == ItemKind.JAVASCRIPT.value
    assert placeholders.PYTHON == ItemKind.PYTHON.value and placeholders.JAVA == ItemKind.JAVA.value
    assert not hasattr(fix_loop, "_LANGUAGES")
    sources = {
        ItemKind.JAVASCRIPT: "// cnry37c\nvar x = 'cnry37 O\\'Reilly \\x41';\n",
        ItemKind.PYTHON: "# cnry37c\nx = 'cnry37 O\\'Reilly \\x41'\n",
        ItemKind.JAVA: "// cnry37c\nString x = \"cnry37 O'Reilly \\u0041\";\n",
    }
    for kind, source in sources.items():
        table = placeholders.Placeholders()
        shown = table.code(source, kind)
        assert "cnry37" not in shown and "Reilly" not in shown, (kind, shown)
        token = table.diff("header X: expected \"cnry37 O'Reilly A\", actual 'x'").split('"')[1]
        assert table.values[token] == "cnry37 O'Reilly A", (kind, table.values)
        assert token in shown, (kind, shown, token)


# ---------------------------------------------------------------- CP8 adversarial round 6: names are positional
#
# Round 6 findings (codex-standards X1, codex-correctness X1, host-alt-correctness A1): literal nested XML in an
# AssignMessage <Payload> was walked as the policy's own schema, so a <Name> or a name=/type= attribute in the payload
# was shown and learned as a known name (then a code literal with the same value was shown too). An Apigee name is now
# positional: a value is a name only at a schema position of names, and data (a payload, a value, a template, entries,
# inline code, and every document embedded in a value) has no names: every value in it is a placeholder.

NESTED_WRAPPERS = ("Customer", "AssignVariable", "Headers", "Step")


def _name_shaped_xml(prefix: str, depth: int) -> str:
    """Literal XML ``depth`` levels deep (wrappers named like Apigee schema elements) holding element text and
    attributes named like Apigee names, each value a canary ``prefix`` + depth + slot + ``Zq``."""
    c = f"{prefix}d{depth}"
    inner = (
        f"<Name>{c}nameZq</Name><Ref>{c}refZq</Ref><Source>{c}srcZq</Source><AssignTo>{c}toZq</AssignTo>"
        f"<Step><Name>{c}stepZq</Name></Step><AssignVariable><Name>{c}avZq</Name></AssignVariable>"
        f'<Item name="{c}aNameZq" type="{c}aTypeZq" ref="{c}aRefZq" target="{c}aTargetZq" file="{c}aFileZq" '
        f'action="{c}aActionZq"/><Header name="{c}hdrZq">{c}hvalZq</Header><Qty>{depth}4321987</Qty>'
    )
    for wrapper in reversed(NESTED_WRAPPERS[:depth]):
        inner = f"<{wrapper}>{inner}</{wrapper}>"
    return inner


def _data_policies(prefix: str) -> list[str]:
    """Policies holding name-shaped literal XML (at depths 1 to 4) in each kind of data position."""
    nested = "".join(_name_shaped_xml(prefix, depth) for depth in range(1, 5))
    return [
        (
            f'<AssignMessage name="AM-Body"><Set><Payload contentType="application/xml">{nested}</Payload></Set>'
            '<AssignTo createNew="false" transport="http" type="request"/></AssignMessage>'
        ),
        (
            f'<RaiseFault name="RF-Body"><FaultResponse><Set><Payload contentType="text/xml">{nested}</Payload></Set>'
            "</FaultResponse></RaiseFault>"
        ),
        (
            f'<AssignMessage name="AM-Var"><AssignVariable><Name>v.one</Name><Value>{nested}</Value></AssignVariable>'
            f"<AssignVariable><Name>v.two</Name><Template>{nested}</Template></AssignVariable></AssignMessage>"
        ),
        (
            f'<KeyValueMapOperations name="KVM" mapIdentifier="m"><InitialEntries><Entry><Key><Parameter>{prefix}kZq'
            f"</Parameter></Key><Value>{nested}</Value></Entry></InitialEntries></KeyValueMapOperations>"
        ),
        (
            f'<AssignMessage name="AM-Esc"><Set><Payload contentType="application/xml">{xml_escape(nested)}</Payload>'
            f"<Payload><![CDATA[{nested}]]></Payload></Set></AssignMessage>"
        ),
        f'<Javascript name="JS-Inline"><Source>{prefix}jsSrcZq</Source></Javascript>',
        (
            f'<AssignMessage name="AM-Odd"><Set><Headers><Header name="X-A"><Name>{prefix}oddZq</Name></Header>'
            "</Headers></Set></AssignMessage>"
        ),
    ]


def _canaries_of(prefix: str, text: str) -> set[str]:
    return set(re.findall(re.escape(prefix) + r"\w*?Zq", text))


def test_CP8_X38_literal_nested_xml_in_a_payload_never_reaches_a_fix_request_and_an_echo_restores(
    tmp_path: Path,
) -> None:
    """[CP8-X38] An AssignMessage whose <Payload> is literal nested XML (a <Name>, name=, type=, ref= ... at depths 1
    to 4) never sends one of its values in any AiRequest field; Mule string literals with the
    same value as a payload <Name>, <Step><Name>, name= or <Header name> stay placeholders (no known-name poisoning);
    an echo restores the files exactly."""
    prefix = "cnry38"
    nested = "".join(_name_shaped_xml(prefix, depth) for depth in range(1, 5))
    bundle = write_shapes_bundle(tmp_path / "in")
    (bundle / "apiproxy" / "policies" / "AM-Shapes.xml").write_text(
        XML_HEAD + '<AssignMessage name="AM-Shapes"><Set><Headers><Header name="X-Nested">yes</Header></Headers>'
        f'<Payload contentType="application/xml">{nested}</Payload>'
        '</Set><AssignTo createNew="false" transport="http" type="request"/></AssignMessage>\n',
        encoding="utf-8",
    )
    app = generate(bundle, tmp_path / SHAPES / "mule-app")
    poisoned = f"{prefix}d1nameZq"
    extra = shapes_mule(
        [
            f"<set-variable variableName=\"s\" value=\"#['{poisoned}' ++ vars.t['{prefix}d2stepZq']]\"/>",
            f'<set-variable variableName="u" value="#[&quot;{prefix}d4hdrZq&quot; ++ &quot;{prefix}d3aNameZq&quot;]"/>',
        ]
    )
    (app.app_dir / SHAPES_REL).write_text(extra, encoding="utf-8")
    canaries = tuple(sorted(_canaries_of(prefix, nested)))
    assert len(canaries) == 4 * 14, canaries
    provider = ScriptedProvider([echo_with_logger])
    runner = ShapesRunner()

    loop = run(app, provider, runner)

    prompt = provider.requests[0].prompt
    assert '<AssignMessage name="AM-Shapes">' in prompt and "<Customer><Name>«v" in prompt, prompt[:4000]
    for request in provider.requests:
        for field_name, text in request_texts(request).items():
            _absent(f"AiRequest.{field_name}", text, canaries)
    assert len(runner.files) == 2, loop.attempts[0].reason
    written = runner.files[1]
    assert written["shapes.xml"] == extra
    assert written["proxy.xml"] == insert_after(app.text, "<http:listener ", LOGGER)


def test_CP8_X40_apigee_names_are_positional_and_data_has_no_names_at_any_depth() -> None:
    """[CP8-X40] In every data position (a literal or escaped or CDATA payload, a value, a template, KVM entries,
    inline JavaScript) every value of name-shaped literal XML, at depths 1 to 4, is a placeholder, numbers included;
    an element called Name outside its schema position is not a name; the schema positions of names stay visible."""
    from a2m.verify.placeholders import Placeholders

    prefix = "cnry40"
    for policy in _data_policies(prefix):
        shown = Placeholders().apigee(policy)
        assert shown is not None, policy
        assert not _canaries_of(prefix, shown), (policy[:60], _canaries_of(prefix, shown))
        for depth in range(1, 5):
            assert f"{depth}4321987" not in shown, (policy[:60], depth, shown)
    json_body = Placeholders().apigee(
        '<AssignMessage name="AM-J"><Set><Payload contentType="application/json">{"qty": 4321987, "vip": true}'
        "</Payload></Set></AssignMessage>"
    )
    assert json_body is not None and "4321987" not in json_body and '"qty"' in json_body, json_body
    schema = (
        '<AssignMessage name="AM-Schema"><AssignVariable><Name>flow.my.var</Name><Ref>request.header.x-in</Ref>'
        '</AssignVariable><Set><Headers><Header name="X-Out">v</Header></Headers><QueryParams><QueryParam '
        'name="page">1</QueryParam></QueryParams><FormParams><FormParam name="field">v</FormParam></FormParams>'
        '</Set><AssignTo createNew="false" transport="http" type="request">myRequest</AssignTo></AssignMessage>'
    )
    shown = Placeholders().apigee(schema)
    assert shown is not None
    for visible in ('name="AM-Schema"', "<Name>flow.my.var</Name>", "<Ref>request.header.x-in</Ref>",
                    '<Header name="X-Out">', '<QueryParam name="page">', '<FormParam name="field">',
                    'type="request">myRequest</AssignTo>'):
        assert visible in shown, (visible, shown)
    endpoint = Placeholders().apigee(
        '<ProxyEndpoint name="default"><Flows><Flow name="GetOne"><Request><Step><Name>AM-Schema</Name></Step>'
        '</Request></Flow></Flows><RouteRule name="r1"><TargetEndpoint>backend</TargetEndpoint></RouteRule>'
        "</ProxyEndpoint>"
    )
    assert endpoint is not None
    for visible in ('<Flow name="GetOne">', "<Name>AM-Schema</Name>", "<TargetEndpoint>backend</TargetEndpoint>"):
        assert visible in endpoint, (visible, endpoint)
    extract = Placeholders().apigee(
        '<ExtractVariables name="EV"><Source>request</Source><VariablePrefix>ext</VariablePrefix><JSONPayload>'
        '<Variable name="orderId"><JSONPath>$.a</JSONPath></Variable></JSONPayload></ExtractVariables>'
    )
    assert extract is not None
    for visible in ("<Source>request</Source>", "<VariablePrefix>ext</VariablePrefix>", '<Variable name="orderId">'):
        assert visible in extract, (visible, extract)


def test_CP8_X41_a_value_in_data_is_never_learned_as_a_known_name() -> None:
    """[CP8-X41] learn_apigee learns names only from schema positions: after learning every data policy, the same
    values written as string literals in JavaScript, Python, Java and a Mule DataWeave expression, or in a Mule name
    attribute, stay placeholders, and so does a header name the policy declares (CP8 round 8: no value is shown
    because it equals a known name)."""
    from a2m.ai.provider import ItemKind
    from a2m.verify.placeholders import Placeholders

    prefix = "cnry41"
    table = Placeholders()
    for policy in _data_policies(prefix):
        table.learn_apigee(policy)
    table.learn_apigee('<AssignMessage name="AM-K"><Set><Headers><Header name="X-Known-Name">v</Header></Headers>'
                       "</Set></AssignMessage>")
    values = sorted(set().union(*(_canaries_of(prefix, policy) for policy in _data_policies(prefix))))
    for value in values:
        for code, kind in (
            (f"var t = '{value}'; var k = 'X-Known-Name';", ItemKind.JAVASCRIPT),
            (f"t = '{value}'\nk = 'X-Known-Name'\n", ItemKind.PYTHON),
            (f'String t = "{value}"; String k = "X-Known-Name";', ItemKind.JAVA),
        ):
            shown = table.code(code, kind)
            assert value not in shown, (kind, value, shown)
            assert "X-Known-Name" not in shown, (kind, shown)
        mule = (f'<mule><flow name="{value}"><set-variable variableName="{value}" '
                f"value=\"#['{value}' ++ attributes.headers['X-Known-Name']]\"/></flow></mule>")
        shown_mule = table.mule({"f.xml": mule})["f.xml"]
        assert shown_mule is not None and value not in shown_mule, (value, shown_mule)
        assert "X-Known-Name" not in shown_mule and "attributes.headers['«v" in shown_mule, shown_mule


# ---------------------------------------------------------------- CP8 adversarial round 8: no value is shown by value
#
# Round 8 findings (codex-standards X1, host-alt-correctness A1, host-alt-standards C1): a string literal was shown
# when its value equalled a known name, so {"password":"admin"} in a payload was sent as it is when the proxy declared
# a variable called admin. A value is now shown only where it stands as a name itself; a string literal, element text
# or any other value is never shown because of what it equals. codex-correctness X1: a JavaScript regular expression
# after the ")" of if (...) was read as division and shown. Every code lexer now fails safe: a "/" that may start a
# regular expression is read as one unless it is provably a division, and an interpolation, escape or comment form
# the lexer cannot follow for sure is a placeholder.

R8_NAME = "cnry42adminZq"
R8_JS = __import__("a2m.ai.provider", fromlist=["ItemKind"]).ItemKind.JAVASCRIPT


def _r8_quoted(text: str, value: str) -> list[str]:
    """Each place ``value`` stands in ``text`` written as a quoted value (a string literal, a JSON value, a quoted
    selector, an XML-escaped quote), with some context."""
    found = []
    for match in re.finditer(re.escape(value), text):
        before = text[max(0, match.start() - 16) : match.start()]
        after = text[match.end() : match.end() + 6]
        if before.endswith(('variableName="', ' name="')):
            continue  # a Mule name attribute: a name position
        if before.endswith(("'", '"', "&quot;", "&apos;", "\\", "`")) or after.startswith(("'", '"', "&quot;", "&apos;")):
            found.append(before + value + after)
    return found


def _r8_policies(name: str) -> list[str]:
    """Policies holding ``name`` as a data value: a JSON payload, value, template and KVM entries, an XML payload
    text and attribute, escaped and CDATA payloads, a form payload and a URL query value."""
    return [
        (
            '<AssignMessage name="AM-Json"><Set><Payload contentType="application/json">'
            f'{{"password": "{name}", "list": ["{name}"], "nested": {{"k": "{name}"}}}}</Payload></Set></AssignMessage>'
        ),
        (
            '<AssignMessage name="AM-Var"><AssignVariable><Name>v.one</Name>'
            f'<Value>{{"secret": "{name}"}}</Value></AssignVariable><AssignVariable><Name>v.two</Name>'
            f'<Template>{{"t": "{name}", "r": "{{request.header.x}}"}}</Template></AssignVariable></AssignMessage>'
        ),
        (
            '<KeyValueMapOperations name="KVM" mapIdentifier="m"><InitialEntries><Entry><Key><Parameter>k</Parameter>'
            f'</Key><Value>["{name}"]</Value></Entry></InitialEntries></KeyValueMapOperations>'
        ),
        (
            '<AssignMessage name="AM-Xml"><Set><Payload contentType="application/xml">'
            f'<Login user="{name}"><Password>{name}</Password><Name>{name}</Name></Login></Payload></Set>'
            "</AssignMessage>"
        ),
        (
            '<AssignMessage name="AM-Esc"><Set><Payload contentType="application/json">'
            f'{xml_escape(chr(123) + chr(34) + "p" + chr(34) + ": " + chr(34) + name + chr(34) + chr(125))}</Payload>'
            f'<Payload><![CDATA[{{"p": "{name}"}}]]></Payload></Set></AssignMessage>'
        ),
        (
            '<AssignMessage name="AM-Form"><Set><Payload contentType="application/x-www-form-urlencoded">'
            f"user=u&amp;password={name}</Payload></Set></AssignMessage>"
        ),
    ]


def test_CP8_X42_a_known_name_equal_to_a_data_value_stays_a_placeholder_everywhere() -> None:
    """[CP8-X42] A name the table learned from a schema position (AssignVariable/Name), from the IR (know) and from a
    generated Mule name attribute is never shown because a value equals it: as a JSON payload, value, template or
    KVM entries value, XML payload text or attribute, escaped, CDATA or form payload value, as a JavaScript, Python
    or Java string literal, and as a DataWeave string literal or quoted selector (vars['x'],
    attributes.headers['x']) in a Mule expression, it is a placeholder; the same name stays visible at its own name
    positions (AssignVariable/Name, a Mule variableName, an unquoted selector vars.x)."""
    from a2m.ai.provider import ItemKind
    from a2m.verify.placeholders import Placeholders

    sources = {
        "schema": lambda t: t.learn_apigee(
            f'<AssignMessage name="SetVars"><AssignVariable><Name>{R8_NAME}</Name><Value>x</Value></AssignVariable>'
            "</AssignMessage>"
        ),
        "ir": lambda t: t.know([R8_NAME]),
        "mule": lambda t: t.learn_mule([f'<mule><set-variable variableName="{R8_NAME}" value="1"/></mule>']),
    }
    for source, learn in sources.items():
        table = Placeholders()
        learn(table)
        declared = table.apigee(
            f'<AssignMessage name="SetVars"><AssignVariable><Name>{R8_NAME}</Name><Value>x</Value></AssignVariable>'
            "</AssignMessage>"
        )
        assert declared is not None and f"<Name>{R8_NAME}</Name>" in declared, (source, declared)
        for policy in _r8_policies(R8_NAME):
            shown = table.apigee(policy)
            assert shown is not None and R8_NAME not in shown, (source, shown)
        for code, kind in (
            (f"var password = '{R8_NAME}'; var p = \"{R8_NAME}\"; ctx.getVariable('{R8_NAME}');", ItemKind.JAVASCRIPT),
            (f"password = '{R8_NAME}'\np = \"{R8_NAME}\"\nflow.getVariable('{R8_NAME}')\n", ItemKind.PYTHON),
            (f'String password = "{R8_NAME}"; m.get("{R8_NAME}");', ItemKind.JAVA),
        ):
            shown = table.code(code, kind)
            assert R8_NAME not in shown, (source, kind, shown)
        original = (
            f'<mule><flow name="f"><set-variable variableName="{R8_NAME}" '
            f"value=\"#['{R8_NAME}' ++ vars['{R8_NAME}'] ++ attributes.headers['{R8_NAME}'] ++ vars.{R8_NAME}]\"/>"
            f'<set-payload value="#[{{password: &quot;{R8_NAME}&quot;}}]"/>'
            f'<set-payload value=\'{{"password": "{R8_NAME}"}}\'/></flow></mule>'
        )
        shown_mule = table.mule({"f.xml": original})["f.xml"]
        assert shown_mule is not None
        assert _r8_quoted(shown_mule, R8_NAME) == [], (source, shown_mule)
        if source != "ir":  # know() names are policy and flow names, not variables
            assert f'variableName="{R8_NAME}"' in shown_mule, (source, shown_mule)
        assert f"vars.{R8_NAME}" in shown_mule, (source, shown_mule)
        assert table.restore("f.xml", shown_mule) == original


def test_CP8_X43_a_known_name_as_a_payload_value_never_reaches_a_fix_request_and_an_echo_restores(
    tmp_path: Path,
) -> None:
    """[CP8-X43] Through the fix loop: a proxy declares a variable (AssignVariable/Name) and the same text is a
    password value in its JSON payload, in a Mule DataWeave literal, a quoted selector and a JSON set-payload; it
    reaches no AiRequest field as a quoted value (only at its name positions), and an echo plus a logger writes every
    file back byte for byte."""
    bundle = write_shapes_bundle(tmp_path / "in")
    (bundle / "apiproxy" / "policies" / "AM-Shapes.xml").write_text(
        XML_HEAD + f'<AssignMessage name="AM-Shapes"><AssignVariable><Name>{R8_NAME}</Name><Value>yes</Value>'
        '</AssignVariable><Set><Headers><Header name="X-Nested">yes</Header></Headers>'
        f'<Payload contentType="application/json">{{"user": "u", "password": "{R8_NAME}"}}</Payload>'
        '</Set><AssignTo createNew="false" transport="http" type="request"/></AssignMessage>\n',
        encoding="utf-8",
    )
    app = generate(bundle, tmp_path / SHAPES / "mule-app")
    extra = shapes_mule(
        [
            f"<set-variable variableName=\"s\" value=\"#['{R8_NAME}' ++ vars['{R8_NAME}'] ++ vars.{R8_NAME}]\"/>",
            f'<set-variable variableName="u" value="#[&quot;{R8_NAME}&quot; ++ attributes.headers[&quot;{R8_NAME}&quot;]]"/>',
            f'<set-payload value=\'{{"password": "{R8_NAME}"}}\'/>',
        ]
    )
    (app.app_dir / SHAPES_REL).write_text(extra, encoding="utf-8")
    provider = ScriptedProvider([echo_with_logger])
    runner = ShapesRunner()

    loop = run(app, provider, runner)

    prompt = provider.requests[0].prompt
    assert f"<Name>{R8_NAME}</Name>" in prompt, prompt[:4000]  # its own name position stays visible
    assert '"password": "«v' in prompt, prompt[:4000]
    for request in provider.requests:
        for field_name, text in request_texts(request).items():
            assert _r8_quoted(text, R8_NAME) == [], (field_name, _r8_quoted(text, R8_NAME))
    assert len(runner.files) == 2, loop.attempts[0].reason
    written = runner.files[1]
    assert written["shapes.xml"] == extra
    assert written["proxy.xml"] == insert_after(app.text, "<http:listener ", LOGGER)


# JavaScript positions where a "/" starts a regular expression (each "{re}" is one).
R8_JS_POSITIONS = (
    "if (checkPartner) {re}.test(host) && ok();",
    "if ((a) && f(b)) {re}.test(host);",
    "while (more()) {re}.exec(s);",
    "for (;;) {re}.test(s);",
    "for (const k of keys) {re}.test(k);",
    "with (o) {re}.test(s);",
    "return {re}.test(s);",
    "x = {re};",
    "x = a ? {re} : {re};",
    "f({re}, {re});",
    "[{re}, {re}]",
    "o = {{k: {re}}};",
    "if (a) {{ b(); }} {re}.test(s);",
    "a(); {re}.test(s);",
    "{re}.test(s);",
    "a &&\n{re}.test(s);",
    "a || {re}.test(s);",
    "!{re}.test(s);",
    "typeof {re};",
    "void {re};",
    "switch (x) {{ case {re}: break; }}",
    "if (a) b(); else {re}.test(s);",
    "do {re}.test(s); while (a);",
    "const f = () => {re};",
    "x = s in {re};",
    "function* g() {{ yield {re}; }}",
    "async function h() {{ await {re}; }}",
    "x = 1 + {re}.source;",
    "x = a - {re}.lastIndex;",
    "x = a * {re}.lastIndex;",
    "x = a % {re}.lastIndex;",
    "x = a < {re}.lastIndex;",
    "x = a > {re}.lastIndex;",
    "x = a == {re};",
    "x = a++ / 2 / {re}.lastIndex;",
    "x = `t ${{{re}.test(s)}} u`;",
    "x = `t ${{ `in ${{{re}.source}}` }} u`;",
    "throw {re};",
    "x = new RegExp({re});",
)
# Regular expression bodies (each holds the canary ``{c}``).
R8_REGEX_BODIES = ("/{c}/", "/{c}/gi", "/a[/]{c}/", "/{c}\\/x/i", "/ {c} /", "/^{c}(?:x|y)$/m")


def _r8_round_trip(table: Any, shown: str) -> str:
    """``shown`` with every placeholder replaced by the value it stands for."""
    from a2m.verify.placeholders import TOKEN

    return TOKEN.sub(lambda match: table.values[match.group(0)], shown)


def test_CP8_X44_a_javascript_regular_expression_in_every_position_never_shows_and_round_trips() -> None:
    """[CP8-X44] A JavaScript regular expression literal after the ")" of if, while, for and with, after return,
    after every operator, at the start of a line or statement, after a block, inside template literals (nested too),
    and as a call argument or array item never shows its content, and the shown code with each placeholder put back
    is the code byte for byte. A provable division (a / b / c, (a + b) / 2, f(x) / 2, a[0] / 2) stays visible."""
    from a2m.verify.placeholders import Placeholders

    count = 0
    for position in R8_JS_POSITIONS:
        for number, body in enumerate(R8_REGEX_BODIES):
            canary = f"cnry44p{count}b{number}Zq"
            code = position.format(re=body.format(c=canary))
            table = Placeholders()
            shown = table.code(code, R8_JS)
            assert canary not in shown, (code, shown)
            assert _r8_round_trip(table, shown) == code, (code, shown)
            count += 1
    for division in ("x = a / b / c;", "y = (a + b) / 2 / 3;", "z = f(x) / 2 / n;", "w = a[0] / 2 / k;"):
        assert Placeholders().code(division, R8_JS) == division


# DataWeave positions where a "/" starts a regular expression.
R8_DW_POSITIONS = (
    "payload splitBy {re}",
    "payload matches {re}",
    "payload replace {re} with ''",
    "vars.a myInfix {re}",
    "if (vars.a) {re} else {re}",
    "unless (vars.a) {re} otherwise {re}",
    "[{re}, {re}]",
    "{{k: {re}}}",
    "(x) -> {re}",
    "payload scan ({re})",
    "payload match {{ case x if (x matches {re}) -> 1 else -> 2 }}",
)


def test_CP8_X45_a_dataweave_regular_expression_never_shows_and_restores_byte_exact() -> None:
    """[CP8-X45] A DataWeave regular expression in a Mule expression (after an infix function, after if (...) and
    unless (...), in a list, an object, a lambda and a match) never shows its content; an echo restores the file
    byte for byte, and so does an answer that changes the expression (the regular expression is written back from
    its placeholder). A field selector division (vars.total / 2 / vars.n) and an output MIME type stay visible."""
    from a2m.verify.placeholders import Placeholders

    for index, position in enumerate(R8_DW_POSITIONS):
        for number, body in enumerate(R8_REGEX_BODIES[:4]):
            canary = f"cnry45p{index}b{number}Zq"
            expression = position.format(re=body.format(c=canary))
            original = f"<mule><flow name=\"f\"><set-payload value={_attr('#[' + expression + ']')}/></flow></mule>"
            table = Placeholders()
            shown = table.mule({"f.xml": original})["f.xml"]
            assert shown is not None and canary not in shown, (expression, shown)
            assert table.restore("f.xml", shown) == original, (expression, shown)
            changed = shown.replace("]\"/>", " ++ 'x']\"/>")
            expected = original.replace("]\"/>", " ++ 'x']\"/>")
            assert table.restore("f.xml", changed) == expected, (expression, changed)
    for visible in ("#[vars.total / 2 / vars.n]", "#[output application/json --- payload.a / 2]"):
        original = f"<mule><flow name=\"f\"><set-payload value={_attr(visible)}/></flow></mule>"
        shown = Placeholders().mule({"f.xml": original})["f.xml"]
        assert shown is not None and f"value={_attr(visible)}" in shown, shown


def test_CP8_X46_a_bounded_generator_of_ambiguous_slashes_never_leaks_a_canary() -> None:
    """[CP8-X46] Bounded generator: every pairing of 30 tokens that can stand before a "/" (identifiers, keywords,
    numbers, literals, closing brackets, operators) with 5 continuations, in JavaScript and DataWeave, each holding a
    canary between two slashes on one line: whatever the reading, the canary never shows unless the "/" is provably
    a division, and the shown code with each placeholder put back is the code byte for byte (DataWeave also through
    a Mule restore)."""
    from a2m.verify.placeholders import Placeholders

    befores = (
        "", "a", "a.b", "return", "typeof", "case", "in", "of", "if (x)", "while (x)", "f(x)", "(a + b)", "a[0]", "}",
        "{", ")", "]", "1", "'s'", "`t`", "x++", "=", "(", ",", "?", ":", "&&", "!", "=>", "else",
    )
    afters = ("{c}/", "{c}/.test(s)", " {c} / 2", "{c}/g, y", "[/]{c}/")
    provable = {"a.b", "f(x)", "(a + b)", "a[0]", "]", "1", "'s'", "`t`"}
    generated = 0
    for language in ("javascript", "dataweave"):
        for before in befores:
            for number, after in enumerate(afters):
                canary = f"cnry46{language[0]}{befores.index(before)}x{number}Zq"
                code = f"{before} /{after.format(c=canary)}"
                table = Placeholders()
                shown = table.code(code, R8_JS) if language == "javascript" else table._code(code, language)
                division = before in provable or (language == "javascript" and before == "a")
                if not division:
                    assert canary not in shown, (language, code, shown)
                assert _r8_round_trip(table, shown) == code, (language, code, shown)
                if language == "dataweave":
                    original = f"<mule><flow name=\"f\"><set-payload value={_attr('#[' + code + ']')}/></flow></mule>"
                    mule_table = Placeholders()
                    shown_mule = mule_table.mule({"f.xml": original})["f.xml"]
                    assert shown_mule is not None and mule_table.restore("f.xml", shown_mule) == original
                    if not division:
                        assert canary not in shown_mule, (code, shown_mule)
                generated += 1
    assert generated == 2 * len(befores) * len(afters)


def test_CP8_X47_every_code_lexer_fails_safe_on_forms_it_cannot_follow() -> None:
    """[CP8-X47] Forms a lexer could misread never show their literals: a JavaScript template literal nested in a
    ${...} part, a DataWeave interpolation holding a quoted string, a Python f-string reusing its quote (3.12), a
    Java unicode escape spelling a quote outside a string, JavaScript HTML-like comments and a #! line, and a string
    after a comma before a colon in a Java case label (not an object key)."""
    from a2m.ai.provider import ItemKind
    from a2m.verify.placeholders import Placeholders

    samples = (
        ("`a ${ `cnry47 inner` } b ${x}`", "javascript"),
        ("`a ${ f(`x ${ 'cnry47 deep' }`) } b`", "javascript"),
        ("`a ${ o[\"}\"] } cnry47 tail`", "javascript"),
        ("<!-- cnry47 html\nx = 1;", "javascript"),
        ("x = 1;\n--> cnry47 close", "javascript"),
        ("#!/usr/bin/env cnry47\nx = 1;", "javascript"),
        ('"a $(upper("cnry47 nested")) b"', "dataweave"),
        ("'a $(vars.x ++ 'cnry47 q') b'", "dataweave"),
        ('x = f"{"cnry47 same"}"', "python"),
        ("x = f'{d[\"k\"]} {\"cnry47 two\"}'", "python"),
        ("String s = \\u0022cnry47 unicode\\u0022;", "java"),
        ('switch (x) { case "a", "cnry47 label": break; }', "java"),
        ('switch (x) { case 1, "cnry47 label": break; }', "javascript"),
    )
    kinds = {"javascript": ItemKind.JAVASCRIPT, "python": ItemKind.PYTHON, "java": ItemKind.JAVA}
    for code, language in samples:
        table = Placeholders()
        shown = table.code(code, kinds[language]) if language in kinds else table._code(code, language)
        assert "cnry47" not in shown, (language, code, shown)


# Round 9: a DataWeave output or input directive is read only where it is one for sure.
R9_WORDS = ("output", "input")
# Positions of the word that are never a directive: field selectors, a variable, inside an expression.
R9_POSITIONS = (
    "payload.{w} matches {re} ++ {s}",
    "payload?.{w} matches {re} ++ {s}",
    "vars.{w} splitBy {re} ++ {s}",
    "payload.a.{w} replace {re} with {s}",
    "payload[0].{w} matches {re} ++ {s}",
    "payload..{w} scan {re} ++ {s}",
    "{w} matches {re} ++ {s}",
    "[{w}, payload.{w} matches {re}, {s}]",
    "{{k: payload.{w} matches {re}, v: {s}}}",
    "if (payload.{w} matches {re}) {s} else payload.{w}",
    "payload map ((item) -> item.{w} matches {re}) ++ [{s}]",
    "upper({w}) matches {re} ++ {s}",
)


def _r9_mule_value(value: str) -> str:
    return f"<mule><flow name=\"f\"><set-payload value={_attr(value)}/></flow></mule>"


def _r9_mule_script(script: str) -> str:
    return f"<mule><flow name=\"f\"><set-payload><![CDATA[{script}]]></set-payload></flow></mule>"


def _r9_check(table: Any, code: str, canaries: tuple[str, ...]) -> None:
    """``code`` (DataWeave) never shows a canary and round-trips byte for byte, alone and in a Mule file (as a
    ``#[...]`` expression, or as a set-payload script when it starts with ``%dw``)."""
    from a2m.verify.placeholders import Placeholders

    shown = table._code(code, "dataweave")
    for canary in canaries:
        assert canary not in shown, (code, shown)
    assert _r8_round_trip(table, shown) == code, (code, shown)
    original = _r9_mule_script(code) if code.startswith("%dw") else _r9_mule_value("#[" + code + "]")
    mule_table = Placeholders()
    shown_mule = mule_table.mule({"f.xml": original})["f.xml"]
    assert shown_mule is not None
    for canary in canaries:
        assert canary not in shown_mule, (code, shown_mule)
    assert mule_table.restore("f.xml", shown_mule) == original, (code, shown_mule)


def test_CP8_X48_output_and_input_as_selectors_variables_or_body_words_never_hide_a_regex_or_string() -> None:
    """[CP8-X48] The words output and input as a field selector (payload.output, payload?.input, vars.output ...),
    as a variable, inside a Mule #[...] expression, in a script header's var line and in a script body are ordinary
    words: a regular expression and a string literal after them never show, and each sample round-trips byte for
    byte, alone and through a Mule restore."""
    from a2m.verify.placeholders import Placeholders

    count = 0
    for word in R9_WORDS:
        for index, position in enumerate(R9_POSITIONS):
            for number, body in enumerate(R8_REGEX_BODIES[:4]):
                regex_canary = f"cnry48r{word[0]}{index}b{number}Zq"
                string_canary = f"cnry48s{word[0]}{index}b{number}Zq"
                fragment = position.format(w=word, re=body.format(c=regex_canary), s=_dw(string_canary))
                layouts = (
                    fragment,
                    "output application/json --- " + fragment,
                    f"%dw 2.0\noutput application/json\n---\n{fragment}\n",
                    f"%dw 2.0\noutput application/json\nvar v = {fragment}\n---\nv\n",
                    f"%dw 2.0\n{fragment}\n---\n1\n",
                    f"%dw 2.0\noutput application/json\n---\npayload ++\n{fragment}\n",
                )
                for code in layouts:
                    _r9_check(Placeholders(), code, (regex_canary, string_canary))
                    count += 1
    assert count == 2 * len(R9_POSITIONS) * 4 * 6


def test_CP8_X49_a_real_dataweave_header_stays_visible_and_the_script_round_trips_byte_exact() -> None:
    """[CP8-X49] A real header (output application/json with writer options, input with a name, a charset parameter,
    a trailing comment, a Mule #[output ... --- ...] expression) keeps its MIME types and option names visible; a
    string option value and a comment are placeholders; the script body after "---" is read as code, so
    payload.output matches /re/ there hides its regular expression; and every script round-trips byte for byte,
    alone and through a Mule restore."""
    from a2m.verify.placeholders import Placeholders

    scripts = (
        (
            "%dw 2.0\noutput application/json\n---\npayload.output matches /cnry49a/ ++ 'cnry49b'\n",
            ("output application/json\n---\npayload.output matches /",),
        ),
        (
            (
                "%dw 2.0\ninput payload application/xml\noutput text/plain; charset=UTF-8\n---\n"
                "payload.input splitBy /cnry49c/\n"
            ),
            ("input payload application/xml", "output text/plain; charset=UTF-8", "payload.input splitBy /"),
        ),
        (
            (
                '%dw 2.0\noutput application/json indent=false, skipNullOn="cnry49d" // cnry49e note\n---\n'
                "payload.total / 2 / vars.n\n"
            ),
            ("output application/json indent=false, skipNullOn=", "payload.total / 2 / vars.n"),
        ),
        (
            "output application/json --- payload.output matches /cnry49f/ ++ 'cnry49g'",
            ("output application/json --- payload.output matches /",),
        ),
        (
            "output application/vnd.api+json\n--- payload.a / 2",
            ("output application/vnd.api+json\n--- payload.a / 2",),
        ),
    )
    for code, visible in scripts:
        table = Placeholders()
        shown = table._code(code, "dataweave")
        for text in visible:
            assert text in shown, (code, shown)
        assert "cnry49" not in shown, (code, shown)
        _r9_check(Placeholders(), code, ("cnry49",))
    # A directive line holding anything outside the directive grammar is not a directive: nothing is shown for it.
    for code in (
        "%dw 2.0\noutput application/json ++ x matches /cnry49h/\n---\n1",
        "%dw 2.0\noutput payload matches /cnry49i/\n---\n1",
        "%dw 2.0\noutput a/b matches /cnry49j/\n---\n1",
        "output application/json matches /cnry49k/",
        "payload ++ output application/json --- x matches /cnry49l/",
    ):
        _r9_check(Placeholders(), code, ("cnry49",))


def test_CP8_X50_a_bounded_generator_over_the_lexer_mode_switches_never_leaks_a_canary() -> None:
    """[CP8-X50] Bounded generator over every lexer state that changes how a "/" or a string is read: the directive
    words (output, input) and words that open other states (case, if, unless, else, var, fun, import, do, match,
    using, type, ns) after 12 selector or expression prefixes (".", "?.", "..", ".@", ".^", ".*", ". " with a space,
    "[", "{k: ", "(", "x ++ ", none), each followed by three continuations (an infix regex, if (...) then a regex, a
    lambda holding a regex), in four layouts (a Mule expression, one after a header, a script header line and a
    script body). A regular expression or a string literal never shows, and each sample round-trips byte for byte,
    alone and through a Mule restore."""
    from a2m.verify.placeholders import Placeholders

    words = ("output", "input", "case", "if", "unless", "else", "var", "fun", "import", "do", "match", "using",
             "type", "ns")
    prefixes = ("", "payload.", "payload?.", "payload..", "payload.@", "payload.^", "payload.*", "payload. ",
                "[", "{k: ", "(", "x ++ ")
    closers = {"[": "]", "{k: ": "}", "(": ")"}
    continuations = (
        "{p}{w}{x} matches /{c}/ ++ '{s}'",
        "if ({p}{w}{x}) /{c}/ else '{s}'",
        "{p}{w}{x} map ((i) -> i splitBy /{c}/) ++ '{s}'",
    )
    generated = 0
    for wi, word in enumerate(words):
        for pi, prefix in enumerate(prefixes):
            for ci, continuation in enumerate(continuations):
                regex_canary = f"cnry50r{wi}p{pi}c{ci}Zq"
                string_canary = f"cnry50s{wi}p{pi}c{ci}Zq"
                fragment = continuation.format(p=prefix, w=word, x=closers.get(prefix, ""), c=regex_canary,
                                               s=string_canary)
                layouts = (
                    fragment,
                    "output application/json --- " + fragment,
                    f"%dw 2.0\n{fragment}\n---\n1\n",
                    f"%dw 2.0\noutput application/json\n---\n{fragment}\n",
                )
                for code in layouts:
                    _r9_check(Placeholders(), code, (regex_canary, string_canary))
                    generated += 1
    assert generated == len(words) * len(prefixes) * len(continuations) * 4


def test_CP8_X51_a_regex_after_a_field_named_output_never_reaches_a_fix_request_and_an_echo_restores(
    tmp_path: Path,
) -> None:
    """[CP8-X51] Through the fix loop: generated Mule expressions and a set-payload script hold a regular expression
    and a string after a field named output or input (#[payload.output matches /.../], payload?.input splitBy /.../,
    a script body's payload.output); no AiRequest field holds a canary, and an echo plus a logger writes every file
    back byte for byte."""
    bundle = write_shapes_bundle(tmp_path / "in")
    app = generate(bundle, tmp_path / SHAPES / "mule-app")
    script = "%dw 2.0\noutput application/json\n---\n{a: payload.output matches /cnry51c/, b: 'cnry51d'}\n"
    extra = shapes_mule(
        [
            '<set-variable variableName="o" value="#[payload.output matches /cnry51a/]"/>',
            "<set-variable variableName=\"i\" value=\"#[payload?.input splitBy /cnry51b/ ++ ['cnry51e']]\"/>",
            f"<set-payload><![CDATA[{script}]]></set-payload>",
        ]
    )
    (app.app_dir / SHAPES_REL).write_text(extra, encoding="utf-8")
    provider = ScriptedProvider([echo_with_logger])
    runner = ShapesRunner()

    loop = run(app, provider, runner)

    assert "payload.output matches /«v" in provider.requests[0].prompt, provider.requests[0].prompt[:4000]
    for request in provider.requests:
        for field_name, text in request_texts(request).items():
            assert "cnry51" not in text, (field_name, text[:4000])
    assert len(runner.files) == 2, loop.attempts[0].reason
    written = runner.files[1]
    assert written["shapes.xml"] == extra
    assert written["proxy.xml"] == insert_after(app.text, "<http:listener ", LOGGER)


def test_CP8_X52_a_slash_that_may_be_a_division_or_a_regex_hides_every_reading() -> None:
    """[CP8-X52] Bounded generator over the slash-pairing state: after a token where a "/" may be a division or start
    a regular expression (sizeOf(a), (x) -> x, if (c) x, a++, a--, a block's "}"), the rest of the line holds a later
    regular expression, a // comment, a string holding "/", a /* comment running to the next line, or a template
    literal. Whatever the reading, no canary shows, and each sample round-trips byte for byte (DataWeave also through
    a Mule restore). A regular expression holding "]" never ends a Mule #[...] expression early."""
    from a2m.verify.placeholders import Placeholders

    befores = {
        "dataweave": ("sizeOf(a)", "(x) -> x", "if (c) x", "f(x) ++ g(y)", "vars.a map (i) -> i"),
        "javascript": ("x = a++", "x = a--", "if (a) {} b", "f(); }", "x = (a) ? b : c++"),
    }
    tails = {
        "dataweave": (
            " / 2 ++ (s splitBy /{c}/)",
            " / 2 // {c} note",
            " / 2 ++ '{c}/x'",
            " / 2 ++ \"a\" ++ '/{c}'",
            " / 2 /* {c}\n{d} */ ++ 1",
            " / 2 / /{c}/",
        ),
        "javascript": (
            " / 2 + s.split(/{c}/);",
            " / 2 // {c} note",
            " / 2 + '{c}/x';",
            " / 2 + \"a\" + '/{c}';",
            " / 2 /* {c}\n{d} */ + 1;",
            " / 2 + `t/{c}`;",
            " / 2 / /{c}/.source;",
        ),
    }
    generated = 0
    for language, language_befores in befores.items():
        for bi, before in enumerate(language_befores):
            for ti, tail in enumerate(tails[language]):
                canary = f"cnry52{language[0]}{bi}t{ti}Zq"
                second = f"cnry52{language[0]}{bi}t{ti}Dq"
                code = before + tail.format(c=canary, d=second)
                if language == "dataweave":
                    _r9_check(Placeholders(), code, (canary, second))
                else:
                    table = Placeholders()
                    shown = table.code(code, R8_JS)
                    assert canary not in shown and second not in shown, (code, shown)
                    assert _r8_round_trip(table, shown) == code, (code, shown)
                generated += 1
    assert generated == 5 * 6 + 5 * 7
    for expression in ("payload matches /a\\]cnry52x/", "payload matches /x]cnry52y/ ++ 'cnry52z'"):
        _r9_check(Placeholders(), expression, ("cnry52",))


# ---------------------------------------------------------------- CP8 adversarial round 10 (CP8-X53..)
# Pinned here (findings of round 10): "the AI changed nothing" is concluded only when a2m is sure. The no-op check
# reads code with the placeholders' fail-safe lexer: blanks between tokens read for sure are reduced, and every
# literal (string, regular expression, template literal, comment) and every point the lexer is unsure of is compared
# byte for byte, so a fix that only changes blanks inside a literal is written and re-tested, never dropped.


def _r10_mule(attribute: str, script: str) -> str:
    """A Mule file with a ``#[...]`` expression attribute and a set-payload script, indented 4 spaces a level."""
    return (
        '<mule xmlns="http://www.mulesoft.org/schema/mule/core">\n    <flow name="f">\n'
        f"        <set-variable variableName=\"v\" value={_attr(attribute)}/>\n"
        f"        <set-payload><![CDATA[{script}]]></set-payload>\n    </flow>\n</mule>\n"
    )


R10_SCRIPT = (
    "%dw 2.0\noutput application/json\n---\n{\n    a: payload replace /a b/ with 'x',\n"
    "    b: 'p  q' ++ \"r s\" ++ `t u`, // note here\n    c: sizeOf(payload) /* d  e */ + 1\n}"
)
R10_ATTRIBUTE = "#[payload replace /a b/ with 'x']"


def test_CP8_X53_a_blank_changed_inside_a_dataweave_literal_is_a_change() -> None:
    """[CP8-X53] In a Mule file, a blank removed, added or changed inside a DataWeave regular expression (in an
    expression attribute and in a script), string, template literal or comment, at a point the lexer is unsure of,
    or in a part it hides, is a change (_same_document is False), as is a trailing blank of a line comment. Blanks
    between tokens are not."""
    from a2m.verify.fix_loop import _same_document

    old = _r10_mule(R10_ATTRIBUTE, R10_SCRIPT)
    changes = [
        ("attribute regex", "#[payload replace /ab/ with 'x']", R10_SCRIPT),
        ("attribute regex trailing blank", "#[payload replace /a b / with 'x']", R10_SCRIPT),
        ("script regex", R10_ATTRIBUTE, R10_SCRIPT.replace("/a b/", "/ab/")),
        ("single-quoted string", R10_ATTRIBUTE, R10_SCRIPT.replace("'p  q'", "'p q'")),
        ("double-quoted string", R10_ATTRIBUTE, R10_SCRIPT.replace('"r s"', '"rs"')),
        ("backtick literal", R10_ATTRIBUTE, R10_SCRIPT.replace("`t u`", "`t\tu`")),
        ("line comment", R10_ATTRIBUTE, R10_SCRIPT.replace("// note here", "// notehere")),
        ("line comment trailing blank", R10_ATTRIBUTE, R10_SCRIPT.replace("// note here", "// note here ")),
        ("block comment", R10_ATTRIBUTE, R10_SCRIPT.replace("/* d  e */", "/* d e */")),
        ("attribute string", "#[payload replace /a b/ with 'x ']", R10_SCRIPT),
    ]
    for label, attribute, script in changes:
        new = _r10_mule(attribute, script)
        assert new != old, label
        assert not _same_document(old, new), label
    # An ambiguous slash (a division or a regular expression) and a hidden part: compared as written.
    for code, edited in (
        ("#[sizeOf(payload) /a b/ 2]", "#[sizeOf(payload) /ab/ 2]"),
        ("#[payload ++ 'open  end]", "#[payload ++ 'open end]"),
        ("#[payload ++ «v1»  x]", "#[payload ++ «v1» x]"),
    ):
        assert not _same_document(_r10_mule(code, R10_SCRIPT), _r10_mule(edited, R10_SCRIPT)), code
    # Blanks between tokens read for sure are not a change.
    assert _same_document(old, _r10_mule("#[payload   replace /a b/   with 'x']", R10_SCRIPT))
    assert _same_document(old, _r10_mule(R10_ATTRIBUTE, R10_SCRIPT.replace("'x',\n    b:", "'x', b:")))
    # Operator characters a blank keeps apart: a - -b is not a--b.
    assert not _same_document(_r10_mule("#[1 - -payload]", R10_SCRIPT), _r10_mule("#[1--payload]", R10_SCRIPT))


def test_CP8_X54_a_blank_changed_inside_a_javascript_literal_is_a_change() -> None:
    """[CP8-X54] JavaScript: a blank changed inside a regular expression, string, template literal (with or without
    ${...}), line or block comment, or at an ambiguous slash is a change in its shape; re-indenting the code and
    changing blanks between tokens on a line is not; removing a line end (automatic semicolons) is."""
    from a2m.verify.fix_loop import _code_shape

    code = (
        "function f(s) {\n    var r = /a b/;\n    var t = 'p  q' + \"r s\" + `t u` + `v ${s} w`;\n"
        "    // note here\n    return s.replace(r, 'x') /* d  e */;\n}\n"
    )
    edits = [
        ("/a b/", "/ab/"),
        ("/a b/", "/a  b/"),
        ("'p  q'", "'p q'"),
        ('"r s"', '"r\ts"'),
        ("`t u`", "`tu`"),
        ("`v ${s} w`", "`v ${s}  w`"),
        ("// note here", "// notehere"),
        ("// note here", "// note here "),
        ("/* d  e */", "/* d e */"),
    ]
    shape = _code_shape(code, "javascript")
    for old, new in edits:
        assert old in code, old
        assert _code_shape(code.replace(old, new, 1), "javascript") != shape, (old, new)
    reindented = "".join(line[2:] if line.startswith("    ") else line for line in code.splitlines(keepends=True))
    assert reindented != code
    assert _code_shape(reindented, "javascript") == shape
    assert _code_shape(code.replace("var r = /a b/;", "var  r=/a b/ ;"), "javascript") == shape
    assert _code_shape(code.replace("{\n    var r", "{ var r"), "javascript") != shape
    assert _code_shape("x = a++ /a b/ 2", "javascript") != _code_shape("x = a++ /ab/ 2", "javascript")
    assert _code_shape("x = a - -b", "javascript") != _code_shape("x = a--b", "javascript")


def test_CP8_X55_a_blank_changed_inside_a_python_literal_or_its_indentation_is_a_change() -> None:
    """[CP8-X55] Python: a blank changed inside a string (plain, triple-quoted, raw, bytes, f-string) or a comment is
    a change, and so is a change of indentation (it is syntax in Python), while blanks between tokens on a line are
    not."""
    from a2m.verify.fix_loop import _code_shape

    code = (
        "def f(s):\n    t = 'p  q' + \"r s\" + '''u v''' + r'w x' + b'y z'\n    # note here\n"
        "    return f'{s}  a' + t\n"
    )
    edits = [
        ("'p  q'", "'p q'"),
        ('"r s"', '"rs"'),
        ("'''u v'''", "'''u\tv'''"),
        ("r'w x'", "r'wx'"),
        ("b'y z'", "b'y  z'"),
        ("# note here", "# notehere"),
        ("# note here", "# note here "),
        ("f'{s}  a'", "f'{s} a'"),
        ("    return", "  return"),
        ("r'w x'", "r 'w x'"),
    ]
    shape = _code_shape(code, "python")
    for old, new in edits:
        assert old in code, old
        assert _code_shape(code.replace(old, new, 1), "python") != shape, (old, new)
    assert _code_shape(code.replace("t = 'p  q' + ", "t='p  q'  +  "), "python") == shape


def test_CP8_X56_pure_re_indentation_outside_literals_is_still_changed_nothing() -> None:
    """[CP8-X56] A Mule file whose expression attribute and script hold a regular expression, strings, a template
    literal and comments, re-indented (4 spaces to 2, and to tabs) and reflowed between tokens, is the same
    document; the same re-indented file with one blank changed inside its regular expression is not."""
    from a2m.verify.fix_loop import _same_document

    old = _r10_mule(R10_ATTRIBUTE, R10_SCRIPT)
    two = "".join(
        " " * ((len(line) - len(line.lstrip(" "))) // 2) + line.lstrip(" ") for line in old.splitlines(keepends=True)
    )
    tabs = "".join(
        "\t" * ((len(line) - len(line.lstrip(" "))) // 4) + line.lstrip(" ") for line in old.splitlines(keepends=True)
    )
    assert two != old and tabs != old
    assert _same_document(old, two)
    assert _same_document(old, tabs)
    assert _same_document(old, old.replace("{\n    a: payload", "{ a:payload").replace("+ 1\n}", "+1 }"))
    assert not _same_document(old, two.replace("/a b/ with 'x',", "/ab/ with 'x',"))
    assert not _same_document(old, tabs.replace("#[payload replace /a b/", "#[payload replace /a  b/"))


def test_CP8_X57_a_bounded_generator_over_literal_kinds_never_drops_a_blank_change() -> None:
    """[CP8-X57] Bounded generator: for DataWeave, JavaScript and Python, every literal kind (strings in each quote,
    template literals, regular expressions, comments, an ambiguous slash, a hidden part) in a context, with 4 blank
    variants inside the literal and 3 layouts of the blanks outside it. Two samples have the same shape exactly when
    the blanks inside the literal are the same, whatever the layout outside; in DataWeave the same holds for
    _same_document on a Mule file (expression attribute or script)."""
    from a2m.verify.fix_loop import _code_shape, _same_document

    inside = (" ", "  ", "\t", "")
    # Each context: the code with {lit} for the literal and {w} for the blanks between two words, {p} next to
    # punctuation; each literal: its text with {s} for the blanks inside it.
    kinds: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
        "dataweave": (
            ("payload{w}matches{w}{lit}", "{{a:{p}payload{w}replace{w}{lit}{w}with{p}'x'{p}}}"),
            ("/a{s}b/", "/[a{s}b]/"),
        ),
        "dataweave-text": (
            ("[1,{p}{lit}{p}]{p}++{p}x", "f({lit}){p}++{p}y"),
            ("'a{s}b'", '"a{s}b"', "`a{s}b`"),
        ),
        "dataweave-comment": (
            ("x{p}+{p}1{p}{lit}\n{p}++{p}y",),
            ("// a{s}b", "/* a{s}b */"),
        ),
        "dataweave-unsure": (
            ("sizeOf(a){p}{lit}{p}2", "payload ++ {lit}"),
            ("/a{s}b/",),
        ),
        "dataweave-hidden": (
            ("payload ++ {lit}",),
            ("'open{s}end",),
        ),
        "javascript": (
            ("var{w}x{p}={p}{lit};", "f({p}{lit}{p},{p}y{p});", "x{p}={p}a++{w}{lit}{w}2;"),
            ("/a{s}b/", "'a{s}b'", '"a{s}b"', "`a{s}b`", "`a{s}${{y}}`", "/* a{s}b */"),
        ),
        "javascript-comment": (
            ("x{p}={p}1;{p}{lit}\ny{p}={p}2;",),
            ("// a{s}b",),
        ),
        "python": (
            ("x{p}={p}{lit}", "f({p}{lit}{p},{p}y{p})"),
            ("'a{s}b'", '"a{s}b"', "'''a{s}b'''", "r'a{s}b'", "b'a{s}b'", "f'{{y}}{s}b'"),
        ),
        "python-comment": (
            ("x{p}={p}1{p}{lit}\ny{p}={p}2",),
            ("# a{s}b",),
        ),
    }
    layouts = ({"w": " ", "p": ""}, {"w": "  ", "p": " "}, {"w": "\t", "p": "  "})
    generated = 0
    for name, (contexts, literals) in kinds.items():
        language = name.partition("-")[0]
        for context in contexts:
            for literal in literals:
                samples = []
                for si, blank in enumerate(inside):
                    for layout in layouts:
                        lit = literal.format(s=blank)
                        samples.append((si, context.format(lit=lit, **layout)))
                        generated += 1
                shapes = [(si, _code_shape(code, language)) for si, code in samples]
                for si, shape in shapes:
                    for sj, other in shapes:
                        assert (shape == other) == (si == sj), (name, context, literal, si, sj, samples)
                if language == "dataweave":
                    for si, code in samples:
                        for sj, other in samples:
                            as_attribute = _same_document(
                                _r10_mule("#[" + code + "]", R10_SCRIPT), _r10_mule("#[" + other + "]", R10_SCRIPT)
                            )
                            as_script = _same_document(
                                _r10_mule(R10_ATTRIBUTE, "%dw 2.0\n---\n" + code),
                                _r10_mule(R10_ATTRIBUTE, "%dw 2.0\n---\n" + other),
                            )
                            assert as_script == (si == sj), (name, code, other)
                            if "\n" not in code and "\n" not in other:
                                assert as_attribute == (si == sj), (name, code, other)
    assert generated == 12 * (2 * 2 + 2 * 3 + 1 * 2 + 2 * 1 + 1 * 1 + 3 * 6 + 1 * 1 + 2 * 6 + 1 * 1)


def _r10_regex_app(tmp_path: Path) -> App:
    """The JS proxy of CP8-X09 with an AI-translated JS-SetVersion step whose expression holds a regular expression
    with a wrong trailing blank: /a / (the fix is /a/)."""
    from a2m.ai.fake import FakeProvider

    step = (
        '<set-variable xmlns="http://www.mulesoft.org/schema/mule/core" variableName="responseHeaders" '
        "value=\"#[vars.responseHeaders default {} ++ {'X-Api-Version': 'a2' replace /a / with ''}]\"/>"
    )
    llm = tmp_path / "llm"
    llm.mkdir(parents=True)
    answer = {"status": "translated", "confidence": "high", "notes": "cp8-x58", "mule": step, "writes": None}
    (llm / "javascript.JS-SetVersion.json").write_text(json.dumps(answer), encoding="utf-8")
    app = generate(write_js_bundle(tmp_path / "in"), tmp_path / JS_APP / "mule-app", provider=FakeProvider(llm))
    assert "replace /a / with" in app.text and 'doc:name="JS-SetVersion"' in app.text
    return app


class RegexVersionRunner(RecordingRunner):
    """The JS proxy's app: forwards each call to the backend and answers X-Api-Version '2' when its proxy.xml holds
    the regular expression /a/ now (re-read on every call), 'a2' when it holds /a / ('a2' holds no "a ")."""

    def start(self, app: Any, *, backend_url: str) -> Any:
        self.started.append(app.name)
        flow = Path(app.app_dir) / FLOW_REL
        self.snapshots.append(flow.read_text(encoding="utf-8"))

        class _Handle:
            running = True
            base_url = "http://fake-regex-version.invalid/app"

            def send(self, request: Any) -> Any:
                version = "2" if "replace /a/ with" in flow.read_text(encoding="utf-8") else "a2"
                return forward(backend_url, request, {"X-Api-Version": version})

            def stop(self) -> None:
                pass

        return _Handle()


def test_CP8_X58_a_fix_that_only_changes_a_blank_inside_a_regex_is_written_retested_and_kept(tmp_path: Path) -> None:
    """[CP8-X58] Through the fix loop: the AI-translated step's regular expression /a / makes the golden case fail;
    the AI's fix only drops that blank (/a/). The fix is written, re-tested and kept: the attempt helped, names
    the file and the step, the result is golden and proxy.xml holds /a/."""
    app = _r10_regex_app(tmp_path)
    golden = write_js_golden(tmp_path / "golden")
    corrected = app.text.replace("replace /a / with", "replace /a/ with")
    assert corrected != app.text
    runner = RegexVersionRunner()

    loop = run(app, ScriptedProvider([lambda request: fixed(corrected, confidence="high")]), runner, golden=golden)

    attempt = loop.attempts[0]
    assert attempt.helped is True, attempt.reason
    assert "changed nothing" not in attempt.reason
    assert attempt.changed_files == (FLOW_REL,)
    assert attempt.changed_steps == ("JS-SetVersion",)
    assert loop.result.type.value == "golden", loop.attempts
    assert flow_text(app) == corrected
    assert "replace /a/ with" in runner.snapshots[-1]
