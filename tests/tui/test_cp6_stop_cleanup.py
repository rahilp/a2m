"""TUI CP6 adversarial round 1 (findings A1, X1): Stop and Quit wait for a2m's own clean-up.

a2m's own clean-up after Ctrl-C can take far longer than the old 15 s grace (stopping a Mule runtime it
started is budgeted at ``a2m.verify.runner.STOP_TIMEOUT``), and a SIGTERM sent meanwhile cuts that clean-up
short. These cases drive the real ``RunScreen`` on the real ``A2MApp`` against a small real child (written
to ``tmp_path`` below) that, after its first SIGINT, keeps "cleaning up" for a set time before it prints a
``stopped`` event and exits; it logs every signal it receives, so a test can tell exactly what was sent.
The clean-up budget is injected (``RunScreen(..., stop_grace=...)``) so most cases run in seconds.

Every PID a test starts is recorded and SIGKILLed at teardown only if still alive; no other process is
ever signalled.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

from tui.screen import _normalize_ws, _run, _screen_text, _settle

CLEANUP_CHILD = """\
import json, os, signal, sys, time
ready, log, cleanup = sys.argv[1], sys.argv[2], float(sys.argv[3])
got = []

def handle(signum, frame):
    name = signal.Signals(signum).name
    with open(log, "a", encoding="utf-8") as out:
        out.write(name + "\\n")
    got.append(name)

signal.signal(signal.SIGINT, handle)
signal.signal(signal.SIGTERM, handle)
def emit(record):
    sys.stdout.write(json.dumps(record) + "\\n")
    sys.stdout.flush()
base = {"schema_version": 1, "total": 3}
emit({**base, "kind": "run-started"})
emit({**base, "kind": "proxy-started", "index": 1, "name": "alpha"})
with open(ready, "w", encoding="utf-8") as out:
    out.write(str(os.getpid()))
while not got:
    time.sleep(0.02)
deadline = time.monotonic() + cleanup
while time.monotonic() < deadline and "SIGTERM" not in got:
    time.sleep(0.02)
code = 143 if "SIGTERM" in got else 130
advice = "rerun with --resume to continue"
emit({"schema_version": 1, "kind": "stopped", "exit_code": code, "signal": got[-1], "advice": advice,
      "message": "a2m migrate: interrupted; " + advice})
sys.exit(code)
"""


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
def started_pids() -> list[int]:
    pids: list[int] = []
    yield pids
    kill_own(pids)


class Child:
    """Paths and argv for one cleanup child."""

    def __init__(self, tmp_path: Path, cleanup_seconds: float) -> None:
        script = tmp_path / "cleanup_child.py"
        script.write_text(CLEANUP_CHILD, encoding="utf-8")
        self.ready = tmp_path / "ready"
        self.log = tmp_path / "signals.log"
        self.argv = [sys.executable, str(script), str(self.ready), str(self.log), str(cleanup_seconds)]

    def signals(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").split() if self.log.is_file() else []


async def wait_pid(pilot: object, ready: Path, timeout: float = 10.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready.is_file() and ready.read_text(encoding="utf-8").strip():
            return int(ready.read_text(encoding="utf-8").strip())
        await pilot.pause(0.05)  # type: ignore[attr-defined]
    raise AssertionError("the run screen never started its child")


async def wait_until(pilot: object, predicate: object, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return
        await pilot.pause(0.05)  # type: ignore[attr-defined]
    raise AssertionError("condition was never met in time")


def flat(app: object) -> str:
    return _normalize_ws(_screen_text(app)).lower()


STOPPING = _normalize_ws("Stopping: waiting for a2m to finish cleaning up").lower()


async def confirm(pilot: object, key: str) -> None:
    await pilot.press(key)  # type: ignore[attr-defined]
    await _settle(pilot)
    await pilot.click("#confirm-primary")  # type: ignore[attr-defined]
    await _settle(pilot)


def test_TUI_CP6_X01_default_budget_outlasts_runtime_stop_and_never_sends_sigterm(
    tmp_path: Path, started_pids: list[int]
) -> None:
    """[TUI-CP6-X01] With the default budget, a child whose clean-up takes longer than the old 15 s grace
    gets exactly one SIGINT and nothing else, the screen shows the stopping state the whole time with no
    Force stop on offer, and the run ends stopped once the child exits on its own. The default budget is
    longer than a2m's own Mule runtime stop budget."""
    from a2m.verify.runner import STOP_TIMEOUT

    child = Child(tmp_path, cleanup_seconds=17.0)

    async def body() -> None:
        from textual.widgets import Button

        from a2m.tui.app import A2MApp
        from a2m.tui.run import STOP_GRACE_SECONDS, RunScreen

        assert STOP_GRACE_SECONDS > STOP_TIMEOUT
        app = A2MApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await app.push_screen(RunScreen(child.argv))
            await _settle(pilot)
            pid = await wait_pid(pilot, child.ready)
            started_pids.append(pid)
            await wait_until(pilot, lambda: "alpha" in flat(app))

            await confirm(pilot, "s")
            await wait_until(pilot, lambda: STOPPING in flat(app))
            while live(pid):
                assert child.signals() in ([], ["SIGINT"]), child.signals()
                assert not app.screen.query_one("#force-stop", Button).display
                await pilot.pause(0.25)
            assert child.signals() == ["SIGINT"]
            await wait_until(pilot, lambda: "resume" in flat(app))

    _run(body)


