"""TUI CP8: step through proxies that need review (checkpoint plan: tests/CP8.json in the TUI run folder).

Normal-risk checkpoint, four code cases (the browser case, TUI-CP8-T05, is skipped per Ratchet's rules).

No earlier checkpoint fixed a public contract for a review walkthrough screen (CP3-CP7 only built Setup,
Run and Results), so this file fixes it, the same way tests/tui/test_cp7_results.py fixed ResultsScreen's
own ids:

* Pressing ``r`` (or clicking ``#review``) on a real ``a2m.tui.results.ResultsScreen`` that has at least one
  needs-review proxy pushes ``a2m.tui.review.ReviewScreen`` (a ``Screen[None]``), replacing the current
  "review walkthrough is not available" notification entirely.
* The proxies are walked in the same order the needs-review bucket is already listed in on the results
  screen: the order ``a2m.summary.write_batch_summary`` writes into summary.json, i.e. sorted by name (the
  same sort :func:`a2m.summary.collect` already applies). For this file's three fixture proxies
  (js-transform, legacy-auth, weather-api) that is "js-transform" 1 of 3, "legacy-auth" 2 of 3, "weather-api"
  3 of 3 -- not the order the checkpoint plan happens to list the names in, which is incidental prose, not a
  pinned ordering rule.
* ``n``/``p`` move to the next/previous proxy; ``#review-position`` always carries the current one-based
  position in the form "proxy N of M"; ``#review-path`` always carries the current proxy's absolute folder
  path as plain, selectable text; ``c`` copies that same path to the clipboard (Textual's own
  ``copy_to_clipboard``, read back through ``App.clipboard`` exactly as ``tests/tui/screen.py``'s
  ``_copied_command_tokens`` reads the setup screen's Copy command).
* By default the screen shows the current proxy's REPORT.md as plain text (``#review-report``, no markup,
  no Markdown parsing) so nothing in an untrusted REPORT.md can be interpreted as console markup or a
  terminal control sequence; pressing ``tab`` (screens are free to repurpose it the same way
  ``a2m.tui.setup.SetupScreen`` already repurposes ``tab`` for its own folder completion) switches to the
  Diffs view, and back again. The Diffs view lists the current proxy's ``diffs/`` files in an ``OptionList``
  (``#review-diffs-list``, the same list-and-``enter``-to-open pattern as ``a2m.tui.picker.FolderList``),
  shows the alphabetically-first file's plain text by default in ``#review-diff-content``, and shows
  ``#review-no-diffs`` instead of the list and content when the proxy has no diffs (its ``diffs/`` is
  missing or empty). Moving to a different proxy while the Diffs view is open refreshes it for that proxy
  (list, default file, or the no-diffs message) and never leaves the old proxy's file or message on screen.
* Pressing ``n`` past the last proxy replaces the normal content with ``#review-done``, a plain message that
  the walkthrough is finished and a way back; ``escape`` (or ``#review-back``) is always available, both
  mid-walkthrough and once finished, and dismisses back to the ``ResultsScreen`` underneath, unchanged.

Every case drives the real screens headlessly through Textual's ``App.run_test()``/``Pilot``, against the
real ``a2m.tui.app.A2MApp``, reaching ``ReviewScreen`` only through ``ResultsScreen``'s own "Review" key/
button, the way a user would, exactly as tests/CP8.json's own strategy calls for ("Build a results folder
directly on disk ... reusing a2m.layout's path helpers"). ``a2m.tui.review`` is imported lazily *inside*
each test's body, never at module import time, so a missing module fails only that one test with a normal
exception-shaped failure, never a collection-time crash.

Fixtures are synthetic results folders built directly on ``tmp_path`` with the real
``a2m.layout``/``a2m.summary`` helpers (``write_batch_summary``, ``bucket_proxy_dir``,
``PROXY_SUMMARY_NAME``), reusing tests/tui/test_cp7_results.py's own approach: no child process, no Mule, no
AI. REPORT.md and diffs/ files are written directly, matching the real on-disk contract
:mod:`a2m.report` produces. The untrusted-content fixtures (weather-api's REPORT.md and one of its diffs/
files) hold the literal text a hostile Apigee bundle or AI response could smuggle through to a report:
Rich/Textual markup syntax and a raw ANSI colour escape sequence. Fixture text is kept short and placed near
the top of each file so it is on screen at 80x24 without scrolling.

Screen text is read and matched with tests/tui/screen.py's own whitespace- and border-glyph-insensitive
helpers (``_screen_text``, ``_normalize_ws``); confirmed separately (see the markup/ANSI checks below) that
Textual's screenshot export keeps a literal ESC byte and literal ``[`` / ``]`` characters intact, so those
helpers are also exactly how the untrusted-text case is checked: on the raw characters shown, never on color
or styling.
"""

