"""CP7: the verification harness, its mock backend and the honest verification type (default suite, no Mule).

Every test here runs without Java, Maven or Mule. Tool detection and the clean
skip run with PATH pointed at a tmp bin folder holding nothing or small shell
stubs (mvn, java) that log their calls; MULE_HOME, A2M_MULE_HOME and
JAVA_HOME are removed from the environment (mise exports them). The decision,
comparison and diff logic runs through a fake runner injected through the
harness's runner interface; the fake simulates the fixture proxies' policies
and forwards allowed requests to a real a2m mock backend on 127.0.0.1, so hit
counts are real. All fixtures (bundles, golden recordings, mule.log excerpts)
are built under tmp_path by this file.

Public contract these tests pin (CP7 plan, files_likely a2m/verify/*):

    a2m.verify (package) exports
        VerificationType            StrEnum: "golden", "battery", "static", "failed"
        HttpRequest(method: str, path: str, headers: Mapping[str, str] = {}, body: bytes = b"")
                                    ``path`` includes the query string ("/orders?id=7")
        HttpResponse(status: int, headers: Mapping[str, str] = {}, body: bytes = b"")
        AppUnderTest(name: str, app_dir: Path)
        Runner (protocol)           start(app: AppUnderTest, *, backend_url: str) -> AppHandle
                                    builds and starts the app pointed at ``backend_url``; raises
                                    a2m.verify.mule.BuildError / DeployError (MuleError) when it cannot
        AppHandle (protocol)        running: bool (False: built but never started), base_url: str | None,
                                    send(request: HttpRequest) -> HttpResponse, stop() -> None
        VerifyConfig(ignore_headers: Sequence[str] = compare.DEFAULT_IGNORED_HEADERS)
        verify_proxy(bundle, app_dir, *, runner, backend=None, golden=None, config=None) -> VerificationResult
                                    ``golden`` is the --golden folder (one sub-folder per proxy name); a
                                    backend passed in is used and left running, otherwise the harness
                                    starts its own and stops it afterwards
        VerificationResult          type, ran, passed, failed (ints), cases (CaseResult...),
                                    untested (UntestedPolicy...), review_flags (ReviewFlag...),
                                    message: str, log_excerpt: str
        CaseResult                  name, situation (battery situation, None for golden), passed: bool,
                                    diff: str, backend_calls: int (calls the backend received in the case)
        UntestedPolicy              name, type, reason
        ReviewFlag                  policy, policy_type, reason
        make_verify_stage(runner=None, config=None) -> engine stage; for each proxy writes
                                    <proxy folder>/verification.json ({"type", "ran", "passed", "failed",
                                    "message", "cases": [{"name", "situation", "passed", "diff", ...}], ...})
                                    and a run.log line "<proxy>: ... verification type: <type> ..."
        explain_deploy_failure(app_name, log_text, *, mule_version) -> VerificationResult
    a2m.verify.tools.detect_tools() -> ToolStatus(missing: tuple[str, ...] of "Java" / "Maven" / "Mule",
                                    mule_home: Path | None)   (A2M_MULE_HOME wins over MULE_HOME; the
                                    folder counts only with lib/boot/mule-module-reboot-*.jar)
    a2m.verify.runner.MuleAppRunner(mule_home: Path, mule_base: Path)
                                    the real Runner (wraps a2m.verify.mule): mvn package in the app folder,
                                    Mule started lazily under the private mule_base; context manager,
                                    close() stops it; pids
    a2m.verify.mock_backend.MockBackend(*, default_status=200, default_headers=None, default_body=b"")
                                    start(), stop(), host, port, url, calls() -> [RecordedCall(method,
                                    path, query: str, headers (lower-case names), body: bytes)], clear(),
                                    respond(method, path, *, status, headers=None, body=b"")
    a2m.verify.batteries.build_battery(bundle, app_dir) -> Battery(cases, untested)
                                    BatteryCase(name, policy, policy_type, situation, calls
                                    (BatteryCall(request: HttpRequest, expected_status: int,
                                    expected_body: bytes | None)...), expected_backend_calls: int,
                                    expected_backend_headers: Mapping[str, str | None] (None: must be
                                    absent)); the valid API key is read from the generated properties
    a2m.verify.compare.compare_response(expected, actual, *, ignore_headers=None) -> Comparison(matched,
                                    diff); expected headers are a subset, names compared without case, JSON
                                    bodies compared as JSON; DEFAULT_IGNORED_HEADERS
    a2m.verify.mule.wait_for_deploy(mule_base, app_name, *, timeout) -> None, raises DeployError

Battery situations pinned here: SpikeArrest under-limit / over-limit; Quota within-allowance /
over-allowance; VerifyAPIKey valid-key / missing-key / bad-key; AssignMessage header-set /
condition-false; ExtractVariables extracted-value-used; RaiseFault fault-raised; BasicAuthentication
correct-credentials / missing-credentials; AccessControl denied-address. Two deliberate deviations from
the plan's wording, grounded in the CP4 templates: a BasicAuthentication Decode with no Authorization
header fails with 500 (a2m/policies/basic_auth.py, Apigee's InvalidBasicAuthenticationSource), not 401,
and a Decode does not check credentials, so there is no "wrong credentials" situation; AccessControl
checks the TCP peer (a2m/policies/access_control.py), which is always 127.0.0.1 locally, so only the
denied-address case is required. SpikeArrest is paced (CP4): "calls 1-3 pass, call 4 gets 429" means
the harness spaces the allowed calls by at least the pace interval and sends the last one too soon.

Golden recording format (a2m's JSON, one exchange per file, replayed in file-name order):

    {"name": "valid-key",
     "calls": [{"after_ms": 0,
                "request": {"method": "GET", "path": "/orders/7", "headers": {...}, "body": ""},
                "response": {"status": 200, "headers": {...}, "body": "{\\"id\\":7}"}}],
     "backend_calls": [{"method": "GET", "path": "/orders/7", "headers": {"X-Client": "a2m"},
                        "response": {"status": 200, "headers": {...}, "body": "..."}}]}

``after_ms`` is how long after the previous call the call was made (from the recording's timestamps);
the harness answers each recorded backend call with the response recorded for it.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import socket
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

REPO = Path(__file__).resolve().parents[1]
CP6 = REPO / "tests" / "fixtures" / "apigee" / "cp6"
LLM = REPO / "tests" / "fixtures" / "llm"
GOOD_KEY = "good-key-123"
BAD_KEY = "nope"
BACKEND_BODY = b'{"id":7}'
JSON_HEADERS = {"Content-Type": "application/json"}
XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
ITEMS_CONDITION = '(proxy.pathsuffix MatchesPath "/items") and (request.verb = "GET")'
TIME_WINDOW_TYPES = {"SpikeArrest", "Quota"}
CLAIMED_TYPE = re.compile(r"""(?i)(verification[ _]type|"type")\W{0,4}(golden|battery)\b""")
VERSION_MISMATCH = "connector version not compatible with Mule runtime 4.9.0"


# ---------------------------------------------------------------- fixture bundles


def policy_verify_key(ref: str = "request.header.x-api-key") -> str:
    return f'<VerifyAPIKey name="verify-key">\n    <APIKey ref="{ref}"/>\n</VerifyAPIKey>\n'


def policy_spike(rate: str) -> str:
    return f'<SpikeArrest name="spike">\n    <Rate>{rate}</Rate>\n</SpikeArrest>\n'


def policy_add_header(name: str = "add-header", header: str = "X-Client", value: str = "a2m") -> str:
    return (
        f'<AssignMessage name="{name}">\n'
        f'    <Set>\n        <Headers>\n            <Header name="{header}">{value}</Header>\n'
        "        </Headers>\n    </Set>\n"
        "    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n"
        '    <AssignTo createNew="false" transport="http" type="request"/>\n'
        "</AssignMessage>\n"
    )


QUOTA = (
    '<Quota name="quota">\n    <Allow count="100"/>\n    <Interval>1</Interval>\n'
    "    <TimeUnit>hour</TimeUnit>\n</Quota>\n"
)
CACHE = (
    '<ResponseCache name="cache">\n    <CacheKey>\n        <KeyFragment ref="request.uri" type="string"/>\n'
    "    </CacheKey>\n    <ExpirySettings>\n        <TimeoutInSec>300</TimeoutInSec>\n    </ExpirySettings>\n"
    "</ResponseCache>\n"
)
OAUTH = '<OAuthV2 name="oauth">\n    <Operation>VerifyAccessToken</Operation>\n</OAuthV2>\n'
EXTRACT = (
    '<ExtractVariables name="extract-id">\n    <Source>request</Source>\n    <VariablePrefix>order</VariablePrefix>\n'
    '    <QueryParam name="id">\n        <Pattern>{id}</Pattern>\n    </QueryParam>\n'
    "    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n</ExtractVariables>\n"
)
RAISE_FAULT = (
    '<RaiseFault name="teapot">\n    <FaultResponse>\n        <Set>\n'
    '            <Payload contentType="application/json">{"error":"teapot"}</Payload>\n'
    "            <StatusCode>418</StatusCode>\n            <ReasonPhrase>I'm a teapot</ReasonPhrase>\n"
    "        </Set>\n    </FaultResponse>\n    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n"
    "</RaiseFault>\n"
)
BASIC_AUTH = (
    '<BasicAuthentication name="decode-basic">\n    <Operation>Decode</Operation>\n'
    "    <IgnoreUnresolvedVariables>false</IgnoreUnresolvedVariables>\n"
    '    <User ref="basic.user"/>\n    <Password ref="basic.password"/>\n'
    "    <Source>request.header.Authorization</Source>\n</BasicAuthentication>\n"
)
ACCESS_CONTROL = (
    '<AccessControl name="ip-check">\n    <IPRules noRuleMatchAction="DENY">\n'
    '        <MatchRule action="ALLOW">\n            <SourceAddress mask="8">10.0.0.0</SourceAddress>\n'
    "        </MatchRule>\n    </IPRules>\n</AccessControl>\n"
)


def _steps(steps: Sequence[tuple[str, str | None]]) -> str:
    out = []
    for name, condition in steps:
        cond = f"<Condition>{condition}</Condition>" if condition else ""
        out.append(f"<Step><Name>{name}</Name>{cond}</Step>")
    return "".join(out)


def write_proxy(
    parent: Path,
    name: str,
    *,
    base_path: str,
    target_url: str,
    policies: Mapping[str, str],
    request_steps: Sequence[tuple[str, str | None]] = (),
    flows: Sequence[tuple[str, str, Sequence[str], Sequence[str]]] = (),
) -> Path:
    """Write parent/<name>/apiproxy/... and return parent/<name>.

    ``flows`` are (flow name, condition, request step names, response step names) conditional flows.
    """
    root = parent / name / "apiproxy"
    for sub in ("policies", "proxies", "targets"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    policy_list = "".join(f"<Policy>{p}</Policy>" for p in policies)
    (root / f"{name}.xml").write_text(
        XML_HEAD + f'<APIProxy revision="1" name="{name}">\n    <DisplayName>{name}</DisplayName>\n'
        f"    <Policies>{policy_list}</Policies>\n"
        "    <ProxyEndpoints><ProxyEndpoint>default</ProxyEndpoint></ProxyEndpoints>\n"
        "    <TargetEndpoints><TargetEndpoint>default</TargetEndpoint></TargetEndpoints>\n</APIProxy>\n",
        encoding="utf-8",
    )
    for policy_name, xml in policies.items():
        (root / "policies" / f"{policy_name}.xml").write_text(XML_HEAD + xml, encoding="utf-8")
    flow_xml = "".join(
        f'<Flow name="{flow}"><Request>{_steps([(s, None) for s in req])}</Request>'
        f"<Response>{_steps([(s, None) for s in resp])}</Response><Condition>{cond}</Condition></Flow>"
        for flow, cond, req, resp in flows
    )
    (root / "proxies" / "default.xml").write_text(
        XML_HEAD + '<ProxyEndpoint name="default">\n'
        f'    <PreFlow name="PreFlow"><Request>{_steps(request_steps)}</Request><Response/></PreFlow>\n'
        f"    <Flows>{flow_xml}</Flows>\n"
        '    <PostFlow name="PostFlow"><Request/><Response/></PostFlow>\n'
        f"    <HTTPProxyConnection><BasePath>{base_path}</BasePath><VirtualHost>default</VirtualHost>"
        "</HTTPProxyConnection>\n"
        '    <RouteRule name="default"><TargetEndpoint>default</TargetEndpoint></RouteRule>\n</ProxyEndpoint>\n',
        encoding="utf-8",
    )
    (root / "targets" / "default.xml").write_text(
        XML_HEAD + '<TargetEndpoint name="default">\n'
        '    <PreFlow name="PreFlow"><Request/><Response/></PreFlow><Flows/>\n'
        '    <PostFlow name="PostFlow"><Request/><Response/></PostFlow>\n'
        f"    <HTTPTargetConnection><URL>{target_url}</URL></HTTPTargetConnection>\n</TargetEndpoint>\n",
        encoding="utf-8",
    )
    return parent / name


def write_orders(
    parent: Path, name: str = "orders-v1", *, rate: str = "3ps", key_ref: str = "request.header.x-api-key"
) -> Path:
    return write_proxy(
        parent,
        name,
        base_path="/orders",
        target_url="http://backend.example/orders",
        policies={
            "verify-key": policy_verify_key(key_ref),
            "spike": policy_spike(rate),
            "add-header": policy_add_header(),
        },
        request_steps=[("verify-key", None), ("spike", None), ("add-header", None)],
    )


def write_keys(parent: Path) -> Path:
    return write_proxy(
        parent,
        "keys-v1",
        base_path="/keys",
        target_url="http://backend.example/keys",
        policies={"verify-key": policy_verify_key(), "add-header": policy_add_header()},
        request_steps=[("verify-key", None), ("add-header", None)],
    )


def write_quota(parent: Path) -> Path:
    return write_proxy(
        parent,
        "quota-v1",
        base_path="/quota",
        target_url="http://backend.example/quota",
        policies={"quota": QUOTA},
        request_steps=[("quota", None)],
    )


def write_cache(parent: Path) -> Path:
    return write_proxy(
        parent,
        "cache-v1",
        base_path="/cache",
        target_url="http://backend.example/cache",
        policies={"cache": CACHE},
        flows=[("items", ITEMS_CONDITION, ["cache"], ["cache"])],
    )


def write_oauth_only(parent: Path) -> Path:
    return write_proxy(
        parent,
        "oauth-only",
        base_path="/oauth",
        target_url="http://backend.example/oauth",
        policies={"oauth": OAUTH},
        request_steps=[("oauth", None)],
    )


# One small proxy per CP4 policy type, for the battery shape (CP7-T08).
TYPE_PROXIES: dict[str, dict[str, Any]] = {
    "SpikeArrest": {"policies": {"spike": policy_spike("3ps")}, "steps": [("spike", None)]},
    "Quota": {"policies": {"quota": QUOTA}, "steps": [("quota", None)]},
    "VerifyAPIKey": {"policies": {"verify-key": policy_verify_key()}, "steps": [("verify-key", None)]},
    "AssignMessage": {
        "policies": {"add-header": policy_add_header()},
        "steps": [("add-header", 'request.header.x-mode = "tag"')],
    },
    "ExtractVariables": {
        "policies": {"extract-id": EXTRACT, "tag-order": policy_add_header("tag-order", "X-Order-Id", "{order.id}")},
        "steps": [("extract-id", None), ("tag-order", None)],
    },
    "RaiseFault": {"policies": {"teapot": RAISE_FAULT}, "steps": [("teapot", None)]},
    "BasicAuthentication": {"policies": {"decode-basic": BASIC_AUTH}, "steps": [("decode-basic", None)]},
    "AccessControl": {"policies": {"ip-check": ACCESS_CONTROL}, "steps": [("ip-check", None)]},
}


def write_type_proxy(parent: Path, policy_type: str) -> Path:
    spec = TYPE_PROXIES[policy_type]
    name = f"type-{policy_type.lower()}"
    return write_proxy(
        parent,
        name,
        base_path=f"/{name}",
        target_url=f"http://backend.example/{name}",
        policies=spec["policies"],
        request_steps=spec["steps"],
    )


def fill_key(app_dir: Path, key: str = GOOD_KEY) -> None:
    """Allow ``key`` in every VerifyAPIKey allowed-keys property of the generated properties (empty by default)."""
    for path in sorted((app_dir / "src" / "main" / "resources").rglob("*.properties")):
        text = path.read_text(encoding="utf-8")
        new = re.sub(r"(?m)^(verifyapikey\.[^=\n]*\.allowedKeys)=[ \t]*$", lambda m: f"{m.group(1)}={key}", text)
        path.write_text(new, encoding="utf-8")


@dataclass
class Generated:
    bundle: Any
    app_dir: Path


def generated(bundle_dir: Path, out: Path, *, key: bool = True) -> Generated:
    """Read ``bundle_dir`` and generate its Mule project under out/<name>/mule-app (allowing GOOD_KEY)."""
    from a2m.generator import generate_project
    from a2m.parser import read_bundle

    bundle = read_bundle(bundle_dir)
    app_dir = out / bundle.name / "mule-app"
    generate_project(bundle, app_dir)
    if key:
        fill_key(app_dir)
    return Generated(bundle, app_dir)


# ---------------------------------------------------------------- the fake runner


@dataclass(frozen=True)
class Profile:
    """What the fixture proxy's policies do, for the faithful fake runtime."""

    base: str
    key: tuple[str, str] | None = None  # ("header", name) or ("query", name)
    spike_rate: float | None = None
    spike_text: str = ""
    quota: int | None = None
    cache_path: str | None = None
    assign: bool = False


ORDERS = Profile("/orders", key=("header", "x-api-key"), spike_rate=3.0, spike_text="3ps", assign=True)
PROFILES = {
    "orders-v1": ORDERS,
    "broken-golden": ORDERS,
    "keys-v1": Profile("/keys", key=("header", "x-api-key"), assign=True),
    "quota-v1": Profile("/quota", quota=100),
    "cache-v1": Profile("/cache", cache_path="/items"),
    "oauth-only": Profile("/oauth"),
}
VARIANTS = {"correct", "built-not-run", "start-fails", "broken-spike", "broken-cache", "leaky-key", "no-header"}
START_ERROR = "orders-v1 failed to start: Address already in use (127.0.0.1:8081)"


def fault_body(faultstring: str, errorcode: str) -> bytes:
    return json.dumps({"fault": {"faultstring": faultstring, "detail": {"errorcode": errorcode}}}).encode()


def missing_key_body(where: str = "header", name: str = "x-api-key") -> bytes:
    return fault_body(
        f"Failed to resolve API Key variable request.{'header' if where == 'header' else 'queryparam'}.{name}",
        "steps.oauth.v2.FailedToResolveAPIKey",
    )


INVALID_KEY_BODY = fault_body("Invalid ApiKey", "oauth.v2.InvalidApiKey")
SPIKE_BODY = fault_body("Spike arrest violation. Allowed rate : 3ps", "policies.ratelimit.SpikeArrestViolation")


def response(status: int, headers: Mapping[str, str], body: bytes) -> Any:
    from a2m.verify import HttpResponse

    return HttpResponse(status=status, headers=dict(headers), body=body)


def lower_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in dict(headers).items()}


