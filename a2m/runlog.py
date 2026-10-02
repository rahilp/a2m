"""The batch's run.log: timestamped lines written through ``logging``.

Every line in run.log, including each line of a traceback, starts with a
local date-time stamp, so the file stays easy to grep and sort. A log message
is always one line: control characters, line breaks and undecodable bytes in
it (for example from a file name) are written as escapes, so input names can
never split a message or forge an entry.

A write to run.log that fails (disk full, an I/O error on a network share)
never prints the logging module's "--- Logging error ---" traceback. The
handler remembers the first failure, and when the run ends normally
:func:`run_log` raises :class:`RunLogError`, so the command reports one line
and exits non-zero: the log no longer names everything the run did.
"""

from __future__ import annotations

import io
import logging
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

LOGGER_NAME = "a2m"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S%z"


def one_line(text: str) -> str:
    """``text`` with every non-printable character (newline, tab, surrogate...) escaped."""
    if text.isprintable():
        return text
    return "".join(
        char if char.isprintable() else char.encode("unicode_escape").decode("ascii") for char in text
    )


class LinePrefixFormatter(logging.Formatter):
    """Prefix every line of a record (message and traceback) with time and level."""

    def __init__(self) -> None:
        super().__init__(datefmt=DATE_FORMAT)

    def format(self, record: logging.LogRecord) -> str:
        prefix = f"{self.formatTime(record, self.datefmt)} {record.levelname:<7} "
        body = one_line(record.getMessage())
        if record.exc_info:
            body = f"{body}\n{self.formatException(record.exc_info)}"
        if record.stack_info:
            body = f"{body}\n{self.formatStack(record.stack_info)}"
        # Traceback lines can carry exception text (file names, input values);
        # each line is escaped the same way as the message.
        lines = body.splitlines() or [""]
        return "\n".join(prefix + one_line(line) for line in lines)


class RunLogError(OSError):
    """run.log could not be written completely; some entries are missing."""


class _OpenerFileHandler(logging.FileHandler):
    """A FileHandler that opens its file through ``opener`` (which returns a file descriptor).

    A record that cannot be written is not reported on stderr (the logging
    module's default): the first failure is kept in :attr:`failure` for
    :func:`run_log` to raise once the run is over.
    """

    def __init__(self, path: Path, opener: Callable[[], int]) -> None:
        self._opener = opener
        self.failure: BaseException | None = None
        super().__init__(path, mode="a", encoding="utf-8", errors="backslashreplace")

    def _open(self) -> io.TextIOWrapper:
        return io.TextIOWrapper(
            io.FileIO(self._opener(), "a"), encoding="utf-8", errors="backslashreplace", line_buffering=False
        )

    def handleError(self, record: logging.LogRecord) -> None:
        if self.failure is None:
            self.failure = sys.exc_info()[1]


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


@contextmanager
def run_log(path: Path, opener: Callable[[], int]) -> Iterator[logging.Logger]:
    """Append to ``path`` for the duration of the block and yield the a2m logger.

    ``opener`` opens ``path`` for appending and returns the file descriptor;
    the engine passes one that refuses links, FIFOs and devices. When any
    entry could not be written, :class:`RunLogError` is raised after a block
    that ended normally (an exception from the block itself wins).
    """
    logger = get_logger()
    handler = _OpenerFileHandler(path, opener)
    handler.setFormatter(LinePrefixFormatter())
    handler.setLevel(logging.INFO)
    previous_level = logger.level
    logger.addHandler(handler)
    if logger.getEffectiveLevel() > logging.INFO:
        logger.setLevel(logging.INFO)
    try:
        yield logger
    finally:
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception as exc:  # noqa: BLE001  a failed final flush is a failed log write, reported below
            if handler.failure is None:
                handler.failure = exc
        logger.setLevel(previous_level)
    if handler.failure is not None:
        raise RunLogError(f"run.log is incomplete, writing {path} failed: {handler.failure}")
