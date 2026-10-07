"""TUI CP6 adversarial round 2 (findings X1, B1): a --force Resume asks first; SIGTERM/SIGHUP stop the child.

X1: when the stopped run's own advice names --force, Resume would redo every proxy, so it asks first with
Cancel focused, and runs only once confirmed. B1: a SIGTERM or SIGHUP sent to the TUI's own process while a
run is going stops the child the same way a confirmed quit does (one SIGINT, the same clean-up budget, SIGTERM
only once that budget is over, never SIGKILL), and the TUI exits only after its child has exited.

The child is a small real script written to ``tmp_path``: it logs every signal it receives and the arguments
of every launch, waits for SIGINT, "cleans up" for a set time, then prints a ``stopped`` event carrying the
advice it was given. The signal cases run the real ``A2MApp`` headless in a small wrapper process and send the
real signal to that wrapper's pid. Every PID a test starts is recorded and SIGKILLed at teardown only if still
alive; no other process is ever signalled.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from tui.screen import _normalize_ws, _run, _screen_text, _settle

REPO = Path(__file__).resolve().parents[2]

SIGNAL_CHILD = """\
import json, os, signal, sys, time
ready, log, launches, cleanup, advice = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4]), sys.argv[5]
got = []

def handle(signum, frame):
    name = signal.Signals(signum).name
    with open(log, "a", encoding="utf-8") as out:
        out.write(name + "\\n")
    got.append(name)

for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
    signal.signal(sig, handle)
with open(launches, "a", encoding="utf-8") as out:
    out.write(json.dumps(sys.argv[6:]) + "\\n")
def emit(record):
    sys.stdout.write(json.dumps(record) + "\\n")
    sys.stdout.flush()
base = {"schema_version": 1, "total": 3}
emit({**base, "kind": "run-started"})
emit({**base, "kind": "proxy-started", "index": 1, "name": "alpha"})
with open(ready, "a", encoding="utf-8") as out:
    out.write(str(os.getpid()) + "\\n")
while not got:
    time.sleep(0.02)
deadline = time.monotonic() + cleanup
while time.monotonic() < deadline and "SIGTERM" not in got:
    time.sleep(0.02)
code = 143 if "SIGTERM" in got else 130
emit({"schema_version": 1, "kind": "stopped", "exit_code": code, "signal": got[0], "advice": advice,
      "message": "a2m migrate: interrupted; " + advice})
sys.exit(code)
"""

# Runs the real app headless with one run screen, and writes the app's exit code to a file once it exits.
WRAPPER = """\
import asyncio, json, sys
from a2m.tui.app import A2MApp
from a2m.tui.run import RunScreen

done, grace, argv = sys.argv[1], float(sys.argv[2]), json.loads(sys.argv[3])

async def main():
    app = A2MApp()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await app.push_screen(RunScreen(argv, stop_grace=grace))
        while app.return_code is None:
            await asyncio.sleep(0.05)
    with open(done, "w", encoding="utf-8") as out:
        out.write(str(app.return_code))

