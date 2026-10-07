"""TUI CP7 adversarial round 1 fixes: optional files in a results folder that cannot be used.

* TUI-CP7-X01: summary.json is there but cannot be read (PermissionError). The folder still opens, the
  proxy lists come from the bucket folders, and the summary says why in one line.
* TUI-CP7-X02: an empty or whitespace-only SUMMARY.md shows the "No SUMMARY.md yet" message (DESIGN.md,
  Markdown viewer, Empty / missing file), not a blank summary pane.

Fixtures are synthetic results folders built with the real a2m.layout/a2m.summary helpers, as in
test_cp7_results.py (small helpers are not shared across TUI test files).
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from a2m import layout, safefs
from a2m.summary import write_batch_summary
from tui.screen import _normalize_ws, _run, _screen_text, _settle

VERIFIED = layout.VERIFIED_DIR_NAME
NEEDS_REVIEW = layout.NEEDS_REVIEW_DIR_NAME
UNSUPPORTED = layout.UNSUPPORTED_DIR_NAME


def _build(tmp_path: Path, proxies: list[tuple[str, str]]) -> Path:
    results = tmp_path / "results"
    results.mkdir()
    layout.results_marker_path(results).write_text(layout.RESULTS_MARKER_TEXT, encoding="utf-8")
    for bucket, name in proxies:
        folder = layout.bucket_proxy_dir(results, bucket, name)
        folder.mkdir(parents=True)
        (folder / layout.REPORT_NAME).write_text(f"# {name}\n", encoding="utf-8")
    write_batch_summary(results)
    return results


def _deny_summary_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every open of summary.json fail the way an unreadable file does (works as root too)."""
    real_open = safefs.open_plain_file

    def fake_open(root: Path, target: Path, flags: int) -> int:
        if Path(target).name == layout.SUMMARY_JSON_NAME:
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(target))
        return real_open(root, target, flags)

    monkeypatch.setattr(safefs, "open_plain_file", fake_open)


PROXIES = [(VERIFIED, "alpha-proxy"), (NEEDS_REVIEW, "bravo-proxy"), (UNSUPPORTED, "charlie-proxy")]


def test_TUI_CP7_X01_unreadable_summary_json_falls_back_to_bucket_folders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[TUI-CP7-X01] summary.json raises PermissionError on read; SUMMARY.md and the bucket folders are
    readable. The folder still loads, the lists come from the bucket folders, and the summary says why."""
    from a2m.tui.read import Results, load_results

    results = _build(tmp_path, PROXIES)
    _deny_summary_json(monkeypatch)

    loaded = load_results(results)
    assert isinstance(loaded, Results), loaded
    assert {(p.bucket, p.name) for p in loaded.proxies} == set(PROXIES)
    for bucket in (VERIFIED, NEEDS_REVIEW, UNSUPPORTED):
        assert loaded.count(bucket) == 1
    assert len(loaded.notes) == 1 and "summary.json" in loaded.notes[0], loaded.notes
    assert os.strerror(errno.EACCES).lower() in loaded.notes[0].lower(), loaded.notes
    assert "bucket folders" in loaded.notes[0], loaded.notes
    assert "bucket folders" in loaded.summary_md, loaded.summary_md

    async def body() -> None:
        from a2m.tui.app import A2MApp
        from a2m.tui.results import ResultsScreen

        app = A2MApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            screen = ResultsScreen(results)
            await app.push_screen(screen)
            await _settle(pilot)
            flat = _screen_text(app).lower()
            assert "alpha-proxy" in flat and "bravo-proxy" in flat and "charlie-proxy" in flat, flat
            assert "cannot read" not in flat, flat
            assert screen.results is not None and screen.results.notes == loaded.notes

    _run(body)


def test_TUI_CP7_X01_readable_summary_json_adds_no_note(tmp_path: Path) -> None:
    """[TUI-CP7-X01] the normal case: a readable summary.json is used and no fallback note is added."""
    from a2m.tui.read import Results, load_results

    loaded = load_results(_build(tmp_path, PROXIES))
    assert isinstance(loaded, Results), loaded
    assert loaded.notes == ()
    assert "bucket folders" not in loaded.summary_md


@pytest.mark.parametrize("content", ["", "  \n\n\t \n"], ids=["empty", "whitespace"])
def test_TUI_CP7_X02_empty_summary_md_shows_no_summary_message(tmp_path: Path, content: str) -> None:
    """[TUI-CP7-X02] an empty or whitespace-only SUMMARY.md shows "No SUMMARY.md yet", not a blank pane."""
    from a2m.tui.read import NO_SUMMARY_TEXT, Problem, ResultsProblem, load_results

    results = _build(tmp_path, PROXIES)
    layout.summary_md_path(results).write_text(content, encoding="utf-8")

    loaded = load_results(results)
    assert isinstance(loaded, ResultsProblem), loaded
    assert loaded.problem is Problem.NO_SUMMARY

    async def body() -> None:
        from a2m.tui.app import A2MApp
        from a2m.tui.results import ResultsScreen

        app = A2MApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await app.push_screen(ResultsScreen(results))
            await _settle(pilot)
            flat = _normalize_ws(_screen_text(app)).lower()
            assert _normalize_ws(NO_SUMMARY_TEXT).lower() in flat, flat
            assert "verified(" not in flat, flat

    _run(body)
