"""CP3: a generated Mule project really builds, deploys and forwards on the local Mule runtime.

Two kinds of test live here:

* CP3-T25 to CP3-T28 are unmarked and run in the default suite. They prove the
  runtime test plumbing itself (default exclusion, skip with a reason,
  A2M_REQUIRE_RUNTIME=1 turning skips into failures) by running pytest in a
  subprocess with a controlled environment. No Java, Maven or Mule needed.
* CP3-T29 to CP3-T35 are marked ``runtime``: excluded from a plain
  ``pytest -q`` and run by the checkpoint command. They generate a fixture
  proxy whose target is a loopback backend started here, build it with real
  Maven and deploy it with a2m's runner (a2m.verify.mule, contract in
  tests/runtime/conftest.py) into a private MULE_BASE under pytest's tmp folder.

Entry points used: a2m.parser.read_bundle, a2m.generator.generate_project and
a2m.verify.mule (MuleRunner, package, DeployError).
"""

from __future__ import annotations

import contextlib
import http.client
import os
import re
import stat
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

REPO = Path(__file__).resolve().parents[2]
THIS_FILE = "tests/runtime/test_cp3_deploy.py"
VENV_PYTHON = REPO / ".venv" / "bin" / "python"
HTTP_NS = "http://www.mulesoft.org/schema/mule/http"
PURE_PLACEHOLDER = re.compile(r"\$\{([^}]+)\}")
DEPLOY_TIMEOUT = 180.0
BUILD_TIMEOUT = 900.0
START_TIMEOUT = 240.0
STRIPPED_ENV = (
    "MULE_HOME",
    "A2M_MULE_HOME",
    "A2M_REQUIRE_RUNTIME",
    "PYTEST_ADDOPTS",
    "JAVA_HOME",
    "A2M_UPDATE_GOLDEN",
    "FORCE_COLOR",
    "PY_COLORS",
)
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


# ---------------------------------------------------------------- subprocess pytest (plumbing cases)


@dataclass
class PytestRun:
    code: int
    out: str
    seconds: float
    basetemp: Path


def run_pytest(
    tmp_path: Path,
    args: list[str],
    *,
    require: str | None = None,
    fake_tools: bool = False,
    mule_home: Path | None = None,
    label: str = "run",
) -> PytestRun:
    """Run the repo's pytest in a subprocess with PATH holding only a tmp folder and no Mule settings."""
    bin_dir = tmp_path / f"bin-{label}"
    bin_dir.mkdir()
    if fake_tools:
        for tool in ("java", "mvn"):
            script = bin_dir / tool
            script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    env = {key: value for key, value in os.environ.items() if key not in STRIPPED_ENV}
    env["PATH"] = str(bin_dir)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if require is not None:
        env["A2M_REQUIRE_RUNTIME"] = require
    if mule_home is not None:
        env["MULE_HOME"] = str(mule_home)
    basetemp = tmp_path / f"inner-{label}"
    cmd = [str(VENV_PYTHON), "-m", "pytest", "-p", "no:cacheprovider", "--color=no", f"--basetemp={basetemp}", *args]
    started = time.monotonic()
    proc = subprocess.run(cmd, cwd=REPO, env=env, capture_output=True, text=True, timeout=120, check=False)
    return PytestRun(proc.returncode, proc.stdout + proc.stderr, time.monotonic() - started, basetemp)


def counts(out: str) -> dict[str, int]:
    """The outcome counts from pytest's final summary line(s)."""
    found: dict[str, int] = {}
    for number, word in re.findall(r"\b(\d+) (passed|failed|skipped|errors?|deselected)\b", ANSI_ESCAPE.sub("", out)):
        found[word.rstrip("s") if word.startswith("error") else word] = int(number)
    return found


def skip_lines(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith("SKIPPED")]


def processes_mentioning(text: str) -> list[int]:
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if text in cmdline:
            found.append(int(entry.name))
    return found


RUNTIME_IDS = [f"CP3_T{n}" for n in range(29, 36)]
PLUMBING_IDS = [f"CP3_T{n}" for n in range(25, 29)]


