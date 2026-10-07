"""TUI CP6 adversarial round 3: SIGINT to the TUI stops cleanly; crashes beat stops; per-run warnings; states.

C1: a SIGINT sent to the TUI's own process (not the keyboard's Ctrl-C, which Textual turns off) takes the same
clean stop path as SIGTERM/SIGHUP while a run is going, and ``a2m tui`` ends with one stderr line and 128 +
SIGINT, never a traceback, with or without a run. B2/C5: an error (or a crash exit) after Stop is shown as an
error, not as a clean stop. B4/C4: Resume gets its own one-time unrecognised-line warning. B5: VerifyStage keeps
MULE_BASE when stopping its runtime was cut short. C2: the stopping, Force stop and force-resume confirm states
can be opened by the capture harness.

Children are small real scripts written to ``tmp_path``. Every PID a test starts is recorded and SIGKILLed at
teardown only if still alive; no other process is ever signalled.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pty
import re
import select
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

from tui.screen import _normalize_ws, _run, _screen_text, _settle
from tui.test_cp6_signals import RESUME_ADVICE, Child, kill_own, live, wait_for

REPO = Path(__file__).resolve().parents[2]
ANSI = re.compile(r"\x1b(\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(\x07|\x1b\\)|[PX^_][^\x1b]*\x1b\\|[@-Z\\-_])")


@pytest.fixture
def own_pids() -> Iterator[list[int]]:
    pids: list[int] = []
    yield pids
    kill_own(pids)


def clean_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("FORCE_COLOR", "PY_COLORS")}
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO), env.get("PYTHONPATH", "")]))
    return env


def flat(app: object) -> str:
    return _normalize_ws(_screen_text(app)).lower()


async def until(pilot: object, predicate: object, *, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return
        await pilot.pause(0.05)  # type: ignore[attr-defined]
    raise AssertionError("condition was never met in time")


# ---------------------------------------------------------------- TUI-CP6-X10


def test_TUI_CP6_X10_sigint_to_a2m_tui_with_no_run_ends_with_one_line_and_130(own_pids: list[int]) -> None:
    """[TUI-CP6-X10] A real ``a2m tui`` in a terminal (a pty), with no run going, sent SIGINT to its own pid:
    it exits with 130 and one 'a2m tui: stopped by SIGINT' line, and no traceback reaches the terminal."""
    env = clean_env()
    env["TERM"] = "xterm-256color"
    pid, master = pty.fork()
    if pid == 0:  # the child: run the real command line in the pty
        try:
            os.chdir(REPO)
            os.execve(sys.executable, [sys.executable, "-c", "import sys; from a2m.cli import main; sys.exit(main(['tui']))"], env)
        finally:
            os._exit(127)
    own_pids.append(pid)
    output = bytearray()

    def read_some(seconds: float) -> str:
        """Read what the pty has: 'data', 'idle' (nothing within ``seconds``) or 'eof'."""
        ready, _, _ = select.select([master], [], [], seconds)
        if not ready:
            return "idle"
        try:
            chunk = os.read(master, 65536)
        except OSError:
            return "eof"
        if not chunk:
            return "eof"
        output.extend(chunk)
        return "data"

    status: int | None = None
    try:
        deadline = time.monotonic() + 60.0
        while b"Quit" not in output and time.monotonic() < deadline:  # the footer is drawn: the app is up
            assert read_some(0.2) != "eof", output.decode(errors="replace")[-2000:]
        assert b"Quit" in output, "the TUI never drew its screen"
        time.sleep(0.3)
        os.kill(pid, signal.SIGINT)
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            read_some(0.1)
            done, code = os.waitpid(pid, os.WNOHANG)
            if done:
                status = code
                break
        while read_some(0.2) == "data":  # what is left in the pty after the exit
            pass
    finally:
        os.close(master)
    assert status is not None, "a2m tui did not exit after SIGINT"
    text = ANSI.sub("", output.decode(errors="replace"))
    assert os.WIFEXITED(status), f"a2m tui died by a signal: {status}"
    assert os.WEXITSTATUS(status) == 128 + signal.SIGINT
    assert "Traceback" not in text and "KeyboardInterrupt" not in text, text[-2000:]
    assert text.count("a2m tui: stopped by SIGINT") == 1


# ---------------------------------------------------------------- TUI-CP6-X11

# Opens the real app headless through the command line's own _open_tui, with one run screen.
CLI_WRAPPER = """\
import asyncio, json, sys
from a2m import cli
from a2m.tui.app import A2MApp
from a2m.tui.run import RunScreen

