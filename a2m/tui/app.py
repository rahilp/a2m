"""The Textual app behind ``a2m tui``: themes, global keys and the first screen.

Importing this module imports Textual, so only the ``tui`` command (and the
dev capture harness) imports it; see :mod:`a2m.tui`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar

from textual.app import App
from textual.binding import Binding, BindingType
from textual.theme import Theme
from textual.widgets import HelpPanel

from a2m.errors import NoTerminalError
from a2m.tui.frame import APP_TITLE
from a2m.tui.setup import SetupScreen

# DESIGN.md section 2, Color. Textual derives its own variables from these; the
# a2m tokens Textual has no name for (foreground-muted, border, overlay) and the
# footer colors (DESIGN.md Layout, Footer) are set explicitly.
DARK = Theme(
    name="a2m-dark",
    dark=True,
    background="#0C0E12",
    surface="#12151B",
    panel="#1A1E26",
    foreground="#E6E9EF",
    primary="#2F6FE4",
    accent="#1A8068",
    success="#257337",
    warning="#E3B341",
    error="#C73E39",
    variables={
        "foreground-muted": "#8B93A7",
        "border": "#2A2F3A",
        "overlay": "#20242E",
        "footer-background": "#0C0E12",
        "footer-key-foreground": "#1A8068",
        "footer-description-foreground": "#8B93A7",
    },
)
LIGHT = Theme(
    name="a2m-light",
    dark=False,
    background="#F5F6F8",
    surface="#FFFFFF",
    panel="#ECEEF2",
    foreground="#1B1F27",
    primary="#2563EB",
    accent="#0C7E69",
    success="#1A7F37",
    warning="#9A6700",
    error="#CF222E",
    variables={
        "foreground-muted": "#5B6472",
        "border": "#D3D7DE",
        "overlay": "#E3E6EC",
        "footer-background": "#F5F6F8",
        "footer-key-foreground": "#0C7E69",
        "footer-description-foreground": "#5B6472",
    },
)


class A2MApp(App[int]):
    """The a2m terminal UI."""

    CSS_PATH = "app.tcss"
    TITLE = APP_TITLE
    # Global keys first in the footer (DESIGN.md Header / Footer): q Quit, ? Help. ctrl+q also quits
    # (Textual's own priority binding, kept out of the footer).
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "quit", "Quit"),
        Binding("question_mark", "toggle_help", "Help", key_display="?"),
    ]

    def __init__(self, *, exports: str = "", results: str = "") -> None:
        """``exports`` and ``results`` fill the setup screen's folder fields when it opens (checked as if typed)."""
        super().__init__()
        self.register_theme(DARK)
        self.register_theme(LIGHT)
        self.theme = DARK.name
        self._exports = exports
        self._results = results

    def on_mount(self) -> None:
        self.push_screen(SetupScreen(exports=self._exports, results=self._results))

    def action_toggle_help(self) -> None:
        """Show the key help panel, or hide it when it is already open."""
        if self.screen.query(HelpPanel):
            self.action_hide_help_panel()
        else:
            self.action_show_help_panel()


def run_app(*, at_terminal: Callable[[], bool]) -> int:
    """Run the TUI until the user quits; returns the exit code for the shell.

    ``at_terminal`` is the command line's check that a person is typing at a terminal. Without one the
    full-screen app would wait forever for keys that can never arrive, so it raises
    :class:`~a2m.errors.NoTerminalError` instead of starting.
    """
    if not at_terminal():
        raise NoTerminalError("a2m tui needs an interactive terminal (stdin and stdout must both be terminals)")
    app = A2MApp()
    app.run()
    return app.return_code or 0
