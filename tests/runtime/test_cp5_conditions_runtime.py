"""CP5 adversarial round 1: translated conditions and templates behave like Apigee on the local Mule runtime.

Every test here is marked ``runtime``: excluded from a plain ``pytest -q``,
skipped with a reason when java, mvn or MULE_HOME is missing, and failing
instead under A2M_REQUIRE_RUNTIME=1 (see tests/runtime/conftest.py).

The 'cp5-runtime' bundle is written under pytest's tmp folder, generated with
a2m, built once with Maven and deployed to the session runtime. Its one
ProxyEndpoint (/cp5):

* request PreFlow: AM-Backend sets the flow variable a2mtest.backend to the
  loopback backend's address; EV-Order extracts $.order.id from a JSON body
  into ext.orderId; AM-Order-Seen (condition ext.orderId = "42") sets the
  request header X-Order-Seen; AM-Json-Accept (condition request.header.Accept =
  "application/json") sets the request header X-Json-Accept;
* response PreFlow: AM-Not-Options (condition request.verb != "OPTIONS") sets
  the response header X-Not-Options; AM-Echo sets the response header X-Echo
  to {request.header.X-Caller};
* the TargetEndpoint URL is http://{a2mtest.backend}/shop.

Only the listen port is set through the generated properties file; the flow
XML is never edited. A loopback backend on 127.0.0.1 records every request.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import re
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

DEPLOY_TIMEOUT = 180.0
BUILD_TIMEOUT = 900.0
BACKEND_BODY = b'{"backend":"ok"}'
XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
APP = "cp5-runtime"


# ---------------------------------------------------------------- loopback recording backend


@dataclass(frozen=True)
class Seen:
    method: str
    path: str
    headers: dict[str, str]


@dataclass
class Backend:
    port: int
    records: list[Seen] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def clear(self) -> None:
        with self.lock:
            self.records.clear()

    def seen(self) -> list[Seen]:
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
                    Seen(self.command, urlsplit(self.path).path, {k.lower(): v for k, v in self.headers.items()})
                )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(BACKEND_BODY)))
            self.end_headers()
            self.wfile.write(BACKEND_BODY)

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _handle

        def log_message(self, format: str, *args: Any) -> None:
            return

    return Handler


@pytest.fixture(scope="module")
def cp5_backend() -> Iterator[Backend]:
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


def call(
    port: int, method: str, path: str, headers: dict[str, str] | None = None, body: bytes | None = None
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
    finally:
        conn.close()


# ---------------------------------------------------------------- the bundle


def _policy(root_tag: str, name: str, body: str) -> str:
    return (
        XML_HEAD + f'<{root_tag} async="false" continueOnError="false" enabled="true" name="{name}">\n'
        f"    <DisplayName>{name}</DisplayName>\n{body}</{root_tag}>\n"
    )


def _step(name: str, condition: str | None = None) -> str:
    cond = f"<Condition>{condition}</Condition>" if condition is not None else ""
    return f"<Step>{cond}<Name>{name}</Name></Step>"


def _set_header(name: str, header: str, value: str, kind: str) -> str:
    return _policy(
        "AssignMessage",
        name,
        f'    <Set>\n        <Headers>\n            <Header name="{header}">{value}</Header>\n'
        "        </Headers>\n    </Set>\n    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n"
        f'    <AssignTo createNew="false" transport="http" type="{kind}"/>\n',
    )


def write_cp5_bundle(parent: Path, backend_port: int) -> Path:
    policies = {
        "AM-Backend": _policy(
            "AssignMessage",
            "AM-Backend",
            "    <AssignVariable>\n        <Name>a2mtest.backend</Name>\n"
            f"        <Value>127.0.0.1:{backend_port}</Value>\n    </AssignVariable>\n",
        ),
        "EV-Order": _policy(
            "ExtractVariables",
            "EV-Order",
            "    <Source>request</Source>\n    <VariablePrefix>ext</VariablePrefix>\n"
            '    <JSONPayload>\n        <Variable name="orderId">\n            <JSONPath>$.order.id</JSONPath>\n'
            "        </Variable>\n    </JSONPayload>\n    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n",
        ),
        "AM-Order-Seen": _set_header("AM-Order-Seen", "X-Order-Seen", "yes", "request"),
        "AM-Json-Accept": _set_header("AM-Json-Accept", "X-Json-Accept", "yes", "request"),
        "AM-Not-Options": _set_header("AM-Not-Options", "X-Not-Options", "yes", "response"),
        "AM-Echo": _set_header("AM-Echo", "X-Echo", "{request.header.X-Caller}", "response"),
    }
    items = "".join(f"        <Policy>{p}</Policy>\n" for p in policies)
    proxy = (
        XML_HEAD + '<ProxyEndpoint name="default">\n'
        '    <PreFlow name="PreFlow">\n'
        f"        <Request>{_step('AM-Backend')}{_step('EV-Order')}"
        f"{_step('AM-Order-Seen', 'ext.orderId = &quot;42&quot;')}"
        f"{_step('AM-Json-Accept', 'request.header.Accept = &quot;application/json&quot;')}</Request>\n"
        f"        <Response>{_step('AM-Not-Options', 'request.verb != &quot;OPTIONS&quot;')}"
        f"{_step('AM-Echo')}</Response>\n"
        "    </PreFlow>\n    <Flows/>\n"
        '    <PostFlow name="PostFlow">\n        <Request/>\n        <Response/>\n    </PostFlow>\n'
        "    <HTTPProxyConnection>\n        <BasePath>/cp5</BasePath>\n        <VirtualHost>default</VirtualHost>\n"
        "    </HTTPProxyConnection>\n"
        '    <RouteRule name="default">\n        <TargetEndpoint>default</TargetEndpoint>\n    </RouteRule>\n'
        "</ProxyEndpoint>\n"
    )
    target = (
        XML_HEAD + '<TargetEndpoint name="default">\n'
        '    <PreFlow name="PreFlow">\n        <Request/>\n        <Response/>\n    </PreFlow>\n    <Flows/>\n'
        '    <PostFlow name="PostFlow">\n        <Request/>\n        <Response/>\n    </PostFlow>\n'
        "    <HTTPTargetConnection>\n        <URL>http://{a2mtest.backend}/shop</URL>\n"
        "    </HTTPTargetConnection>\n</TargetEndpoint>\n"
    )
    manifest = (
        XML_HEAD + f'<APIProxy revision="1" name="{APP}">\n    <DisplayName>{APP}</DisplayName>\n'
        f"    <Policies>\n{items}    </Policies>\n"
        "    <ProxyEndpoints>\n        <ProxyEndpoint>default</ProxyEndpoint>\n    </ProxyEndpoints>\n"
        "    <TargetEndpoints>\n        <TargetEndpoint>default</TargetEndpoint>\n    </TargetEndpoints>\n"
        "</APIProxy>\n"
    )
    root = parent / APP
    files = {
        f"apiproxy/{APP}.xml": manifest,
        "apiproxy/proxies/default.xml": proxy,
        "apiproxy/targets/default.xml": target,
        **{f"apiproxy/policies/{name}.xml": text for name, text in policies.items()},
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


# ---------------------------------------------------------------- building and deploying


@dataclass
class Cp5App:
    port: int
    project: Path
    result: Any


def set_listen_port(project: Path, port: int) -> None:
    found = sorted((project / "src" / "main" / "resources").glob("*.properties"))
    assert len(found) == 1, found
    text = found[0].read_text(encoding="utf-8")
    updated, count = re.subn(r"(?m)^http\.listener\.port=.*$", f"http.listener.port={port}", text)
    assert count == 1, text
    found[0].write_text(updated, encoding="utf-8")


@pytest.fixture(scope="module")
def cp5_app(
    mule_runtime: Any, cp5_backend: Backend, free_port: Callable[[], int], tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Cp5App]:
    from a2m.generator import generate_project
    from a2m.parser import read_bundle
    from a2m.verify.mule import BuildError, DeployError, package

    work = tmp_path_factory.mktemp("cp5-runtime")
    bundle = read_bundle(write_cp5_bundle(work / "bundles", cp5_backend.port))
    project = work / APP / "mule-app"
    result = generate_project(bundle, project, shared_flows=())
    port = free_port()
    set_listen_port(project, port)
    try:
        jar = package(project, timeout=BUILD_TIMEOUT)
    except BuildError as exc:
        pytest.fail(f"mvn package failed for {APP}: {exc}\n{exc.output}", pytrace=False)
    try:
        mule_runtime.runner.deploy(Path(jar), app_name=APP, timeout=DEPLOY_TIMEOUT)
    except DeployError as exc:
        pytest.fail(f"{APP} did not deploy: {exc}\n{exc.log_excerpt}", pytrace=False)
    try:
        yield Cp5App(port, project, result)
    finally:
        with contextlib.suppress(Exception):
            mule_runtime.runner.undeploy(APP, timeout=60)


# ---------------------------------------------------------------- runtime cases


@pytest.mark.runtime
def test_CP5_T45_a_response_step_guarded_by_the_request_verb_runs_for_get_and_not_for_options(
    cp5_app: Cp5App, cp5_backend: Backend
) -> None:
    """[CP5-T45] A response step guarded by request.verb != "OPTIONS" runs for GET and not for OPTIONS."""
    records = {str(c.name): c for c in cp5_app.result.conditions}
    assert records["AM-Not-Options"].ok is True, records["AM-Not-Options"].reason

    status, headers, body = call(cp5_app.port, "GET", "/cp5/items/1", headers={"X-Caller": "tester"})
    assert status == 200, body
    assert headers.get("x-not-options") == "yes", headers
    assert headers.get("x-echo") == "tester", headers

    status, headers, body = call(cp5_app.port, "OPTIONS", "/cp5/items/1")
    assert status == 200, body
    assert "x-not-options" not in headers, headers


@pytest.mark.runtime
def test_CP5_T46_a_target_url_with_a_flow_variable_reaches_the_backend_with_the_request_path(
    cp5_app: Cp5App, cp5_backend: Backend
) -> None:
    """[CP5-T46] A target URL with a {variable} sends the call there, with the path below the base path."""
    cp5_backend.clear()

    status, _, body = call(cp5_app.port, "GET", "/cp5/items/1?x=2")

    assert status == 200, body
    assert json.loads(body) == {"backend": "ok"}
    seen = cp5_backend.seen()
    assert len(seen) == 1, seen
    assert (seen[0].method, seen[0].path) == ("GET", "/shop/items/1"), seen[0]


@pytest.mark.runtime
def test_CP5_T47_a_number_extracted_from_json_is_compared_as_text(cp5_app: Cp5App, cp5_backend: Backend) -> None:
    """[CP5-T47] ExtractVariables stores a JSON number as text, so ext.orderId = "42" matches {"id": 42}."""
    json_type = {"Content-Type": "application/json"}
    results: dict[str, str | None] = {}
    for label, payload in (("number", b'{"order":{"id":42}}'), ("other", b'{"order":{"id":43}}')):
        cp5_backend.clear()
        status, _, body = call(cp5_app.port, "POST", "/cp5/orders", headers=json_type, body=payload)
        assert status == 200, (label, body)
        seen = cp5_backend.seen()
        assert len(seen) == 1, (label, seen)
        results[label] = seen[0].headers.get("x-order-seen")

    assert results == {"number": "yes", "other": None}, results


@pytest.mark.runtime
def test_CP5_T48_a_header_condition_compares_the_first_comma_separated_value(
    cp5_app: Cp5App, cp5_backend: Backend
) -> None:
    """[CP5-T48] request.header.Accept = "application/json" matches only when that is the FIRST value."""
    records = {str(c.name): c for c in cp5_app.result.conditions}
    assert records["AM-Json-Accept"].ok is True, records["AM-Json-Accept"].reason

    results: dict[str, str | None] = {}
    for accept in ("application/json, text/plain", "text/plain, application/json", "application/json"):
        cp5_backend.clear()
        status, _, body = call(cp5_app.port, "GET", "/cp5/items/1", headers={"Accept": accept})
        assert status == 200, (accept, body)
        seen = cp5_backend.seen()
        assert len(seen) == 1, (accept, seen)
        results[accept] = seen[0].headers.get("x-json-accept")

    assert results == {
        "application/json, text/plain": "yes",
        "text/plain, application/json": None,
        "application/json": "yes",
    }, results


# ---------------------------------------------------------------- CP5 adversarial round 2
# A second app, 'cp5-r2' (/cp5r2): a request PreFlow VerifyAPIKey VK reading request.header.X-Api-Key, and a
# Flows section holding only a catch-all Flow 'all' whose AM-Flow sets the request header X-Flow. Its target is
# the loopback backend. The allowed keys property (empty by default) is set to good-key, as a user would.

APP_R2 = "cp5-r2"


def write_cp5_r2_bundle(parent: Path, backend_port: int) -> Path:
    policies = {
        "VK": _policy("VerifyAPIKey", "VK", '    <APIKey ref="request.header.X-Api-Key"/>\n'),
        "AM-Flow": _set_header("AM-Flow", "X-Flow", "yes", "request"),
    }
    items = "".join(f"        <Policy>{p}</Policy>\n" for p in policies)
    proxy = (
        XML_HEAD + '<ProxyEndpoint name="default">\n'
        f'    <PreFlow name="PreFlow">\n        <Request>{_step("VK")}</Request>\n        <Response/>\n'
        "    </PreFlow>\n"
        f'    <Flows><Flow name="all"><Request>{_step("AM-Flow")}</Request><Response/></Flow></Flows>\n'
        '    <PostFlow name="PostFlow">\n        <Request/>\n        <Response/>\n    </PostFlow>\n'
        "    <HTTPProxyConnection>\n        <BasePath>/cp5r2</BasePath>\n        <VirtualHost>default</VirtualHost>\n"
        "    </HTTPProxyConnection>\n"
        '    <RouteRule name="default">\n        <TargetEndpoint>default</TargetEndpoint>\n    </RouteRule>\n'
        "</ProxyEndpoint>\n"
    )
    target = (
        XML_HEAD + '<TargetEndpoint name="default">\n'
        '    <PreFlow name="PreFlow">\n        <Request/>\n        <Response/>\n    </PreFlow>\n    <Flows/>\n'
        '    <PostFlow name="PostFlow">\n        <Request/>\n        <Response/>\n    </PostFlow>\n'
        f"    <HTTPTargetConnection>\n        <URL>http://127.0.0.1:{backend_port}/r2</URL>\n"
        "    </HTTPTargetConnection>\n</TargetEndpoint>\n"
    )
    manifest = (
        XML_HEAD + f'<APIProxy revision="1" name="{APP_R2}">\n    <DisplayName>{APP_R2}</DisplayName>\n'
        f"    <Policies>\n{items}    </Policies>\n"
        "    <ProxyEndpoints>\n        <ProxyEndpoint>default</ProxyEndpoint>\n    </ProxyEndpoints>\n"
        "    <TargetEndpoints>\n        <TargetEndpoint>default</TargetEndpoint>\n    </TargetEndpoints>\n"
        "</APIProxy>\n"
    )
    root = parent / APP_R2
    files = {
        f"apiproxy/{APP_R2}.xml": manifest,
        "apiproxy/proxies/default.xml": proxy,
        "apiproxy/targets/default.xml": target,
        **{f"apiproxy/policies/{name}.xml": text for name, text in policies.items()},
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def set_property(project: Path, key: str, value: str) -> None:
    found = sorted((project / "src" / "main" / "resources").glob("*.properties"))
    assert len(found) == 1, found
    text = found[0].read_text(encoding="utf-8")
    updated, count = re.subn(rf"(?m)^{re.escape(key)}=.*$", f"{key}={value}", text)
    assert count == 1, text
    found[0].write_text(updated, encoding="utf-8")


@pytest.fixture(scope="module")
def cp5_r2_app(
    mule_runtime: Any, cp5_backend: Backend, free_port: Callable[[], int], tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Cp5App]:
    from a2m.generator import generate_project
    from a2m.parser import read_bundle
    from a2m.verify.mule import BuildError, DeployError, package

    work = tmp_path_factory.mktemp("cp5-r2")
    bundle = read_bundle(write_cp5_r2_bundle(work / "bundles", cp5_backend.port))
    project = work / APP_R2 / "mule-app"
    result = generate_project(bundle, project, shared_flows=())
    port = free_port()
    set_listen_port(project, port)
    set_property(project, "verifyapikey.VK.allowedKeys", "good-key")
    try:
        jar = package(project, timeout=BUILD_TIMEOUT)
    except BuildError as exc:
        pytest.fail(f"mvn package failed for {APP_R2}: {exc}\n{exc.output}", pytrace=False)
    try:
        mule_runtime.runner.deploy(Path(jar), app_name=APP_R2, timeout=DEPLOY_TIMEOUT)
    except DeployError as exc:
        pytest.fail(f"{APP_R2} did not deploy: {exc}\n{exc.log_excerpt}", pytrace=False)
    try:
        yield Cp5App(port, project, result)
    finally:
        with contextlib.suppress(Exception):
            mule_runtime.runner.undeploy(APP_R2, timeout=60)


@pytest.mark.runtime
def test_CP5_T53_an_app_whose_flows_are_only_a_catch_all_deploys_and_runs_that_flow(
    cp5_r2_app: Cp5App, cp5_backend: Backend
) -> None:
    """[CP5-T53] Flows holding only a catch-all Flow deploy, and the catch-all's step runs on every call."""
    cp5_backend.clear()
    status, _, body = call(cp5_r2_app.port, "GET", "/cp5r2/items/7", headers={"X-Api-Key": "good-key"})
    assert status == 200, body
    seen = cp5_backend.seen()
    assert len(seen) == 1, seen
    assert (seen[0].path, seen[0].headers.get("x-flow")) == ("/r2/items/7", "yes"), seen[0]