grace, argv = float(sys.argv[1]), json.loads(sys.argv[2])

def run_app(*, at_terminal):
    app = A2MApp()

    async def main():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await app.push_screen(RunScreen(argv, stop_grace=grace))
            while app.return_code is None:
                await asyncio.sleep(0.05)

    asyncio.run(main())
    return app.return_code or 0

sys.exit(cli._open_tui(run_app))
"""


def test_TUI_CP6_X11_sigint_to_the_tui_during_a_run_stops_the_child_first_and_exits_130(
    tmp_path: Path, own_pids: list[int]
) -> None:
    """[TUI-CP6-X11] A real SIGINT sent to the TUI's own process while a run is going takes the SIGTERM/SIGHUP
    path: the child gets exactly one SIGINT, the TUI stays up until the child has exited, and the command line
    then exits 130 with exactly one stderr line and no traceback."""
    child = Child(tmp_path, cleanup_seconds=1.5, advice=RESUME_ADVICE)
    wrapper = tmp_path / "cli_wrapper.py"
    wrapper.write_text(CLI_WRAPPER, encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(wrapper), "30", json.dumps(child.argv)],
        cwd=REPO, env=clean_env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    own_pids.append(proc.pid)
    try:
        wait_for(lambda: bool(child.pids()) or proc.poll() is not None, timeout=30.0, what="the TUI starting its child")
        assert proc.poll() is None
        pid = child.pids()[0]
        own_pids.append(pid)
        time.sleep(0.3)
        os.kill(proc.pid, signal.SIGINT)
        deadline = time.monotonic() + 30.0
        while live(pid):
            assert time.monotonic() < deadline, "the child never exited"
            assert proc.poll() is None, "the TUI exited while its child was still running"
            time.sleep(0.02)
        _out, err = proc.communicate(timeout=30.0)
    finally:
        if proc.poll() is None:
            kill_own([proc.pid])
            proc.wait(timeout=10.0)
    assert child.signals() == ["SIGINT"]
    assert proc.returncode == 128 + signal.SIGINT
    lines = [line for line in err.decode(errors="replace").splitlines() if line.strip()]
    assert len(lines) == 1, lines
    assert lines[0].startswith("a2m tui: stopped by SIGINT")
    assert not live(pid)


# ---------------------------------------------------------------- TUI-CP6-X12 .. X14

OUTCOME_CHILD = """\
import json, os, signal, sys, time
ready, mode = sys.argv[1], sys.argv[2]
got = []
signal.signal(signal.SIGINT, lambda signum, frame: got.append(signum))
signal.signal(signal.SIGTERM, lambda signum, frame: got.append(signum))
def emit(record):
    sys.stdout.write(json.dumps(record) + "\\n")
    sys.stdout.flush()
base = {"schema_version": 1, "total": 3}
emit({**base, "kind": "run-started"})
emit({**base, "kind": "proxy-started", "index": 1, "name": "alpha"})
if mode == "garbled":
    sys.stdout.write("this is not a progress line\\n")
    sys.stdout.flush()
with open(ready, "a", encoding="utf-8") as out:
    out.write(str(os.getpid()) + "\\n")
while not got:
    time.sleep(0.02)
if mode == "error":
    emit({"schema_version": 1, "kind": "error", "exit_code": 1,
          "message": "a2m migrate: error: cannot write results to out: disk full"})
    sys.exit(1)
