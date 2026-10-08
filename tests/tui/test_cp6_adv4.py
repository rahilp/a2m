"""TUI CP6 adversarial round 4: every guarded signal ends ``a2m tui`` cleanly on any screen; leftovers are ended.

D1: SIGINT, SIGTERM and SIGHUP sent to the TUI's own process at any time (setup screen with no run, or a run
going) end the app through Textual's own shutdown: the terminal is restored (alternate screen left, cooked
mode back), one ``a2m tui: stopped by SIGNAME`` line, exit code 128 + the signal, and a running child is
stopped first through the safe stop path. The handlers live for the whole TUI and the previous ones come back
afterwards. A1: VerifyStage.close ends a leftover runtime recorded in the base's PID file before it removes
the base, when this run never started its own runtime.

The TUI runs for real in a pseudo-terminal, through the command line and the production ``run_app``. Every
PID a test starts is recorded and SIGKILLed at teardown only if still alive; no other process is signalled.
"""

from __future__ import annotations

import json
import os
import pty
import re
import select
import signal
import subprocess
import sys
import termios
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tui.test_cp6_signals import RESUME_ADVICE, Child, kill_own, live

REPO = Path(__file__).resolve().parents[2]
ANSI = re.compile(r"\x1b(\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(\x07|\x1b\\)|[PX^_][^\x1b]*\x1b\\|[@-Z\\-_])")
ENTER_ALT_SCREEN = b"\x1b[?1049h"
LEAVE_ALT_SCREEN = b"\x1b[?1049l"


@pytest.fixture
def own_pids() -> Iterator[list[int]]:
    pids: list[int] = []
    yield pids
    kill_own(pids)


def clean_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("FORCE_COLOR", "PY_COLORS")}
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO), env.get("PYTHONPATH", "")]))
    env["TERM"] = "xterm-256color"
    return env


class PtyRun:
    """A command run in a fresh pseudo-terminal, with everything it wrote to the terminal."""

    def __init__(self, argv: list[str], own_pids: list[int]) -> None:
        env = clean_env()
        pid, master = pty.fork()
        if pid == 0:  # the child: run the command in the pty
            try:
                os.chdir(REPO)
                os.execve(argv[0], argv, env)
            finally:
                os._exit(127)
        own_pids.append(pid)
        self.pid = pid
        self.master = master
        self.output = bytearray()
        self.status: int | None = None
        self.lflag_after: int | None = None

    def read_some(self, seconds: float) -> str:
        """Read what the pty has: 'data', 'idle' (nothing within ``seconds``) or 'eof'."""
        ready, _, _ = select.select([self.master], [], [], seconds)
        if not ready:
            return "idle"
        try:
            chunk = os.read(self.master, 65536)
        except OSError:
            return "eof"
        if not chunk:
            return "eof"
        self.output.extend(chunk)
        return "data"

    def wait_until(self, predicate: Callable[[], bool], *, timeout: float, what: str) -> None:
        deadline = time.monotonic() + timeout
        while not predicate():
            assert time.monotonic() < deadline, f"{what} never happened: {self.text()[-2000:]}"
            assert self.read_some(0.1) != "eof", f"the pty closed before {what}: {self.text()[-2000:]}"

    def wait_exit(self, *, timeout: float, while_waiting: Callable[[], None] = lambda: None) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.read_some(0.05)
            done, code = os.waitpid(self.pid, os.WNOHANG)
            if done:
                self.status = code
                break
            while_waiting()
        while self.read_some(0.2) == "data":  # what is left in the pty after the exit
            pass
        try:
            self.lflag_after = termios.tcgetattr(self.master)[3]
        except termios.error:
            self.lflag_after = None

    def close(self) -> None:
        os.close(self.master)

    def text(self) -> str:
        return ANSI.sub("", self.output.decode(errors="replace"))