@pytest.mark.runtime
def test_CP5_T54_verifyapikey_reads_the_first_comma_separated_value_of_the_key_header(
    cp5_r2_app: Cp5App, cp5_backend: Backend
) -> None:
    """[CP5-T54] VerifyAPIKey reads request.header.X-Api-Key as conditions do: its first comma-separated value."""
    results: dict[str, int] = {}
    for key in ("good-key, extra", "good-key", "extra, good-key", None):
        headers = {} if key is None else {"X-Api-Key": key}
        status, _, _ = call(cp5_r2_app.port, "GET", "/cp5r2/items/1", headers=headers)
        results[str(key)] = status

    assert results == {"good-key, extra": 200, "good-key": 200, "extra, good-key": 401, "None": 401}, results


# ---------------------------------------------------------------- CP5 adversarial round 3
# A third app, 'cp5-r3' (/cp5r3), request PreFlow:
# * AM-Corr sets the request header X-Corr to {request.header.X-Correlation-ID:unknown} (a template default);
# * AM-Set's AssignVariable risk.score has the Value {system.timestamp}, which a2m refuses, and RF-Guard (a 403
#   RaiseFault) is guarded by risk.score != null: the reviewer's case, now can't translate;
# * AM-Tier sets tier to gold (a write a2m makes exactly) and AM-Mark (condition tier = "gold") sets X-Tier;
# * AM-Star (condition request.header.X-Code Matches "abc%*") sets X-Star: %* is a literal '*'.