class FakeHandle:
    def __init__(self, runner: FakeRunner, name: str, variant: str, backend_url: str) -> None:
        self.runner = runner
        self.name = name
        self.variant = variant
        self.backend_url = backend_url
        self.profile = PROFILES[name]
        self.running = variant != "built-not-run"
        self.base_url: str | None = f"http://fake.invalid/{name}" if self.running else None
        self.last_spike: float | None = None
        self.quota_used = 0
        self.cached: Any = None
        self.stopped = False

    def send(self, request: Any) -> Any:
        assert self.running, f"the harness sent {request.method} {request.path} to {self.name}, which never started"
        assert not self.stopped, f"the harness sent {request.method} {request.path} to {self.name} after stop()"
        with self.runner.lock:
            self.runner.sent.append((self.name, request.method, request.path, lower_headers(request.headers or {})))
        return self._simulate(request)

    def stop(self) -> None:
        self.stopped = True
        self.runner.stopped.append(self.name)

    def _simulate(self, request: Any) -> Any:
        p = self.profile
        split = urlsplit(request.path)
        path = split.path
        query = parse_qs(split.query, keep_blank_values=True)
        headers = lower_headers(request.headers or {})
        if not (path == p.base or path.startswith(p.base + "/")):
            return response(404, JSON_HEADERS, b'{"fault":"no route"}')
        if p.key is not None:
            where, name = p.key
            key = headers.get(name, "") if where == "header" else (query.get(name) or [""])[0]
            if not key.strip():
                if self.variant == "leaky-key":
                    self._forward(request, headers, {})
                return response(401, JSON_HEADERS, missing_key_body(where, name))
            if key != GOOD_KEY:
                return response(401, JSON_HEADERS, INVALID_KEY_BODY)
        if p.spike_rate is not None and self.variant != "broken-spike":
            now = time.monotonic()
            if self.last_spike is not None and now - self.last_spike < 1.0 / p.spike_rate:
                return response(
                    429,
                    JSON_HEADERS,
                    fault_body(
                        f"Spike arrest violation. Allowed rate : {p.spike_text}",
                        "policies.ratelimit.SpikeArrestViolation",
                    ),
                )
            self.last_spike = now
        if p.quota is not None:
            self.quota_used += 1
            if self.quota_used > p.quota:
                return response(
                    429,
                    JSON_HEADERS,
                    fault_body(
                        f"Rate limit quota violation. Quota limit {p.quota} per 1 hour exceeded",
                        "policies.ratelimit.QuotaViolation",
                    ),
                )
        cacheable = p.cache_path is not None and request.method == "GET" and path == p.base + p.cache_path
        if cacheable and self.variant != "broken-cache" and self.cached is not None:
            return self.cached
        extra = {"x-client": "a2m"} if p.assign and self.variant != "no-header" else {}
        answer = self._forward(request, headers, extra)
        if cacheable:
            self.cached = answer
        return answer

    def _forward(self, request: Any, headers: Mapping[str, str], extra: Mapping[str, str]) -> Any:
        target = urlsplit(self.backend_url)
        sent = {
            k: v for k, v in headers.items() if k not in ("host", "content-length", "connection", "transfer-encoding")
        }
        sent.update(extra)
        conn = http.client.HTTPConnection(target.hostname or "127.0.0.1", target.port or 80, timeout=10)
        try:
            conn.request(request.method, request.path, body=request.body or None, headers=sent)
            got = conn.getresponse()
            body = got.read()
            return response(got.status, dict(got.getheaders()), body)
        finally:
            conn.close()


class FakeRunner:
    """A Runner whose apps are simulated in-process; ``overrides`` picks a variant per app name."""

    def __init__(self, variant: str = "correct", overrides: Mapping[str, str] | None = None) -> None:
        assert variant in VARIANTS and all(v in VARIANTS for v in (overrides or {}).values())
        self.variant = variant
        self.overrides = dict(overrides or {})
        self.lock = threading.Lock()
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.backend_urls: dict[str, str] = {}
        self.sent: list[tuple[str, str, str, dict[str, str]]] = []

    def start(self, app: Any, *, backend_url: str) -> FakeHandle:
        name = app.name
        self.started.append(name)
        self.backend_urls[name] = backend_url
        variant = self.overrides.get(name, self.variant)
        if variant == "start-fails":
            from a2m.verify.mule import DeployError

            raise DeployError(
                START_ERROR,
                "ERROR 2026-10-01 10:00:00,000 [WrapperListener_start_runner] java.net.BindException: "
                "Address already in use",
            )
        return FakeHandle(self, name, variant, backend_url)

    def requests_for(self, name: str) -> list[tuple[str, str, str | None]]:
        return [(m, p, h.get("x-api-key")) for n, m, p, h in self.sent if n == name]


# ---------------------------------------------------------------- shared helpers


@pytest.fixture
def backend() -> Iterator[Any]:
    """A running a2m mock backend answering 200 {"id":7} as JSON."""
    from a2m.verify.mock_backend import MockBackend

    mock = MockBackend(default_status=200, default_headers=dict(JSON_HEADERS), default_body=BACKEND_BODY)
    mock.start()
    try:
        yield mock
    finally:
        mock.stop()


def verify(gen: Generated, runner: Any, backend: Any, *, golden: Path | None = None, config: Any = None) -> Any:
    from a2m.verify import verify_proxy

    return verify_proxy(gen.bundle, gen.app_dir, runner=runner, backend=backend, golden=golden, config=config)


def describe(result: Any) -> str:
    cases = [(c.name, c.passed, c.diff) for c in result.cases]
    return (
        f"type={result.type} ran={result.ran} passed={result.passed} failed={result.failed} "
        f"message={result.message!r} cases={cases}"
    )


def case_of(battery: Any, policy_type: str, situation: str) -> Any:
    found = [c for c in battery.cases if c.policy_type == policy_type and c.situation == situation]
    assert len(found) == 1, [(c.policy_type, c.situation) for c in battery.cases]
    return found[0]


def result_case(result: Any, situation: str | None = None, name: str | None = None) -> Any:
    found = [
        c for c in result.cases if (situation is None or c.situation == situation) and (name is None or c.name == name)
    ]
    assert len(found) == 1, describe(result)
    return found[0]


def header(headers: Mapping[str, Any], name: str) -> Any:
    matches = [v for k, v in dict(headers).items() if str(k).lower() == name.lower()]
    assert len(matches) <= 1, headers
    return matches[0] if matches else "<absent>"


def query_of(request: Any) -> dict[str, list[str]]:
    return parse_qs(urlsplit(request.path).query, keep_blank_values=True)


def backend_line(diff: str, expected: int, got: int) -> bool:
    """True when one line of ``diff`` names the backend with the expected and then the actual call count."""
    pattern = rf"(?i)backend[^\n]*\b{expected}\b[^\n]*\b{got}\b"
    return re.search(pattern, diff) is not None


def http_call(
    port: int, method: str, path: str, headers: Mapping[str, str] | None = None, body: bytes | None = None
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=dict(headers or {}))
        got = conn.getresponse()
        return got.status, dict(got.getheaders()), got.read()
    finally:
        conn.close()


def write_exchange(folder: Path, file_name: str, exchange: Mapping[str, Any]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / file_name
    path.write_text(json.dumps(exchange, indent=2), encoding="utf-8")
    return path


def call_entry(
    request_headers: Mapping[str, str], status: int, body: bytes, *, path: str = "/orders/7", after_ms: int = 0
) -> dict[str, Any]:
    return {
        "after_ms": after_ms,
        "request": {"method": "GET", "path": path, "headers": dict(request_headers), "body": ""},
        "response": {"status": status, "headers": dict(JSON_HEADERS), "body": body.decode()},
    }


def backend_entry(path: str = "/orders/7") -> dict[str, Any]:
    return {
        "method": "GET",
        "path": path,
        "headers": {"X-Client": "a2m"},
        "response": {"status": 200, "headers": dict(JSON_HEADERS), "body": BACKEND_BODY.decode()},
    }


def write_orders_golden(golden_root: Path, proxy: str = "orders-v1", *, missing_key_status: int = 401) -> Path:
    """The 3 recorded orders-v1 exchanges: valid key, missing key, and the 4th call inside the spike pace."""
    folder = golden_root / proxy
    key = {"x-api-key": GOOD_KEY}
    write_exchange(
        folder,
        "01-valid-key.json",
        {
            "name": "valid-key",
            "calls": [call_entry(key, 200, BACKEND_BODY, after_ms=0)],
            "backend_calls": [backend_entry()],
        },
    )
    write_exchange(
        folder,
        "02-missing-key.json",
        {"name": "missing-key", "calls": [call_entry({}, missing_key_status, missing_key_body())], "backend_calls": []},
    )
    write_exchange(
        folder,
        "03-spike-over-limit.json",
        {
            "name": "spike-over-limit",
            "calls": [
                call_entry(key, 200, BACKEND_BODY, after_ms=600),
                call_entry(key, 200, BACKEND_BODY, after_ms=600),
                call_entry(key, 200, BACKEND_BODY, after_ms=600),
                call_entry(key, 429, SPIKE_BODY, after_ms=0),
            ],
            "backend_calls": [backend_entry(), backend_entry(), backend_entry()],
        },
    )
    return golden_root


ORDERS_GOLDEN_REQUESTS = [("GET", "/orders/7", GOOD_KEY), ("GET", "/orders/7", None)] + [
    ("GET", "/orders/7", GOOD_KEY)
] * 4


def verification_json(results: Path, proxy: str) -> dict[str, Any]:
    found = [p for p in results.rglob("verification.json") if p.parent.name == proxy]
    assert len(found) == 1, sorted(str(p) for p in results.rglob("*") if p.is_file())
    data = json.loads(found[0].read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def run_log(results: Path) -> str:
    return (results / "run.log").read_text(encoding="utf-8")


def proxy_lines(log: str, proxy: str) -> list[str]:
    return [line for line in log.splitlines() if proxy in line]


def fill_key_stage(context: Any) -> None:
    fill_key(Path(context.out_dir) / "mule-app")


def stages_with(runner: Any, config: Any = None) -> list[Callable[[Any], None]]:
    from a2m.engine import generate, parse
    from a2m.verify import make_verify_stage

    return [parse, generate, fill_key_stage, make_verify_stage(runner=runner, config=config)]


def claims_golden_or_battery(results: Path, proxy: str) -> list[str]:
    """Files under ``results`` (outside the work area) that label ``proxy`` golden or battery."""
    hits = []
    for path in sorted(results.rglob("*")):
        if not path.is_file() or ".a2m-work" in path.parts or path.suffix in (".jar", ".class"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        relevant = text if proxy in path.parts else "\n".join(line for line in text.splitlines() if proxy in line)
        if CLAIMED_TYPE.search(relevant):
            hits.append(str(path))
    return hits


# ---------------------------------------------------------------- tool stubs


def tool_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    stubs: Sequence[str] = (),
    failing_mvn: bool = False,
    mule_home: str | None = None,
) -> Path:
    """PATH = one tmp bin folder holding ``stubs``; MULE_HOME, A2M_MULE_HOME, JAVA_HOME removed. Returns the call log.

    ``mule_home``: None leaves A2M_MULE_HOME unset, "empty" points it at an empty folder, "stub" at a folder
    with the lib/boot jar a Mule 4 install has (plus its empty services/ and conf/). a2m runs Mule's JVM
    directly, so the JVM a run starts there is the ``java`` stub on PATH.
    """
    calls = tmp_path / "tool-calls.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for tool in stubs:
        write_stub(bin_dir / tool, calls, fail=(tool == "mvn" and failing_mvn))
    for name in ("MULE_HOME", "A2M_MULE_HOME", "JAVA_HOME", "MAVEN_HOME", "M2_HOME", "MULE_BASE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PATH", str(bin_dir))
    if mule_home is not None:
        home = tmp_path / f"mule-home-{mule_home}"
        home.mkdir(exist_ok=True)
        if mule_home == "stub":
            write_mule_boot(home)
        monkeypatch.setenv("A2M_MULE_HOME", str(home))
    return calls


def write_mule_boot(home: Path) -> None:
    """Make ``home`` look like a Mule 4 standalone install: services/, conf/ and lib/boot with its boot jar."""
    for sub in ("services", "conf", "lib/boot"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    (home / "lib" / "boot" / "mule-module-reboot-4.9.0.jar").write_bytes(b"")


def write_stub(path: Path, calls: Path, *, fail: bool = False) -> None:
    lines = ["#!/bin/sh", f'printf \'%s\\t%s\\t%s\\n\' "${{0##*/}}" "$PWD" "$*" >> \'{calls}\'']
    if fail:
        lines += ["echo '[INFO] Scanning for projects...'", "echo '[ERROR] BUILD FAILURE'", "exit 1"]
    else:
        lines.append("exit 0")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o755)


def tool_calls(calls: Path) -> list[tuple[str, str, str]]:
    if not calls.exists():
        return []
    rows = []
    for line in calls.read_text(encoding="utf-8").splitlines():
        tool, cwd, args = (line.split("\t") + ["", ""])[:3]
        rows.append((tool, cwd, args))
    return rows


def orders_input(tmp_path: Path, *writers: Callable[[Path], Path]) -> Path:
    exports = tmp_path / "in"
    exports.mkdir(exist_ok=True)
    for writer in writers or (write_orders,):
        writer(exports)
    return exports


# ================================================================ CP7-T01 .. T04: tools and the clean skip


def test_CP7_T01_without_tools_the_run_skips_and_labels_the_proxy_static(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_cli: Any
) -> None:
    """[CP7-T01] Without Java, Maven or Mule the run says the step was skipped and labels the proxy static."""
    tool_env(monkeypatch, tmp_path)
    results = tmp_path / "results"

    res = run_cli(["migrate", str(orders_input(tmp_path)), "--out", str(results), "--llm", "fake", "--mock-backends"])

    assert res.code == 0, res.err
    log = run_log(results)
    lines = proxy_lines(log, "orders-v1")
    skip_lines = [
        line
        for line in lines
        if "verification type: static" in line
        and re.search(r"(?i)skipp", line)
        and re.search(r"(?i)not installed", line)
        and re.search(r"Maven|Java|Mule", line)
    ]
    assert len(skip_lines) >= 1, log
    data = verification_json(results, "orders-v1")
    assert data["type"] == "static"
    assert data["ran"] == 0
    assert data["passed"] == 0
    for text in (res.out, res.err, log):
        assert "Traceback" not in text
    assert claims_golden_or_battery(results, "orders-v1") == []


def test_CP7_T02_no_runtime_forces_static_and_never_calls_the_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_cli: Any
) -> None:
    """[CP7-T02] --no-runtime forces static even when the tools are installed, and never calls them."""
    calls = tool_env(monkeypatch, tmp_path, stubs=("mvn", "java"), mule_home="stub")
    results = tmp_path / "results"

    res = run_cli(
        [
            "migrate",
            str(orders_input(tmp_path)),
            "--out",
            str(results),
            "--llm",
            "fake",
            "--mock-backends",
            "--no-runtime",
        ]
    )

    assert res.code == 0, res.err
    assert verification_json(results, "orders-v1")["type"] == "static"
    log = run_log(results)
    disabled = [
        line for line in proxy_lines(log, "orders-v1") if "--no-runtime" in line and "verification type: static" in line
    ]
    assert len(disabled) >= 1, log
    assert tool_calls(calls) == []


def test_CP7_T03_some_tools_missing_gives_static_naming_the_missing_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_cli: Any
) -> None:
    """[CP7-T03] Having only some of the tools still gives static, and the message names the one that is missing."""
    from a2m.verify.tools import detect_tools

    # case A: java and mvn on PATH, A2M_MULE_HOME is an empty folder
    tool_env(monkeypatch, tmp_path, stubs=("java", "mvn"), mule_home="empty")
    status = detect_tools()
    assert tuple(status.missing) == ("Mule",)
    assert status.mule_home is None

    results = tmp_path / "results"
    res = run_cli(["migrate", str(orders_input(tmp_path)), "--out", str(results), "--llm", "fake", "--mock-backends"])

    assert res.code == 0, res.err
    data = verification_json(results, "orders-v1")
    assert data["type"] == "static"
    assert data["ran"] == 0
    assert "Mule" in data["message"], data["message"]
    assert "Maven" not in data["message"] and "Java" not in data["message"], data["message"]

    # case B: A2M_MULE_HOME holds lib/boot/mule-module-reboot-*.jar
    (tmp_path / "b").mkdir()
    tool_env(monkeypatch, tmp_path / "b", stubs=("java", "mvn"), mule_home="stub")
    status_b = detect_tools()
    assert tuple(status_b.missing) == ()
    assert status_b.mule_home is not None
    assert Path(status_b.mule_home).resolve() == (tmp_path / "b" / "mule-home-stub").resolve()


def test_CP7_T04_real_runner_builds_with_maven_in_the_project_and_a_failed_build_is_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: Any
) -> None:
    """[CP7-T04] The real runner builds with Maven in the generated project folder; a failed build is labelled failed."""
    from a2m.verify.runner import MuleAppRunner

    gen = generated(write_orders(tmp_path / "bundles"), tmp_path / "out")

    # first run: mvn succeeds (the stub builds nothing)
    (tmp_path / "ok").mkdir()
    calls = tool_env(monkeypatch, tmp_path / "ok", stubs=("mvn", "java"), mule_home="stub")
    runner = MuleAppRunner(mule_home=Path(os.environ["A2M_MULE_HOME"]), mule_base=tmp_path / "base-ok")
    try:
        verify(gen, runner, backend)
    finally:
        runner.close()
    mvn = [row for row in tool_calls(calls) if row[0] == "mvn"]
    assert len(mvn) == 1, tool_calls(calls)
    assert "package" in mvn[0][2].split(), mvn
    assert Path(mvn[0][1]).resolve() == gen.app_dir.resolve()

    # second run: mvn exits 1 printing BUILD FAILURE
    (tmp_path / "bad").mkdir()
    tool_env(monkeypatch, tmp_path / "bad", stubs=("mvn", "java"), failing_mvn=True, mule_home="stub")
    runner = MuleAppRunner(mule_home=Path(os.environ["A2M_MULE_HOME"]), mule_base=tmp_path / "base-bad")
    try:
        result = verify(gen, runner, backend)
    finally:
        runner.close()
    assert result.type == "failed", describe(result)
    assert re.search(r"(?i)build failed|failed to build", result.message), result.message
    assert "BUILD FAILURE" in result.log_excerpt, result.log_excerpt
    assert result.ran == 0
    assert result.passed == 0
    assert backend.calls() == []


# ================================================================ CP7-T05 .. T07: the mock backend


def listening_addresses(port: int) -> set[str]:
    """Local addresses with a TCP socket listening on ``port`` (from /proc/net/tcp and tcp6)."""
    found: set[str] = set()
    for table, v6 in (("/proc/net/tcp", False), ("/proc/net/tcp6", True)):
        try:
            rows = Path(table).read_text(encoding="ascii").splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            fields = row.split()
            local, state = fields[1], fields[3]
            addr_hex, port_hex = local.split(":")
            if state != "0A" or int(port_hex, 16) != port:
                continue
            if v6:
                found.add("v6:" + addr_hex)
            else:
                found.add(socket.inet_ntoa(bytes.fromhex(addr_hex)[::-1]))
    return found


def test_CP7_T05_mock_backend_answers_and_records_every_call_in_order() -> None:
    """[CP7-T05] The mock backend answers requests and remembers every call in order."""
    from a2m.verify.mock_backend import MockBackend

    mock = MockBackend()
    other = MockBackend()
    mock.start()
    other.start()
    try:
        assert mock.host == "127.0.0.1"
        assert 0 < mock.port < 65536
        assert mock.port != other.port
        assert mock.url == f"http://127.0.0.1:{mock.port}"
        assert listening_addresses(mock.port) == {"127.0.0.1"}

        first = http_call(mock.port, "GET", "/orders?id=7", {"X-Trace": "abc"})
        second = http_call(mock.port, "POST", "/orders", {"Content-Type": "application/json"}, b'{"qty":2}')

        assert 200 <= first[0] < 300 and 200 <= second[0] < 300, (first, second)
        calls = mock.calls()
        assert [(c.method, c.path) for c in calls] == [("GET", "/orders"), ("POST", "/orders")]
        assert calls[0].query == "id=7"
        assert calls[0].headers["x-trace"] == "abc"
        assert calls[1].query == ""
        assert calls[1].body == b'{"qty":2}'
    finally:
        mock.stop()
        other.stop()


def test_CP7_T06_mock_backend_can_be_programmed_cleared_and_stopped() -> None:
    """[CP7-T06] The mock backend can be told what to answer, cleared between tests and shut down."""
    from a2m.verify.mock_backend import MockBackend

    mock = MockBackend()
    mock.start()
    port = mock.port
    try:
        mock.respond("GET", "/fail", status=503, headers={"Retry-After": "5"}, body=b"down")

        status, headers, body = http_call(port, "GET", "/fail")

        assert status == 503
        assert header(headers, "Retry-After") == "5"
        assert body == b"down"
        assert [(c.method, c.path) for c in mock.calls()] == [("GET", "/fail")]

        mock.clear()
        assert mock.calls() == []
        http_call(port, "GET", "/other")
        assert [(c.method, c.path) for c in mock.calls()] == [("GET", "/other")]
    finally:
        mock.stop()

    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", port), timeout=5).close()
    assert [(c.method, c.path) for c in mock.calls()] == [("GET", "/other")]


def test_CP7_T07_mock_backend_records_every_call_under_concurrency() -> None:
    """[CP7-T07] The mock backend records every call when many arrive at once."""
    from a2m.verify.mock_backend import MockBackend

    mock = MockBackend()
    mock.start()
    try:
        with ThreadPoolExecutor(max_workers=10) as pool:
            statuses = list(pool.map(lambda i: http_call(mock.port, "GET", f"/c/{i}")[0], range(20)))
        assert len(statuses) == 20 and all(200 <= s < 300 for s in statuses), statuses
        calls = mock.calls()
        assert len(calls) == 20
        assert sorted(c.path for c in calls) == sorted(f"/c/{i}" for i in range(20))
    finally:
        mock.stop()


# ================================================================ CP7-T08 .. T09: batteries


def build(gen: Generated) -> Any:
    from a2m.verify.batteries import build_battery

    return build_battery(gen.bundle, gen.app_dir)


def last_status(case: Any) -> int:
    return int(case.calls[-1].expected_status)


