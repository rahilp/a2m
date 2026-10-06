"""Progress events of a batch: which proxy is running, its slow steps, and how it ended.

The engine reports a batch as a sequence of :class:`ProgressEvent` values to one optional callback (see
:func:`a2m.engine.run_batch`): ``run-started`` with the number of selected proxies, then for each proxy
``proxy-started`` (when it is actually processed), any number of ``step`` events (building, deploying,
running tests, each AI fix attempt) and ``proxy-finished`` with its outcome and bucket, and finally
``run-finished`` with the counts. Events are plain frozen dataclasses with closed vocabularies, so they turn
into JSON with ``dataclasses.asdict``.

Progress never changes a run. :class:`Progress` calls the callback through a guard: a callback that raises
is logged once in run.log and never called again, and the migration carries on as if it was never there.
Stages deep in the pipeline report their steps with :func:`step`, which goes to the proxy being processed
(set by the engine with :meth:`Progress.proxy`) and does nothing outside a batch.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum

from a2m.runlog import get_logger


class EventKind(StrEnum):
    RUN_STARTED = "run-started"
    PROXY_STARTED = "proxy-started"
    STEP = "step"
    PROXY_FINISHED = "proxy-finished"
    RUN_FINISHED = "run-finished"


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
