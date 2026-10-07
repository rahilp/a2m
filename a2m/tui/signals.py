"""Route SIGINT, SIGTERM and SIGHUP sent to the TUI's own process into the run screen's stop path.

While a run screen has a live ``a2m migrate`` child, a plain ``kill <pid>`` (SIGTERM) or a hang-up (SIGHUP)
on the TUI would otherwise end the TUI at once with no Python code run, leaving the child unsupervised, and a
SIGINT sent to the TUI's pid (``kill -INT``, a supervisor whose stop signal is SIGINT; the keyboard's Ctrl-C
never sends one, as Textual turns the terminal's ISIG off) would end it with a raw KeyboardInterrupt.
:class:`TerminationGuard` installs handlers for all three signals for as long as a child runs and restores
the previous handlers afterwards, the way a2m's own engine handles them for itself
(:func:`a2m.engine._stop_on_termination`): main thread only, and a signal that was ignored stays ignored.
With no child running, :func:`routed_interrupt` (installed for the whole life of ``a2m tui``) routes the same
three signals into the app, so it exits through Textual's own shutdown and the terminal is restored.

The handler itself does no work: it only schedules ``on_signal(signum)`` onto the event loop with
``call_soon_threadsafe`` (no locks, safe from a signal handler), where the screen stops the run. Imports no
Textual.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import threading
from collections.abc import Callable, Iterator
from types import FrameType

from a2m.engine import Terminated

GUARDED_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)

_Handler = Callable[[int, FrameType | None], object] | int | signal.Handlers | None


class TerminationGuard:
    """SIGINT, SIGTERM and SIGHUP handlers that hand the signal to ``on_signal`` on ``loop``; install, then remove."""

    def __init__(self, loop: asyncio.AbstractEventLoop, on_signal: Callable[[int], object]) -> None:
        self._loop = loop
        self._on_signal = on_signal
        self._previous: dict[signal.Signals, _Handler] = {}

    def _handler(self, signum: int, frame: FrameType | None) -> None:
        # Signal context: schedule only. A closed loop means the app is gone; nothing is left to stop.
        with contextlib.suppress(RuntimeError):
            self._loop.call_soon_threadsafe(self._on_signal, signum)

    def install(self) -> None:
        """Install the handlers (main thread only; a no-op when already installed or elsewhere)."""
        if self._previous or threading.current_thread() is not threading.main_thread():
            return
        for sig in GUARDED_SIGNALS:
            try:
                current = signal.getsignal(sig)
                if current == signal.SIG_IGN:
                    continue  # ignored stays ignored (e.g. under nohup)
                signal.signal(sig, self._handler)
            except (OSError, ValueError):
                continue
            self._previous[sig] = current

    def remove(self) -> None:
        """Put back the handlers that were there before :meth:`install`."""
        if threading.current_thread() is not threading.main_thread():
            return
        previous, self._previous = self._previous, {}
        for sig, old in previous.items():
            with contextlib.suppress(OSError, ValueError, TypeError):
                signal.signal(sig, old if old is not None else signal.SIG_DFL)  # type: ignore[arg-type]


@contextlib.contextmanager
def routed_interrupt(deliver: Callable[[int], bool]) -> Iterator[None]:
    """For the block (the whole life of ``a2m tui``), SIGINT, SIGTERM and SIGHUP to the TUI's own process call
    ``deliver(signum)`` instead of ending the process at once (SIGTERM, SIGHUP) or raising KeyboardInterrupt
    (SIGINT), so the app ends the same way on any screen, with or without a run going: through Textual's own
    shutdown (the terminal is restored) with 128 + the signal, after stopping any run first. ``deliver`` runs
    in signal context, so it must only schedule work; when it returns False (no event loop yet, or already
    closed) the signal raises KeyboardInterrupt (SIGINT) or :class:`a2m.engine.Terminated` (SIGTERM, SIGHUP),
    which the command line turns into the same one line and exit code. Main thread only; an ignored signal
    stays ignored (e.g. SIGHUP under nohup). The previous handlers are put back when the block ends. Without
    this, asyncio turns a SIGINT into cancelling the app, which Textual ends quietly with exit code 0, and a
    SIGTERM or SIGHUP kills the process with the terminal left in the alternate screen and raw mode.

    While a run's child is alive, the run screen's :class:`TerminationGuard` takes the three signals over and
    hands them back when the child has exited."""

    def handler(signum: int, frame: FrameType | None) -> None:
        if deliver(signum):
            return
        if signum == signal.SIGINT:
            raise KeyboardInterrupt
        raise Terminated(signum)

    previous: dict[signal.Signals, _Handler] = {}
    if threading.current_thread() is threading.main_thread():
        for sig in GUARDED_SIGNALS:
            try:
                current = signal.getsignal(sig)
                if current == signal.SIG_IGN:
                    continue
                signal.signal(sig, handler)
            except (OSError, ValueError):
                continue
            previous[sig] = current
    try:
        yield
    finally:
        for sig, old in previous.items():
            with contextlib.suppress(OSError, ValueError, TypeError):
                signal.signal(sig, old if old is not None else signal.SIG_DFL)  # type: ignore[arg-type]