def test_CP7_T08_each_standard_policy_type_gets_its_own_cases(tmp_path: Path) -> None:
    """[CP7-T08] Each standard policy type gets its own set of test cases."""
    batteries = {t: build(generated(write_type_proxy(tmp_path / "bundles", t), tmp_path / "out")) for t in TYPE_PROXIES}

    for policy_type, battery in batteries.items():
        mine = [c for c in battery.cases if c.policy_type == policy_type]
        assert len(mine) >= 1, (policy_type, [(c.policy_type, c.situation) for c in battery.cases])
        base = f"/type-{policy_type.lower()}"
        for case in battery.cases:
            assert len(case.calls) >= 1, case
            for call in case.calls:
                assert call.request.method in ("GET", "POST", "PUT", "DELETE", "PATCH"), call
                assert urlsplit(call.request.path).path.startswith(base), (base, call.request.path)
                assert isinstance(call.expected_status, int) and 100 <= call.expected_status < 600, call
            assert isinstance(case.expected_backend_calls, int) and case.expected_backend_calls >= 0, case

    spike = batteries["SpikeArrest"]
    assert last_status(case_of(spike, "SpikeArrest", "under-limit")) == 200
    over = case_of(spike, "SpikeArrest", "over-limit")
    assert last_status(over) == 429 and len(over.calls) >= 2

    quota = batteries["Quota"]
    assert last_status(case_of(quota, "Quota", "within-allowance")) == 200
    assert last_status(case_of(quota, "Quota", "over-allowance")) == 429

    keys = batteries["VerifyAPIKey"]
    valid = case_of(keys, "VerifyAPIKey", "valid-key")
    missing = case_of(keys, "VerifyAPIKey", "missing-key")
    bad = case_of(keys, "VerifyAPIKey", "bad-key")
    assert (last_status(valid), valid.expected_backend_calls) == (200, 1)
    assert (last_status(missing), missing.expected_backend_calls) == (401, 0)
    assert (last_status(bad), bad.expected_backend_calls) == (401, 0)

    assign = batteries["AssignMessage"]
    header_set = case_of(assign, "AssignMessage", "header-set")
    assert header(header_set.expected_backend_headers, "X-Client") == "a2m"
    assert header_set.expected_backend_calls == 1
    cond_false = case_of(assign, "AssignMessage", "condition-false")
    assert header(cond_false.expected_backend_headers, "X-Client") is None
    assert cond_false.expected_backend_calls == 1

    extract = case_of(batteries["ExtractVariables"], "ExtractVariables", "extracted-value-used")
    assert (last_status(extract), extract.expected_backend_calls) == (200, 1)

    fault = case_of(batteries["RaiseFault"], "RaiseFault", "fault-raised")
    assert last_status(fault) == 418
    assert fault.calls[-1].expected_body is not None
    assert json.loads(fault.calls[-1].expected_body) == {"error": "teapot"}
    assert fault.expected_backend_calls == 0

    basic = batteries["BasicAuthentication"]
    correct = case_of(basic, "BasicAuthentication", "correct-credentials")
    assert (last_status(correct), correct.expected_backend_calls) == (200, 1)
    assert str(header(correct.calls[-1].request.headers, "Authorization")).startswith("Basic ")
    no_creds = case_of(basic, "BasicAuthentication", "missing-credentials")
    assert (last_status(no_creds), no_creds.expected_backend_calls) == (500, 0)
    assert header(no_creds.calls[-1].request.headers, "Authorization") == "<absent>"

    denied = case_of(batteries["AccessControl"], "AccessControl", "denied-address")
    assert (last_status(denied), denied.expected_backend_calls) == (403, 0)


def test_CP7_T09_cases_use_the_limits_and_key_location_from_the_proxy(tmp_path: Path) -> None:
    """[CP7-T09] Test cases use the limits and names from the proxy's own settings."""
    orders = build(generated(write_orders(tmp_path / "a"), tmp_path / "out-a"))
    copy = build(
        generated(write_orders(tmp_path / "b", rate="5ps", key_ref="request.queryparam.apikey"), tmp_path / "out-b")
    )

    over = case_of(orders, "SpikeArrest", "over-limit")
    assert [c.expected_status for c in over.calls] == [200, 200, 200, 429]
    assert over.expected_backend_calls == 3
    for situation in ("valid-key", "bad-key"):
        req = case_of(orders, "VerifyAPIKey", situation).calls[-1].request
        assert header(req.headers, "x-api-key") != "<absent>", req
        assert "apikey" not in query_of(req), req
    assert header(case_of(orders, "VerifyAPIKey", "valid-key").calls[-1].request.headers, "x-api-key") == GOOD_KEY
    assert header(case_of(orders, "VerifyAPIKey", "bad-key").calls[-1].request.headers, "x-api-key") != GOOD_KEY
    assert header(case_of(orders, "VerifyAPIKey", "missing-key").calls[-1].request.headers, "x-api-key") == "<absent>"

    over5 = case_of(copy, "SpikeArrest", "over-limit")
    assert [c.expected_status for c in over5.calls] == [200, 200, 200, 200, 200, 429]
    assert over5.expected_backend_calls == 5
    valid = case_of(copy, "VerifyAPIKey", "valid-key").calls[-1].request
    assert query_of(valid).get("apikey") == [GOOD_KEY], valid
    assert header(valid.headers, "x-api-key") == "<absent>"
    bad = case_of(copy, "VerifyAPIKey", "bad-key").calls[-1].request
    assert len(query_of(bad).get("apikey", [])) == 1 and query_of(bad)["apikey"] != [GOOD_KEY], bad
    assert "apikey" not in query_of(case_of(copy, "VerifyAPIKey", "missing-key").calls[-1].request)


# ================================================================ CP7-T10 .. T20: verification decisions


def test_CP7_T10_a_policy_without_cases_is_untested_and_zero_tests_is_never_battery(
    tmp_path: Path, backend: Any
) -> None:
    """[CP7-T10] A policy with no test cases is listed as untested, and zero tests can never count as battery."""
    gen = generated(write_oauth_only(tmp_path / "bundles"), tmp_path / "out")

    result = verify(gen, FakeRunner("correct"), backend)

    assert result.type == "static", describe(result)
    assert (result.ran, result.passed, result.failed) == (0, 0, 0)
    untested = [(u.name, u.type) for u in result.untested]
    assert untested == [("oauth", "OAuthV2")]
    assert result.untested[0].reason.strip() != ""


def test_CP7_T11_golden_recordings_are_replayed_instead_of_the_battery(tmp_path: Path, backend: Any) -> None:
    """[CP7-T11] With --golden the saved recordings are replayed instead of the battery."""
    gen = generated(write_orders(tmp_path / "bundles"), tmp_path / "out")
    golden = write_orders_golden(tmp_path / "golden")
    runner = FakeRunner("correct")

    result = verify(gen, runner, backend, golden=golden)

    assert result.type == "golden", describe(result)
    assert runner.requests_for("orders-v1") == ORDERS_GOLDEN_REQUESTS
    assert [(c.name, c.passed) for c in result.cases] == [
        ("valid-key", True),
        ("missing-key", True),
        ("spike-over-limit", True),
    ]
    assert (result.ran, result.passed, result.failed) == (3, 3, 0)
    assert len(backend.calls()) == 4
    assert all(c.headers.get("x-client") == "a2m" for c in backend.calls())

    backend.clear()
    battery_run = verify(gen, FakeRunner("correct"), backend)
    assert battery_run.type == "battery", describe(battery_run)
    assert battery_run.ran == len(build(gen).cases) and battery_run.ran > 0


def test_CP7_T12_missing_or_broken_golden_recordings_never_give_golden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_cli: Any
) -> None:
    """[CP7-T12] Missing or broken golden recordings never produce a golden label."""
    golden = write_orders_golden(tmp_path / "golden")
    broken = golden / "broken-golden"
    broken.mkdir()
    (broken / "01-valid-key.json").write_text('{"name": "valid-key", "calls": [{"request": {"meth', encoding="utf-8")
    exports = orders_input(tmp_path, write_orders, write_quota, lambda parent: write_orders(parent, "broken-golden"))
    results = tmp_path / "results"
    runner = FakeRunner("correct")

    res = run_cli(
        ["migrate", str(exports), "--out", str(results), "--llm", "fake", "--mock-backends", "--golden", str(golden)],
        stages=stages_with(runner),
    )

    assert res.code == 0, res.err
    log = run_log(results)
    # (a) no recordings for quota-v1: its battery runs instead
    quota = verification_json(results, "quota-v1")
    assert quota["type"] == "battery", quota
    assert any(re.search(r"(?i)no\b[^\n]*recording", line) for line in proxy_lines(log, "quota-v1")), log
    # (b) the broken recording is named, and orders-v1 is still golden after it
    broken_result = verification_json(results, "broken-golden")
    assert broken_result["type"] != "golden", broken_result
    assert "01-valid-key.json" in broken_result["message"], broken_result
    assert verification_json(results, "orders-v1")["type"] == "golden"
    assert "Traceback" not in res.err

    # (c) --golden naming a folder that does not exist
    missing = tmp_path / "no-such-golden"
    results_c = tmp_path / "results-c"
    res_c = run_cli(
        ["migrate", str(exports), "--out", str(results_c), "--llm", "fake", "--mock-backends", "--golden", str(missing)]
    )
    assert res_c.code == 2, (res_c.code, res_c.err)
    err_lines = [line for line in res_c.err.splitlines() if line.strip()]
    assert len(err_lines) == 1 and str(missing) in err_lines[0], res_c.err
    processed = sorted(str(p) for p in results_c.rglob("*") if p.name in (".done", "mule-app", "verification.json"))
    assert processed == []


def compare(expected: Any, actual: Any, **kwargs: Any) -> Any:
    from a2m.verify.compare import compare_response

    return compare_response(expected, actual, **kwargs)


def test_CP7_T13_status_header_and_body_differences_give_a_readable_diff() -> None:
    """[CP7-T13] Differences in status, headers or body show up as a readable diff."""
    expected = response(
        200,
        {"Content-Type": "application/json", "X-RateLimit-Remaining": "2"},
        b'{"id":7,"name":"pen"}',
    )

    same = compare(
        expected,
        response(200, {"content-type": "application/json", "x-ratelimit-remaining": "2"}, b'{"name": "pen", "id": 7}'),
    )
    assert same.matched is True
    assert same.diff == ""

    status = compare(
        expected,
        response(500, {"Content-Type": "application/json", "X-RateLimit-Remaining": "2"}, b'{"id":7,"name":"pen"}'),
    )
    assert status.matched is False
    assert re.search(r"(?i)status", status.diff) and "200" in status.diff and "500" in status.diff, status.diff

    no_header = compare(expected, response(200, {"Content-Type": "application/json"}, b'{"id":7,"name":"pen"}'))
    assert no_header.matched is False
    assert "x-ratelimit-remaining" in no_header.diff.lower() and "2" in no_header.diff, no_header.diff
    assert "missing" in no_header.diff.lower(), no_header.diff

    body = compare(
        expected,
        response(200, {"Content-Type": "application/json", "X-RateLimit-Remaining": "2"}, b'{"id":7,"name":"cup"}'),
    )
    assert body.matched is False
    assert "name" in body.diff and "pen" in body.diff and "cup" in body.diff, body.diff

    text = compare(
        response(200, {"Content-Type": "text/plain"}, b"ok"), response(200, {"Content-Type": "text/plain"}, b"okay")
    )
    assert text.matched is False
    assert re.search(r"\bok\b", text.diff), text.diff
    assert "okay" in text.diff, text.diff


def test_CP7_T14_backend_calls_are_compared_so_a_leaky_rejection_fails(tmp_path: Path, backend: Any) -> None:
    """[CP7-T14] The calls the backend received are compared too, so a rejected request that still reached it fails."""
    gen = generated(write_orders(tmp_path / "bundles"), tmp_path / "out")

    leaky = verify(gen, FakeRunner("leaky-key"), backend)

    assert leaky.type == "failed", describe(leaky)
    missing = result_case(leaky, "missing-key")
    assert missing.passed is False
    assert missing.backend_calls == 1
    assert backend_line(missing.diff, 0, 1), missing.diff

    backend.clear()
    no_header = verify(gen, FakeRunner("no-header"), backend)

    assert no_header.type == "failed", describe(no_header)
    valid = result_case(no_header, "valid-key")
    assert valid.passed is False
    assert "x-client" in valid.diff.lower() and "a2m" in valid.diff and "missing" in valid.diff.lower(), valid.diff


def test_CP7_T15_caching_and_rate_limits_are_checked_by_counting_backend_hits(tmp_path: Path, backend: Any) -> None:
    """[CP7-T15] Caching and rate limits are checked by counting backend hits over a series of calls."""
    cache_gen = generated(write_cache(tmp_path / "bundles"), tmp_path / "out")
    orders_gen = generated(write_orders(tmp_path / "bundles"), tmp_path / "out")

    cache_battery = build(cache_gen)
    cache_cases = [c for c in cache_battery.cases if c.policy_type == "ResponseCache"]
    assert cache_cases == [], [(c.policy_type, c.situation) for c in cache_battery.cases]
    cache_untested = [u for u in cache_battery.untested if u.type == "ResponseCache"]
    assert len(cache_untested) == 1, cache_battery.untested
    assert cache_untested[0].reason.startswith("not generated by a2m"), cache_untested[0].reason
    over = case_of(build(orders_gen), "SpikeArrest", "over-limit")
    assert (len(over.calls), over.expected_backend_calls) == (4, 3)

    cache_runner = FakeRunner("broken-cache")
    cache_result = verify(cache_gen, cache_runner, backend)
    assert cache_runner.sent == [], cache_runner.sent
    assert [c for c in cache_result.cases if c.policy_type == "ResponseCache"] == [], describe(cache_result)
    assert any(
        u.type == "ResponseCache" and u.reason.startswith("not generated by a2m") for u in cache_result.untested
    ), cache_result.untested
    backend.clear()
    assert verify(orders_gen, FakeRunner("correct"), backend).type == "battery"

    backend.clear()
    broken_spike = verify(orders_gen, FakeRunner("broken-spike"), backend)
    assert broken_spike.type == "failed", describe(broken_spike)
    spike = result_case(broken_spike, "over-limit")
    assert spike.passed is False and spike.backend_calls == 4
    assert "429" in spike.diff and re.search(r"(?i)call\s*#?\s*4\b|\b4(th)?\s+call", spike.diff), spike.diff
    assert backend_line(spike.diff, 3, 4), spike.diff


def test_CP7_T16_quota_and_spike_arrest_are_flagged_for_review(tmp_path: Path, backend: Any) -> None:
    """[CP7-T16] Quota and SpikeArrest are flagged for human review because their time windows can't be proven."""
    quota = verify(generated(write_quota(tmp_path / "b"), tmp_path / "o"), FakeRunner("correct"), backend)
    backend.clear()
    orders = verify(generated(write_orders(tmp_path / "b"), tmp_path / "o"), FakeRunner("correct"), backend)
    backend.clear()
    keys = verify(generated(write_keys(tmp_path / "b"), tmp_path / "o"), FakeRunner("correct"), backend)

    for result in (quota, orders, keys):
        assert result.ran > 0 and result.failed == 0 and result.passed == result.ran, describe(result)
    quota_flags = [f for f in quota.review_flags if f.policy == "quota"]
    assert len(quota_flags) == 1, quota.review_flags
    assert quota_flags[0].policy_type == "Quota"
    assert re.search(r"(?i)\b1\s*hour", quota_flags[0].reason), quota_flags[0].reason
    spike_flags = [f for f in orders.review_flags if f.policy == "spike"]
    assert len(spike_flags) == 1, orders.review_flags
    assert spike_flags[0].policy_type == "SpikeArrest"
    assert re.search(r"(?i)\b3\s*(per\s+second|/\s*s(ec(ond)?)?\b|ps\b)", spike_flags[0].reason), spike_flags[0].reason
    assert [f for f in keys.review_flags if f.policy_type in TIME_WINDOW_TYPES] == []


def test_CP7_T17_built_but_never_run_is_static_not_battery(tmp_path: Path, backend: Any) -> None:
    """[CP7-T17] An app that was built but never run is labelled static, not battery."""
    gen = generated(write_orders(tmp_path / "bundles"), tmp_path / "out")
    runner = FakeRunner("built-not-run")

    result = verify(gen, runner, backend)

    assert result.type == "static", describe(result)
    assert (result.ran, result.passed) == (0, 0)
    assert backend.calls() == []
    assert runner.sent == []
    assert re.search(r"(?i)built\b.*\bnot\s+(run|started)", result.message), result.message


def test_CP7_T18_battery_and_golden_require_every_test_to_run_and_pass(tmp_path: Path, backend: Any) -> None:
    """[CP7-T18] Battery and golden labels require every test to actually run and pass."""
    gen = generated(write_orders(tmp_path / "bundles"), tmp_path / "out")
    total = len(build(gen).cases)

    good = verify(gen, FakeRunner("correct"), backend)
    assert good.type == "battery", describe(good)
    assert (good.ran, good.passed, good.failed) == (total, total, 0)
    assert all(c.passed for c in good.cases) and len(good.cases) == total

    backend.clear()
    one_bad = verify(gen, FakeRunner("broken-spike"), backend)
    assert one_bad.type == "failed", describe(one_bad)
    assert (one_bad.ran, one_bad.passed, one_bad.failed) == (total, total - 1, 1)
    failing = [c for c in one_bad.cases if not c.passed]
    assert [c.situation for c in failing] == ["over-limit"]
    assert failing[0].name.strip() != "" and "429" in failing[0].diff, failing[0]

    backend.clear()
    golden = write_orders_golden(tmp_path / "golden", missing_key_status=403)
    mismatch = verify(gen, FakeRunner("correct"), backend, golden=golden)
    assert mismatch.type == "failed", describe(mismatch)
    bad_exchange = [c for c in mismatch.cases if not c.passed]
    assert [c.name for c in bad_exchange] == ["missing-key"]
    assert "403" in bad_exchange[0].diff and "401" in bad_exchange[0].diff, bad_exchange[0].diff
    assert (mismatch.ran, mismatch.passed, mismatch.failed) == (3, 2, 1)


def test_CP7_T19_an_app_that_fails_to_start_is_failed_and_the_batch_carries_on(tmp_path: Path, run_cli: Any) -> None:
    """[CP7-T19] An app that fails to start is labelled failed for that proxy and the batch carries on."""
    exports = orders_input(tmp_path, write_orders, write_quota)
    results = tmp_path / "results"
    runner = FakeRunner("correct", overrides={"orders-v1": "start-fails"})

    res = run_cli(
        ["migrate", str(exports), "--out", str(results), "--llm", "fake", "--mock-backends"], stages=stages_with(runner)
    )

    assert res.code == 0, res.err
    assert "Traceback" not in res.err
    orders = verification_json(results, "orders-v1")
    assert orders["type"] == "failed", orders
    assert "Address already in use" in orders["message"], orders
    assert orders["ran"] == 0 and orders["passed"] == 0
    assert any("Address already in use" in line for line in proxy_lines(run_log(results), "orders-v1"))
    backend_url = urlsplit(runner.backend_urls["orders-v1"])
    assert backend_url.hostname == "127.0.0.1"
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", int(backend_url.port or 0)), timeout=5).close()
    quota = verification_json(results, "quota-v1")
    assert quota["type"] == "battery", quota
    assert len([p for p in results.rglob(".done") if p.parent.name == "quota-v1"]) == 1