def assert_ended_cleanly(run: PtyRun, signum: signal.Signals) -> None:
    """Exited (not killed) with 128 + signum, one line naming the signal, no traceback, terminal restored."""
    assert run.status is not None, f"a2m tui did not exit after {signum.name}"
    assert os.WIFEXITED(run.status), f"a2m tui died by signal {os.WTERMSIG(run.status)} instead of exiting"
    assert os.WEXITSTATUS(run.status) == 128 + signum
    text = run.text()
    assert "Traceback" not in text and "KeyboardInterrupt" not in text, text[-2000:]
    assert text.count("a2m tui: stopped by") == 1, text[-2000:]
    assert text.count(f"a2m tui: stopped by {signum.name}") == 1, text[-2000:]
    entered = run.output.rfind(ENTER_ALT_SCREEN)
    assert entered >= 0, "the TUI never entered the alternate screen"
    assert run.output.find(LEAVE_ALT_SCREEN, entered) > entered, "the alternate screen was never left"
    if run.lflag_after is not None:  # the line discipline is back in cooked mode with echo
        assert run.lflag_after & termios.ICANON and run.lflag_after & termios.ECHO


# ---------------------------------------------------------------- TUI-CP6-X16


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
def test_TUI_CP6_X16_sigterm_or_sighup_on_the_setup_screen_exits_cleanly(
    own_pids: list[int], signum: signal.Signals
) -> None:
    """[TUI-CP6-X16] A real ``python -m a2m tui`` in a pty, on the setup screen with no run going, sent
    SIGTERM or SIGHUP: it exits (not killed) with 128 + the signal, writes one 'a2m tui: stopped by SIGNAME'
    line, no traceback, and leaves the alternate screen with the terminal back in cooked mode."""
    run = PtyRun([sys.executable, "-m", "a2m", "tui"], own_pids)
    try:
        run.wait_until(lambda: b"Quit" in run.output, timeout=60.0, what="the setup screen being drawn")
        time.sleep(0.3)
        os.kill(run.pid, signum)
        run.wait_exit(timeout=30.0)
    finally:
        run.close()
    assert_ended_cleanly(run, signum)


# ---------------------------------------------------------------- TUI-CP6-X17

# The real command line and the real a2m.tui.app.run_app (in a pty); the app opens a run screen on start.
RUN_WRAPPER = """\
import json, sys
from a2m import cli
from a2m.tui import app as appmod
from a2m.tui.run import RunScreen

grace, argv = float(sys.argv[1]), json.loads(sys.argv[2])
mount = appmod.A2MApp.on_mount

def on_mount(self):
    mount(self)
    self.push_screen(RunScreen(argv, stop_grace=grace))

appmod.A2MApp.on_mount = on_mount
sys.exit(cli.main(["tui"]))
"""


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_TUI_CP6_X17_signal_during_a_run_through_the_real_run_app_stops_the_child_first(
    tmp_path: Path, own_pids: list[int], signum: signal.Signals
) -> None:
    """[TUI-CP6-X17] Through the production run_app (routed_interrupt with the run screen's guard on top), a
    signal to the TUI while a run is going gives the child exactly one SIGINT; the TUI stays up until the child
    has exited, then exits with 128 + the signal, one line, and the terminal restored."""
    child = Child(tmp_path, cleanup_seconds=1.5, advice=RESUME_ADVICE)
    wrapper = tmp_path / "run_wrapper.py"
    wrapper.write_text(RUN_WRAPPER, encoding="utf-8")
    run = PtyRun([sys.executable, str(wrapper), "30", json.dumps(child.argv)], own_pids)
    child_pid: list[int] = []

    def child_outlived_by_tui() -> None:
        if child_pid and live(child_pid[0]):
            assert live(run.pid), "the TUI exited while its child was still running"

    try:
        run.wait_until(lambda: bool(child.pids()), timeout=60.0, what="the run's child starting")
        child_pid.extend(child.pids())
        own_pids.extend(child_pid)
        run.wait_until(lambda: b"alpha" in run.output, timeout=30.0, what="the run screen showing the proxy")
        time.sleep(0.3)
        os.kill(run.pid, signum)
        run.wait_exit(timeout=40.0, while_waiting=child_outlived_by_tui)
    finally:
        run.close()
    assert child.signals() == ["SIGINT"]
    assert not live(child_pid[0])
    assert_ended_cleanly(run, signum)


