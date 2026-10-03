"""CP3 adversarial round 1: redeploying an app name that is already deployed (CP3-T39, marked ``runtime``).

Kept out of tests/runtime/test_cp3_deploy.py, whose plumbing test CP3-T25 counts
that file's runtime tests. Reuses its fixture proxy, build helper and loopback
backend; the Mule runtime is the session one from tests/runtime/conftest.py.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from .test_cp3_deploy import DEPLOY_TIMEOUT, Backend, backend, build_app, call, pid_alive

__all__ = ["backend"]  # the loopback backend fixture, used by name below


def _answers(port: int, path: str, want: tuple[int, bytes], seconds: float = 30.0) -> bool:
    """True once a request to ``port`` gets ``want`` back (status, body) within ``seconds``."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if call(port, "GET", path)[::2] == want:
                return True
        except OSError:
            pass
        time.sleep(0.5)
    return False


@pytest.mark.runtime
def test_CP3_T39_redeploying_under_the_same_name_proves_the_new_deployment(
    mule_runtime: Any, backend: Backend, free_port: Callable[[], int], tmp_path: Path
) -> None:
    """[CP3-T39] Redeploying under an app name already deployed waits for the new app, and reports its failure."""
    from a2m.verify.mule import DeployError

    runner = mule_runtime.runner
    name = "runtime-redeploy"
    first_port, second_port = free_port(), free_port()
    _, first = build_app(tmp_path / "v1", name, "/redo", backend.port, first_port)
    _, second = build_app(tmp_path / "v2", name, "/redo", backend.port, second_port)
    _, broken = build_app(tmp_path / "v3", name, "/redo", backend.port, free_port(), drop_port=True)
    try:
        runner.deploy(first, app_name=name, timeout=DEPLOY_TIMEOUT)
        assert call(first_port, "GET", "/redo/a")[::2] == (200, b"echo-7f3a:/backend/a")

        runner.deploy(second, app_name=name, timeout=DEPLOY_TIMEOUT)
        # deploy returned, so the new app (listening on its own port) must already answer.
        assert call(second_port, "GET", "/redo/b")[::2] == (200, b"echo-7f3a:/backend/b")

        with pytest.raises(DeployError) as failure:
            runner.deploy(broken, app_name=name, timeout=DEPLOY_TIMEOUT)
        assert name in str(failure.value.log_excerpt)
        assert not (Path(mule_runtime.mule_base) / "apps" / f"{name}-anchor.txt").exists()
        assert not _answers(second_port, "/redo/c", (200, b"echo-7f3a:/backend/c"), seconds=2)
        pids = list(runner.pids)
        assert pids and all(pid_alive(int(pid)) for pid in pids)
    finally:
        with contextlib.suppress(Exception):
            runner.undeploy(name, timeout=60)


# ---------------------------------------------------------------------------
# CP3 adversarial round 2: start and deploy only count evidence from their own action.
#
# CP3-T40 to CP3-T42 are unmarked and need no Java or Mule: a fake MULE_HOME whose
# bin/mule is a small shell script stands in for the runtime and writes the log the
# way the case needs. CP3-T43 is marked ``runtime`` and restarts the real runtime.

import os  # noqa: E402
import stat  # noqa: E402
import textwrap  # noqa: E402

from .conftest import START_TIMEOUT, stop_and_reap  # noqa: E402
from .test_cp3_deploy import processes_mentioning  # noqa: E402

STALE_START = "INFO  2026-10-01 10:00:00 org.mule.runtime: Mule is up and kicking (every 5000ms)\n"


def _fake_mule_home(root: Path, script: str) -> Path:
    """A MULE_HOME with empty conf/ and services/ and ``bin/mule`` running ``script`` (sh, cwd = MULE_BASE)."""
    home = root / "fake-mule-home"
    for part in ("bin", "conf", "services"):
        (home / part).mkdir(parents=True, exist_ok=True)
    launcher = home / "bin" / "mule"
    launcher.write_text("#!/bin/sh\n" + textwrap.dedent(script), encoding="utf-8")
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return home


def _base_with_log(root: Path, text: str) -> Path:
    """A MULE_BASE whose logs/mule.log already holds ``text`` (an earlier run's log)."""
    base = root / "mule-base"
    (base / "logs").mkdir(parents=True)
    (base / "logs" / "mule.log").write_text(text, encoding="utf-8")
    return base


