"""The folder browser: a dialog for choosing the exports or results folder, one folder at a time.

It shows the folder it is in as a full path, lists only that folder's sub-folders, and previews the
highlighted folder with the engine's own read-only checks (how many proxies it holds, or whether it
could hold results). Enter opens a folder, Backspace goes up, ``~`` goes home and typing letters jumps
to a matching name. It only reads folders; choosing one fills the setup screen's field, exactly as
typing the path would. Folder names are shown as plain text, never read as Textual or Rich markup.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from textual import events, work
from textual.app import ComposeResult
from textual.binding import ActiveBinding, Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.screen import ModalScreen
from textual.suggester import Suggester
from textual.widgets import Button, Footer, OptionList, Static
from textual.widgets.option_list import Option
from textual.worker import get_current_worker

from a2m.errors import A2mError
from a2m.layout import collision_key
from a2m.tui.folders import FieldCheck, Status
from a2m.tui.frame import EdgeButton, global_keys_first

# At most this many sub-folders are listed (or looked at when completing a path), so a huge folder
# cannot stall the app.
MAX_FOLDERS = 2000
# The preview starts this long after the highlight stops moving, so holding an arrow key does not scan
# every folder on the way.
PREVIEW_DELAY_SECONDS = 0.15
# Letters typed within this long of each other jump to a name starting with all of them.
TYPE_AHEAD_SECONDS = 1.0
THIS_FOLDER = ". (this folder)"
CHECKING_TEXT = "Checking…"


@dataclass(frozen=True, slots=True)
class Subfolder:
    name: str
    path: Path
    readable: bool


@dataclass(frozen=True, slots=True)
class Listing:
    """The sub-folders of one folder, or why they could not be read."""

    folders: tuple[Subfolder, ...] = ()
    error: str | None = None
    truncated: bool = False


def list_subfolders(folder: Path, *, limit: int = MAX_FOLDERS) -> Listing:
    """The non-hidden sub-folders of ``folder``, sorted by name; read-only and never raises OSError."""
    found: list[Subfolder] = []
    truncated = False
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                try:
                    if not entry.is_dir():
                        continue
                except OSError:
                    continue
                if len(found) >= limit:
                    truncated = True
                    break
                path = Path(entry.path)
                found.append(Subfolder(entry.name, path, os.access(path, os.R_OK | os.X_OK)))
    except OSError as exc:
        return Listing(error=exc.strerror or str(exc))
    found.sort(key=lambda sub: (collision_key(sub.name), sub.name))
    return Listing(tuple(found), truncated=truncated)


def printable(text: str) -> str:
    """``text`` with control characters (e.g. a newline in a folder name) shown as ``?``, so it stays one line."""
    return "".join(ch if ch.isprintable() else "?" for ch in text)


def display_path(path: Path) -> str:
    """``path`` in full, with ``~`` for the home folder."""
    home = Path.home()
    try:
        inside = path.relative_to(home)
    except ValueError:
        text = str(path)
    else:
        text = "~" if str(inside) == "." else f"~{os.sep}{inside}"
    return printable(text)


def start_folder(text_path: Path | None) -> Path:
    """Where the browser opens: the nearest existing folder at or above ``text_path``, else the home folder."""
    if text_path is not None:
        absolute = Path(os.path.abspath(text_path))
        for folder in (absolute, *absolute.parents):
            try:
                if folder.is_dir():
                    return folder
            except OSError:
                continue
    return Path.home()


def complete_folder(value: str) -> str | None:
    """``value`` completed to the first sub-folder whose name starts with its last part, plus a separator.

    ``~`` alone completes to ``~/``. Nothing is suggested for a blank value or one ending in a separator
    (so Tab then moves on), and hidden folders only when the typed part starts with a dot. Read-only.
    """
    if not value.strip() or value.endswith(os.sep):
        return None
    head, sep, tail = value.rpartition(os.sep)
    if not sep:
        if value == "~":
            return value + os.sep
        if value.startswith("~"):
            return None
        parent_text, base = "", Path(".")
    else:
        parent_text = head + sep
        base = Path(parent_text).expanduser()
    names: list[str] = []
    try:
        with os.scandir(base) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_FOLDERS:
                    break
                if not entry.name.startswith(tail) or (entry.name.startswith(".") and not tail.startswith(".")):
                    continue
                try:
                    if entry.is_dir():
                        names.append(entry.name)
                except OSError:
                    continue
    except OSError:
        return None
    if not names:
        return None
    return f"{parent_text}{min(names)}{os.sep}"


class FolderSuggester(Suggester):
    """Suggests the rest of a folder path as it is typed (Tab or Right accepts it)."""

    def __init__(self) -> None:
        super().__init__(use_cache=False, case_sensitive=True)

    async def get_suggestion(self, value: str) -> str | None:
        return await asyncio.to_thread(complete_folder, value)


class FolderList(OptionList):
    """The current folder's sub-folders; typed letters jump to a matching name instead of reaching other keys."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("enter", "select", "Open")]

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._typed = ""
        self._typed_at = 0.0
        # The folder each row stands for: row 0 is the current folder, None marks a message row.
        self.rows: list[Subfolder | None] = []

    def check_consume_key(self, key: str, character: str | None) -> bool:
        # Letters go to the type-ahead (so q does not quit from here); ~ is left for "home".
        return character is not None and character.isprintable() and character != "~"

    def on_key(self, event: events.Key) -> None:
        character = event.character
        if character is None or not character.isprintable() or character == "~":
            return
        event.stop()
        event.prevent_default()
        now = time.monotonic()
        typed = self._typed + character if now - self._typed_at < TYPE_AHEAD_SECONDS else character
        self._typed_at = now
        index = self._first_match(typed)
        if index is None and len(typed) > 1:
            typed = character
            index = self._first_match(typed)
        self._typed = typed
        if index is not None:
            self.highlighted = index

    def _first_match(self, typed: str) -> int | None:
        wanted = collision_key(typed)
        for index, row in enumerate(self.rows):
            if index > 0 and row is not None and collision_key(row.name).startswith(wanted):
                return index
        return None


