"""CP6 adversarial round 6: an AI equals-ignore-case condition on a header that can be missing, on the local runtime.

Marked ``runtime`` like the other files here: excluded from a plain ``pytest
-q``, skipped with a reason when java, mvn or MULE_HOME is missing, and failing
instead under A2M_REQUIRE_RUNTIME=1 (see tests/runtime/conftest.py). Run it with
``A2M_REQUIRE_RUNTIME=1 mise exec -- .venv/bin/python -m pytest -q -m runtime
tests/runtime/test_cp6_ai_runtime.py``.

The 'cp6-ai-runtime' bundle (one ProxyEndpoint, /cp6) has three request PreFlow
steps: AM-Backend sets the backend's address; AM-Cp5 (condition
``request.header.X-Foo := "bar"``, written by CP5) sets the request header
X-Cp5-Hit; AM-Ai (condition ``request.header.X-Foo =| "bar"``, which CP5 refuses)
sets X-Ai-Hit, and its condition is the AI's structured answer
``request.header.X-Foo equals-ignore-case "bar"``, checked and written by a2m.
A loopback backend records the headers each request reaches it with.
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
    _set_header,
    _step,
    call,
    make_handler,
    set_listen_port,
)

APP = "cp6-ai-runtime"
AI_CONDITION = {"variable": "request.header.X-Foo", "operator": "equals-ignore-case", "value": "bar"}


class ConditionAnswers:
    """A provider that answers every condition with :data:`AI_CONDITION` and records what it was asked."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def complete(self, request: Any) -> str:
        self.asked.append(str(request.name))
        return json.dumps(
            {"status": "translated", "confidence": "high", "notes": "cp6 round 6", "condition": AI_CONDITION}
        )


@pytest.fixture(scope="module")
def cp6_backend() -> Iterator[Backend]:
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


def write_cp6_bundle(parent: Path, backend_port: int) -> Path:
    policies = {
        "AM-Backend": _policy(
            "AssignMessage",
            "AM-Backend",
            "    <AssignVariable>\n        <Name>a2mtest.backend</Name>\n"
            f"        <Value>127.0.0.1:{backend_port}</Value>\n    </AssignVariable>\n",
        ),
        "AM-Cp5": _set_header("AM-Cp5", "X-Cp5-Hit", "yes", "request"),
        "AM-Ai": _set_header("AM-Ai", "X-Ai-Hit", "yes", "request"),
    }
    items = "".join(f"        <Policy>{p}</Policy>\n" for p in policies)
    proxy = (
        XML_HEAD + '<ProxyEndpoint name="default">\n'
        '    <PreFlow name="PreFlow">\n'
        f"        <Request>{_step('AM-Backend')}"
        f"{_step('AM-Cp5', 'request.header.X-Foo := &quot;bar&quot;')}"
        f"{_step('AM-Ai', 'request.header.X-Foo =| &quot;bar&quot;')}</Request>\n"
        "        <Response/>\n    </PreFlow>\n    <Flows/>\n"
        '    <PostFlow name="PostFlow">\n        <Request/>\n        <Response/>\n    </PostFlow>\n'
        "    <HTTPProxyConnection>\n        <BasePath>/cp6</BasePath>\n        <VirtualHost>default</VirtualHost>\n"
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


class Cp6App:
    def __init__(self, port: int, result: Any, provider: ConditionAnswers) -> None:
        self.port = port
        self.result = result
        self.provider = provider


@pytest.fixture(scope="module")
def cp6_app(
    mule_runtime: Any, cp6_backend: Backend, free_port: Callable[[], int], tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Cp6App]:
    from a2m.generator import generate_project
    from a2m.parser import read_bundle
    from a2m.verify.mule import BuildError, DeployError, package

    work = tmp_path_factory.mktemp("cp6-ai-runtime")
    bundle = read_bundle(write_cp6_bundle(work / "bundles", cp6_backend.port))
    project = work / APP / "mule-app"
    provider = ConditionAnswers()
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
        yield Cp6App(port, result, provider)
    finally:
        with contextlib.suppress(Exception):
            mule_runtime.runner.undeploy(APP, timeout=60)


@pytest.mark.runtime
def test_CP6_T54_ai_equals_ignore_case_on_a_missing_header_is_false_and_never_fails_the_request(
    cp6_app: Cp6App, cp6_backend: Backend
) -> None:
    """[CP6-T54] The reviewer's repro on the runtime: the AI's equals-ignore-case on X-Foo is used, and a request
    without X-Foo gets the backend's 200 with the step skipped (false, as in Apigee), never a DataWeave error; the
    same holds for CP5's own := on the same header. With X-Foo it matches without case, as Apigee does."""
    records = [r for r in cp6_app.result.conditions if 'X-Foo =| "bar"' in str(r.original)]
    assert len(records) == 1, [str(r.original) for r in cp6_app.result.conditions]
    assert (records[0].method, records[0].ok, records[0].needs_review) == ("ai", True, False), records[0].reason
    assert "lower(" in str(records[0].dw)

    for headers, hit in (({}, None), ({"X-Foo": "BAR"}, "yes"), ({"X-Foo": "bar"}, "yes"), ({"X-Foo": "baz"}, None)):
        cp6_backend.clear()
        status, _, body = call(cp6_app.port, "GET", "/cp6/x", headers)
        assert status == 200, (headers, status, body[:500])
        seen = cp6_backend.seen()
        assert len(seen) == 1, (headers, seen)
        assert seen[0].headers.get("x-ai-hit") == hit, (headers, seen[0].headers)
        assert seen[0].headers.get("x-cp5-hit") == hit, (headers, seen[0].headers)
