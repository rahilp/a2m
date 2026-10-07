"""TUI CP7: read the summary after a run (checkpoint plan: tests/CP7.json in the TUI run folder).

Normal-risk checkpoint, four code cases (the browser case, TUI-CP7-T05, is skipped per Ratchet's rules).

No earlier checkpoint fixed a public contract for a results screen (CP3-CP6 only built Setup and Run), so
this file fixes it, the same way tests/tui/test_cp4_setup_folders.py and test_cp6_run.py fixed their own
screens' ids:

* ``a2m.tui.results.ResultsScreen(results: Path)`` is a ``Screen[None]`` that, once mounted, loads
  ``results`` in a worker (never on the main thread) through a read-only helper and renders it:
  ``#results-counts`` is not required to carry specific text, but each bucket section
  ``#bucket-verified-header`` / ``#bucket-needs-review-header`` / ``#bucket-unsupported-header`` names the
  bucket and its count (``"Verified (1)"``), and the matching ``#bucket-verified-list`` /
  ``#bucket-needs-review-list`` / ``#bucket-unsupported-list`` Static lists the bucket's proxy names one
  per line, or the literal word "None" when the bucket is empty (DESIGN.md Components, DataTable/ListView
  rows, "Empty bucket"). ``#results-summary`` is a real ``textual.widgets.Markdown`` (a structured widget,
  never raw Markdown source text) rendering SUMMARY.md. A folder that is not a2m results, or has the
  ``.a2m-results`` marker but no SUMMARY.md/summary.json yet, shows one message in ``#results-error``
  instead of the bucket sections and the summary pane.
* The setup screen gets a new entry point, a button ``#open-results``, that opens the same folder browser
  the Browse… buttons use (started at the Results field's own folder, confirmed the same way, "Use this
  folder" / ``#picker-use``) and, once a folder is chosen, pushes ``ResultsScreen`` for it.

Every case drives the real screens headlessly through Textual's ``App.run_test()``/``Pilot``, against the
real ``a2m.tui.app.A2MApp`` (Header/Footer/theme/global keys are the real app's), exactly as
tests/CP7.json's own strategy calls for. ``a2m.tui.results`` is imported lazily *inside* each test's body,
never at module import time, so a missing module fails only that one test (with a normal AssertionError-
or exception-shaped failure, never a collection-time crash).

Fixtures are synthetic results folders built directly on ``tmp_path`` with the real
``a2m.layout``/``a2m.summary`` helpers (``write_batch_summary``, ``bucket_proxy_dir``,
``results_marker_path``), per tests/CP7.json's own strategy ("No child process, no Mule, no AI"): no Mule
runtime or AI call can ever land a proxy in ``verified`` (rule 16: a build alone never yields verified), so
only a hand-built fixture -- not a real ``--no-runtime`` CLI run -- can give TUI-CP7-T01 the one verified
proxy its fixture list calls for. The same "no child process" strategy note is why the "run screen's own
run-finished event" half of TUI-CP7-T01's "given" is covered by reaching ``ResultsScreen`` directly (the
same screen a finished run's hand-over would push) rather than by driving a real ``a2m migrate`` child
process to actually finish one: the "then" that case pins (identical rendering) does not depend on which
of the two paths reached the screen, and both paths constructed here push the identical ``ResultsScreen``.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from a2m import layout
from a2m.summary import ProxySummary, proxy_facts_json, write_batch_summary
from a2m.verify.model import VerificationType
from tui.screen import _normalize_ws, _run, _screen_text, _set_input, _settle

VERIFIED = layout.VERIFIED_DIR_NAME
NEEDS_REVIEW = layout.NEEDS_REVIEW_DIR_NAME
UNSUPPORTED = layout.UNSUPPORTED_DIR_NAME


# ---------------------------------------------------------------- fixture builders (own copies: tests/tui's
# own convention per test_cp6_run.py's header -- small helpers are not shared across TUI test files)


def _write_marker(results: Path) -> None:
    results.mkdir(parents=True, exist_ok=True)
    layout.results_marker_path(results).write_text(layout.RESULTS_MARKER_TEXT, encoding="utf-8")


def _write_proxy(results: Path, bucket: str, name: str) -> None:
    folder = layout.bucket_proxy_dir(results, bucket, name)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / layout.REPORT_NAME).write_text(f"# {name}\n\nReport body for {name}.\n", encoding="utf-8")
    if bucket != UNSUPPORTED:
        vtype = VerificationType.BATTERY.value if bucket == VERIFIED else VerificationType.STATIC.value
        fact = ProxySummary(
            name, bucket, vtype, {"step": 2, "policy": 1, "condition": 0}, {"assign-message": 1}
        )
        (folder / layout.PROXY_SUMMARY_NAME).write_text(proxy_facts_json(fact), encoding="utf-8")


def build_results(tmp_path: Path, folder_name: str, proxies: list[tuple[str, str]]) -> Path:
    """A real ``.a2m-results`` folder with the given ``(bucket, name)`` proxies, plus a real
    SUMMARY.md/summary.json written by :func:`write_batch_summary`, so the fixture matches what a real run
    produces (tests/CP7.json fixtures: "results-mixed", "results-clean")."""
    results = tmp_path / folder_name
    _write_marker(results)
    for bucket, name in proxies:
        _write_proxy(results, bucket, name)
    write_batch_summary(results)
    return results


def build_stopped(tmp_path: Path, folder_name: str = "results-stopped") -> Path:
    """The marker alone, no SUMMARY.md/summary.json yet (tests/CP7.json fixture "results-stopped": a run
    that was interrupted before the report stage)."""
    results = tmp_path / folder_name
    _write_marker(results)
    return results


def build_not_results(tmp_path: Path, folder_name: str = "not-results") -> Path:
    """An ordinary folder with unrelated files and no ``.a2m-results`` marker (tests/CP7.json fixture
    "not-results")."""
    folder = tmp_path / folder_name
    folder.mkdir(parents=True)
    (folder / "notes.txt").write_text("just a folder, not a2m results\n", encoding="utf-8")
    return folder


def _summary_json(results: Path) -> dict:
    return json.loads((results / layout.SUMMARY_JSON_NAME).read_text(encoding="utf-8"))


def _names_by_bucket(data: dict) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {VERIFIED: [], NEEDS_REVIEW: [], UNSUPPORTED: []}
    for proxy in data["proxies"]:
        found[proxy["bucket"]].append(proxy["name"])
    return found


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


def _assert_results_rendered(app: object, results: Path) -> None:
    """Every ``then`` TUI-CP7-T01 pins: counts from summary.json, exactly those proxies, SUMMARY.md as a
    structured widget rather than raw Markdown source."""
    from textual.widgets import Markdown

    data = _summary_json(results)
    buckets = data["buckets"]
    names = _names_by_bucket(data)

    flat = _screen_text(app)
    lower = flat.lower()
    assert re.search(rf"verified\s*\({buckets[VERIFIED]}\)", lower), flat
    assert re.search(rf"needs[- ]?review\s*\({buckets[NEEDS_REVIEW]}\)", lower), flat
    assert re.search(rf"unsupported\s*\({buckets[UNSUPPORTED]}\)", lower), flat

    for bucket_names in names.values():
        for name in bucket_names:
            assert lower.count(name.lower()) == 1, (name, flat)

    norm = _normalize_ws(flat).lower()
    assert "a2mmigrationsummary" in norm, flat  # SUMMARY.md's own title, rendered
    assert "bucket" in norm and "proxies" in norm, flat  # the Buckets table's own header cells, rendered

    # Rendered as formatted text (structured widgets), never the raw Markdown source:
    assert "## buckets" not in lower, flat
    assert "| --- |" not in flat, flat
    assert isinstance(app.screen.query_one("#results-summary"), Markdown)  # type: ignore[attr-defined]


# ---------------------------------------------------------------- TUI-CP7-T01


def test_TUI_CP7_T01_results_screen_renders_summary_and_bucket_counts(tmp_path: Path) -> None:
    """[TUI-CP7-T01] results-mixed (1 verified, 3 needs-review, 1 unsupported): the results screen shows
    SUMMARY.md rendered as formatted text and the verified/needs-review/unsupported lists show exactly
    those proxies with counts matching summary.json, whether the screen is reached directly (the same
    screen a finished run's own hand-over would push) or by choosing "Open results" from Setup for a
    folder made by an earlier, separate run."""
    results = build_results(
        tmp_path,
        "results-mixed",
        [
            (VERIFIED, "alpha-proxy"),
            (NEEDS_REVIEW, "bravo-proxy"),
            (NEEDS_REVIEW, "charlie-proxy"),
            (NEEDS_REVIEW, "delta-proxy"),
            (UNSUPPORTED, "echo-proxy"),
        ],
    )

    async def body() -> None:
        from a2m.tui.app import A2MApp
        from a2m.tui.results import ResultsScreen

        # Reached directly: the same screen a finished run's own hand-over pushes.
        direct_app = A2MApp()
        async with direct_app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await direct_app.push_screen(ResultsScreen(results))
            await _settle(pilot)
            await wait_until(pilot, lambda: "alpha-proxy" in _screen_text(direct_app).lower())
            _assert_results_rendered(direct_app, results)

        # Reached by choosing "Open results" from Setup for a folder made by an earlier, separate run.
        setup_app = A2MApp()
        async with setup_app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await _set_input(pilot, "#input-results", str(results))
            await pilot.click("#open-results")
            await _settle(pilot)

            from a2m.tui.picker import FolderPicker

            assert isinstance(setup_app.screen, FolderPicker), setup_app.screen
            await pilot.click("#picker-use")
            await _settle(pilot)

            from a2m.tui.results import ResultsScreen as _RS

            assert isinstance(setup_app.screen, _RS), setup_app.screen
            await wait_until(pilot, lambda: "alpha-proxy" in _screen_text(setup_app).lower())
            _assert_results_rendered(setup_app, results)

    _run(body)


# ---------------------------------------------------------------- TUI-CP7-T02


def test_TUI_CP7_T02_a_not_ready_or_non_a2m_folder_shows_a_clear_message(tmp_path: Path) -> None:
    """[TUI-CP7-T02] not-results (no .a2m-results marker) shows "does not look like an a2m results
    folder"; results-stopped (marker present, no SUMMARY.md/summary.json yet) shows "no summary.md yet"
    and that the run was stopped or still in progress -- two distinct messages, no crash, and neither
    shows the bucket lists or the summary pane."""
    not_results = build_not_results(tmp_path, "not-results")
    stopped = build_stopped(tmp_path, "results-stopped")

    async def body() -> None:
        from a2m.tui.app import A2MApp
        from a2m.tui.results import ResultsScreen

        app1 = A2MApp()
        async with app1.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await app1.push_screen(ResultsScreen(not_results))
            await _settle(pilot)
            await wait_until(pilot, lambda: _normalize_ws(_screen_text(app1)) != "")
            flat = _normalize_ws(_screen_text(app1)).lower()
            assert _normalize_ws("does not look like an a2m results folder").lower() in flat, flat
            assert "verified(" not in flat, flat
            assert "needsreview(" not in flat and "needs-review(" not in flat, flat

        app2 = A2MApp()
        async with app2.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await app2.push_screen(ResultsScreen(stopped))
            await _settle(pilot)
            await wait_until(pilot, lambda: _normalize_ws(_screen_text(app2)) != "")
            flat2 = _normalize_ws(_screen_text(app2)).lower()
            assert _normalize_ws("no summary.md yet").lower() in flat2, flat2
            assert "verified(" not in flat2, flat2
            assert "needsreview(" not in flat2 and "needs-review(" not in flat2, flat2
            # distinct from the not-results message
            assert _normalize_ws("does not look like an a2m results folder").lower() not in flat2, flat2

    _run(body)


# ---------------------------------------------------------------- TUI-CP7-T03


def test_TUI_CP7_T03_a_run_with_nothing_to_review_says_so_plainly(tmp_path: Path) -> None:
    """[TUI-CP7-T03] results-clean (0 needs-review proxies, all verified or unsupported): the needs-review
    section's count is 0 and it also says "None" in plain words, not an empty list with a bare "0" heading
    and nothing else."""
    results = build_results(
        tmp_path,
        "results-clean",
        [
            (VERIFIED, "alpha-proxy"),
            (VERIFIED, "bravo-proxy"),
            (UNSUPPORTED, "charlie-proxy"),
        ],
    )

    async def body() -> None:
        from a2m.tui.app import A2MApp
        from a2m.tui.results import ResultsScreen

        app = A2MApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await app.push_screen(ResultsScreen(results))
            await _settle(pilot)
            await wait_until(pilot, lambda: "alpha-proxy" in _screen_text(app).lower())

            flat = _screen_text(app)
            lower = flat.lower()
            assert re.search(r"needs[- ]?review\s*\(0\)", lower), flat
            norm = _normalize_ws(flat).lower()
            assert "none" in norm, flat  # DESIGN.md's own wording for an empty bucket, not a bare "0"

    _run(body)


# ---------------------------------------------------------------- TUI-CP7-T04


def test_TUI_CP7_T04_opening_and_browsing_results_never_writes(tmp_path: Path) -> None:
    """[TUI-CP7-T04] Opening results-mixed and switching between its bucket sections never writes,
    creates or deletes a single file in the results folder: every file's content and mtime are the same
    before and after."""
    results = build_results(
        tmp_path,
        "results-mixed",
        [
            (VERIFIED, "alpha-proxy"),
            (NEEDS_REVIEW, "bravo-proxy"),
            (NEEDS_REVIEW, "charlie-proxy"),
            (NEEDS_REVIEW, "delta-proxy"),
            (UNSUPPORTED, "echo-proxy"),
        ],
    )
    before = _snapshot(results)

    async def body() -> None:
        from a2m.tui.app import A2MApp
        from a2m.tui.results import ResultsScreen

        app = A2MApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await app.push_screen(ResultsScreen(results))
            await _settle(pilot)
            await wait_until(pilot, lambda: "alpha-proxy" in _screen_text(app).lower())

            # Switch focus between the bucket sections and the summary pane, and scroll within them.
            for _ in range(6):
                await pilot.press("tab")
            await _settle(pilot)
            await pilot.press("pagedown")
            await _settle(pilot)
            await pilot.press("pageup")
            await _settle(pilot)

    _run(body)

    after = _snapshot(results)
    assert after == before, "opening and browsing results must never write to the results folder"
