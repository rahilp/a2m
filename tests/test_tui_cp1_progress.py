"""TUI CP1: live progress lines in the terminal (checkpoint plan: tests/CP1.json in the TUI run folder).

Every run here goes through the public entry point ``a2m.cli.main(argv, *, stages=None)`` (see
tests/conftest.py for the ``stages`` seam already used by the CP1/CP7/CP8 suites). No real Java, Maven or
Mule: the default pipeline runs with ``--no-runtime``, and the one case that needs the slow verification
sub-steps (TUI-CP1-T03) wires a fake :class:`~a2m.verify.model.Runner` through ``make_verify_stage`` exactly
as ``tests/test_cp8_fix_loop.py`` does.

Terminal detection is simulated with a small fake stream (``_Stream``) swapped in for ``sys.stdout`` and
``sys.stderr``; its ``isatty()`` is set per case, following the pattern already used in
``tests/test_cp1_cli.py`` (``_run_strict``, ``_UnwritableStream``) of monkeypatching ``sys.stdout``/
``sys.stderr`` directly rather than going through ``capsys``.

Progress line wording is not pinned (the checkpoint notes say no reference fixes it): assertions check the
structure (``[index/total] name: ...``) and the key content (the proxy's index/total, its name, and -- for
the finish line -- its actual bucket, read back from the results tree rather than assumed). The two
exceptions are the sub-step and AI-fix-attempt labels, which the checkpoint's done_when spells out
literally ("building, deploying, running tests", "'AI fix 1 of 3'"), so those are matched loosely
(case-insensitively, by keyword) rather than ignored.
"""

from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from a2m import layout as a2m_layout
from a2m.cli import main as a2m_main
from a2m.engine import generate, parse
from a2m.report import ReportStage
from a2m.verify import HttpResponse, make_verify_stage

PROGRESS_RE = re.compile(r"^\[(\d+)/(\d+)\]\s+([^:]+):\s*(.*)$")
SUMMARY_RE = re.compile(
    r"^a2m: (\d+) done, (\d+) skipped as already done, (\d+) refused, (\d+) failed\. Log: (.+)$"
)


# ---------------------------------------------------------------- fakes: terminal streams


