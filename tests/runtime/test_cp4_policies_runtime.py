"""CP4: generated policy steps behave like Apigee on the local Mule runtime (Mule Kernel CE 4.9.0).

Every test here is marked ``runtime``: excluded from a plain ``pytest -q``,
skipped with a reason when java, mvn or MULE_HOME is missing, and failing
instead under A2M_REQUIRE_RUNTIME=1 (see tests/runtime/conftest.py).

The 'policy-runtime' fixture bundle (tests/fixtures/apigee/policy-runtime/) is
generated with a2m, built once with Maven and deployed to the CP3 session
runtime. Its three ProxyEndpoints each use one base path, so no case changes
another's state: /keyed (VerifyAPIKey on query parameter apikey, then
AssignMessage setting X-A2M-Test), /spike (SpikeArrest 30pm, shared) and
/quota (Quota 2 per minute, shared). The listen port, the target address and
the allowed API key are set through the generated properties file only; the
flow XML is never edited. The allowed-keys property is found as the one
property that is empty in the generated file (a2m's fail-closed default) and
is referenced from the flow XML.

A loopback backend on 127.0.0.1 records every request it receives and answers
200 {"backend":"ok"}.

Entry points used: a2m.parser.read_bundle, a2m.generator.generate_project and
a2m.verify.mule (package, BuildError, DeployError, MuleRunner via the
mule_runtime fixture).
"""

from __future__ import annotations

import contextlib
import http.client
import json
import re
import shutil
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "apigee"
POLICY_RUNTIME = FIXTURES / "policy-runtime"
ORDERS_API = FIXTURES / "cp4" / "orders-api"
CORE = "http://www.mulesoft.org/schema/mule/core"
HTTP_NS = "http://www.mulesoft.org/schema/mule/http"
POM = "http://maven.apache.org/POM/4.0.0"
PLACEHOLDER = re.compile(r"\$\{([^}]*)\}")
P_FUNCTION = re.compile(r"""(?:Mule::)?p\(\s*(['"])(.*?)\1\s*\)""")
PURE_PLACEHOLDER = re.compile(r"\$\{([^}]+)\}")
DEPLOY_TIMEOUT = 180.0
BUILD_TIMEOUT = 900.0
GOOD_KEY = "good-key-123"
BACKEND_BODY = b'{"backend":"ok"}'
PINNED = {
    HTTP_NS: ("org.mule.connectors", "mule-http-connector", "1.11.3"),
    "http://www.mulesoft.org/schema/mule/os": ("org.mule.connectors", "mule-objectstore-connector", "1.2.2"),
    "http://www.mulesoft.org/schema/mule/validation": ("org.mule.modules", "mule-validation-module", "2.0.9"),
}
XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'


# ---------------------------------------------------------------- loopback recording backend


@dataclass(frozen=True)
class Recorded:
    method: str
    path: str
    headers: dict[str, str]


@dataclass
class Backend:
    port: int
    records: list[Recorded] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def clear(self) -> None:
        with self.lock:
            self.records.clear()

    def seen(self) -> list[Recorded]:
        with self.lock:
            return list(self.records)


def make_handler(backend: Backend) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            with backend.lock:
                backend.records.append(
                    Recorded(self.command, urlsplit(self.path).path, {k.lower(): v for k, v in self.headers.items()})
                )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(BACKEND_BODY)))
            self.end_headers()
            self.wfile.write(BACKEND_BODY)

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _handle

        def log_message(self, format: str, *args: Any) -> None:
            return

    return Handler


@pytest.fixture(scope="module")
def backend() -> Iterator[Backend]:
    state = Backend(port=0)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    server.daemon_threads = True
    state.port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