def collected(out: str, case: str) -> bool:
    return any(f"::test_{case}_" in line for line in out.splitlines())


def test_CP3_T25_a_plain_test_run_leaves_out_the_runtime_tests(tmp_path: Path) -> None:
    """[CP3-T25] A plain test run leaves out the slow Mule runtime tests."""
    plain = run_pytest(tmp_path, ["-q", "--collect-only", THIS_FILE], label="plain")
    everything = run_pytest(
        tmp_path, ["-q", "--collect-only", "-m", "runtime or not runtime", THIS_FILE], label="all"
    )

    assert plain.code == 0, plain.out
    for case in RUNTIME_IDS:
        assert not collected(plain.out, case), f"{case} collected without -m:\n{plain.out}"
    for case in PLUMBING_IDS:
        assert collected(plain.out, case), f"{case} not collected:\n{plain.out}"
    assert counts(plain.out).get("deselected") == len(RUNTIME_IDS), plain.out

    assert everything.code == 0, everything.out
    for case in PLUMBING_IDS + RUNTIME_IDS:
        assert collected(everything.out, case), f"{case} not collected with -m:\n{everything.out}"
    for run in (plain, everything):
        assert "PytestUnknownMarkWarning" not in run.out


def test_CP3_T26_without_tools_the_runtime_tests_are_skipped_with_a_reason(tmp_path: Path) -> None:
    """[CP3-T26] Without Java, Maven or Mule the runtime tests are skipped with a clear reason."""
    run = run_pytest(tmp_path, [THIS_FILE, "-m", "runtime", "-k", "CP3_T30", "-rs"])

    assert run.code == 0, run.out
    result = counts(run.out)
    assert result.get("skipped") == 1, run.out
    assert "failed" not in result and "error" not in result and "passed" not in result, run.out
    lines = skip_lines(run.out)
    assert len(lines) == 1, run.out
    for name in ("java", "mvn", "MULE_HOME"):
        assert name in lines[0], f"skip reason does not name {name}: {lines[0]}"
    assert run.seconds < 30, f"took {run.seconds:.1f}s"
    assert processes_mentioning(str(run.basetemp)) == []


def test_CP3_T27_require_runtime_turns_a_missing_tool_into_a_failure(tmp_path: Path) -> None:
    """[CP3-T27] A2M_REQUIRE_RUNTIME=1 turns a missing tool into a failure."""
    run = run_pytest(tmp_path, [THIS_FILE, "-m", "runtime", "-k", "CP3_T30", "-rs"], require="1")

    assert run.code != 0, run.out
    result = counts(run.out)
    assert result.get("failed", 0) + result.get("error", 0) == 1, run.out
    assert "skipped" not in result and skip_lines(run.out) == [], run.out
    for name in ("java", "mvn", "MULE_HOME", "A2M_REQUIRE_RUNTIME"):
        assert name in run.out, f"output does not mention {name}:\n{run.out}"


def test_CP3_T28_only_really_absent_items_are_named(tmp_path: Path) -> None:
    """[CP3-T28] Only the tools that are really missing are named, and a MULE_HOME that does not exist counts."""
    absent_home = tmp_path / "no-such-mule"
    for label, require in (("unset", None), ("zero", "0")):
        run = run_pytest(
            tmp_path,
            [THIS_FILE, "-m", "runtime", "-k", "CP3_T30", "-rs"],
            require=require,
            fake_tools=True,
            mule_home=absent_home,
            label=label,
        )
        assert run.code == 0, run.out
        assert counts(run.out).get("skipped") == 1, run.out
        lines = skip_lines(run.out)
        assert len(lines) == 1, run.out
        reason = lines[0].replace(str(tmp_path), "<tmp>")
        assert "MULE_HOME" in reason, reason
        assert not re.search(r"\bjava\b", reason, re.IGNORECASE), reason
        assert not re.search(r"\bmvn\b", reason, re.IGNORECASE), reason


# ---------------------------------------------------------------- local recording backend


