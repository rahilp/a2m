"""Dev-only capture harness: open the a2m TUI in one named UI state.

    python tools/tui_states.py --state setup-ready
    textual serve --host 127.0.0.1 --port 8765 ".venv/bin/python tools/tui_states.py --state setup-ready"

Each state name matches a ``ui_states`` entry in the TUI run's checkpoints.json
and the prototype's ``?state=<name>`` route, and loads the same sample data
the prototype uses for that state (DESIGN.md section 9), so a capture of the
real app can be compared with the prototype screenshot of the same state.
States that need folders get real ones, built in a temporary folder that is
removed when the app exits: the prototype's 12 proxies and 2 shared flows, and
for an earlier run the results of 8 of them. Folder paths differ from the
prototype's ``/home/user/...`` only in their temporary parent folder. The run
states replay the prototype's progress events through a tiny stand-in child
process (written to the temporary folder) instead of running a migration, and
read a clock fixed at the prototype's elapsed time.
Later steps add their states to ``STATES``. Not shipped with the package.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import tempfile
import types
from collections.abc import Callable
from pathlib import Path

# Run from a checkout without installing: make the repo's a2m package importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from a2m import layout
from a2m.ai.claude import KEY_ENV, SDK_MODULE
from a2m.engine import LlmChoice
from a2m.progress import EventKind, Outcome, ProgressEvent, Step, event_fields, json_line
from a2m.tui import app as app_module
from a2m.tui.app import A2MApp
from a2m.tui.command import SetupChoices
from a2m.tui.run import RunScreen

# The prototype's ALL_PROXIES (name, bucket), in its order, and its SHARED_FLOWS_FOUND = 2.
PROXIES: tuple[tuple[str, str], ...] = (
    ("orders-api", layout.VERIFIED_DIR_NAME),
    ("cart-api", layout.VERIFIED_DIR_NAME),
    ("payments-api", layout.VERIFIED_DIR_NAME),
    ("inventory-api", layout.VERIFIED_DIR_NAME),
    ("catalog-api", layout.VERIFIED_DIR_NAME),
    ("pricing-api", layout.VERIFIED_DIR_NAME),
    ("legacy-auth", layout.NEEDS_REVIEW_DIR_NAME),
    ("js-transform", layout.NEEDS_REVIEW_DIR_NAME),
    ("weather-api", layout.NEEDS_REVIEW_DIR_NAME),
    ("loyalty-api", layout.NEEDS_REVIEW_DIR_NAME),
    ("shipping-api", layout.UNSUPPORTED_DIR_NAME),
    ("returns-api", layout.UNSUPPORTED_DIR_NAME),
)
SHARED_FLOWS: tuple[str, ...] = ("common-auth", "common-logging")
# setup-existing-results: "8 of 12 proxies already have a .done marker from an earlier run".
EARLIER_DONE = 8

EXPORTS_NAME = "apigee-exports"
RESULTS_NAME = "a2m-out"
GOLDEN_NAME = "golden-recordings"
# setup-advanced: the prototype's Advanced values (mock backends on, 5 fix attempts, one ignored header).
ADVANCED_IGNORED_HEADER = "Authorization"
ADVANCED_FIX_ATTEMPTS = "5"
# setup-advanced has Claude usable: a stand-in key for this process only (never shown by the app), and a bare
# stand-in for the Anthropic SDK when it is not installed (the app only checks that it can be imported).
STAND_IN_KEY = "stand-in-key-for-tui-states"


def _write_exports(root: Path) -> Path:
    """The exports folder: one minimal folder bundle per prototype proxy and shared flow."""
    exports = root / EXPORTS_NAME
    for name, _bucket in PROXIES:
        bundle = exports / name / "apiproxy"
        bundle.mkdir(parents=True)
        (bundle / f"{name}.xml").write_text(
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<APIProxy revision="1" name="{name}"/>\n',
            encoding="utf-8",
        )
    for name in SHARED_FLOWS:
        bundle = exports / name / "sharedflowbundle"
        bundle.mkdir(parents=True)
        (bundle / f"{name}.xml").write_text(
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<SharedFlowBundle revision="1" name="{name}"/>\n',
            encoding="utf-8",
        )
    return exports


def _write_earlier_results(root: Path) -> Path:
    """A results folder an earlier run left: the a2m marker and the first proxies finished in their buckets."""
    out = root / RESULTS_NAME
    out.mkdir()
    layout.results_marker_path(out).write_text(layout.RESULTS_MARKER_TEXT, encoding="utf-8")
    for name, bucket in PROXIES[:EARLIER_DONE]:
        proxy_dir = layout.bucket_proxy_dir(out, bucket, name)
        proxy_dir.mkdir(parents=True)
        (proxy_dir / layout.DONE_MARKER_NAME).write_text(f"a2m finished {name}\n", encoding="utf-8")
    return out


def _setup_empty(root: Path) -> A2MApp:
    """CP3/CP4 setup-empty: the app just opened, nothing chosen yet (no fixture data needed)."""
    return A2MApp()


def _setup_ready(root: Path) -> A2MApp:
    """CP4/CP5 setup-ready: both folders valid (12 proxies, 2 shared flows; a new empty results folder), No AI
    chosen, Advanced closed."""
    exports = _write_exports(root)
    results = root / RESULTS_NAME
    results.mkdir()
    return A2MApp(exports=str(exports), results=str(results))


def _setup_invalid(root: Path) -> A2MApp:
    """CP4 setup-invalid: the results folder is inside the exports folder."""
    exports = _write_exports(root)
    return A2MApp(exports=str(exports), results=str(exports / "out"))


def _setup_existing_results(root: Path) -> A2MApp:
    """CP4 setup-existing-results: results from an earlier run (8 of 12 done); Resume or Force not picked yet."""
    exports = _write_exports(root)
    results = _write_earlier_results(root)
    return A2MApp(exports=str(exports), results=str(results))


def _setup_no_key(root: Path) -> A2MApp:
    """CP5 setup-no-key: both folders valid, Claude chosen without ANTHROPIC_API_KEY; message shown, Start disabled."""
    os.environ.pop(KEY_ENV, None)
    exports = _write_exports(root)
    results = root / RESULTS_NAME
    results.mkdir()
    return A2MApp(choices=SetupChoices(exports=str(exports), results=str(results), llm=LlmChoice.CLAUDE))


def _setup_advanced(root: Path) -> A2MApp:
    """CP5 setup-advanced: Claude usable, Advanced open with mock backends, a golden recordings folder, one
    ignored header and 5 AI fix attempts."""
    os.environ[KEY_ENV] = STAND_IN_KEY
    if SDK_MODULE not in sys.modules and importlib.util.find_spec(SDK_MODULE) is None:
        sys.modules[SDK_MODULE] = types.ModuleType(SDK_MODULE)
    exports = _write_exports(root)
    results = root / RESULTS_NAME
    results.mkdir()
    golden = root / GOLDEN_NAME
    golden.mkdir()
    choices = SetupChoices(
        exports=str(exports),
        results=str(results),
        llm=LlmChoice.CLAUDE,
        mock_backends=True,
        golden=str(golden),
        ignore_headers=ADVANCED_IGNORED_HEADER,
        max_fix_attempts=ADVANCED_FIX_ATTEMPTS,
    )
    return A2MApp(choices=choices, advanced_open=True)


# ---------------------------------------------------------------- run states (CP6)

# The prototype's run-progress finished list (ALL_PROXIES[0, 1, 6, 10, 2]) and the proxy building now.
RUN_FINISHED: tuple[tuple[str, str], ...] = (PROXIES[0], PROXIES[1], PROXIES[6], PROXIES[10], PROXIES[2])
RUN_CURRENT = "notifications-api"
RUN_PROGRESS_ELAPSED = 47.0  # "0:47"
RUN_STOPPED_ELAPSED = 41.0  # "0:41"
RUN_STOPPED_KEPT = 4
RUN_START_FAILED_MESSAGE = "Results folder is already in use by another a2m run."
# How long after the run screen opens the stop states press Stop (the replay takes a few milliseconds).
RUN_ACTION_DELAY = 1.0

# The stand-in child: prints the events file to stdout, then waits; SIGINT or SIGTERM prints a2m's
# 'stopped' line and exits 130 (the real child's exit). "startfail" prints one stderr line and exits 2.
# "slowstop" keeps "cleaning up" after SIGINT until SIGTERM (Force stop) arrives (then exits 143) or 30 s pass;
# "forceadvice" stops with a2m's --force advice (the run stopped before an earlier run's markers were cleared).
REPLAY_CHILD = """\
import json, signal, sys, time
mode, arg = sys.argv[1], sys.argv[2]
if mode == "startfail":
    sys.stderr.write(arg + "\\n")
    sys.exit(2)
