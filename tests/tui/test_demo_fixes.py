"""Fixes from the client demo dry run (Oct 2026): plural wording in the exports check, a stop message that
counts refused proxies, and a bucket header that says when its pane hides rows."""

from __future__ import annotations

from pathlib import Path

from tui.screen import _run, _screen_text, _settle
from tui.test_cp7_results import NEEDS_REVIEW, UNSUPPORTED, VERIFIED, build_results


def test_found_line_uses_singular_for_one() -> None:
    from a2m.tui.folders import _found_line

    assert "1 proxy, 1 shared flow found" in _found_line(1, 1, 0)
    assert "6 proxies, 2 shared flows found" in _found_line(6, 2, 0)


def test_stopped_text_counts_a_refused_proxy() -> None:
    from a2m.tui.run import RunScreen

    run = RunScreen(["a2m"])
    run._seen_start, run._total, run._processed, run._refused = True, 6, 1, 1
    text = run._stopped_text()
    assert text.startswith("Stopped at 1 of 6 proxies: 1 unsupported."), text
    assert "0 " not in text, text


def test_bucket_header_says_when_rows_are_hidden(tmp_path: Path) -> None:
    results = build_results(
        tmp_path,
        "results-overflow",
        [
            (VERIFIED, "alpha-proxy"),
            (NEEDS_REVIEW, "bravo-proxy"),
            (NEEDS_REVIEW, "charlie-proxy"),
            (NEEDS_REVIEW, "delta-proxy"),
            (NEEDS_REVIEW, "foxtrot-proxy"),
            (UNSUPPORTED, "echo-proxy"),
        ],
    )

    async def body() -> None:
        from a2m.tui.app import A2MApp
        from a2m.tui.results import MORE_HINT, ResultsScreen

        app = A2MApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await app.push_screen(ResultsScreen(results))
            await _settle(pilot)
            await pilot.pause(0.5)
            headers = {
                bucket: str(app.screen.query_one(f"#bucket-{bucket}-header").render())
                for bucket in (VERIFIED, NEEDS_REVIEW)
            }
            assert headers[NEEDS_REVIEW].endswith(MORE_HINT.strip()), (headers, _screen_text(app))
            assert MORE_HINT.strip() not in headers[VERIFIED], headers

    _run(body)