if mode == "crash":
    sys.stderr.write("RuntimeError: boom during clean-up\\n")
    sys.exit(1)
advice = "rerun with --resume to continue"
emit({"schema_version": 1, "kind": "stopped", "exit_code": 130, "signal": "SIGINT", "advice": advice,
      "message": "a2m migrate: interrupted; " + advice})
sys.exit(130)
"""


def outcome_argv(tmp_path: Path, mode: str) -> tuple[list[str], Path]:
    script = tmp_path / "outcome_child.py"
    script.write_text(OUTCOME_CHILD, encoding="utf-8")
    ready = tmp_path / "ready"
    return [sys.executable, str(script), str(ready), mode], ready


def pids_in(ready: Path) -> list[int]:
    return [int(x) for x in ready.read_text(encoding="utf-8").split()] if ready.is_file() else []


async def press_stop(pilot: object) -> None:
    await pilot.press("s")  # type: ignore[attr-defined]
    await _settle(pilot)
    await pilot.click("#confirm-primary")  # type: ignore[attr-defined]
    await _settle(pilot)


@pytest.mark.parametrize("mode", ["error", "crash"])
def test_TUI_CP6_X12_an_error_or_crash_after_stop_is_shown_as_such_not_as_a_clean_stop(
    tmp_path: Path, own_pids: list[int], mode: str
) -> None:
    """[TUI-CP6-X12] Stop is pressed and confirmed, and the child then reports an error event (or crashes with
    exit 1 and a stderr line, with no 'stopped' event): the run screen shows the error, never 'Stopped at'."""
    argv, ready = outcome_argv(tmp_path, mode)

    async def body() -> None:
        from a2m.tui.app import A2MApp
        from a2m.tui.run import Phase, RunScreen

        app = A2MApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            run = RunScreen(argv)
            await app.push_screen(run)
            await _settle(pilot)
            await until(pilot, lambda: bool(pids_in(ready)))
            own_pids.extend(pids_in(ready))
            await until(pilot, lambda: "alpha" in flat(app))
            await press_stop(pilot)
            await until(pilot, lambda: run.phase not in (Phase.RUNNING, Phase.STOPPING))
            await _settle(pilot)
            assert run.phase is Phase.ENDED
            text = flat(app)
            assert "stoppedat" not in text
            expected = "diskfull" if mode == "error" else "exitcode1"
            assert expected in text, text

    _run(body)


def test_TUI_CP6_X13_resume_gets_its_own_unrecognised_line_warning(tmp_path: Path, own_pids: list[int]) -> None:
    """[TUI-CP6-X13] The first run shows the unrecognised-line warning once; after Stop and Resume, a new
    unrecognised line in the resumed run shows it again."""
    argv, ready = outcome_argv(tmp_path, "garbled")

    async def body() -> None:
        from a2m.tui.app import A2MApp
        from a2m.tui.run import UNRECOGNISED_WARNING, Phase, RunScreen

        app = A2MApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            run = RunScreen(argv)
            warnings: list[object] = []
            notify = run.notify

            def counting(message: object, *args: object, **kwargs: object) -> None:
                if message == UNRECOGNISED_WARNING:
                    warnings.append(message)
                notify(message, *args, **kwargs)  # type: ignore[arg-type]

            run.notify = counting  # type: ignore[method-assign]
            await app.push_screen(run)
            await _settle(pilot)
            await until(pilot, lambda: bool(pids_in(ready)))
            own_pids.extend(pids_in(ready))
            await until(pilot, lambda: len(warnings) == 1)
            await press_stop(pilot)
            await until(pilot, lambda: run.phase is Phase.STOPPED)
            await pilot.press("r")
            await _settle(pilot)
            await until(pilot, lambda: len(pids_in(ready)) == 2)
            own_pids.extend(pids_in(ready)[1:])
            await until(pilot, lambda: len(warnings) == 2)
            assert run.phase is Phase.RUNNING

    _run(body)


# ---------------------------------------------------------------- TUI-CP6-X14


class _FakeRuntime:
    def __init__(self, mule_base: Path, interrupted: bool) -> None:
        self.mule_base = mule_base
        self._interrupted = interrupted

    def close(self) -> None:
        if self._interrupted:
            raise KeyboardInterrupt  # a second signal raised while the runtime was being stopped


@pytest.mark.parametrize("interrupted", [True, False])
def test_TUI_CP6_X14_mule_base_is_kept_when_stopping_the_runtime_was_cut_short(
    tmp_path: Path, interrupted: bool
) -> None:
    """[TUI-CP6-X14] VerifyStage.close removes the private MULE_BASE only once its runtime's stop completed; a
    stop cut short by a signal leaves it (and the PID file the next run uses to end leftovers) in place."""
    from a2m import layout
    from a2m.verify.harness import VerifyStage

    results = tmp_path / "results"
    base = layout.mule_base_dir(results)
    base.mkdir(parents=True)
    (base / "a2m-mule.pids").write_text('{"pgid": 0, "pids": []}\n', encoding="utf-8")
    stage = VerifyStage()
    stage._real = _FakeRuntime(base, interrupted)  # type: ignore[assignment]
    stage._results_root = results
    if interrupted:
        with pytest.raises(KeyboardInterrupt):
            stage.close()
        assert (base / "a2m-mule.pids").is_file()
    else:
        stage.close()
        assert not base.exists()


# ---------------------------------------------------------------- TUI-CP6-X15


def load_states() -> ModuleType:
    spec = importlib.util.spec_from_file_location("tui_states_cp6_x15", REPO / "tools" / "tui_states.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def replay_children(tmp_path: Path) -> list[int]:
    """PIDs of the capture harness's stand-in children started from ``tmp_path`` (and only those)."""
    script = str(tmp_path / "replay_child.py").encode()
    found = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                if script in (entry / "cmdline").read_bytes().split(b"\0"):
                    found.append(int(entry.name))
            except OSError:
                continue
    return found


