"""The setup screen: pick the exports and results folders and see the matching command.

Each folder can be typed (Tab completes a folder name, and the dim line under the field shows the
absolute folder a relative path names) or picked in the folder browser (the Browse… button, or ctrl+o
in a folder field). Every change is
checked in a worker thread with the engine's own read-only checks (:mod:`a2m.tui.folders`), so the
lines under the fields carry the same messages ``a2m migrate`` gives, and Start is enabled only when a
run could start. A results folder from an earlier run asks for Resume or Force. The command preview
always shows the ``a2m migrate`` line for the current choices. The AI choice and the Advanced options
are drawn but not built yet, so they stay disabled; nothing here pretends to work.

Folder names and paths are user data: every widget that shows them renders plain text, never markup.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import ClassVar

from textual import events, work
from textual.app import ComposeResult
from textual.binding import ActiveBinding, Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Button, Collapsible, Footer, Input, RadioButton, RadioSet, Static
from textual.worker import get_current_worker

from a2m.errors import A2mError
from a2m.tui.command import RerunChoice, SetupChoices, command_preview, folder_path, resolved_folder
from a2m.tui.folders import FieldCheck, SetupCheck, Status, check_setup, preview_exports, preview_results
from a2m.tui.frame import AppHeader, EdgeButton, global_keys_first
from a2m.tui.picker import FolderPicker, FolderSuggester, start_folder

# A check starts this long after the last change, so typing a path does not scan every folder on the way.
CHECK_DELAY_SECONDS = 0.15
CHECKING_TEXT = "Checking…"
CHOOSE_RERUN_REASON = "Pick Resume or Force to continue"
START_NOT_BUILT = "Starting a run from here is not built yet; run the command shown in a terminal."
COMMAND_COPIED = "Command copied"


class Body(VerticalScroll, can_focus=False):
    """The setup form between the Header and the Footer.

    The sections sit row on row as in the prototype (only the Resume/Force prompt has a blank row above it,
    dropped when both folder fields show their absolute folder line), so every setup state fits 80x24
    without scrolling. On a terminal shorter than that the form scrolls
    under the pinned Footer instead of losing rows, and focus moving to a widget scrolls it into view.
    """


class FolderInput(Input):
    """A folder field: type a path (Tab completes a folder name), or press ctrl+o to browse for it."""

    # In a field q types a q, so the footer shows ctrl+q for Quit (the app's own binding does the quitting).
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+q", "app.quit", "Quit", priority=True),
        Binding("ctrl+o", "browse", "Browse folders"),
        Binding("tab", "complete", "Complete folder name", show=False),
    ]

    def __init__(self, *, title: str, placeholder: str, id: str) -> None:
        super().__init__(placeholder=placeholder, id=id, suggester=FolderSuggester())
        self.picker_title = title

    def action_browse(self) -> None:
        screen = self.screen
        if isinstance(screen, SetupScreen):
            screen.browse(self)

    def action_complete(self) -> None:
        """Accept the suggested folder name; with none suggested, Tab moves on as usual."""
        if self.cursor_at_end and self._suggestion:
            self.action_cursor_right()
        else:
            self.screen.focus_next()

    def picked(self, folder: Path | None) -> None:
        """Fill the field with the folder chosen in the browser (nothing changes when it was cancelled)."""
        if folder is not None:
            self.value = str(folder)
            self.cursor_position = len(self.value)
        self.focus()


class SetupScreen(Screen[None]):
    """Pick the exports folder, the results folder and the AI choice, then start a run."""

    # ctrl+y works from a folder field too (priority), and copies the full command even when the
    # one-line preview is cut short with an ellipsis.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+y", "copy_command", "Copy command", priority=True),
    ]

    # Nothing is focused when the screen opens (Tab moves into the first field), so q still quits.
    AUTO_FOCUS = ""  # "" focuses nothing (None would inherit the app's "*")

    def __init__(self, *, exports: str = "", results: str = "") -> None:
        super().__init__()
        self._initial = SetupChoices(exports=exports, results=results)
        self._rerun: RerunChoice | None = None
        # Set by a Resume or Force pick: once that pick makes the run ready, Start comes into view.
        self._reveal_start = False
        self._generation = 0
        self._command = command_preview()

    @property
    def active_bindings(self) -> dict[str, ActiveBinding]:
        """The keys in reach, global keys (Quit, Help) first so the footer lists them first (DESIGN.md)."""
        return global_keys_first(self.app, super().active_bindings)

    def compose(self) -> ComposeResult:
        yield AppHeader(id="hdr")
        with Body(id="body"):
            yield Static("Exports folder", classes="field-label", markup=False)
            with Horizontal(classes="field-row"):
                with Vertical(id="field-exports", classes="field"):
                    yield FolderInput(
                        title="Choose the exports folder", placeholder="/path/to/apigee-exports", id="input-exports"
                    )
                yield EdgeButton("Browse…", id="browse-exports", compact=True)
            yield Static("", id="input-exports-resolved", classes="resolved", markup=False)
            yield Static("", id="input-exports-hint", classes="hint", markup=False)
            yield Static("Results folder", id="label-results", classes="field-label", markup=False)
            with Horizontal(classes="field-row"):
                with Vertical(id="field-results", classes="field"):
                    yield FolderInput(
                        title="Choose the results folder", placeholder="/path/to/results", id="input-results"
                    )
                yield EdgeButton("Browse…", id="browse-results", compact=True)
            yield Static("", id="input-results-resolved", classes="resolved", markup=False)
            yield Static("", id="input-results-hint", classes="hint", markup=False)
            yield Static(
                "Results folder has an earlier run. Continue it or redo everything?",
                id="resume-prompt",
                classes="section-label gap-1",
                markup=False,
            )
            with Horizontal(id="resume-choice"):
                yield EdgeButton("Resume", id="btn-resume", compact=True)
                yield EdgeButton("Force (redo all)", id="btn-force", compact=True)
            yield Static("AI", id="label-ai", classes="section-label", markup=False)
            with RadioSet(id="radio-ai", compact=True, disabled=True):
                yield RadioButton("Claude (uses ANTHROPIC_API_KEY)", id="radio-ai-claude", compact=True)
                yield RadioButton("No AI", id="radio-ai-none", compact=True)
            yield Collapsible(
                title="Advanced options",
                collapsed=True,
                collapsed_symbol="▸",
                expanded_symbol="▾",
                id="collapsible-advanced",
                disabled=True,
            )
            yield Static("Command", id="label-command", classes="section-label", markup=False)
            yield Static(self._command, id="command-preview", markup=False)
            yield EdgeButton("Start", id="start", variant="primary", compact=True, disabled=True)
            yield Static(CHOOSE_RERUN_REASON, id="start-disabled-reason", markup=False)
        yield Footer(id="ftr", compact=True, show_command_palette=False)

    def on_mount(self) -> None:
        self._show_rerun_choice(False)
        self._show_hint("#input-exports-hint", FieldCheck())
        self._show_hint("#input-results-hint", FieldCheck())
        self._show_resolved("#input-exports-resolved", "")
        self._show_resolved("#input-results-resolved", "")
        self.query_one("#start-disabled-reason").display = False
        exports = self.query_one("#input-exports", Input)
        results = self.query_one("#input-results", Input)
        if self._initial.exports or self._initial.results:
            # Setting a value posts Input.Changed, which starts the check.
            exports.value = self._initial.exports
            results.value = self._initial.results

    # ------------------------------------------------------------------ current choices

    def choices(self) -> SetupChoices:
        """What is typed or picked right now."""
        return SetupChoices(
            exports=self.query_one("#input-exports", Input).value,
            results=self.query_one("#input-results", Input).value,
            rerun=self._rerun,
        )

    # ------------------------------------------------------------------ events

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        if event.input.id == "input-results":
            # A different results folder: an earlier Resume or Force pick no longer applies.
            self._rerun = None
            self._reveal_start = False
        self._show_resolved(f"#{event.input.id}-resolved", event.value)
        hint = f"#{event.input.id}-hint"
        self._show_hint(hint, FieldCheck(Status.NONE, CHECKING_TEXT if event.value.strip() else ""))
        self._check()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "btn-resume":
            self._rerun = RerunChoice.RESUME
            self._reveal_start = True
            self._check()
        elif event.button.id == "btn-force":
            self._rerun = RerunChoice.FORCE
            self._reveal_start = True
            self._check()
        elif event.button.id == "browse-exports":
            self.browse(self.query_one("#input-exports", FolderInput))
        elif event.button.id == "browse-results":
            self.browse(self.query_one("#input-results", FolderInput))
        elif event.button.id == "start":
            self.notify(START_NOT_BUILT, markup=False)

    def browse(self, field: FolderInput) -> None:
        """Open the folder browser for ``field``, at its folder (or the nearest one above it that exists)."""
        preview: Callable[[Path], FieldCheck]
        if field.id == "input-results":
            exports = folder_path(self.query_one("#input-exports", Input).value)
            preview = partial(preview_results, exports=exports)
        else:
            preview = preview_exports
        picker = FolderPicker(field.picker_title, start_folder(folder_path(field.value)), preview=preview)
        self.app.push_screen(picker, field.picked)

    def action_copy_command(self) -> None:
        """Copy the full, shell-quoted command for the current choices to the clipboard."""
        self.app.copy_to_clipboard(self._command)
        self.notify(COMMAND_COPIED, markup=False)

    # ------------------------------------------------------------------ checking

    def _check(self) -> None:
        """Show the command for the current choices at once, then check the folders in a worker."""
        self._generation += 1
        choices = self.choices()
        self._command = command_preview(choices)
        self.query_one("#command-preview", Static).update(self._command)
        self._set_start(False, None)
        self._mark_rerun_buttons(choices.rerun)
        self._run_check(self._generation, choices)

    @work(thread=True, exclusive=True, group="setup-check", exit_on_error=False)
    def _run_check(self, generation: int, choices: SetupChoices) -> None:
        time.sleep(CHECK_DELAY_SECONDS)
        worker = get_current_worker()
        if worker.is_cancelled:
            return
        try:
            result = check_setup(choices)
        except (A2mError, OSError, RuntimeError, ValueError) as exc:  # show what went wrong, never crash the app
            message = f"Could not check the folders: {exc}"
            result = SetupCheck(choices, FieldCheck.invalid(message), FieldCheck.invalid(message))
        if not worker.is_cancelled:
            self.app.call_from_thread(self._apply, generation, result)

    def _apply(self, generation: int, result: SetupCheck) -> None:
        if generation != self._generation:
            return  # the choices changed while this check ran; a newer check is on its way
        self._show_hint("#input-exports-hint", result.exports)
        self._show_hint("#input-results-hint", result.results)
        self._show_rerun_choice(result.needs_rerun_choice)
        reason = CHOOSE_RERUN_REASON if result.needs_rerun_choice and result.choices.rerun is None else None
        self._set_start(result.ready, reason)
        if self._reveal_start and result.choices.rerun is not None:
            self._reveal_start = False
            if result.ready:
                # The pick completed the form: keep Start in view (only a terminal under 24 rows scrolls).
                self.call_after_refresh(self.query_one("#start").scroll_visible, animate=False)

    # ------------------------------------------------------------------ drawing

    def _show_hint(self, selector: str, check: FieldCheck) -> None:
        hint = self.query_one(selector, Static)
        field = self.query_one(selector.removesuffix("-hint"), Input)
        hint.update(check.text)
        hint.display = bool(check.text)
        hint.set_class(check.status is Status.VALID, "-valid")
        hint.set_class(check.status is Status.INVALID, "-invalid")
        # The border is the field's box around the Input (Textual's Input sets its own -valid/-invalid).
        box = field.parent
        assert isinstance(box, Vertical)
        box.set_class(check.status is Status.VALID, "-valid")
        box.set_class(check.status is Status.INVALID, "-invalid")

    def _show_resolved(self, selector: str, text: str) -> None:
        """Under a field, the absolute folder a typed path names, when that is not just what was typed."""
        line = self.query_one(selector, Static)
        resolved = resolved_folder(text)
        shown = resolved is not None and str(resolved) not in (text, text.rstrip(os.sep))
        line.display = shown
        line.update(f"→ {resolved}" if shown else "")
        self._fit_rerun_gap()

    def _fit_rerun_gap(self) -> None:
        """Drop the blank row above the Resume prompt when both absolute folder lines show, so 80x24 fits."""
        both = all(self.query_one(f"#input-{name}-resolved").display for name in ("exports", "results"))
        self.query_one("#resume-prompt").set_class(not both, "gap-1")

    def _show_rerun_choice(self, shown: bool) -> None:
        self.query_one("#resume-prompt").display = shown
        self.query_one("#resume-choice").display = shown

    def _mark_rerun_buttons(self, rerun: RerunChoice | None) -> None:
        self.query_one("#btn-resume", Button).variant = "primary" if rerun is RerunChoice.RESUME else "default"
        self.query_one("#btn-force", Button).variant = "error" if rerun is RerunChoice.FORCE else "default"

    def _set_start(self, enabled: bool, reason: str | None) -> None:
        self.query_one("#start", Button).disabled = not enabled
        line = self.query_one("#start-disabled-reason", Static)
        line.display = reason is not None
        if reason is not None:
            line.update(reason)

    # ------------------------------------------------------------------ scrolling

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        """Scroll the newly focused control into view; a folder field brings its label and bordered box."""
        widget = event.widget
        box = widget.parent
        row = box.parent if box is not None else None
        if isinstance(widget, FolderInput) and isinstance(row, Widget):
            body = self.query_one(Body)
            siblings = list(body.children)
            index = siblings.index(row)
            region = row.virtual_region_with_margin
            if index > 0:
                region = region.union(siblings[index - 1].virtual_region_with_margin)
            body.scroll_to_region(region, animate=False, immediate=True)
        else:
            widget.scroll_visible(animate=False)
