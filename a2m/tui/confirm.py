"""The modal confirm (DESIGN.md Components, Modal confirm): a title, one or two lines naming the exact
consequence, and two buttons. The safer action (Cancel) is focused when it opens and Escape always picks
it; the destructive action is the error-filled button. Dismisses with True only for the destructive
action."""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from a2m.tui.frame import EdgeButton

CANCEL_LABEL = "Cancel"


class ConfirmScreen(ModalScreen[bool]):
    """Ask before a destructive action; ``tone`` is ``"warning"`` (Stop) or ``"error"`` (quit while a run
    is going, since it also stops the run), the color of the dialog's heavy border."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", CANCEL_LABEL)]

    def __init__(
        self, title: str, lines: tuple[str, ...], action: str, *, tone: str = "warning", purpose: str = ""
    ) -> None:
        """``purpose`` names what is being confirmed (e.g. ``"quit"``), so a caller can tell its own dialog."""
        super().__init__()
        self.purpose = purpose
        self._title = title
        self._lines = lines
        self._action = action
        self._tone = tone

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm", classes=f"-{self._tone}"):
            yield Static(self._title, id="confirm-title", markup=False)
            for line in self._lines:
                yield Static(line, classes="confirm-line", markup=False)
            with Horizontal(id="confirm-buttons"):
                yield EdgeButton(CANCEL_LABEL, id="confirm-cancel", compact=True)
                yield EdgeButton(self._action, id="confirm-primary", variant="error", compact=True)

    def on_mount(self) -> None:
        self.query_one("#confirm-cancel", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(event.button.id == "confirm-primary")

    def action_cancel(self) -> None:
        self.dismiss(False)
