"""The setup screen: the first screen the TUI shows.

This is the screen's layout in its "nothing chosen yet" state (DESIGN.md Layout,
Screen anatomy, Setup). The folder pickers, the AI choice and the Advanced
options are drawn but disabled: picking folders and options, validation and
the Start button arrive with later steps, and until then nothing here accepts
input, so nothing pretends to work.
"""

from __future__ import annotations

import shlex

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Button, Collapsible, Footer, Input, RadioButton, RadioSet, Static

from a2m.tui.frame import AppHeader

EXPORTS_PLACEHOLDER = "<exports folder>"
RESULTS_PLACEHOLDER = "<results folder>"


def command_preview(exports: str | None = None, results: str | None = None) -> str:
    """The ``a2m migrate`` command for the current choices, with placeholders for folders not picked yet."""
    return " ".join(
        ["a2m", "migrate", shlex.quote(exports or EXPORTS_PLACEHOLDER), "--out", shlex.quote(results or RESULTS_PLACEHOLDER)]
    )


class SetupScreen(Screen[None]):
    """Pick the exports folder, the results folder and the AI choice, then start a run."""

    def compose(self) -> ComposeResult:
        yield AppHeader(id="hdr")
        with Vertical(id="body"):
            yield Static("Exports folder", classes="field-label", markup=False)
            yield Input(placeholder="/path/to/apigee-exports", id="input-exports", disabled=True)
            yield Static("Results folder", classes="field-label gap-1", markup=False)
            yield Input(placeholder="/path/to/results", id="input-results", disabled=True)
            yield Static("AI", classes="section-label gap-2", markup=False)
            with RadioSet(id="radio-ai", compact=True, disabled=True):
                yield RadioButton("Claude (uses ANTHROPIC_API_KEY)", id="radio-ai-claude", compact=True)
                yield RadioButton("No AI", id="radio-ai-none", compact=True)
            yield Collapsible(
                title="Advanced options",
                collapsed=True,
                collapsed_symbol="▸",
                expanded_symbol="▾",
                id="collapsible-advanced",
                classes="gap-1",
                disabled=True,
            )
            yield Static("Command", classes="section-label gap-1", markup=False)
            yield Static(command_preview(), id="command-preview", markup=False)
            yield Button("Start", id="start", variant="primary", compact=True, disabled=True, classes="gap-1")
        yield Footer(id="ftr", compact=True, show_command_palette=False)