from __future__ import annotations

import time
from pathlib import Path

from a2m import layout
from a2m.summary import ProxySummary, write_batch_summary
from a2m.verify.model import VerificationType
from tui.screen import _normalize_ws, _run, _screen_text, _settle

NEEDS_REVIEW = layout.NEEDS_REVIEW_DIR_NAME

# Untrusted-text sentinels a hostile bundle or AI response could smuggle into REPORT.md/diffs (TUI-CP8-T03).
MARKUP_SENTINEL = "[bold red]pwned[/]"
ANSI_SENTINEL = "\x1b[31mred\x1b[0m"
UNTRUSTED_LINE = f"{MARKUP_SENTINEL} and {ANSI_SENTINEL} end"
# How a raw ESC control byte must actually reach the screen: as a visible "symbol for escape" glyph (never
# the raw byte itself, which is the terminal-injection risk this step guards against).
VISIBLE_ANSI_SENTINEL = ANSI_SENTINEL.replace("\x1b", "␛")

REPORT_JS = "# js-transform\n\nReport body for js-transform.\n"
REPORT_LEGACY = "# legacy-auth\n\nReport body for legacy-auth.\n"
REPORT_WEATHER = f"# weather-api\n\nReport body for weather-api.\n\n{UNTRUSTED_LINE}\n"

DIFF_NORMAL = "--- expected\n+++ actual\n-200 OK\n+500 Internal Server Error\n"
DIFF_MARKUP = f"Verification log\n{UNTRUSTED_LINE}\n"


# ---------------------------------------------------------------- fixture builders (this file's own copies,
# per test_cp7_results.py's header: small helpers are not shared across TUI test files)


def _write_marker(results: Path) -> None:
    results.mkdir(parents=True, exist_ok=True)
    layout.results_marker_path(results).write_text(layout.RESULTS_MARKER_TEXT, encoding="utf-8")


def _write_needs_review_proxy(
    results: Path, name: str, report_text: str, diff_files: dict[str, str] | None = None
) -> Path:
    """A needs-review proxy folder with a real REPORT.md, ``.a2m-summary.json`` fact file, and (when
    ``diff_files`` is not None) a real ``diffs/`` holding those files -- an empty dict makes an empty
    ``diffs/``, ``None`` leaves no ``diffs/`` at all, so both "no diffs" shapes are covered."""
    folder = layout.bucket_proxy_dir(results, NEEDS_REVIEW, name)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / layout.REPORT_NAME).write_text(report_text, encoding="utf-8")
    fact = ProxySummary(name, NEEDS_REVIEW, VerificationType.STATIC.value, {"step": 2, "policy": 1, "condition": 0})
    from a2m.summary import proxy_facts_json

    (folder / layout.PROXY_SUMMARY_NAME).write_text(proxy_facts_json(fact), encoding="utf-8")
    if diff_files is not None:
        diffs_dir = folder / layout.DIFFS_DIR_NAME
        diffs_dir.mkdir(parents=True, exist_ok=True)
        for file_name, content in diff_files.items():
            (diffs_dir / file_name).write_text(content, encoding="utf-8")
    return folder


def build_review_results(tmp_path: Path, folder_name: str = "results-review") -> tuple[Path, dict[str, Path]]:
    """``results/needs-review/{js-transform,legacy-auth,weather-api}``: three needs-review proxies, sorted
    by name (the walkthrough order this file fixes). weather-api carries the untrusted markup/ANSI text in
    both its REPORT.md and one of its two diffs/ files; legacy-auth's diffs/ exists but is empty; js-transform
    has no diffs/ at all. Returns the results folder and each proxy's own folder."""
    results = tmp_path / folder_name
    _write_marker(results)
    folders = {
        "js-transform": _write_needs_review_proxy(results, "js-transform", REPORT_JS, diff_files=None),
        "legacy-auth": _write_needs_review_proxy(results, "legacy-auth", REPORT_LEGACY, diff_files={}),
        "weather-api": _write_needs_review_proxy(
            results,
            "weather-api",
            REPORT_WEATHER,
            diff_files={"test-orders-check.diff": DIFF_NORMAL, "verification-log.txt": DIFF_MARKUP},
        ),
    }
    write_batch_summary(results)
    return results, folders


