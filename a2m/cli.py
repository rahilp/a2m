"""Command-line entry point: argument parsing and exit codes only.

Exit codes: 0 when the batch finishes, 1 when any proxy failed with an error
or the results (run.log included) could not be written, 2 for usage errors
(bad flags, missing input folder, results folder in use), 130 when the run was
interrupted (Ctrl-C), 128 plus the signal number when it was stopped by
SIGTERM (143) or SIGHUP (129). An interrupted or stopped run still stops the
Mule runtime it started.

Terminal output is best effort and never changes the exit code: when stdout
or stderr is closed, is a pipe whose reader has gone (``a2m ... | head``) or
cannot be written (``> /dev/full``), the message is dropped without a
traceback and the exit code still reports the batch. The results folder and
its run.log are the record of the run, not the terminal.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from a2m import __version__
from a2m.engine import LlmChoice, RunOptions, Stage, interrupted_signal, prepare_run, rerun_advice, run_batch
from a2m.errors import UnsafePathError, UsageError
from a2m.progress import ProgressCallback, ProgressEvent, describe
from a2m.redaction import redact
from a2m.runlog import one_line

if TYPE_CHECKING:
    from _typeshed import SupportsWrite

EXIT_OK = 0
EXIT_PROXY_FAILED = 1
EXIT_USAGE = 2
# The shell convention for a run stopped by Ctrl-C (128 + SIGINT).
EXIT_INTERRUPTED = 130

# --progress: "auto" shows progress lines on stderr only when it is a terminal.
PROGRESS_AUTO = "auto"
PROGRESS_LINES = "lines"
PROGRESS_NONE = "none"
PROGRESS_CHOICES = (PROGRESS_AUTO, PROGRESS_LINES, PROGRESS_NONE)


def _write(stream: SupportsWrite[str] | None, text: str) -> None:
    """The one place a2m writes to the terminal.

    Every line goes through :func:`a2m.runlog.one_line`, so control characters,
    line breaks and undecodable bytes (lone surrogates from file names or
    argv) are escaped, and the result is made encodable by ``stream``. Help
    text keeps its line structure; every message a2m itself prints is a single
    line (see :func:`_say`).

    Writing never raises. A missing stream (``None`` when the shell closed
    it) is skipped. When the write or flush fails (a closed file, a broken
    pipe, a full device), the stream's file descriptor is pointed at
    os.devnull, as the Python docs advise for SIGPIPE, so the interpreter's
    flush at exit cannot print "Exception ignored" either.
    """
    if stream is None:
        return
    try:
        encoding = getattr(stream, "encoding", None) or "utf-8"
        safe = "".join(one_line(line) + "\n" for line in redact(text).splitlines())
        stream.write(safe.encode(encoding, "backslashreplace").decode(encoding, "replace"))
        flush = getattr(stream, "flush", None)
        if flush is not None:
            flush()
    except (OSError, ValueError):
        _silence(stream)


def _silence(stream: object) -> None:
    """Point ``stream``'s file descriptor at os.devnull so later writes and the exit flush succeed."""
    try:
        fd = stream.fileno()  # type: ignore[attr-defined]
        devnull = os.open(os.devnull, os.O_WRONLY)
    except (AttributeError, OSError, ValueError):
        return  # no real file descriptor (an in-memory stream): nothing is flushed at exit
    try:
        os.dup2(devnull, fd)
    except OSError:
        pass
    finally:
        os.close(devnull)


def _say(message: str, *, err: bool = False, terminal_only: bool = False) -> None:
    """Print ``message`` as exactly one line on stdout (or stderr with ``err``).

    With ``terminal_only`` the line is dropped unless that stream is a terminal (``--progress auto``).
    """
    stream = sys.stderr if err else sys.stdout
    if terminal_only and not _is_terminal(stream):
        return
    _write(stream, one_line(message))


def _is_terminal(stream: object) -> bool:
    try:
        return bool(stream.isatty())  # type: ignore[attr-defined]
    except (AttributeError, OSError, ValueError):
        return False


class _Parser(argparse.ArgumentParser):
    """An ArgumentParser whose errors are one stderr line and exit code 2."""

    def error(self, message: str) -> NoReturn:
        text = " ".join(message.split())
        self.exit(EXIT_USAGE, f"{self.prog}: usage error: {text} (see '{self.prog} --help')\n")

    def _print_message(self, message: str, file: SupportsWrite[str] | None = None) -> None:
        # Help, version and usage errors all reach the terminal through here.
        if message:
            _write(file or sys.stderr, message)


def _non_negative_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number 0 or greater, got {value!r}") from None
    if number < 0:
        raise argparse.ArgumentTypeError(f"expected a whole number 0 or greater, got {value!r}")
    return number


def _path_arg(value: str) -> Path:
    """The argparse type for every path argument: an empty or blank value is a usage error.

    ``Path("")`` is ``.``, so an empty argument (often an unset shell
    variable) would otherwise quietly mean the current folder.
    """
    if not value:
        raise argparse.ArgumentTypeError("expected a path, got an empty string")
    if not value.strip():
        raise argparse.ArgumentTypeError(f"expected a path, got only whitespace {value!r}")
    return Path(value)