@dataclass(frozen=True)
class Recorded:
    method: str
    path: str
    query: str
    headers: dict[str, str]
    body: bytes


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


def scripted_reply(method: str, path: str) -> tuple[int, dict[str, str], bytes]:
    if method == "POST" and path == "/backend/orders":
        return 201, {"X-Backend": "local", "Content-Type": "application/json"}, b'{"created":"A1"}'
    if method == "GET" and path == "/backend/missing":
        return 404, {"X-Backend": "local", "Content-Type": "text/plain"}, b"not here"
    return 200, {"X-Backend": "local", "Content-Type": "text/plain"}, f"echo-7f3a:{path}".encode()


def make_handler(backend: Backend) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _body(self) -> bytes:
            if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
                data = b""
                while True:
                    size = int(self.rfile.readline().split(b";", 1)[0].strip() or b"0", 16)
                    if size == 0:
                        while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                            pass
                        return data
                    data += self.rfile.read(size)
                    self.rfile.readline()
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length else b""

        def _handle(self) -> None:
            split = urlsplit(self.path)
            body = self._body()
            with backend.lock:
                backend.records.append(
                    Recorded(
                        self.command,
                        split.path,
                        split.query,
                        {k.lower(): v for k, v in self.headers.items()},
                        body,
                    )
                )
            status, headers, payload = scripted_reply(self.command, split.path)
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

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


def call(
    port: int, method: str, path: str, body: bytes | None = None, headers: dict[str, str] | None = None
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
    finally:
        conn.close()


# ---------------------------------------------------------------- generating and building runtime fixtures


def write_runtime_bundle(parent: Path, name: str, base_path: str, target_url: str) -> Path:
    """One ProxyEndpoint, one TargetEndpoint 'default', one unconditional RouteRule; no policies, no conditions."""
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
        f"<HTTPTargetConnection><URL>{target_url}</URL></HTTPTargetConnection></TargetEndpoint>\n",
    }
    root = parent / name
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def read_properties(path: Path) -> dict[str, str]:
    """A java.util.Properties reader for the generated file (escapes, continuations, last definition wins)."""
    props: dict[str, str] = {}
    logical, pending = [], ""
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


def listener_port_key(project: Path) -> str:
    """The property the http:listener-config's port refers to, found by reading the flow XML."""
    keys = set()
    for path in sorted((project / "src" / "main" / "mule").glob("*.xml")):
        for connection in ET.parse(path).getroot().iter(f"{{{HTTP_NS}}}listener-connection"):
            match = PURE_PLACEHOLDER.fullmatch(connection.get("port") or "")
            assert match is not None, f"{path.name}: listener port {connection.get('port')!r} is not a property"
            keys.add(match.group(1))
    assert len(keys) == 1, keys
    return keys.pop()


def parse_bundle(path: Path) -> Any:
    from a2m.parser import read_bundle

    return read_bundle(path)


def build_app(
    root: Path, name: str, base_path: str, backend_port: int, listen_port: int, *, drop_port: bool = False
) -> tuple[Path, Path]:
    """Generate ``name`` with a2m, point its listener at ``listen_port`` (or drop that property), build it."""
    from a2m.generator import generate_project
    from a2m.verify.mule import package

    bundle = parse_bundle(
        write_runtime_bundle(root / "bundles", name, base_path, f"http://127.0.0.1:{backend_port}/backend")
    )
    project = root / name / "mule-app"
    generate_project(bundle, project, shared_flows=())
    key = listener_port_key(project)
    props_path = properties_file(project)
    props = read_properties(props_path)
    assert key in props, f"listener port property {key!r} missing from the generated file"
    if drop_port:
        del props[key]
    else:
        props[key] = str(listen_port)
    write_properties(props_path, props)
    jar = package(project, timeout=BUILD_TIMEOUT)
    return project, Path(jar)


def snapshot(mule_home: Path) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for part in ("apps", "logs"):
        folder = mule_home / part
        found[part] = sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*")) if folder.is_dir() else []
    return found


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def log_lines(mule_base: Path) -> list[str]:
    log = mule_base / "logs" / "mule.log"
    return log.read_text(encoding="utf-8", errors="replace").splitlines() if log.is_file() else []