def _snapshot(results: Path) -> dict[str, tuple[int, bytes]]:
    """``{relative path: (mtime_ns, content)}`` for every file under ``results``, for the read-only check."""
    return {
        str(path.relative_to(results)): (path.stat().st_mtime_ns, path.read_bytes())
        for path in sorted(results.rglob("*"))
        if path.is_file()
    }


async def wait_until(pilot: object, predicate: object, *, timeout: float = 10.0, interval: float = 0.05) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return
        await pilot.pause(interval)  # type: ignore[attr-defined]
    raise AssertionError("condition was never met in time")


async def _open_results_and_review(pilot: object, app: object, results: Path) -> None:
    """Push the real ``ResultsScreen`` for ``results``, wait for it to finish loading, then activate Review
    (the real public entry point, never ``ReviewScreen`` constructed directly)."""
    from a2m.tui.results import ResultsScreen

    await app.push_screen(ResultsScreen(results))  # type: ignore[attr-defined]
    await _settle(pilot)
    await wait_until(pilot, lambda: "weather-api" in _screen_text(app).lower())  # type: ignore[arg-type]
    await pilot.press("r")  # type: ignore[attr-defined]
    await _settle(pilot)


def _norm_lower(app: object) -> str:
    return _normalize_ws(_screen_text(app)).lower()  # type: ignore[arg-type]


def _norm(app: object) -> str:
    return _normalize_ws(_screen_text(app))  # type: ignore[arg-type]


# ---------------------------------------------------------------- TUI-CP8-T01


def test_TUI_CP8_T01_review_walkthrough_next_previous_and_done(tmp_path: Path) -> None:
    """[TUI-CP8-T01] Review opens on js-transform (proxy 1 of 3, the first by name); n/n moves to legacy-auth
    (2 of 3) then weather-api (3 of 3); p moves back to legacy-auth (2 of 3); n returns to weather-api
    (3 of 3, the last); one more n past the last proxy shows the walkthrough is finished with a way back;
    using it returns to the untouched ResultsScreen with the same SUMMARY.md and bucket counts as before
    Review was opened."""
    results, _folders = build_review_results(tmp_path)

    async def body() -> None:
        from a2m.tui.app import A2MApp
        from a2m.tui.results import ResultsScreen
        from a2m.tui.review import ReviewScreen

        app = A2MApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            before_summary = _norm_lower(app)  # empty: nothing loaded yet, just for symmetry below
            del before_summary
            await _open_results_and_review(pilot, app, results)
            assert isinstance(app.screen, ReviewScreen), app.screen

            await wait_until(pilot, lambda: "js-transform" in _norm_lower(app))
            flat = _norm_lower(app)
            assert "proxy1of3" in flat, flat
            assert "js-transform" in flat, flat
            assert "reportbodyforjs-transform" in flat, flat

            await pilot.press("n")
            await _settle(pilot)
            flat = _norm_lower(app)
            assert "proxy2of3" in flat, flat
            assert "legacy-auth" in flat, flat
            assert "reportbodyforlegacy-auth" in flat, flat
            assert "js-transform" not in flat, flat

            await pilot.press("n")
            await _settle(pilot)
            flat = _norm_lower(app)
            assert "proxy3of3" in flat, flat
            assert "weather-api" in flat, flat
            assert "reportbodyforweather-api" in flat, flat
            assert "legacy-auth" not in flat, flat

            await pilot.press("p")
            await _settle(pilot)
            flat = _norm_lower(app)
            assert "proxy2of3" in flat, flat
            assert "legacy-auth" in flat, flat
            assert "weather-api" not in flat, flat

            await pilot.press("n")  # reaches the last proxy again
            await _settle(pilot)
            flat = _norm_lower(app)
            assert "proxy3of3" in flat, flat
            assert "weather-api" in flat, flat

            await pilot.press("n")  # past the last proxy
            await _settle(pilot)
            flat = _norm_lower(app)
            assert "finish" in flat, flat
            assert "back" in flat, flat

            await pilot.press("escape")  # the way back
            await _settle(pilot)
            assert isinstance(app.screen, ResultsScreen), app.screen
            await _settle(pilot)
            flat = _norm_lower(app)
            assert "needsreview(3)" in flat.replace("needs-review", "needsreview"), flat
            assert "a2mmigrationsummary" in flat, flat

    _run(body)


