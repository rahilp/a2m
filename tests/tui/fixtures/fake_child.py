"""A tiny real child process for CP6 TUI tests: speaks the real ``--progress json`` event schema
(``a2m/progress.py``) without running a real migration, so the run screen's own code (reading a child's
stdout line by line, sending it signals, watching it exit) is exercised against a real process rather than a
mock.

Not imported by any test (and not collected by pytest): it only ever runs as ``sys.executable
tests/tui/fixtures/fake_child.py ...`` in a subprocess. It writes its own pid to ``<ready-file>`` before
doing anything else, so a test can wait for it to be up and signal it by that pid.

Usage::

    fake_child.py <ready-file> startfail <message>
        Print exactly one line to stderr and exit 2, before any stdout line (a start failure).

    fake_child.py <ready-file> replay <events-file> [--pause-after N] [--pause-seconds S]
                  [--ignore-first-sigint] [--advice TEXT] [--message TEXT]
        Print each line of <events-file> to stdout in order, with a small delay between lines; after
        printing line N (0-based), pause for S seconds (default: a short delay for every line) unless a
        signal cuts the pause short. On SIGTERM, or on SIGINT (unless --ignore-first-sigint and this is the
        very first SIGINT, which is swallowed so the test can exercise a SIGTERM escalation), stop at once
        and print one final 'stopped' event (schema: a2m/progress.py) built from --advice/--message and the
        signal actually received, then exit 130 (SIGINT) or 128+signum (otherwise). Reaching the end of the
        file with no signal exits 0 and prints nothing further.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

DEFAULT_DELAY_SECONDS = 0.02


def _write_ready(path: Path) -> None:
    path.write_text(str(os.getpid()), encoding="utf-8")


def _emit_stdout(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _run_startfail(ready: Path, message: str) -> int:
    _write_ready(ready)
    sys.stderr.write(message + "\n")
    sys.stderr.flush()
    return 2


def _run_replay(
    ready: Path,
    events_path: Path,
    *,
    pause_after: int,
    pause_seconds: float,
    ignore_first_sigint: bool,
    advice: str,
    message: str,
) -> int:
    _write_ready(ready)
    lines = [line for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    state = {"stop": False, "ignored_once": False, "signum": signal.SIGINT}

    def handle(signum: int, _frame: object) -> None:
        if signum == signal.SIGINT and ignore_first_sigint and not state["ignored_once"]:
            state["ignored_once"] = True
            return
        state["stop"] = True
        state["signum"] = signum

    signal.signal(signal.SIGINT, handle)
    signal.signal(signal.SIGTERM, handle)

    for index, line in enumerate(lines):
        if state["stop"]:
            break
        _emit_stdout(line)
        delay = pause_seconds if index == pause_after else DEFAULT_DELAY_SECONDS
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline and not state["stop"]:
            time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))

    if not state["stop"]:
        return 0

    signum = state["signum"]
    exit_code = 130 if signum == signal.SIGINT else 128 + signum
    stopped = {
        "schema_version": 1,
        "kind": "stopped",
        "exit_code": exit_code,
        "signal": signal.Signals(signum).name,
        "advice": advice,
        "message": message,
    }
    _emit_stdout(json.dumps(stopped, sort_keys=True))
    return exit_code


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("ready", type=Path)
    sub = parser.add_subparsers(dest="mode", required=True)

    startfail = sub.add_parser("startfail")
    startfail.add_argument("message")

    replay = sub.add_parser("replay")
    replay.add_argument("events", type=Path)
    replay.add_argument("--pause-after", type=int, default=-1)
    replay.add_argument("--pause-seconds", type=float, default=DEFAULT_DELAY_SECONDS)
    replay.add_argument("--ignore-first-sigint", action="store_true")
    replay.add_argument("--advice", default="")
    replay.add_argument("--message", default="")

    args = parser.parse_args(argv)
    if args.mode == "startfail":
        return _run_startfail(args.ready, args.message)
    return _run_replay(
        args.ready,
        args.events,
        pause_after=args.pause_after,
        pause_seconds=args.pause_seconds,
        ignore_first_sigint=args.ignore_first_sigint,
        advice=args.advice,
        message=args.message,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
