"""The frame every screen shares: the header row (title left, version right)."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Static

from a2m import __version__

APP_TITLE = "a2m"


class AppHeader(Horizontal):
    """Row 0: the bold "a2m" title on the left and "a2m · v<version>" on the right (DESIGN.md Layout)."""

    def compose(self) -> ComposeResult:
        yield Static(APP_TITLE, id="hdr-title", markup=False)
        yield Static(f"{APP_TITLE} · v{__version__}", id="hdr-version", markup=False)
