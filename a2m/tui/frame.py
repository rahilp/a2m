"""The frame every screen shares: the header row (title left, version right) and the compact button."""

from __future__ import annotations

from typing import Any, ClassVar

from textual.app import App, ComposeResult, RenderResult
from textual.binding import ActiveBinding
from textual.containers import Horizontal
from textual.content import Content
from textual.widgets import Button, Static

from a2m import __version__

APP_TITLE = "a2m"


def global_keys_first(app: App[Any], bindings: dict[str, ActiveBinding]) -> dict[str, ActiveBinding]:
    """``bindings`` with the app's global keys (Quit, Help) first, so a footer lists them first (DESIGN.md)."""

    def is_global(active: ActiveBinding) -> bool:
        return active.node is app or active.binding.action.startswith("app.")

    return dict(sorted(bindings.items(), key=lambda item: not is_global(item[1])))


class AppHeader(Horizontal):
    """Row 0: the bold "a2m" title on the left and "a2m · v<version>" on the right (DESIGN.md Layout)."""

    def compose(self) -> ComposeResult:
        yield Static(APP_TITLE, id="hdr-title", markup=False)
        yield Static(f"{APP_TITLE} · v{__version__}", id="hdr-version", markup=False)


class EdgeButton(Button):
    """A compact button: ``┃`` + padded label + ``┃`` (DESIGN.md Components, Button).

    The ``┃`` edges are the button's left and right borders, drawn on the button's own background. The
    padded label is drawn with the ``edge-button--fill`` style, so a primary or error button fills only
    its label in the role color and keeps its edge glyphs visible on the surface around it. The label's
    one-cell padding is part of the fill, so Button's own line padding is turned off (CSS cannot set 0).
    """

    COMPONENT_CLASSES: ClassVar[set[str]] = Button.COMPONENT_CLASSES | {"edge-button--fill"}

    def on_mount(self) -> None:
        self.styles.line_pad = 0

    def render(self) -> RenderResult:
        label = self.label if isinstance(self.label, Content) else Content(str(self.label))
        return label.pad(1, 1).stylize_before(self.get_visual_style("edge-button--fill"))