HEADER_NAME_CHARS = frozenset("!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")


def _header_name(value: str) -> str:
    """The argparse type for --golden-ignore-header: one HTTP header name."""
    name = value.strip()
    if not name or any(ch not in HEADER_NAME_CHARS for ch in name):
        raise argparse.ArgumentTypeError(f"expected an HTTP header name, got {value!r}")
    return name


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="a2m", description="Migrate Apigee proxy bundles to Mule 4 projects.")
    parser.add_argument("--version", action="version", version=f"a2m {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    migrate = commands.add_parser(
        "migrate",
        help="migrate a folder of Apigee proxy exports",
        description="Migrate every Apigee proxy bundle (folder or .zip) in EXPORTS into the results folder.",
    )
    migrate.add_argument("exports", type=_path_arg, metavar="EXPORTS", help="folder of Apigee proxy bundle exports")
    migrate.add_argument("--out", type=_path_arg, required=True, metavar="DIR", help="results folder to write")
    migrate.add_argument("--only", metavar="NAME", help="process only the proxy with this name")
    rerun = migrate.add_mutually_exclusive_group()
    rerun.add_argument("--resume", action="store_true", help="continue an earlier run, skipping finished proxies")
    rerun.add_argument("--force", action="store_true", help="redo every proxy, even ones already finished")
    migrate.add_argument(
        "--golden", type=_path_arg, metavar="DIR", help="recorded Apigee responses to compare the Mule apps against"
    )
    migrate.add_argument(
        "--golden-ignore-header",
        action="append",
        type=_header_name,
        default=[],
        metavar="NAME",
        help=(
            "a header that differs on every call (e.g. X-Apigee-Message-ID): a golden replay does not compare it in "
            "responses or backend calls; repeat for more. Always ignored: Date, Server, Content-Length, "
            "Transfer-Encoding, Connection, X-Request-ID, X-Correlation-ID, Keep-Alive, and Host and forwarding "
            "headers on backend calls"
        ),
    )
    migrate.add_argument(
        "--mock-backends", action="store_true", help="run the Mule apps against mock backends that record calls"
    )
    migrate.add_argument(
        "--max-fix-attempts",
        type=_non_negative_int,
        default=3,
        metavar="N",
        help="how many times the AI may try to fix a failing app (default: 3)",
    )
    migrate.add_argument(
        "--llm",
        choices=[choice.value for choice in LlmChoice],
        default=LlmChoice.CLAUDE.value,
        help="AI provider for steps templates cannot handle (default: claude)",
    )
    migrate.add_argument(
        "--no-runtime", action="store_true", help="skip steps that need Java, Maven or the Mule runtime"
    )
    migrate.add_argument(
        "--progress",
        choices=PROGRESS_CHOICES,
        default=PROGRESS_AUTO,
        help="progress lines on stderr: auto (only when stderr is a terminal, the default), lines or none",
    )
    return parser


def _progress_callback(mode: str) -> ProgressCallback | None:
    """What prints the progress events: each as one line on stderr (escaped and masked like every line a2m
    prints); with ``auto`` only while stderr is a terminal; with ``none`` nothing."""
    if mode == PROGRESS_NONE:
        return None
    terminal_only = mode == PROGRESS_AUTO

    def show(event: ProgressEvent) -> None:
        line = describe(event)
        if line is not None:
            _say(line, err=True, terminal_only=terminal_only)

    return show


def main(argv: list[str] | None = None, *, stages: Sequence[Stage] | None = None) -> int:
    """Run the a2m command line and return its exit code."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --help, --version and usage errors
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    options = RunOptions(
        input_dir=args.exports,
        out_dir=args.out,
        only=args.only,
        resume=args.resume,
        force=args.force,
        golden=args.golden,
        mock_backends=args.mock_backends,
        max_fix_attempts=args.max_fix_attempts,
        llm=LlmChoice(args.llm),
        no_runtime=args.no_runtime,
        golden_ignore_headers=tuple(args.golden_ignore_header),
    )
    try:
        plan = prepare_run(options)
        result = run_batch(plan, stages, progress=_progress_callback(args.progress))
    except KeyboardInterrupt as exc:
        signum = interrupted_signal(exc)
        what = "interrupted" if signum is None else f"stopped by {signal.Signals(signum).name}"
        _say(f"a2m migrate: {what}; {rerun_advice(options, exc)}", err=True)
        return EXIT_INTERRUPTED if signum is None else 128 + signum
    except UsageError as exc:
        _say(f"a2m migrate: usage error: {exc}", err=True)
        return EXIT_USAGE
    except (OSError, UnsafePathError) as exc:
        _say(f"a2m migrate: error: cannot write results to {options.out_dir}: {exc}", err=True)
        return EXIT_PROXY_FAILED

    for notice in result.notices:
        _say(f"a2m: {notice}", err=True)
    _say(
        f"a2m: {len(result.finished)} done, {len(result.skipped)} skipped as already done, "
        f"{len(result.refused)} refused, {len(result.crashed)} failed. "
        f"Log: {result.log_path}"
    )
    return EXIT_PROXY_FAILED if result.crashed else EXIT_OK