# ---------------------------------------------------------------- TUI-CP6-X18


def test_TUI_CP6_X18_routed_handlers_cover_all_three_signals_and_are_restored(own_pids: list[int]) -> None:
    """[TUI-CP6-X18] routed_interrupt takes SIGINT, SIGTERM and SIGHUP for its block, leaves an ignored one
    ignored, gets them back from a run's TerminationGuard when it is removed, and puts the previous handlers
    back at the end. With no app to take it, SIGTERM ends a2m tui with 143 and one line (not a raw kill)."""
    import asyncio

    from a2m import cli
    from a2m.tui.signals import TerminationGuard, routed_interrupt

    def previous_handler(signum: int, frame: object) -> None:
        return None

    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    delivered: list[int] = []
    try:
        signal.signal(signal.SIGINT, previous_handler)
        signal.signal(signal.SIGTERM, previous_handler)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        with routed_interrupt(lambda signum: delivered.append(signum) is None):
            routed = signal.getsignal(signal.SIGTERM)
            assert routed is not previous_handler and signal.getsignal(signal.SIGINT) is routed
            assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN
            loop = asyncio.new_event_loop()
            try:
                guard = TerminationGuard(loop, lambda signum: None)
                guard.install()
                assert signal.getsignal(signal.SIGTERM) is not routed
                guard.remove()
            finally:
                loop.close()
            assert signal.getsignal(signal.SIGTERM) is routed
            os.kill(os.getpid(), signal.SIGTERM)
            deadline = time.monotonic() + 5.0
            while not delivered and time.monotonic() < deadline:
                time.sleep(0.01)
            assert delivered == [signal.SIGTERM]
        assert signal.getsignal(signal.SIGINT) is previous_handler
        assert signal.getsignal(signal.SIGTERM) is previous_handler
        assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN
    finally:
        for sig, old in saved.items():
            signal.signal(sig, old)  # type: ignore[arg-type]

    lines: list[str] = []

    def app_not_up(*, at_terminal: Callable[[], bool]) -> int:
        with routed_interrupt(lambda signum: False):
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(1.0)
        return 0

    original_say = cli._say
    cli._say = lambda text, err=False: lines.append(text)  # type: ignore[assignment]
    try:
        code = cli._open_tui(app_not_up)
    finally:
        cli._say = original_say  # type: ignore[assignment]
    assert code == 128 + signal.SIGTERM
    assert lines == ["a2m tui: stopped by SIGTERM"]
    assert signal.getsignal(signal.SIGTERM) == saved[signal.SIGTERM]


# ---------------------------------------------------------------- TUI-CP6-X19


def test_TUI_CP6_X19_close_ends_a_recorded_leftover_runtime_before_removing_the_base(
    tmp_path: Path, own_pids: list[int]
) -> None:
    """[TUI-CP6-X19] A run whose own runtime never started: VerifyStage.close ends the leftover runtime an
    earlier a2m recorded in the base's PID file (a synthetic process whose command line names that MULE_BASE, as
    the JVM a2m starts does) before it removes the base, instead of deleting the only record of a runtime that is
    still running."""
    from a2m import layout
    from a2m.verify.harness import VerifyStage
    from a2m.verify.mule import PID_FILE
    from a2m.verify.runner import MuleAppRunner

    results = tmp_path / "results"
    base = layout.mule_base_dir(results).absolute()
    base.mkdir(parents=True)
    leftover = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)", f"-Dmule.base={base}"],
        env=clean_env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    own_pids.append(leftover.pid)
    try:
        (base / PID_FILE).write_text(json.dumps({"pgid": leftover.pid, "pids": [leftover.pid]}) + "\n", encoding="utf-8")
        stage = VerifyStage()
        stage._real = MuleAppRunner(tmp_path / "no-mule-home", base)
        stage._results_root = results
        stage.close()
        assert leftover.wait(timeout=30.0) is not None
    finally:
        if leftover.poll() is None:
            kill_own([leftover.pid])
            leftover.wait(timeout=10.0)
    assert leftover.returncode == -signal.SIGTERM
    assert not base.exists()