def _all_gone(pids: list[int], seconds: float = 20.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not any(pid_alive(pid) for pid in pids):
            return True
        time.sleep(0.2)
    return False


@pytest.mark.parametrize(
    "script",
    [
        pytest.param("echo 'Error: JAVA_HOME points to a missing folder' >&2\nexit 3\n", id="launch-exits"),
        pytest.param("exec sleep 300\n", id="launch-hangs-silent"),
        pytest.param("echo 'starting again' >> logs/mule.log\nexit 1\n", id="launch-logs-then-exits"),
    ],
)
def test_CP3_T40_an_earlier_runs_startup_line_does_not_make_a_failed_launch_ready(
    tmp_path: Path, script: str
) -> None:
    """[CP3-T40] mule.log from an earlier run says "up and kicking", the new launch fails: start raises."""
    from a2m.verify.mule import MuleError, MuleRunner

    base = _base_with_log(tmp_path, "earlier run\n" + STALE_START + "earlier run stopped\n")
    runner = MuleRunner(mule_home=_fake_mule_home(tmp_path, script), mule_base=base)
    try:
        with pytest.raises(MuleError):
            runner.start(timeout=4)
        pids = [int(pid) for pid in runner.pids]
        assert pids, "the runner recorded no PID for the launch"
        assert _all_gone(pids), "a failed start left a launched process running"
    finally:
        stop_and_reap(runner)


@pytest.mark.parametrize(
    "script",
    [
        pytest.param(
            "sleep 1\necho 'INFO Mule is up and kicking (every 5000ms)' >> logs/mule.log\nexec sleep 300\n",
            id="appended",
        ),
        pytest.param(
            "sleep 1\nmv logs/mule.log logs/mule.log.1\n"
            "echo 'INFO Mule is up and kicking (every 5000ms)' > logs/mule.log\nexec sleep 300\n",
            id="rolled-over",
        ),
        pytest.param(
            "sleep 1\necho 'INFO Mule is up and kicking (every 5000ms)' > logs/mule.log\nexec sleep 300\n",
            id="truncated",
        ),
    ],
)
def test_CP3_T41_a_launch_that_logs_its_own_startup_line_is_ready(tmp_path: Path, script: str) -> None:
    """[CP3-T41] The new launch's own "up and kicking" makes start return, also after the log rolled over."""
    from a2m.verify.mule import MuleRunner

    base = _base_with_log(tmp_path, "earlier run\n" * 200 + STALE_START)
    runner = MuleRunner(mule_home=_fake_mule_home(tmp_path, script), mule_base=base)
    try:
        started = time.monotonic()
        runner.start(timeout=30)
        # The fake runtime writes its line after a 1 s pause; returning earlier means the old line counted.
        assert time.monotonic() - started >= 0.9
        pids = [int(pid) for pid in runner.pids]
        assert pids and all(pid_alive(pid) for pid in pids)
        runner.stop(timeout=20)
        assert _all_gone(pids)
    finally:
        stop_and_reap(runner)


# The fake runtime for CP3-T42: once apps/<name>.jar appears it writes a failure line for
# that app into mule.log and then rolls the log over (the failure ends up in mule.log.1).
FAILING_RUNTIME = """\
echo 'INFO Mule is up and kicking (every 5000ms)' >> logs/mule.log
while [ ! -f apps/rolled-app.jar ]; do sleep 0.1; done
echo "ERROR Failed to deploy artifact [rolled-app] (new failure)" >> logs/mule.log
echo "Caused by: the new deployment is broken" >> logs/mule.log
mv logs/mule.log logs/mule.log.1
echo 'INFO rolled over' > logs/mule.log
exec sleep 300
"""


def test_CP3_T42_a_deploy_failure_written_just_before_the_log_rolled_over_is_reported(tmp_path: Path) -> None:
    """[CP3-T42] A deploy failure logged just before mule.log rolled over is still reported as that failure."""
    from a2m.verify.mule import DeployError, MuleRunner

    base = _base_with_log(tmp_path, "ERROR Failed to deploy artifact [other-app] (earlier)\n" * 50)
    runner = MuleRunner(mule_home=_fake_mule_home(tmp_path, FAILING_RUNTIME), mule_base=base)
    jar = tmp_path / "rolled-app-mule-application.jar"
    jar.write_bytes(b"not really a jar")
    try:
        runner.start(timeout=30)
        with pytest.raises(DeployError) as failure:
            runner.deploy(jar, app_name="rolled-app", timeout=10)
        assert "failed to deploy" in str(failure.value)
        assert "new failure" in failure.value.log_excerpt
        assert "the new deployment is broken" in failure.value.log_excerpt
        assert "earlier" not in failure.value.log_excerpt
        assert not (base / "apps" / "rolled-app-anchor.txt").exists()
    finally:
        stop_and_reap(runner)


@pytest.mark.runtime
def test_CP3_T43_restarting_the_runtime_under_the_same_base_proves_each_start(
    runtime_tools: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[CP3-T43] Start, stop, a failing start, then a good start under one MULE_BASE: each start is proven on its own."""
    from a2m.verify.mule import MuleError, MuleRunner

    base = tmp_path / "mule-base-restart"
    base.mkdir()
    runner = MuleRunner(mule_home=Path(runtime_tools.mule_home), mule_base=base)
    recorded: list[int] = []
    try:
        runner.start(timeout=START_TIMEOUT)
        first = [int(pid) for pid in runner.pids]
        recorded += first
        runner.stop(timeout=60)
        assert _all_gone(first, 60)
        assert "Mule is up and kicking" in (base / "logs" / "mule.log").read_text(encoding="utf-8", errors="replace")

        # The same base again, but Java cannot be found: the earlier run's startup line must not count.
        monkeypatch.setenv("JAVA_HOME", str(tmp_path / "no-such-java"))
        with pytest.raises(MuleError):
            runner.start(timeout=60)
        failed = [int(pid) for pid in runner.pids]
        recorded += failed
        assert _all_gone(failed, 60)
        monkeypatch.undo()

        runner.start(timeout=START_TIMEOUT)
        second = [int(pid) for pid in runner.pids]
        recorded += second
        assert len(second) > 1, "the restarted runtime's wrapper and JVM were not recorded"
        assert all(pid_alive(pid) for pid in second)
        assert not set(second) & set(first)
        runner.stop(timeout=60)
        assert _all_gone(second, 60)
        assert processes_mentioning(str(base)) == []
    finally:
        with contextlib.suppress(Exception):
            runner.stop(timeout=60)
        for pid in recorded:
            if pid_alive(pid):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, 9)


# ---------------------------------------------------------------------------
# CP3 adversarial round 4: a flow error never sends Mule's error details to the caller (CP3-T47).
#
# The target is a loopback port first with nothing listening (connection refused), then with a
# server that accepts and never answers (timeout). The caller must get a fixed Apigee-style JSON
# fault with no target host, port or path in it; the details belong in the app's log only.

import json  # noqa: E402
import socket  # noqa: E402
import threading  # noqa: E402

from .test_cp3_deploy import (  # noqa: E402
    BUILD_TIMEOUT,
    listener_port_key,
    parse_bundle,
    properties_file,
    read_properties,
    write_properties,
)

HIDDEN_PATH = "internal-backend-9c41"
TARGET_TIMEOUT_MS = 2000


def _write_timeout_bundle(parent: Path, name: str, base_path: str, target_url: str) -> Path:
    """Like the deploy file's fixture proxy, with io.timeout.millis set on the target."""
    head = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    empty = "<Request/><Response/>"
    files = {
        f"apiproxy/{name}.xml": head + f'<APIProxy revision="1" name="{name}"><DisplayName>{name}</DisplayName>'
        "<Policies/><ProxyEndpoints><ProxyEndpoint>default</ProxyEndpoint></ProxyEndpoints>"
        "<TargetEndpoints><TargetEndpoint>default</TargetEndpoint></TargetEndpoints></APIProxy>\n",
        "apiproxy/proxies/default.xml": head + '<ProxyEndpoint name="default">'
        f'<PreFlow name="PreFlow">{empty}</PreFlow><Flows/><PostFlow name="PostFlow">{empty}</PostFlow>'
        f"<HTTPProxyConnection><BasePath>{base_path}</BasePath><VirtualHost>default</VirtualHost>"
        '</HTTPProxyConnection><RouteRule name="default"><TargetEndpoint>default</TargetEndpoint></RouteRule>'
        "</ProxyEndpoint>\n",
        "apiproxy/targets/default.xml": head + '<TargetEndpoint name="default">'
        f'<PreFlow name="PreFlow">{empty}</PreFlow><Flows/><PostFlow name="PostFlow">{empty}</PostFlow>'
        "<HTTPTargetConnection><Properties>"
        f'<Property name="io.timeout.millis">{TARGET_TIMEOUT_MS}</Property></Properties>'
        f"<URL>{target_url}</URL></HTTPTargetConnection></TargetEndpoint>\n",
    }
    root = parent / name
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


class _SilentServer:
    """Accepts connections on a port and never answers, so the proxy's request times out."""

    def __init__(self, port: int) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", port))
        self.sock.listen(16)
        self.sock.settimeout(0.5)
        self.held: list[socket.socket] = []
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def _accept(self) -> None:
        while not self.stopping.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            self.held.append(conn)

    def close(self) -> None:
        self.stopping.set()
        self.thread.join(timeout=5)
        for conn in self.held:
            with contextlib.suppress(OSError):
                conn.close()
        self.sock.close()


def _log_text(mule_base: Path) -> str:
    folder = mule_base / "logs"
    return "\n".join(
        p.read_text(encoding="utf-8", errors="replace") for p in sorted(folder.rglob("*.log")) if p.is_file()
    )


def _assert_generic_fault(
    answer: tuple[int, dict[str, str], bytes], status: int, errorcode: str, target_port: int
) -> None:
    got_status, headers, body = answer
    assert got_status == status, (got_status, body)
    assert headers.get("content-type", "").startswith("application/json"), headers
    fault = json.loads(body)
    assert set(fault) == {"fault"}, fault
    assert fault["fault"]["detail"] == {"errorcode": errorcode}, fault
    assert set(fault["fault"]) == {"faultstring", "detail"}, fault
    text = body.decode("utf-8", "replace")
    for secret in ("127.0.0.1", "localhost", str(target_port), HIDDEN_PATH, "HTTP:", "onnection", "timed out", "GET "):
        assert secret not in text, f"the caller sees {secret!r}: {text}"


@pytest.mark.runtime
def test_CP3_T47_a_target_that_is_down_or_silent_gives_a_generic_fault_with_no_details(
    mule_runtime: Any, free_port: Callable[[], int], tmp_path: Path
) -> None:
    """[CP3-T47] A target that refuses or never answers gives the caller a generic fault; details go to the log."""
    from a2m.generator import generate_project
    from a2m.verify.mule import package

    runner = mule_runtime.runner
    name = "runtime-faults"
    target_port, listen_port = free_port(), free_port()
    bundle = parse_bundle(
        _write_timeout_bundle(
            tmp_path / "bundles", name, "/faults", f"http://127.0.0.1:{target_port}/{HIDDEN_PATH}"
        )
    )
    project = tmp_path / name / "mule-app"
    generate_project(bundle, project, shared_flows=())
    props_path = properties_file(project)
    props = read_properties(props_path)
    props[listener_port_key(project)] = str(listen_port)
    write_properties(props_path, props)
    jar = package(project, timeout=BUILD_TIMEOUT)
    silent: _SilentServer | None = None
    try:
        runner.deploy(jar, app_name=name, timeout=DEPLOY_TIMEOUT)

        # Nothing listens on the target port: connection refused.
        _assert_generic_fault(
            call(listen_port, "GET", "/faults/orders/1"),
            503,
            "messaging.adaptors.http.flow.ServiceUnavailable",
            target_port,
        )

        # The target accepts and never answers: the request times out.
        silent = _SilentServer(target_port)
        started = time.monotonic()
        answer = call(listen_port, "GET", "/faults/orders/2")
        assert time.monotonic() - started < 25
        _assert_generic_fault(answer, 504, "messaging.adaptors.http.flow.GatewayTimeout", target_port)

        # The details the caller did not get are in the runtime's logs.
        base = Path(mule_runtime.mule_base)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and str(target_port) not in _log_text(base):
            time.sleep(1)
        assert str(target_port) in _log_text(base), "the target error was not logged"
    finally:
        if silent is not None:
            silent.close()
        with contextlib.suppress(Exception):
            runner.undeploy(name, timeout=60)


# ---------------------------------------------------------------------------
# CP3 behaviour gate: deploy returns only once the app's HTTP listener serves (CP3-T48).
#
# Right after an app's anchor and "Started app" line, Mule's HTTP service can still answer
# every request with its own container-level 503 ("Server not available ..."), so the first
# request after deploy returned failed now and then. Unmarked: a fake runtime writes the start
# signals and a fake HTTP server on the app's listener port plays Mule's HTTP service.

import zipfile  # noqa: E402

from .conftest import free_port  # noqa: E402

CONTAINER_503 = b"Server not available to handle this request, either not initialized yet or it has been disposed."
APP_503 = (
    b'{"fault":{"faultstring":"The Service is temporarily unavailable",'
    b'"detail":{"errorcode":"messaging.adaptors.http.flow.ServiceUnavailable"}}}'
)
STARTING_RUNTIME = """\
echo 'INFO Mule is up and kicking (every 5000ms)' >> logs/mule.log
while [ ! -f apps/probe-app.jar ]; do sleep 0.1; done
echo "INFO  * Started app 'probe-app'  *" >> logs/mule.log
touch apps/probe-app-anchor.txt
exec sleep 300
"""


class _ListenerStandIn:
    """Answers the first ``container_answers`` requests with Mule's container 503, later ones with an app's 503."""

    def __init__(self, port: int, container_answers: int) -> None:
        self.container_answers = container_answers
        self.answers: list[tuple[float, bytes]] = []
        self._sock = socket.create_server(("127.0.0.1", port))
        self._sock.settimeout(0.2)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                continue
            with conn:
                conn.settimeout(5)
                data = b""
                with contextlib.suppress(OSError):
                    while b"\r\n\r\n" not in data:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                body, kind = (
                    (CONTAINER_503, "text/plain")
                    if len(self.answers) < self.container_answers
                    else (APP_503, "application/json")
                )
                self.answers.append((time.monotonic(), body))
                head = f"HTTP/1.1 503 Service Unavailable\r\nContent-Type: {kind}\r\nContent-Length: {len(body)}\r\n\r\n"
                with contextlib.suppress(OSError):
                    conn.sendall(head.encode("ascii") + body)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(5)
        self._sock.close()


def _app_jar(path: Path, port: int) -> Path:
    """A stand-in app jar whose config.properties sets the HTTP listener to 0.0.0.0:``port``."""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("config.properties", f"http.listener.host=0.0.0.0\nhttp.listener.port={port}\n")
        archive.writestr("mule-artifact.json", "{}")
    return path


@pytest.mark.parametrize("container_answers", [0, 3])
def test_CP3_T48_deploy_waits_through_container_503s_and_accepts_an_app_503(
    tmp_path: Path, container_answers: int
) -> None:
    """[CP3-T48] deploy returns only after the listener port stops sending Mule's container 503; an app 503 is ready."""
    from a2m.verify.mule import MuleRunner

    port = free_port()
    listener = _ListenerStandIn(port, container_answers)
    runner = MuleRunner(mule_home=_fake_mule_home(tmp_path, STARTING_RUNTIME), mule_base=tmp_path / "mule-base")
    try:
        runner.start(timeout=30)
        runner.deploy(_app_jar(tmp_path / "probe-app.jar", port), app_name="probe-app", timeout=30)
        returned = time.monotonic()
        bodies = [body for _, body in listener.answers]
        assert bodies[:container_answers] == [CONTAINER_503] * container_answers
        assert APP_503 in bodies, "deploy returned before the listener answered as the app"
        first_app_answer = next(at for at, body in listener.answers if body == APP_503)
        assert first_app_answer <= returned
    finally:
        listener.close()
        stop_and_reap(runner)


def test_CP3_T48_deploy_fails_when_the_listener_never_leaves_the_container_503(tmp_path: Path) -> None:
    """[CP3-T48] A listener port that only ever sends Mule's container 503 makes deploy raise DeployError in time."""
    from a2m.verify.mule import DeployError, MuleRunner

    port = free_port()
    listener = _ListenerStandIn(port, container_answers=10**6)
    runner = MuleRunner(mule_home=_fake_mule_home(tmp_path, STARTING_RUNTIME), mule_base=tmp_path / "mule-base")
    try:
        runner.start(timeout=30)
        started = time.monotonic()
        with pytest.raises(DeployError) as failure:
            runner.deploy(_app_jar(tmp_path / "probe-app.jar", port), app_name="probe-app", timeout=4)
        assert time.monotonic() - started < 15
        assert str(port) in str(failure.value)
        assert len(listener.answers) >= 2, "deploy did not keep probing the listener"
    finally:
        listener.close()
        stop_and_reap(runner)


# ---------------------------------------------------------------------------
# CP3 adversarial round 6: a wildcard base path forwards only what follows the matched base path (CP3-T51).
#
# The base path is '/v1/*/search'. The tenant segment differs in length per request, so a suffix
# computed from the pattern's own length would forward part of the matched base path.


@pytest.mark.runtime
def test_CP3_T51_a_wildcard_base_path_forwards_only_the_suffix_below_it(
    mule_runtime: Any, backend: Backend, free_port: Callable[[], int], tmp_path: Path
) -> None:
    """[CP3-T51] /v1/acme/search/items on base path /v1/*/search reaches the backend as <target path>/items."""
    runner = mule_runtime.runner
    name = "runtime-wildcard"
    port = free_port()
    _, jar = build_app(tmp_path, name, "/v1/*/search", backend.port, port)
    cases = (
        ("/v1/acme/search/items", "/backend/items"),
        ("/v1/a/search/items", "/backend/items"),
        ("/v1/acmecorporation/search/items/42?color=red", "/backend/items/42"),
        ("/v1/acme/search", "/backend"),
        ("/v1/acme/search/", "/backend/"),
        ("/v1/acme/search/a%20b", "/backend/a%20b"),
    )
    try:
        runner.deploy(jar, app_name=name, timeout=DEPLOY_TIMEOUT)
        for request_path, forwarded in cases:
            backend.clear()
            status, _, body = call(port, "GET", request_path)
            assert (status, body) == (200, f"echo-7f3a:{forwarded}".encode()), (request_path, status, body)
            (seen,) = backend.seen()
            assert seen.path == forwarded, (request_path, seen.path)
            if "?" in request_path:
                assert seen.query == "color=red", seen.query
    finally:
        with contextlib.suppress(Exception):
            runner.undeploy(name, timeout=60)


# ---------------------------------------------------------------------------
# CP3 adversarial round 7: a backend redirect reaches the caller unchanged (CP3-T53).
#
# The backend answers every /backend request with a 302 whose Location points at another path on
# the same server. Apigee returns that 302, its Location, headers and body to the caller; a proxy
# that followed it would send a second request and return the destination's 200.

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

REDIRECT_BODY = b"moved-5d21"


class _RedirectingBackend:
    """Answers /backend/... with 302 + Location to /destination; records every request path."""

    def __init__(self) -> None:
        self.paths: list[str] = []
        self.lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                with owner.lock:
                    owner.paths.append(self.path)
                if self.path.startswith("/backend"):
                    status, payload = 302, REDIRECT_BODY
                    extra = {"Location": owner.location, "X-Backend": "redirect"}
                else:
                    status, payload, extra = 200, b"followed", {}
                self.send_response(status)
                for key, value in extra.items():
                    self.send_header(key, value)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = _handle

            def log_message(self, format: str, *args: Any) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = int(self.server.server_address[1])
        self.location = f"http://127.0.0.1:{self.port}/destination?from=backend"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def seen(self) -> list[str]:
        with self.lock:
            return list(self.paths)

    def clear(self) -> None:
        with self.lock:
            self.paths.clear()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.mark.runtime
def test_CP3_T53_a_backend_redirect_reaches_the_caller_unchanged(
    mule_runtime: Any, free_port: Callable[[], int], tmp_path: Path
) -> None:
    """[CP3-T53] A backend 302 reaches the caller with its Location, headers and body; the backend is called once."""
    runner = mule_runtime.runner
    name = "runtime-redirect"
    port = free_port()
    redirecting = _RedirectingBackend()
    try:
        _, jar = build_app(tmp_path, name, "/moves", redirecting.port, port)
        runner.deploy(jar, app_name=name, timeout=DEPLOY_TIMEOUT)
        for method, body in (("GET", None), ("POST", b'{"a":1}')):
            redirecting.clear()
            status, headers, got = call(port, method, "/moves/orders/1", body=body)
            assert status == 302, (method, status, got)
            assert headers.get("location") == redirecting.location, (method, headers)
            assert headers.get("x-backend") == "redirect", (method, headers)
            assert got == REDIRECT_BODY, (method, got)
            assert redirecting.seen() == ["/backend/orders/1"], (method, redirecting.seen())
    finally:
        with contextlib.suppress(Exception):
            runner.undeploy(name, timeout=60)
        redirecting.close()
