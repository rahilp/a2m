"""TUI CP2: a machine-readable progress stream another program can follow (checkpoint plan: tests/CP2.json
in the TUI run folder).

Every case here is the planned code-layer case from the checkpoint plan; there are no browser-layer cases
to skip. Three of the four cases need a real subprocess (the plan's own strategy): T01 compares the
installed ``a2m`` console script against ``python -m a2m`` (both new with this checkpoint: a2m/__main__.py
does not exist yet), and T02 sends a real SIGINT to a child process so it can be observed mid-run, the same
pattern as tests/test_cp7_verify.py's SIGNAL_SCRIPT. T03 and T04 stay in-process through
a2m.cli.main/tests/conftest.py's run_cli fixture, since they need no real signal delivery.

Progress JSON is read as data, never as text: every stdout line must parse on its own with json.loads, and
assertions check field values (kind, name, total, the run-finished counts) against the real vocabulary in
a2m/progress.py (EventKind). The one field this checkpoint still has to add -- the final "stopped" event's
rerun advice -- is checked against a2m.engine.rerun_advice's own text, not a hardcoded string, so the test
does not pin wording the plan does not fix.

No case touches Java, Maven or the real Mule runtime (--no-runtime, --llm fake throughout, matching every
other CLI suite in this repo).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from a2m import layout as a2m_layout
from a2m import safefs as a2m_safefs
from a2m.progress import EventKind

REPO = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- helpers shared by every case


def _child_env(base: dict[str, str]) -> dict[str, str]:
    """``base`` (already stripped of ANTHROPIC_API_KEY by the subprocess_env fixture) minus FORCE_COLOR and
    PY_COLORS (so a2m's own output cannot pick up stray ANSI codes a JSON parser would choke on), plus a
    PYTHONPATH pointing at the repo so the child always finds this checked-out ``a2m``."""
    env = {k: v for k, v in base.items() if k not in ("FORCE_COLOR", "PY_COLORS")}
    env["PYTHONPATH"] = str(REPO)
    return env


def _console_script() -> Path:
    exe = Path(sys.executable).parent / "a2m"
    assert exe.is_file(), f"console script not installed at {exe}"
    return exe


def _parse_json_lines(text: str) -> list[dict[str, Any]]:
    """Every non-empty line of ``text`` parsed as its own JSON object.

    Deliberately not forgiving: a multi-line object, stray trailing text on the final line, or any plain-text
    notice mixed into stdout makes one of these json.loads calls raise, which is exactly the failure a reader
    of the stream would hit.
    """
    lines = [line for line in text.split("\n") if line != ""]
    return [json.loads(line) for line in lines]


def _one_stderr_line(err: str) -> str:
    lines = [line for line in err.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one stderr line, got: {err!r}"
    return lines[0]


def _tree_state(folder: Path) -> dict[str, object]:
    state: dict[str, object] = {}
    if not folder.exists():
        return state
    for path in sorted(folder.rglob("*")):
        rel = str(path.relative_to(folder))
        if path.is_symlink():
            state[rel] = ("link", os.readlink(path))
        elif path.is_dir():
            state[rel] = ("dir",)
        else:
            state[rel] = ("file", path.read_bytes())
    return state


def _bucket_of(out: Path, name: str) -> str:
    found = [b for b in a2m_layout.BUCKET_DIR_NAMES if (out / b / name).is_dir()]
    assert len(found) == 1, f"expected exactly one bucket for {name} under {out}, found {found}"
    return found[0]


def _is_json_line(line: str) -> bool:
    try:
        json.loads(line)
    except json.JSONDecodeError:
        return False
    return True


# ---------------------------------------------------------------- TUI-CP2-T01


def test_TUI_CP2_T01_every_progress_event_is_one_json_line_with_a_correct_final_event(
    mixed_exports: Path, tmp_path: Path, subprocess_env: dict[str, str]
) -> None:
    """[TUI-CP2-T01] `a2m migrate ... --progress json` and the equivalent `python -m a2m migrate ...` both
    print one JSON object per stdout line, starting with run-started/total=3, naming alpha, beta and gamma
    in a proxy-started and a later proxy-finished event each, ending with exactly one run-finished event
    whose counts match what happened, and agree on their sequence of event kinds and exit code."""
    env = _child_env(subprocess_env)
    exe = _console_script()
    out_a = tmp_path / "out-a"
    out_b = tmp_path / "out-b"

    cmd_a = [str(exe), "migrate", str(mixed_exports), "--out", str(out_a), "--llm", "fake", "--no-runtime",
              "--progress", "json"]
    cmd_b = [sys.executable, "-m", "a2m", "migrate", str(mixed_exports), "--out", str(out_b), "--llm", "fake",
              "--no-runtime", "--progress", "json"]

    proc_a = subprocess.run(cmd_a, cwd=REPO, env=env, capture_output=True, text=True, timeout=120, check=False)
    proc_b = subprocess.run(cmd_b, cwd=REPO, env=env, capture_output=True, text=True, timeout=120, check=False)

    kinds_by_label: dict[str, list[str]] = {}
    for label, proc in (("a2m", proc_a), ("python -m a2m", proc_b)):
        assert proc.returncode == 0, (label, proc.stdout, proc.stderr)
        events = _parse_json_lines(proc.stdout)
        assert events, (label, proc.stdout, proc.stderr)

        assert events[0]["kind"] == EventKind.RUN_STARTED.value, (label, events[0])
        assert events[0]["total"] == 3, (label, events[0])

        started_names = {e["name"] for e in events if e["kind"] == EventKind.PROXY_STARTED.value}
        finished_names = {e["name"] for e in events if e["kind"] == EventKind.PROXY_FINISHED.value}
        assert started_names == {"alpha", "beta", "gamma"}, (label, events)
        assert finished_names == {"alpha", "beta", "gamma"}, (label, events)

        for name in ("alpha", "beta", "gamma"):
            start_i = next(
                i for i, e in enumerate(events) if e["kind"] == EventKind.PROXY_STARTED.value and e["name"] == name
            )
            finish_i = next(
                i for i, e in enumerate(events) if e["kind"] == EventKind.PROXY_FINISHED.value and e["name"] == name
            )
            assert start_i < finish_i, (label, name, events)

        final = events[-1]
        assert final["kind"] == EventKind.RUN_FINISHED.value, (label, final)
        assert (final["finished"], final["skipped"], final["refused"], final["failed"]) == (3, 0, 0, 0), (
            label, final,
        )
        kinds_by_label[label] = [e["kind"] for e in events]

    assert kinds_by_label["a2m"] == kinds_by_label["python -m a2m"], kinds_by_label
    assert proc_a.returncode == proc_b.returncode


# ---------------------------------------------------------------- TUI-CP2-T02


BLOCK_SCRIPT = """\
import sys, time
from pathlib import Path
from a2m.cli import main
from a2m.engine import default_stages

ready, exports, out = sys.argv[1:4]
stages = list(default_stages())


class Block:
    __name__ = "block-stage"

    def __call__(self, proxy):
        if proxy.name == "beta":
            Path(ready).write_text("blocked", encoding="utf-8")
            time.sleep(120)


sys.exit(main(
    ["migrate", exports, "--out", out, "--llm", "fake", "--no-runtime", "--progress", "json"],
    stages=[stages[0], stages[1], Block(), *stages[2:]],
))
"""


def _wait_for_ready(path: Path, process: subprocess.Popen[str], timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file() and path.read_text(encoding="utf-8").strip():
            return
        if process.poll() is not None:
            raise AssertionError(f"child exited before writing {path} (exit code {process.poll()})")
        time.sleep(0.1)
    raise AssertionError(f"{path} never appeared (child still running: {process.poll() is None})")


def test_TUI_CP2_T02_ctrl_c_during_a_json_run_ends_the_stream_with_a_stopped_event(
    mixed_exports: Path, tmp_path: Path, subprocess_env: dict[str, str]
) -> None:
    """[TUI-CP2-T02] Ctrl-C sent once alpha has finished and beta's stage is blocked: the process exits 130,
    its very last stdout line is a JSON 'stopped' event carrying the same rerun advice
    a2m.engine.rerun_advice gives for this run, alpha keeps its .done marker, and beta/gamma have none."""
    from a2m.engine import RunOptions, rerun_advice

    script = tmp_path / "block.py"
    script.write_text(BLOCK_SCRIPT, encoding="utf-8")
    ready = tmp_path / "ready"
    out = tmp_path / "out"
    env = _child_env(subprocess_env)

    process = subprocess.Popen(
        [sys.executable, str(script), str(ready), str(mixed_exports), str(out)],
        cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        _wait_for_ready(ready, process, timeout=60)
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=60)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(10)

    assert process.returncode == 130, (process.returncode, stdout, stderr)

    events = _parse_json_lines(stdout)
    assert events, (stdout, stderr)
    last = events[-1]
    assert last["kind"] == "stopped", (last, events)

    expected_advice = rerun_advice(RunOptions(input_dir=mixed_exports, out_dir=out))
    text_values = [v for v in last.values() if isinstance(v, str)]
    assert any(expected_advice in v for v in text_values), (expected_advice, last)

    alpha_bucket = _bucket_of(out, "alpha")
    assert (out / alpha_bucket / "alpha" / ".done").is_file()
    for name in ("beta", "gamma"):
        assert not any((out / b / name / ".done").is_file() for b in a2m_layout.BUCKET_DIR_NAMES), name
        assert not (out / name / ".done").is_file(), name


# ---------------------------------------------------------------- TUI-CP2-T03


def test_TUI_CP2_T03_usage_errors_are_one_stderr_line_and_never_a_json_event(
    mixed_exports: Path, tmp_path: Path, run_cli: Any
) -> None:
    """[TUI-CP2-T03] A missing exports folder, a results folder already locked by another process, and an
    unrecognised --progress value all leave stdout with no JSON lines at all, give exactly one clear stderr
    line, exit 2, and (for the locked-results case) leave the results folder byte-for-byte unchanged."""
    # scenario 1: the exports folder does not exist.
    missing = tmp_path / "does-not-exist"
    res1 = run_cli(
        ["migrate", str(missing), "--out", str(tmp_path / "out1"), "--llm", "fake", "--no-runtime",
         "--progress", "json"]
    )
    assert res1.code == 2, (res1.out, res1.err)
    assert res1.out == "", res1.out
    _one_stderr_line(res1.err)

    # scenario 2: the results folder is locked by another process.
    out2 = tmp_path / "out2"
    first = run_cli(["migrate", str(mixed_exports), "--out", str(out2), "--llm", "fake", "--no-runtime"])
    assert first.code == 0, (first.out, first.err)
    before = _tree_state(out2)

    with a2m_safefs.exclusive_lock(out2, a2m_layout.lock_path(out2)):
        res2 = run_cli(
            ["migrate", str(mixed_exports), "--out", str(out2), "--llm", "fake", "--no-runtime",
             "--progress", "json", "--resume"]
        )
    assert res2.code == 2, (res2.out, res2.err)
    assert res2.out == "", res2.out
    line2 = _one_stderr_line(res2.err)
    assert "in use by another a2m run" in line2, line2
    assert _tree_state(out2) == before

    # scenario 3: --progress is given a value nobody defined.
    res3 = run_cli(
        ["migrate", str(mixed_exports), "--out", str(tmp_path / "out3"), "--llm", "fake", "--no-runtime",
         "--progress", "loud"]
    )
    assert res3.code == 2, (res3.out, res3.err)
    assert res3.out == "", res3.out
    _one_stderr_line(res3.err)


# ---------------------------------------------------------------- TUI-CP2-T04


def test_TUI_CP2_T04_adding_progress_json_does_not_change_the_other_progress_modes(
    mixed_exports: Path, tmp_path: Path, run_cli: Any
) -> None:
    """[TUI-CP2-T04] Repeating the same run with no --progress flag, --progress lines and --progress none:
    none of the three prints a JSON line to stdout, and the three results folders are byte-for-byte
    identical to each other except run.log/summary.json timestamps (CP1's own guarantee)."""
    from e2e_support import strip_timestamps

    outs: dict[str, Path] = {}
    for label, extra in (("default", []), ("lines", ["--progress", "lines"]), ("none", ["--progress", "none"])):
        out = tmp_path / f"out-{label}"
        res = run_cli(["migrate", str(mixed_exports), "--out", str(out), "--llm", "fake", "--no-runtime", *extra])
        assert res.code == 0, (label, res.out, res.err)
        assert not any(_is_json_line(line) for line in res.out.splitlines() if line.strip()), (label, res.out)
        assert not any(_is_json_line(line) for line in res.err.splitlines() if line.strip()), (label, res.err)
        outs[label] = out

    def file_map(root: Path) -> dict[str, Path]:
        return {str(p.relative_to(root)): p for p in sorted(root.rglob("*")) if p.is_file() and not p.is_symlink()}

    maps = {label: file_map(out) for label, out in outs.items()}
    names = {label: set(m) for label, m in maps.items()}
    assert names["default"] == names["lines"] == names["none"], names

    base_label = "default"
    for label in ("lines", "none"):
        for rel, base_path in maps[base_label].items():
            other_path = maps[label][rel]
            if rel in ("run.log", "summary.json"):
                base_text = strip_timestamps(base_path.read_text(encoding="utf-8")).replace(
                    str(outs[base_label]), "<out>"
                )
                other_text = strip_timestamps(other_path.read_text(encoding="utf-8")).replace(
                    str(outs[label]), "<out>"
                )
                assert base_text == other_text, (label, rel)
            else:
                assert base_path.read_bytes() == other_path.read_bytes(), (label, rel)
