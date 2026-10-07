"""TUI CP5 (adversarial round 1, A2): the compact toggle glyphs keep their brackets and inner glyph visible.

``a2m.tui.frame.GlyphRadioButton`` and ``GlyphCheckbox`` redraw Textual's toggle glyph so the inner glyph sits
on the widget's own background between the two brackets (DESIGN.md Components, RadioSet and Checkbox). They
hook into Textual internals, so this test renders them on and off and checks the drawn cells: if a Textual
upgrade stops calling the override, the inner cell goes back to the bracket color and this test fails.
"""

from __future__ import annotations

import asyncio

from textual.app import App, ComposeResult
from textual.widget import Widget


def _glyph_cells(widget: Widget) -> list[tuple[str, object, object]]:
    """The first three drawn cells of ``widget``'s first row: (text, foreground, background) per cell."""
    cells: list[tuple[str, object, object]] = []
    for segment in widget.render_line(0):
        style = segment.style
        for char in segment.text:
            cells.append((char, style.color if style else None, style.bgcolor if style else None))
    return cells[:3]


def test_TUI_CP5_X01_glyph_toggles_draw_brackets_and_a_visible_inner_glyph_on_and_off() -> None:
    """[TUI-CP5-X01] A GlyphRadioButton and a GlyphCheckbox, off and on, draw ``▐`` + inner glyph + ``▌``;
    the brackets are drawn in a color on the widget's background, the inner glyph sits on that same
    background in a color that differs from it, and the inner glyph's color changes when the toggle turns on."""
    from a2m.tui.frame import GlyphCheckbox, GlyphRadioButton

    class GlyphApp(App[None]):
        def compose(self) -> ComposeResult:
            yield GlyphRadioButton("Claude", False, id="radio", compact=True)
            yield GlyphCheckbox("mock-backends", False, id="checkbox", compact=True)

    async def scenario() -> None:
        app = GlyphApp()
        async with app.run_test(size=(40, 6)) as pilot:
            await pilot.pause()
            for selector, inner in (("#radio", "●"), ("#checkbox", "X")):
                widget = app.query_one(selector)
                inner_colors = []
                for value in (False, True):
                    widget.value = value  # type: ignore[attr-defined]
                    await pilot.pause()
                    cells = _glyph_cells(widget)
                    assert [cell[0] for cell in cells] == ["▐", inner, "▌"], (selector, value, cells)
                    (_, left_fg, left_bg), (_, inner_fg, inner_bg), (_, right_fg, right_bg) = cells
                    assert left_fg != left_bg and right_fg != right_bg, (selector, value, cells)
                    assert inner_bg == left_bg == right_bg, (selector, value, cells)
                    assert inner_fg is not None and inner_fg != inner_bg, (selector, value, cells)
                    inner_colors.append(inner_fg)
                assert inner_colors[0] != inner_colors[1], (selector, inner_colors)

    asyncio.run(scenario())