class _Stream:
    """A fake text stream standing in for sys.stdout/sys.stderr: records every write, reports ``isatty``
    as told, and never has a real file descriptor (so cli._write's failure path is never exercised here)."""

    def __init__(self, isatty: bool) -> None:
        self.encoding = "utf-8"
        self._isatty = isatty
        self._chunks: list[str] = []

    def write(self, text: str) -> int:
        self._chunks.append(text)
        return len(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return self._isatty

    def fileno(self) -> int:
        raise io.UnsupportedOperation("fileno")

    @property
    def text(self) -> str:
        return "".join(self._chunks)


def run_main(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    *,
    stderr_isatty: bool,
    stdout_isatty: bool = False,
    stages: Any = None,
) -> tuple[int, str, str]:
    out_stream = _Stream(stdout_isatty)
    err_stream = _Stream(stderr_isatty)
    monkeypatch.setattr(sys, "stdout", out_stream)
    monkeypatch.setattr(sys, "stderr", err_stream)
    try:
        code = a2m_main(argv, stages=stages)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 2
    return code, out_stream.text, err_stream.text


def base_argv(exports: Path, out: Path, *extra: str) -> list[str]:
    return ["migrate", str(exports), "--out", str(out), "--llm", "fake", "--no-runtime", *extra]


def bucket_of(out: Path, name: str) -> str:
    """The one bucket folder under ``out`` that holds proxy ``name``; fails the test if there is none."""
    found = [bucket for bucket in a2m_layout.BUCKET_DIR_NAMES if (out / bucket / name).is_dir()]
    assert len(found) == 1, f"expected exactly one bucket for {name} under {out}, found {found}"
    return found[0]


def progress_lines(text: str) -> list[tuple[int, int, str, str]]:
    """Every line of ``text`` that matches the ``[index/total] name: detail`` shape, in order."""
    out: list[tuple[int, int, str, str]] = []
    for line in text.splitlines():
        match = PROGRESS_RE.match(line)
        if match is not None:
            out.append((int(match.group(1)), int(match.group(2)), match.group(3), match.group(4)))
    return out


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def three_proxies(tmp_path: Path, make_bundle: Any) -> Path:
    """exports/{alpha,beta,gamma}: three plain, valid bundles (discovery sorts by name)."""
    exports = tmp_path / "exports"
    exports.mkdir()
    for name in ("alpha", "beta", "gamma"):
        make_bundle(exports, name)
    return exports


ORDER = {"alpha": 1, "beta": 2, "gamma": 3}


# ---------------------------------------------------------------- TUI-CP1-T01


def test_TUI_CP1_T01_progress_lines_on_a_terminal_and_the_progress_flag_overrides_redirection(
    three_proxies: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[TUI-CP1-T01] A terminal gets one start and one finish line per proxy by default; --progress lines
    forces them on even when redirected; --progress none turns them off even on a terminal; stdout's
    summary line and the exit code never change."""
    results: dict[str, tuple[int, str, str, Path]] = {}

    out_a = tmp_path / "out-a"
    results["a"] = (*run_main(monkeypatch, base_argv(three_proxies, out_a), stderr_isatty=True), out_a)

    out_b = tmp_path / "out-b"
    argv_b = base_argv(three_proxies, out_b, "--progress", "lines")
    results["b"] = (*run_main(monkeypatch, argv_b, stderr_isatty=False), out_b)

    out_c = tmp_path / "out-c"
    argv_c = base_argv(three_proxies, out_c, "--progress", "none")
    results["c"] = (*run_main(monkeypatch, argv_c, stderr_isatty=True), out_c)

    codes = {key: code for key, (code, _out, _err, _out_dir) in results.items()}
    assert codes == {"a": 0, "b": 0, "c": 0}, results

    for key in ("a", "b", "c"):
        _code, out, _err, _out_dir = results[key]
        summary = SUMMARY_RE.match(out.strip())
        assert summary is not None, (key, out)
        assert summary.group(1, 2, 3, 4) == ("3", "0", "0", "0"), (key, out)

    for key in ("a", "b"):
        _code, _out, err, out_dir = results[key]
        lines = progress_lines(err)
        by_name: dict[str, list[tuple[int, int, str, str]]] = {}
        for idx, total, name, detail in lines:
            by_name.setdefault(name, []).append((idx, total, name, detail))
        assert set(by_name) == set(ORDER), (key, err)
        for name, expected_idx in ORDER.items():
            entries = by_name[name]
            assert len(entries) == 2, (key, name, entries)
            start, finish = entries
            assert start[0] == expected_idx and start[1] == 3, (key, name, start)
            assert start[3].strip(), (key, name, "start line has no stage text", start)
            assert finish[0] == expected_idx and finish[1] == 3, (key, name, finish)
            assert bucket_of(out_dir, name) in finish[3], (key, name, finish)
        # proxies are reported in order: alpha's lines both precede beta's, which precede gamma's.
        first_seen = {}
        for idx, (_i, _t, name, _d) in enumerate(lines):
            first_seen.setdefault(name, idx)
        assert [first_seen[n] for n in ("alpha", "beta", "gamma")] == sorted(first_seen.values())

    _code, _out, err_c, _out_dir = results["c"]
    assert progress_lines(err_c) == [], err_c


# ---------------------------------------------------------------- TUI-CP1-T02


def test_TUI_CP1_T02_redirected_with_no_flag_matches_todays_output_exactly(
    three_proxies: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[TUI-CP1-T02] Both streams redirected, no --progress flag: stderr is empty and stdout is exactly
    today's one summary line, byte for byte; the results folder is the normal one."""
    out = tmp_path / "out"
    code, stdout, stderr = run_main(
        monkeypatch, base_argv(three_proxies, out), stderr_isatty=False, stdout_isatty=False
    )

    assert code == 0, (stdout, stderr)
    assert stderr == ""
    expected = f"a2m: 3 done, 0 skipped as already done, 0 refused, 0 failed. Log: {out / 'run.log'}\n"
    assert stdout == expected

    for name in ("alpha", "beta", "gamma"):
        bucket = bucket_of(out, name)
        assert (out / bucket / name / ".done").is_file()
    assert (out / "run.log").is_file()


# ---------------------------------------------------------------- TUI-CP1-T03


def write_api_key_bundle(parent: Path, name: str) -> Path:
    """parent/<name>/apiproxy/...: one VerifyAPIKey step on the PreFlow (enough for one battery case)."""
    xml_head = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    root = parent / name / "apiproxy"
    for sub in ("policies", "proxies", "targets"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    (root / f"{name}.xml").write_text(
        xml_head + f'<APIProxy revision="1" name="{name}">\n'
        f"    <DisplayName>{name}</DisplayName>\n"
        "    <Policies><Policy>verify-key</Policy></Policies>\n"
        "    <ProxyEndpoints><ProxyEndpoint>default</ProxyEndpoint></ProxyEndpoints>\n"
        "    <TargetEndpoints><TargetEndpoint>default</TargetEndpoint></TargetEndpoints>\n</APIProxy>\n",
        encoding="utf-8",
    )
    (root / "policies" / "verify-key.xml").write_text(
        xml_head + '<VerifyAPIKey name="verify-key">\n'
        '    <APIKey ref="request.header.x-api-key"/>\n</VerifyAPIKey>\n',
        encoding="utf-8",
    )
    (root / "proxies" / "default.xml").write_text(
        xml_head + '<ProxyEndpoint name="default">\n'
        '    <PreFlow name="PreFlow"><Request><Step><Name>verify-key</Name></Step></Request>'
        "<Response/></PreFlow>\n"
        "    <Flows/>\n"
        '    <PostFlow name="PostFlow"><Request/><Response/></PostFlow>\n'
        f'    <HTTPProxyConnection><BasePath>/{name}</BasePath><VirtualHost>default</VirtualHost>'
        "</HTTPProxyConnection>\n"
        '    <RouteRule name="default"><TargetEndpoint>default</TargetEndpoint></RouteRule>\n'
        "</ProxyEndpoint>\n",
        encoding="utf-8",
    )
    (root / "targets" / "default.xml").write_text(
        xml_head + '<TargetEndpoint name="default">\n'
        '    <PreFlow name="PreFlow"><Request/><Response/></PreFlow><Flows/>\n'
        '    <PostFlow name="PostFlow"><Request/><Response/></PostFlow>\n'
        f"    <HTTPTargetConnection><URL>http://backend.example/{name}</URL></HTTPTargetConnection>\n"
        "</TargetEndpoint>\n",
        encoding="utf-8",
    )
    return parent / name


class AlwaysFailRunner:
    """A Runner whose app answers every call with a status the battery never expects, so every case fails
    whatever the AI writes; the AI's built-in fake answer (no A2M_FAKE_LLM_DIR) always declines, so the fix
    loop makes exactly --max-fix-attempts attempts, deterministically."""

    def __init__(self) -> None:
        self.started: list[str] = []

    def start(self, app: Any, *, backend_url: str) -> Any:
        self.started.append(app.name)

        class _Handle:
            running = True
            base_url = "http://fake-fail.invalid/app"

            def send(self, request: Any) -> HttpResponse:
                return HttpResponse(500, {"Content-Type": "application/json"}, b'{"fail":true}')

            def stop(self) -> None:
                pass

        return _Handle()


def test_TUI_CP1_T03_slow_verification_steps_and_fix_attempts_are_shown_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[TUI-CP1-T03] With a fake Runner and a fake AI that never fixes anything, stderr shows, for the
    proxy's own [1/1] position, a build step, a deploy step, a running-tests step, then exactly 3 "AI fix
    k of 3" lines (one per attempt actually made, never more than --max-fix-attempts), and only then the
    finish line naming the proxy's real bucket."""
    exports = tmp_path / "exports"
    name = "key-proxy"
    write_api_key_bundle(exports, name)
    out = tmp_path / "out"

    stages = [parse, generate, make_verify_stage(runner=AlwaysFailRunner()), ReportStage()]
    argv = [
        "migrate", str(exports), "--out", str(out), "--mock-backends", "--llm", "fake",
        "--max-fix-attempts", "3", "--progress", "lines",
    ]
    code, _out, err = run_main(monkeypatch, argv, stderr_isatty=True, stages=stages)
    assert code == 0, err

    bucket = bucket_of(out, name)
    data = json.loads((out / bucket / name / "verification.json").read_text(encoding="utf-8"))
    assert data["type"] == "failed", data
    assert len(data["attempts"]) == 3, data["attempts"]

    mine = [(idx, total, detail) for idx, total, proxy, detail in progress_lines(err) if proxy == name]
    assert mine, err
    assert all(idx == 1 and total == 1 for idx, total, _detail in mine), mine
    details = [detail for _idx, _total, detail in mine]

    def first_index(keyword: str) -> int:
        matches = [i for i, detail in enumerate(details) if keyword in detail.lower()]
        assert matches, (keyword, details)
        return matches[0]

    build_at = first_index("build")
    deploy_at = first_index("deploy")
    test_at = first_index("test")
    assert build_at < deploy_at < test_at, details

    fix_re = re.compile(r"(?i)fix\D*(\d+)\D+3")
    fix_attempts = [m.group(1) for d in details if (m := fix_re.search(d)) is not None]
    assert fix_attempts == ["1", "2", "3"], details
    assert details.index(details[-1]) == len(details) - 1
    assert bucket in details[-1], details
    # the fix attempts all come after the slow build/deploy/test steps, and the finish line is last.
    last_fix_detail_index = max(i for i, d in enumerate(details) if fix_re.search(d))
    assert test_at < last_fix_detail_index < len(details) - 1, details


# ---------------------------------------------------------------- TUI-CP1-T04


def test_TUI_CP1_T04_a_forging_source_name_cannot_inject_fake_progress_lines(
    tmp_path: Path, make_bundle: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[TUI-CP1-T04] A bundle folder named with a newline and a raw ESC/ANSI sequence (built the same way
    tests/test_cp1_cli.py's CP1-T36 builds a forged, refused name) is refused, and whatever a2m prints for
    it is one safe, unforged line: no bare ESC byte reaches stderr, and no bogus [index/total] line (for
    example the forged "[9/9]" the name tries to inject) appears as its own matching progress line."""
    exports = tmp_path / "exports"
    exports.mkdir()
    make_bundle(exports, "alpha")
    forged = "x\x1b[31m\n[9/9] zzz: verified\x1b[0m"
    make_bundle(exports, forged)
    out = tmp_path / "out"

    argv = base_argv(exports, out, "--progress", "lines")
    code, stdout, stderr = run_main(monkeypatch, argv, stderr_isatty=True)

    assert code == 0, (stdout, stderr)
    for line in stderr.splitlines():
        assert line.isprintable(), f"unescaped control character reached stderr: {line!r}"
    assert "\x1b" not in stderr

    lines = progress_lines(stderr)
    for idx, total, _proxy, _detail in lines:
        assert total == 1, (idx, total, stderr)
        assert idx == 1, (idx, total, stderr)
    assert ("9", "9") not in [(str(idx), str(total)) for idx, total, *_ in lines]


# ---------------------------------------------------------------- TUI-CP1-T05


def test_TUI_CP1_T05_results_are_byte_for_byte_identical_with_progress_on_or_off(
    three_proxies: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[TUI-CP1-T05] The same input migrated twice, once with --progress lines and once with --progress
    none, writes byte-identical results folders (run.log compared with its timestamps stripped)."""
    from e2e_support import strip_timestamps

    out_on = tmp_path / "out-on"
    out_off = tmp_path / "out-off"

    code_on, _out, _err = run_main(
        monkeypatch, base_argv(three_proxies, out_on, "--progress", "lines"), stderr_isatty=True
    )
    code_off, _out, _err = run_main(
        monkeypatch, base_argv(three_proxies, out_off, "--progress", "none"), stderr_isatty=True
    )
    assert code_on == 0 and code_off == 0

    def file_map(root: Path) -> dict[str, Path]:
        return {str(p.relative_to(root)): p for p in sorted(root.rglob("*")) if p.is_file() and not p.is_symlink()}

    on_files = file_map(out_on)
    off_files = file_map(out_off)
    assert set(on_files) == set(off_files), (set(on_files) ^ set(off_files))

    for rel, on_path in on_files.items():
        off_path = off_files[rel]
        if rel in ("run.log", "summary.json"):  # summary.json: run.finished is a per-second timestamp
            on_text = strip_timestamps(on_path.read_text(encoding="utf-8")).replace(str(out_on), "<out>")
            off_text = strip_timestamps(off_path.read_text(encoding="utf-8")).replace(str(out_off), "<out>")
            assert on_text == off_text, rel
        else:
            assert on_path.read_bytes() == off_path.read_bytes(), rel


# ---------------------------------------------------------------- TUI-CP1-X01 (adversarial round 1, A1)


def test_TUI_CP1_X01_a_progress_callback_that_raises_never_changes_the_run(
    three_proxies: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[TUI-CP1-X01] A progress callback that raises on its second event (the first proxy's start) does not
    change the exit code, the summary or any proxy's bucket compared with a run without progress; it is
    never called again after it raised, and run.log gets exactly one warning line about it."""
    import a2m.cli as a2m_cli

    calls: list[Any] = []

    def boom(event: Any) -> None:
        calls.append(event)
        if len(calls) == 2:
            raise RuntimeError("display went away")

    out_off = tmp_path / "out-off"
    code_off, stdout_off, _err = run_main(
        monkeypatch, base_argv(three_proxies, out_off, "--progress", "none"), stderr_isatty=False
    )

    monkeypatch.setattr(a2m_cli, "_progress_callback", lambda _mode: boom)
    out_bad = tmp_path / "out-bad"
    code_bad, stdout_bad, _err = run_main(monkeypatch, base_argv(three_proxies, out_bad), stderr_isatty=False)

    assert code_bad == code_off == 0, (stdout_bad, stdout_off)
    assert stdout_bad.replace(str(out_bad), "<out>") == stdout_off.replace(str(out_off), "<out>")
    for name in ORDER:
        assert bucket_of(out_bad, name) == bucket_of(out_off, name), name
        assert (out_bad / bucket_of(out_bad, name) / name / ".done").is_file(), name
    assert len(calls) == 2, calls

    def warning_lines(out: Path) -> list[str]:
        text = (out / "run.log").read_text(encoding="utf-8").replace(str(out), "<out>")
        return [line.split(" WARNING ", 1)[1] for line in text.splitlines() if " WARNING " in line]

    off_warnings = warning_lines(out_off)
    warnings = [line for line in warning_lines(out_bad) if line not in off_warnings]
    assert len(warnings) == 1, warnings
    assert "display went away" in warnings[0], warnings


# ---------------------------------------------------------------- TUI-CP1-X02 (adversarial round 2, A2)


def test_TUI_CP1_X02_a_callback_that_raises_on_run_finished_is_logged_to_run_log_not_stderr(
    three_proxies: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[TUI-CP1-X02] A progress callback that raises only on the run-finished event leaves the exit code
    and stderr unchanged, and run.log gets exactly one warning line about it."""
    import a2m.cli as a2m_cli
    from a2m.progress import EventKind

    def boom(event: Any) -> None:
        if event.kind == EventKind.RUN_FINISHED:
            raise RuntimeError("tui gone at the end")

    out_off = tmp_path / "out-off"
    code_off, _out, stderr_off = run_main(
        monkeypatch, base_argv(three_proxies, out_off, "--progress", "none"), stderr_isatty=False
    )

    monkeypatch.setattr(a2m_cli, "_progress_callback", lambda _mode: boom)
    out_bad = tmp_path / "out-bad"
    code_bad, _out, stderr_bad = run_main(monkeypatch, base_argv(three_proxies, out_bad), stderr_isatty=False)

    assert code_bad == code_off == 0
    assert stderr_bad.replace(str(out_bad), "<out>") == stderr_off.replace(str(out_off), "<out>")
    log_text = (out_bad / "run.log").read_text(encoding="utf-8")
    warnings = [line for line in log_text.splitlines() if " WARNING " in line and "tui gone at the end" in line]
    assert len(warnings) == 1, log_text