def call(port: int, path: str, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        conn.request("GET", path, headers=headers or {})
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


# ---------------------------------------------------------------- generating, configuring and building


def read_properties(path: Path) -> dict[str, str]:
    props: dict[str, str] = {}
    logical: list[str] = []
    pending = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.lstrip()
        if not pending and (not line or line[0] in "#!"):
            continue
        if (len(line) - len(line.rstrip("\\"))) % 2 == 1:
            pending += line[:-1]
            continue
        logical.append(pending + line)
        pending = ""
    if pending:
        logical.append(pending)
    for line in logical:
        match = re.match(r"((?:\\.|[^=:\s\\])*)\s*[=:]?\s*(.*)$", line)
        assert match is not None
        props[_unescape(match.group(1))] = _unescape(match.group(2))
    return props


def _unescape(text: str) -> str:
    def one(match: re.Match[str]) -> str:
        token = match.group(1)
        if token.startswith("u"):
            return chr(int(token[1:], 16))
        return {"t": "\t", "n": "\n", "r": "\r", "f": "\f"}.get(token, token)

    return re.sub(r"\\(u[0-9a-fA-F]{4}|.)", one, text)


def _escape(text: str, *, key: bool) -> str:
    out = []
    for i, char in enumerate(text):
        if char == "\\":
            out.append("\\\\")
        elif char in "\t\n\r\f":
            out.append({"\t": "\\t", "\n": "\\n", "\r": "\\r", "\f": "\\f"}[char])
        elif char in "=:#!" or (char == " " and (key or i == 0)):
            out.append("\\" + char)
        elif ord(char) > 126 or ord(char) < 32:
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    return "".join(out)


def write_properties(path: Path, props: dict[str, str]) -> None:
    path.write_text(
        "".join(f"{_escape(k, key=True)}={_escape(v, key=False)}\n" for k, v in sorted(props.items())),
        encoding="utf-8",
    )


def properties_file(project: Path) -> Path:
    found = sorted(p for p in (project / "src" / "main" / "resources").rglob("*.properties") if p.is_file())
    assert len(found) == 1, found
    return found[0]


def flow_docs(project: Path) -> list[ET.Element]:
    return [ET.parse(path).getroot() for path in sorted((project / "src" / "main" / "mule").glob("*.xml"))]


def pure_key(value: str | None, what: str) -> str:
    match = PURE_PLACEHOLDER.fullmatch(value or "")
    assert match is not None, f"{what} {value!r} is not set through a property"
    return match.group(1)


def flow_property_refs(project: Path) -> set[str]:
    refs: set[str] = set()
    for doc in flow_docs(project):
        for element in doc.iter():
            for value in [*element.attrib.values(), element.text or ""]:
                refs |= set(PLACEHOLDER.findall(value)) | {m.group(2) for m in P_FUNCTION.finditer(value)}
    return refs


def configure(project: Path, listen_port: int, backend_port: int, api_key: str | None) -> None:
    """Point the app at ``listen_port`` and the loopback backend, and allow ``api_key``, through properties only."""
    docs = flow_docs(project)
    settings: dict[str, str] = {}
    for doc in docs:
        for conn in doc.iter(f"{{{HTTP_NS}}}listener-connection"):
            settings[pure_key(conn.get("port"), "listener port")] = str(listen_port)
        for conn in doc.iter(f"{{{HTTP_NS}}}request-connection"):
            settings[pure_key(conn.get("protocol"), "target protocol")] = "HTTP"
            settings[pure_key(conn.get("host"), "target host")] = "127.0.0.1"
            settings[pure_key(conn.get("port"), "target port")] = str(backend_port)
    path = properties_file(project)
    props = read_properties(path)
    missing = sorted(set(settings) - set(props))
    assert not missing, f"properties referenced by the flow are missing from {path.name}: {missing}"
    if api_key is not None:
        candidates = sorted(k for k in flow_property_refs(project) if props.get(k) == "" and k not in settings)
        assert len(candidates) == 1, (
            f"expected exactly one empty property (the allowed API keys) referenced by the flow, found {candidates}"
        )
        settings[candidates[0]] = api_key
    props.update(settings)
    write_properties(path, props)


@dataclass
class BuiltApp:
    name: str
    port: int
    project: Path
    jar: Path
    result: Any


def build(bundle_dir: Path, work: Path, backend_port: int, listen_port: int, api_key: str | None) -> BuiltApp:
    from a2m.generator import generate_project
    from a2m.parser import read_bundle
    from a2m.verify.mule import BuildError, package

    bundle = read_bundle(bundle_dir)
    project = work / bundle.name / "mule-app"
    result = generate_project(bundle, project, shared_flows=())
    configure(project, listen_port, backend_port, api_key)
    try:
        jar = package(project, timeout=BUILD_TIMEOUT)
    except BuildError as exc:
        pytest.fail(f"mvn package failed for {bundle.name}: {exc}\n{exc.output}", pytrace=False)
    return BuiltApp(bundle.name, listen_port, project, Path(jar), result)


def log_lines(mule_base: Path) -> list[str]:
    log = mule_base / "logs" / "mule.log"
    return log.read_text(encoding="utf-8", errors="replace").splitlines() if log.is_file() else []


def deploy(mule_runtime: Any, app: BuiltApp) -> None:
    from a2m.verify.mule import DeployError

    try:
        mule_runtime.runner.deploy(app.jar, app_name=app.name, timeout=DEPLOY_TIMEOUT)
    except DeployError as exc:
        pytest.fail(f"{app.name} did not deploy: {exc}\n{exc.log_excerpt}", pytrace=False)


def assert_started(mule_runtime: Any, name: str) -> None:
    base = Path(mule_runtime.mule_base)
    lines = log_lines(base)
    anchor = base / "apps" / f"{name}-anchor.txt"
    started = anchor.is_file() or any("Started app" in line and name in line for line in lines)
    excerpt = "\n".join(lines[-60:])
    assert started, f"no deploy signal for {name}:\n{excerpt}"
    failed = [line for line in lines if "Failed to deploy artifact" in line and name in line]
    assert failed == [], f"{name} failed to deploy:\n{excerpt}"


def assert_pom_pins(project: Path) -> None:
    used: set[str] = set()
    for doc in flow_docs(project):
        for element in doc.iter():
            for name in [element.tag, *element.attrib]:
                if isinstance(name, str) and name.startswith("{"):
                    used.add(name[1:].split("}", 1)[0])
    ns = {"m": POM}
    pom = ET.parse(project / "pom.xml").getroot()
    deps = {
        ((d.findtext("m:groupId", "", ns) or "").strip(), (d.findtext("m:artifactId", "", ns) or "").strip()): (
            d.findtext("m:version", "", ns) or ""
        ).strip()
        for d in pom.findall("m:dependencies/m:dependency", ns)
    }
    for uri, (group, artifact, version) in PINNED.items():
        if uri in used:
            assert deps.get((group, artifact)) == version, f"{uri} is used; pom has {group}:{artifact} {deps}"


@pytest.fixture(scope="module")
def policy_app(
    mule_runtime: Any, backend: Backend, free_port: Callable[[], int], tmp_path_factory: pytest.TempPathFactory
) -> Iterator[BuiltApp]:
    """policy-runtime generated, configured, built once and deployed for the module."""
    work = tmp_path_factory.mktemp("cp4-policy-runtime")
    source = work / "bundles" / "policy-runtime"
    shutil.copytree(POLICY_RUNTIME, source)
    app = build(source, work, backend.port, free_port(), GOOD_KEY)
    deploy(mule_runtime, app)
    try:
        yield app
    finally:
        with contextlib.suppress(Exception):
            mule_runtime.runner.undeploy(app.name, timeout=60)


# ---------------------------------------------------------------- runtime cases


@pytest.mark.runtime
def test_CP4_T35_the_generated_policy_app_builds_and_starts_on_the_local_mule_runtime(
    mule_runtime: Any, policy_app: BuiltApp
) -> None:
    """[CP4-T35] The generated policy app builds and starts on the local Mule runtime."""
    jars = sorted((policy_app.project / "target").glob("*-mule-application.jar"))
    assert len(jars) == 1, jars
    assert policy_app.jar.resolve() == jars[0].resolve()
    assert_started(mule_runtime, policy_app.name)
    assert_pom_pins(policy_app.project)


@pytest.mark.runtime
def test_CP4_T36_a_valid_api_key_gets_through_to_the_backend(policy_app: BuiltApp, backend: Backend) -> None:
    """[CP4-T36] A valid API key gets through to the backend."""
    backend.clear()

    status, body = call(policy_app.port, f"/keyed/ping?apikey={GOOD_KEY}")

    assert status == 200, body
    assert json.loads(body) == {"backend": "ok"}
    seen = backend.seen()
    assert len(seen) == 1, seen
    assert seen[0].path.endswith("/ping"), seen[0].path


@pytest.mark.runtime
def test_CP4_T37_a_missing_api_key_gets_401_and_never_reaches_the_backend(
    policy_app: BuiltApp, backend: Backend
) -> None:
    """[CP4-T37] A missing API key gets 401 and never reaches the backend."""
    backend.clear()

    status, body = call(policy_app.port, "/keyed/ping")

    assert status == 401, (status, body)
    assert backend.seen() == []


@pytest.mark.runtime
def test_CP4_T38_a_wrong_or_empty_api_key_gets_401_and_never_reaches_the_backend(
    policy_app: BuiltApp, backend: Backend
) -> None:
    """[CP4-T38] A wrong or empty API key gets 401 and never reaches the backend."""
    backend.clear()

    statuses = [
        call(policy_app.port, f"/keyed/ping?apikey={key}")[0] for key in ("wrong-key", "", GOOD_KEY.upper())
    ]

    assert statuses == [401, 401, 401], statuses
    assert backend.seen() == []


@pytest.mark.runtime
def test_CP4_T39_assign_message_sets_the_header_the_backend_receives(policy_app: BuiltApp, backend: Backend) -> None:
    """[CP4-T39] AssignMessage sets the header the backend receives."""
    backend.clear()

    status, body = call(policy_app.port, f"/keyed/ping?apikey={GOOD_KEY}", headers={"X-Caller": "test-suite"})

    assert status == 200, body
    seen = backend.seen()
    assert len(seen) == 1, seen
    assert seen[0].headers.get("x-a2m-test") == "assigned-by-a2m", seen[0].headers
    assert seen[0].headers.get("x-caller") == "test-suite", seen[0].headers


@pytest.mark.runtime
def test_CP4_T40_spike_arrest_answers_429_too_soon_and_lets_calls_through_after_the_interval(
    policy_app: BuiltApp, backend: Backend
) -> None:
    """[CP4-T40] SpikeArrest answers 429 for a call that comes too soon, and lets calls through again later."""
    backend.clear()

    first = call(policy_app.port, "/spike/ping")[0]
    second = call(policy_app.port, "/spike/ping")[0]
    time.sleep(2.5)
    third = call(policy_app.port, "/spike/ping")[0]

    assert [first, second, third] == [200, 429, 200]
    assert len(backend.seen()) == 2, backend.seen()


@pytest.mark.runtime
def test_CP4_T41_quota_answers_429_once_the_allowance_is_used_up(policy_app: BuiltApp, backend: Backend) -> None:
    """[CP4-T41] Quota answers 429 once the allowance for the window is used up."""
    backend.clear()

    statuses = [call(policy_app.port, "/quota/ping")[0] for _ in range(4)]

    assert statuses == [200, 200, 429, 429], statuses
    assert len(backend.seen()) == 2, backend.seen()


ORDER_NOT_FOUND = (
    XML_HEAD + '<RaiseFault async="false" continueOnError="false" enabled="true" name="Order-Not-Found">\n'
    "    <DisplayName>Order-Not-Found</DisplayName>\n    <FaultResponse>\n        <Set>\n"
    '            <Headers>\n                <Header name="X-Error">missing-order</Header>\n            </Headers>\n'
    '            <Payload contentType="application/json">{"error":"not found"}</Payload>\n'
    "            <StatusCode>404</StatusCode>\n            <ReasonPhrase>Not Found</ReasonPhrase>\n"
    "        </Set>\n    </FaultResponse>\n</RaiseFault>\n"
)


def all_eight_bundle(parent: Path) -> Path:
    """orders-api plus a RaiseFault step at the end of the proxy PostFlow response: all eight standard types."""
    root = parent / "orders-api"
    shutil.copytree(ORDERS_API, root)
    (root / "apiproxy" / "policies" / "Order-Not-Found.xml").write_text(ORDER_NOT_FOUND, encoding="utf-8")
    path = root / "apiproxy" / "proxies" / "default.xml"
    tree = ET.parse(path)
    response = tree.getroot().find("PostFlow/Response")
    assert response is not None
    step = ET.SubElement(response, "Step")
    ET.SubElement(step, "Name").text = "Order-Not-Found"
    path.write_text(XML_HEAD + ET.tostring(tree.getroot(), encoding="unicode") + "\n", encoding="utf-8")
    return root


@pytest.mark.runtime
def test_CP4_T42_an_app_using_all_eight_policy_templates_deploys_on_mule_490_ce(
    mule_runtime: Any, backend: Backend, free_port: Callable[[], int], tmp_path: Path
) -> None:
    """[CP4-T42] An app using all eight policy templates deploys on Mule 4.9.0 CE."""
    from a2m.parser import read_bundle

    source = all_eight_bundle(tmp_path / "bundles")
    eight = {
        "SpikeArrest",
        "Quota",
        "VerifyAPIKey",
        "AssignMessage",
        "ExtractVariables",
        "RaiseFault",
        "BasicAuthentication",
        "AccessControl",
    }
    assert {p.type for p in read_bundle(source).policies} == eight
    app = build(source, tmp_path, backend.port, free_port(), None)
    templated = {str(r.type) for r in app.result.policies if str(r.method) == "template"}
    assert templated == eight, [(str(r.name), str(r.method), str(r.reason)) for r in app.result.policies]
    try:
        deploy(mule_runtime, app)
        assert_started(mule_runtime, app.name)
    finally:
        with contextlib.suppress(Exception):
            mule_runtime.runner.undeploy(app.name, timeout=60)


# ---------------------------------------------------------------- CP4 adversarial round 1: concurrent bursts
#
# SpikeArrest and Quota must hold their limit when calls arrive at the same time on different worker
# threads (findings A1 and A2): a read-then-write counter lets several calls of a burst read the same state
# and all pass. Each burst below opens one connection per call and releases all calls together.

import concurrent.futures  # noqa: E402

BURST = 20
BURST_ROUNDS = 4
RACE_QUOTA = 5
RACE_POLICIES = {
    "Spike-Arrest.xml": (
        XML_HEAD + '<SpikeArrest async="false" continueOnError="false" enabled="true" name="Spike-Arrest">\n'
        "    <DisplayName>Spike Arrest</DisplayName>\n"
        '    <Identifier ref="request.header.x-client"/>\n'
        "    <Rate>30pm</Rate>\n</SpikeArrest>\n"
    ),
    "Minute-Quota.xml": (
        XML_HEAD + '<Quota async="false" continueOnError="false" enabled="true" name="Minute-Quota">\n'
        "    <DisplayName>Minute Quota</DisplayName>\n"
        f'    <Allow count="{RACE_QUOTA}"/>\n'
        "    <Interval>1</Interval>\n    <TimeUnit>minute</TimeUnit>\n"
        '    <Identifier ref="request.header.x-client"/>\n</Quota>\n'
    ),
}


@dataclass(frozen=True)
class BodyRecord:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


@dataclass
class BodyBackend:
    port: int
    records: list[BodyRecord] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def seen(self) -> list[BodyRecord]:
        with self.lock:
            return list(self.records)

    def for_client(self, client: str) -> list[BodyRecord]:
        return [r for r in self.seen() if r.headers.get("x-client") == client]


def make_body_handler(backend: BodyBackend) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            with backend.lock:
                backend.records.append(
                    BodyRecord(
                        self.command,
                        urlsplit(self.path).path,
                        {k.lower(): v for k, v in self.headers.items()},
                        body,
                    )
                )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(BACKEND_BODY)))
            self.end_headers()
            self.wfile.write(BACKEND_BODY)

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _handle

        def log_message(self, format: str, *args: Any) -> None:
            return

    return Handler


