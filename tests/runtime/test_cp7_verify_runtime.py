"""CP7 runtime: the verification harness on the real local Mule runtime (Mule Kernel CE 4.9.0).

Every test here is marked ``runtime``: excluded from a plain ``pytest -q``,
skipped with a reason when java, mvn or MULE_HOME is missing, and failing
instead under A2M_REQUIRE_RUNTIME=1 (see tests/runtime/conftest.py).

These tests run the REAL runner (a2m.verify.runner.MuleAppRunner, which wraps
CP3's a2m/verify/mule.py: mvn package in the generated project, hot deploy into
a private MULE_BASE, the deploy signal) against an a2m mock backend the test
starts and reads itself, so backend hit counts are checked independently of the
harness's own report. The contract is the one documented at the top of
tests/test_cp7_verify.py (verify_proxy, AppUnderTest, the Runner and AppHandle
protocols, VerificationResult, the golden recording format).

The known-good proxy 'keys-v1' (base path /keys, target
http://backend.example/keys, PreFlow VerifyAPIKey on header x-api-key then
AssignMessage setting X-Client: a2m) is written under pytest's tmp folder and
generated once with a2m; the test allows the key 'good-key-123' in the
generated properties file (empty by default), and the battery reads it from
there. Its battery is exactly valid-key, missing-key and bad-key.

T27 and T28 start their own Mule runtimes and run first, before the
module-scoped runtime session of T23 to T26 is started, so at most one Mule
runs at a time.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import socket
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

GOOD_KEY = "good-key-123"
BAD_KEY = "nope"
BACKEND_BODY = b'{"id":7}'
BACKEND_HEADERS = {"Content-Type": "application/json", "X-Backend": "mock"}
XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
VERSION_MISMATCH = "connector version not compatible with Mule runtime 4.9.0"
STOP_WAIT = 60.0


# ---------------------------------------------------------------- the keys-v1 fixture proxy

KEYS_FILES = {
    "keys-v1.xml": (
        '<APIProxy revision="1" name="keys-v1">\n    <DisplayName>keys-v1</DisplayName>\n'
        "    <Policies><Policy>verify-key</Policy><Policy>add-header</Policy></Policies>\n"
        "    <ProxyEndpoints><ProxyEndpoint>default</ProxyEndpoint></ProxyEndpoints>\n"
        "    <TargetEndpoints><TargetEndpoint>default</TargetEndpoint></TargetEndpoints>\n</APIProxy>\n"
    ),
    "policies/verify-key.xml": (
        '<VerifyAPIKey name="verify-key">\n    <APIKey ref="request.header.x-api-key"/>\n</VerifyAPIKey>\n'
    ),
    "policies/add-header.xml": (
        '<AssignMessage name="add-header">\n    <Set>\n        <Headers>\n'
        '            <Header name="X-Client">a2m</Header>\n        </Headers>\n    </Set>\n'
        "    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n"
        '    <AssignTo createNew="false" transport="http" type="request"/>\n</AssignMessage>\n'
    ),
    "proxies/default.xml": (
        '<ProxyEndpoint name="default">\n'
        '    <PreFlow name="PreFlow"><Request><Step><Name>verify-key</Name></Step>'
        "<Step><Name>add-header</Name></Step></Request><Response/></PreFlow>\n"
        '    <Flows/>\n    <PostFlow name="PostFlow"><Request/><Response/></PostFlow>\n'
        "    <HTTPProxyConnection><BasePath>/keys</BasePath><VirtualHost>default</VirtualHost></HTTPProxyConnection>\n"
        '    <RouteRule name="default"><TargetEndpoint>default</TargetEndpoint></RouteRule>\n</ProxyEndpoint>\n'
    ),
    "targets/default.xml": (
        '<TargetEndpoint name="default">\n    <PreFlow name="PreFlow"><Request/><Response/></PreFlow><Flows/>\n'
        '    <PostFlow name="PostFlow"><Request/><Response/></PostFlow>\n'
        "    <HTTPTargetConnection><URL>http://backend.example/keys</URL></HTTPTargetConnection>\n</TargetEndpoint>\n"
    ),
}


def write_keys(parent: Path) -> Path:
    root = parent / "keys-v1"
    for rel, text in KEYS_FILES.items():
        path = root / "apiproxy" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(XML_HEAD + text, encoding="utf-8")
    return root


def fill_key(app_dir: Path, key: str = GOOD_KEY) -> None:
    """Allow ``key`` in every VerifyAPIKey allowed-keys property of the generated properties file."""
    changed = 0
    for path in sorted((app_dir / "src" / "main" / "resources").rglob("*.properties")):
        text = path.read_text(encoding="utf-8")
        new, count = re.subn(
            r"(?m)^(verifyapikey\.[^=\n]*\.allowedKeys)=[ \t]*$", lambda m: f"{m.group(1)}={key}", text
        )
        changed += count
        path.write_text(new, encoding="utf-8")
    assert changed == 1, f"expected one empty allowed-keys property in {app_dir}"


@dataclass
class KeysProject:
    bundle: Any
    app_dir: Path
    work: Path


@pytest.fixture(scope="module")
def keys_project(runtime_tools: Any, tmp_path_factory: pytest.TempPathFactory) -> KeysProject:
    """keys-v1 written and generated by a2m once for the module, with GOOD_KEY allowed."""
    from a2m.generator import generate_project
    from a2m.parser import read_bundle

    work = tmp_path_factory.mktemp("cp7-keys")
    bundle = read_bundle(write_keys(work / "bundles"))
    app_dir = work / "out" / "keys-v1" / "mule-app"
    result = generate_project(bundle, app_dir)
    assert result.unsupported == (), result.unsupported
    assert result.requires_enterprise is False
    fill_key(app_dir)
    return KeysProject(bundle, app_dir, work)


def copy_app(src: Path, dest: Path) -> Path:
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns("target"))
    return dest


def make_bad_app(src: Path, dest: Path) -> Path:
    """keys-v1's app with every flow file made a plain pass-through: listener on the same property-driven port and
    path /keys/*, forwarding every request to the backend (the VerifyAPIKey and AssignMessage steps removed)."""
    copy_app(src, dest)
    doc_ns = "http://www.mulesoft.org/schema/mule/documentation"
    flows = sorted((dest / "src" / "main" / "mule").glob("*.xml"))
    assert flows, dest
    for path in flows:
        for _, (prefix, uri) in ET.iterparse(path, events=("start-ns",)):
            ET.register_namespace(prefix, uri)
        tree = ET.parse(path)
        removed = 0
        for parent in list(tree.getroot().iter()):
            for child in list(parent):
                if child.get(f"{{{doc_ns}}}name") in ("verify-key", "add-header"):
                    parent.remove(child)
                    removed += 1
        # Drop error handlers for app-defined (A2M:*) error types that nothing in the app raises or maps any more,
        # so the pass-through app still builds; then drop error-handler blocks left empty.
        root = tree.getroot()
        defined = {el.get("type") for el in root.iter() if el.tag.endswith("}raise-error")}
        defined |= {el.get("targetType") for el in root.iter() if el.tag.endswith("}error-mapping")}
        for parent in list(root.iter()):
            for child in list(parent):
                kind = child.get("type") or ""
                if child.tag.split("}")[-1].startswith("on-error-") and kind.startswith("A2M:") and kind not in defined:
                    parent.remove(child)
        for parent in list(root.iter()):
            for child in list(parent):
                if child.tag.endswith("}error-handler") and len(child) == 0:
                    parent.remove(child)
        tree.write(path, encoding="UTF-8", xml_declaration=True)
        text = path.read_text(encoding="utf-8")
        assert "verify-key" not in text and "x-client" not in text.lower(), text
        assert 'path="/keys/*"' in text, text
    assert removed == 2
    return dest


def make_version_mismatch_app(src: Path, dest: Path) -> Path:
    """keys-v1's app with pom.xml's mule-http-connector changed from 1.11.3 to 1.12.1 (builds, fails to deploy)."""
    copy_app(src, dest)
    pom = dest / "pom.xml"
    text = pom.read_text(encoding="utf-8")
    new, count = re.subn(
        r"(<artifactId>mule-http-connector</artifactId>\s*<version>)1\.11\.3(</version>)", r"\g<1>1.12.1\g<2>", text
    )
    assert count == 1, text
    pom.write_text(new, encoding="utf-8")
    return dest


# ---------------------------------------------------------------- golden recordings for keys-v1


def fault(faultstring: str, errorcode: str) -> str:
    return json.dumps({"fault": {"faultstring": faultstring, "detail": {"errorcode": errorcode}}})


def recorded_headers(n: int) -> dict[str, str]:
    """Headers Apigee sent that always differ from the live app's: Date, Server, X-Request-ID."""
    return {"Date": f"Mon, 01 Sep 2026 10:00:0{n} GMT", "Server": "Apigee-Router", "X-Request-ID": f"rec-{n}"}


def write_keys_golden(root: Path, *, wrong: bool = False) -> Path:
    folder = root / "keys-v1"
    folder.mkdir(parents=True)
    status, body = (201, '{"id":8}') if wrong else (200, '{"id":7}')
    exchanges = {
        "01-valid-key.json": {
            "name": "valid-key",
            "calls": [
                {
                    "after_ms": 0,
                    "request": {"method": "GET", "path": "/keys", "headers": {"x-api-key": GOOD_KEY}, "body": ""},
                    "response": {
                        "status": status,
                        "headers": {**recorded_headers(1), "X-Backend": "mock"},
                        "body": body,
                    },
                }
            ],
            "backend_calls": [
                {
                    "method": "GET",
                    "path": "/keys",
                    "headers": {"X-Client": "a2m"},
                    "response": {"status": 200, "headers": dict(BACKEND_HEADERS), "body": BACKEND_BODY.decode()},
                }
            ],
        },
        "02-missing-key.json": {
            "name": "missing-key",
            "calls": [
                {
                    "after_ms": 0,
                    "request": {"method": "GET", "path": "/keys", "headers": {}, "body": ""},
                    "response": {
                        "status": 401,
                        "headers": recorded_headers(2),
                        "body": fault(
                            "Failed to resolve API Key variable request.header.x-api-key",
                            "steps.oauth.v2.FailedToResolveAPIKey",
                        ),
                    },
                }
            ],
            "backend_calls": [],
        },
        "03-bad-key.json": {
            "name": "bad-key",
            "calls": [
                {
                    "after_ms": 0,
                    "request": {"method": "GET", "path": "/keys", "headers": {"x-api-key": BAD_KEY}, "body": ""},
                    "response": {
                        "status": 401,
                        "headers": recorded_headers(3),
                        "body": fault("Invalid ApiKey", "oauth.v2.InvalidApiKey"),
                    },
                }
            ],
            "backend_calls": [],
        },
    }
    for name, exchange in exchanges.items():
        (folder / name).write_text(json.dumps(exchange, indent=2), encoding="utf-8")
    return root


GOLDEN_REQUESTS = [("GET", "/keys", GOOD_KEY), ("GET", "/keys", None), ("GET", "/keys", BAD_KEY)]


# ---------------------------------------------------------------- runner, backend and process helpers


@pytest.fixture(scope="module")
def backend() -> Iterator[Any]:
    """The test's own mock backend on 127.0.0.1, answering 200 {"id":7}."""
    from a2m.verify.mock_backend import MockBackend

    mock = MockBackend(default_status=200, default_headers=dict(BACKEND_HEADERS), default_body=BACKEND_BODY)
    mock.start()
    try:
        yield mock
    finally:
        mock.stop()


def pid_alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


def reap(pids: list[int]) -> None:
    """SIGKILL only the given PIDs (recorded by the runner under test) that are still alive."""
    for pid in pids:
        if pid_alive(pid):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)


def close_runner(runner: Any) -> None:
    pids = [int(p) for p in getattr(runner, "pids", ())]
    try:
        runner.close()
    finally:
        reap([*pids, *(int(p) for p in getattr(runner, "pids", ()))])


def wait_all_dead(pids: list[int], timeout: float = STOP_WAIT) -> list[int]:
    deadline = time.monotonic() + timeout
    alive = [p for p in pids if pid_alive(p)]
    while alive and time.monotonic() < deadline:
        time.sleep(0.5)
        alive = [p for p in alive if pid_alive(p)]
    return alive


def refused(base_url: str) -> bool:
    url = urlsplit(base_url)
    try:
        socket.create_connection((url.hostname or "127.0.0.1", int(url.port or 80)), timeout=3).close()
    except ConnectionRefusedError:
        return True
    return False


def apps_listing(mule_home: Path) -> list[str]:
    folder = mule_home / "apps"
    return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*")) if folder.is_dir() else []


def under_base(mule_base: Path) -> list[int]:
    """Live PIDs whose environment names this test's own private MULE_BASE (never matched by command line)."""
    marker = f"MULE_BASE={Path(mule_base).absolute()}".encode()
    found: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            environ = (entry / "environ").read_bytes()
        except OSError:
            continue
        if marker in environ.split(b"\0") and pid_alive(int(entry.name)):
            found.append(int(entry.name))
    return sorted(found)


def runtime_leftovers(recorded: list[int], groups: list[int], mule_base: Path) -> list[int]:
    """Still-alive processes of the runtime under test: its recorded PIDs, members of its recorded process
    group(s), and processes whose environment names its private MULE_BASE."""
    alive = {p for p in recorded if pid_alive(p)}
    alive.update(group_members(groups))
    alive.update(under_base(mule_base))
    return sorted(alive)


def wait_no_leftovers(recorded: list[int], groups: list[int], mule_base: Path, timeout: float = 30) -> list[int]:
    deadline = time.monotonic() + timeout
    leftover = runtime_leftovers(recorded, groups, mule_base)
    while leftover and time.monotonic() < deadline:
        time.sleep(0.5)
        leftover = runtime_leftovers(recorded, groups, mule_base)
    return leftover


@dataclass
class RecordingHandle:
    inner: Any
    log: list[tuple[str, str, str | None]]

    @property
    def running(self) -> bool:
        return bool(self.inner.running)

    @property
    def base_url(self) -> str | None:
        return self.inner.base_url  # type: ignore[no-any-return]

    def send(self, request: Any) -> Any:
        headers = {str(k).lower(): str(v) for k, v in dict(request.headers or {}).items()}
        self.log.append((request.method, request.path, headers.get("x-api-key")))
        return self.inner.send(request)

    def stop(self) -> None:
        self.inner.stop()


@dataclass
class RecordingRunner:
    """Passes everything to the real runner and records the requests the harness sends and each app's base URL."""

    inner: Any
    sent: list[tuple[str, str, str | None]] = field(default_factory=list)
    base_urls: list[str] = field(default_factory=list)

    def start(self, app: Any, *, backend_url: str) -> RecordingHandle:
        handle = self.inner.start(app, backend_url=backend_url)
        if handle.base_url:
            self.base_urls.append(str(handle.base_url))
        return RecordingHandle(handle, self.sent)


def verify(bundle: Any, app_dir: Path, runner: Any, backend: Any, golden: Path | None = None) -> Any:
    from a2m.verify import verify_proxy

    return verify_proxy(bundle, app_dir, runner=runner, backend=backend, golden=golden)


def describe(result: Any) -> str:
    cases = [(c.name, c.situation, c.passed, c.backend_calls, c.diff) for c in result.cases]
    return (
        f"type={result.type} ran={result.ran} passed={result.passed} failed={result.failed} "
        f"message={result.message!r} cases={cases} log={result.log_excerpt[-3000:]}"
    )


def by_situation(result: Any) -> dict[str, Any]:
    found = {c.situation: c for c in result.cases}
    assert len(found) == len(result.cases), describe(result)
    return found


def by_name(result: Any) -> dict[str, Any]:
    found = {c.name: c for c in result.cases}
    assert len(found) == len(result.cases), describe(result)
    return found


def backend_line(diff: str, expected: int, got: int) -> bool:
    return re.search(rf"(?i)backend[^\n]*\b{expected}\b[^\n]*\b{got}\b", diff) is not None


def app_entries(mule_base: Path, name: str) -> list[str]:
    apps = mule_base / "apps"
    return sorted(p.name for p in apps.iterdir() if p.name.startswith(name)) if apps.is_dir() else []


# ================================================================ CP7-T27 .. T28: own runtimes, run first


@pytest.mark.runtime
def test_CP7_T27_the_runtime_started_for_a_batch_is_always_stopped(
    runtime_tools: Any, keys_project: KeysProject, backend: Any, tmp_path: Path
) -> None:
    """[CP7-T27] The Mule runtime started for a batch is always stopped, even when the batch crashes."""
    from a2m.verify.runner import MuleAppRunner

    from a2m.verify import AppUnderTest

    home_before = apps_listing(runtime_tools.mule_home)

    # (a) keys-v1 verified, the session closed normally
    backend.clear()
    pids_a: list[int] = []
    recorder: RecordingRunner | None = None
    runner_a = MuleAppRunner(mule_home=runtime_tools.mule_home, mule_base=tmp_path / "base-a")
    try:
        with runner_a as runner:
            recorder = RecordingRunner(runner)
            result = verify(keys_project.bundle, keys_project.app_dir, recorder, backend)
            pids_a = [int(p) for p in runner.pids]
            assert result.type == "battery", describe(result)
    finally:
        reap([*pids_a, *(int(p) for p in runner_a.pids)])
    assert pids_a, "the runner recorded no PIDs for the Mule runtime it started"
    assert wait_all_dead(pids_a) == []
    assert recorder is not None and recorder.base_urls, "the real runner's handle exposed no base_url"
    assert all(refused(url) for url in recorder.base_urls), recorder.base_urls

    # (b) a new session deploys keys-v1, then the batch raises before it finishes
    pids_b: list[int] = []
    base_url: str | None = None
    runner_b = MuleAppRunner(mule_home=runtime_tools.mule_home, mule_base=tmp_path / "base-b")
    try:
        with pytest.raises(RuntimeError, match="boom inside the batch"), runner_b as runner:
            handle = runner.start(AppUnderTest(name="keys-v1", app_dir=keys_project.app_dir), backend_url=backend.url)
            base_url = handle.base_url
            pids_b = [int(p) for p in runner.pids]
            assert handle.running is True
            raise RuntimeError("boom inside the batch")
    finally:
        reap([*pids_b, *(int(p) for p in runner_b.pids)])
    assert pids_b
    assert wait_all_dead(pids_b) == []
    assert base_url is not None and refused(base_url), base_url
    assert apps_listing(runtime_tools.mule_home) == home_before


@pytest.mark.runtime
def test_CP7_T28_a2m_migrate_verifies_a_proxy_for_real_by_default(
    runtime_tools: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """[CP7-T28] Running a2m migrate on this machine verifies a proxy for real by default. Cleanup is checked with
    the PIDs and process group the runner recorded and the run's private MULE_BASE, never by matching command
    lines."""
    from a2m import layout
    from a2m.cli import main
    from a2m.verify.mule import MuleRunner

    exports = tmp_path / "in"
    write_keys(exports)
    results = tmp_path / "results"
    mule_base = layout.mule_base_dir(results)
    recorded: list[int] = []
    groups: list[int] = []
    real_write = MuleRunner._write_pid_file

    def record_pids(self: Any) -> None:
        real_write(self)
        pids = [int(p) for p in self.pids]
        recorded.extend(p for p in pids if p not in recorded)
        if pids and pids[0] not in groups:
            groups.append(pids[0])  # the launcher starts its own session: its PID is the group's id

    monkeypatch.setattr(MuleRunner, "_write_pid_file", record_pids)
    leftover: list[int] = []
    try:
        code = main(["migrate", str(exports), "--out", str(results), "--llm", "fake", "--mock-backends"])

        captured = capsys.readouterr()
        leftover = wait_no_leftovers(recorded, groups, mule_base)
    finally:
        reap(runtime_leftovers(recorded, groups, mule_base))
    assert recorded, "the run never started its Mule runtime"
    assert leftover == [], f"Mule processes started by the run are still alive: {leftover}"
    assert code == 0, captured.err
    log = (results / "run.log").read_text(encoding="utf-8")
    lines = [line for line in log.splitlines() if "keys-v1" in line]
    assert any("verification type: battery" in line for line in lines), log
    assert any(re.search(r"\b3\b[^\n]*\bpassed\b", line) for line in lines), log
    assert not any(re.search(r"(?i)skipped|not installed", line) for line in lines), log
    done = [p for p in results.rglob(".done") if p.parent.name == "keys-v1"]
    assert len(done) == 1, sorted(str(p) for p in results.rglob("*"))


# ================================================================ CP7-T23 .. T26: one shared runtime session


@pytest.fixture(scope="module")
def cp7_runner(runtime_tools: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:
    """One real runner (one Mule start) for T23 to T26, under a private MULE_BASE in pytest's tmp folder."""
    from a2m.verify.runner import MuleAppRunner

    runner = MuleAppRunner(mule_home=runtime_tools.mule_home, mule_base=tmp_path_factory.mktemp("cp7-mule-base"))
    try:
        yield runner
    finally:
        close_runner(runner)


@pytest.mark.runtime
def test_CP7_T23_a_known_good_proxy_passes_its_api_key_tests_and_is_battery(
    cp7_runner: Any, keys_project: KeysProject, backend: Any
) -> None:
    """[CP7-T23] On the real Mule runtime, a known-good proxy passes its API key tests and is labelled battery."""
    backend.clear()

    result = verify(keys_project.bundle, keys_project.app_dir, cp7_runner, backend)

    assert result.type == "battery", describe(result)
    assert (result.ran, result.passed, result.failed) == (3, 3, 0), describe(result)
    cases = by_situation(result)
    assert sorted(cases) == ["bad-key", "missing-key", "valid-key"], describe(result)
    assert all(c.passed for c in cases.values()), describe(result)
    assert (cases["valid-key"].backend_calls, cases["missing-key"].backend_calls, cases["bad-key"].backend_calls) == (
        1,
        0,
        0,
    )
    seen = backend.calls()
    assert len(seen) == 1, seen
    assert seen[0].path == "/keys" or seen[0].path.startswith("/keys/"), seen[0].path
    assert seen[0].headers.get("x-client") == "a2m", seen[0].headers
    assert app_entries(Path(cp7_runner.mule_base), "keys-v1") == []
    assert cp7_runner.pids and all(pid_alive(int(p)) for p in cp7_runner.pids)


@pytest.mark.runtime
def test_CP7_T24_a_saved_recording_gives_golden_and_a_wrong_one_a_readable_diff(
    cp7_runner: Any, keys_project: KeysProject, backend: Any, tmp_path: Path
) -> None:
    """[CP7-T24] On the real Mule runtime, a saved recording gives golden, and a wrong recording gives a readable diff."""
    good = write_keys_golden(tmp_path / "golden")
    wrong = write_keys_golden(tmp_path / "golden-wrong", wrong=True)

    backend.clear()
    recorder = RecordingRunner(cp7_runner)
    result = verify(keys_project.bundle, keys_project.app_dir, recorder, backend, golden=good)

    assert result.type == "golden", describe(result)
    assert recorder.sent == GOLDEN_REQUESTS
    assert [(c.name, c.passed) for c in result.cases] == [("valid-key", True), ("missing-key", True), ("bad-key", True)]
    seen = backend.calls()
    assert len(seen) == 1 and seen[0].headers.get("x-client") == "a2m", seen

    backend.clear()
    mismatch = verify(keys_project.bundle, keys_project.app_dir, cp7_runner, backend, golden=wrong)

    assert mismatch.type == "failed", describe(mismatch)
    cases = by_name(mismatch)
    assert (cases["missing-key"].passed, cases["bad-key"].passed) == (True, True), describe(mismatch)
    valid = cases["valid-key"]
    assert valid.passed is False
    assert re.search(r"(?i)status", valid.diff) and "201" in valid.diff and "200" in valid.diff, valid.diff
    assert re.search(r"\bid\b", valid.diff) and "8" in valid.diff and "7" in valid.diff, valid.diff


@pytest.mark.runtime
def test_CP7_T25_a_deliberately_bad_app_is_failed_never_battery(
    cp7_runner: Any, keys_project: KeysProject, backend: Any, tmp_path: Path
) -> None:
    """[CP7-T25] On the real Mule runtime, a deliberately bad app is labelled failed, never battery."""
    bad_app = make_bad_app(keys_project.app_dir, tmp_path / "bad" / "keys-v1" / "mule-app")
    backend.clear()

    result = verify(keys_project.bundle, bad_app, cp7_runner, backend)

    assert result.type == "failed", describe(result)
    assert (result.ran, result.passed, result.failed) == (3, 0, 3), describe(result)
    cases = by_situation(result)
    for situation in ("missing-key", "bad-key"):
        case = cases[situation]
        assert case.passed is False
        assert "401" in case.diff and "200" in case.diff, case.diff
        assert backend_line(case.diff, 0, 1), case.diff
    valid = cases["valid-key"]
    assert valid.passed is False
    assert "x-client" in valid.diff.lower() and "a2m" in valid.diff and "missing" in valid.diff.lower(), valid.diff
    assert len(backend.calls()) == 3


@pytest.mark.runtime
def test_CP7_T26_a_connector_version_mismatch_is_plain_and_the_next_proxy_still_verifies(
    cp7_runner: Any, keys_project: KeysProject, backend: Any, tmp_path: Path
) -> None:
    """[CP7-T26] On the real Mule runtime, a connector version mismatch is reported in plain words and the next
    proxy still verifies."""
    mismatch_app = make_version_mismatch_app(keys_project.app_dir, tmp_path / "vm" / "keys-v1" / "mule-app")
    backend.clear()

    result = verify(keys_project.bundle, mismatch_app, cp7_runner, backend)

    jars = sorted((mismatch_app / "target").glob("*-mule-application.jar"))
    assert len(jars) == 1, "the version-mismatch app must build; only its deploy fails"
    assert result.type == "failed", describe(result)
    assert VERSION_MISMATCH in result.message, describe(result)
    assert "Failed to deploy artifact" in result.log_excerpt, result.log_excerpt
    assert (result.ran, result.passed) == (0, 0)
    assert backend.calls() == []

    after = verify(keys_project.bundle, keys_project.app_dir, cp7_runner, backend)

    assert after.type == "battery", describe(after)
    assert (after.ran, after.passed, after.failed) == (3, 3, 0)


# ================================================================ CP7 adversarial round 1 (CP7-X15 .. X17)
# New blocks only; every line above is locked. X15 and X16 use the shared session (X16 ends it, so at most
# one Mule runs at a time), X17 starts its own a2m run.

import subprocess  # noqa: E402
import sys  # noqa: E402

REPO = Path(__file__).resolve().parents[2]


@pytest.mark.runtime
def test_CP7_X15_golden_on_an_unedited_project_with_verify_api_key_is_golden(
    cp7_runner: Any, runtime_tools: Any, backend: Any, tmp_path: Path
) -> None:
    """[CP7-X15] A golden run of a VerifyAPIKey proxy needs no hand-edited keys: the recorded valid key is allowed
    in the deployed copy only, and the run is golden."""
    from a2m.generator import generate_project
    from a2m.parser import read_bundle

    bundle = read_bundle(write_keys(tmp_path / "bundles"))
    app_dir = tmp_path / "out" / "keys-v1" / "mule-app"
    generate_project(bundle, app_dir)
    props = app_dir / "src" / "main" / "resources" / "config.properties"
    before = props.read_text(encoding="utf-8")
    assert re.search(r"(?m)^verifyapikey\.[^=\n]*\.allowedKeys=[ \t]*$", before), before
    golden = write_keys_golden(tmp_path / "golden")

    backend.clear()
    recorder = RecordingRunner(cp7_runner)
    result = verify(bundle, app_dir, recorder, backend, golden=golden)

    assert result.type == "golden", describe(result)
    assert recorder.sent == GOLDEN_REQUESTS
    assert "deployed copy only" in result.message, result.message
    assert props.read_text(encoding="utf-8") == before


def executable(pid: int) -> str | None:
    try:
        return Path(os.readlink(f"/proc/{pid}/exe")).name
    except OSError:
        return None


@pytest.mark.runtime
def test_CP7_X16_a_jvm_killed_mid_batch_is_detected_and_the_runtime_restarted(
    cp7_runner: Any, keys_project: KeysProject, backend: Any
) -> None:
    """[CP7-X16] On the real runtime, a JVM killed between two proxies is detected, the runtime restarted, and the
    next proxy still verifies (battery), instead of being blamed for the dead runtime."""
    try:
        backend.clear()
        first = verify(keys_project.bundle, keys_project.app_dir, cp7_runner, backend)
        assert first.type == "battery", describe(first)
        old = [int(p) for p in cp7_runner.pids]
        jvms = [p for p in old if executable(p) == "java" and pid_alive(p)]
        assert jvms, f"no JVM among the runner's recorded PIDs {old}"
        for pid in jvms:
            os.kill(pid, signal.SIGKILL)  # a recorded PID of the runner under test, nothing else
        assert wait_all_dead(jvms, timeout=30) == []

        backend.clear()
        second = verify(keys_project.bundle, keys_project.app_dir, cp7_runner, backend)

        assert second.type == "battery", describe(second)
        assert (second.ran, second.passed) == (3, 3), describe(second)
        new = [int(p) for p in cp7_runner.pids]
        assert new and new[0] != old[0], (old, new)
        assert wait_all_dead(old) == [], "processes of the dead runtime are still alive"
    finally:
        close_runner(cp7_runner)


A2M_MAIN = "import sys\nfrom a2m.cli import main\nsys.exit(main(sys.argv[1:]))\n"


@pytest.mark.runtime
def test_CP7_X17_sigterm_mid_batch_leaves_no_mule_process(runtime_tools: Any, tmp_path: Path) -> None:
    """[CP7-X17] SIGTERM sent to a real `a2m migrate` while its Mule runtime runs: a2m stops that runtime (by its
    recorded PIDs) and exits 143; no Mule process it started is left."""
    from a2m import layout

    exports = tmp_path / "in"
    write_keys(exports)
    results = tmp_path / "results"
    pid_file = layout.mule_base_dir(results) / "a2m-mule.pids"
    env = {k: v for k, v in os.environ.items() if k not in ("FORCE_COLOR", "PY_COLORS")}
    env["PYTHONPATH"] = str(REPO)
    mule_base = layout.mule_base_dir(results)
    groups: list[int] = []
    process = subprocess.Popen(
        [sys.executable, "-c", A2M_MAIN, "migrate", str(exports), "--out", str(results), "--llm", "fake",
         "--mock-backends"],
        cwd=REPO,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    recorded: list[int] = []
    try:
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline and process.poll() is None:
            try:
                data = json.loads(pid_file.read_text(encoding="utf-8"))
                recorded = [int(p) for p in data["pids"]]
                groups = [int(data["pgid"])] if data.get("pgid") else recorded[:1]
            except (OSError, ValueError, KeyError, TypeError):
                recorded = []
            if len(recorded) > 1:
                break
            time.sleep(0.5)
        assert len(recorded) > 1, f"the run never started its Mule runtime (exit code {process.poll()})"
        process.send_signal(signal.SIGTERM)
        _, err = process.communicate(timeout=180)
        assert process.returncode == 143, (process.returncode, err.decode(errors="replace")[-2000:])
        assert wait_all_dead(recorded) == [], f"recorded Mule PIDs still alive: {recorded}"
        leftover = wait_no_leftovers(recorded, groups, mule_base)
        assert leftover == [], f"Mule processes started by the run are still alive: {leftover}"
        assert not layout.mule_base_dir(results).exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(30)
        reap(runtime_leftovers(recorded, groups, mule_base))


# ================================================================ CP7 adversarial round 2 (CP7-X23)
# New blocks only; every line above is locked. X23 starts its own a2m run (the shared session is closed by X16).


@pytest.mark.runtime
def test_CP7_X23_a2m_migrate_with_a_relative_out_verifies_for_real(
    runtime_tools: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """[CP7-X23] `a2m migrate in --out results --mock-backends` run from a temp folder (relative paths, as the
    brief's usage line has them) starts the local Mule runtime and labels the known-good proxy battery. Cleanup
    is checked with the PIDs and the process group the runner recorded, never by matching command lines."""
    from a2m.cli import main
    from a2m.verify.mule import MuleRunner

    write_keys(tmp_path / "in")
    monkeypatch.chdir(tmp_path)
    recorded: list[int] = []
    groups: list[int] = []
    real_write = MuleRunner._write_pid_file

    def record_pids(self: Any) -> None:
        real_write(self)
        pids = [int(p) for p in self.pids]
        recorded.extend(p for p in pids if p not in recorded)
        if pids and pids[0] not in groups:
            groups.append(pids[0])  # the launcher starts its own session: its PID is the group's id

    monkeypatch.setattr(MuleRunner, "_write_pid_file", record_pids)
    try:
        code = main(["migrate", "in", "--out", "results", "--llm", "fake", "--mock-backends"])

        captured = capsys.readouterr()
        assert recorded, "the run never started its Mule runtime"
        assert wait_all_dead(recorded) == [], f"recorded Mule PIDs still alive: {recorded}"
        deadline = time.monotonic() + 30
        leftover = group_members(groups)
        while leftover and time.monotonic() < deadline:
            time.sleep(0.5)
            leftover = group_members(groups)
        assert leftover == [], f"processes of the runtime's process group are still alive: {leftover}"
    finally:
        reap([*recorded, *group_members(groups)])
    assert code == 0, captured.err
    results = tmp_path / "results"
    log = (results / "run.log").read_text(encoding="utf-8")
    lines = [line for line in log.splitlines() if "keys-v1" in line]
    assert any("verification type: battery" in line for line in lines), log
    assert not any("did not start" in line for line in lines), log
    assert not (results / ".a2m-work" / ".mule-base").exists()


# ================================================================ CP7 adversarial round 3
# New blocks only.


def group_members(groups: list[int]) -> list[int]:
    """Live PIDs whose process group is one the runner under test recorded (its launcher's own session)."""
    found: list[int] = []
    if not groups:
        return found
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fields = stat.rsplit(")", 1)[1].split()
        if len(fields) > 2 and fields[0] != "Z" and int(fields[2]) in groups:
            found.append(int(entry.name))
    return sorted(found)