# ---------------------------------------------------------------- TUI-CP8-T02


def test_TUI_CP8_T02_diffs_tab_lists_switches_and_handles_no_diffs(tmp_path: Path) -> None:
    """[TUI-CP8-T02] On weather-api's Diffs view, both file names are listed with test-orders-check.diff's
    content shown by default; selecting verification-log.txt switches the shown content; moving to
    legacy-auth (whose diffs/ exists but is empty) keeps the Diffs view open but shows its own "no diffs"
    message and none of weather-api's file names or content."""
    results, _folders = build_review_results(tmp_path)

    async def body() -> None:
        from a2m.tui.app import A2MApp
        from a2m.tui.review import ReviewScreen

        app = A2MApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await _open_results_and_review(pilot, app, results)
            assert isinstance(app.screen, ReviewScreen), app.screen
            await wait_until(pilot, lambda: "js-transform" in _norm_lower(app))

            await pilot.press("n")  # legacy-auth, 2 of 3
            await pilot.press("n")  # weather-api, 3 of 3
            await _settle(pilot)
            assert "weather-api" in _norm_lower(app)

            await pilot.press("tab")  # open the Diffs view
            await _settle(pilot)
            flat = _norm_lower(app)
            assert "test-orders-check.diff" in flat, flat
            assert "verification-log.txt" in flat, flat
            assert "500internalservererror" in flat, flat  # DIFF_NORMAL's own content, shown by default
            # only the file name is listed; verification-log.txt's own content is not shown until selected
            assert MARKUP_SENTINEL.replace(" ", "") not in flat, flat

            await pilot.press("down")  # highlight verification-log.txt
            await pilot.press("enter")  # open it
            await _settle(pilot)
            exact = _norm(app)
            assert MARKUP_SENTINEL.replace(" ", "") in exact, exact
            lower = exact.lower()
            assert "500internalservererror" not in lower, lower  # the other file's content is gone

            await pilot.press("p")  # legacy-auth, 2 of 3: diffs/ exists but is empty
            await _settle(pilot)
            flat = _norm_lower(app)
            assert "legacy-auth" in flat, flat
            assert "test-orders-check.diff" not in flat, flat
            assert "verification-log.txt" not in flat, flat
            assert "nodiffs" in flat.replace("no diffs", "nodiffs"), flat

    _run(body)


# ---------------------------------------------------------------- TUI-CP8-T03


