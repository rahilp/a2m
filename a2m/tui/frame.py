"""The frame every screen shares: the header row (title left, version right), the compact button and the
compact toggle glyphs."""

from __future__ import annotations

from typing import Any, ClassVar

from textual.app import App, ComposeResult, RenderResult
from textual.binding import ActiveBinding
from textual.containers import Horizontal
from textual.content import Content
from textual.geometry import Size
from textual.style import Style
from textual.widgets import Button, Checkbox, RadioButton, Static
from textual.widgets._toggle_button import ToggleButton

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


def bracket_glyph(toggle: ToggleButton) -> Content:
    """``▐`` + inner glyph + ``▌`` drawn as separate glyphs on the toggle's own background (DESIGN.md
    Components, RadioSet and Checkbox).

    The ``toggle--button`` style's background colors the outer brackets and its color the inner glyph, so
    off reads as a muted pill, on as a primary one, and focus can recolor the brackets alone. Textual's own
    glyph fills the inner cell with the bracket color, which hides an inner glyph of that same color.
    """
    button_style = toggle.get_visual_style("toggle--button")
    behind = toggle.background_colors[1]
    side_style = Style(foreground=button_style.background, background=behind)
    inner_style = button_style + Style(background=behind)
    return Content.assemble(
        (toggle.BUTTON_LEFT, side_style),
        (toggle.BUTTON_INNER, inner_style),
        (toggle.BUTTON_RIGHT, side_style),
    )


class GlyphRadioButton(RadioButton):
    """A RadioButton whose ``▐●▌`` glyph keeps its brackets and dot visible (see :func:`bracket_glyph`)."""

    @property
    def _button(self) -> Content:
        return bracket_glyph(self)


class GlyphCheckbox(Checkbox):
    """A Checkbox whose ``▐X▌`` glyph keeps its brackets and X visible (see :func:`bracket_glyph`).

    The label follows the glyph with no gap (``▐X▌mock-backends``), as in the prototype; one trailing cell
    keeps the focus highlight off the next widget.
    """

    @property
    def _button(self) -> Content:
        return bracket_glyph(self)

    def render(self) -> Content:
        label = self._label.pad(0, 1).stylize_before(self.get_visual_style("toggle--label"))
        return Content.assemble(self._button, label)

    def get_content_width(self, container: Size, viewport: Size) -> int:
        return self._button.cell_length + (1 if self._label else 0) + self._label.cell_length
