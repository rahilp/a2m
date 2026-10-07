"""TUI CP6: watch progress and stop a run (checkpoint plan: tests/CP6.json in the TUI run folder).

High risk checkpoint, five code cases (the browser case, TUI-CP6-B01, is skipped per Ratchet's rules).

No earlier checkpoint fixed a public contract for the run screen (CP3-CP5 only built Setup), so this file
fixes it, the same way tests/tui/test_cp4_setup_folders.py and test_cp5_setup_options.py fixed Setup's own
ids:

* ``a2m.tui.run.RunScreen(argv: list[str], *, clock: Callable[[], float] | None = None)`` is a
  ``Screen[None]`` that, once mounted, runs ``argv`` as a real child process (environment inherited) and
  reads its stdout line by line as the ``--progress json`` event stream (schema: ``a2m/progress.py``).
  ``clock`` (default ``time.monotonic``) is read whenever the elapsed-time display refreshes; it is never
  read only when an event arrives, so elapsed time keeps moving between events.
* Resume relaunches the *same* ``argv`` with ``--resume`` or ``--force`` appended (whichever the ``stopped``
  event's own ``advice`` text names), never a command rebuilt from scratch.
* Stop sends SIGINT to the child; if it is still alive after a grace period, SIGTERM (never SIGKILL).
  Quitting the app while a child is alive does the same, after the same confirm step.
* Widget ids this file fixes: ``#run-counts`` ("N of M proxies"), ``#run-current`` (current proxy name and
  step), ``#run-elapsed`` (elapsed time), ``#run-progress`` (a ``ProgressBar``), ``#run-finished`` (a
  ``ListView``, newest proxy at the top, each row naming the proxy and showing its bucket), ``#stop``,
  ``#run-message`` (the one-line message when the child exited before any ``run-started`` event),
  ``#back-to-setup``, ``#resume``. Both the Stop and the quit-while-running confirm dialogs are the same
  reusable "modal confirm" component (DESIGN.md Components, Modal confirm): ``#confirm-primary`` (the
  destructive action) and ``#confirm-cancel`` (the safer action, focused by default).

Every case below drives the real ``RunScreen`` headlessly through Textual's ``App.run_test()``/``Pilot``,
pushed directly onto the real ``a2m.tui.app.A2MApp`` (so Header/Footer/theme/global keys are the real
app's, as the checkpoint plan's own strategy calls for: "Textual App.run_test()/Pilot against the run
screen"). ``a2m.tui.run``/``a2m.tui.app`` are imported lazily *inside* each test's body, never at module
import time, so a missing module fails only that one test.

Four of the five cases are wired to ``tests/tui/fixtures/fake_child.py``, a tiny real subprocess (never
imported, only ever run with ``sys.executable``) that replays ``tests/tui/fixtures/events_12.jsonl`` -- a
canned ``run-started(total=12)`` plus a handful of proxy events with mixed outcomes/buckets, built with
``a2m.progress``'s own types so it matches the real wire schema -- and can be told to pause after a given
0-based line of that file, to ignore its first SIGINT (to exercise the SIGTERM escalation), and to print a
real ``stopped`` event on SIGINT/SIGTERM. The fifth (TUI-CP6-T04) runs a real ``a2m.cli.main`` migration
over two real fixture bundles in a child process, through a small blocking stage (the same technique as
tests/test_tui_cp2_progress_json.py's ``BLOCK_SCRIPT``), so Resume is checked against the real engine's
resume semantics, not a replayed fixture.

Every PID a test starts is recorded in the ``started_pids`` fixture and force-killed at teardown if still
alive; no test ever signals a PID it did not start itself.
"""

from __future__ import annotations

import re
import sys
import time
from collections.abc import Sequence
from pathlib import Path

import pytest
from conftest import write_bundle_dir

from tui.screen import _normalize_ws, _run, _screen_rows, _screen_text, _settle

REPO = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
FAKE_CHILD = FIXTURES / "fake_child.py"
EVENTS_12 = FIXTURES / "events_12.jsonl"