class FolderPicker(ModalScreen[Path | None]):
    """Browse to a folder and use it; dismisses with the folder chosen, or None when cancelled.

    ``preview`` says what a2m makes of a folder (run in a worker thread, read-only); its line under the
    list follows the highlighted row, and row 0 (". (this folder)") stands for the folder shown at the top.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        # Typed letters are the type-ahead here, so the footer shows ctrl+q for Quit.
        Binding("ctrl+q", "app.quit", "Quit", priority=True),
        Binding("backspace", "parent", "Up a folder"),
        Binding("tilde", "home", "Home", key_display="~"),
        Binding("ctrl+u", "use", "Use this folder"),
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, title: str, start: Path, *, preview: Callable[[Path], FieldCheck]) -> None:
        super().__init__()
        self._title = title
        self._preview = preview
        self._current = start
        self._generation = 0

    @property
    def active_bindings(self) -> dict[str, ActiveBinding]:
        """The keys in reach, Quit first so the footer lists it first (DESIGN.md)."""
        return global_keys_first(self.app, super().active_bindings)

    @property
    def current(self) -> Path:
        """The folder shown at the top."""
        return self._current

    def compose(self) -> ComposeResult:
        with Vertical(id="picker"):
            yield Static(self._title, id="picker-title", markup=False)
            yield Static("", id="picker-path", markup=False)
            yield FolderList(id="picker-list")
            yield Static("", id="picker-preview", markup=False)
            with Horizontal(id="picker-buttons"):
                yield EdgeButton("Use this folder", id="picker-use", variant="primary", compact=True)
                yield EdgeButton("Cancel", id="picker-cancel", compact=True)
        yield Footer(compact=True, show_command_palette=False)

    def on_mount(self) -> None:
        self._open(self._current)
        self.query_one(FolderList).focus()

    # ------------------------------------------------------------------ events and keys

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()
        self._show_preview_for(event.option_index)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        rows = self.query_one(FolderList).rows
        row = rows[event.option_index] if event.option_index < len(rows) else None
        if event.option_index == 0:
            self.dismiss(self._current)
        elif row is not None and row.readable:
            self._open(row.path)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "picker-use":
            self.action_use()
        else:
            self.action_cancel()

    def action_use(self) -> None:
        """Use the highlighted folder (row 0: the folder shown at the top)."""
        self.dismiss(self._target())

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_parent(self) -> None:
        """Go up to the folder above, with the folder just left highlighted."""
        parent = self._current.parent
        if parent != self._current:
            self._open(parent, highlight=self._current)

    def action_home(self) -> None:
        self._open(Path.home())

    # ------------------------------------------------------------------ drawing

    def _open(self, folder: Path, *, highlight: Path | None = None) -> None:
        """Show ``folder``: its path at the top and its sub-folders in the list."""
        self._current = folder
        self.query_one("#picker-path", Static).update(display_path(folder))
        listing = list_subfolders(folder)
        rows: list[Subfolder | None] = [Subfolder(THIS_FOLDER, folder, readable=True)]
        options: list[Option] = [Option(Content(THIS_FOLDER))]
        for sub in listing.folders:
            rows.append(sub)
            label = f"▸ {printable(sub.name)}" if sub.readable else f"✗ {printable(sub.name)} (cannot read)"
            options.append(Option(Content(label)))
        if listing.error is not None:
            rows.append(None)
            options.append(Option(Content(f"✗ cannot read this folder: {printable(listing.error)}"), disabled=True))
        elif not listing.folders:
            rows.append(None)
            options.append(Option(Content("No subfolders"), disabled=True))
        if listing.truncated:
            rows.append(None)
            options.append(Option(Content(f"Showing the first {MAX_FOLDERS} folders"), disabled=True))
        folder_list = self.query_one(FolderList)
        folder_list.rows = rows
        folder_list.clear_options()
        folder_list.add_options(options)
        index = next(
            (i for i, row in enumerate(rows) if i > 0 and row is not None and row.path == highlight),
            0,
        )
        folder_list.highlighted = index
        self._show_preview_for(index)

    def _target(self) -> Path:
        folder_list = self.query_one(FolderList)
        index = folder_list.highlighted
        row = folder_list.rows[index] if index is not None and index < len(folder_list.rows) else None
        return self._current if row is None else row.path

    def _show_preview_for(self, index: int | None) -> None:
        rows = self.query_one(FolderList).rows
        row = rows[index] if index is not None and index < len(rows) else None
        self._generation += 1
        if row is not None and not row.readable:
            self._apply_preview(self._generation, FieldCheck.invalid(f"cannot read {row.path}: permission denied"))
            return
        self._apply_preview(self._generation, FieldCheck(Status.NONE, CHECKING_TEXT))
        self._run_preview(self._generation, self._current if row is None else row.path)

    @work(thread=True, exclusive=True, group="folder-preview", exit_on_error=False)
    def _run_preview(self, generation: int, folder: Path) -> None:
        time.sleep(PREVIEW_DELAY_SECONDS)
        worker = get_current_worker()
        if worker.is_cancelled:
            return
        try:
            check = self._preview(folder)
        except (A2mError, OSError, RuntimeError, ValueError) as exc:  # show what went wrong, never crash
            check = FieldCheck.invalid(f"Could not check {folder}: {exc}")
        if not worker.is_cancelled:
            self.app.call_from_thread(self._apply_preview, generation, check)

    def _apply_preview(self, generation: int, check: FieldCheck) -> None:
        if generation != self._generation:
            return  # the highlight moved on; a newer preview is on its way
        line = self.query_one("#picker-preview", Static)
        line.update(printable(check.text))
        line.set_class(check.status is Status.VALID, "-valid")
        line.set_class(check.status is Status.INVALID, "-invalid")