def test_CP7_T20_golden_ignores_volatile_headers_and_the_list_can_be_extended(tmp_path: Path) -> None:
    """[CP7-T20] Golden replay ignores headers that always change, and the ignore list can be extended."""
    from a2m.verify.compare import DEFAULT_IGNORED_HEADERS

    from a2m.verify import VerifyConfig

    recorded = response(
        200,
        {
            "Date": "Mon, 01 Sep 2026 10:00:00 GMT",
            "Server": "Apigee-Router",
            "X-Request-ID": "abc-1",
            "Content-Length": "9",
            "X-Custom": "one",
        },
        b'{"id":7}',
    )
    live_headers = {
        "Date": "Thu, 01 Oct 2026 12:00:00 GMT",
        "Server": "Mule",
        "x-request-id": "zzz-9",
        "Content-Length": "11",
        "X-Custom": "one",
    }
    lowered = {h.lower() for h in DEFAULT_IGNORED_HEADERS}
    assert {"date", "server", "content-length", "transfer-encoding", "connection", "x-request-id"} <= lowered
    assert {"x-correlation-id", "keep-alive"} <= lowered

    same = compare(recorded, response(200, live_headers, b'{ "id": 7 }'))
    assert same.matched is True and same.diff == "", same.diff

    changed = compare(recorded, response(200, {**live_headers, "X-Custom": "two"}, b'{"id":7}'))
    assert changed.matched is False
    assert "x-custom" in changed.diff.lower() and "one" in changed.diff and "two" in changed.diff, changed.diff
    for volatile in ("date", "server", "x-request-id", "content-length"):
        assert volatile not in changed.diff.lower(), changed.diff

    extended = VerifyConfig(ignore_headers=(*DEFAULT_IGNORED_HEADERS, "X-Custom"))
    again = compare(
        recorded,
        response(200, {**live_headers, "X-Custom": "two"}, b'{"id":7}'),
        ignore_headers=extended.ignore_headers,
    )
    assert again.matched is True and again.diff == "", again.diff

    # through the harness: a golden replay with the extended configuration
    from a2m.verify.mock_backend import MockBackend

    gen = generated(write_keys(tmp_path / "bundles"), tmp_path / "out")
    golden = tmp_path / "golden"
    write_exchange(
        golden / "keys-v1",
        "01-valid-key.json",
        {
            "name": "valid-key",
            "calls": [
                {
                    "after_ms": 0,
                    "request": {"method": "GET", "path": "/keys", "headers": {"x-api-key": GOOD_KEY}, "body": ""},
                    "response": {
                        "status": 200,
                        "headers": {"X-Custom": "one", "Date": "Mon, 01 Sep 2026 10:00:00 GMT"},
                        "body": '{"id":7}',
                    },
                }
            ],
            "backend_calls": [
                {
                    "method": "GET",
                    "path": "/keys",
                    "headers": {"X-Client": "a2m"},
                    "response": {"status": 200, "headers": {"X-Custom": "two"}, "body": '{"id":7}'},
                }
            ],
        },
    )
    mock = MockBackend()
    mock.start()
    try:
        default_run = verify(gen, FakeRunner("correct"), mock, golden=golden)
        mock.clear()
        extended_run = verify(gen, FakeRunner("correct"), mock, golden=golden, config=extended)
    finally:
        mock.stop()
    assert default_run.type == "failed", describe(default_run)
    assert "x-custom" in result_case(default_run, name="valid-key").diff.lower()
    assert extended_run.type == "golden", describe(extended_run)


# ================================================================ CP7-T21 .. T22: deploy outcomes from mule.log

# Captured from the real mule-http-connector 1.12.x deploy failure on this machine (Mule Kernel 4.9.0,
# ~/.local/share/mule/mule-standalone-4.9.0/logs/mule.log, Oct 1 2026), app name changed to keys-v1.
VERSION_MISMATCH_LOG = """++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
+ New app 'keys-v1'                                                         +
++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++

++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
+ Initializing app 'keys-v1'                                                +
++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++

++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
+ Disposing application 'keys-v1'                                           +
++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++

++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
+ Failed to deploy artifact 'keys-v1',                                      +
+ org.mule.runtime.deployment.model.api.DeploymentInitException:               +
+ EnumConstantNotPresentException: org.mule.sdk.api.meta.JavaVersion.JAVA_25   +
++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
ERROR 2026-10-01 19:47:29,024 [WrapperListener_start_runner] [processor: ; event: ] org.mule.runtime.module.deployment.internal.DefaultArchiveDeployer: Failed to deploy artifact [keys-v1]
org.mule.runtime.deployment.model.api.DeploymentException: Failed to deploy artifact [keys-v1]
Caused by: org.mule.runtime.api.exception.MuleRuntimeException: org.mule.runtime.deployment.model.api.DeploymentInitException: EnumConstantNotPresentException: org.mule.sdk.api.meta.JavaVersion.JAVA_25
Caused by: org.mule.runtime.deployment.model.api.DeploymentInitException: EnumConstantNotPresentException: org.mule.sdk.api.meta.JavaVersion.JAVA_25
Caused by: org.mule.runtime.core.api.config.ConfigurationException: org.mule.sdk.api.meta.JavaVersion.JAVA_25
Caused by: java.lang.EnumConstantNotPresentException: org.mule.sdk.api.meta.JavaVersion.JAVA_25
\tat java.base/sun.reflect.annotation.EnumConstantNotPresentExceptionProxy.generateException(EnumConstantNotPresentExceptionProxy.java:47) ~[?:?]
\tat java.base/sun.reflect.annotation.AnnotationInvocationHandler.invoke(AnnotationInvocationHandler.java:89) ~[?:?]
\tat jdk.proxy4/jdk.proxy4.$Proxy92.value(Unknown Source) ~[?:?]
\tat org.mule.runtime.extensions.support@4.9.0/org.mule.runtime.module.extension.internal.loader.java.type.runtime.ClassBasedAnnotationValueFetcher.getArrayValue(ClassBasedAnnotationValueFetcher.java:55) ~[mule-module-extensions-support-4.9.0.jar:?]
\tat org.mule.runtime.extensions.support@4.9.0/org.mule.runtime.module.extension.api.loader.java.type.AnnotationValueFetcher.getEnumArrayValue(AnnotationValueFetcher.java:90) ~[mule-module-extensions-support-4.9.0.jar:?]
"""

BAD_CONFIG_LOG = (
    "++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++\n"
    "+ New app 'keys-v1'                                                            +\n"
    "++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++\n"
    "\n"
    "++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++\n"
    "+ Failed to deploy artifact 'keys-v1', see below                               +\n"
    "++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++\n"
    "ERROR 2026-10-02 10:15:03,511 [WrapperListener_start_runner] [processor: ; event: ] "
    "org.mule.runtime.module.deployment.internal.DefaultArchiveDeployer: Failed to deploy artifact [keys-v1]\n"
    "org.mule.runtime.deployment.model.api.DeploymentException: Failed to deploy artifact [keys-v1]\n"
    "Caused by: org.mule.runtime.api.exception.MuleRuntimeException: "
    "org.mule.runtime.deployment.model.api.DeploymentInitException: ConfigurationException: "
    "[proxy.xml:12]: Invalid content was found starting with element 'http:bogus-element'. "
    "One of '{\"http://www.mulesoft.org/schema/mule/http\":request-connection}' is expected.\n"
    "Caused by: org.mule.runtime.core.api.config.ConfigurationException: [proxy.xml:12]: Invalid content was found "
    "starting with element 'http:bogus-element'.\n"
)

STARTED_LOG = (
    "**********************************************************************\n"
    "* Application: keys-v1                                               *\n"
    "* OS encoding: UTF-8, Mule encoding: UTF-8                           *\n"
    "**********************************************************************\n"
    "\n"
    "**********************************************************************\n"
    "* Started app 'keys-v1'                                              *\n"
    "* Application plugins:                                               *\n"
    "*  - HTTP : 1.11.3                                                   *\n"
    "**********************************************************************\n"
)


def test_CP7_T21_connector_runtime_mismatch_is_reported_in_plain_words() -> None:
    """[CP7-T21] A connector that does not match the Mule runtime is reported in plain words, from a real log excerpt."""
    from a2m.verify import explain_deploy_failure

    assert (
        "EnumConstantNotPresentException" in VERSION_MISMATCH_LOG
        and "Failed to deploy artifact" in VERSION_MISMATCH_LOG
    )

    mismatch = explain_deploy_failure("keys-v1", VERSION_MISMATCH_LOG, mule_version="4.9.0")
    bad_config = explain_deploy_failure("keys-v1", BAD_CONFIG_LOG, mule_version="4.9.0")

    assert mismatch.type == "failed"
    assert VERSION_MISMATCH in mismatch.message, mismatch.message
    assert any("EnumConstantNotPresentException" in line for line in mismatch.log_excerpt.splitlines()), (
        mismatch.log_excerpt
    )
    assert bad_config.type == "failed"
    assert "connector version" not in bad_config.message.lower(), bad_config.message
    assert re.search(r"(?i)deploy", bad_config.message), bad_config.message
    assert "Invalid content was found starting with element 'http:bogus-element'" in bad_config.log_excerpt
    assert "EnumConstantNotPresentException" not in bad_config.log_excerpt
    for result in (mismatch, bad_config):
        assert result.message.strip().lower() not in ("failed", "unknown error", "error", "")
        assert "unknown error" not in result.message.lower()
        assert (result.ran, result.passed) == (0, 0)


@pytest.fixture
def mule_base(tmp_path: Path) -> Path:
    base = tmp_path / "mule-base"
    (base / "apps").mkdir(parents=True)
    (base / "logs").mkdir()
    (base / "logs" / "mule.log").write_text("INFO earlier run of the runtime\n", encoding="utf-8")
    return base


def later(seconds: float, action: Callable[[], None]) -> threading.Timer:
    timer = threading.Timer(seconds, action)
    timer.daemon = True
    timer.start()
    return timer


def append_log(base: Path, text: str) -> Callable[[], None]:
    def write() -> None:
        with (base / "logs" / "mule.log").open("a", encoding="utf-8") as log:
            log.write(text)

    return write


def test_CP7_T22_the_runner_knows_started_failed_and_timed_out(mule_base: Path) -> None:
    """[CP7-T22] The runner knows when an app has started, failed to deploy, or timed out."""
    from a2m.verify.mule import DeployError, wait_for_deploy

    # (a) the anchor file appears after 0.5 s
    timer = later(0.5, lambda: (mule_base / "apps" / "keys-v1-anchor.txt").write_text("anchor", encoding="utf-8"))
    started = time.monotonic()
    wait_for_deploy(mule_base, "keys-v1", timeout=2.0)
    assert time.monotonic() - started < 2.0
    timer.join()
    (mule_base / "apps" / "keys-v1-anchor.txt").unlink()

    # (b) 'Started app' is logged
    timer = later(0.3, append_log(mule_base, STARTED_LOG))
    started = time.monotonic()
    wait_for_deploy(mule_base, "keys-v1", timeout=2.0)
    assert time.monotonic() - started < 2.0
    timer.join()

    # (c) a deploy failure is reported well before the timeout
    timer = later(0.3, append_log(mule_base, BAD_CONFIG_LOG))
    started = time.monotonic()
    with pytest.raises(DeployError) as failed:
        wait_for_deploy(mule_base, "keys-v1", timeout=2.0)
    assert time.monotonic() - started < 1.6
    assert "Failed to deploy artifact" in failed.value.log_excerpt
    timer.join()

    # (d) nothing happens, or only another app starts: a start timeout of 2 seconds
    timer = later(0.3, append_log(mule_base, STARTED_LOG.replace("keys-v1", "other-app")))
    started = time.monotonic()
    with pytest.raises(DeployError) as timed_out:
        wait_for_deploy(mule_base, "keys-v1", timeout=2.0)
    assert time.monotonic() - started >= 1.9
    assert re.search(r"(?i)\b2(\.0)?\s*(s|sec|seconds?)\b", str(timed_out.value)), str(timed_out.value)
    assert re.search(r"(?i)start|deploy", str(timed_out.value)), str(timed_out.value)
    timer.join()


# ================================================================ CP7-T29: apps needing Mule Enterprise are never run on CE


def test_CP7_T29_an_app_needing_mule_enterprise_is_honestly_skipped_never_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_cli: Any, backend: Any
) -> None:
    """[CP7-T29] An app with ee: components (requires Mule Enterprise) is skipped as "requires Mule Enterprise runtime".

    Mule Kernel CE cannot deploy it, so verification never starts it, never claims it verified, and labels it static.
    """
    monkeypatch.setenv("A2M_FAKE_LLM_DIR", str(LLM))
    monkeypatch.delenv("A2M_PROMPTS_DIR", raising=False)
    exports = tmp_path / "in"
    shutil.copytree(CP6 / "py-callout", exports / "py-callout")
    results = tmp_path / "results"
    runner = FakeRunner("correct")

    res = run_cli(
        ["migrate", str(exports), "--out", str(results), "--llm", "fake", "--mock-backends"], stages=stages_with(runner)
    )

    assert res.code == 0, res.err
    data = verification_json(results, "py-callout")
    assert data["type"] == "static", data
    assert (data["ran"], data["passed"]) == (0, 0)
    assert "requires Mule Enterprise runtime" in data["message"], data["message"]
    assert runner.started == []
    lines = [
        line
        for line in proxy_lines(run_log(results), "py-callout")
        if "requires Mule Enterprise runtime" in line and "verification type: static" in line
    ]
    assert len(lines) >= 1, run_log(results)
    assert claims_golden_or_battery(results, "py-callout") == []

    # the harness itself refuses too, whoever calls it
    from a2m.generator import generate_project
    from a2m.parser import read_bundle

    bundle = read_bundle(exports / "py-callout")
    app_dir = tmp_path / "direct" / "py-callout" / "mule-app"
    gen_result = generate_project(bundle, app_dir, provider=fake_provider())
    assert gen_result.requires_enterprise is True
    direct_runner = FakeRunner("correct")
    result = verify(Generated(bundle, app_dir), direct_runner, backend)
    assert result.type == "static", describe(result)
    assert "requires Mule Enterprise runtime" in result.message
    assert direct_runner.started == []
    assert backend.calls() == []


def fake_provider() -> Any:
    """The --llm fake provider (canned answers from tests/fixtures/llm, via A2M_FAKE_LLM_DIR)."""
    from a2m.ai import make_provider

    return make_provider("fake", {"A2M_FAKE_LLM_DIR": str(LLM)})


# ================================================================ CP7 adversarial round 1 (CP7-X01 .. X14)
# New blocks only; every line above is locked.

import signal  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import zipfile  # noqa: E402

KEY_HEADERS = {"x-api-key": GOOD_KEY}


def keys_exchange(
    name: str,
    *,
    method: str = "GET",
    path: str = "/keys",
    body: str = "",
    request_headers: Mapping[str, str] | None = None,
    status: int = 200,
    response_body: str = BACKEND_BODY.decode(),
    response_headers: Mapping[str, str] | None = None,
    backend_path: str | None = "/keys",
    backend_headers: Mapping[str, str] | None = None,
    backend_body: str | None = None,
) -> dict[str, Any]:
    """One keys-v1 exchange: one call, and one recorded backend call (none when ``backend_path`` is None)."""
    backend: dict[str, Any] = {
        "method": method,
        "path": backend_path,
        "headers": dict(backend_headers if backend_headers is not None else {"X-Client": "a2m"}),
        "response": {"status": 200, "headers": dict(JSON_HEADERS), "body": BACKEND_BODY.decode()},
    }
    if backend_body is not None:
        backend["body"] = backend_body
    return {
        "name": name,
        "calls": [
            {
                "after_ms": 0,
                "request": {
                    "method": method,
                    "path": path,
                    "headers": dict(request_headers if request_headers is not None else KEY_HEADERS),
                    "body": body,
                },
                "response": {
                    "status": status,
                    "headers": dict(response_headers if response_headers is not None else JSON_HEADERS),
                    "body": response_body,
                },
            }
        ],
        "backend_calls": [backend] if backend_path is not None else [],
    }


def golden_with(root: Path, *exchanges: Mapping[str, Any], proxy: str = "keys-v1") -> Path:
    for number, exchange in enumerate(exchanges, 1):
        write_exchange(root / proxy, f"{number:02d}-{exchange['name']}.json", exchange)
    return root


def test_CP7_X01_golden_compares_the_full_backend_query_string(tmp_path: Path, backend: Any) -> None:
    """[CP7-X01] A golden replay compares the backend call's whole query string, also when none was recorded."""
    gen = generated(write_keys(tmp_path / "bundles"), tmp_path / "out")

    unexpected = verify(
        gen,
        FakeRunner("correct"),
        backend,
        golden=golden_with(tmp_path / "g1", keys_exchange("leaky-query", path="/keys?apikey=SECRET123")),
    )
    assert unexpected.type == "failed", describe(unexpected)
    diff = result_case(unexpected, name="leaky-query").diff
    assert "backend call 1" in diff and "apikey" in diff and "SECRET123" in diff, diff

    backend.clear()
    same_params = verify(
        gen,
        FakeRunner("correct"),
        backend,
        golden=golden_with(
            tmp_path / "g2", keys_exchange("reordered", path="/keys?b=2&a=1", backend_path="/keys?a=1&b=2")
        ),
    )
    assert same_params.type == "golden", describe(same_params)

    backend.clear()
    missing = verify(
        gen,
        FakeRunner("correct"),
        backend,
        golden=golden_with(tmp_path / "g3", keys_exchange("missing-param", path="/keys", backend_path="/keys?a=1")),
    )
    assert missing.type == "failed", describe(missing)
    diff = result_case(missing, name="missing-param").diff
    assert re.search(r"backend call 1 query a: expected '1', actual absent", diff), diff


def test_CP7_X02_golden_compares_the_body_the_backend_received(tmp_path: Path, backend: Any) -> None:
    """[CP7-X02] A golden replay compares the request body the backend received (JSON-aware); a wrong one fails."""
    gen = generated(write_keys(tmp_path / "bundles"), tmp_path / "out")
    headers = {**KEY_HEADERS, "Content-Type": "application/json"}

    wrong = verify(
        gen,
        FakeRunner("correct"),
        backend,
        golden=golden_with(
            tmp_path / "g1",
            keys_exchange("rewritten", method="POST", body='{"qty":2}', request_headers=headers, backend_body='{"qty":3}'),
        ),
    )
    assert wrong.type == "failed", describe(wrong)
    diff = result_case(wrong, name="rewritten").diff
    assert "backend call 1 body" in diff and "qty" in diff and "3" in diff and "2" in diff, diff

    backend.clear()
    spaced = verify(
        gen,
        FakeRunner("correct"),
        backend,
        golden=golden_with(
            tmp_path / "g2",
            keys_exchange("same", method="POST", body='{"qty":2}', request_headers=headers, backend_body='{ "qty": 2 }'),
        ),
    )
    assert spaced.type == "golden", describe(spaced)

    backend.clear()
    unrecorded = verify(
        gen,
        FakeRunner("correct"),
        backend,
        golden=golden_with(
            tmp_path / "g3", keys_exchange("no-body-recorded", method="POST", body='{"qty":2}', request_headers=headers)
        ),
    )
    assert unrecorded.type == "failed", describe(unrecorded)
    assert "backend call 1 body" in result_case(unrecorded, name="no-body-recorded").diff


def test_CP7_X03_host_is_never_compared_and_the_ignore_list_is_a_cli_option(
    tmp_path: Path, backend: Any, run_cli: Any
) -> None:
    """[CP7-X03] Host on a backend call is never compared; --golden-ignore-header extends the list from the CLI."""
    gen = generated(write_keys(tmp_path / "bundles"), tmp_path / "out")
    host = verify(
        gen,
        FakeRunner("correct"),
        backend,
        golden=golden_with(
            tmp_path / "g1",
            keys_exchange(
                "apigee-trace",
                backend_headers={"Host": "backend.example.com", "X-Forwarded-For": "10.1.2.3", "X-Client": "a2m"},
            ),
        ),
    )
    assert host.type == "golden", describe(host)

    # through the CLI: a per-call header in the recorded response fails unless it is ignored
    exports = orders_input(tmp_path, write_keys)
    golden = golden_with(
        tmp_path / "g2",
        keys_exchange("valid", response_headers={**JSON_HEADERS, "X-Apigee-Message-ID": "rec-1"}),
    )
    base = ["migrate", str(exports), "--llm", "fake", "--mock-backends", "--golden", str(golden)]
    plain = run_cli([*base, "--out", str(tmp_path / "r1")], stages=stages_with(FakeRunner("correct")))
    assert plain.code == 0, plain.err
    assert verification_json(tmp_path / "r1", "keys-v1")["type"] == "failed"
    ignored = run_cli(
        [*base, "--out", str(tmp_path / "r2"), "--golden-ignore-header", "X-Apigee-Message-ID"],
        stages=stages_with(FakeRunner("correct")),
    )
    assert ignored.code == 0, ignored.err
    assert verification_json(tmp_path / "r2", "keys-v1")["type"] == "golden"

    help_text = run_cli(["migrate", "--help"]).out
    assert "--golden-ignore-header" in help_text
    bad = run_cli([*base, "--out", str(tmp_path / "r3"), "--golden-ignore-header", "bad header:"])
    assert bad.code == 2, bad.err


class CapturingRunner:
    """A FakeRunner that also records the properties each app was deployed with."""

    def __init__(self, inner: FakeRunner) -> None:
        self.inner = inner
        self.properties: list[dict[str, str]] = []

    def start(self, app: Any, *, backend_url: str) -> Any:
        self.properties.append(dict(app.properties))
        return self.inner.start(app, backend_url=backend_url)


def allowed_keys_line(app_dir: Path) -> str:
    text = (app_dir / "src" / "main" / "resources" / "config.properties").read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if re.match(r"verifyapikey\..*\.allowedKeys=", line)]
    assert len(lines) == 1, text
    return lines[0]


