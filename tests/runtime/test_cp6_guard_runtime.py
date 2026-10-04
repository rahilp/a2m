"""CP6 adversarial round 9: a callout guard's default, on the local runtime.

Marked ``runtime`` like the other files here: excluded from a plain ``pytest
-q``, skipped with a reason when java, mvn or MULE_HOME is missing, and failing
instead under A2M_REQUIRE_RUNTIME=1 (see tests/runtime/conftest.py). Run it with
``A2M_REQUIRE_RUNTIME=1 mise exec -- .venv/bin/python -m pytest -q -m runtime
tests/runtime/test_cp6_guard_runtime.py``.

The 'cp6-guard-runtime' bundle (one ProxyEndpoint, /cp6g) has two request
PreFlow steps: AM-Backend sets the backend's address, and JS-Mode is a
JavaScript callout that treats a missing mode as 'fallback' and then marks the
request when the mode is 'fallback'. The AI's answer for it copies the X-Mode
header into vars.mode and guards the mark with
``#[(vars.mode default 'fallback') == 'fallback']``: a request without X-Mode
must be marked (before the round-9 fix a2m dropped the default and wrote
``vars['mode'] == "fallback"``, which skipped the mark).
"""

from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Callable, Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from .test_cp5_conditions_runtime import (
    BUILD_TIMEOUT,
    DEPLOY_TIMEOUT,
    XML_HEAD,
    Backend,
    _policy,
    _step,
    call,
    make_handler,
    set_listen_port,
)

APP = "cp6-guard-runtime"
SCRIPT = (
    "var mode = context.getVariable('request.header.X-Mode') || 'fallback';\n"
    "if (mode == 'fallback') { context.setVariable('request.header.X-Guard-Hit', 'yes'); }\n"
)
GUARD = "#[(vars.mode default 'fallback') == 'fallback']"
MULE = (
    "<set-variable variableName=\"mode\" value=\"#[attributes.headers['x-mode']]\"/>"
    f'<choice><when expression="{GUARD}">'
    '<set-variable variableName="a2mRequestHeaders" value="#[output application/java --- '
    "(vars.a2mRequestHeaders default attributes.headers) ++ {'x-guard-hit': 'yes'}]\"/>"
    "</when></choice>"
)

WRITES = {
    "request_headers": ["x-guard-hit"],
    "query_params": [],
    "verb": False,
    "payload": False,
    "response_headers": [],
    "variables": ["mode"],
}


class CalloutAnswers:
    """A provider that answers every callout with :data:`MULE` and records what it was asked."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def complete(self, request: Any) -> str:
        self.asked.append(str(request.name))
        return json.dumps(
            {"status": "translated", "confidence": "high", "notes": "cp6 round 9", "mule": MULE, "writes": WRITES}
        )


@pytest.fixture(scope="module")
def guard_backend() -> Iterator[Backend]:
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


def write_guard_bundle(parent: Path, backend_port: int) -> Path:
    policies = {
        "AM-Backend": _policy(
            "AssignMessage",
            "AM-Backend",
            "    <AssignVariable>\n        <Name>a2mtest.backend</Name>\n"
            f"        <Value>127.0.0.1:{backend_port}</Value>\n    </AssignVariable>\n",
        ),
        "JS-Mode": _policy("Javascript", "JS-Mode", "    <ResourceURL>jsc://mode.js</ResourceURL>\n"),
    }
    items = "".join(f"        <Policy>{p}</Policy>\n" for p in policies)
    proxy = (
        XML_HEAD + '<ProxyEndpoint name="default">\n'
        '    <PreFlow name="PreFlow">\n'
        f"        <Request>{_step('AM-Backend')}{_step('JS-Mode')}</Request>\n"
        "        <Response/>\n    </PreFlow>\n    <Flows/>\n"
        '    <PostFlow name="PostFlow">\n        <Request/>\n        <Response/>\n    </PostFlow>\n'
        "    <HTTPProxyConnection>\n        <BasePath>/cp6g</BasePath>\n        <VirtualHost>default</VirtualHost>\n"
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
        "apiproxy/resources/jsc/mode.js": SCRIPT,
        **{f"apiproxy/policies/{name}.xml": text for name, text in policies.items()},
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


class GuardApp:
    def __init__(self, port: int, result: Any, provider: CalloutAnswers) -> None:
        self.port = port
        self.result = result
        self.provider = provider


@pytest.fixture(scope="module")
def guard_app(
    mule_runtime: Any, guard_backend: Backend, free_port: Callable[[], int], tmp_path_factory: pytest.TempPathFactory
) -> Iterator[GuardApp]:
    from a2m.generator import generate_project
    from a2m.parser import read_bundle
    from a2m.verify.mule import BuildError, DeployError, package

    work = tmp_path_factory.mktemp("cp6-guard-runtime")
    bundle = read_bundle(write_guard_bundle(work / "bundles", guard_backend.port))
    project = work / APP / "mule-app"
    provider = CalloutAnswers()
    result = generate_project(bundle, project, shared_flows=(), results_root=work, provider=provider)
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
        yield GuardApp(port, result, provider)
    finally:
        with contextlib.suppress(Exception):
            mule_runtime.runner.undeploy(APP, timeout=60)


@pytest.mark.runtime
def test_CP6_T59_guard_default_selects_the_branch_for_a_missing_value_on_the_runtime(
    guard_app: GuardApp, guard_backend: Backend
) -> None:
    """[CP6-T59] The reviewers' repro on the runtime: the AI's guard (vars.mode default 'fallback') == 'fallback' is
    used without review, and a request without X-Mode takes the fallback branch, as the original script does; a
    mode of 'fallback' takes it too and any other mode does not."""
    steps = [s for s in guard_app.result.policies if str(s.name) == "JS-Mode"]
    assert len(steps) == 1, [str(s.name) for s in guard_app.result.policies]
    assert (steps[0].method, steps[0].needs_review) == ("ai", False), steps[0]

    for headers, hit in (({}, "yes"), ({"X-Mode": "fallback"}, "yes"), ({"X-Mode": "other"}, None)):
        guard_backend.clear()
        status, _, body = call(guard_app.port, "GET", "/cp6g/x", headers)
        assert status == 200, (headers, status, body[:500])
        seen = guard_backend.seen()
        assert len(seen) == 1, (headers, seen)
        assert seen[0].headers.get("x-guard-hit") == hit, (headers, seen[0].headers)