@dataclass
class DeployedApp:
    name: str
    port: int
    project: Path
    jar: Path
    deploy_seconds: float


@pytest.fixture(scope="module")
def echo_app(
    mule_runtime: Any, backend: Backend, free_port: Callable[[], int], tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeployedApp]:
    """runtime-echo generated, built and deployed once for the module, independent of CP3-T29."""
    root = tmp_path_factory.mktemp("runtime-echo")
    port = free_port()
    project, jar = build_app(root, "runtime-echo", "/rt", backend.port, port)
    started = time.monotonic()
    mule_runtime.runner.deploy(jar, app_name="runtime-echo", timeout=DEPLOY_TIMEOUT)
    seconds = time.monotonic() - started
    try:
        yield DeployedApp("runtime-echo", port, project, jar, seconds)
    finally:
        with contextlib.suppress(Exception):
            mule_runtime.runner.undeploy("runtime-echo", timeout=60)


# ---------------------------------------------------------------- runtime cases


@pytest.mark.runtime
def test_CP3_T29_a_generated_project_builds_with_maven(runtime_tools: Any, tmp_path: Path) -> None:
    """[CP3-T29] A generated project builds with Maven."""
    project, jar = build_app(tmp_path, "runtime-echo", "/rt", 9, 8081)

    jars = sorted((project / "target").glob("*-mule-application.jar"))
    assert len(jars) == 1, jars
    assert jar.resolve() == jars[0].resolve()
    assert jars[0].name.startswith("runtime-echo")
    with zipfile.ZipFile(jars[0]) as archive:
        basenames = {name.rsplit("/", 1)[-1] for name in archive.namelist()}
    expected = {"mule-artifact.json", properties_file(project).name}
    expected |= {p.name for p in (project / "src" / "main" / "mule").glob("*.xml")}
    assert expected <= basenames, sorted(expected - basenames)


@pytest.mark.runtime
def test_CP3_T30_the_built_app_starts_on_a_private_runtime(mule_runtime: Any, echo_app: DeployedApp) -> None:
    """[CP3-T30] The built app starts on a private Mule runtime without touching the user's install."""
    base = Path(mule_runtime.mule_base)

    assert echo_app.deploy_seconds <= DEPLOY_TIMEOUT
    lines = log_lines(base)
    anchor = base / "apps" / "runtime-echo-anchor.txt"
    assert anchor.is_file() or any("Started app" in line and "runtime-echo" in line for line in lines)
    assert not any("Failed to deploy artifact" in line and "runtime-echo" in line for line in lines)
    assert base.resolve().is_relative_to(Path(mule_runtime.tmp_root).resolve())
    assert snapshot(Path(mule_runtime.mule_home)) == mule_runtime.home_before


@pytest.mark.runtime
def test_CP3_T31_a_request_reaches_the_local_backend_and_returns_its_answer(
    echo_app: DeployedApp, backend: Backend
) -> None:
    """[CP3-T31] A request to the proxy's path reaches the local backend and returns its answer."""
    backend.clear()

    status, headers, body = call(echo_app.port, "GET", "/rt/items/42?color=red", headers={"X-Trace": "abc"})
    assert (status, body) == (200, b"echo-7f3a:/backend/items/42")
    assert headers.get("x-backend") == "local"

    status, _, body = call(
        echo_app.port, "POST", "/rt/orders", body=b'{"sku":"A1"}', headers={"Content-Type": "application/json"}
    )
    assert (status, body) == (201, b'{"created":"A1"}')

    seen = backend.seen()
    assert len(seen) == 2, seen
    first, second = seen
    assert (first.method, first.path, first.query) == ("GET", "/backend/items/42", "color=red")
    assert first.headers.get("x-trace") == "abc"
    assert (second.method, second.path, second.body) == ("POST", "/backend/orders", b'{"sku":"A1"}')