def test_CP7_X04_golden_allows_the_recorded_api_keys_in_the_deployed_copy_only(
    tmp_path: Path, backend: Any
) -> None:
    """[CP7-X04] On an unedited project (no allowed key), a golden run allows the recorded valid keys and is golden."""
    gen = generated(write_keys(tmp_path / "bundles"), tmp_path / "out", key=False)
    before = allowed_keys_line(gen.app_dir)
    assert before.endswith("="), before
    golden = golden_with(
        tmp_path / "golden",
        keys_exchange("valid-key"),
        keys_exchange(
            "missing-key", request_headers={}, status=401, response_body=missing_key_body().decode(), backend_path=None
        ),
        keys_exchange(
            "bad-key",
            request_headers={"x-api-key": BAD_KEY},
            status=401,
            response_body=INVALID_KEY_BODY.decode(),
            backend_path=None,
        ),
    )
    runner = CapturingRunner(FakeRunner("correct"))

    result = verify(gen, runner, backend, golden=golden)

    assert result.type == "golden", describe(result)
    prop = before[:-1]
    assert runner.properties == [{prop: GOOD_KEY}], runner.properties
    assert "deployed copy only" in result.message, result.message
    assert GOOD_KEY not in result.message and BAD_KEY not in result.message, result.message
    assert allowed_keys_line(gen.app_dir) == before


def write_keys_with_default_fault_rule(parent: Path) -> Path:
    """keys-v1 plus a DefaultFaultRule that runs an AssignMessage setting status 403 on every error."""
    root = write_keys(parent) / "apiproxy"
    (root / "policies" / "fault-status.xml").write_text(
        XML_HEAD + '<AssignMessage name="fault-status">\n    <Set><StatusCode>403</StatusCode></Set>\n'
        '    <AssignTo createNew="false" transport="http" type="response"/>\n</AssignMessage>\n',
        encoding="utf-8",
    )
    manifest = root / "keys-v1.xml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("</Policies>", "<Policy>fault-status</Policy></Policies>"),
        encoding="utf-8",
    )
    endpoint = root / "proxies" / "default.xml"
    endpoint.write_text(
        endpoint.read_text(encoding="utf-8").replace(
            "    <HTTPProxyConnection>",
            '    <DefaultFaultRule name="all"><Step><Name>fault-status</Name></Step>'
            "<AlwaysEnforce>true</AlwaysEnforce></DefaultFaultRule>\n    <HTTPProxyConnection>",
        ),
        encoding="utf-8",
    )
    return parent / "keys-v1"


def test_CP7_X05_errors_a_fault_rule_may_change_are_untested_never_passed(tmp_path: Path, backend: Any) -> None:
    """[CP7-X05] With a DefaultFaultRule, the expected error answers are untested (not passed), with the reason."""
    gen = generated(write_keys_with_default_fault_rule(tmp_path / "bundles"), tmp_path / "out")
    battery = build(gen)
    situations = {(c.policy_type, c.situation) for c in battery.cases}
    assert ("VerifyAPIKey", "missing-key") not in situations and ("VerifyAPIKey", "bad-key") not in situations
    assert ("VerifyAPIKey", "valid-key") in situations
    untested = {(u.name, u.reason) for u in battery.untested}
    fault_reasons = [r for n, r in untested if n == "verify-key"]
    assert len(fault_reasons) == 2 and all("DefaultFaultRule" in r for r in fault_reasons), untested
    fault_policy = [r for n, r in untested if n == "fault-status"]
    assert fault_policy and "FaultRule" in fault_policy[0] and "no flow step" not in fault_policy[0], untested

    result = verify(gen, FakeRunner("correct"), backend)
    assert all(c.situation not in ("missing-key", "bad-key") for c in result.cases), describe(result)
    assert result.ran == len(battery.cases)
    assert "not tested" in result.message and "verify-key" in result.message, result.message


class BrokenSendRunner(FakeRunner):
    """Its apps answer the first call with a broken HTTP response (``error`` raised by send)."""

    def __init__(self, error: Exception) -> None:
        super().__init__("correct")
        self.error = error

    def start(self, app: Any, *, backend_url: str) -> FakeHandle:
        handle = super().start(app, backend_url=backend_url)
        error, sends = self.error, [0]
        original = handle.send

        def send(request: Any) -> Any:
            sends[0] += 1
            if sends[0] == 1:
                raise error
            return original(request)

        handle.send = send  # type: ignore[method-assign]
        return handle


@pytest.mark.parametrize(
    "error",
    [http.client.IncompleteRead(b"{"), http.client.BadStatusLine("garbage"), http.client.LineTooLong("header line")],
    ids=["incomplete-read", "bad-status-line", "line-too-long"],
)
def test_CP7_X06_a_broken_http_answer_is_a_failed_case_never_a_crash(
    tmp_path: Path, backend: Any, error: Exception
) -> None:
    """[CP7-X06] An http.client error while sending a case is a failed case with a diff line, never a crash."""
    gen = generated(write_keys(tmp_path / "bundles"), tmp_path / "out")

    battery = verify(gen, BrokenSendRunner(error), backend)
    assert battery.type == "failed", describe(battery)
    broken = [c for c in battery.cases if not c.passed]
    assert len(broken) == 1 and "no valid response from the app" in broken[0].diff, describe(battery)

    backend.clear()
    replay = verify(gen, BrokenSendRunner(error), backend, golden=golden_with(tmp_path / "g", keys_exchange("one")))
    assert replay.type == "failed", describe(replay)
    assert "no valid response from the app" in result_case(replay, name="one").diff


def test_CP7_X06_the_real_handle_turns_a_malformed_answer_into_a_mule_error() -> None:
    """[CP7-X06] MuleAppHandle.send raises MuleError (not an http.client error) for a malformed HTTP answer."""
    from a2m.verify import HttpRequest
    from a2m.verify.mule import MuleError
    from a2m.verify.runner import MuleAppHandle

    server = socket.create_server(("127.0.0.1", 0))
    port = server.getsockname()[1]

    def answer() -> None:
        conn, _ = server.accept()
        with conn:
            conn.recv(65536)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 50\r\n\r\n{\"short\":")

    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    try:
        handle = MuleAppHandle(None, "keys-v1", port)  # type: ignore[arg-type]
        with pytest.raises(MuleError, match="no valid HTTP response"):
            handle.send(HttpRequest("GET", "/keys"))
    finally:
        thread.join(5)
        server.close()


def write_jar_mvn(path: Path, calls: Path) -> None:
    """An mvn stub (Python) that logs its call and writes target/stub-1.0.0-mule-application.jar."""
    path.write_text(
        f"#!{sys.executable}\n"
        "import os, pathlib, sys, zipfile\n"
        f"with open({str(calls)!r}, 'a') as log:\n"
        "    log.write('mvn\\t' + os.getcwd() + '\\t' + ' '.join(sys.argv[1:]) + '\\n')\n"
        "target = pathlib.Path('target')\n"
        "target.mkdir(exist_ok=True)\n"
        "with zipfile.ZipFile(target / 'stub-1.0.0-mule-application.jar', 'w') as jar:\n"
        "    jar.writestr('config.properties', 'http.listener.port=8081\\n')\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def test_CP7_X07_a_runtime_that_will_not_start_is_static_with_one_console_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_cli: Any
) -> None:
    """[CP7-X07] Tools installed but the Mule runtime cannot start: every proxy is static, said once on the console."""
    calls = tool_env(monkeypatch, tmp_path, stubs=("java",), mule_home="stub")
    write_jar_mvn(tmp_path / "bin" / "mvn", calls)
    exports = orders_input(tmp_path, write_orders, write_quota)
    results = tmp_path / "results"

    res = run_cli(["migrate", str(exports), "--out", str(results), "--llm", "fake", "--mock-backends"])

    assert res.code == 0, res.err
    for proxy in ("orders-v1", "quota-v1"):
        data = verification_json(results, proxy)
        assert data["type"] == "static", data
        assert "not run" in data["message"] and "Mule runtime did not start" in data["message"], data
    assert [row[0] for row in tool_calls(calls)].count("java") == 1, tool_calls(calls)
    notices = [line for line in res.err.splitlines() if "Mule runtime did not start" in line]
    assert len(notices) == 1 and "static" in notices[0], res.err
    assert "Traceback" not in res.err


def test_CP7_X08_the_tool_skip_is_said_once_on_the_console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_cli: Any
) -> None:
    """[CP7-X08] Without the tools, the 'skipped: not installed' message also reaches stderr, once per run."""
    tool_env(monkeypatch, tmp_path)
    exports = orders_input(tmp_path, write_orders, write_quota)

    res = run_cli(["migrate", str(exports), "--out", str(tmp_path / "results"), "--llm", "fake", "--mock-backends"])

    assert res.code == 0, res.err
    lines = [line for line in res.err.splitlines() if "skipped" in line and "not installed" in line]
    assert len(lines) == 1, res.err
    assert re.search(r"Maven|Java|Mule", lines[0]) and "static" in lines[0], lines


class FakeMule:
    """Stands in for a2m.verify.mule.MuleRunner inside MuleAppRunner: a runtime the test can 'kill'."""

    def __init__(self, *, fail_on_start: int | None = None, die_on_deploy: bool = False) -> None:
        self.alive = False
        self.starts = 0
        self.stops = 0
        self.fail_on_start = fail_on_start
        self.die_on_deploy = die_on_deploy
        self.pids: tuple[int, ...] = ()

    def start(self, timeout: float = 0) -> None:
        from a2m.verify.mule import MuleError

        self.starts += 1
        if self.fail_on_start == self.starts:
            raise MuleError("Mule exited with code 1 under the test base")
        self.alive = True

    def stop(self, timeout: float = 0) -> None:
        self.stops += 1
        self.alive = False

    def health_problem(self) -> str | None:
        return None if self.alive else "the Mule runtime's JVM stopped"

    def deploy(self, jar: Path, *, app_name: str, timeout: float = 0, ports: Any = None) -> None:
        from a2m.verify.mule import RuntimeStoppedError

        if self.die_on_deploy:
            self.alive = False
            raise RuntimeStoppedError(f"the Mule runtime exited with code 137 while {app_name} was deploying", "")

    def undeploy(self, app_name: str, *, timeout: float = 0) -> None:
        return None


def fake_mule_runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mule: FakeMule) -> Any:
    from a2m.verify import runner as runner_module

    jar = tmp_path / "built.jar"
    with zipfile.ZipFile(jar, "w") as archive:
        archive.writestr("config.properties", "http.listener.port=8081\n")
    monkeypatch.setattr(runner_module, "package", lambda app_dir, timeout=0: jar)
    real = runner_module.MuleAppRunner(mule_home=tmp_path / "no-mule-home", mule_base=tmp_path / "base")
    real._mule = mule
    return real


def test_CP7_X09_a_runtime_that_died_is_restarted_once_then_reported_static(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: Any
) -> None:
    """[CP7-X09] A runtime found dead before a deploy is restarted once; after that, later apps are static."""
    from a2m.verify import AppUnderTest
    from a2m.verify.mule import RuntimeUnavailableError

    gen = generated(write_keys(tmp_path / "bundles"), tmp_path / "out")
    mule = FakeMule()
    runner = fake_mule_runner(tmp_path, monkeypatch, mule)
    app = AppUnderTest("keys-v1", gen.app_dir)

    runner.start(app, backend_url=backend.url).stop()
    assert mule.starts == 1
    mule.alive = False  # the JVM was killed between two proxies
    runner.start(app, backend_url=backend.url).stop()
    assert (mule.starts, mule.stops) == (2, 1)
    mule.alive = False  # and again
    with pytest.raises(RuntimeUnavailableError, match="stopped again"):
        runner.start(app, backend_url=backend.url)
    assert mule.starts == 2

    result = verify(gen, runner, backend)
    assert result.type == "static", describe(result)
    assert "not run" in result.message and "stopped again" in result.message, result.message
    assert runner.unavailable_reason is not None


def test_CP7_X09_a_failed_restart_or_a_death_during_deploy_is_said_plainly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: Any
) -> None:
    """[CP7-X09] A restart that fails gives static 'not run'; a runtime dying during a deploy is not an app result."""
    gen = generated(write_keys(tmp_path / "bundles"), tmp_path / "out")

    (tmp_path / "a").mkdir()
    mule = FakeMule(fail_on_start=2)
    runner = fake_mule_runner(tmp_path / "a", monkeypatch, mule)
    verify(gen, runner, backend)  # the stand-in runtime serves nothing; only the start count matters here
    assert mule.starts == 1
    mule.alive = False
    second = verify(gen, runner, backend)
    assert second.type == "static", describe(second)
    assert "did not restart" in second.message, second.message
    assert second.ran == 0

    (tmp_path / "b").mkdir()
    dying = fake_mule_runner(tmp_path / "b", monkeypatch, FakeMule(die_on_deploy=True))
    died = verify(gen, dying, backend)
    assert died.type == "failed", describe(died)
    assert "Mule runtime stopped while the app was deploying" in died.message, died.message
    assert died.ran == 0


# A stand-in for Mule's JVM (a2m starts $JAVA_HOME/bin/java directly, its output going to logs/mule.log). It
# loops instead of exec-ing a sleep, so its command line keeps the -Dmule.base=<base> that names its runtime.
STUB_RUNTIME = """\
#!/bin/sh
echo 'INFO Mule is up and kicking (every 5000ms)'
while :; do sleep 1; done
"""
STUB_JAVA_HOME = "stub-java-home"


def stub_mule_home(root: Path, script: str = STUB_RUNTIME) -> Path:
    """A Mule 4 home under ``root``, and ``root/stub-java-home`` whose bin/java runs ``script``.

    Point JAVA_HOME at :func:`stub_java_home` so the runner starts the stand-in JVM."""
    home = root / "stub-mule-home"
    write_mule_boot(home)
    java = stub_java_home(root) / "bin" / "java"
    java.parent.mkdir(parents=True, exist_ok=True)
    java.write_text(script, encoding="utf-8")
    java.chmod(0o755)
    return home


def stub_java_home(root: Path) -> Path:
    return root / STUB_JAVA_HOME


def stub_env(root: Path) -> dict[str, str]:
    """child_env() with JAVA_HOME pointing at the stand-in JVM of :func:`stub_mule_home`."""
    return dict(child_env(), JAVA_HOME=str(stub_java_home(root)))


