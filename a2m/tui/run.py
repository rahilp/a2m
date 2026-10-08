"""The run screen: watch a migration run, stop it safely, and resume it.

The run is the setup screen's own ``a2m migrate`` command, started as a child process with
``--progress json`` (:mod:`a2m.tui.child`). The screen shows the overall progress bar with "N of M
proxies" (N is the proxy being worked on, or the last one finished), the elapsed time (read from a clock
on a timer, so it moves between events), the proxy running now and its step, and the finished proxies,
newest first, each with its bucket marker.

Stop asks first, then sends SIGINT once, the signal a2m stops cleanly on: finished proxies keep their
results and ``.done`` marker. a2m then cleans up on its own (stopping a Mule runtime it started can take up
to :data:`a2m.verify.runner.STOP_TIMEOUT` and more), and the screen waits for it, saying so, for
:data:`STOP_GRACE_SECONDS`, never signalling it again in that time. Only once that budget is over does it
offer Force stop, with a warning, which sends SIGTERM when pressed; the screen never sends SIGKILL. The
stopped screen says how many proxies finished and are kept and offers Resume, which runs the same command
again with the flag the stopped run's own advice names (``--resume``, or ``--force`` when the run stopped
before an earlier run's markers were cleared; that redoes every proxy, so Resume asks first). Quitting the app while a run is going (q, ctrl+q, or any
other request to exit) asks first where a person asked, stops the run the same way, keeps the app open on
the stopping state, and quits only once the child has exited, so no migration (or Mule runtime it started)
is left running. A SIGINT, SIGTERM or SIGHUP sent to the TUI's own process while a run is going takes the same
path with no question asked (there is no one to ask): one SIGINT, the same clean-up budget, then SIGTERM to
the child only once that budget is over, and the TUI exits once the child has exited
(:mod:`a2m.tui.signals`). Every wait is asynchronous: the screen keeps drawing and answering keys meanwhile.

A run that cannot start (a usage error: exit code 2 and one stderr line, no events) shows a2m's own line
unchanged with a way back to setup and no progress bar. Lines the screen does not recognise are skipped
with one warning; they never stop the screen. Proxy names and messages are shown as plain text.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

from rich.console import Console, ConsoleOptions
from rich.console import RenderResult as RichRenderResult
from rich.style import Style as RichStyle
from rich.text import Text
from textual.app import ComposeResult, ScreenError
from textual.binding import ActiveBinding, Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.message import Message
from textual.renderables.bar import Bar as BarRenderable
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import Button, ListItem, ListView, ProgressBar, Static

from a2m.layout import NEEDS_REVIEW_DIR_NAME, UNSUPPORTED_DIR_NAME, VERIFIED_DIR_NAME
from a2m.progress import EventKind, Outcome, activity
from a2m.tui.child import FORCE_FLAG, ChildOutput, ChildProcess, Exited, StreamEvent, Unrecognised, resume_argv
from a2m.tui.confirm import ConfirmScreen
from a2m.tui.frame import AppFooter, AppHeader, EdgeButton, global_keys_first
from a2m.tui.signals import TerminationGuard
from a2m.verify.runner import STOP_TIMEOUT as RUNTIME_STOP_SECONDS

# a2m's own worst case to clean up after Ctrl-C is dominated by stopping the Mule runtime it started
# (runner.STOP_TIMEOUT, then a forced stop and reap); the margin covers ending the step it was in (a build
# subprocess is killed), reaping the runtime and writing the run log and the stopped event.
STOP_MARGIN_SECONDS = 60.0
# How long the screen waits after SIGINT, sending nothing more, before it offers Force stop.
STOP_GRACE_SECONDS = RUNTIME_STOP_SECONDS + STOP_MARGIN_SECONDS
# How often an asynchronous wait for the child checks whether it has exited.
STOP_POLL_SECONDS = 0.1
# How often the elapsed time is redrawn.
ELAPSED_REFRESH_SECONDS = 0.5

# Bucket marker per bucket (DESIGN.md Color, Bucket colors): symbol, CSS class.
BUCKET_MARKS: dict[str, tuple[str, str]] = {
    VERIFIED_DIR_NAME: ("✓", "bucket-verified"),
    NEEDS_REVIEW_DIR_NAME: ("!", "bucket-needs-review"),
    UNSUPPORTED_DIR_NAME: ("✗", "bucket-unsupported"),
}
FAILED_MARK = ("✗", "bucket-failed")
FAILED_LABEL = "failed"

FINISHED_LABEL = "Finished proxies"
NONE_YET = "None yet"
STOP_TITLE = "Stop now?"
STOP_LINES = (
    "Finished proxies stay in the results folder.",
    "The proxy running now is redone on Resume.",
)
QUIT_TITLE = "Quit while a run is going?"
QUIT_LINES = (
    "The run is stopped first.",
    "Finished proxies stay in the results folder.",
)
QUIT_PURPOSE = "quit"
FORCE_RESUME_TITLE = "Resume with --force?"
FORCE_RESUME_LINES = (
    "This run can only continue with --force: every proxy is redone.",
    "Existing results in the results folder are regenerated.",
)
FORCE_RESUME_ACTION = "Redo all"
FORCE_RESUME_PURPOSE = "force-resume"
RESUME_NOTE = "Resume skips the done ones and checks the rest again."
STOPPING_TEXT = "Stopping: waiting for a2m to finish cleaning up…"
STOPPING_QUIT_TEXT = "Stopping: waiting for a2m to finish cleaning up; the TUI quits once it has stopped…"
STILL_STOPPING_TEXT = "Stopping: a2m is still cleaning up after {elapsed}. Keep waiting, or press Force stop."
FORCE_WARNING = "Force stop sends SIGTERM: the results folder may then need --force on the next run."
FORCED_TEXT = "Force stop sent SIGTERM; waiting for a2m to stop…"
UNRECOGNISED_WARNING = "Skipped a progress line from a2m that the TUI does not recognise."


class Phase(StrEnum):
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FINISHED = "finished"
    START_FAILED = "start-failed"
    ENDED = "ended"


def format_elapsed(seconds: float) -> str:
    """``m:ss`` (``h:mm:ss`` from an hour on)."""
    whole = max(0, int(seconds))
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _exit_is_a_stop(returncode: int) -> bool:
    """True for the exit a stop ends in: killed by a signal, or a2m's own 128 + signal exit."""
    return returncode < 0 or returncode > 128