@pytest.fixture(scope="module")
def body_backend() -> Iterator[BodyBackend]:
    state = BodyBackend(port=0)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_body_handler(state))
    server.daemon_threads = True
    state.port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


def burst(port: int, path: str, headers: dict[str, str] | None = None, size: int = BURST) -> list[int]:
    """``size`` calls released together, one connection and one thread each; their statuses."""
    gate = threading.Barrier(size)

    def one(_: int) -> int:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        try:
            conn.connect()
            gate.wait(timeout=30)
            conn.request("GET", path, headers=headers or {})
            response = conn.getresponse()
            response.read()
            return response.status
        finally:
            conn.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=size) as pool:
        return list(pool.map(one, range(size)))


def wait_for_fresh_minute(margin: float = 8.0) -> None:
    """Sleep past the next minute boundary when fewer than ``margin`` seconds are left in this one."""
    left = 60.0 - (time.time() % 60.0)
    if left < margin:
        time.sleep(left + 0.5)


def race_bundle(parent: Path) -> Path:
    """policy-runtime renamed to policy-race, with SpikeArrest 30pm and Quota 5 per minute keyed by X-Client."""
    root = parent / "policy-race"
    shutil.copytree(POLICY_RUNTIME, root)
    manifest = root / "apiproxy" / "policy-runtime.xml"
    tree = ET.parse(manifest)
    tree.getroot().set("name", "policy-race")
    (root / "apiproxy" / "policy-race.xml").write_text(
        XML_HEAD + ET.tostring(tree.getroot(), encoding="unicode") + "\n", encoding="utf-8"
    )
    manifest.unlink()
    for name, text in RACE_POLICIES.items():
        (root / "apiproxy" / "policies" / name).write_text(text, encoding="utf-8")
    return root