asyncio.run(main())
"""

FORCE_ADVICE = "rerun with --force to redo every proxy"
RESUME_ADVICE = "rerun with --resume to continue"


def live(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace").rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state != "Z"


def kill_own(pids: Sequence[int]) -> None:
    """SIGKILL only PIDs a test itself recorded, if still alive."""
    for pid in pids:
        if live(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


@pytest.fixture
def started_pids() -> Iterator[list[int]]:
    pids: list[int] = []
    yield pids
    kill_own(pids)


class Child:
    """Paths and argv for the signal-logging child."""

    def __init__(self, tmp_path: Path, cleanup_seconds: float, advice: str) -> None:
        script = tmp_path / "signal_child.py"
        script.write_text(SIGNAL_CHILD, encoding="utf-8")
        self.ready = tmp_path / "ready"
        self.log = tmp_path / "signals.log"
        self.launches = tmp_path / "launches.log"
        self.argv = [
            sys.executable, str(script), str(self.ready), str(self.log), str(self.launches),
            str(cleanup_seconds), advice,
        ]

    def pids(self) -> list[int]:
        return [int(x) for x in self.ready.read_text(encoding="utf-8").split()] if self.ready.is_file() else []

    def signals(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").split() if self.log.is_file() else []

    def launch_args(self) -> list[str]:
        return self.launches.read_text(encoding="utf-8").splitlines() if self.launches.is_file() else []


def wait_for(predicate: object, *, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return
        time.sleep(0.05)
    raise AssertionError(f"{what} never happened in time")


async def pilot_wait(pilot: object, predicate: object, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return
        await pilot.pause(0.05)  # type: ignore[attr-defined]
    raise AssertionError("condition was never met in time")


def flat(app: object) -> str:
    return _normalize_ws(_screen_text(app)).lower()


async def stopped_with_force_advice(pilot: object, app: object, child: Child, started_pids: list[int]) -> None:
    """Start the run, stop it (confirmed), and wait for the stopped state offering Resume."""
    from a2m.tui.run import Phase, RunScreen

    await app.push_screen(RunScreen(child.argv))  # type: ignore[attr-defined]
    await _settle(pilot)
    await pilot_wait(pilot, lambda: bool(child.pids()))
    started_pids.extend(child.pids())
    await pilot_wait(pilot, lambda: "alpha" in flat(app))
    await pilot.press("s")  # type: ignore[attr-defined]
    await _settle(pilot)
    await pilot.click("#confirm-primary")  # type: ignore[attr-defined]
    await _settle(pilot)
    screen = app.screen_stack[-1]  # type: ignore[attr-defined]
    await pilot_wait(pilot, lambda: screen.phase is Phase.STOPPED)


def test_TUI_CP6_X06_resume_with_force_asks_first_and_cancel_runs_nothing(
    tmp_path: Path, started_pids: list[int]
) -> None:
    """[TUI-CP6-X06] When the stopped run's advice names --force, Resume opens a confirm naming that every
    proxy is redone and existing results are regenerated, with Cancel focused; Cancel (and Escape) start
    nothing and leave the screen stopped. The TUI's SIGTERM handler is installed only while the child runs
    and the previous one is back once it has exited."""
    child = Child(tmp_path, cleanup_seconds=0.2, advice=FORCE_ADVICE)
    before = signal.getsignal(signal.SIGTERM)

    async def body() -> None:
        from textual.widgets import Button

        from a2m.tui.app import A2MApp
        from a2m.tui.confirm import ConfirmScreen
        from a2m.tui.run import Phase, RunScreen

        app = A2MApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await app.push_screen(RunScreen(child.argv))
            await _settle(pilot)
            await pilot_wait(pilot, lambda: bool(child.pids()))
            started_pids.extend(child.pids())
            assert signal.getsignal(signal.SIGTERM) != before, "no SIGTERM handler while the child runs"
            await pilot_wait(pilot, lambda: "alpha" in flat(app))
            await pilot.press("s")
            await _settle(pilot)
            await pilot.click("#confirm-primary")
            await _settle(pilot)
            run = app.screen
            assert isinstance(run, RunScreen)
            await pilot_wait(pilot, lambda: run.phase is Phase.STOPPED)
            assert signal.getsignal(signal.SIGTERM) == before, "the previous SIGTERM handler was not restored"

            for dismiss in ("click", "escape"):
                await pilot.press("r")
                await _settle(pilot)
                dialog = app.screen
                assert isinstance(dialog, ConfirmScreen), "Resume with --force did not ask first"
                text = flat(app)
                assert _normalize_ws("--force").lower() in text
                assert "regenerated" in text
                focused = app.focused
                assert isinstance(focused, Button) and focused.id == "confirm-cancel"
                if dismiss == "click":
                    await pilot.click("#confirm-cancel")
                else:
                    await pilot.press("escape")
                await _settle(pilot)
                await pilot.pause(0.5)
                assert app.screen is run
                assert run.phase is Phase.STOPPED
                assert len(child.launch_args()) == 1, "Cancel must not start a run"

    _run(body)


def test_TUI_CP6_X07_resume_with_force_runs_with_force_once_confirmed(
    tmp_path: Path, started_pids: list[int]
) -> None:
    """[TUI-CP6-X07] Confirming the --force Resume dialog runs the same command again with --force."""
    child = Child(tmp_path, cleanup_seconds=0.2, advice=FORCE_ADVICE)

    async def body() -> None:
        from a2m.tui.app import A2MApp
        from a2m.tui.run import Phase, RunScreen

        app = A2MApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await stopped_with_force_advice(pilot, app, child, started_pids)
            await pilot.press("r")
            await _settle(pilot)
            await pilot.click("#confirm-primary")
            await _settle(pilot)
            await pilot_wait(pilot, lambda: len(child.pids()) == 2)
            started_pids.extend(child.pids()[1:])
            launches = child.launch_args()
            assert len(launches) == 2
            assert "--force" not in launches[0] and "--force" in launches[1]
            run = app.screen
            assert isinstance(run, RunScreen) and run.phase is Phase.RUNNING

    _run(body)


def start_wrapper(tmp_path: Path, child: Child, grace: float, started_pids: list[int]) -> tuple[subprocess.Popen[bytes], Path]:
    import json

    wrapper = tmp_path / "tui_wrapper.py"
    wrapper.write_text(WRAPPER, encoding="utf-8")
    done = tmp_path / "tui_exit_code"
    env = {k: v for k, v in os.environ.items() if k not in ("FORCE_COLOR", "PY_COLORS")}
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO), env.get("PYTHONPATH", "")]))
    proc = subprocess.Popen(
        [sys.executable, str(wrapper), str(done), str(grace), json.dumps(child.argv)],
        cwd=REPO, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    started_pids.append(proc.pid)
    return proc, done


def signal_the_tui(
    tmp_path: Path, started_pids: list[int], *, signum: signal.Signals, cleanup: float, grace: float
) -> tuple[Child, int, int]:
    """Run the TUI in a wrapper, send ``signum`` to the wrapper's pid once the child is up, and check the
    wrapper outlives the child. Returns the child, its pid, and the TUI's exit code."""
    child = Child(tmp_path, cleanup_seconds=cleanup, advice=RESUME_ADVICE)
    proc, done = start_wrapper(tmp_path, child, grace, started_pids)
    try:
        wait_for(lambda: bool(child.pids()) or proc.poll() is not None, timeout=30.0, what="the TUI starting its child")
        assert proc.poll() is None, proc.stderr.read().decode() if proc.stderr else ""
        pid = child.pids()[0]
        started_pids.append(pid)
        assert os.getsid(pid) != os.getsid(proc.pid), "the child should run in its own session"
        time.sleep(0.3)
        os.kill(proc.pid, signum)
        deadline = time.monotonic() + 30.0
        while live(pid):
            assert time.monotonic() < deadline, "the child never exited"
            assert proc.poll() is None, "the TUI exited while its child was still running"
            time.sleep(0.02)
        proc.wait(timeout=30.0)
    finally:
        if proc.poll() is None:
            kill_own([proc.pid])
            proc.wait(timeout=10.0)
        if proc.stderr is not None:
            proc.stderr.close()
    assert proc.returncode == 0, f"the TUI process died instead of exiting (returncode {proc.returncode})"
    return child, pid, int(done.read_text(encoding="utf-8"))