@pytest.mark.runtime
def test_CP3_T32_a_backend_error_answer_passes_back_unchanged(echo_app: DeployedApp, backend: Backend) -> None:
    """[CP3-T32] An error answer from the backend is passed back to the caller unchanged."""
    backend.clear()

    status, _, body = call(echo_app.port, "GET", "/rt/missing")

    assert (status, body) == (404, b"not here")
    assert [(r.method, r.path) for r in backend.seen()] == [("GET", "/backend/missing")]


@pytest.mark.runtime
def test_CP3_T33_undeploying_stops_the_app_while_the_runtime_keeps_running(
    mule_runtime: Any, backend: Backend, free_port: Callable[[], int], tmp_path: Path
) -> None:
    """[CP3-T33] Undeploying an app stops it answering while the runtime keeps running."""
    runner = mule_runtime.runner
    port = free_port()
    _, jar = build_app(tmp_path, "runtime-gone", "/gone", backend.port, port)
    runner.deploy(jar, app_name="runtime-gone", timeout=DEPLOY_TIMEOUT)
    try:
        assert call(port, "GET", "/gone/x")[::2] == (200, b"echo-7f3a:/backend/x")

        runner.undeploy("runtime-gone", timeout=60)

        refused = False
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                call(port, "GET", "/gone/x")
            except ConnectionRefusedError:
                refused = True
                break
            except OSError:
                pass
            time.sleep(1)
        assert refused, "runtime-gone still accepts connections 60s after undeploy"
        assert not (Path(mule_runtime.mule_base) / "apps" / "runtime-gone-anchor.txt").exists()
        pids = list(runner.pids)
        assert pids and all(pid_alive(int(pid)) for pid in pids)
    finally:
        with contextlib.suppress(Exception):
            runner.undeploy("runtime-gone", timeout=60)


@pytest.mark.runtime
def test_CP3_T34_stopping_the_runtime_leaves_no_mule_process(runtime_tools: Any, tmp_path: Path) -> None:
    """[CP3-T34] Stopping the runtime leaves no Mule process behind."""
    from a2m.verify.mule import MuleRunner

    home = Path(runtime_tools.mule_home)
    before = snapshot(home)
    base = tmp_path / "mule-base-2"
    base.mkdir()
    runner = MuleRunner(mule_home=home, mule_base=base)
    try:
        runner.start(timeout=START_TIMEOUT)
        pids = [int(pid) for pid in runner.pids]
        assert pids, "the runner recorded no PID"

        runner.stop(timeout=60)
        runner.stop(timeout=60)

        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and any(pid_alive(pid) for pid in pids):
            time.sleep(1)
        for pid in pids:
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
        assert processes_mentioning(str(base)) == []
        assert snapshot(home) == before
    finally:
        for pid in list(getattr(runner, "pids", ())):
            if pid_alive(int(pid)):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(int(pid), 9)


@pytest.mark.runtime
def test_CP3_T35_an_app_that_fails_to_deploy_is_reported_with_the_log(
    mule_runtime: Any, backend: Backend, free_port: Callable[[], int], tmp_path: Path
) -> None:
    """[CP3-T35] An app that fails to deploy is reported as a failure with the log, not as started."""
    from a2m.verify.mule import DeployError

    runner = mule_runtime.runner
    _, jar = build_app(tmp_path, "runtime-broken", "/broken", backend.port, free_port(), drop_port=True)
    assert jar.is_file()
    try:
        started = time.monotonic()
        with pytest.raises(DeployError) as failure:
            runner.deploy(jar, app_name="runtime-broken", timeout=DEPLOY_TIMEOUT)
        seconds = time.monotonic() - started
        assert seconds < 120, f"deploy failure took {seconds:.0f}s to report, like a generic timeout"
        excerpt = str(failure.value.log_excerpt)
        assert "Failed to deploy artifact" in excerpt, excerpt
        assert "runtime-broken" in excerpt, excerpt
        pids = list(runner.pids)
        assert pids and all(pid_alive(int(pid)) for pid in pids)
    finally:
        with contextlib.suppress(Exception):
            runner.undeploy("runtime-broken", timeout=60)