def live(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace").rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state != "Z"


def wait_dead(pids: Sequence[int], timeout: float = 30.0) -> list[int]:
    deadline = time.monotonic() + timeout
    alive = [p for p in pids if live(p)]
    while alive and time.monotonic() < deadline:
        time.sleep(0.2)
        alive = [p for p in alive if live(p)]
    return alive


def kill_own(pids: Sequence[int]) -> None:
    """SIGKILL only PIDs this test recorded, if still alive."""
    for pid in pids:
        if live(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def test_CP7_X10_a_stopped_runtime_is_detected_and_a_deploy_does_not_hang(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[CP7-X10] The real MuleRunner sees its runtime die, and a deploy waiting on a dead runtime stops at once."""
    from a2m.verify.mule import MuleRunner, RuntimeStoppedError

    runner = MuleRunner(mule_home=stub_mule_home(tmp_path), mule_base=tmp_path / "base")
    monkeypatch.setenv("JAVA_HOME", str(stub_java_home(tmp_path)))
    pids: list[int] = []
    try:
        runner.start(timeout=30)
        pids = [int(p) for p in runner.pids]
        assert runner.health_problem() is None
        os.kill(pids[0], signal.SIGKILL)
        assert wait_dead(pids[:1]) == []
        deadline = time.monotonic() + 10
        while runner.health_problem() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        assert runner.health_problem() is not None
        jar = tmp_path / "keys-v1.jar"
        with zipfile.ZipFile(jar, "w") as archive:
            archive.writestr("config.properties", "")
        started = time.monotonic()
        with pytest.raises(RuntimeStoppedError):
            runner.deploy(jar, app_name="keys-v1", timeout=60)
        assert time.monotonic() - started < 10
    finally:
        runner.stop(timeout=10)
        kill_own(pids)


SIGNAL_SCRIPT = """\
import sys, time
from pathlib import Path
from a2m.cli import main
from a2m.engine import parse
from a2m.verify.mule import MuleRunner

home, base, ready, exports, out = sys.argv[1:6]


class MuleStage:
    __name__ = "mule-stage"

    def __init__(self):
        self.runner = None

    def __call__(self, context):
        self.runner = MuleRunner(mule_home=Path(home), mule_base=Path(base))
        self.runner.start(timeout=30)
        Path(ready).write_text(" ".join(str(p) for p in self.runner.pids))
        time.sleep(120)

    def close(self):
        if self.runner is not None:
            self.runner.stop(timeout=10)


sys.exit(main(["migrate", exports, "--out", out, "--llm", "fake", "--no-runtime"], stages=[parse, MuleStage()]))
"""

EXIT_SCRIPT = """\
import sys
from pathlib import Path
from a2m.verify.mule import MuleRunner

home, base, ready = sys.argv[1:4]
runner = MuleRunner(mule_home=Path(home), mule_base=Path(base))
runner.start(timeout=30)
Path(ready).write_text(" ".join(str(p) for p in runner.pids))
raise SystemExit(3)
"""


def child_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("FORCE_COLOR", "PY_COLORS")}
    env["PYTHONPATH"] = str(REPO)
    return env


def wait_for_file(path: Path, process: subprocess.Popen[bytes], timeout: float = 60.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file() and path.read_text(encoding="utf-8").strip():
            return path.read_text(encoding="utf-8")
        if process.poll() is not None:
            break
        time.sleep(0.1)
    raise AssertionError(f"{path} never appeared (exit code {process.poll()})")


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP, signal.SIGINT], ids=["SIGTERM", "SIGHUP", "SIGINT"])
def test_CP7_X11_a_signal_mid_batch_stops_the_runtime_a2m_started(tmp_path: Path, sig: signal.Signals) -> None:
    """[CP7-X11] SIGTERM, SIGHUP or SIGINT sent to a2m mid-batch: the batch closes its stages, no runtime is left."""
    exports = orders_input(tmp_path)
    ready = tmp_path / "ready"
    script = tmp_path / "run.py"
    script.write_text(SIGNAL_SCRIPT, encoding="utf-8")
    args = [str(stub_mule_home(tmp_path)), str(tmp_path / "base"), str(ready), str(exports), str(tmp_path / "out")]
    process = subprocess.Popen(
        [sys.executable, str(script), *args], cwd=REPO, env=stub_env(tmp_path), stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    pids: list[int] = []
    try:
        pids = [int(p) for p in wait_for_file(ready, process).split()]
        assert pids and all(live(p) for p in pids)
        process.send_signal(sig)
        _, err = process.communicate(timeout=60)
        assert wait_dead(pids) == [], f"runtime processes left alive after {sig.name}"
        expected = 130 if sig == signal.SIGINT else 128 + int(sig)
        assert process.returncode == expected, (process.returncode, err.decode())
        assert b"Traceback" not in err, err.decode()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(10)
        kill_own(pids)


def test_CP7_X12_the_exit_hook_stops_a_runtime_nobody_stopped(tmp_path: Path) -> None:
    """[CP7-X12] A program that exits without stopping its runtime still leaves no Mule process (atexit hook)."""
    ready = tmp_path / "ready"
    script = tmp_path / "exit.py"
    script.write_text(EXIT_SCRIPT, encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, str(script), str(stub_mule_home(tmp_path)), str(tmp_path / "base"), str(ready)],
        cwd=REPO,
        env=stub_env(tmp_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    pids: list[int] = []
    try:
        process.communicate(timeout=60)
        pids = [int(p) for p in ready.read_text(encoding="utf-8").split()]
        assert process.returncode == 3
        assert wait_dead(pids) == []
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(10)
        kill_own(pids)


@pytest.mark.parametrize("processes", ["proc", "ps"])
def test_CP7_X13_a_new_start_ends_what_a_killed_a2m_left_under_the_same_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, processes: str
) -> None:
    """[CP7-X13] A runtime left running under a MULE_BASE (its a2m was killed) is ended by the next start there:
    its JVM is recognised by the -Dmule.base=<base> on its command line, read from /proc or from ps (macOS)."""
    from a2m.verify import mule
    from a2m.verify.mule import MuleRunner

    if processes == "ps":
        monkeypatch.setattr(mule, "_processes", mule._PsTable())
        monkeypatch.setattr(mule, "_pidfd_open", None)
    home = stub_mule_home(tmp_path)
    monkeypatch.setenv("JAVA_HOME", str(stub_java_home(tmp_path)))
    base = tmp_path / "base"
    orphan = MuleRunner(mule_home=home, mule_base=base)
    fresh = MuleRunner(mule_home=home, mule_base=base)
    old: list[int] = []
    new: list[int] = []
    try:
        orphan.start(timeout=30)
        old = [int(p) for p in orphan.pids]
        fresh.start(timeout=30)
        new = [int(p) for p in fresh.pids]
        assert wait_dead(old) == [], "the earlier runtime under the same base is still running"
        assert all(live(p) for p in new)
    finally:
        fresh.stop(timeout=10)
        orphan.stop(timeout=10)
        kill_own([*old, *new])
    assert wait_dead(new) == []


def test_CP7_X14_a_logged_deploy_failure_is_always_explained_in_plain_words(tmp_path: Path, backend: Any) -> None:
    """[CP7-X14] A runner's generic DeployError whose log shows a connector mismatch is reported in plain words."""
    from a2m.verify import explain_deploy_failure
    from a2m.verify.mule import DeployError

    class MismatchRunner(FakeRunner):
        mule_version = "4.9.0"

        def start(self, app: Any, *, backend_url: str) -> FakeHandle:
            raise DeployError(f"{app.name} failed to deploy", VERSION_MISMATCH_LOG)

    gen = generated(write_keys(tmp_path / "bundles"), tmp_path / "out")
    result = verify(gen, MismatchRunner(), backend)

    assert result.type == "failed", describe(result)
    assert VERSION_MISMATCH in result.message, result.message
    expected = explain_deploy_failure("keys-v1", VERSION_MISMATCH_LOG, mule_version="4.9.0")
    assert (result.message, result.log_excerpt) == (expected.message, expected.log_excerpt)


# ================================================================ CP7 adversarial round 2 (CP7-X18 .. X22)
# New blocks only; every line above is locked.


RECORDING_MULE = """\
#!/bin/sh
printf '%s\\n%s\\n' "$MULE_BASE" "$MULE_HOME" > '{record}'
if test -d "$MULE_BASE/conf"; then echo conf-found >> '{record}'; fi
printf '%s\\n' "$@" >> '{record}'
exit 1
"""


def test_CP7_X18_relative_paths_reach_the_mule_runtime_as_absolute_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_cli: Any
) -> None:
    """[CP7-X18] `a2m migrate in --out results` with a relative MULE_HOME, run from a temp folder: the Mule
    launcher gets an absolute MULE_BASE (the run's own .a2m-work/.mule-base) and MULE_HOME, and finds its conf.
    The launcher is Mule's JVM itself (the java on PATH), and its -Dmule.base and -Dmule.home are absolute too."""
    calls = tool_env(monkeypatch, tmp_path, stubs=("java",))
    write_jar_mvn(tmp_path / "bin" / "mvn", calls)
    home = tmp_path / "rel-mule-home"
    write_mule_boot(home)
    record = tmp_path / "mule-env.txt"
    launcher = tmp_path / "bin" / "java"
    launcher.write_text(RECORDING_MULE.format(record=record), encoding="utf-8")
    launcher.chmod(0o755)
    monkeypatch.setenv("A2M_MULE_HOME", "rel-mule-home")
    monkeypatch.chdir(tmp_path)
    orders_input(tmp_path, write_keys)
    cwd = Path(os.getcwd())

    res = run_cli(["migrate", "in", "--out", "results", "--llm", "fake", "--mock-backends"])

    assert res.code == 0, res.err
    assert record.is_file(), "the Mule launcher was never started"
    lines = record.read_text(encoding="utf-8").splitlines()
    assert lines[0] == str(cwd / "results" / ".a2m-work" / ".mule-base"), lines
    assert lines[1] == str(cwd / "rel-mule-home"), lines
    assert "conf-found" in lines, lines
    assert f"-Dmule.base={cwd / 'results' / '.a2m-work' / '.mule-base'}" in lines, lines
    assert f"-Dmule.home={cwd / 'rel-mule-home'}" in lines, lines


STRIP_KEYS = (
    '<AssignMessage name="strip-keys">\n    <Remove>\n        <Headers>\n'
    '            <Header name="x-apikey"/>\n            <Header name="Authorization"/>\n'
    "        </Headers>\n    </Remove>\n    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n"
    '    <AssignTo createNew="false" transport="http" type="request"/>\n</AssignMessage>\n'
)
STRIP_POWERED = (
    '<AssignMessage name="strip-powered">\n    <Remove>\n        <Headers>\n'
    '            <Header name="X-Powered-By"/>\n        </Headers>\n    </Remove>\n'
    "    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n"
    '    <AssignTo createNew="false" transport="http" type="response"/>\n</AssignMessage>\n'
)


def write_strip(parent: Path) -> Path:
    """strip-v1: removes x-apikey and Authorization before the target, and X-Powered-By from the response."""
    return write_proxy(
        parent,
        "strip-v1",
        base_path="/strip",
        target_url="http://backend.example/strip",
        policies={"strip-keys": STRIP_KEYS, "strip-powered": STRIP_POWERED},
        request_steps=[("strip-keys", None)],
        flows=[("all", 'request.verb = "GET"', [], ["strip-powered"])],
    )


def write_recordings(golden: Path, proxy: str, exchanges: Mapping[str, Any]) -> Path:
    folder = golden / proxy
    folder.mkdir(parents=True, exist_ok=True)
    for name, exchange in exchanges.items():
        (folder / name).write_text(json.dumps(exchange), encoding="utf-8")
    return golden


STRIP_EXCHANGE = {
    "name": "keys-removed",
    "calls": [
        {
            "request": {
                "method": "GET",
                "path": "/strip",
                "headers": {"x-apikey": "PROD-KEY-123", "Authorization": "Bearer secret-token"},
            },
            "response": {"status": 200, "headers": {"Content-Type": "application/json"}, "body": '{"id":7}'},
        }
    ],
    "backend_calls": [
        {
            "method": "GET",
            "path": "/strip",
            "headers": {},
            "response": {
                "status": 200,
                "headers": {"Content-Type": "application/json", "X-Powered-By": "Express"},
                "body": '{"id":7}',
            },
        }
    ],
}


class ForwardingHandle:
    """An app that forwards each client header (minus ``drop``, plus ``add``) to the backend and passes the
    backend's answer on (minus ``drop_response``), like a2m's generated apps forward headers."""

    running = True

    def __init__(
        self, backend_url: str, drop: Sequence[str], add: Mapping[str, str], drop_response: Sequence[str]
    ) -> None:
        self.backend_url = backend_url
        self.drop = {d.lower() for d in drop}
        self.add = dict(add)
        self.drop_response = {d.lower() for d in drop_response}
        self.base_url: str | None = "http://fake.invalid/forwarding"

    def send(self, request: Any) -> Any:
        target = urlsplit(self.backend_url)
        headers = {k.lower(): v for k, v in dict(request.headers).items() if k.lower() not in self.drop}
        headers.update(self.add)
        conn = http.client.HTTPConnection(target.hostname or "127.0.0.1", target.port or 80, timeout=10)
        try:
            conn.request(request.method, request.path, body=request.body or None, headers=headers)
            got = conn.getresponse()
            body = got.read()
            kept = {k: v for k, v in got.getheaders() if k.lower() not in self.drop_response}
            return response(got.status, kept, body)
        finally:
            conn.close()

    def stop(self) -> None:
        return None


class ForwardingRunner:
    def __init__(
        self, drop: Sequence[str] = (), add: Mapping[str, str] | None = None, drop_response: Sequence[str] = ()
    ) -> None:
        self.drop, self.add, self.drop_response = drop, dict(add or {}), drop_response

    def start(self, app: Any, *, backend_url: str) -> ForwardingHandle:
        return ForwardingHandle(backend_url, self.drop, self.add, self.drop_response)


def test_CP7_X19_golden_fails_when_the_app_sends_a_header_apigee_did_not(tmp_path: Path, backend: Any) -> None:
    """[CP7-X19] A golden replay compares headers beyond the recorded ones: an API key or Authorization the
    proxy removes but the app forwards, a backend header the proxy strips but the app passes on, or a header the
    app makes up, each breaks golden with an 'expected absent' line; the faithful app is golden."""
    gen = generated(write_strip(tmp_path / "bundles"), tmp_path / "out", key=False)
    golden = write_recordings(tmp_path / "golden", "strip-v1", {"01-keys-removed.json": STRIP_EXCHANGE})
    faithful = {"drop": ("x-apikey", "authorization"), "drop_response": ("x-powered-by",)}

    good = verify(gen, ForwardingRunner(**faithful), backend, golden=golden)  # type: ignore[arg-type]
    assert good.type == "golden", describe(good)

    backend.clear()
    leaky = verify(gen, ForwardingRunner(), backend, golden=golden)
    assert leaky.type == "failed", describe(leaky)
    diff = leaky.cases[0].diff
    assert "backend call 1 header x-apikey: expected absent, actual 'PROD-KEY-123'" in diff, diff
    assert "backend call 1 header authorization: expected absent" in diff, diff
    assert "header x-powered-by: expected absent, actual 'Express'" in diff, diff

    backend.clear()
    made_up = verify(gen, ForwardingRunner(add={"X-Debug": "1"}, **faithful), backend, golden=golden)  # type: ignore[arg-type]
    assert made_up.type == "failed", describe(made_up)
    assert "backend call 1 header x-debug: expected absent, actual '1'" in made_up.cases[0].diff, describe(made_up)


def test_CP7_X19_backend_call_headers_are_compared_beyond_the_recorded_ones() -> None:
    """[CP7-X19] _backend_diffs: a forwarded header a step of the proxy removes is a mismatch when the recording
    lacks it; Host, the ignore list and the local client defaults are not."""
    from a2m.verify import HttpRequest
    from a2m.verify.batteries import HeaderChanges
    from a2m.verify.golden import Exchange, RecordedBackendCall, RecordedCall
    from a2m.verify.harness import VerifyConfig, _backend_diffs
    from a2m.verify.mock_backend import RecordedCall as Received

    client = RecordedCall(0, HttpRequest("GET", "/keys", {"x-apikey": "PROD-KEY-123"}), response(200, {}, b""))
    exchange = Exchange("x", "x.json", (client,), (RecordedBackendCall("GET", "/keys", {"X-Client": "a2m"}, response(200, {}, b"")),))
    received = Received(
        "GET",
        "/keys",
        "",
        {
            "x-client": "a2m",
            "x-apikey": "PROD-KEY-123",
            "host": "127.0.0.1:1234",
            "user-agent": "AHC/1.0",
            "accept": "*/*",
            "accept-encoding": "identity",
            "x-correlation-id": "abc",
        },
        b"",
    )

    removed = _backend_diffs(exchange, [received], VerifyConfig(), HeaderChanges(request=frozenset({"x-apikey"})))
    assert removed == ["backend call 1 header x-apikey: expected absent, actual 'PROD-KEY-123'"], removed
    any_step = _backend_diffs(exchange, [received], VerifyConfig(), HeaderChanges(request_any=True))
    assert any_step == ["backend call 1 header x-apikey: expected absent, actual 'PROD-KEY-123'"], any_step
    assert _backend_diffs(exchange, [received], VerifyConfig(), HeaderChanges()) == []


def test_CP7_X20_a_policy_a2m_did_not_generate_is_untested_never_a_failing_case(tmp_path: Path, backend: Any) -> None:
    """[CP7-X20] ResponseCache has no Mule template, so the battery has no case for it: it is listed as untested,
    'not generated by a2m', and the proxy is never run against a cache the app does not have."""
    gen = generated(write_cache(tmp_path / "bundles"), tmp_path / "out")

    battery = build(gen)

    assert [c for c in battery.cases if c.policy_type == "ResponseCache"] == []
    untested = [u for u in battery.untested if u.type == "ResponseCache"]
    assert len(untested) == 1 and untested[0].name == "cache", battery.untested
    assert untested[0].reason.startswith("not generated by a2m"), untested[0].reason

    runner = FakeRunner("broken-cache")
    result = verify(gen, runner, backend)

    assert result.type == "static", describe(result)
    assert result.ran == 0 and runner.sent == [], describe(result)
    assert any(u.reason.startswith("not generated by a2m") for u in result.untested), result.untested


class DyingRunner(FakeRunner):
    """A runner whose runtime dies after ``alive_calls`` calls: later sends fail and runtime_problem says why."""

    def __init__(self, alive_calls: int = 1) -> None:
        super().__init__("correct")
        self.alive_calls = alive_calls
        self.calls = 0
        self.dead = False

    def start(self, app: Any, *, backend_url: str) -> FakeHandle:
        from a2m.verify.mule import MuleError

        handle = super().start(app, backend_url=backend_url)
        inner = handle.send

        def send(request: Any) -> Any:
            self.calls += 1
            if self.calls > self.alive_calls:
                self.dead = True
            if self.dead:
                raise MuleError(f"no valid HTTP response from {app.name}: ConnectionRefusedError: refused")
            return inner(request)

        handle.send = send  # type: ignore[method-assign]
        return handle

    def runtime_problem(self) -> str | None:
        return "the Mule runtime's JVM stopped" if self.dead else None


def test_CP7_X21_a_runtime_that_dies_during_the_tests_is_said_plainly(tmp_path: Path, backend: Any) -> None:
    """[CP7-X21] The runtime dies while a proxy's battery or golden calls run: the tests stop at once and the proxy
    is failed with 'the Mule runtime stopped during the tests', never a list of broken-app diffs."""
    gen = generated(write_keys(tmp_path / "bundles"), tmp_path / "out")

    runner = DyingRunner(alive_calls=1)
    result = verify(gen, runner, backend)

    assert result.type == "failed", describe(result)
    assert "Mule runtime stopped during the tests" in result.message, result.message
    assert "JVM stopped" in result.message, result.message
    assert runner.calls == 2, "the tests must stop at the first call the dead runtime did not answer"
    assert not any("no valid response" in c.diff for c in result.cases), describe(result)
    assert runner.stopped == ["keys-v1"]

    exchange = {
        "name": "valid",
        "calls": [
            {
                "request": {"method": "GET", "path": "/keys", "headers": {"x-api-key": GOOD_KEY}},
                "response": {"status": 200, "headers": {}, "body": '{"id":7}'},
            }
        ],
        "backend_calls": [{"method": "GET", "path": "/keys", "headers": {"X-Client": "a2m"}}],
    }
    golden = write_recordings(tmp_path / "golden", "keys-v1", {"01.json": exchange, "02.json": exchange})
    backend.clear()
    replay_runner = DyingRunner(alive_calls=1)
    replayed = verify(gen, replay_runner, backend, golden=golden)

    assert replayed.type == "failed", describe(replayed)
    assert "Mule runtime stopped during the tests" in replayed.message, replayed.message
    assert replay_runner.calls == 2


class DiesAfterFirstDeploy(FakeMule):
    def __init__(self) -> None:
        super().__init__()
        self.deploys = 0

    def deploy(self, jar: Path, *, app_name: str, timeout: float = 0, ports: Any = None) -> None:
        self.deploys += 1
        if self.deploys == 1:
            self.alive = False  # the JVM dies right after the app started, before its tests


def test_CP7_X21_the_real_runner_sees_the_death_and_restarts_for_the_next_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: Any
) -> None:
    """[CP7-X21] MuleAppRunner: a runtime that died under an app's tests is reported as stopped, and the next proxy
    gets a restarted runtime (its own results are then about the app, not the dead runtime)."""
    gen = generated(write_keys(tmp_path / "bundles"), tmp_path / "out")
    mule = DiesAfterFirstDeploy()
    runner = fake_mule_runner(tmp_path, monkeypatch, mule)

    first = verify(gen, runner, backend)

    assert first.type == "failed", describe(first)
    assert "Mule runtime stopped during the tests" in first.message, first.message
    assert first.ran == 0
    assert mule.starts == 1

    second = verify(gen, runner, backend)

    assert mule.starts == 2, "the next proxy must get the runtime restarted"
    assert "stopped during the tests" not in second.message, second.message
    assert second.ran == 3, describe(second)


def test_CP7_X22_a_mule_home_without_services_is_static_with_one_console_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_cli: Any
) -> None:
    """[CP7-X22] MULE_HOME with Mule's boot jar but no services/ folder (a broken install): every proxy is static
    with 'not run', one console notice, exit 0, and no proxy crashes."""
    calls = tool_env(monkeypatch, tmp_path, stubs=("java",))
    write_jar_mvn(tmp_path / "bin" / "mvn", calls)
    home = tmp_path / "mule3-home"
    write_mule_boot(home)
    (home / "services").rmdir()
    monkeypatch.setenv("A2M_MULE_HOME", str(home))
    exports = orders_input(tmp_path, write_orders, write_quota)
    results = tmp_path / "results"

    res = run_cli(["migrate", str(exports), "--out", str(results), "--llm", "fake", "--mock-backends"])

    assert res.code == 0, res.err
    for proxy in ("orders-v1", "quota-v1"):
        data = verification_json(results, proxy)
        assert data["type"] == "static", data
        assert "not run" in data["message"] and "cannot be prepared" in data["message"], data
    assert [row[0] for row in tool_calls(calls)].count("java") == 0, tool_calls(calls)
    notices = [line for line in res.err.splitlines() if "cannot be prepared" in line]
    assert len(notices) == 1 and "static" in notices[0], res.err
    assert "Traceback" not in res.err


# ================================================================ CP7 adversarial round 3 (CP7-X24 .. X28)
# New blocks only; every line above is locked.


class StickySessionHandle(ForwardingHandle):
    """An app with a scoping bug: it forwards the first X-Session it saw on every later backend call too."""

    def __init__(self, backend_url: str) -> None:
        super().__init__(backend_url, (), {"X-Client": "a2m"}, ())
        self.session: str | None = None

    def send(self, request: Any) -> Any:
        headers = dict(request.headers)
        sent = next((v for k, v in headers.items() if k.lower() == "x-session"), None)
        if self.session is None:
            self.session = sent
        if sent is not None and self.session is not None:
            headers = {k: (self.session if k.lower() == "x-session" else v) for k, v in headers.items()}
        return super().send(type(request)(request.method, request.path, headers, request.body))


class StickySessionRunner:
    def start(self, app: Any, *, backend_url: str) -> StickySessionHandle:
        return StickySessionHandle(backend_url)


def two_session_exchange() -> dict[str, Any]:
    """keys-v1: two client calls with their own X-Session; each backend call lists only X-Client."""

    def call(session: str) -> dict[str, Any]:
        return {
            "request": {"method": "GET", "path": "/keys", "headers": {**KEY_HEADERS, "X-Session": session}},
            "response": {"status": 200, "headers": dict(JSON_HEADERS), "body": BACKEND_BODY.decode()},
        }

    backend_call = {
        "method": "GET",
        "path": "/keys",
        "headers": {"X-Client": "a2m"},
        "response": {"status": 200, "headers": dict(JSON_HEADERS), "body": BACKEND_BODY.decode()},
    }
    return {
        "name": "two-sessions",
        "calls": [call("trace-1"), call("trace-2")],
        "backend_calls": [backend_call, dict(backend_call)],
    }


def test_CP7_X24_a_header_value_from_another_client_call_breaks_golden(tmp_path: Path, backend: Any) -> None:
    """[CP7-X24] The client headers a backend call may carry unrecorded are those of the client call it was made
    for: an app that forwards call 1's X-Session on call 2's backend request fails golden; the faithful one passes."""
    gen = generated(write_keys(tmp_path / "bundles"), tmp_path / "out")
    golden = write_recordings(tmp_path / "golden", "keys-v1", {"01-two-sessions.json": two_session_exchange()})

    good = verify(gen, ForwardingRunner(add={"X-Client": "a2m"}), backend, golden=golden)  # type: ignore[arg-type]
    assert good.type == "golden", describe(good)

    backend.clear()
    sticky = verify(gen, StickySessionRunner(), backend, golden=golden)  # type: ignore[arg-type]
    assert sticky.type == "failed", describe(sticky)
    diff = sticky.cases[0].diff
    assert "backend call 2 header x-session: expected absent, actual 'trace-1'" in diff, diff
    assert "backend call 1" not in diff, diff


def test_CP7_X24_backend_diffs_tolerate_only_the_originating_client_calls_headers() -> None:
    """[CP7-X24] _backend_diffs: with the origin of each backend call, only that client call's value is tolerated;
    without origins, a backend call is matched to the client call with its number when the counts agree, and
    otherwise only a header every client call sent with the same value is tolerated."""
    from a2m.verify import HttpRequest
    from a2m.verify.batteries import HeaderChanges
    from a2m.verify.golden import Exchange, RecordedBackendCall, RecordedCall
    from a2m.verify.harness import VerifyConfig, _backend_diffs
    from a2m.verify.mock_backend import RecordedCall as Received

    def client(session: str) -> Any:
        return RecordedCall(0, HttpRequest("GET", "/keys", {"X-Session": session, "X-Tenant": "t1"}), response(200, {}, b""))

    recorded = RecordedBackendCall("GET", "/keys", {}, response(200, {}, b""))
    exchange = Exchange("x", "x.json", (client("trace-1"), client("trace-2")), (recorded, recorded))

    def got(session: str) -> Any:
        return Received("GET", "/keys", "", {"x-session": session, "x-tenant": "t1"}, b"")

    leaked = [got("trace-1"), got("trace-1")]
    expected = ["backend call 2 header x-session: expected absent, actual 'trace-1'"]
    assert _backend_diffs(exchange, leaked, VerifyConfig(), HeaderChanges(), [0, 1]) == expected
    assert _backend_diffs(exchange, leaked, VerifyConfig(), HeaderChanges()) == expected
    assert _backend_diffs(exchange, [got("trace-1"), got("trace-2")], VerifyConfig(), HeaderChanges(), [0, 1]) == []
    # One backend call for two client calls, origin unknown: only the common X-Tenant is tolerated.
    single = Exchange("y", "y.json", (client("trace-1"), client("trace-2")), (recorded,))
    lines = _backend_diffs(single, [got("trace-2")], VerifyConfig(), HeaderChanges())
    assert lines == ["backend call 1 header x-session: expected absent, actual 'trace-2'"], lines
    assert _backend_diffs(single, [got("trace-2")], VerifyConfig(), HeaderChanges(), [1]) == []