class BlockBar(BarRenderable):
    """The bar as solid, full-height cells (DESIGN.md Components, ProgressBar): the fill in the bar's color
    over a solid track in its background color, rounded to whole cells, with no thin-line glyphs or
    half-cell end caps."""

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RichRenderResult:
        fill = console.get_style(self.highlight_style).color
        track = console.get_style(self.background_style).color
        width = self.width or options.max_width
        start, end = self.highlight_range
        first = min(width, max(0, int(start + 0.5)))
        last = min(width, max(first, int(end + 0.5)))
        bar = Text(end="")
        bar.append(" " * first, style=RichStyle(bgcolor=track))
        bar.append(" " * (last - first), style=RichStyle(bgcolor=fill))
        bar.append(" " * (width - last), style=RichStyle(bgcolor=track))
        yield bar


class BlockProgressBar(ProgressBar):
    """A ProgressBar drawn with :class:`BlockBar`."""

    BAR_RENDERABLE = BlockBar


class RunScreen(Screen[None]):
    """Run ``argv`` (an ``a2m migrate ... --progress json`` command) and show its progress."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("s", "stop", "Stop"),
        Binding("f", "force_stop", "Force stop"),
        Binding("r", "resume", "Resume"),
    ]
    AUTO_FOCUS = ""  # "" focuses nothing, so the screen opens as the prototype shows it

    class ChildOutputArrived(Message):
        """One item from the child's reader thread, handed to the screen on the app's event loop."""

        def __init__(self, generation: int, item: ChildOutput) -> None:
            super().__init__()
            self.generation = generation
            self.item = item

    class TerminationSignalled(Message):
        """SIGINT, SIGTERM or SIGHUP reached the TUI's own process (see :mod:`a2m.tui.signals`)."""

        def __init__(self, signum: int) -> None:
            super().__init__()
            self.signum = signum

    def __init__(
        self,
        argv: Sequence[str],
        *,
        clock: Callable[[], float] | None = None,
        stop_grace: float = STOP_GRACE_SECONDS,
        results: Path | None = None,
    ) -> None:
        """``stop_grace`` is how long a stop waits after SIGINT before Force stop is offered (default
        :data:`STOP_GRACE_SECONDS`). ``results`` is the run's results folder: when given, a run that finishes
        closes this screen and names that folder in :attr:`handed_over`, for its results screen."""
        super().__init__()
        self._results = results
        self._handed_over: Path | None = None
        self._argv = list(argv)
        self._clock = clock or time.monotonic
        self._stop_grace = stop_grace
        self._child: ChildProcess | None = None
        self._generation = 0
        self._phase = Phase.RUNNING
        self._started_at = 0.0
        self._ended_at: float | None = None
        self._grace_timer: Timer | None = None
        self._stop_sent_at: float | None = None
        self._quit_after_stop = False
        self._terminated_by: int | None = None
        self._guard: TerminationGuard | None = None
        self._reset_run_state()

    def _reset_run_state(self) -> None:
        self._seen_start = False
        self._total = 0
        self._position = 0
        self._processed = 0
        self._kept = 0
        self._refused = 0
        self._failed = 0
        self._stopped: StreamEvent | None = None
        self._finished: StreamEvent | None = None
        self._error: StreamEvent | None = None
        self._stop_requested = False
        self._force_offered = False
        self._forced = False
        self._warned = False  # one unrecognised-line warning per run, a resumed run included

    # ------------------------------------------------------------------ public state

    @property
    def phase(self) -> Phase:
        return self._phase

    @property
    def handed_over(self) -> Path | None:
        """The results folder of a run that finished and closed this screen to show its results, else None."""
        return self._handed_over

    @property
    def running(self) -> bool:
        """True while the child process is alive (running or still stopping)."""
        return self._child is not None and self._child.alive

    @property
    def active_bindings(self) -> dict[str, ActiveBinding]:
        """The keys in reach, global keys (Quit, Help) first so the footer lists them first (DESIGN.md)."""
        return global_keys_first(self.app, super().active_bindings)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "stop":
            return self._phase is Phase.RUNNING
        if action == "force_stop":
            return self._force_available
        if action == "resume":
            return self._phase is Phase.STOPPED
        return True

    @property
    def _force_available(self) -> bool:
        return self._phase is Phase.STOPPING and self._force_offered and not self._forced

    # ------------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        yield AppHeader(id="hdr")
        with Vertical(id="body"):
            with Horizontal(id="run-bar-row"):
                yield BlockProgressBar(id="run-progress", show_percentage=False, show_eta=False)
                yield Static("", id="run-counts", markup=False)
                yield Static("", id="run-elapsed", markup=False)
            yield Static("", id="run-current", classes="gap-1", markup=False)
            yield Static(FORCE_WARNING, id="run-force-note", markup=False)
            yield Static("", id="run-message", classes="gap-1", markup=False)
            yield Static(FINISHED_LABEL, id="run-finished-label", classes="section-label gap-1", markup=False)
            yield Static(NONE_YET, id="run-finished-empty", markup=False)
            yield ListView(id="run-finished")
            with Horizontal(id="run-buttons", classes="gap-2"):
                yield EdgeButton("Stop", id="stop", variant="error", compact=True)
                yield EdgeButton("Force stop", id="force-stop", variant="error", compact=True)
                yield EdgeButton("Resume", id="resume", variant="primary", compact=True)
                yield EdgeButton("Back to setup", id="back-to-setup", compact=True)
        yield AppFooter(id="ftr", compact=True, show_command_palette=False)

    def on_mount(self) -> None:
        self.set_interval(ELAPSED_REFRESH_SECONDS, self._show_elapsed)
        self._launch(self._argv)

    async def on_unmount(self) -> None:
        """Last line of defence when the app closes some other way (an unhandled error): the app does not
        finish closing while the child is alive. The child gets the same single SIGINT and the same
        clean-up budget, waited for asynchronously so the event loop keeps running. With no screen left to
        offer Force stop on, a child still running once the budget is over gets SIGTERM (a2m handles it like
        Ctrl-C) and is then waited for until it exits; never SIGKILL."""
        try:
            await self._stop_child_before_closing()
        finally:
            self._remove_guard()

    async def _stop_child_before_closing(self) -> None:
        child = self._child
        if child is None or not child.alive:
            return
        if self._stop_sent_at is None and child.interrupt():
            self._stop_sent_at = time.monotonic()
        started = self._stop_sent_at if self._stop_sent_at is not None else time.monotonic()
        while child.alive and time.monotonic() - started < self._stop_grace:
            await asyncio.sleep(STOP_POLL_SECONDS)
        if child.alive and not self._forced:
            self._forced = child.terminate()
        while child.alive:
            await asyncio.sleep(STOP_POLL_SECONDS)

    # ------------------------------------------------------------------ the child

    def _launch(self, argv: list[str]) -> None:
        self._argv = argv
        self._generation += 1
        generation = self._generation
        self._reset_run_state()
        self._phase = Phase.RUNNING
        self._started_at = self._clock()
        self._ended_at = None
        self._stop_sent_at = None
        self.query_one("#run-finished", ListView).clear()
        loop = asyncio.get_running_loop()

        def deliver(item: ChildOutput) -> None:
            try:
                loop.call_soon_threadsafe(self.post_message, self.ChildOutputArrived(generation, item))
            except RuntimeError:
                pass  # the app has closed; on_unmount has already stopped the child

        child = ChildProcess(argv, deliver)
        self._child = child
        try:
            child.start()
        except OSError as exc:
            self._child = None
            self._end(Phase.START_FAILED, f"✗ could not start a2m: {exc}")
            return
        self._install_guard(loop)
        self._draw()

    def _install_guard(self, loop: asyncio.AbstractEventLoop) -> None:
        """SIGINT, SIGTERM and SIGHUP on the TUI's own process stop the child first, for as long as it runs."""
        if self._guard is None:
            # Handled as a message on this screen, so it runs in the screen's own context (timers it sets work).
            self._guard = TerminationGuard(loop, lambda signum: self.post_message(self.TerminationSignalled(signum)))
        self._guard.install()

    def _remove_guard(self) -> None:
        if self._guard is not None:
            self._guard.remove()

    def on_run_screen_child_output_arrived(self, message: ChildOutputArrived) -> None:
        message.stop()
        if message.generation != self._generation:
            return
        item = message.item
        if isinstance(item, StreamEvent):
            self._apply_event(item)
        elif isinstance(item, Unrecognised):
            if not self._warned:
                self._warned = True
                self.notify(UNRECOGNISED_WARNING, severity="warning", markup=False)
        elif isinstance(item, Exited):
            self._child_exited(item)

    def _apply_event(self, item: StreamEvent) -> None:
        event = item.event
        kind = item.kind
        if kind is EventKind.RUN_STARTED:
            self._seen_start = True
            self._total = event.total
        elif kind in (EventKind.PROXY_STARTED, EventKind.STEP):
            self._total = max(self._total, event.total)
            self._position = max(self._position, event.index)
            if self._phase is Phase.RUNNING:
                self._show_current("Running: ", event.name, activity(event))
        elif kind is EventKind.PROXY_FINISHED:
            self._total = max(self._total, event.total)
            self._position = max(self._position, event.index)
            self._processed += 1
            if event.outcome in (Outcome.FINISHED, Outcome.SKIPPED):
                self._kept += 1
            elif event.outcome is Outcome.REFUSED:
                self._refused += 1
            else:
                self._failed += 1
            self._add_finished_row(event.name, event.bucket, event.outcome)
            if self._phase is Phase.RUNNING:
                self._show_current("", event.name, activity(event))
        elif kind is EventKind.RUN_FINISHED:
            self._finished = item
        elif kind is EventKind.STOPPED:
            self._stopped = item
        elif kind is EventKind.ERROR:
            self._error = item
        self._draw()

    def _add_finished_row(self, name: str, bucket: str | None, outcome: Outcome | None) -> None:
        symbol, css = BUCKET_MARKS.get(bucket or "", FAILED_MARK)
        text = f"{symbol} {name}"
        if bucket is None:
            text += f" ({FAILED_LABEL if outcome is Outcome.FAILED else (outcome or FAILED_LABEL)})"
        row = ListItem(Static(text, classes=css, markup=False))
        self.query_one("#run-finished", ListView).insert(0, [row])

    def _child_exited(self, exited: Exited) -> None:
        self._remove_guard()
        if self._grace_timer is not None:
            self._grace_timer.stop()
            self._grace_timer = None
        # An error a2m reported wins over a Stop pressed meanwhile: a crash is never shown as a clean stop.
        # Without a 'stopped' event, a requested stop counts only when the exit looks like one (a signal).
        if self._error is not None:
            self._end(Phase.ENDED, f"✗ {self._error.message}")
        elif self._stopped is not None or (
            self._stop_requested and self._finished is None and _exit_is_a_stop(exited.returncode)
        ):
            self._end(Phase.STOPPED, self._stopped_text())
        elif self._finished is not None:
            self._end(Phase.FINISHED, self._finished_text(self._finished))
        elif not self._seen_start:
            line = exited.stderr[-1] if exited.stderr else f"a2m exited with code {exited.returncode} before the run started"
            self._end(Phase.START_FAILED, f"✗ {line}")
        else:
            line = exited.stderr[-1] if exited.stderr else ""
            ended = f"✗ a2m ended before the run finished (exit code {exited.returncode})"
            self._end(Phase.ENDED, f"{ended}: {line}" if line else ended)
        if self._quit_after_stop:
            self.app.exit(return_code=self._quit_code())
        elif self._phase is Phase.FINISHED and self._results is not None:
            self._hand_over(self._results)

    def _hand_over(self, results: Path) -> None:
        """The run finished: close this screen (and any dialog over it) so its results screen opens."""
        if not self.is_attached:
            return
        try:
            self.pop_until_active()
        except ScreenError:
            return  # not on the app's screen stack any more; nothing to hand over to
        self._handed_over = results
        self.dismiss(None)

    def _quit_code(self) -> int:
        """The TUI's exit code when it quits after a stop: 128 + the signal when a signal ended it, else 0."""
        return 128 + self._terminated_by if self._terminated_by is not None else 0

    def _stopped_text(self) -> str:
        if not self._seen_start:
            return "Stopped before any proxy ran. Nothing was kept."
        # Only done proxies are kept for Resume: a refused or failed one leaves no .done marker, so Resume
        # checks it again. Saying "0 kept" next to a finished row read as a lost result.
        counts = ((self._kept, "done"), (self._refused, "unsupported"), (self._failed, "failed"))
        parts = ", ".join(f"{count} {label}" for count, label in counts if count)
        line = f"Stopped at {self._processed} of {self._total} proxies"
        line += f": {parts}." if parts else "."
        return f"{line} {RESUME_NOTE}"

    @staticmethod
    def _finished_text(item: StreamEvent) -> str:
        event = item.event
        return (
            f"Finished: {event.finished} done, {event.skipped} skipped as already done, "
            f"{event.refused} refused, {event.failed} failed."
        )

    def _end(self, phase: Phase, message: str) -> None:
        self._phase = phase
        if self._ended_at is None:
            self._ended_at = self._clock()
        self.query_one("#run-message", Static).update(message)
        self._draw()
        self.refresh_bindings()

    # ------------------------------------------------------------------ stop, quit, resume

    def action_stop(self) -> None:
        """Ask, then stop the run (see :meth:`stop_run`)."""
        if self._phase is not Phase.RUNNING or not self.running:
            return
        confirm = ConfirmScreen(STOP_TITLE, STOP_LINES, "Stop", tone="warning")
        self.app.push_screen(confirm, self._stop_confirmed)

    def _stop_confirmed(self, confirmed: bool | None) -> None:
        if confirmed:
            self.stop_run()

    def stop_run(self) -> None:
        """Send the child SIGINT once and wait for it to clean up and exit on its own; nothing more is sent
        automatically. Force stop is offered once the clean-up budget (``stop_grace``) is over."""
        child = self._child
        if child is None or self._phase is not Phase.RUNNING:
            return
        self._stop_requested = True
        if not child.interrupt():
            return  # it has already exited; its exit is on its way
        self._stop_sent_at = time.monotonic()
        self._phase = Phase.STOPPING
        self._grace_timer = self.set_timer(self._stop_grace, self._grace_over)
        self._show_stopping()

    def _grace_over(self) -> None:
        """The clean-up budget is over and the child is still running: offer Force stop (never sent here)."""
        self._grace_timer = None
        if self._phase is not Phase.STOPPING or not self.running:
            return
        self._force_offered = True
        if self._terminated_by is not None:
            self._terminate_child()  # the TUI itself is being terminated: no one is there to press Force stop
            return
        self._show_stopping()

    def action_force_stop(self) -> None:
        """Send SIGTERM, only once a stop has outlasted its clean-up budget and only when asked to."""
        if not self._force_available:
            return
        self._terminate_child()

    def _terminate_child(self) -> None:
        """Send the child SIGTERM once (a2m handles it like Ctrl-C); never SIGKILL."""
        child = self._child
        if child is not None and not self._forced and child.terminate():
            self._forced = True
            self._show_stopping()

    def _show_stopping(self) -> None:
        """The stopping state: what the screen is waiting for, and Force stop once it is offered."""
        if self._forced:
            text = FORCED_TEXT
        elif self._force_offered:
            started = self._stop_sent_at if self._stop_sent_at is not None else time.monotonic()
            text = STILL_STOPPING_TEXT.format(elapsed=format_elapsed(time.monotonic() - started))
        elif self._quit_after_stop:
            text = STOPPING_QUIT_TEXT
        else:
            text = STOPPING_TEXT
        self.query_one("#run-current", Static).update(text)
        self._draw()
        self.refresh_bindings()

    def quit_when_stopped(self) -> None:
        """Quit the app once the child has exited: stop the run if it is still going (one SIGINT), and keep
        the app open on the stopping state until then. The app quits at once when no child is alive."""
        self._quit_after_stop = True
        if not self.running:
            self.app.exit(return_code=self._quit_code())
        elif self._phase is Phase.RUNNING:
            self.stop_run()
        elif self._phase is Phase.STOPPING:
            self._show_stopping()

    def request_quit(self) -> bool:
        """The app's Quit while this screen holds a run: ask first, then stop the run and quit once the
        child has exited. Returns False when no run is going (the app quits at once)."""
        if not self.running:
            return False
        top = self.app.screen
        if isinstance(top, ConfirmScreen):
            if top.purpose == QUIT_PURPOSE:
                return True  # already asking
            top.dismiss(False)
        if self._phase is Phase.STOPPING:
            self.quit_when_stopped()  # the stop was confirmed already; quit once it is done
            return True
        confirm = ConfirmScreen(QUIT_TITLE, QUIT_LINES, "Stop and quit", tone="error", purpose=QUIT_PURPOSE)
        self.app.push_screen(confirm, self._quit_confirmed)
        return True

    def _quit_confirmed(self, confirmed: bool | None) -> None:
        if confirmed:
            self.quit_when_stopped()

    def on_run_screen_termination_signalled(self, message: TerminationSignalled) -> None:
        """SIGINT, SIGTERM or SIGHUP on the TUI's own process, handed over by the guard (see :mod:`a2m.tui.signals`).

        No one is there to ask, so this is a confirmed quit: an open confirm is cancelled, the child gets the
        same single SIGINT and the same clean-up budget, SIGTERM follows only once that budget is over, and
        the TUI exits once the child has exited. A repeat signal changes nothing."""
        message.stop()
        if self._terminated_by is None:
            self._terminated_by = message.signum
        if not self.is_attached:
            return
        top = self.app.screen
        if isinstance(top, ConfirmScreen):
            top.dismiss(False)
        self.quit_when_stopped()
        if self._phase is Phase.STOPPING and self._force_offered:
            self._terminate_child()  # the budget is already over

    def action_resume(self) -> None:
        """Run the same command again with the stopped run's advice (--resume, or --force). A --force rerun
        redoes every proxy, so it asks first (Cancel focused) and runs only once confirmed."""
        if self._phase is not Phase.STOPPED:
            return
        advice = self._stopped.advice if self._stopped is not None else ""
        argv = resume_argv(self._argv, advice)
        if FORCE_FLAG not in argv:
            self._resume(argv)
            return
        confirm = ConfirmScreen(
            FORCE_RESUME_TITLE, FORCE_RESUME_LINES, FORCE_RESUME_ACTION, tone="error", purpose=FORCE_RESUME_PURPOSE
        )

        def confirmed(answer: bool | None) -> None:
            if answer and self._phase is Phase.STOPPED:
                self._resume(argv)

        self.app.push_screen(confirm, confirmed)

    def _resume(self, argv: list[str]) -> None:
        self.query_one("#run-message", Static).update("")
        self._launch(argv)
        self.refresh_bindings()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "stop":
            self.action_stop()
        elif event.button.id == "force-stop":
            self.action_force_stop()
        elif event.button.id == "resume":
            self.action_resume()
        elif event.button.id == "back-to-setup" and not self.running:
            self.dismiss(None)

    # ------------------------------------------------------------------ drawing

    def _draw(self) -> None:
        phase = self._phase
        show_bar = self._seen_start and phase is not Phase.START_FAILED
        self.query_one("#run-bar-row").display = show_bar
        bar = self.query_one("#run-progress", ProgressBar)
        bar.set_class(phase is Phase.STOPPED, "-stopped")
        if phase is Phase.FINISHED:
            shown = self._total
        elif phase is Phase.STOPPED or phase is Phase.ENDED:
            shown = self._processed
        else:
            shown = self._position
        if show_bar:
            bar.update(total=max(self._total, 1), progress=min(shown, max(self._total, 1)))
            self.query_one("#run-counts", Static).update(f"{shown} of {self._total} proxies")
        self._show_elapsed()
        live = phase in (Phase.RUNNING, Phase.STOPPING)
        self.query_one("#run-current").display = live
        message = self.query_one("#run-message", Static)
        message.display = not live
        message.set_class(show_bar, "gap-1")
        message.set_class(phase is Phase.STOPPED, "-warning")
        message.set_class(phase in (Phase.START_FAILED, Phase.ENDED), "-error")
        message.set_class(phase is Phase.FINISHED, "-success")
        finished = self.query_one("#run-finished", ListView)
        self.query_one("#run-finished-empty").display = self._processed == 0
        finished.display = self._processed > 0
        stop = self.query_one("#stop", Button)
        stop.display = live
        stop.disabled = phase is not Phase.RUNNING
        force = self._force_available
        self.query_one("#force-stop").display = force
        self.query_one("#run-force-note").display = force
        self.query_one("#resume").display = phase is Phase.STOPPED
        back = self.query_one("#back-to-setup", Button)
        back.display = not live
        back.variant = "primary" if phase in (Phase.START_FAILED, Phase.ENDED, Phase.FINISHED) else "default"

    def _show_current(self, prefix: str, name: str, what: str | None) -> None:
        """The current proxy line: ``Running: <name>: <step>``, the name in bold (plain text, never markup)."""
        line = Content.assemble(prefix, (name, "bold"), f": {what}" if what else "")
        self.query_one("#run-current", Static).update(line)

    def _show_elapsed(self) -> None:
        end = self._ended_at if self._ended_at is not None else self._clock()
        self.query_one("#run-elapsed", Static).update(format_elapsed(end - self._started_at))