@pytest.mark.parametrize("state", ["run-stopping", "run-force-stop", "run-force-resume-confirm"])
def test_TUI_CP6_X15_capture_harness_opens_the_stopping_force_stop_and_force_resume_states(
    tmp_path: Path, own_pids: list[int], state: str
) -> None:
    """[TUI-CP6-X15] tools/tui_states.py opens each new state: stopping (waiting, no Force stop yet), Force
    stop offered with its warning, and the --force Resume confirm."""
    states = load_states()

    async def body() -> None:
        from a2m.tui.confirm import ConfirmScreen
        from a2m.tui.run import FORCE_WARNING, STOPPING_TEXT, Phase, RunScreen

        app = states.STATES[state](tmp_path)
        async with app.run_test(size=(80, 30)) as pilot:
            await until(pilot, lambda: any(isinstance(s, RunScreen) for s in app.screen_stack))
            run = next(s for s in app.screen_stack if isinstance(s, RunScreen))
            await until(pilot, lambda: bool(replay_children(tmp_path)))
            own_pids.extend(replay_children(tmp_path))
            if state == "run-stopping":
                await until(pilot, lambda: run.phase is Phase.STOPPING)
                await pilot.pause(0.3)
                assert _normalize_ws(STOPPING_TEXT).lower() in flat(app)
                assert not run.query_one("#force-stop").display
                for pid in replay_children(tmp_path):
                    os.kill(pid, signal.SIGTERM)  # end the stand-in's clean-up so the app can close
            elif state == "run-force-stop":
                await until(pilot, lambda: run.phase is Phase.STOPPING and run.query_one("#force-stop").display)
                await pilot.pause(0.3)
                assert _normalize_ws(FORCE_WARNING).lower() in flat(app)
                await pilot.press("f")
            else:
                await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
                await pilot.pause(0.3)
                assert "--force" in _screen_text(app)
                assert run.phase is Phase.STOPPED
            await until(pilot, lambda: not run.running)

    _run(body)