# 0-based line indices into events_12.jsonl (see that file's own header and the generator noted there):
# line 9 is delta's (proxy 4 of 12) step event, printed right after it started and before it finishes;
# line 11 is echo's (proxy 5 of 12) proxy-started event, printed once alpha/beta/gamma/delta (4 proxies)
# have all already finished and before echo finishes.
PAUSE_AFTER_DELTA_STEP = 9
PAUSE_AFTER_ECHO_STARTED = 11


# ---------------------------------------------------------------- process helpers (own copy: see
# tests/test_cp7_verify.py for the same small helpers, not shared across files by convention there either)


def live(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace").rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state != "Z"


def wait_dead(pids: Sequence[int], timeout: float = 20.0) -> list[int]:
    """Blocking poll (only safe to call outside an async Textual body): the still-alive subset of ``pids``."""
    deadline = time.monotonic() + timeout
    alive = [p for p in pids if live(p)]
    while alive and time.monotonic() < deadline:
        time.sleep(0.05)
        alive = [p for p in alive if live(p)]
    return alive


async def async_wait_dead(pilot: object, pids: Sequence[int], timeout: float = 20.0) -> list[int]:
    """Like :func:`wait_dead`, but yields to the Textual app's own event loop between checks (``pilot.pause``)
    instead of a blocking sleep, so an implementation that schedules its SIGINT->SIGTERM escalation with an
    async timer actually gets to run while this test waits for it."""
    deadline = time.monotonic() + timeout
    alive = [p for p in pids if live(p)]
    while alive and time.monotonic() < deadline:
        await pilot.pause(0.1)  # type: ignore[attr-defined]
        alive = [p for p in alive if live(p)]
    return alive


def kill_own(pids: Sequence[int]) -> None:
    """SIGKILL only PIDs a test itself recorded, if still alive."""
    import os
    import signal

    for pid in pids:
        if live(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


@pytest.fixture
def started_pids() -> list[int]:
    """Every PID a test starts goes here; force-killed (only if still alive) once the test ends, as a
    safety net on top of whatever the test itself already asserted about that PID being gone."""
    pids: list[int] = []
    yield pids
    kill_own(pids)


def wait_for_pid(ready: Path, timeout: float = 10.0) -> int:
    """``fake_child.py`` writes its own pid to ``ready`` before doing anything else; wait for that."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready.is_file():
            text = ready.read_text(encoding="utf-8").strip()
            if text:
                return int(text)
        time.sleep(0.02)
    raise AssertionError(f"{ready} never appeared; the run screen never started its child process")


async def wait_until(pilot: object, predicate: object, *, timeout: float = 10.0, interval: float = 0.05) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return
        await pilot.pause(interval)  # type: ignore[attr-defined]
    raise AssertionError("condition was never met in time")


def first_row_containing(app: object, text: str) -> int:
    """The smallest row number whose rendered text contains ``text`` (case-insensitive)."""
    rows = _screen_rows(app)
    lower = text.lower()
    matches = [y for y, line in rows.items() if lower in line.lower()]
    assert matches, (text, rows)
    return min(matches)


def row_marker(app: object, row: int, name: str) -> str:
    """Row ``row``'s text with ``name`` and all whitespace/border glyphs removed: whatever is left is the
    bucket marker/label this row shows for that proxy (DESIGN.md: "name + bucket marker/label only")."""
    rows = _screen_rows(app)
    text = rows.get(row, "")
    stripped = re.sub(re.escape(name), "", text, flags=re.IGNORECASE)
    return _normalize_ws(stripped)


def fake_child_argv(*, pause_after: int, pause_seconds: float, ready: Path, advice: str = "", message: str = "",
                     ignore_first_sigint: bool = False) -> list[str]:
    argv = [
        sys.executable, str(FAKE_CHILD), str(ready), "replay", str(EVENTS_12),
        "--pause-after", str(pause_after), "--pause-seconds", str(pause_seconds),
        "--advice", advice, "--message", message,
    ]
    if ignore_first_sigint:
        argv.append("--ignore-first-sigint")
    return argv


# ---------------------------------------------------------------- TUI-CP6-T01


def test_TUI_CP6_T01_progress_screen_shows_bar_current_step_elapsed_and_growing_finished_list(
    tmp_path: Path, started_pids: list[int]
) -> None:
    """[TUI-CP6-T01] Once delta (proxy 4 of 12) is the one running: the screen reads "4 of 12 proxies" (and
    the ProgressBar itself reports total=12, progress=4), delta's name is shown as the current proxy, the
    finished list already holds alpha/beta/gamma with gamma (finished last) nearest the top and beta's
    different bucket (needs-review) shown with a different marker than alpha/gamma's shared one (verified),
    and the elapsed time changes when the injected clock moves forward with no new event arriving, proving
    it is the clock driving it and not the event stream."""
    ready = tmp_path / "ready"
    clock_box = [1_000.0]

    async def body() -> None:
        from textual.widgets import Button, ProgressBar

        from a2m.tui.app import A2MApp
        from a2m.tui.run import RunScreen

        app = A2MApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            argv = fake_child_argv(pause_after=PAUSE_AFTER_DELTA_STEP, pause_seconds=6.0, ready=ready)
            await app.push_screen(RunScreen(argv, clock=lambda: clock_box[0]))
            await _settle(pilot)

            pid = wait_for_pid(ready)
            started_pids.append(pid)

            await wait_until(pilot, lambda: "delta" in _normalize_ws(_screen_text(app)).lower())

            flat = _normalize_ws(_screen_text(app))
            assert _normalize_ws("4 of 12 proxies") in flat, flat
            assert "delta" in flat.lower(), flat

            bar = app.screen.query_one("#run-progress", ProgressBar)
            assert bar.total == 12, bar.total
            assert bar.progress == 4, bar.progress

            y_alpha = first_row_containing(app, "alpha")
            y_beta = first_row_containing(app, "beta")
            y_gamma = first_row_containing(app, "gamma")
            assert y_gamma < y_beta < y_alpha, (y_gamma, y_beta, y_alpha)  # newest (gamma) nearest the top

            marker_alpha = row_marker(app, y_alpha, "alpha")
            marker_beta = row_marker(app, y_beta, "beta")
            marker_gamma = row_marker(app, y_gamma, "gamma")
            assert marker_alpha != "", "a finished row must show something besides the bare proxy name"
            assert marker_alpha == marker_gamma, (marker_alpha, marker_gamma)  # same bucket (verified)
            assert marker_alpha != marker_beta, (marker_alpha, marker_beta)  # different bucket (needs-review)

            # Button import above is also used here: Stop must be present and enabled while a run is going.
            assert app.screen.query_one("#stop", Button).disabled is False

            before = _normalize_ws(_screen_text(app))
            clock_box[0] += 65.0  # no new event arrives; only the clock moves
            await wait_until(pilot, lambda: _normalize_ws(_screen_text(app)) != before, timeout=8.0)
            after = _normalize_ws(_screen_text(app))
            assert after != before, "the elapsed display must move on its own, driven by the clock"

    _run(body)


# ---------------------------------------------------------------- TUI-CP6-T02


START_FAIL_MESSAGE = "a2m migrate: usage error: results folder is in use by another a2m run (see 'a2m --help')"


def test_TUI_CP6_T02_a_start_failure_shows_the_exact_message_with_a_way_back(tmp_path: Path) -> None:
    """[TUI-CP6-T02] A child that exits 2 with one usage-error line before any run-started event shows that
    exact line unchanged, a Back to setup action, and never an "N of M" progress bar."""
    ready = tmp_path / "ready"

    async def body() -> None:
        from a2m.tui.app import A2MApp
        from a2m.tui.run import RunScreen

        app = A2MApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            argv = [sys.executable, str(FAKE_CHILD), str(ready), "startfail", START_FAIL_MESSAGE]
            screen_below = app.screen
            await app.push_screen(RunScreen(argv))
            await _settle(pilot)

            await wait_until(pilot, lambda: _normalize_ws(START_FAIL_MESSAGE) in _normalize_ws(_screen_text(app)))

            flat = _normalize_ws(_screen_text(app))
            assert _normalize_ws(START_FAIL_MESSAGE) in flat, flat
            assert not re.search(r"\d+\s*of\s*\d+", _screen_text(app)), _screen_text(app)

            await pilot.click("#back-to-setup")
            await _settle(pilot)
            assert app.screen is screen_below, "Back to setup must return to the screen underneath"

    _run(body)


# ---------------------------------------------------------------- TUI-CP6-T03


STOP_ADVICE = "rerun with --resume to continue"
STOP_MESSAGE = "a2m migrate: stopped by SIGINT; rerun with --resume to continue"


def test_TUI_CP6_T03_stop_confirms_then_stops_and_offers_resume(tmp_path: Path, started_pids: list[int]) -> None:
    """[TUI-CP6-T03] Pressing Stop shows a confirmation while the child is still alive and unsignalled;
    confirming sends SIGINT, the child exits, and the screen then names 4 (alpha/beta/gamma/delta, every
    proxy already finished before the stop) as finished, and offers Resume."""
    ready = tmp_path / "ready"

    async def body() -> None:
        from textual.widgets import Button

        from a2m.tui.app import A2MApp
        from a2m.tui.run import RunScreen

        app = A2MApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            argv = fake_child_argv(
                pause_after=PAUSE_AFTER_ECHO_STARTED, pause_seconds=60.0, ready=ready,
                advice=STOP_ADVICE, message=STOP_MESSAGE,
            )
            await app.push_screen(RunScreen(argv))
            await _settle(pilot)

            pid = wait_for_pid(ready)
            started_pids.append(pid)

            await wait_until(pilot, lambda: "echo" in _normalize_ws(_screen_text(app)).lower())

            await pilot.press("s")
            await _settle(pilot)
            assert live(pid), "the child must still be running while Stop is only being confirmed"
            app.screen.query_one("#confirm-cancel")
            confirm = app.screen.query_one("#confirm-primary", Button)

            await pilot.click(confirm)
            await _settle(pilot)

            assert await async_wait_dead(pilot, [pid]) == [], "the child must be gone once Stop is confirmed"

            await wait_until(pilot, lambda: "resume" in _screen_text(app).lower())
            flat = _normalize_ws(_screen_text(app))
            assert "4" in flat, flat
            assert "finished" in flat.lower(), flat
            app.screen.query_one("#resume", Button)

    _run(body)


# ---------------------------------------------------------------- TUI-CP6-T04


RESUME_BLOCK_SCRIPT = """\
import sys, time
from pathlib import Path
from a2m.cli import main
from a2m.engine import default_stages

ready, exports, out, second_name = sys.argv[1:5]
extra = sys.argv[5:]
stages = list(default_stages())


class Block:
    __name__ = "resume-block-stage"

    def __call__(self, proxy):
        if proxy.name == second_name and "--resume" not in extra and "--force" not in extra:
            Path(ready).write_text("blocked", encoding="utf-8")
            time.sleep(120)


sys.exit(main(
    ["migrate", exports, "--out", out, "--llm", "none", "--no-runtime", "--progress", "json", *extra],
    stages=[stages[0], stages[1], Block(), *stages[2:]],
))
"""


def _tree_bytes(folder: Path) -> dict[str, bytes]:
    if not folder.exists():
        return {}
    return {str(p.relative_to(folder)): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


def _bucket_of(out: Path, name: str) -> str | None:
    from a2m import layout as a2m_layout

    found = [b for b in a2m_layout.BUCKET_DIR_NAMES if (out / b / name).is_dir()]
    return found[0] if found else None


def test_TUI_CP6_T04_resume_continues_without_redoing_the_finished_proxy(
    tmp_path: Path, started_pids: list[int]
) -> None:
    """[TUI-CP6-T04] A real migration over alpha and beta, stopped (via the run screen's own Stop) once
    alpha's .done marker is written and beta's generation is blocked: pressing Resume relaunches the run,
    alpha is never reprocessed (its output files stay byte-for-byte the same) and beta ends up finished
    too."""
    exports = tmp_path / "exports"
    write_bundle_dir(exports, "alpha")
    write_bundle_dir(exports, "beta")
    out = tmp_path / "out"
    ready = tmp_path / "blocked"
    script = tmp_path / "resume_block.py"
    script.write_text(RESUME_BLOCK_SCRIPT, encoding="utf-8")

    async def body() -> None:
        from textual.widgets import Button

        from a2m.tui.app import A2MApp
        from a2m.tui.run import RunScreen

        app = A2MApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            argv = [sys.executable, str(script), str(ready), str(exports), str(out), "beta"]
            await app.push_screen(RunScreen(argv))
            await _settle(pilot)

            deadline = time.monotonic() + 30.0
            while not ready.is_file() and time.monotonic() < deadline:
                await pilot.pause(0.1)
            assert ready.is_file(), "beta's stage never blocked; alpha may not have finished either"

            alpha_bucket = _bucket_of(out, "alpha")
            assert alpha_bucket is not None, "alpha must already be finished and bucketed before Stop"
            alpha_before = _tree_bytes(out / alpha_bucket / "alpha")
            assert alpha_before, "alpha must have written output files before Stop"

            await pilot.press("s")
            await _settle(pilot)
            await pilot.click("#confirm-primary")
            await _settle(pilot)

            await wait_until(pilot, lambda: "resume" in _screen_text(app).lower(), timeout=30.0)
            await pilot.click(app.screen.query_one("#resume", Button))
            await _settle(pilot)

            resume_deadline = time.monotonic() + 60.0
            while time.monotonic() < resume_deadline:
                if (out / alpha_bucket / "alpha" / ".done").is_file() and _bucket_of(out, "beta") is not None:
                    beta_bucket = _bucket_of(out, "beta")
                    if (out / beta_bucket / "beta" / ".done").is_file():
                        break
                await pilot.pause(0.2)
            else:
                raise AssertionError("beta never finished after Resume")

            assert _tree_bytes(out / alpha_bucket / "alpha") == alpha_before, "alpha must not be reprocessed"

    _run(body)


# ---------------------------------------------------------------- TUI-CP6-T05


STOP_GRACE_FOR_TEST = 1.0


def test_TUI_CP6_T05_quitting_mid_run_confirms_first_and_never_leaves_an_orphan(
    tmp_path: Path, started_pids: list[int]
) -> None:
    """[TUI-CP6-T05] After Stop is confirmed, the screen sends SIGINT once and never escalates on its own:
    against a child that ignores its first SIGINT, no SIGTERM is sent while the injected ``stop_grace``
    budget (RunScreen(..., stop_grace=...)) is still running, and Force stop is not even offered yet. Once
    that budget is over, Force stop appears, and only pressing it (the ``f`` key) sends SIGTERM -- never
    SIGKILL -- after which the child is gone, the screen shows the stopped state, and no orphan remains."""
    ready = tmp_path / "ready"

    async def body() -> None:
        from textual.widgets import Button

        from a2m.tui.app import A2MApp
        from a2m.tui.run import RunScreen

        app = A2MApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            argv = fake_child_argv(
                pause_after=PAUSE_AFTER_ECHO_STARTED, pause_seconds=90.0, ready=ready, ignore_first_sigint=True,
            )
            await app.push_screen(RunScreen(argv, stop_grace=STOP_GRACE_FOR_TEST))
            await _settle(pilot)

            pid = wait_for_pid(ready)
            started_pids.append(pid)

            await wait_until(pilot, lambda: "echo" in _normalize_ws(_screen_text(app)).lower())

            await pilot.press("s")
            await _settle(pilot)
            confirm = app.screen.query_one("#confirm-primary", Button)
            await pilot.click(confirm)
            await _settle(pilot)

            assert live(pid), "SIGINT alone must not kill a child that ignores its first SIGINT"
            assert "force stop" not in _screen_text(app).lower(), (
                "Force stop must not be offered before the stop_grace budget is over"
            )

            # The budget running out must never itself send a signal; it only reveals Force stop.
            await wait_until(pilot, lambda: "force stop" in _screen_text(app).lower(), timeout=STOP_GRACE_FOR_TEST + 10.0)
            assert live(pid), "the stop_grace budget running out must not auto-escalate to SIGTERM on its own"

            await pilot.press("f")
            await _settle(pilot)

            assert await async_wait_dead(pilot, [pid], timeout=30.0) == [], (
                "Force stop must send SIGTERM (never SIGKILL) and leave no orphan running"
            )

            await wait_until(pilot, lambda: "stopped" in _screen_text(app).lower())
            assert "stopped" in _screen_text(app).lower()

    _run(body)