APP_R3 = "cp5-r3"


def _assign_variable(name: str, variable: str, value: str) -> str:
    return _policy(
        "AssignMessage",
        name,
        f"    <AssignVariable>\n        <Name>{variable}</Name>\n        <Value>{value}</Value>\n"
        "    </AssignVariable>\n    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n",
    )


def write_cp5_r3_bundle(parent: Path, backend_port: int) -> Path:
    policies = {
        "AM-Corr": _set_header("AM-Corr", "X-Corr", "{request.header.X-Correlation-ID:unknown}", "request"),
        "AM-Set": _assign_variable("AM-Set", "risk.score", "{system.timestamp}"),
        "RF-Guard": _policy(
            "RaiseFault",
            "RF-Guard",
            "    <FaultResponse>\n        <Set>\n            <StatusCode>403</StatusCode>\n"
            "            <ReasonPhrase>Forbidden</ReasonPhrase>\n        </Set>\n    </FaultResponse>\n"
            "    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n",
        ),
        "AM-Tier": _assign_variable("AM-Tier", "tier", "gold"),
        "AM-Mark": _set_header("AM-Mark", "X-Tier", "yes", "request"),
        "AM-Star": _set_header("AM-Star", "X-Star", "yes", "request"),
    }
    steps = (
        _step("AM-Corr")
        + _step("AM-Set")
        + _step("RF-Guard", "risk.score != null")
        + _step("AM-Tier")
        + _step("AM-Mark", "tier = &quot;gold&quot;")
        + _step("AM-Star", "request.header.X-Code Matches &quot;abc%*&quot;")
    )
    items = "".join(f"        <Policy>{p}</Policy>\n" for p in policies)
    proxy = (
        XML_HEAD + '<ProxyEndpoint name="default">\n'
        f'    <PreFlow name="PreFlow">\n        <Request>{steps}</Request>\n        <Response/>\n'
        "    </PreFlow>\n    <Flows/>\n"
        '    <PostFlow name="PostFlow">\n        <Request/>\n        <Response/>\n    </PostFlow>\n'
        "    <HTTPProxyConnection>\n        <BasePath>/cp5r3</BasePath>\n        <VirtualHost>default</VirtualHost>\n"
        "    </HTTPProxyConnection>\n"
        '    <RouteRule name="default">\n        <TargetEndpoint>default</TargetEndpoint>\n    </RouteRule>\n'
        "</ProxyEndpoint>\n"
    )
    target = (
        XML_HEAD + '<TargetEndpoint name="default">\n'
        '    <PreFlow name="PreFlow">\n        <Request/>\n        <Response/>\n    </PreFlow>\n    <Flows/>\n'
        '    <PostFlow name="PostFlow">\n        <Request/>\n        <Response/>\n    </PostFlow>\n'
        f"    <HTTPTargetConnection>\n        <URL>http://127.0.0.1:{backend_port}/r3</URL>\n"
        "    </HTTPTargetConnection>\n</TargetEndpoint>\n"
    )
    manifest = (
        XML_HEAD + f'<APIProxy revision="1" name="{APP_R3}">\n    <DisplayName>{APP_R3}</DisplayName>\n'
        f"    <Policies>\n{items}    </Policies>\n"
        "    <ProxyEndpoints>\n        <ProxyEndpoint>default</ProxyEndpoint>\n    </ProxyEndpoints>\n"
        "    <TargetEndpoints>\n        <TargetEndpoint>default</TargetEndpoint>\n    </TargetEndpoints>\n"
        "</APIProxy>\n"
    )
    root = parent / APP_R3
    files = {
        f"apiproxy/{APP_R3}.xml": manifest,
        "apiproxy/proxies/default.xml": proxy,
        "apiproxy/targets/default.xml": target,
        **{f"apiproxy/policies/{name}.xml": text for name, text in policies.items()},
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


@pytest.fixture(scope="module")
def cp5_r3_app(
    mule_runtime: Any, cp5_backend: Backend, free_port: Callable[[], int], tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Cp5App]:
    from a2m.generator import generate_project
    from a2m.parser import read_bundle
    from a2m.verify.mule import BuildError, DeployError, package

    work = tmp_path_factory.mktemp("cp5-r3")
    bundle = read_bundle(write_cp5_r3_bundle(work / "bundles", cp5_backend.port))
    project = work / APP_R3 / "mule-app"
    result = generate_project(bundle, project, shared_flows=())
    port = free_port()
    set_listen_port(project, port)
    try:
        jar = package(project, timeout=BUILD_TIMEOUT)
    except BuildError as exc:
        pytest.fail(f"mvn package failed for {APP_R3}: {exc}\n{exc.output}", pytrace=False)
    try:
        mule_runtime.runner.deploy(Path(jar), app_name=APP_R3, timeout=DEPLOY_TIMEOUT)
    except DeployError as exc:
        pytest.fail(f"{APP_R3} did not deploy: {exc}\n{exc.log_excerpt}", pytrace=False)
    try:
        yield Cp5App(port, project, result)
    finally:
        with contextlib.suppress(Exception):
            mule_runtime.runner.undeploy(APP_R3, timeout=60)


def _r3_backend_header(app: Cp5App, backend: Backend, header: str, headers: dict[str, str]) -> str | None:
    backend.clear()
    status, _, body = call(app.port, "GET", "/cp5r3/items", headers=headers)
    assert status == 200, (headers, status, body)
    seen = backend.seen()
    assert len(seen) == 1, seen
    return seen[0].headers.get(header)


@pytest.mark.runtime
def test_CP5_T58_a_template_default_is_sent_when_the_header_is_missing(
    cp5_r3_app: Cp5App, cp5_backend: Backend
) -> None:
    """[CP5-T58] {request.header.X-Correlation-ID:unknown} sends the header's value, or unknown without it."""
    results = {
        "abc": _r3_backend_header(cp5_r3_app, cp5_backend, "x-corr", {"X-Correlation-ID": "abc"}),
        "missing": _r3_backend_header(cp5_r3_app, cp5_backend, "x-corr", {}),
    }
    assert results == {"abc": "abc", "missing": "unknown"}, results


@pytest.mark.runtime
def test_CP5_T59_a_read_of_a_variable_whose_write_was_refused_is_cant_translate_and_never_runs_unguarded(
    cp5_r3_app: Cp5App, cp5_backend: Backend
) -> None:
    """[CP5-T59] The reviewer's AssignVariable/RaiseFault case: RF-Guard's condition is reported can't translate,
    naming AM-Set, and the step never runs; a variable a2m writes exactly is still read (X-Tier reaches the
    backend)."""
    guard = [c for c in cp5_r3_app.result.conditions if str(c.name) == "RF-Guard"]
    assert len(guard) == 1 and guard[0].ok is False, guard
    assert "AM-Set" in str(guard[0].reason) and "risk.score" in str(guard[0].reason), guard[0].reason
    mark = [c for c in cp5_r3_app.result.conditions if str(c.name) == "AM-Mark"]
    assert len(mark) == 1 and mark[0].ok is True, mark
    import xml.etree.ElementTree as ET

    doc_name = "{http://www.mulesoft.org/schema/mule/documentation}name"
    roots = [ET.parse(p).getroot() for p in sorted((cp5_r3_app.project / "src" / "main" / "mule").glob("*.xml"))]
    whens = [w for root in roots for w in root.iter() if any(c.get(doc_name) == "RF-Guard" for c in w)]
    assert [w.get("expression") for w in whens] == ["#[false]"], [w.attrib for w in whens]
    expressions = [v for root in roots for e in root.iter() for k, v in e.attrib.items() if not k.startswith("{")]
    assert not any("vars['risk.score']" in v for v in expressions), "risk.score was read as a missing value"
    # Every call passes RF-Guard (it never runs) and reaches the backend with AM-Mark's header.
    assert _r3_backend_header(cp5_r3_app, cp5_backend, "x-tier", {}) == "yes"


@pytest.mark.runtime
def test_CP5_T60_matches_with_an_escaped_asterisk_matches_only_the_literal_asterisk(
    cp5_r3_app: Cp5App, cp5_backend: Backend
) -> None:
    """[CP5-T60] request.header.X-Code Matches "abc%*" is true for abc* and false for abcd and abc%d."""
    results = {
        code: _r3_backend_header(cp5_r3_app, cp5_backend, "x-star", {"X-Code": code})
        for code in ("abc*", "abcd", "abc%d", "abc")
    }
    assert results == {"abc*": "yes", "abcd": None, "abc%d": None, "abc": None}, results


# ---------------------------------------------------------------- CP5 adversarial round 4
# A fourth app, 'cp5-r4' (/cp5r4):
# * Flow risky (condition client.ip = "10.0.0.1": client.ip has no mapping, so a #[false] branch) has AM-Gold,
#   which sets tier to gold exactly; PostFlow AM-Not-Gold (condition tier != "gold") sets X-Not-Gold. Read as
#   vars['tier'] it would always be true in the app, though Apigee skips it whenever the Flow ran;
# * PostFlow JS-Tier is a JavaScript a2m skips (in Apigee it may set request.header.X-Tier), and AM-Hdr
#   (condition request.header.X-Tier != "gold") sets X-Hdr: read from the caller's request it would run for
#   callers that send no X-Tier, though Apigee's script may have set it;
# * AM-Always sets X-Always, so every call is seen to reach the backend.

APP_R4 = "cp5-r4"


def write_cp5_r4_bundle(parent: Path, backend_port: int) -> Path:
    policies = {
        "AM-Gold": _assign_variable("AM-Gold", "tier", "gold"),
        "AM-Not-Gold": _set_header("AM-Not-Gold", "X-Not-Gold", "yes", "request"),
        "JS-Tier": _policy("Javascript", "JS-Tier", "    <ResourceURL>jsc://tier.js</ResourceURL>\n"),
        "AM-Hdr": _set_header("AM-Hdr", "X-Hdr", "yes", "request"),
        "AM-Always": _set_header("AM-Always", "X-Always", "yes", "request"),
    }
    flow = (
        f'<Flow name="risky"><Request>{_step("AM-Gold")}</Request><Response/>'
        "<Condition>client.ip = &quot;10.0.0.1&quot;</Condition></Flow>"
    )
    post = (
        _step("AM-Not-Gold", "tier != &quot;gold&quot;")
        + _step("JS-Tier")
        + _step("AM-Hdr", "request.header.X-Tier != &quot;gold&quot;")
        + _step("AM-Always")
    )
    items = "".join(f"        <Policy>{p}</Policy>\n" for p in policies)
    proxy = (
        XML_HEAD + '<ProxyEndpoint name="default">\n'
        '    <PreFlow name="PreFlow">\n        <Request/>\n        <Response/>\n    </PreFlow>\n'
        f"    <Flows>{flow}</Flows>\n"
        f'    <PostFlow name="PostFlow">\n        <Request>{post}</Request>\n        <Response/>\n    </PostFlow>\n'
        "    <HTTPProxyConnection>\n        <BasePath>/cp5r4</BasePath>\n        <VirtualHost>default</VirtualHost>\n"
        "    </HTTPProxyConnection>\n"
        '    <RouteRule name="default">\n        <TargetEndpoint>default</TargetEndpoint>\n    </RouteRule>\n'
        "</ProxyEndpoint>\n"
    )
    target = (
        XML_HEAD + '<TargetEndpoint name="default">\n'
        '    <PreFlow name="PreFlow">\n        <Request/>\n        <Response/>\n    </PreFlow>\n    <Flows/>\n'
        '    <PostFlow name="PostFlow">\n        <Request/>\n        <Response/>\n    </PostFlow>\n'
        f"    <HTTPTargetConnection>\n        <URL>http://127.0.0.1:{backend_port}/r4</URL>\n"
        "    </HTTPTargetConnection>\n</TargetEndpoint>\n"
    )
    manifest = (
        XML_HEAD + f'<APIProxy revision="1" name="{APP_R4}">\n    <DisplayName>{APP_R4}</DisplayName>\n'
        f"    <Policies>\n{items}    </Policies>\n"
        "    <ProxyEndpoints>\n        <ProxyEndpoint>default</ProxyEndpoint>\n    </ProxyEndpoints>\n"
        "    <TargetEndpoints>\n        <TargetEndpoint>default</TargetEndpoint>\n    </TargetEndpoints>\n"
        "</APIProxy>\n"
    )
    root = parent / APP_R4
    files = {
        f"apiproxy/{APP_R4}.xml": manifest,
        "apiproxy/proxies/default.xml": proxy,
        "apiproxy/targets/default.xml": target,
        **{f"apiproxy/policies/{name}.xml": text for name, text in policies.items()},
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


@pytest.fixture(scope="module")
def cp5_r4_app(
    mule_runtime: Any, cp5_backend: Backend, free_port: Callable[[], int], tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Cp5App]:
    from a2m.generator import generate_project
    from a2m.parser import read_bundle
    from a2m.verify.mule import BuildError, DeployError, package

    work = tmp_path_factory.mktemp("cp5-r4")
    bundle = read_bundle(write_cp5_r4_bundle(work / "bundles", cp5_backend.port))
    project = work / APP_R4 / "mule-app"
    result = generate_project(bundle, project, shared_flows=())
    port = free_port()
    set_listen_port(project, port)
    try:
        jar = package(project, timeout=BUILD_TIMEOUT)
    except BuildError as exc:
        pytest.fail(f"mvn package failed for {APP_R4}: {exc}\n{exc.output}", pytrace=False)
    try:
        mule_runtime.runner.deploy(Path(jar), app_name=APP_R4, timeout=DEPLOY_TIMEOUT)
    except DeployError as exc:
        pytest.fail(f"{APP_R4} did not deploy: {exc}\n{exc.log_excerpt}", pytrace=False)
    try:
        yield Cp5App(port, project, result)
    finally:
        with contextlib.suppress(Exception):
            mule_runtime.runner.undeploy(APP_R4, timeout=60)


@pytest.mark.runtime
def test_CP5_T66_a_read_of_a_write_in_a_dead_flow_or_a_skipped_script_never_runs_unguarded(
    cp5_r4_app: Cp5App, cp5_backend: Backend
) -> None:
    """[CP5-T66] AM-Not-Gold (tier != "gold", tier written only in the #[false] Flow risky) and AM-Hdr
    (request.header.X-Tier != "gold", after the skipped JS-Tier) are can't translate, naming the writer, and
    never run in the deployed app: the backend gets X-Always but neither X-Not-Gold nor X-Hdr."""
    records = {str(c.name): c for c in cp5_r4_app.result.conditions}
    assert records["risky"].ok is False, records["risky"]
    for name, writer, needle in (("AM-Not-Gold", "AM-Gold", "Flow risky"), ("AM-Hdr", "JS-Tier", "X-Tier")):
        assert records[name].ok is False, records[name]
        assert writer in str(records[name].reason) and needle in str(records[name].reason), records[name].reason
    for headers in ({}, {"X-Tier": "silver"}):
        cp5_backend.clear()
        status, _, body = call(cp5_r4_app.port, "GET", "/cp5r4/items", headers=headers)
        assert status == 200, (headers, status, body)
        seen = cp5_backend.seen()
        assert len(seen) == 1, seen
        got = {h: seen[0].headers.get(h) for h in ("x-always", "x-not-gold", "x-hdr")}
        assert got == {"x-always": "yes", "x-not-gold": None, "x-hdr": None}, (headers, got)


# ---------------------------------------------------------------- CP5 adversarial round 7
# A fifth app, 'cp5-r7' (/cp5r7), with its own loopback backend that answers Content-Type: application/json and
# X-Multi: a, b.
# * request PreFlow: AM-Default sets tier=free, qv=dq, pv=dp, tok=dt, jv=dj; ExtractVariables then overrides each
#   from header X-Tier ({tier}), query parameter q ({qv}), the URI path (/items/{pv}), header Authorization
#   (Bearer {tok}) and the JSON body ($.j into jv). AM-Vals sends them to the backend as X-Vals, and RF-Free (a 403
#   RaiseFault) runs under tier = "free". In Apigee an extraction that finds nothing leaves the default.
# * response PreFlow: EV-Ct extracts Content-Type ({ct}) and X-Multi ({mv}) from the response; AM-Ct2 sets ct2 and
#   mv2 by Ref to response.header.Content-Type and response.header.X-Multi; AM-Show sets response headers from
#   templates; AM-Json (response.header.Content-Type = "application/json") sets X-Ct-Json; AM-Body (caller sent
#   X-Body: 1) sets a text/plain payload, and AM-Text (response.header.Content-Type = "text/plain") sets X-Ct-Text.

APP_R7 = "cp5-r7"
R7_BODY = b'{"r7":"ok"}'


@pytest.fixture(scope="module")
def cp5_r7_backend() -> Iterator[Backend]:
    state = Backend(port=0)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            with state.lock:
                state.records.append(
                    Seen(self.command, urlsplit(self.path).path, {k.lower(): v for k, v in self.headers.items()})
                )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("X-Multi", "a, b")
            self.send_header("Content-Length", str(len(R7_BODY)))
            self.end_headers()
            self.wfile.write(R7_BODY)

        do_GET = do_POST = _handle

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    state.port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


def _r7_extract(name: str, source: str, body: str) -> str:
    return _policy(
        "ExtractVariables",
        name,
        f"    <Source>{source}</Source>\n{body}    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n",
    )


def _r7_headers(name: str, headers: list[tuple[str, str]], kind: str) -> str:
    rows = "".join(f'            <Header name="{h}">{v}</Header>\n' for h, v in headers)
    return _policy(
        "AssignMessage",
        name,
        f"    <Set>\n        <Headers>\n{rows}        </Headers>\n    </Set>\n"
        "    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n"
        f'    <AssignTo createNew="false" transport="http" type="{kind}"/>\n',
    )


def _r7_assign(name: str, pairs: list[tuple[str, str]]) -> str:
    rows = "".join(f"    <AssignVariable>\n        <Name>{n}</Name>\n        {inner}\n    </AssignVariable>\n" for n, inner in pairs)
    return _policy("AssignMessage", name, rows + "    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n")


def write_cp5_r7_bundle(parent: Path, backend_port: int) -> Path:
    defaults = [("tier", "free"), ("qv", "dq"), ("pv", "dp"), ("tok", "dt"), ("jv", "dj")]
    policies = {
        "AM-Default": _r7_assign("AM-Default", [(n, f"<Value>{v}</Value>") for n, v in defaults]),
        "EV-Tier": _r7_extract(
            "EV-Tier", "request", '    <Header name="X-Tier">\n        <Pattern>{tier}</Pattern>\n    </Header>\n'
        ),
        "EV-Query": _r7_extract(
            "EV-Query", "request", '    <QueryParam name="q">\n        <Pattern>{qv}</Pattern>\n    </QueryParam>\n'
        ),
        "EV-Path": _r7_extract("EV-Path", "request", "    <URIPath>\n        <Pattern>/items/{pv}</Pattern>\n    </URIPath>\n"),
        "EV-Tok": _r7_extract(
            "EV-Tok",
            "request",
            '    <Header name="Authorization">\n        <Pattern>Bearer {tok}</Pattern>\n    </Header>\n',
        ),
        "EV-Json": _r7_extract(
            "EV-Json",
            "request",
            '    <JSONPayload>\n        <Variable name="jv">\n            <JSONPath>$.j</JSONPath>\n'
            "        </Variable>\n    </JSONPayload>\n",
        ),
        "AM-Vals": _r7_headers("AM-Vals", [("X-Vals", "{tier}|{qv}|{pv}|{tok}|{jv}")], "request"),
        "RF-Free": _policy(
            "RaiseFault",
            "RF-Free",
            "    <FaultResponse>\n        <Set>\n            <StatusCode>403</StatusCode>\n"
            "            <ReasonPhrase>Forbidden</ReasonPhrase>\n        </Set>\n    </FaultResponse>\n"
            "    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n",
        ),
        "EV-Ct": _r7_extract(
            "EV-Ct",
            "response",
            '    <Header name="Content-Type">\n        <Pattern>{ct}</Pattern>\n    </Header>\n'
            '    <Header name="X-Multi">\n        <Pattern>{mv}</Pattern>\n    </Header>\n',
        ),
        "AM-Ct2": _r7_assign(
            "AM-Ct2", [("ct2", "<Ref>response.header.Content-Type</Ref>"), ("mv2", "<Ref>response.header.X-Multi</Ref>")]
        ),
        "AM-Show": _r7_headers(
            "AM-Show",
            [
                ("X-Ct", "{response.header.Content-Type}"),
                ("X-Ct-Ev", "{ct}"),
                ("X-Ct2", "{ct2}"),
                ("X-Mv", "{mv}|{mv2}|{response.header.X-Multi}"),
            ],
            "response",
        ),
        "AM-Json": _r7_headers("AM-Json", [("X-Ct-Json", "yes")], "response"),
        "AM-Body": _policy(
            "AssignMessage",
            "AM-Body",
            '    <Set>\n        <Payload contentType="text/plain">hi</Payload>\n    </Set>\n'
            "    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n"
            '    <AssignTo createNew="false" transport="http" type="response"/>\n',
        ),
        "AM-Text": _r7_headers("AM-Text", [("X-Ct-Text", "yes")], "response"),
    }
    request = (
        "".join(_step(n) for n in ("AM-Default", "EV-Tier", "EV-Query", "EV-Path", "EV-Tok", "EV-Json", "AM-Vals"))
        + _step("RF-Free", "tier = &quot;free&quot;")
    )
    response = (
        _step("EV-Ct")
        + _step("AM-Ct2")
        + _step("AM-Show")
        + _step("AM-Json", "response.header.Content-Type = &quot;application/json&quot;")
        + _step("AM-Body", "request.header.X-Body = &quot;1&quot;")
        + _step("AM-Text", "response.header.Content-Type = &quot;text/plain&quot;")
    )
    items = "".join(f"        <Policy>{p}</Policy>\n" for p in policies)
    proxy = (
        XML_HEAD + '<ProxyEndpoint name="default">\n'
        f'    <PreFlow name="PreFlow">\n        <Request>{request}</Request>\n'
        f"        <Response>{response}</Response>\n    </PreFlow>\n    <Flows/>\n"
        '    <PostFlow name="PostFlow">\n        <Request/>\n        <Response/>\n    </PostFlow>\n'
        "    <HTTPProxyConnection>\n        <BasePath>/cp5r7</BasePath>\n        <VirtualHost>default</VirtualHost>\n"
        "    </HTTPProxyConnection>\n"
        '    <RouteRule name="default">\n        <TargetEndpoint>default</TargetEndpoint>\n    </RouteRule>\n'
        "</ProxyEndpoint>\n"
    )
    target = (
        XML_HEAD + '<TargetEndpoint name="default">\n'
        '    <PreFlow name="PreFlow">\n        <Request/>\n        <Response/>\n    </PreFlow>\n    <Flows/>\n'
        '    <PostFlow name="PostFlow">\n        <Request/>\n        <Response/>\n    </PostFlow>\n'
        f"    <HTTPTargetConnection>\n        <URL>http://127.0.0.1:{backend_port}/r7</URL>\n"
        "    </HTTPTargetConnection>\n</TargetEndpoint>\n"
    )
    manifest = (
        XML_HEAD + f'<APIProxy revision="1" name="{APP_R7}">\n    <DisplayName>{APP_R7}</DisplayName>\n'
        f"    <Policies>\n{items}    </Policies>\n"
        "    <ProxyEndpoints>\n        <ProxyEndpoint>default</ProxyEndpoint>\n    </ProxyEndpoints>\n"
        "    <TargetEndpoints>\n        <TargetEndpoint>default</TargetEndpoint>\n    </TargetEndpoints>\n"
        "</APIProxy>\n"
    )
    root = parent / APP_R7
    files = {
        f"apiproxy/{APP_R7}.xml": manifest,
        "apiproxy/proxies/default.xml": proxy,
        "apiproxy/targets/default.xml": target,
        **{f"apiproxy/policies/{name}.xml": text for name, text in policies.items()},
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


@pytest.fixture(scope="module")
def cp5_r7_app(
    mule_runtime: Any, cp5_r7_backend: Backend, free_port: Callable[[], int], tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Cp5App]:
    from a2m.generator import generate_project
    from a2m.parser import read_bundle
    from a2m.verify.mule import BuildError, DeployError, package

    work = tmp_path_factory.mktemp("cp5-r7")
    bundle = read_bundle(write_cp5_r7_bundle(work / "bundles", cp5_r7_backend.port))
    project = work / APP_R7 / "mule-app"
    result = generate_project(bundle, project, shared_flows=())
    port = free_port()
    set_listen_port(project, port)
    try:
        jar = package(project, timeout=BUILD_TIMEOUT)
    except BuildError as exc:
        pytest.fail(f"mvn package failed for {APP_R7}: {exc}\n{exc.output}", pytrace=False)
    try:
        mule_runtime.runner.deploy(Path(jar), app_name=APP_R7, timeout=DEPLOY_TIMEOUT)
    except DeployError as exc:
        pytest.fail(f"{APP_R7} did not deploy: {exc}\n{exc.log_excerpt}", pytrace=False)
    try:
        yield Cp5App(port, project, result)
    finally:
        with contextlib.suppress(Exception):
            mule_runtime.runner.undeploy(APP_R7, timeout=60)


def _r7_call(app: Cp5App, path: str, headers: dict[str, str], body: bytes) -> tuple[int, dict[str, str], bytes]:
    return call(app.port, "POST", path, headers={"Content-Type": "application/json", **headers}, body=body)


@pytest.mark.runtime
def test_CP5_T69_a_default_survives_an_extraction_that_finds_nothing_so_the_condition_on_it_fires(
    cp5_r7_app: Cp5App, cp5_r7_backend: Backend
) -> None:
    """[CP5-T69] The reviewers' repro: tier = free, then ExtractVariables {tier} from X-Tier, then RF-Free under
    tier = "free". Without X-Tier Apigee keeps free and answers 403 without calling the backend; with X-Tier: gold
    the call goes through."""
    assert all(c.ok for c in cp5_r7_app.result.conditions), [c for c in cp5_r7_app.result.conditions if not c.ok]
    results: dict[str, tuple[int, int]] = {}
    for label, headers in (("missing", {}), ("gold", {"X-Tier": "gold"}), ("free", {"X-Tier": "free"})):
        cp5_r7_backend.clear()
        status, _, _ = _r7_call(cp5_r7_app, "/cp5r7/items/abc", headers, b'{"j":"jj"}')
        results[label] = (status, len(cp5_r7_backend.seen()))
    assert results == {"missing": (403, 0), "gold": (200, 1), "free": (403, 0)}, results


@pytest.mark.runtime
def test_CP5_T69_every_extraction_source_that_finds_nothing_keeps_the_earlier_value(
    cp5_r7_app: Cp5App, cp5_r7_backend: Backend
) -> None:
    """[CP5-T69] Header, query parameter, URI path, a header pattern that does not match and a missing JSON value:
    each extraction that finds nothing keeps AM-Default's value, and each that finds one replaces it."""
    found: dict[str, str | None] = {}
    for label, path, headers, body in (
        ("found", "/cp5r7/items/abc?q=qq", {"X-Tier": "gold", "Authorization": "Bearer t1"}, b'{"j":"jj"}'),
        ("nothing", "/cp5r7/other", {"X-Tier": "gold", "Authorization": "Basic x"}, b'{"k":1}'),
        ("no-auth", "/cp5r7/items", {"X-Tier": "gold"}, b'{"k":2}'),
    ):
        cp5_r7_backend.clear()
        status, _, reply = _r7_call(cp5_r7_app, path, headers, body)
        assert status == 200, (label, status, reply)
        seen = cp5_r7_backend.seen()
        assert len(seen) == 1, (label, seen)
        found[label] = seen[0].headers.get("x-vals")
    assert found == {"found": "gold|qq|abc|t1|jj", "nothing": "gold|dq|dp|dt|dj", "no-auth": "gold|dq|dp|dt|dj"}, found


@pytest.mark.runtime
def test_CP5_T69_response_header_reads_after_the_target_call_see_the_target_headers(
    cp5_r7_app: Cp5App, cp5_r7_backend: Backend
) -> None:
    """[CP5-T69] After the target call, response.header.Content-Type reads application/json in ExtractVariables, an
    AssignVariable Ref, a template and a condition; response.header.X-Multi (a, b) reads its first value, a, in all
    of them; the caller still gets the target's Content-Type."""
    status, headers, body = _r7_call(cp5_r7_app, "/cp5r7/items/abc", {"X-Tier": "gold"}, b"{}")
    assert status == 200 and body == R7_BODY, (status, body)
    shown = {h: headers.get(h) for h in ("x-ct", "x-ct-ev", "x-ct2", "x-mv", "x-ct-json", "x-ct-text")}
    assert shown == {
        "x-ct": "application/json",
        "x-ct-ev": "application/json",
        "x-ct2": "application/json",
        "x-mv": "a|a|a",
        "x-ct-json": "yes",
        "x-ct-text": None,
    }, shown
    assert headers.get("content-type", "").split(";")[0].strip() == "application/json", headers


@pytest.mark.runtime
def test_CP5_T69_a_response_payload_content_type_is_sent_and_read_back(
    cp5_r7_app: Cp5App, cp5_r7_backend: Backend
) -> None:
    """[CP5-T69] AM-Body sets a text/plain payload on the response: the caller gets text/plain (not the target's
    application/json) and the later condition response.header.Content-Type = "text/plain" fires."""
    status, headers, body = _r7_call(cp5_r7_app, "/cp5r7/items/abc", {"X-Tier": "gold", "X-Body": "1"}, b"{}")
    assert (status, body) == (200, b"hi"), (status, body)
    assert headers.get("content-type", "").split(";")[0].strip() == "text/plain", headers
    assert headers.get("x-ct-text") == "yes", headers


# ---------------------------------------------------------------- CP5 adversarial round 8
# H1: URIPath matches Apigee's proxy.pathsuffix ('' for the bare base path), the same suffix the generator forwards.
# B1: a JSONPath step is applied only to the JSON type it fits (a property step to an object, an index to an array);
# anything else finds nothing and the variable keeps its earlier value, as in Apigee.

APP_R8 = "cp5-r8"


def write_cp5_r8_bundle(parent: Path, backend_port: int) -> Path:
    defaults = [("pid", "dp"), ("ja", "dja"), ("jn", "djn"), ("jo", "djo"), ("ji", "dji"), ("jx", "djx")]
    json_paths = [("ja", "$.j"), ("jn", "$.items.id"), ("jo", "$[0]"), ("ji", "$[0].j"), ("jx", "$.items[1].id")]
    json_vars = "".join(
        f'        <Variable name="{n}">\n            <JSONPath>{p}</JSONPath>\n        </Variable>\n' for n, p in json_paths
    )
    policies = {
        "AM-Default": _r7_assign("AM-Default", [(n, f"<Value>{v}</Value>") for n, v in defaults]),
        "EV-Path": _r7_extract("EV-Path", "request", "    <URIPath>\n        <Pattern>/{pid}</Pattern>\n    </URIPath>\n"),
        "EV-Json": _r7_extract("EV-Json", "request", f"    <JSONPayload>\n{json_vars}    </JSONPayload>\n"),
        "AM-Vals": _r7_headers("AM-Vals", [("X-Vals", "{pid}|{ja}|{jn}|{jo}|{ji}|{jx}")], "request"),
        # Never fires in these tests; a2m's generated app needs a RaiseFault to declare its fault error type.
        "RF-Never": _policy(
            "RaiseFault",
            "RF-Never",
            "    <FaultResponse>\n        <Set>\n            <StatusCode>418</StatusCode>\n"
            "        </Set>\n    </FaultResponse>\n"
            "    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n",
        ),
    }
    request = "".join(_step(n) for n in ("AM-Default", "EV-Path", "EV-Json", "AM-Vals")) + _step(
        "RF-Never", "request.header.X-Never = &quot;1&quot;"
    )
    items = "".join(f"        <Policy>{p}</Policy>\n" for p in policies)
    proxy = (
        XML_HEAD + '<ProxyEndpoint name="default">\n'
        f'    <PreFlow name="PreFlow">\n        <Request>{request}</Request>\n'
        "        <Response/>\n    </PreFlow>\n    <Flows/>\n"
        '    <PostFlow name="PostFlow">\n        <Request/>\n        <Response/>\n    </PostFlow>\n'
        "    <HTTPProxyConnection>\n        <BasePath>/cp5r8</BasePath>\n        <VirtualHost>default</VirtualHost>\n"
        "    </HTTPProxyConnection>\n"
        '    <RouteRule name="default">\n        <TargetEndpoint>default</TargetEndpoint>\n    </RouteRule>\n'
        "</ProxyEndpoint>\n"
    )
    target = (
        XML_HEAD + '<TargetEndpoint name="default">\n'
        '    <PreFlow name="PreFlow">\n        <Request/>\n        <Response/>\n    </PreFlow>\n    <Flows/>\n'
        '    <PostFlow name="PostFlow">\n        <Request/>\n        <Response/>\n    </PostFlow>\n'
        f"    <HTTPTargetConnection>\n        <URL>http://127.0.0.1:{backend_port}/r8</URL>\n"
        "    </HTTPTargetConnection>\n</TargetEndpoint>\n"
    )
    manifest = (
        XML_HEAD + f'<APIProxy revision="1" name="{APP_R8}">\n    <DisplayName>{APP_R8}</DisplayName>\n'
        f"    <Policies>\n{items}    </Policies>\n"
        "    <ProxyEndpoints>\n        <ProxyEndpoint>default</ProxyEndpoint>\n    </ProxyEndpoints>\n"
        "    <TargetEndpoints>\n        <TargetEndpoint>default</TargetEndpoint>\n    </TargetEndpoints>\n"
        "</APIProxy>\n"
    )
    root = parent / APP_R8
    files = {
        f"apiproxy/{APP_R8}.xml": manifest,
        "apiproxy/proxies/default.xml": proxy,
        "apiproxy/targets/default.xml": target,
        **{f"apiproxy/policies/{name}.xml": text for name, text in policies.items()},
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


@pytest.fixture(scope="module")
def cp5_r8_app(
    mule_runtime: Any, cp5_r7_backend: Backend, free_port: Callable[[], int], tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Cp5App]:
    from a2m.generator import generate_project
    from a2m.parser import read_bundle
    from a2m.verify.mule import BuildError, DeployError, package

    work = tmp_path_factory.mktemp("cp5-r8")
    bundle = read_bundle(write_cp5_r8_bundle(work / "bundles", cp5_r7_backend.port))
    project = work / APP_R8 / "mule-app"
    result = generate_project(bundle, project, shared_flows=())
    port = free_port()
    set_listen_port(project, port)
    try:
        jar = package(project, timeout=BUILD_TIMEOUT)
    except BuildError as exc:
        pytest.fail(f"mvn package failed for {APP_R8}: {exc}\n{exc.output}", pytrace=False)
    try:
        mule_runtime.runner.deploy(Path(jar), app_name=APP_R8, timeout=DEPLOY_TIMEOUT)
    except DeployError as exc:
        pytest.fail(f"{APP_R8} did not deploy: {exc}\n{exc.log_excerpt}", pytrace=False)
    try:
        yield Cp5App(port, project, result)
    finally:
        with contextlib.suppress(Exception):
            mule_runtime.runner.undeploy(APP_R8, timeout=60)


def _r8_vals(app: Cp5App, backend: Backend, path: str, body: bytes) -> tuple[str | None, str]:
    """The X-Vals header and the path the backend got for a JSON POST to ``path``."""
    backend.clear()
    status, _, reply = call(app.port, "POST", path, headers={"Content-Type": "application/json"}, body=body)
    assert status == 200, (path, status, reply)
    seen = backend.seen()
    assert len(seen) == 1, (path, seen)
    return seen[0].headers.get("x-vals"), seen[0].path


@pytest.mark.runtime
def test_CP5_T70_uripath_on_the_bare_base_path_finds_nothing_and_keeps_the_earlier_value(
    cp5_r8_app: Cp5App, cp5_r7_backend: Backend
) -> None:
    """[CP5-T70] EV-Path extracts /{pid} after AM-Default sets pid = dp. Apigee's path suffix is '' for the bare base
    path, so /{pid} finds nothing there and pid stays dp; the base path with a trailing slash gives pid = '' and
    /cp5r8/x7 gives x7. The target gets the same suffix (nothing, '/', '/x7')."""
    body = b'{"k":1}'
    got = {path: _r8_vals(cp5_r8_app, cp5_r7_backend, path, body) for path in ("/cp5r8", "/cp5r8/", "/cp5r8/x7")}
    pids = {path: (vals or "").split("|")[0] for path, (vals, _) in got.items()}
    assert pids == {"/cp5r8": "dp", "/cp5r8/": "", "/cp5r8/x7": "x7"}, got
    assert {path: seen for path, (_, seen) in got.items()} == {
        "/cp5r8": "/r8",
        "/cp5r8/": "/r8/",
        "/cp5r8/x7": "/r8/x7",
    }, got


@pytest.mark.runtime
def test_CP5_T70_a_jsonpath_step_on_the_wrong_json_type_finds_nothing_and_keeps_the_earlier_value(
    cp5_r8_app: Cp5App, cp5_r7_backend: Backend
) -> None:
    """[CP5-T70] $.j on a top-level array body, $.items.id where items is an array and $[0] on an object body each
    find nothing in Apigee, so the variable keeps AM-Default's value; $[0] and $[0].j on the array and $.j and
    $.items[1].id on the object find their values."""
    array_vals, _ = _r8_vals(cp5_r8_app, cp5_r7_backend, "/cp5r8/x7", b'[{"j":"zz"}]')
    object_vals, _ = _r8_vals(cp5_r8_app, cp5_r7_backend, "/cp5r8/x7", b'{"items":[{"id":"a"},{"id":"b"}],"j":"v"}')
    pid, ja, jn, jo, ji, jx = (array_vals or "").split("|")
    assert (pid, ja, jn, ji, jx) == ("x7", "dja", "djn", "zz", "djx"), array_vals
    assert json.loads(jo) == {"j": "zz"}, array_vals
    assert object_vals == "x7|v|djn|djo|dji|b", object_vals


# ---------------------------------------------------------------- CP5 adversarial round 9
# A step a2m wraps in a try scope, in an app with no RaiseFault, used to name the undeclared error type
# A2M:POLICY_FAULT, and Mule refused to deploy the app. This app has wrapped steps on both sides and no RaiseFault.

APP_R9 = "cp5-r9"


def write_cp5_r9_bundle(parent: Path, backend_port: int) -> Path:
    policies = {
        "AM-Two": _r7_assign("AM-Two", [("a", "<Value>1</Value>"), ("b", "<Value>2</Value>")]),
        "AM-Vals": _r7_headers("AM-Vals", [("X-Vals", "{a}|{b}")], "request"),
        "AM-Two-Resp": _r7_assign("AM-Two-Resp", [("c", "<Value>3</Value>"), ("d", "<Value>4</Value>")]),
        "AM-Out": _r7_headers("AM-Out", [("X-Out", "{c}|{d}")], "response"),
    }
    request = _step("AM-Two") + _step("AM-Vals")
    response = _step("AM-Two-Resp", "request.verb = &quot;GET&quot;") + _step("AM-Out")
    items = "".join(f"        <Policy>{p}</Policy>\n" for p in policies)
    proxy = (
        XML_HEAD + '<ProxyEndpoint name="default">\n'
        f'    <PreFlow name="PreFlow">\n        <Request>{request}</Request>\n'
        f"        <Response>{response}</Response>\n    </PreFlow>\n    <Flows/>\n"
        '    <PostFlow name="PostFlow">\n        <Request/>\n        <Response/>\n    </PostFlow>\n'
        "    <HTTPProxyConnection>\n        <BasePath>/cp5r9</BasePath>\n        <VirtualHost>default</VirtualHost>\n"
        "    </HTTPProxyConnection>\n"
        '    <RouteRule name="default">\n        <TargetEndpoint>default</TargetEndpoint>\n    </RouteRule>\n'
        "</ProxyEndpoint>\n"
    )
    target = (
        XML_HEAD + '<TargetEndpoint name="default">\n'
        '    <PreFlow name="PreFlow">\n        <Request/>\n        <Response/>\n    </PreFlow>\n    <Flows/>\n'
        '    <PostFlow name="PostFlow">\n        <Request/>\n        <Response/>\n    </PostFlow>\n'
        f"    <HTTPTargetConnection>\n        <URL>http://127.0.0.1:{backend_port}/r9</URL>\n"
        "    </HTTPTargetConnection>\n</TargetEndpoint>\n"
    )
    manifest = (
        XML_HEAD + f'<APIProxy revision="1" name="{APP_R9}">\n    <DisplayName>{APP_R9}</DisplayName>\n'
        f"    <Policies>\n{items}    </Policies>\n"
        "    <ProxyEndpoints>\n        <ProxyEndpoint>default</ProxyEndpoint>\n    </ProxyEndpoints>\n"
        "    <TargetEndpoints>\n        <TargetEndpoint>default</TargetEndpoint>\n    </TargetEndpoints>\n"
        "</APIProxy>\n"
    )
    root = parent / APP_R9
    files = {
        f"apiproxy/{APP_R9}.xml": manifest,
        "apiproxy/proxies/default.xml": proxy,
        "apiproxy/targets/default.xml": target,
        **{f"apiproxy/policies/{name}.xml": text for name, text in policies.items()},
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


@pytest.mark.runtime
def test_CP5_T71_an_app_with_wrapped_steps_and_no_raisefault_deploys_and_runs_them(
    mule_runtime: Any, cp5_r7_backend: Backend, free_port: Callable[[], int], tmp_path_factory: pytest.TempPathFactory
) -> None:
    """[CP5-T71] AM-Two and AM-Two-Resp each assign two variables, so a2m wraps each in a try scope; the bundle has no
    RaiseFault. The app builds and deploys, and both wrapped steps run: the backend gets X-Vals 1|2 and the caller
    gets X-Out 3|4."""
    import xml.etree.ElementTree as ET

    from a2m.generator import generate_project
    from a2m.parser import read_bundle
    from a2m.verify.mule import BuildError, DeployError, package

    work = tmp_path_factory.mktemp("cp5-r9")
    bundle = read_bundle(write_cp5_r9_bundle(work / "bundles", cp5_r7_backend.port))
    project = work / APP_R9 / "mule-app"
    generate_project(bundle, project, shared_flows=())
    scopes = [
        e
        for xml in (project / "src" / "main" / "mule").glob("*.xml")
        for e in ET.parse(xml).getroot().iter("{http://www.mulesoft.org/schema/mule/core}try")
    ]
    assert len(scopes) >= 2, "the scenario needs steps wrapped in try scopes"
    port = free_port()
    set_listen_port(project, port)
    try:
        jar = package(project, timeout=BUILD_TIMEOUT)
    except BuildError as exc:
        pytest.fail(f"mvn package failed for {APP_R9}: {exc}\n{exc.output}", pytrace=False)
    try:
        mule_runtime.runner.deploy(Path(jar), app_name=APP_R9, timeout=DEPLOY_TIMEOUT)
    except DeployError as exc:
        pytest.fail(f"{APP_R9} did not deploy: {exc}\n{exc.log_excerpt}", pytrace=False)
    try:
        cp5_r7_backend.clear()
        status, headers, reply = call(port, "GET", "/cp5r9/x")
        assert status == 200, (status, reply)
        seen = cp5_r7_backend.seen()
        assert [(s.path, s.headers.get("x-vals")) for s in seen] == [("/r9/x", "1|2")], seen
        assert headers.get("x-out") == "3|4", headers
    finally:
        with contextlib.suppress(Exception):
            mule_runtime.runner.undeploy(APP_R9, timeout=60)


# ---------------------------------------------------------------- CP5 adversarial round 9: JSONPayload Content-Type
# Apigee extracts JSONPayload only when the message's Content-Type is JSON (exactly application/json, parameters
# ignored; a +json type is not JSON, CP5 round 10); any other body, even one DataWeave parses into an object (a form or XML body), finds nothing
# and the variable keeps its earlier value. Request side: the cp5-r8 app (EV-Json $.j into ja, default dja).
# Response side: a sixth app, 'cp5-r10' (/cp5r10), whose loopback backend answers with the Content-Type the caller
# names in X-Reply-Type; EV-Resp extracts $.e from the response into re (default dre), sent back as X-Re. Its
# request side sets a JSON payload with a contentType (AM-Body, when X-Set: 1) before EV-Req extracts $.q into rq
# (default drq), sent to the backend as X-Rq.


def _x72_ja(app: Cp5App, backend: Backend, content_type: str | None, body: bytes) -> str:
    """The ja value the backend got for a POST of ``body`` with ``content_type`` to the cp5-r8 app."""
    backend.clear()
    headers = {"Content-Type": content_type} if content_type is not None else {}
    status, _, reply = call(app.port, "POST", "/cp5r8/x7", headers=headers, body=body)
    assert status == 200, (content_type, status, reply)
    seen = backend.seen()
    assert len(seen) == 1, (content_type, seen)
    return (seen[0].headers.get("x-vals") or "").split("|")[1]


@pytest.mark.runtime
def test_CP5_T72_a_form_or_xml_request_body_keeps_the_earlier_value_and_a_json_one_is_extracted(
    cp5_r8_app: Cp5App, cp5_r7_backend: Backend
) -> None:
    """[CP5-T72] $.j into ja after ja = dja: a form body j=formv, an XML body <j>xmlv</j>, a JSON body sent as
    text/plain, a JSON body with no Content-Type and a +json subtype each keep dja, as in Apigee; application/json,
    with charset=utf-8 and in upper case each give the JSON value."""
    got = {
        label: _x72_ja(cp5_r8_app, cp5_r7_backend, content_type, body)
        for label, content_type, body in (
            ("form", "application/x-www-form-urlencoded", b"j=formv"),
            ("xml", "application/xml", b"<j>xmlv</j>"),
            ("text", "text/plain", b'{"j":"textv"}'),
            ("none", None, b'{"j":"nonev"}'),
            ("json", "application/json", b'{"j":"v1"}'),
            ("charset", "application/json; charset=utf-8", b'{"j":"v2"}'),
            ("upper", "Application/JSON", b'{"j":"v3"}'),
            ("plus", "application/vnd.api+json", b'{"j":"v4"}'),
        )
    }
    assert got == {
        "form": "dja",
        "xml": "dja",
        "text": "dja",
        "none": "dja",
        "json": "v1",
        "charset": "v2",
        "upper": "v3",
        "plus": "dja",
    }, got


APP_R10 = "cp5-r10"
X72_REPLIES = {
    "json": ("application/json", b'{"e":"jv"}'),
    "charset": ("application/json; charset=utf-8", b'{"e":"cv"}'),
    "problem": ("application/problem+json", b'{"e":"pv"}'),
    "xml": ("application/xml", b"<e>xv</e>"),
    "form": ("application/x-www-form-urlencoded", b"e=fv"),
    "text": ("text/plain", b'{"e":"tv"}'),
}


@pytest.fixture(scope="module")
def cp5_r10_backend() -> Iterator[Backend]:
    state = Backend(port=0)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            with state.lock:
                state.records.append(
                    Seen(self.command, urlsplit(self.path).path, {k.lower(): v for k, v in self.headers.items()})
                )
            content_type, body = X72_REPLIES.get(self.headers.get("X-Reply-Type") or "json", X72_REPLIES["json"])
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _handle

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    state.port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


def write_cp5_r10_bundle(parent: Path, backend_port: int) -> Path:
    policies = {
        "AM-Default": _r7_assign("AM-Default", [("rq", "<Value>drq</Value>")]),
        "AM-Body": _policy(
            "AssignMessage",
            "AM-Body",
            '    <Set>\n        <Payload contentType="application/json">{"q":"set"}</Payload>\n    </Set>\n'
            "    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n"
            '    <AssignTo createNew="false" transport="http" type="request"/>\n',
        ),
        "EV-Req": _r7_extract(
            "EV-Req",
            "request",
            '    <JSONPayload>\n        <Variable name="rq">\n            <JSONPath>$.q</JSONPath>\n'
            "        </Variable>\n    </JSONPayload>\n",
        ),
        "AM-Vals": _r7_headers("AM-Vals", [("X-Rq", "{rq}")], "request"),
        "AM-Default-R": _r7_assign("AM-Default-R", [("re", "<Value>dre</Value>")]),
        "EV-Resp": _r7_extract(
            "EV-Resp",
            "response",
            '    <JSONPayload>\n        <Variable name="re">\n            <JSONPath>$.e</JSONPath>\n'
            "        </Variable>\n    </JSONPayload>\n",
        ),
        "AM-Out": _r7_headers("AM-Out", [("X-Re", "{re}")], "response"),
    }
    request = (
        _step("AM-Default")
        + _step("AM-Body", "request.header.X-Set = &quot;1&quot;")
        + _step("EV-Req")
        + _step("AM-Vals")
    )
    response = _step("AM-Default-R") + _step("EV-Resp") + _step("AM-Out")
    items = "".join(f"        <Policy>{p}</Policy>\n" for p in policies)
    proxy = (
        XML_HEAD + '<ProxyEndpoint name="default">\n'
        f'    <PreFlow name="PreFlow">\n        <Request>{request}</Request>\n'
        f"        <Response>{response}</Response>\n    </PreFlow>\n    <Flows/>\n"
        '    <PostFlow name="PostFlow">\n        <Request/>\n        <Response/>\n    </PostFlow>\n'
        "    <HTTPProxyConnection>\n        <BasePath>/cp5r10</BasePath>\n        <VirtualHost>default</VirtualHost>\n"
        "    </HTTPProxyConnection>\n"
        '    <RouteRule name="default">\n        <TargetEndpoint>default</TargetEndpoint>\n    </RouteRule>\n'
        "</ProxyEndpoint>\n"
    )
    target = (
        XML_HEAD + '<TargetEndpoint name="default">\n'
        '    <PreFlow name="PreFlow">\n        <Request/>\n        <Response/>\n    </PreFlow>\n    <Flows/>\n'
        '    <PostFlow name="PostFlow">\n        <Request/>\n        <Response/>\n    </PostFlow>\n'
        f"    <HTTPTargetConnection>\n        <URL>http://127.0.0.1:{backend_port}/r10</URL>\n"
        "    </HTTPTargetConnection>\n</TargetEndpoint>\n"
    )
    manifest = (
        XML_HEAD + f'<APIProxy revision="1" name="{APP_R10}">\n    <DisplayName>{APP_R10}</DisplayName>\n'
        f"    <Policies>\n{items}    </Policies>\n"
        "    <ProxyEndpoints>\n        <ProxyEndpoint>default</ProxyEndpoint>\n    </ProxyEndpoints>\n"
        "    <TargetEndpoints>\n        <TargetEndpoint>default</TargetEndpoint>\n    </TargetEndpoints>\n"
        "</APIProxy>\n"
    )
    root = parent / APP_R10
    files = {
        f"apiproxy/{APP_R10}.xml": manifest,
        "apiproxy/proxies/default.xml": proxy,
        "apiproxy/targets/default.xml": target,
        **{f"apiproxy/policies/{name}.xml": text for name, text in policies.items()},
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


@pytest.fixture(scope="module")
def cp5_r10_app(
    mule_runtime: Any, cp5_r10_backend: Backend, free_port: Callable[[], int], tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Cp5App]:
    from a2m.generator import generate_project
    from a2m.parser import read_bundle
    from a2m.verify.mule import BuildError, DeployError, package

    work = tmp_path_factory.mktemp("cp5-r10")
    bundle = read_bundle(write_cp5_r10_bundle(work / "bundles", cp5_r10_backend.port))
    project = work / APP_R10 / "mule-app"
    result = generate_project(bundle, project, shared_flows=())
    port = free_port()
    set_listen_port(project, port)
    try:
        jar = package(project, timeout=BUILD_TIMEOUT)
    except BuildError as exc:
        pytest.fail(f"mvn package failed for {APP_R10}: {exc}\n{exc.output}", pytrace=False)
    try:
        mule_runtime.runner.deploy(Path(jar), app_name=APP_R10, timeout=DEPLOY_TIMEOUT)
    except DeployError as exc:
        pytest.fail(f"{APP_R10} did not deploy: {exc}\n{exc.log_excerpt}", pytrace=False)
    try:
        yield Cp5App(port, project, result)
    finally:
        with contextlib.suppress(Exception):
            mule_runtime.runner.undeploy(APP_R10, timeout=60)


def _x72_call(app: Cp5App, backend: Backend, headers: dict[str, str], body: bytes) -> tuple[str | None, str | None]:
    """(X-Rq the backend got, X-Re the caller got) for a POST of ``body`` with ``headers`` to the cp5-r10 app."""
    backend.clear()
    status, reply_headers, reply = call(app.port, "POST", "/cp5r10/x", headers=headers, body=body)
    assert status == 200, (headers, status, reply)
    seen = backend.seen()
    assert len(seen) == 1, (headers, seen)
    return seen[0].headers.get("x-rq"), reply_headers.get("x-re")


@pytest.mark.runtime
def test_CP5_T72_a_response_jsonpayload_extracts_only_from_a_json_response(
    cp5_r10_app: Cp5App, cp5_r10_backend: Backend
) -> None:
    """[CP5-T72] $.e into re after re = dre: a backend answer of application/json or application/json;
    charset=utf-8 gives its value; an application/problem+json, XML, form or text/plain answer (the first and last
    with a JSON body) keeps dre, as in Apigee."""
    got = {
        reply: _x72_call(cp5_r10_app, cp5_r10_backend, {"Content-Type": "application/json", "X-Reply-Type": reply},
                         b'{"q":"c"}')[1]
        for reply in X72_REPLIES
    }
    assert got == {"json": "jv", "charset": "cv", "problem": "dre", "xml": "dre", "form": "dre", "text": "dre"}, got


@pytest.mark.runtime
def test_CP5_T72_a_set_payload_with_a_json_content_type_is_read_as_json(
    cp5_r10_app: Cp5App, cp5_r10_backend: Backend
) -> None:
    """[CP5-T72] AM-Body (when X-Set: 1) sets the request payload {"q":"set"} with contentType application/json, so
    EV-Req reads q = set even when the caller sent text/plain or XML; without it a text/plain JSON body keeps drq
    and an application/json one gives its value."""
    cases = {
        "set-over-text": ({"Content-Type": "text/plain", "X-Set": "1"}, b'{"q":"orig"}'),
        "set-over-xml": ({"Content-Type": "application/xml", "X-Set": "1"}, b"<q>orig</q>"),
        "text": ({"Content-Type": "text/plain"}, b'{"q":"orig"}'),
        "json": ({"Content-Type": "application/json"}, b'{"q":"orig"}'),
    }
    got = {label: _x72_call(cp5_r10_app, cp5_r10_backend, headers, body)[0] for label, (headers, body) in cases.items()}
    assert got == {"set-over-text": "set", "set-over-xml": "set", "text": "drq", "json": "orig"}, got


# ---------------------------------------------------------------- CP5 adversarial round 10: exact application/json
# Apigee's ExtractVariables reference: JSON extraction is performed only when the message's Content-Type is
# application/json. A +json type (JSON:API, HAL, RFC 7807 problem details) keeps the earlier value.


@pytest.mark.runtime
def test_CP5_T73_a_plus_json_request_body_keeps_the_earlier_value(
    cp5_r8_app: Cp5App, cp5_r7_backend: Backend
) -> None:
    """[CP5-T73] $.j into ja after ja = dja: a JSON body sent as application/vnd.api+json, application/hal+json or
    application/problem+json; charset=utf-8 keeps dja; the same body as Application/Json; charset=UTF-8 gives v."""
    got = {
        label: _x72_ja(cp5_r8_app, cp5_r7_backend, content_type, b'{"j":"v"}')
        for label, content_type in (
            ("vnd", "application/vnd.api+json"),
            ("hal", "application/hal+json"),
            ("problem", "application/problem+json; charset=utf-8"),
            ("json", "Application/Json; charset=UTF-8"),
        )
    }
    assert got == {"vnd": "dja", "hal": "dja", "problem": "dja", "json": "v"}, got


@pytest.mark.runtime
def test_CP5_T73_a_problem_json_response_keeps_the_default(cp5_r10_app: Cp5App, cp5_r10_backend: Backend) -> None:
    """[CP5-T73] A backend answering application/problem+json with the JSON body {"e":"pv"} leaves re at its default
    dre; an application/json answer gives jv. Both EV steps of cp5-r10 set IgnoreUnresolvedVariables to true, so
    neither lists that setting."""
    got = {
        reply: _x72_call(cp5_r10_app, cp5_r10_backend, {"Content-Type": "application/json", "X-Reply-Type": reply},
                         b'{"q":"c"}')[1]
        for reply in ("problem", "json")
    }
    assert got == {"problem": "dre", "json": "jv"}, got
    for name in ("EV-Req", "EV-Resp"):
        records = [r for r in cp5_r10_app.result.policies if str(r.name) == name]
        assert len(records) == 1, [str(r.name) for r in cp5_r10_app.result.policies]
        names = [str(o.name) for o in records[0].unsupported_options]
        assert "IgnoreUnresolvedVariables" not in names, (name, names)
