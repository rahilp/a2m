"""Command-line entry point: argument parsing and exit codes only.

Exit codes: 0 when the batch finishes, 1 when any proxy failed with an error
or the results (run.log included) could not be written, 2 for usage errors
(bad flags, missing input folder, results folder in use), 130 when the run was
interrupted (Ctrl-C), 128 plus the signal number when it was stopped by
SIGTERM (143) or SIGHUP (129). An interrupted or stopped run still stops the
Mule runtime it started.

``a2m tui``, and a bare ``a2m`` typed in a terminal (stdin and stdout both
terminals), open the terminal UI. It needs the optional ``tui`` extra; without
it they print one line naming ``pip install "a2m[tui]"`` and exit 2. Textual
is imported only for those two, never for any other command.
``a2m tui`` without an interactive terminal prints one line saying so and
exits 2 rather than waiting for keys that can never arrive.

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
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from a2m import __version__
from a2m.engine import LlmChoice, RunOptions, Stage, interrupted_signal, prepare_run, rerun_advice, run_batch
from a2m.errors import NoTerminalError, UnsafePathError, UsageError
from a2m.progress import EventKind, ProgressCallback, ProgressEvent, describe, event_fields, json_line
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
# --progress json: one JSON object per stdout line for another program (see a2m.progress); text goes to stderr.
PROGRESS_JSON = "json"
PROGRESS_CHOICES = (PROGRESS_AUTO, PROGRESS_LINES, PROGRESS_NONE, PROGRESS_JSON)

# `a2m tui` (and a bare `a2m` typed in a terminal) opens the terminal UI, which needs the optional tui extra.
TUI_COMMAND = "tui"
TUI_INSTALL = 'pip install "a2m[tui]"'


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


def _say(message: str | None, *, err: bool = False, terminal_only: bool = False) -> bool:
    """Print ``message`` as exactly one line on stdout (or stderr with ``err``).

    With ``terminal_only`` the line is dropped unless that stream is a terminal (``--progress auto``).
    With ``message`` None nothing is written: the call only reports whether that stream is a
    terminal, so the terminal checks share the one place that picks the stream. Returns whether the
    stream is a terminal for a ``None`` message, and whether a line was written otherwise.
    """
    stream = sys.stderr if err else sys.stdout
    if message is None:
        return _is_terminal(stream)
    if terminal_only and not _is_terminal(stream):
        return False
    _write(stream, one_line(message))
    return True


def _is_terminal(stream: object) -> bool:
    try:
        return bool(stream.isatty())  # type: ignore[attr-defined]
    except (AttributeError, OSError, ValueError):
        return False


def _at_terminal() -> bool:
    """True when a person is typing at a terminal: stdin and stdout are both terminals.

    Only asks the streams whether they are terminals and never writes to them. stdout is asked through
    :func:`_say` (with no message), the helper that owns every stdout and stderr access.
    """
    return _is_terminal(sys.stdin) and _say(None)


def _usage_error_line(prog: str, message: str) -> str:
    text = " ".join(message.split())
    return f"{prog}: usage error: {text} (see '{prog} --help')"


class _Parser(argparse.ArgumentParser):
    """An ArgumentParser whose errors are one stderr line and exit code 2."""

    def error(self, message: str) -> NoReturn:
        self.exit(EXIT_USAGE, _usage_error_line(self.prog, message) + "\n")

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
        help=(
            "progress lines on stderr: auto (only when stderr is a terminal, the default), lines or none; json "
            "writes one JSON event per stdout line for another program"
        ),
    )
    commands.add_parser(
        TUI_COMMAND,
        help="open the terminal UI (needs the tui extra)",
        description=f"Open the a2m terminal UI. It needs the tui extra: {TUI_INSTALL}",
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


class _JsonStream:
    """The --progress json stream on stdout (shape: :mod:`a2m.progress`).

    The engine's run-finished event is held back so the CLI can write it last, with the exit code.
    """

    def __init__(self) -> None:
        self.started = False
        self.run_finished: ProgressEvent | None = None

    def send(self, fields: dict[str, object]) -> None:
        self.started = True
        _say(json_line(fields))

    def event(self, event: ProgressEvent) -> None:
        if event.kind is EventKind.RUN_FINISHED:
            self.run_finished = event
        else:
            self.send(event_fields(event))


def _load_tui() -> Callable[..., int] | None:
    """The TUI's entry point, or None when the tui extra (Textual) is not installed.

    The only place the command line imports :mod:`a2m.tui`, so every other command runs without Textual.
    """
    try:
        from a2m.tui.app import run_app
    except ModuleNotFoundError as exc:
        if (exc.name or "").partition(".")[0] != "textual":
            raise
        return None
    return run_app


def _open_tui(run_app: Callable[..., int]) -> int:
    """Open the TUI; without an interactive terminal, one clear stderr line and EXIT_USAGE instead of a hang."""
    try:
        return run_app(at_terminal=_at_terminal)
    except NoTerminalError as exc:
        _say(str(exc), err=True)
        return EXIT_USAGE


def main(argv: list[str] | None = None, *, stages: Sequence[Stage] | None = None) -> int:
    """Run the a2m command line and return its exit code."""
    parser = build_parser()
    if not (sys.argv[1:] if argv is None else argv) and _at_terminal():
        # A bare `a2m` typed in a terminal opens the TUI; piped or scripted, it stays a usage error.
        run_app = _load_tui()
        if run_app is None:
            missing = _usage_error_line(parser.prog, "the following arguments are required: COMMAND")
            _say(f"{missing}; for the terminal UI (a2m tui), install the tui extra: {TUI_INSTALL}", err=True)
            return EXIT_USAGE
        return _open_tui(run_app)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --help, --version and usage errors
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    if args.command == TUI_COMMAND:
        run_app = _load_tui()
        if run_app is None:
            _say(f"a2m tui: the terminal UI needs the tui extra; install it with: {TUI_INSTALL}", err=True)
            return EXIT_USAGE
        return _open_tui(run_app)
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
    json_stream = _JsonStream() if args.progress == PROGRESS_JSON else None
    callback = json_stream.event if json_stream is not None else _progress_callback(args.progress)
    try:
        plan = prepare_run(options)
        result = run_batch(plan, stages, progress=callback)
    except KeyboardInterrupt as exc:
        signum = interrupted_signal(exc)
        what = "interrupted" if signum is None else f"stopped by {signal.Signals(signum).name}"
        advice = rerun_advice(options, exc)
        message = f"a2m migrate: {what}; {advice}"
        code = EXIT_INTERRUPTED if signum is None else 128 + signum
        _say(message, err=True)
        if json_stream is not None:
            name = signal.Signals(signum if signum is not None else signal.SIGINT).name
            json_stream.send(
                {"kind": EventKind.STOPPED, "exit_code": code, "signal": name, "advice": advice, "message": message}
            )
        return code
    except UsageError as exc:
        # One stderr line and exit 2, never a JSON event: a front end shows the line as it is.
        _say(f"a2m migrate: usage error: {exc}", err=True)
        return EXIT_USAGE
    except (OSError, UnsafePathError) as exc:
        message = f"a2m migrate: error: cannot write results to {options.out_dir}: {exc}"
        _say(message, err=True)
        if json_stream is not None and json_stream.started:
            json_stream.send({"kind": EventKind.ERROR, "exit_code": EXIT_PROXY_FAILED, "message": message})
        return EXIT_PROXY_FAILED

    code = EXIT_PROXY_FAILED if result.crashed else EXIT_OK
    for notice in result.notices:
        _say(f"a2m: {notice}", err=True)
    _say(
        f"a2m: {len(result.finished)} done, {len(result.skipped)} skipped as already done, "
        f"{len(result.refused)} refused, {len(result.crashed)} failed. "
        f"Log: {result.log_path}",
        err=json_stream is not None,  # stdout carries only JSON in json mode
    )
    if json_stream is not None:
        finished = json_stream.run_finished
        fields: dict[str, object] = (
            event_fields(finished) if finished is not None else {"kind": EventKind.RUN_FINISHED}
        )
        fields.update(
            finished=len(result.finished), skipped=len(result.skipped), refused=len(result.refused),
            failed=len(result.crashed), exit_code=code, log=str(result.log_path), notices=list(result.notices),
        )
        json_stream.send(fields)
    return code
