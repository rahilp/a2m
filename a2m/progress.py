"""Progress events of a batch: which proxy is running, its slow steps, and how it ended.

The engine reports a batch as a sequence of :class:`ProgressEvent` values to one optional callback (see
:func:`a2m.engine.run_batch`): ``run-started`` with the number of selected proxies, then for each proxy
``proxy-started`` (when it is actually processed), any number of ``step`` events (building, deploying,
running tests, each AI fix attempt) and ``proxy-finished`` with its outcome and bucket, and finally
``run-finished`` with the counts. Events are plain frozen dataclasses with closed vocabularies, so they turn
into JSON with ``dataclasses.asdict``.

``a2m migrate --progress json`` writes the stream another program (the TUI) reads: one JSON object per
stdout line (:func:`json_line`), keys sorted, ASCII only, every string escaped to one line and masked like
terminal output. Each object has ``schema_version`` (:data:`SCHEMA_VERSION`) and ``kind``:

- ``run-started``, ``proxy-started``, ``step``, ``proxy-finished``: every :class:`ProgressEvent` field
  (``total``, ``index``, ``name``, ``step``, ``attempt``, ``attempts``, ``outcome``, ``bucket``, and the
  counts, which are 0 here). Where a field does not apply, the int fields (``total``, ``index``,
  ``attempt``, ``attempts`` and the counts) are ``0``; only ``step``, ``outcome`` and ``bucket`` are
  ``null``. Read a field only for the kinds it belongs to: ``attempt`` 0 means "not an AI fix attempt".
- ``run-finished`` (last line of a completed run): the same fields with the counts ``finished``,
  ``skipped``, ``refused``, ``failed``, plus ``exit_code`` (0, or 1 when a proxy failed), ``log`` (the
  run.log path) and ``notices`` (batch-wide notices, a list of strings).
- ``stopped`` (last line of a run stopped by Ctrl-C or a signal): ``exit_code`` (130, or 128 plus the
  signal), ``signal`` (e.g. ``SIGINT``), ``advice`` (how to finish the job, as the plain CLI prints it) and
  ``message`` (the CLI's own one-line notice).
- ``error`` (last line when the results could not be written after the stream began): ``exit_code`` (1)
  and ``message``.

A stream is not guaranteed to begin with ``run-started``: a Ctrl-C or signal that arrives before the batch
starts (while the run is being prepared) gives a stream of exactly one ``stopped`` line. A consumer must
treat a lone ``stopped`` as a valid, complete stream (the run ended before any proxy was processed).
``error`` is only written after ``run-started``.

Usage errors (bad flags, missing folder, results folder in use) give no JSON at all: one stderr line and
exit code 2. Human notices and the summary line go to stderr in this mode, never stdout.

Progress never changes a run. :class:`Progress` calls the callback through a guard: a callback that raises
is logged once in run.log and never called again, and the migration carries on as if it was never there.
Stages deep in the pipeline report their steps with :func:`step`, which goes to the proxy being processed
(set by the engine with :meth:`Progress.proxy`) and does nothing outside a batch.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
from collections.abc import Callable, Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum

from a2m.redaction import redact
from a2m.runlog import get_logger, one_line

# The version of the --progress json stream's shape (see the module docstring); bumped on incompatible change.
SCHEMA_VERSION = 1


class EventKind(StrEnum):
    RUN_STARTED = "run-started"
    PROXY_STARTED = "proxy-started"
    STEP = "step"
    PROXY_FINISHED = "proxy-finished"
    RUN_FINISHED = "run-finished"
    # Written by the command line only, as the last line of a run that did not finish (see the module docstring).
    STOPPED = "stopped"
    ERROR = "error"


class Step(StrEnum):
    """The slow steps of one proxy worth showing while they run."""

    BUILD = "build"
    DEPLOY = "deploy"
    TESTS = "tests"
    AI_FIX = "ai-fix"


class Outcome(StrEnum):
    """How a selected proxy ended (the lists of :class:`a2m.engine.BatchResult`)."""

    FINISHED = "finished"
    SKIPPED = "skipped"
    REFUSED = "refused"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """One progress event. ``index`` (1-based) and ``total`` place a proxy in the batch; ``attempt`` and
    ``attempts`` are set for an AI fix step; the counts are set on ``run-finished``."""

    kind: EventKind
    total: int = 0
    index: int = 0
    name: str = ""
    step: Step | None = None
    attempt: int = 0
    attempts: int = 0
    outcome: Outcome | None = None
    bucket: str | None = None
    finished: int = 0
    skipped: int = 0
    refused: int = 0
    failed: int = 0


ProgressCallback = Callable[[ProgressEvent], None]

_STEP_TEXT = {Step.BUILD: "building", Step.DEPLOY: "deploying", Step.TESTS: "running tests"}
_OUTCOME_TEXT = {
    Outcome.FINISHED: "done",
    Outcome.SKIPPED: "skipped, already done",
    Outcome.REFUSED: "refused",
    Outcome.FAILED: "failed",
}


def describe(event: ProgressEvent) -> str | None:
    """The one terminal line for ``event`` (``[3/12] orders-api: generating``), or None for batch-level events.

    The proxy name is as given; the caller writes the line through the terminal writer, which escapes control
    characters and masks secrets like every other line.
    """
    if event.kind is EventKind.PROXY_STARTED:
        text = "generating"
    elif event.kind is EventKind.STEP and event.step is not None:
        if event.step is Step.AI_FIX:
            text = f"AI fix {event.attempt} of {event.attempts}"
        else:
            text = _STEP_TEXT[event.step]
    elif event.kind is EventKind.PROXY_FINISHED and event.outcome is not None:
        text = _OUTCOME_TEXT[event.outcome]
        if event.bucket is not None:
            text += f", in {event.bucket}/"
    else:
        return None
    return f"[{event.index}/{event.total}] {event.name}: {text}"


def event_fields(event: ProgressEvent) -> dict[str, object]:
    """``event`` as the plain fields of its JSON object (enums as their values)."""
    return dataclasses.asdict(event)


def _safe(value: object) -> object:
    if isinstance(value, str):
        return one_line(redact(str(value)))
    if isinstance(value, list | tuple):
        return [_safe(item) for item in value]
    return value


def json_line(fields: Mapping[str, object]) -> str:
    """One line of the --progress json stream: ``fields`` plus ``schema_version``, strings made safe."""
    record = {key: _safe(value) for key, value in fields.items()}
    record["schema_version"] = SCHEMA_VERSION
    return json.dumps(record, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


_current: ContextVar[Progress | None] = ContextVar("a2m_progress", default=None)


class Progress:
    """Sends a batch's events to ``callback`` (None: nowhere), isolated from the run (see the module docstring)."""

    def __init__(self, callback: ProgressCallback | None, total: int) -> None:
        self._callback = callback
        self.total = total
        self._index = 0
        self._name = ""
        self._last_step: tuple[Step, int] | None = None

    def emit(self, event: ProgressEvent) -> None:
        callback = self._callback
        if callback is None:
            return
        try:
            callback(event)
        except Exception as exc:  # noqa: BLE001  progress is display only: it must never fail or change the run
            self._callback = None
            get_logger().warning(
                "progress display failed (%s: %s); no more progress is shown for this run", type(exc).__name__, exc
            )

    def run_started(self) -> None:
        self.emit(ProgressEvent(EventKind.RUN_STARTED, total=self.total))

    def run_finished(self, *, finished: int, skipped: int, refused: int, failed: int) -> None:
        self.emit(
            ProgressEvent(
                EventKind.RUN_FINISHED, total=self.total, finished=finished, skipped=skipped, refused=refused,
                failed=failed,
            )
        )

    @contextlib.contextmanager
    def proxy(self, index: int, name: str) -> Iterator[None]:
        """For the block, proxy ``name`` (``index`` of :attr:`total`) is the one :func:`step` reports on."""
        self._index, self._name, self._last_step = index, name, None
        token = _current.set(self)
        try:
            yield
        finally:
            _current.reset(token)

    def started(self) -> None:
        self.emit(ProgressEvent(EventKind.PROXY_STARTED, self.total, self._index, self._name))

    def finished(self, outcome: Outcome, bucket: str | None = None) -> None:
        self.emit(
            ProgressEvent(EventKind.PROXY_FINISHED, self.total, self._index, self._name, outcome=outcome, bucket=bucket)
        )

    def step(self, kind: Step, attempt: int = 0, attempts: int = 0) -> None:
        # The same step twice in a row is shown once (the runner and the harness may both report a deploy).
        if self._last_step == (kind, attempt):
            return
        self._last_step = (kind, attempt)
        self.emit(
            ProgressEvent(
                EventKind.STEP, self.total, self._index, self._name, step=kind, attempt=attempt, attempts=attempts
            )
        )


def step(kind: Step, attempt: int = 0, attempts: int = 0) -> None:
    """Report that the proxy being processed has reached step ``kind``; nothing happens outside a batch."""
    progress = _current.get()
    if progress is not None:
        progress.step(kind, attempt, attempts)