stop = []
signal.signal(signal.SIGINT, lambda signum, _frame: stop.append(signum))
signal.signal(signal.SIGTERM, lambda signum, _frame: stop.append(signum))
with open(arg, encoding="utf-8") as events:
    for line in events:
        sys.stdout.write(line)
sys.stdout.flush()
while not stop:
    time.sleep(0.05)
cleanup_ends = time.monotonic() + 30.0  # bounded, so a capture that is never closed cannot leave it behind
while mode == "slowstop" and signal.SIGTERM not in stop and time.monotonic() < cleanup_ends:
    time.sleep(0.05)
if mode == "forceadvice":
    advice = "rerun with --force to redo every proxy"
else:
    advice = "rerun with --resume to continue"
code, name = (143, "SIGTERM") if signal.SIGTERM in stop else (130, "SIGINT")
print(json.dumps({"schema_version": 1, "kind": "stopped", "exit_code": code, "signal": name,
                  "advice": advice, "message": "a2m migrate: interrupted; " + advice}), flush=True)
sys.exit(code)
"""
# run-force-stop: the clean-up budget the run screen waits after Stop before it offers Force stop (the real one
# is minutes long; the state shortens it so the capture shows the offer).
RUN_FORCE_GRACE = 1.0


class FixedElapsedClock:
    """A clock whose first reading (the run's start) is 0 and every later one ``elapsed``."""

    def __init__(self, elapsed: float) -> None:
        self._elapsed = elapsed
        self._started = False

    def __call__(self) -> float:
        if not self._started:
            self._started = True
            return 0.0
        return self._elapsed


class RunStateApp(A2MApp):
    """The app on the setup screen for ``setup`` choices with the run screen for ``argv`` on top."""

    # A subclass outside a2m/tui would look for its own app.tcss next to this file; use the app's.
    CSS_PATH = str(Path(app_module.__file__).with_name(A2MApp.CSS_PATH))

    def __init__(
        self,
        argv: list[str],
        clock: FixedElapsedClock,
        *,
        setup: SetupChoices,
        then: Callable[[RunScreen], None] | None = None,
        stop_grace: float | None = None,
    ) -> None:
        super().__init__(choices=setup)
        self._run_argv = argv
        self._run_clock = clock
        self._then = then
        self._stop_grace = stop_grace

    def on_mount(self) -> None:
        # Textual runs A2MApp.on_mount (which opens the setup screen) after this one, so the run screen
        # is pushed once the setup screen is in place.
        self.call_later(self._open_run)

    def _open_run(self) -> None:
        if self._stop_grace is None:
            screen = RunScreen(self._run_argv, clock=self._run_clock)
        else:
            screen = RunScreen(self._run_argv, clock=self._run_clock, stop_grace=self._stop_grace)
        self.push_screen(screen)
        then = self._then
        if then is not None:
            self.set_timer(RUN_ACTION_DELAY, lambda: then(screen))


def _event(
    kind: EventKind,
    index: int = 0,
    name: str = "",
    *,
    step: Step | None = None,
    outcome: Outcome | None = None,
    bucket: str | None = None,
) -> str:
    event = ProgressEvent(kind, len(PROXIES), index, name, step=step, outcome=outcome, bucket=bucket)
    return json_line(event_fields(event))


def _run_events(finished: tuple[tuple[str, str], ...], current: str) -> list[str]:
    """run-started, then each finished proxy started and finished in its bucket, then ``current`` building."""
    lines = [_event(EventKind.RUN_STARTED)]
    for index, (name, bucket) in enumerate(finished, 1):
        lines.append(_event(EventKind.PROXY_STARTED, index, name))
        lines.append(_event(EventKind.PROXY_FINISHED, index, name, outcome=Outcome.FINISHED, bucket=bucket))
    index = len(finished) + 1
    lines.append(_event(EventKind.PROXY_STARTED, index, current))
    lines.append(_event(EventKind.STEP, index, current, step=Step.BUILD))
    return lines


def _run_app(
    root: Path,
    argv_tail: list[str],
    elapsed: float,
    then: Callable[[RunScreen], None] | None = None,
    *,
    stop_grace: float | None = None,
) -> A2MApp:
    exports = _write_exports(root)
    results = root / RESULTS_NAME
    results.mkdir()
    child = root / "replay_child.py"
    child.write_text(REPLAY_CHILD, encoding="utf-8")
    setup = SetupChoices(exports=str(exports), results=str(results))
    argv = [sys.executable, str(child), *argv_tail]
    return RunStateApp(argv, FixedElapsedClock(elapsed), setup=setup, then=then, stop_grace=stop_grace)


def _replay(root: Path, lines: list[str], mode: str = "replay") -> list[str]:
    events = root / "events.jsonl"
    events.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return [mode, str(events)]


def _run_progress(root: Path) -> A2MApp:
    """CP6 run-progress: 5 of the prototype's proxies finished (mixed buckets), notifications-api building."""
    return _run_app(root, _replay(root, _run_events(RUN_FINISHED, RUN_CURRENT)), RUN_PROGRESS_ELAPSED)


def _run_stop_confirm(root: Path) -> A2MApp:
    """CP6 run-stop-confirm: run-progress with Stop pressed, its confirm dialog open."""
    lines = _run_events(RUN_FINISHED, RUN_CURRENT)
    return _run_app(root, _replay(root, lines), RUN_PROGRESS_ELAPSED, then=lambda screen: screen.action_stop())


def _run_stopped(root: Path) -> A2MApp:
    """CP6 run-stopped: stopped after 4 proxies finished (kept), Resume shown."""
    finished = RUN_FINISHED[:RUN_STOPPED_KEPT]
    lines = _run_events(finished, RUN_FINISHED[RUN_STOPPED_KEPT][0])
    return _run_app(root, _replay(root, lines), RUN_STOPPED_ELAPSED, then=lambda screen: screen.stop_run())


def _run_stopping(root: Path) -> A2MApp:
    """CP6 run-stopping: Stop confirmed, a2m still cleaning up; the screen says it is waiting (no Force stop yet)."""
    lines = _run_events(RUN_FINISHED, RUN_CURRENT)
    argv = _replay(root, lines, "slowstop")
    return _run_app(root, argv, RUN_PROGRESS_ELAPSED, then=lambda screen: screen.stop_run())


def _run_force_stop(root: Path) -> A2MApp:
    """CP6 run-force-stop: a stop has outlasted its clean-up budget; Force stop is offered with its warning."""
    lines = _run_events(RUN_FINISHED, RUN_CURRENT)
    argv = _replay(root, lines, "slowstop")
    return _run_app(
        root, argv, RUN_PROGRESS_ELAPSED, then=lambda screen: screen.stop_run(), stop_grace=RUN_FORCE_GRACE
    )


def _stop_then_resume(screen: RunScreen) -> None:
    screen.stop_run()
    screen.set_timer(RUN_ACTION_DELAY, screen.action_resume)


def _run_force_resume_confirm(root: Path) -> A2MApp:
    """CP6 run-force-resume-confirm: stopped with a2m's --force advice; Resume pressed, its confirm dialog open."""
    finished = RUN_FINISHED[:RUN_STOPPED_KEPT]
    lines = _run_events(finished, RUN_FINISHED[RUN_STOPPED_KEPT][0])
    argv = _replay(root, lines, "forceadvice")
    return _run_app(root, argv, RUN_STOPPED_ELAPSED, then=_stop_then_resume)


def _run_start_failed(root: Path) -> A2MApp:
    """CP6 run-start-failed: the child exits with a usage error before any proxy ran."""
    return _run_app(root, ["startfail", RUN_START_FAILED_MESSAGE], 0.0)


STATES: dict[str, Callable[[Path], A2MApp]] = {
    "setup-empty": _setup_empty,
    "setup-ready": _setup_ready,
    "setup-invalid": _setup_invalid,
    "setup-existing-results": _setup_existing_results,
    "setup-no-key": _setup_no_key,
    "setup-advanced": _setup_advanced,
    "run-progress": _run_progress,
    "run-stop-confirm": _run_stop_confirm,
    "run-stopped": _run_stopped,
    "run-start-failed": _run_start_failed,
    "run-stopping": _run_stopping,
    "run-force-stop": _run_force_stop,
    "run-force-resume-confirm": _run_force_resume_confirm,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Open the a2m TUI in one named UI state (dev capture harness).")
    parser.add_argument("--state", required=True, choices=sorted(STATES), help="the ui_states name to show")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="a2m-") as root:
        app = STATES[args.state](Path(root))
        app.run()
    return app.return_code or 0


if __name__ == "__main__":
    sys.exit(main())