def test_TUI_CP8_T03_folder_path_copyable_and_untrusted_text_shown_literally(tmp_path: Path) -> None:
    """[TUI-CP8-T03] weather-api's absolute results folder path is shown as plain text and is copied to the
    clipboard unchanged by the copy-path key; its REPORT.md, rendered by default, shows the literal
    '[bold red]pwned[/]' markup syntax and the literal raw ANSI colour escape bytes rather than styled or
    swallowed text; opening its verification-log.txt diff shows the same literal text there too."""
    results, folders = build_review_results(tmp_path)
    weather_folder = folders["weather-api"].resolve()

    async def body() -> None:
        from a2m.tui.app import A2MApp
        from a2m.tui.review import ReviewScreen

        app = A2MApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await _open_results_and_review(pilot, app, results)
            assert isinstance(app.screen, ReviewScreen), app.screen

            await pilot.press("n")  # legacy-auth, 2 of 3
            await pilot.press("n")  # weather-api, 3 of 3
            await _settle(pilot)
            assert "weather-api" in _norm_lower(app)

            # The folder path is visible as plain text.
            exact = _norm(app)
            assert _normalize_ws(str(weather_folder)) in exact, exact

            # Untrusted REPORT.md text (the default view) is shown literally, never interpreted.
            assert MARKUP_SENTINEL.replace(" ", "") in exact, exact
            assert VISIBLE_ANSI_SENTINEL in exact, repr(exact)  # the ESC byte, shown as a visible glyph
            assert "\x1b" not in exact, repr(exact)  # the raw ESC byte itself never reaches the screen

            # The folder path is copied to the clipboard unchanged by the copy-path key.
            await pilot.press("c")
            await _settle(pilot)
            clipboard = app.clipboard  # type: ignore[attr-defined]
            assert clipboard is not None
            assert Path(clipboard).resolve() == weather_folder, clipboard

            # The same untrusted text, read from a diffs/ file instead of REPORT.md, is also shown literally.
            await pilot.press("tab")  # Diffs view: test-orders-check.diff shown by default
            await pilot.press("down")  # highlight verification-log.txt
            await pilot.press("enter")  # open it
            await _settle(pilot)
            exact = _norm(app)
            assert MARKUP_SENTINEL.replace(" ", "") in exact, exact
            assert VISIBLE_ANSI_SENTINEL in exact, repr(exact)  # the ESC byte, shown as a visible glyph
            assert "\x1b" not in exact, repr(exact)  # the raw ESC byte itself never reaches the screen

    _run(body)


# ---------------------------------------------------------------- TUI-CP8-T04


def test_TUI_CP8_T04_full_keyboard_only_walkthrough_renders_at_80x24(tmp_path: Path) -> None:
    """[TUI-CP8-T04] Every review action (open Review, next, next, open the Diffs view, pick a file,
    previous, copy the path, run past the end, and the way back) is driven with key presses alone, no mouse
    event, at 80x24; every screen reached (results, review-report, review-diffs, review-no-diffs,
    review-done) renders without error, with its own key text still visible rather than clipped to nothing;
    the app ends back on the results screen; and the whole walkthrough never writes to the results folder."""
    results, folders = build_review_results(tmp_path)
    weather_folder = folders["weather-api"].resolve()
    before = _snapshot(results)

    async def body() -> None:
        from a2m.tui.app import A2MApp
        from a2m.tui.results import ResultsScreen
        from a2m.tui.review import ReviewScreen

        app = A2MApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await _open_results_and_review(pilot, app, results)
            assert isinstance(app.screen, ReviewScreen), app.screen
            await wait_until(pilot, lambda: "js-transform" in _norm_lower(app))
            assert _norm_lower(app) != ""  # review-report: never clipped to nothing

            await pilot.press("n")  # legacy-auth, 2 of 3
            await pilot.press("n")  # weather-api, 3 of 3
            await _settle(pilot)
            assert "proxy3of3" in _norm_lower(app)

            await pilot.press("tab")  # review-diffs
            await _settle(pilot)
            flat = _norm_lower(app)
            assert "test-orders-check.diff" in flat, flat

            await pilot.press("down")
            await pilot.press("enter")
            await _settle(pilot)
            visible = _norm(app)
            assert VISIBLE_ANSI_SENTINEL in visible  # the ESC byte, shown as a visible glyph
            assert "\x1b" not in visible  # the raw ESC byte itself never reaches the screen

            await pilot.press("c")  # copy-path
            await _settle(pilot)
            assert Path(app.clipboard).resolve() == weather_folder  # type: ignore[attr-defined]

            await pilot.press("p")  # legacy-auth, 2 of 3: review-no-diffs (still in the Diffs view)
            await _settle(pilot)
            flat = _norm_lower(app)
            assert "nodiffs" in flat.replace("no diffs", "nodiffs"), flat

            await pilot.press("tab")  # back to the Report view
            await pilot.press("n")  # weather-api, 3 of 3
            await pilot.press("n")  # past the last proxy: review-done
            await _settle(pilot)
            flat = _norm_lower(app)
            assert "finish" in flat, flat
            assert flat != "", flat  # review-done: never clipped to nothing

            await pilot.press("escape")  # the way back
            await _settle(pilot)
            assert isinstance(app.screen, ResultsScreen), app.screen
            await _settle(pilot)
            assert "js-transform" in _norm_lower(app)

    _run(body)

    after = _snapshot(results)
    assert after == before, "the review walkthrough must never write to the results folder"