VERSIONED_CONDITION = "request.header.x-api-version = 2"


def write_versioned_keys(parent: Path, *, on_flow: bool) -> Path:
    """versioned-v1: a VerifyAPIKey whose condition a2m can't translate (a number on the right), on the step itself
    or on the conditional flow that holds it."""
    if on_flow:
        return write_proxy(
            parent,
            "versioned-v1",
            base_path="/versioned",
            target_url="http://backend.example/versioned",
            policies={"verify-key": policy_verify_key()},
            flows=[("v2", VERSIONED_CONDITION, ["verify-key"], [])],
        )
    return write_proxy(
        parent,
        "versioned-v1",
        base_path="/versioned",
        target_url="http://backend.example/versioned",
        policies={"verify-key": policy_verify_key()},
        request_steps=[("verify-key", VERSIONED_CONDITION)],
    )


@pytest.mark.parametrize("on_flow", [False, True], ids=["condition-on-step", "condition-on-flow"])
def test_CP7_X25_a_step_whose_condition_is_not_translated_is_untested_never_failed(
    tmp_path: Path, backend: Any, on_flow: bool
) -> None:
    """[CP7-X25] A VerifyAPIKey a2m generated under a #[false] guard (its condition, or its flow's, can't be
    translated) never runs in the app: no missing-key or bad-key case, untested 'not generated by a2m', never failed;
    the same through the engine stages, which use the generator's saved per-step records."""
    from a2m.generator import generate_project
    from a2m.parser import read_bundle
    from a2m.verify.batteries import build_battery
    from a2m.verify.generated import GeneratedSteps

    bundle_dir = write_versioned_keys(tmp_path / "bundles", on_flow=on_flow)
    bundle = read_bundle(bundle_dir)
    app_dir = tmp_path / "out" / "versioned-v1" / "mule-app"
    result = generate_project(bundle, app_dir)
    fill_key(app_dir)
    assert "#[false]" in (app_dir / "src" / "main" / "mule" / "proxy.xml").read_text(encoding="utf-8")

    for battery in (build_battery(bundle, app_dir, generated=GeneratedSteps.from_result(result)),
                    build_battery(bundle, app_dir)):
        assert [c.situation for c in battery.cases if c.policy_type == "VerifyAPIKey"] == [], battery.cases
        mine = [u for u in battery.untested if u.name == "verify-key"]
        assert len(mine) == 1 and mine[0].reason.startswith("not generated by a2m"), battery.untested
        assert "can't be translated" in mine[0].reason, mine[0].reason

    runner = FakeRunner("correct")
    verified = verify(Generated(bundle, app_dir), runner, backend)
    assert verified.type != "failed", describe(verified)
    assert runner.sent == [], runner.sent

    exports = tmp_path / "in"
    write_versioned_keys(exports, on_flow=on_flow)
    results = tmp_path / "results"
    from a2m.cli import main

    code = main(
        ["migrate", str(exports), "--out", str(results), "--llm", "fake", "--mock-backends"],
        stages=stages_with(FakeRunner("correct")),
    )
    assert code == 0
    data = verification_json(results, "versioned-v1")
    assert data["type"] != "failed", data
    assert any(u["name"] == "verify-key" and u["reason"].startswith("not generated by a2m") for u in data["untested"]), data


def test_CP7_X25_the_engine_saves_the_generators_records_and_the_stage_uses_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[CP7-X25] The battery is built from the records the generate stage saved (what the generator really did,
    e.g. with an AI provider), not from a2m re-deciding: a saved record that says the step was skipped wins."""
    from a2m import layout
    from a2m.cli import main
    from a2m.verify import batteries as batteries_module
    from a2m.verify.generated import GeneratedSteps

    exports = tmp_path / "in"
    write_keys(exports)
    results = tmp_path / "results"
    seen: list[Any] = []
    real_build = batteries_module.build_battery

    def spy(bundle: Any, app_dir: Path, *, generated: Any = None) -> Any:
        saved = layout.generated_steps_path(results, "keys-v1")
        assert saved.is_file(), "the generate stage must save the per-step records before verification"
        records = GeneratedSteps.from_json(saved.read_text(encoding="utf-8"))
        assert generated == records
        seen.append(generated)
        skipped = GeneratedSteps(
            tuple(
                type(s)(s.name, s.type, "skipped", s.location, "the AI's answer could not be used", False)
                if s.name == "verify-key" else s
                for s in records.steps
            ),
            records.conditions,
        )
        return real_build(bundle, app_dir, generated=skipped)

    monkeypatch.setattr("a2m.verify.harness.build_battery", spy)
    code = main(
        ["migrate", str(exports), "--out", str(results), "--llm", "fake", "--mock-backends"],
        stages=stages_with(FakeRunner("correct")),
    )

    assert code == 0 and len(seen) == 1
    data = verification_json(results, "keys-v1")
    assert not any(c["name"].startswith("verify-key") for c in data["cases"]), data
    mine = [u for u in data["untested"] if u["name"] == "verify-key"]
    assert mine and "the AI's answer could not be used" in mine[0]["reason"], data


DEAD_TAG = (
    '<AssignMessage name="tag-v2">\n    <Set>\n        <Headers>\n            <Header name="X-Version">v2</Header>\n'
    "        </Headers>\n    </Set>\n    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n"
    '    <AssignTo createNew="false" transport="http" type="request"/>\n</AssignMessage>\n'
)


def test_CP7_X25_a_case_whose_request_runs_a_step_the_app_does_not_run_is_untested(tmp_path: Path) -> None:
    """[CP7-X25] When a case's request, in Apigee, also runs a step a2m disabled in the app (its condition can't be
    translated), the case is untested: Apigee's answer would include what that step does, the app's never can."""
    from a2m.verify.batteries import build_battery

    bundle_dir = write_proxy(
        tmp_path / "bundles",
        "tagged-v1",
        base_path="/tagged",
        target_url="http://backend.example/tagged",
        policies={"verify-key": policy_verify_key(), "tag-v2": DEAD_TAG},
        request_steps=[("verify-key", None), ("tag-v2", "request.header.x-api-version != 2")],
    )
    gen = generated(bundle_dir, tmp_path / "out")
    battery = build_battery(gen.bundle, gen.app_dir)

    tag = [u for u in battery.untested if u.name == "tag-v2"]
    assert tag and tag[0].reason.startswith("not generated by a2m"), battery.untested
    for case in battery.cases:
        assert "X-Version" not in dict(case.expected_backend_headers), case
    valid = [u for u in battery.untested if u.name == "verify-key" and "tag-v2" in u.reason]
    assert valid and "the app does not run" in valid[0].reason, battery.untested


def test_CP7_X26_tests_that_passed_before_the_runtime_died_keep_their_results(tmp_path: Path, backend: Any) -> None:
    """[CP7-X26] When the runtime dies mid-battery or mid-replay, the tests that finished before keep their
    results next to the plain 'stopped during the tests' reason; the proxy is still failed."""
    gen = generated(write_keys(tmp_path / "bundles"), tmp_path / "out")

    result = verify(gen, DyingRunner(alive_calls=1), backend)

    assert result.type == "failed", describe(result)
    assert "Mule runtime stopped during the tests" in result.message, result.message
    assert (result.ran, result.passed, result.failed) == (1, 1, 0), describe(result)
    assert [c.passed for c in result.cases] == [True], describe(result)
    assert "1 of 1 tests that ran before it stopped passed" in result.message, result.message

    exchange = keys_exchange("valid")
    golden = write_recordings(tmp_path / "golden", "keys-v1", {"01.json": exchange, "02.json": exchange})
    backend.clear()
    replayed = verify(gen, DyingRunner(alive_calls=1), backend, golden=golden)

    assert replayed.type == "failed", describe(replayed)
    assert (replayed.ran, replayed.passed) == (1, 1), describe(replayed)
    assert "Mule runtime stopped during the tests" in replayed.message, replayed.message


class AlwaysDiesOnDeploy(FakeMule):
    """A runtime whose JVM dies right after every deploy, before the app's tests."""

    def deploy(self, jar: Path, *, app_name: str, timeout: float = 0, ports: Any = None) -> None:
        self.alive = False


def test_CP7_X26_the_restart_is_promised_only_when_it_will_happen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: Any
) -> None:
    """[CP7-X26] 'it is restarted for the next proxy' is said only while the runner will restart it: after the one
    restart of the batch, a second death says it is not restarted again; a runner that can't tell promises nothing."""
    gen = generated(write_keys(tmp_path / "bundles"), tmp_path / "out")
    mule = AlwaysDiesOnDeploy()
    runner = fake_mule_runner(tmp_path, monkeypatch, mule)

    first = verify(gen, runner, backend)
    assert "stopped during the tests" in first.message, first.message
    assert "restarted for the next proxy" in first.message, first.message

    second = verify(gen, runner, backend)
    assert mule.starts == 2
    assert "stopped during the tests" in second.message, second.message
    assert "restarted for the next proxy" not in second.message, second.message
    assert "not restarted again" in second.message, second.message

    third = verify(gen, runner, backend)
    assert third.type == "static" and mule.starts == 2, describe(third)

    unknown = verify(gen, DyingRunner(alive_calls=0), backend)
    assert "stopped during the tests" in unknown.message, unknown.message
    assert "restarted" not in unknown.message, unknown.message


SECRET_TOKEN = "Bearer prod-token-xyz"
OTHER_TOKEN = "Bearer other-token-000"


def secret_exchange() -> dict[str, Any]:
    exchange = keys_exchange(
        "with-credentials",
        request_headers={**KEY_HEADERS, "Authorization": SECRET_TOKEN},
        backend_headers={"X-Client": "a2m", "Authorization": OTHER_TOKEN},
    )
    return exchange


def test_CP7_X27_credentials_are_masked_in_diffs_reports_and_run_log(
    tmp_path: Path, backend: Any, run_cli: Any
) -> None:
    """[CP7-X27] An API key a VerifyAPIKey reads, Authorization values and cookies are shown masked (first 4
    characters and the length) in every diff line, in verification.json and in run.log."""
    gen = generated(write_keys(tmp_path / "bundles"), tmp_path / "out")
    golden = write_recordings(tmp_path / "golden", "keys-v1", {"01-with-credentials.json": secret_exchange()})
    leaky = ForwardingRunner(add={"X-Client": "a2m", "X-Leak": GOOD_KEY})

    result = verify(gen, leaky, backend, golden=golden)  # type: ignore[arg-type]

    assert result.type == "failed", describe(result)
    diff = result.cases[0].diff
    for raw in (GOOD_KEY, "prod-token-xyz", "other-token-000"):
        assert raw not in diff, diff
    assert "backend call 1 header x-leak: expected absent, actual 'good*** (12 chars)'" in diff, diff
    assert "backend call 1 header Authorization: expected 'Bear*** (22 chars)', actual 'Bear*** (21 chars)'" in diff

    exports = orders_input(tmp_path, write_keys)
    results = tmp_path / "results"
    res = run_cli(
        ["migrate", str(exports), "--out", str(results), "--llm", "fake", "--mock-backends", "--golden", str(golden)],
        stages=stages_with(leaky),
    )
    assert res.code == 0, res.err
    report = (results / "keys-v1" / "verification.json").read_text(encoding="utf-8")
    log = run_log(results)
    assert json.loads(report)["type"] == "failed"
    for raw in (GOOD_KEY, "prod-token-xyz", "other-token-000"):
        assert raw not in report and raw not in log and raw not in res.err, raw
    assert "good*** (12 chars)" in log, log


def test_CP7_X27_query_keys_cookies_and_registered_secrets_are_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    """[CP7-X27] The masker: a VerifyAPIKey query parameter in a backend path, a cookie, a short credential (length
    only) and a value a2m.redaction masks never show in a diff line."""
    from a2m import redaction
    from a2m.verify.masking import Masker

    monkeypatch.setattr(redaction, "_registered", {"registered-secret-value"})
    masker = Masker(query_names=["apikey"])
    masker.learn_target("/keys?apikey=QUERY-KEY-777&page=2")
    masker.learn_headers({"Cookie": "session=abc123def", "Authorization": "pw12"})
    text = masker.mask(
        "backend call 1: expected GET /keys?apikey=QUERY-KEY-777&page=2, got GET /keys?page=2\n"
        "header cookie: expected absent, actual 'session=abc123def'\n"
        "header authorization: expected absent, actual 'pw12'\n"
        "body: expected 'registered-secret-value', actual ''"
    )
    for raw in ("QUERY-KEY-777", "session=abc123def", "'pw12'", "registered-secret-value"):
        assert raw not in text, text
    assert "QUER*** (13 chars)" in text and "*** (4 chars)" in text and "page=2" in text, text


# ================================================================ CP7 adversarial round 4 (CP7-X29 .. X31)
# New blocks only; every line above is locked.


def _letters(seed: str, count: int) -> str:
    """``count`` letters G-V derived from ``seed``: no run of 8 of them shows up in a2m's own text by chance."""
    import hashlib

    out = ""
    block = 0
    while len(out) < count:
        digest = hashlib.sha256(f"{seed}-{block}".encode()).hexdigest()
        out += digest.translate(str.maketrans("0123456789abcdef", "GHIJKLMNOPQRSTUV"))
        block += 1
    return out[:count]


LONG_KEY = "LK" + _letters("api-key", 298)  # 300 characters, longer than a diff shows a value
LONG_JWT = f"eyJhbGciOiJSUzI1NiJ9.{_letters('claims', 320)}.{_letters('signature', 60)}"
OTHER_JWT = f"eyJhbGciOiJSUzI1NiJ9.{_letters('other-claims', 300)}.{_letters('other-signature', 60)}"
# Never in a request, a recording or a setting: only its shape says it is a credential.
STRAY_JWT = f"eyJhbGciOiJIUzI1NiJ9.{_letters('stray-claims', 280)}.{_letters('stray-signature', 50)}"
LONG_SECRETS = (LONG_KEY, LONG_JWT, OTHER_JWT, STRAY_JWT)


def leaked_pieces(text: str, secrets: Sequence[str] = LONG_SECRETS) -> list[str]:
    """Every 8-character piece of ``secrets`` found in ``text`` (any longer piece holds one of them)."""
    found = []
    for secret in secrets:
        for start in range(len(secret) - 7):
            piece = secret[start : start + 8]
            if piece in text and not piece.startswith("eyJhbGci"):  # the JWT header is public, not a secret
                found.append(piece)
    return found


def long_key_stage(context: Any) -> None:
    fill_key(Path(context.out_dir) / "mule-app", LONG_KEY)


def long_credential_exchange() -> dict[str, Any]:
    return keys_exchange(
        "long-credentials",
        request_headers={"x-api-key": LONG_KEY, "Authorization": f"Bearer {LONG_JWT}"},
        backend_headers={"X-Client": "a2m", "Authorization": f"Bearer {OTHER_JWT}"},
    )


def test_CP7_X29_long_credentials_never_leak_from_a_golden_mismatch_or_a_battery_failure(
    tmp_path: Path, backend: Any, run_cli: Any
) -> None:
    """[CP7-X29] Credentials longer than a diff shows a value (a 300-character API key a VerifyAPIKey reads, a
    Bearer JWT in Authorization, a JWT nobody registered) are masked on the whole value before it is cut short:
    neither the value nor any 8-character piece of it reaches a diff, verification.json, run.log or the console,
    for a golden mismatch and for a battery failure."""
    leak = {"X-Client": "a2m", "X-Leak": f"{LONG_KEY} Bearer {STRAY_JWT}"}
    golden = write_recordings(tmp_path / "golden", "keys-v1", {"01-long-credentials.json": long_credential_exchange()})

    gen = generated(write_keys(tmp_path / "bundles"), tmp_path / "out")
    direct = verify(gen, ForwardingRunner(add=leak), backend, golden=golden)
    assert direct.type == "failed", describe(direct)
    diff = direct.cases[0].diff
    assert "backend call 1 header Authorization: expected 'Bear*** (" in diff, diff
    assert "backend call 1 header x-leak: expected absent, actual 'LK" in diff, diff
    assert leaked_pieces(json.dumps(direct.to_json_data())) == [], diff

    exports = orders_input(tmp_path, write_keys)
    golden_results = tmp_path / "golden-results"
    res = run_cli(
        ["migrate", str(exports), "--out", str(golden_results), "--llm", "fake", "--golden", str(golden)],
        stages=stages_with(ForwardingRunner(add=leak)),
    )
    assert res.code == 0, res.err
    report = (golden_results / "keys-v1" / "verification.json").read_text(encoding="utf-8")
    assert json.loads(report)["type"] == "failed", report
    for text in (report, run_log(golden_results), res.out, res.err):
        assert leaked_pieces(text) == [], text

    from a2m.engine import generate, parse
    from a2m.verify import make_verify_stage

    battery_runner = ForwardingRunner(
        add={"X-Client": f"{LONG_KEY} Bearer {STRAY_JWT}", "Authorization": f"Bearer {LONG_JWT}"}
    )
    battery_results = tmp_path / "battery-results"
    res = run_cli(
        ["migrate", str(exports), "--out", str(battery_results), "--llm", "fake", "--mock-backends"],
        stages=[parse, generate, long_key_stage, make_verify_stage(runner=battery_runner)],
    )
    assert res.code == 0, res.err
    data = json.loads((battery_results / "keys-v1" / "verification.json").read_text(encoding="utf-8"))
    assert data["type"] == "failed" and data["ran"] > 0, data
    diffs = "\n".join(case["diff"] for case in data["cases"])
    assert "header X-Client: expected 'a2m', actual 'LK" in diffs, diffs
    for text in (json.dumps(data), run_log(battery_results), res.out, res.err):
        assert leaked_pieces(text) == [], text


def deploy_failure_log(key: str) -> str:
    return (
        "INFO  2026-10-04 10:00:00,000 [main] Deploying artifact 'keys-v1'\n"
        "ERROR 2026-10-04 10:00:01,000 [main] Failed to deploy artifact 'keys-v1', see below\n"
        "org.mule.runtime.deployment.model.api.DeploymentException: Failed to deploy artifact [keys-v1]\n"
        f"Caused by: org.mule.runtime.api.exception.MuleRuntimeException: Invalid value {key} for property "
        "verifyapikey.verify-key.allowedKeys\n"
    )


class EchoingDeployRunner(FakeRunner):
    """A runtime whose deployment fails with a log that echoes the API keys the deployed copy was given."""

    mule_version = "4.9.0"

    def __init__(self) -> None:
        super().__init__()
        self.given: list[dict[str, str]] = []

    def start(self, app: Any, *, backend_url: str) -> FakeHandle:
        from a2m.verify.mule import DeployError

        self.given.append(dict(app.properties))
        keys = ",".join(app.properties.values())
        raise DeployError(f"{app.name} failed to deploy", deploy_failure_log(keys))


def test_CP7_X30_a_deploy_failure_that_echoes_a_recorded_key_is_masked(
    tmp_path: Path, backend: Any, run_cli: Any
) -> None:
    """[CP7-X30] The golden replay allows the recorded API key in the deployed copy; when that deployment fails
    with a mule.log line that echoes the key, the key is masked in the result's message and log excerpt, in
    verification.json, run.log and on the console."""
    golden = write_recordings(tmp_path / "golden", "keys-v1", {"01-long-credentials.json": long_credential_exchange()})
    gen = generated(write_keys(tmp_path / "bundles"), tmp_path / "out", key=False)

    runner = EchoingDeployRunner()
    result = verify(gen, runner, backend, golden=golden)
    assert result.type == "failed", describe(result)
    assert any(LONG_KEY in value for given in runner.given for value in given.values()), runner.given
    assert "failed to deploy on Mule runtime 4.9.0" in result.message, result.message
    assert "LK" in result.message and leaked_pieces(result.message) == [], result.message
    assert leaked_pieces(result.log_excerpt) == [], result.log_excerpt

    from a2m.engine import generate, parse
    from a2m.verify import make_verify_stage

    results = tmp_path / "results"
    res = run_cli(
        ["migrate", str(orders_input(tmp_path, write_keys)), "--out", str(results), "--llm", "fake", "--golden",
         str(golden)],
        stages=[parse, generate, make_verify_stage(runner=EchoingDeployRunner())],
    )
    assert res.code == 0, res.err
    report = (results / "keys-v1" / "verification.json").read_text(encoding="utf-8")
    assert json.loads(report)["type"] == "failed", report
    for text in (report, run_log(results), res.out, res.err):
        assert leaked_pieces(text) == [], text