def test_TUI_CP6_X08_sigterm_to_the_tui_stops_the_child_with_one_sigint_and_exits_after_it(
    tmp_path: Path, started_pids: list[int]
) -> None:
    """[TUI-CP6-X08] A real SIGTERM sent to the TUI's own process while a run is going makes the child get
    exactly one SIGINT (and nothing else within the budget); the TUI stays up until the child has exited,
    then exits with 128 + SIGTERM. No process is left running."""
    child, pid, code = signal_the_tui(tmp_path, started_pids, signum=signal.SIGTERM, cleanup=1.5, grace=30.0)
    assert child.signals() == ["SIGINT"]
    assert code == 128 + signal.SIGTERM
    assert not live(pid)


def test_TUI_CP6_X09_sighup_to_the_tui_sends_sigterm_to_the_child_only_after_the_budget(
    tmp_path: Path, started_pids: list[int]
) -> None:
    """[TUI-CP6-X09] A SIGHUP to the TUI's process takes the same path; with a child still cleaning up when
    the (injected) budget is over, the TUI sends it SIGTERM (never SIGKILL) and exits once it has exited."""
    child, pid, code = signal_the_tui(tmp_path, started_pids, signum=signal.SIGHUP, cleanup=60.0, grace=1.0)
    assert child.signals() == ["SIGINT", "SIGTERM"]
    assert code == 128 + signal.SIGHUP
    assert not live(pid)