def test_TUI_CP6_X02_force_stop_is_offered_after_the_budget_and_sends_sigterm_only_when_pressed(
    tmp_path: Path, started_pids: list[int]
) -> None:
    """[TUI-CP6-X02] Once the (injected) budget is over and the child is still cleaning up, the screen
    offers Force stop with a warning naming --force, still sends nothing on its own, and sends SIGTERM only
    when Force stop is pressed; the child then exits and the run ends stopped."""
    child = Child(tmp_path, cleanup_seconds=60.0)

    async def body() -> None:
        from textual.widgets import Button

        from a2m.tui.app import A2MApp
        from a2m.tui.run import RunScreen

        app = A2MApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await app.push_screen(RunScreen(child.argv, stop_grace=1.0))
            await _settle(pilot)
            pid = await wait_pid(pilot, child.ready)
            started_pids.append(pid)
            await wait_until(pilot, lambda: "alpha" in flat(app))

            await confirm(pilot, "s")
            await wait_until(pilot, lambda: STOPPING in flat(app))
            assert not app.screen.query_one("#force-stop", Button).display

            await wait_until(pilot, lambda: app.screen.query_one("#force-stop", Button).display, timeout=5.0)
            assert "--force" in flat(app)
            await pilot.pause(1.5)
            assert live(pid)
            assert child.signals() == ["SIGINT"], "nothing more may be sent without Force stop being pressed"

            await pilot.press("f")
            await wait_until(pilot, lambda: not live(pid), timeout=10.0)
            assert child.signals() == ["SIGINT", "SIGTERM"]
            await wait_until(pilot, lambda: "resume" in flat(app))

    _run(body)


def test_TUI_CP6_X03_ctrl_q_mid_run_stays_open_stopping_until_the_child_exits(
    tmp_path: Path, started_pids: list[int]
) -> None:
    """[TUI-CP6-X03] ctrl+q while a run is going asks first; once confirmed the app stays open on the
    stopping state past the (injected) budget, sends no SIGTERM, and exits only after the child has
    exited on its own."""
    child = Child(tmp_path, cleanup_seconds=3.0)

    async def body() -> None:
        from a2m.tui.app import A2MApp
        from a2m.tui.run import RunScreen

        app = A2MApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await app.push_screen(RunScreen(child.argv, stop_grace=1.0))
            await _settle(pilot)
            pid = await wait_pid(pilot, child.ready)
            started_pids.append(pid)
            await wait_until(pilot, lambda: "alpha" in flat(app))

            await confirm(pilot, "ctrl+q")
            await wait_until(pilot, lambda: STOPPING in flat(app))
            while live(pid):
                assert app.return_code is None, "the app must not exit while its child is alive"
                assert child.signals() in ([], ["SIGINT"]), child.signals()
                await pilot.pause(0.1)
            await wait_until(pilot, lambda: app.return_code is not None, timeout=10.0)
            assert child.signals() == ["SIGINT"]

    _run(body)
    assert not live(int(child.ready.read_text(encoding="utf-8")))


def test_TUI_CP6_X04_an_exit_request_mid_run_goes_through_the_same_stop_path(
    tmp_path: Path, started_pids: list[int]
) -> None:
    """[TUI-CP6-X04] A programmatic exit request while a run is going (app.exit()) does not exit: it stops
    the run with one SIGINT, shows the stopping state, and the app exits once the child has exited."""
    child = Child(tmp_path, cleanup_seconds=2.0)

    async def body() -> None:
        from a2m.tui.app import A2MApp
        from a2m.tui.run import RunScreen

        app = A2MApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await app.push_screen(RunScreen(child.argv, stop_grace=30.0))
            await _settle(pilot)
            pid = await wait_pid(pilot, child.ready)
            started_pids.append(pid)
            await wait_until(pilot, lambda: "alpha" in flat(app))

            app.exit()
            await wait_until(pilot, lambda: STOPPING in flat(app))
            assert app.return_code is None
            await wait_until(pilot, lambda: app.return_code is not None, timeout=10.0)
            assert not live(pid)
            assert child.signals() == ["SIGINT"]

    _run(body)


def test_TUI_CP6_X05_teardown_waits_for_the_child_without_blocking_the_event_loop(
    tmp_path: Path, started_pids: list[int]
) -> None:
    """[TUI-CP6-X05] When the app is torn down with a run going (the test harness closing it), teardown
    sends one SIGINT, does not finish while the child is alive, sends no SIGTERM within the budget, and
    keeps the event loop running while it waits."""
    child = Child(tmp_path, cleanup_seconds=2.5)
    ticks: list[float] = []
    pid_box: list[int] = []

    async def body() -> None:
        from a2m.tui.app import A2MApp
        from a2m.tui.run import RunScreen

        async def tick() -> None:
            while True:
                ticks.append(time.monotonic())
                await asyncio.sleep(0.05)

        app = A2MApp()
        ticker: asyncio.Task[None] | None = None
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await app.push_screen(RunScreen(child.argv, stop_grace=30.0))
            await _settle(pilot)
            pid = await wait_pid(pilot, child.ready)
            started_pids.append(pid)
            pid_box.append(pid)
            await wait_until(pilot, lambda: "alpha" in flat(app))
            ticker = asyncio.get_running_loop().create_task(tick())
            ticks.clear()
        closed_at = time.monotonic()
        assert not live(pid), "teardown finished while the child was still alive"
        assert ticker is not None
        ticker.cancel()
        during = [t for t in ticks if t <= closed_at]
        assert len(during) >= 10, f"the event loop was blocked during teardown ({len(during)} ticks)"

    _run(body)
    assert child.signals() == ["SIGINT"]
    assert not live(pid_box[0])