def test_CP7_X31_credential_shaped_values_are_masked_without_being_learned(tmp_path: Path) -> None:
    """[CP7-X31] The masker recognises credentials it never learned, at any length: the token after Bearer or
    Basic, a JWT, a cookie, and a header or query parameter a VerifyAPIKey reads; diff wording stays readable,
    masking twice changes nothing, every text of a result is masked, and compare.shown masks the whole value
    before cutting it short."""
    from a2m.parser import read_bundle
    from a2m.verify import compare
    from a2m.verify.masking import Masker
    from a2m.verify.model import CaseResult, ReviewFlag, UntestedPolicy, VerificationResult, VerificationType

    query_bundle = read_bundle(
        write_proxy(
            tmp_path / "bundles",
            "query-keys-v1",
            base_path="/qkeys",
            target_url="http://backend.example/qkeys",
            policies={"verify-key": policy_verify_key("request.queryparam.apikey")},
            request_steps=[("verify-key", None)],
        )
    )
    basic = _letters("basic-credentials", 280)
    cookie = _letters("session-cookie", 270)
    log_text = (
        f"Authorization: Bearer {LONG_JWT}\n"
        f'{{"proxy-authorization": "Basic {basic}", "accept": "*/*"}}\n'
        f"Cookie: session={cookie}; theme=dark\n"
        f"GET /qkeys?page=2&apikey={LONG_KEY}&x=1\n"
        f"jwt in a body: {STRAY_JWT}\n"
        "header cookie: expected absent, actual missing"
    )
    secrets = (*LONG_SECRETS, basic, cookie)
    masker = Masker.for_bundle(query_bundle)
    masked_text = masker.mask(log_text)
    assert leaked_pieces(masked_text, secrets) == [], masked_text
    assert "page=2&apikey=LKGH" not in masked_text and "&x=1" in masked_text, masked_text
    assert "header cookie: expected absent, actual missing" in masked_text, masked_text
    assert masker.mask(masked_text) == masked_text

    header_masker = Masker(header_names=["x-api-key"])
    assert leaked_pieces(header_masker.mask(f"x-api-key: {LONG_KEY}")) == []

    shown_plain = compare.shown(f"Bearer {STRAY_JWT}")
    assert leaked_pieces(shown_plain) == [], shown_plain
    learned = Masker(header_names=["x-custom-key"])
    learned.learn_headers({"X-Custom-Key": LONG_KEY})
    with learned.active():
        line = compare.header_diffs({"x-trace": "a"}, {"x-trace": LONG_KEY + "-suffix"})
    assert line and leaked_pieces("\n".join(line)) == [], line

    result = VerificationResult(
        VerificationType.FAILED,
        1,
        0,
        1,
        (CaseResult("c", None, False, f"diff Bearer {LONG_JWT}", 0),),
        (UntestedPolicy("p", "VerifyAPIKey", f"reason Bearer {LONG_JWT}"),),
        (ReviewFlag("p", "Quota", f"flag Bearer {LONG_JWT}"),),
        f"message Bearer {LONG_JWT}",
        f"excerpt Bearer {LONG_JWT}",
    )
    assert leaked_pieces(json.dumps(Masker().mask_result(result).to_json_data())) == []


# ================================================================ CP7 adversarial round 5 (CP7-X32 .. X34)
# New blocks only; every line above is locked.

SESSION_TOKEN = "ST" + _letters("session-token", 298)  # longer than a diff shows a value


def test_CP7_X32_a_set_cookie_token_echoed_in_a_json_body_is_masked(tmp_path: Path, backend: Any, run_cli: Any) -> None:
    """[CP7-X32] The recorded answer sets a session cookie (Set-Cookie: session=<token>; Path=/; HttpOnly) and
    echoes the same token in a JSON field; the app's answer lacks both. The token is masked in the body diff
    line too, in the result, verification.json, run.log and on the console, not only as a whole header."""
    exchange = keys_exchange(
        "session-echo",
        response_headers={**JSON_HEADERS, "Set-Cookie": f"session={SESSION_TOKEN}; Path=/; HttpOnly"},
        response_body=json.dumps({"id": 7, "session": SESSION_TOKEN}),
    )
    golden = write_recordings(tmp_path / "golden", "keys-v1", {"01-session-echo.json": exchange})
    gen = generated(write_keys(tmp_path / "bundles"), tmp_path / "out")

    direct = verify(gen, ForwardingRunner(), backend, golden=golden)
    assert direct.type == "failed", describe(direct)
    diff = direct.cases[0].diff
    assert "body field session: expected 'STQN*** (300 chars)', actual missing" in diff, diff
    assert leaked_pieces(json.dumps(direct.to_json_data()), [SESSION_TOKEN]) == [], diff

    results = tmp_path / "results"
    res = run_cli(
        ["migrate", str(orders_input(tmp_path, write_keys)), "--out", str(results), "--llm", "fake", "--golden",
         str(golden)],
        stages=stages_with(ForwardingRunner()),
    )
    assert res.code == 0, res.err
    report = (results / "keys-v1" / "verification.json").read_text(encoding="utf-8")
    assert json.loads(report)["type"] == "failed", report
    for text in (report, run_log(results), res.out, res.err):
        assert leaked_pieces(text, [SESSION_TOKEN]) == [], text


def test_CP7_X32_cookie_values_and_authorization_credentials_are_learned_one_by_one() -> None:
    """[CP7-X32] The masker learns each cookie value of Cookie and Set-Cookie (not attributes such as Path or
    HttpOnly), the credential after an Authorization scheme, and for Basic the decoded user:password, user and
    password; each is masked wherever it shows up alone."""
    import base64

    from a2m.verify.masking import Masker

    session = _letters("cookie-session", 40)
    csrf = _letters("cookie-csrf", 24)
    user, password = "svc" + _letters("basic-user", 9), _letters("basic-password", 30)
    basic = base64.b64encode(f"{user}:{password}".encode()).decode()
    proxy_token = _letters("proxy-token", 40)
    masker = Masker()
    masker.learn_headers(
        {
            "Set-Cookie": f"session={session}; Path=/api; Expires=Wed, 21 Oct 2026 07:28:00 GMT; HttpOnly",
            "Cookie": f'theme=darkblue; csrf="{csrf}"',
            "Authorization": f"Basic {basic}",
            "Proxy-Authorization": f"Token {proxy_token}",
        }
    )
    text = (
        f'{{"session": "{session}", "csrf": "{csrf}", "token": "{proxy_token}"}}\n'
        f"redirect to /login?s={session}\n"
        f"user {user} logged in with {password}; raw {user}:{password}; encoded {basic}\n"
        "the cookie path is /api"
    )
    masked_text = masker.mask(text)
    assert leaked_pieces(masked_text, [session, csrf, password, basic, proxy_token, user]) == [], masked_text
    assert "the cookie path is /api" in masked_text, masked_text


def test_CP7_X33_a_bad_recorded_path_never_echoes_a_secret(tmp_path: Path, backend: Any, run_cli: Any) -> None:
    """[CP7-X33] A recording whose path breaks the rules (a blank in the query, no leading /) or whose header
    name holds a line break is refused naming the file and the rule, without echoing the query, the text after
    the blank or the bad header: no secret in it reaches the error, verification.json, run.log or the console."""
    from a2m.verify.golden import RecordingError, load_exchanges

    secret = "QK" + _letters("query-secret", 30)
    bad_paths = {
        "blank": f"/keys?token=x {secret}",
        "blank-in-path": f"/keys {secret}",
        "no-slash": f"keys?token={secret}",
        "bad-header": "/keys",
    }
    for case, path in bad_paths.items():
        exchange = keys_exchange(case, path=path)
        if case == "bad-header":
            exchange["calls"][0]["request"]["headers"] = {f"X-Key\n{secret}": "v"}
        folder = tmp_path / "loose" / case
        write_recordings(folder, "keys-v1", {"01-bad.json": exchange})
        with pytest.raises(RecordingError) as caught:
            load_exchanges(folder, "keys-v1")
        message = str(caught.value)
        assert "01-bad.json" in message, message
        assert leaked_pieces(message, [secret]) == [], message

    golden = write_recordings(
        tmp_path / "golden", "keys-v1", {"01-bad.json": keys_exchange("bad", path=f"/keys?token=x {secret}")}
    )
    results = tmp_path / "results"
    res = run_cli(
        ["migrate", str(orders_input(tmp_path, write_keys)), "--out", str(results), "--llm", "fake", "--golden",
         str(golden)],
        stages=stages_with(ForwardingRunner()),
    )
    assert res.code == 0, res.err
    report = (results / "keys-v1" / "verification.json").read_text(encoding="utf-8")
    assert "01-bad.json" in report, report
    for text in (report, run_log(results), res.out, res.err):
        assert leaked_pieces(text, [secret]) == [], text


def test_CP7_X34_backend_paths_are_compared_as_sent() -> None:
    """[CP7-X34] A backend path is compared as sent: an encoded reserved character (%2F) is not the character
    itself, so /orders%2F7 and /orders/7 differ; only encoded unreserved characters (%7E is ~) and the case of
    the hex digits are normalised (RFC 3986 section 6.2.2)."""
    from a2m.verify.compare import target_diffs

    assert target_diffs("GET", "/orders%2F7", "GET", "/orders/7", "", where="backend call 1") != []
    assert target_diffs("GET", "/orders/7", "GET", "/orders%2F7", "", where="backend call 1") != []
    assert target_diffs("GET", "/objects/a%2Fb", "GET", "/objects/a/b", "", where="backend call 1") != []
    assert target_diffs("GET", "/a%3Fb", "GET", "/a?b", "", where="backend call 1") != []
    assert target_diffs("GET", "/a%7Eb", "GET", "/a~b", "", where="backend call 1") == []
    assert target_diffs("GET", "/a%2fb", "GET", "/a%2Fb", "", where="backend call 1") == []
    assert target_diffs("GET", "/a%41b", "GET", "/aAb?x=1", "x=1", where="backend call 1") != []
    assert target_diffs("GET", "/a%41b?x=1", "GET", "/aAb", "x=1", where="backend call 1") == []


# ================================================================ CP7 adversarial round 6 (CP7-X35 .. X36)
# New blocks only; every line above is locked.


def test_CP7_X35_the_mock_backend_matches_paths_the_way_the_comparison_does(tmp_path: Path, backend: Any) -> None:
    """[CP7-X35] A recorded backend answer queued for /keys/%7Ealice is the answer to the equivalent call
    /keys/~alice (the comparison already treats them as one path), so a faithful app is golden; a queued or
    standing answer for an encoded reserved character (/orders%2F7) never answers /orders/7."""
    exchange = keys_exchange(
        "tilde", path="/keys/~alice", backend_path="/keys/%7Ealice", response_body='{"user":"alice","n":35}'
    )
    exchange["backend_calls"][0]["response"]["body"] = '{"user":"alice","n":35}'
    golden = write_recordings(tmp_path / "golden", "keys-v1", {"01-tilde.json": exchange})
    gen = generated(write_keys(tmp_path / "bundles"), tmp_path / "out")
    result = verify(gen, ForwardingRunner(add={"X-Client": "a2m"}), backend, golden=golden)
    assert result.type == "golden", describe(result)

    backend.clear()
    backend.enqueue("GET", "/users/%7ealice", status=200, headers=dict(JSON_HEADERS), body=b'{"queued":1}')
    assert http_call(backend.port, "GET", "/users/~alice")[2] == b'{"queued":1}'
    backend.enqueue("GET", "/users/~bob", status=200, headers=dict(JSON_HEADERS), body=b'{"queued":2}')
    assert http_call(backend.port, "GET", "/users/%7Ebob?x=1")[2] == b'{"queued":2}'
    backend.respond("GET", "/files/a%2fb", status=200, headers=dict(JSON_HEADERS), body=b'{"standing":1}')
    assert http_call(backend.port, "GET", "/files/a%2Fb")[2] == b'{"standing":1}'
    assert http_call(backend.port, "GET", "/files/a/b")[2] == BACKEND_BODY
    backend.enqueue("GET", "/orders%2F7", status=200, headers=dict(JSON_HEADERS), body=b'{"queued":3}')
    assert http_call(backend.port, "GET", "/orders/7")[2] == BACKEND_BODY
    assert [call.path for call in backend.calls()] == [
        "/users/~alice", "/users/%7Ebob", "/files/a%2Fb", "/files/a/b", "/orders/7"
    ]


def test_CP7_X36_content_type_parameters_that_change_meaning_are_compared() -> None:
    """[CP7-X36] The media type is compared without case and every parameter the expectation names must match:
    a different multipart boundary or charset is a mismatch, while case, spacing and quoting are not; a charset
    only one side names is fine when it is UTF-8 or US-ASCII and a mismatch otherwise."""
    from a2m.verify.compare import compare_response

    body = b"--original\r\nContent-Type: text/plain\r\n\r\nhi\r\n--original--\r\n"

    def check(expected: str, actual: str, payload: bytes = b"{}") -> Any:
        return compare_response(
            response(200, {"Content-Type": expected}, payload), response(200, {"Content-Type": actual}, payload)
        )

    boundary = check("multipart/mixed; boundary=original", "multipart/mixed; boundary=wrong", body)
    assert not boundary.matched
    assert "header Content-Type: expected 'multipart/mixed; boundary=original', actual " in boundary.diff
    assert not check("multipart/mixed; boundary=original", "multipart/mixed", body).matched
    assert not check("multipart/mixed; boundary=original", "multipart/mixed; boundary=ORIGINAL", body).matched
    assert check("multipart/mixed; boundary=original", 'Multipart/Mixed;BOUNDARY="original"', body).matched
    assert check('multipart/form-data; boundary="a;b"', "multipart/form-data; boundary=\"a;b\"", body).matched
    assert not check('multipart/form-data; boundary="a;b"', "multipart/form-data; boundary=a", body).matched

    assert not check("text/plain; charset=UTF-8", "text/plain; charset=ISO-8859-1").matched
    assert not check("application/json; charset=utf-8", "application/json; charset=utf-16").matched
    assert not check("text/plain", "text/plain; charset=ISO-8859-1").matched
    assert not check("text/plain; charset=windows-1252", "text/plain").matched
    assert check("text/plain; charset=UTF-8", "TEXT/PLAIN;charset=utf-8").matched
    assert check("application/json", "application/json; charset=UTF-8").matched
    assert check("application/json; charset=UTF-8", "application/json").matched
    assert check("text/plain; charset=us-ascii", "text/plain").matched
    assert not check("application/json", "application/xml").matched
    assert not check("application/json; version=2", "application/json; version=1").matched
    assert check("application/json", "application/json; version=1").matched


def test_CP7_X36_a_golden_replay_fails_when_the_multipart_boundary_or_charset_changes(
    tmp_path: Path, backend: Any
) -> None:
    """[CP7-X36] Apigee answered multipart with boundary=original and the app passes on a backend answer whose
    body is the same but whose boundary (or charset) differs: the replay is failed, naming Content-Type; the
    same answer with only case and quoting changed is golden."""
    gen = generated(write_keys(tmp_path / "bundles"), tmp_path / "out")
    body = "--original\r\nContent-Type: text/plain\r\n\r\nhi\r\n--original--\r\n"
    outcomes = {}
    for case, live in {
        "boundary": "multipart/mixed; boundary=wrong",
        "charset": "multipart/mixed; boundary=original; charset=ISO-8859-1",
        "harmless": 'Multipart/Mixed; BOUNDARY="original"',
    }.items():
        exchange = keys_exchange(
            case, response_headers={"Content-Type": "multipart/mixed; boundary=original"}, response_body=body
        )
        exchange["backend_calls"][0]["response"] = {"status": 200, "headers": {"Content-Type": live}, "body": body}
        golden = write_recordings(tmp_path / "golden" / case, "keys-v1", {f"01-{case}.json": exchange})
        backend.clear()
        result = verify(gen, ForwardingRunner(add={"X-Client": "a2m"}), backend, golden=golden)
        outcomes[case] = (result.type, result.cases[0].diff if result.cases else "")
    assert outcomes["boundary"][0] == "failed", outcomes
    assert "header Content-Type: expected 'multipart/mixed; boundary=original'" in outcomes["boundary"][1], outcomes
    assert outcomes["charset"][0] == "failed", outcomes
    assert outcomes["harmless"][0] == "golden", outcomes


# ================================================================ Direct JVM launch (CP7-X37 .. X39)
# a2m starts Mule's JVM itself, without Mule's Tanuki wrapper (no ARM Linux or 64-bit macOS build in Mule 4.9.0).


def test_CP7_X37_mule_is_started_as_one_java_command_naming_its_home_and_base(tmp_path: Path) -> None:
    """[CP7-X37] The JVM command: java first, the org.mule.boot module from MULE_HOME/lib/boot last, absolute
    -Dmule.home and -Dmule.base (the base marks the JVM as this runtime's), and nothing from Tanuki."""
    from a2m.verify.mule import jvm_command

    java, home, base = tmp_path / "jdk" / "bin" / "java", tmp_path / "mule home", tmp_path / "results" / "base"
    argv = jvm_command(java, home, base)

    assert argv[0] == str(java)
    assert argv[-1] == "--module=org.mule.boot/org.mule.runtime.module.reboot.MuleContainerBootstrap"
    assert f"-Dmule.home={home}" in argv and f"-Dmule.base={base}" in argv, argv
    assert f"--module-path={home / 'lib' / 'boot'}" in argv, argv
    basic = "org.mule.runtime.module.boot.internal.MuleContainerBasicWrapper"
    assert f"-Dmule.bootstrap.container.wrapper.class={basic}" in argv, argv
    assert "-Xmx1024m" in argv and "-XX:+ExitOnOutOfMemoryError" in argv, argv
    assert not [arg for arg in argv if "tanuki" in arg.lower()], argv
    assert argv.index("--add-modules=java.se,org.mule.runtime.jpms.utils,com.fasterxml.jackson.core") < len(argv) - 1


def test_CP7_X38_a_mule_home_counts_only_with_its_boot_module_jar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[CP7-X38] A Mule home is found by lib/boot/mule-module-reboot-*.jar (the module a2m starts), not bin/mule."""
    from a2m.verify.tools import HINTS, MULE, find_mule_home

    launcher_only = tmp_path / "launcher-only"
    (launcher_only / "bin").mkdir(parents=True)
    write_stub(launcher_only / "bin" / "mule", tmp_path / "calls")
    monkeypatch.setenv("A2M_MULE_HOME", str(launcher_only))
    assert find_mule_home() is None
    write_mule_boot(tmp_path / "mule4")
    monkeypatch.setenv("A2M_MULE_HOME", str(tmp_path / "mule4"))
    assert find_mule_home() == (tmp_path / "mule4").absolute()
    assert "lib/boot/mule-module-reboot-" in HINTS[MULE] and "bin/mule" not in HINTS[MULE]


def test_CP7_X39_process_facts_from_ps_match_proc(tmp_path: Path) -> None:
    """[CP7-X39] The ps backend (macOS) reads the same process group, liveness and command line as /proc,
    arguments with spaces included, and a stable start time; a zombie counts as ended and a gone PID as None."""
    from a2m.verify import mule

    if not Path("/proc/self/stat").exists() or shutil.which("ps") is None:
        pytest.skip("needs /proc and ps to compare them")
    base = tmp_path / "a base"
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", f"-Dmule.base={base}", "x"], start_new_session=True
    )
    try:
        deadline = time.monotonic() + 10
        while (early := mule._ProcTable().facts(child.pid)) is None or "-Dmule.base" not in early.command:
            assert time.monotonic() < deadline, "the child never ran its command"  # exec has not happened yet
            time.sleep(0.02)
        proc, ps = mule._ProcTable().facts(child.pid), mule._PsTable().facts(child.pid)
        assert proc is not None and ps is not None
        assert (ps.running, ps.group, ps.command) == (proc.running, proc.group, proc.command) == (
            True, child.pid, f"{sys.executable} -c import time; time.sleep(60) -Dmule.base={base} x"
        )
        assert mule._PsTable().facts(child.pid) == ps  # the start time is the same on every read
        assert mule._names_base(ps.command, base) and not mule._names_base(ps.command, tmp_path / "a bas")
        child.kill()
        deadline = time.monotonic() + 10
        while (facts := mule._ProcTable().facts(child.pid)) is not None and facts.running:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        zombie = mule._PsTable().facts(child.pid)
        assert zombie is not None and zombie.running is False
    finally:
        child.kill()
        child.wait()
    assert mule._PsTable().facts(child.pid) is None
