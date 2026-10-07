"""Shared screenshot-parsing, screen-text and whitespace-normalizing helpers for the TUI's Pilot tests.

One copy of each helper that used to be duplicated (sometimes with small drift) across
tests/tui/test_cp3_launch.py, tests/tui/test_cp4_setup_folders.py, tests/tui/test_cp5_setup_options.py and
tests/tui/test_cp4_folder_browser.py. Plain functions, imported as a sibling module inside the ``tui`` test
package (``from tui.screen import ...``) rather than through tests/conftest.py, since these helpers are
specific to driving the TUI headlessly through Textual's Pilot and are not needed by the rest of the suite.
"""

from __future__ import annotations

import asyncio
import html
import re
import shlex
from collections.abc import Awaitable, Callable

_TEXT_RE = re.compile(
    r'<text[^>]*\sx="([\d.]+)"[^>]*clip-path="url\(#[\w-]+-line-(\d+)\)"[^>]*>(.*?)</text>',
    re.DOTALL,
)

_WHITESPACE_RE = re.compile(r"\s+")
# Box Drawing (U+2500-257F) and Block Elements (U+2580-259F): the border/padding glyphs Textual draws as
# literal text content for round/heavy borders (``╭─╮│╰─╯``, ``┏━┓┃┗━┛``) and compact toggle brackets
# (``▐▌``) -- see DESIGN.md's Border/Elevation and Button/Checkbox sections.
_BORDER_CHARS_RE = re.compile(r"[─-▟]")


def _screen_rows(app: object) -> dict[int, str]:
    """Plain text per terminal row of ``app``'s current screenshot (no widget-class assumptions)."""
    svg = app.export_screenshot(simplify=True)  # type: ignore[attr-defined]
    cells: dict[int, list[tuple[float, str]]] = {}
    for x, line_no, text in _TEXT_RE.findall(svg):
        clean = html.unescape(text).replace("\xa0", " ")
        cells.setdefault(int(line_no), []).append((float(x), clean))
    return {line_no: "".join(text for _x, text in sorted(entries)) for line_no, entries in cells.items()}


def _screen_text(app: object) -> str:
    """Every row of ``app``'s current screenshot, newline-joined, for substring checks."""
    rows = _screen_rows(app)
    height = app.size.height  # type: ignore[attr-defined]
    return "\n".join(rows.get(y, "") for y in range(height))


def _normalize_ws(text: str) -> str:
    """Strip every whitespace character and every border/padding glyph out of ``text`` entirely.

    A message that is longer than the 80-column screen wraps across more than one row, and a long path
    can wrap mid-word with no space at the break; the row(s) it wraps into can also carry a surrounding
    widget's border or padding glyphs at the point ``_screen_text`` stitches rows together with newlines.
    Removing all whitespace and all border/padding glyphs from both the rendered screen text and the
    expected message before a substring check makes the match tolerant of that wrapping without weakening
    what is asserted: the exact same full message, character-for-character once whitespace and borders are
    out of the way, still has to appear.
    """
    text = _BORDER_CHARS_RE.sub("", text)
    return _WHITESPACE_RE.sub("", text)


def _flat(text: str) -> str:
    """``text`` without whitespace or border glyphs, so a path wrapped across rows still matches.

    tests/tui/test_cp4_folder_browser.py's own name for the same normalization ``_normalize_ws`` does (one
    combined regex instead of two passes); kept under its original name, rather than folded into
    ``_normalize_ws``, so no test's call site has to change.
    """
    return re.sub(r"[\s─-▟]", "", text)


def _run(coro_factory: Callable[[], Awaitable[None]]) -> None:
    """Run one async Textual test body to completion (no pytest-asyncio plugin installed)."""
    asyncio.run(coro_factory())


async def _settle(pilot: object) -> None:
    """Let one round of validation (a worker thread per DESIGN.md's notes) finish and the screen redraw."""
    await pilot.pause()  # type: ignore[attr-defined]
    await asyncio.wait_for(pilot.app.workers.wait_for_complete(), timeout=10)  # type: ignore[attr-defined]
    await pilot.pause()  # type: ignore[attr-defined]


async def _set_input(pilot: object, widget_id: str, value: str) -> None:
    """Finish typing ``value`` into the Input ``widget_id``, the way a user pasting/typing it would."""
    from textual.widgets import Input

    field = pilot.app.screen.query_one(widget_id, Input)  # type: ignore[attr-defined]
    field.value = value
    await _settle(pilot)


async def _copied_command_tokens(pilot: object) -> list[str]:
    """Press ctrl+y (the app's "Copy command" key) and ``shlex.split`` the full command it copies.

    This reads the command the way a user actually would: Textual's own ``App.copy_to_clipboard`` sets
    ``App.clipboard``, so pressing the bound key and reading that attribute gets the full, untruncated
    command even when the on-screen preview line is too narrow to show all of it (the full text is only
    guaranteed reachable through the Copy action/key per DESIGN.md's Command preview pattern, never by
    re-parsing a possibly-truncated visible line).
    """
    await pilot.press("ctrl+y")  # type: ignore[attr-defined]
    await _settle(pilot)
    clipboard = pilot.app.clipboard  # type: ignore[attr-defined]
    return shlex.split(clipboard)