@pytest.fixture(scope="module")
def race_app(
    mule_runtime: Any,
    body_backend: BodyBackend,
    free_port: Callable[[], int],
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[BuiltApp]:
    """policy-race generated, configured, built once and deployed for the module."""
    work = tmp_path_factory.mktemp("cp4-policy-race")
    source = race_bundle(work / "bundles")
    app = build(source, work, body_backend.port, free_port(), None)
    deploy(mule_runtime, app)
    try:
        yield app
    finally:
        with contextlib.suppress(Exception):
            mule_runtime.runner.undeploy(app.name, timeout=60)


@pytest.mark.runtime
def test_CP4_T44_a_concurrent_burst_at_a_shared_spike_arrest_lets_exactly_one_call_through(
    policy_app: BuiltApp, backend: Backend
) -> None:
    """[CP4-T44] A concurrent burst at a shared SpikeArrest (30pm) lets exactly one call through, every round."""
    for round_no in range(BURST_ROUNDS):
        time.sleep(2.5)
        backend.clear()

        statuses = burst(policy_app.port, "/spike/ping")

        assert sorted(statuses) == [200] + [429] * (BURST - 1), (round_no, statuses)
        assert len(backend.seen()) == 1, (round_no, backend.seen())


@pytest.mark.runtime
def test_CP4_T45_a_concurrent_burst_per_client_lets_exactly_the_spike_arrest_allowance_through(
    race_app: BuiltApp, body_backend: BodyBackend
) -> None:
    """[CP4-T45] A concurrent burst per client at SpikeArrest 30pm lets exactly one call through, every round."""
    for round_no in range(BURST_ROUNDS):
        client = f"spike-client-{round_no}-{time.time_ns()}"

        statuses = burst(race_app.port, "/spike/ping", headers={"X-Client": client})

        assert sorted(statuses) == [200] + [429] * (BURST - 1), (round_no, statuses)
        assert len(body_backend.for_client(client)) == 1, (round_no, body_backend.for_client(client))


@pytest.mark.runtime
def test_CP4_T46_a_concurrent_burst_per_client_lets_exactly_the_quota_allowance_through(
    race_app: BuiltApp, body_backend: BodyBackend
) -> None:
    """[CP4-T46] A concurrent burst per client at Quota 5 per minute lets exactly 5 calls through, every round."""
    for round_no in range(BURST_ROUNDS):
        client = f"quota-client-{round_no}-{time.time_ns()}"
        wait_for_fresh_minute()

        statuses = burst(race_app.port, "/quota/ping", headers={"X-Client": client})
        late = call(race_app.port, "/quota/ping", headers={"X-Client": client})[0]

        assert sorted(statuses) == [200] * RACE_QUOTA + [429] * (BURST - RACE_QUOTA), (round_no, statuses)
        assert late == 429, (round_no, late)
        assert len(body_backend.for_client(client)) == RACE_QUOTA, (round_no, body_backend.for_client(client))


@pytest.mark.runtime
def test_CP4_T47_calls_allowed_by_quota_and_spike_arrest_reach_the_backend_unchanged(
    race_app: BuiltApp, body_backend: BodyBackend
) -> None:
    """[CP4-T47] A call allowed by Quota or SpikeArrest reaches the backend with its method, path, headers and body."""
    for base in ("/quota", "/spike"):
        client = f"body-client-{base[1:]}-{time.time_ns()}"
        wait_for_fresh_minute()
        conn = http.client.HTTPConnection("127.0.0.1", race_app.port, timeout=30)
        try:
            conn.request(
                "POST",
                f"{base}/orders/7",
                body=b'{"order":7}',
                headers={"X-Client": client, "X-Extra": "kept", "Content-Type": "application/json"},
            )
            response = conn.getresponse()
            status, body = response.status, response.read()
        finally:
            conn.close()

        assert status == 200, (base, status, body)
        seen = body_backend.for_client(client)
        assert len(seen) == 1, (base, seen)
        assert seen[0].method == "POST", (base, seen[0])
        assert seen[0].path.endswith("/orders/7"), (base, seen[0].path)
        assert seen[0].headers.get("x-extra") == "kept", (base, seen[0].headers)
        assert seen[0].body == b'{"order":7}', (base, seen[0].body)
