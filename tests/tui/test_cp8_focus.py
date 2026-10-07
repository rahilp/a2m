"""TUI CP8 adversarial round 1 (finding S1): a focused primary button must look different from an unfocused one.

"Back to summary" on the finished review walkthrough is a primary button whose edges stay primary while
focused, so focus needs a second, non-colour cue. This file renders that button focused and unfocused through
the real screens and checks the two renderings differ.
"""

from __future__ import annotations

from pathlib import Path

from tui.screen import _normalize_ws, _run, _screen_text, _settle
from tui.test_cp8_review import _open_results_and_review, build_review_results, wait_until


def _button_render(button: object) -> list[tuple[str, str]]:
    """The button's own rendered row as (text, style) pairs."""
    from textual.geometry import Region

    width = button.size.width  # type: ignore[attr-defined]
    strips = button.render_lines(Region(0, 0, width, 1))  # type: ignore[attr-defined]
    return [(segment.text, str(segment.style)) for strip in strips for segment in strip]


def test_TUI_CP8_X01_focused_back_to_summary_differs_from_unfocused(tmp_path: Path) -> None:
    """[TUI-CP8-X01] On the finished walkthrough, "Back to summary" is a primary button; its focused rendering
    differs from its unfocused rendering (a visible focus state that is not lost to the primary edge colour),
    and the focused label carries a non-colour cue (bold)."""
    results, _folders = build_review_results(tmp_path)

    async def body() -> None:
        from textual.widgets import Button

        from a2m.tui.app import A2MApp
        from a2m.tui.review import ReviewScreen

        app = A2MApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await _open_results_and_review(pilot, app, results)
            assert isinstance(app.screen, ReviewScreen), app.screen
            await wait_until(pilot, lambda: "proxy1of3" in _normalize_ws(_screen_text(app)).lower())
            for _ in range(3):
                await pilot.press("n")
                await _settle(pilot)
            assert "finish" in _normalize_ws(_screen_text(app)).lower()

            back = app.screen.query_one("#review-back", Button)
            assert back.variant == "primary", back.variant

            back.focus()
            await _settle(pilot)
            assert app.focused is back
            focused = _button_render(back)

            app.set_focus(None)
            await _settle(pilot)
            assert app.focused is not back
            unfocused = _button_render(back)

            assert focused != unfocused, (focused, unfocused)
            label_styles = [style for text, style in focused if "Back to summary" in text]
            assert label_styles and all("bold" in style for style in label_styles), focused
            assert not any("bold" in style for text, style in unfocused if "Back to summary" in text), unfocused

    _run(body)
