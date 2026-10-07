"""The ``a2m migrate`` child process a run screen watches, and the ``--progress json`` stream it reads.

A run from the TUI is the same command the setup screen previews, run as a child process with
``--progress json`` (:func:`migrate_child_argv`): the child's stdout is one JSON event per line (schema:
:mod:`a2m.progress`) and its stderr carries a2m's own messages, such as the one usage-error line a run that
cannot start prints. :class:`ChildProcess` reads both in two reader threads and hands everything to one
``deliver`` callback, in order: each :class:`StreamEvent`, an :class:`Unrecognised` for any stdout line
that is not a known event, and finally one :class:`Exited`. Reading is bounded: an over-long line is
dropped (reported as unrecognised) and only the last few stderr lines are kept.

Signals go only to the process this module started, and only while it has not been reaped, so a recycled
process id is never signalled. Nothing here sends SIGKILL. Imports no Textual.
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import threading
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import IO

from a2m.layout import BUCKET_DIR_NAMES
from a2m.progress import SCHEMA_VERSION, EventKind, Outcome, ProgressEvent, Step
from a2m.tui.command import SetupChoices, migrate_argv

PROGRESS_FLAGS = ("--progress", "json")
RESUME_FLAG = "--resume"
FORCE_FLAG = "--force"
# A stdout line longer than this is not an event a2m writes; it is dropped, never held in memory whole.
MAX_LINE_BYTES = 256 * 1024
# How many stderr lines are kept (a2m's own messages; the last one explains a run that could not start).
STDERR_KEEP = 20
# How long to wait for the stderr reader after the child exited (a process it started may hold stderr open).
STDERR_DRAIN_SECONDS = 2.0

_KINDS = frozenset(kind.value for kind in EventKind)
_STEPS = frozenset(step.value for step in Step)
_OUTCOMES = frozenset(outcome.value for outcome in Outcome)
_INT_FIELDS = ("total", "index", "attempt", "attempts", "finished", "skipped", "refused", "failed")


def migrate_child_argv(choices: SetupChoices) -> list[str]:
    """The child process for a run with ``choices``: this Python running ``a2m migrate`` with the same
    arguments as the command preview, plus ``--progress json``."""
    return [sys.executable, "-m", "a2m", *migrate_argv(choices)[1:], *PROGRESS_FLAGS]


def resume_argv(argv: Sequence[str], advice: str) -> list[str]:
    """``argv`` again with the flag the stopped run's own ``advice`` names (``--force`` when it says so,
    ``--resume`` otherwise) in place of any ``--resume`` or ``--force`` it already had."""
    flag = FORCE_FLAG if FORCE_FLAG in advice.split() else RESUME_FLAG
    return [arg for arg in argv if arg not in (RESUME_FLAG, FORCE_FLAG)] + [flag]


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """One known line of the ``--progress json`` stream.

    ``event`` holds the progress fields (its ``kind`` names the line); ``exit_code``, ``advice``,
    ``message`` and ``signal`` are set only on the lines that carry them (``run-finished``, ``stopped``,
    ``error``).
    """

    event: ProgressEvent
    exit_code: int | None = None
    advice: str = ""
    message: str = ""
    signal: str = ""

    @property
    def kind(self) -> EventKind:
        return self.event.kind


@dataclass(frozen=True, slots=True)
class Unrecognised:
    """A stdout line that is not a known event (malformed, too long, or from a newer schema)."""

    line: str


@dataclass(frozen=True, slots=True)
class Exited:
    """The child has exited with ``returncode`` (negative: killed by that signal); ``stderr`` holds its last
    non-empty stderr lines, oldest first."""

    returncode: int
    stderr: tuple[str, ...]


ChildOutput = StreamEvent | Unrecognised | Exited


def _int_field(record: dict[str, object], key: str) -> int | None:
    value = record.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _text_field(record: dict[str, object], key: str) -> str | None:
    value = record.get(key, "")
    return value if isinstance(value, str) else None


def parse_line(line: str) -> StreamEvent | None:
    """The event on one ``--progress json`` line, or None when it is not a well-formed known event."""
    try:
        record = json.loads(line)
    except (ValueError, RecursionError):
        return None
    if not isinstance(record, dict) or record.get("schema_version") != SCHEMA_VERSION:
        return None
    kind_text = record.get("kind")
    if not isinstance(kind_text, str) or kind_text not in _KINDS:
        return None
    kind = EventKind(kind_text)
    ints: dict[str, int] = {}
    for key in _INT_FIELDS:
        number = _int_field(record, key)
        if number is None:
            return None
        ints[key] = number
    name = _text_field(record, "name")
    if name is None:
        return None
    step_text, outcome_text = record.get("step"), record.get("outcome")
    if step_text is not None and not (isinstance(step_text, str) and step_text in _STEPS):
        return None
    if outcome_text is not None and not (isinstance(outcome_text, str) and outcome_text in _OUTCOMES):
        return None
    step = None if step_text is None else Step(str(step_text))
    outcome = None if outcome_text is None else Outcome(str(outcome_text))
    bucket = record.get("bucket")
    if bucket is not None and not (isinstance(bucket, str) and bucket in BUCKET_DIR_NAMES):
        return None
    if kind is EventKind.PROXY_FINISHED and outcome is None:
        return None
    exit_code: int | None = None
    if kind in (EventKind.RUN_FINISHED, EventKind.STOPPED, EventKind.ERROR):
        raw = record.get("exit_code")
        if isinstance(raw, bool) or not isinstance(raw, int):
            return None
        exit_code = raw
    texts = {key: _text_field(record, key) for key in ("advice", "message", "signal")}
    if any(value is None for value in texts.values()):
        return None
    event = ProgressEvent(kind, name=name, step=step, outcome=outcome, bucket=bucket, **ints)
    return StreamEvent(
        event,
        exit_code=exit_code,
        advice=texts["advice"] or "",
        message=texts["message"] or "",
        signal=texts["signal"] or "",
    )


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", "replace").rstrip("\r\n")


def _read_line(stream: IO[bytes]) -> tuple[bytes, bool]:
    """The next line of ``stream`` (b"" at the end) and whether it fit in :data:`MAX_LINE_BYTES`; the rest
    of an over-long line is read and dropped, never kept."""
    raw = stream.readline(MAX_LINE_BYTES + 1)
    if len(raw) <= MAX_LINE_BYTES or raw.endswith(b"\n"):
        return raw, True
    while True:
        rest = stream.readline(MAX_LINE_BYTES)
        if not rest or rest.endswith(b"\n"):
            return raw[:MAX_LINE_BYTES], False


class ChildProcess:
    """One ``a2m migrate --progress json`` child: started once, read until it exits, stopped by signal.

    ``deliver`` is called from a reader thread, in order, with every :data:`ChildOutput`; a caller with
    an event loop hands each one over to it (see :mod:`a2m.tui.run`).
    """

    def __init__(self, argv: Sequence[str], deliver: Callable[[ChildOutput], None]) -> None:
        self.argv = list(argv)
        self._deliver = deliver
        self._process: subprocess.Popen[bytes] | None = None
        self._stderr: deque[str] = deque(maxlen=STDERR_KEEP)
        self._stderr_done = threading.Event()

    def start(self) -> None:
        """Start the child (environment inherited, no stdin) and its reader threads; raises OSError when
        it cannot be started at all.

        The child runs in its own session, as a2m runs its own Mule runtime: a signal aimed at the TUI's
        terminal or process group (a hang-up, ``kill -- -<pgid>``) reaches only the TUI, which stops the child
        with exactly one SIGINT. Shared with the TUI's group, the child would get that signal as well as the
        TUI's SIGINT, and the second one would cut a2m's clean-up short."""
        process = subprocess.Popen(
            self.argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            start_new_session=True,
        )
        self._process = process
        threading.Thread(target=self._read_stderr, name="a2m-child-stderr", daemon=True).start()
        threading.Thread(target=self._read_stdout, name="a2m-child-stdout", daemon=True).start()

    @property
    def pid(self) -> int | None:
        return None if self._process is None else self._process.pid

    @property
    def alive(self) -> bool:
        """True while the child started and has not exited yet."""
        return self._process is not None and self._process.poll() is None

    def interrupt(self) -> bool:
        """Send SIGINT (what Ctrl-C sends: a2m stops cleanly and keeps finished proxies); False when the
        child is not running."""
        return self._signal(signal.SIGINT)

    def terminate(self) -> bool:
        """Send SIGTERM (a2m handles it like Ctrl-C); False when the child is not running."""
        return self._signal(signal.SIGTERM)

    def _signal(self, signum: signal.Signals) -> bool:
        process = self._process
        if process is None or process.poll() is not None:
            return False
        try:
            # Popen.send_signal checks the child has not been reaped first, so a reused pid is never hit.
            process.send_signal(signum)
        except ProcessLookupError:
            return False
        return True

    def _read_stderr(self) -> None:
        process = self._process
        assert process is not None and process.stderr is not None
        try:
            while True:
                raw, _whole = _read_line(process.stderr)
                if not raw:
                    break
                text = _decode(raw).strip()
                if text:
                    self._stderr.append(text)
        except (OSError, ValueError):
            pass
        finally:
            process.stderr.close()
            self._stderr_done.set()

    def _read_stdout(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        try:
            while True:
                raw, whole = _read_line(process.stdout)
                if not raw:
                    break
                line = _decode(raw)
                if not line.strip():
                    continue
                parsed = parse_line(line) if whole else None
                self._deliver(parsed if parsed is not None else Unrecognised(line[:200]))
        except (OSError, ValueError):
            pass
        finally:
            process.stdout.close()
        returncode = process.wait()
        self._stderr_done.wait(STDERR_DRAIN_SECONDS)
        self._deliver(Exited(returncode, tuple(self._stderr)))
