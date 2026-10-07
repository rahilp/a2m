"""The review walkthrough: step through the needs-review proxies one by one.

The results screen opens it ("Review", ``r``) on the first needs-review proxy, in the order the results
screen lists them (summary.json's order, which a2m writes sorted by name). The screen shows "proxy N of M",
the proxy's REPORT.md (Report view) or its diffs/ files (Diffs view: the file list, and the chosen file's
text), and the proxy's folder path, which ``c`` copies. ``n`` and ``p`` move to the next and previous proxy;
moving past the last one says the walkthrough is finished, and ``escape`` (or "Back to summary") returns to
the results screen at any time.

Everything is read in a worker thread through the read-only helpers in :mod:`a2m.tui.read`: nothing is
written, nothing reached through a link is opened (the same refusal a2m applies), and a large file shows its
first lines with a line saying so. REPORT.md and diffs are untrusted text from the user's bundles, recorded
responses and AI output, so they are shown literally: never parsed as markup, and control characters (an
escape sequence, a carriage return) are shown as visible symbols (``␛``) instead of reaching the terminal,
where they would be interpreted rather than shown.
"""

from __future__ import annotations

import asyncio
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

from textual import events, work
from textual.app import ComposeResult
from textual.binding import ActiveBinding, Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.screen import Screen
from textual.widgets import Button, OptionList, Static
from textual.widgets.option_list import Option

from a2m import layout
from a2m.tui.frame import AppFooter, AppHeader, EdgeButton, global_keys_first
from a2m.tui.picker import printable
from a2m.tui.read import (
    MORE_DIFFS_NOTE,
    NO_DIFFS_TEXT,
    NO_REPORT_TEXT,
    DiffListing,
    Results,
    ResultsProxy,
    ReviewProxy,
    ShownFile,
    load_review_proxy,
    read_shown_file,
    truncated_note,
)

LOADING_TEXT = "Loading…"
COPY_HINT = " (press c to copy)"
PATH_COPIED = "Path copied"
DONE_TEXT = "Review finished. All {total} needs-review proxies have been seen."
DONE_ONE_TEXT = "Review finished. The 1 needs-review proxy has been seen."
FILES_LABEL = "Files"
SHOWN_MARK = "▸ "
OTHER_MARK = "  "

# C0 control characters (newline and tab kept) as their Unicode Control Pictures, DEL as ␡, C1 controls as �.
_LITERAL = {code: chr(0x2400 + code) for code in range(0x20) if chr(code) not in "\n\t"}
_LITERAL[0x7F] = "␡"
_LITERAL.update({code: "�" for code in range(0x80, 0xA0)})


def literal(text: str) -> str:
    """``text`` safe to draw: line endings normalised, every other control character a visible symbol, so
    nothing in it can act as a terminal control sequence. Markup is never parsed where it is shown."""
    return text.replace("\r\n", "\n").translate(_LITERAL)


class View(StrEnum):
    REPORT = "report"
    DIFFS = "diffs"


def shown_text(shown: ShownFile) -> Content:
    """A file's text as plain content, with the truncation note (muted, italic) when it was cut."""
    if shown.error is not None:
        return Content.styled(f"✗ {literal(shown.error)}", "bold $error")
    text = Content(literal(shown.text))
    if shown.truncated_at is not None:
        note = Content.styled(truncated_note(shown.truncated_at), "italic $foreground-muted")
        text = text.rstrip() + Content("\n\n") + note
    return text


class ReviewScreen(Screen[None]):
    """Walk the needs-review proxies of ``results``, starting at ``index`` (``len`` = finished) in ``view``."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("n", "next", "Next"),
        Binding("p", "previous", "Previous"),
        Binding("tab", "toggle_view", "Report/Diffs"),
        Binding("d", "toggle_view", "Report/Diffs", show=False),
        Binding("c", "copy_path", "Copy path"),
        Binding("escape", "back", "Back to summary", show=False),
    ]
    AUTO_FOCUS = ""

    def __init__(self, results: Results, *, index: int = 0, view: View = View.REPORT) -> None:
        super().__init__()
        self._root = results.folder
        self._proxies: tuple[ResultsProxy, ...] = results.by_bucket[layout.NEEDS_REVIEW_DIR_NAME]
        self._index = max(0, min(index, len(self._proxies)))
        self._view = view
        self._loaded: ReviewProxy | None = None
        self._shown_file: str | None = None
        # Bumped on every move, so a read that finishes after the user moved on is dropped.
        self._token = 0

    # ------------------------------------------------------------------ state

    @property
    def done(self) -> bool:
        return self._index >= len(self._proxies)

    @property
    def index(self) -> int:
        return self._index

    @property
    def view(self) -> View:
        return self._view

    def proxy_path(self, proxy: ResultsProxy) -> Path:
        """The proxy's absolute results folder (where it would be, when the results screen refused it)."""
        return proxy.folder or layout.bucket_proxy_dir(self._root, proxy.bucket, proxy.name)

    @property
    def active_bindings(self) -> dict[str, ActiveBinding]:
        """Global keys first, then this screen's in the order they are declared (n, p, tab, c)."""
        order = {binding.key: rank for rank, binding in enumerate(self.BINDINGS) if isinstance(binding, Binding)}
        bindings = global_keys_first(self.app, super().active_bindings)
        return dict(sorted(bindings.items(), key=lambda item: order.get(item[0], -1)))

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in ("next", "toggle_view", "copy_path"):
            return not self.done
        if action == "previous":
            if self.done:
                return False
            return None if self._index == 0 else True
        return True

    # ------------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        yield AppHeader(id="hdr")
        with Vertical(id="body"):
            yield Static("", id="review-position", markup=False)
            with Horizontal(id="review-tabs", classes="gap-1"):
                yield Static(" Report ", id="review-tab-report", classes="review-tab", markup=False)
                yield Static(" Diffs ", id="review-tab-diffs", classes="review-tab", markup=False)
            with VerticalScroll(id="review-report-pane", classes="gap-1 review-pane"):
                yield Static("", id="review-report", markup=False)
            with Vertical(id="review-diffs-view", classes="gap-1"):
                yield Static(NO_DIFFS_TEXT, id="review-no-diffs", markup=False)
                yield Static(FILES_LABEL, id="review-files-label", classes="section-label", markup=False)
                yield OptionList(id="review-diffs-list")
                with VerticalScroll(id="review-diff-pane", classes="gap-1 review-pane"):
                    yield Static("", id="review-diff-content", markup=False)
            yield Static("", id="review-path", classes="gap-1", markup=False)
            yield Static("", id="review-done", classes="section-label gap-2", markup=False)
            with Horizontal(id="review-buttons", classes="gap-1"):
                yield EdgeButton("Previous", id="review-previous", compact=True)
                yield EdgeButton("Next", id="review-next", variant="primary", compact=True)
                yield EdgeButton("Back to summary", id="review-back", compact=True)
        yield AppFooter(id="ftr", compact=True, show_command_palette=False)

    def on_mount(self) -> None:
        self._go(self._index)

    # ------------------------------------------------------------------ moving

    def _go(self, index: int) -> None:
        """Show proxy ``index`` (or the finished state), clearing the last proxy's text at once."""
        self._index = index
        self._token += 1
        self._loaded = None
        self._shown_file = None
        done = self.done
        for widget_id in ("#review-position", "#review-tabs", "#review-path", "#review-previous", "#review-next"):
            self.query_one(widget_id).display = not done
        self.query_one("#review-done").display = done
        back = self.query_one("#review-back", Button)
        back.variant = "primary" if done else "default"
        if done:
            total = len(self._proxies)
            self.query_one("#review-done", Static).update(
                DONE_ONE_TEXT if total == 1 else DONE_TEXT.format(total=total)
            )
            self.query_one("#review-report-pane").display = False
            self.query_one("#review-diffs-view").display = False
            back.focus()
        else:
            proxy = self._proxies[index]
            position = f"proxy {index + 1} of {len(self._proxies)} · "
            self.query_one("#review-position", Static).update(
                Content(position) + Content.styled(printable(proxy.name), "bold")
            )
            path = Content(printable(str(self.proxy_path(proxy))))
            self.query_one("#review-path", Static).update(path + Content.styled(COPY_HINT, "italic $foreground-muted"))
            self.query_one("#review-previous", Button).disabled = index == 0
            self.query_one("#review-report", Static).update(LOADING_TEXT)
            self._show_loading_diffs()
            self._show_view()
            self._load(self._token, proxy)
        self.refresh_bindings()

    def _show_view(self) -> None:
        report = self._view is View.REPORT
        self.query_one("#review-report-pane").display = report
        self.query_one("#review-diffs-view").display = not report
        self.query_one("#review-tab-report").set_class(report, "-active")
        self.query_one("#review-tab-diffs").set_class(not report, "-active")
        if report:
            self.query_one("#review-report-pane").focus()
        else:
            diffs_list = self.query_one("#review-diffs-list", OptionList)
            if diffs_list.display:
                diffs_list.focus()
            else:
                self.query_one("#review-diff-pane").focus()

    # ------------------------------------------------------------------ loading

    @work(exclusive=True, group="review-load", exit_on_error=False)
    async def _load(self, token: int, proxy: ResultsProxy) -> None:
        """Read the proxy's REPORT.md and diffs/ in a thread (never on the app's event loop), then show them."""
        folder_text = printable(str(self.proxy_path(proxy)))
        loaded = await asyncio.to_thread(load_review_proxy, self._root, proxy.folder, folder_text)
        if token != self._token or not self.is_attached:
            return
        self._loaded = loaded
        self.query_one("#review-report", Static).update(self._report_content(loaded.report))
        self._show_diffs(loaded.diffs, loaded.diffs.files[0] if loaded.diffs.files else None, loaded.first_diff)
        self.query_one("#review-report-pane", VerticalScroll).scroll_home(animate=False)

    @work(exclusive=True, group="review-diff", exit_on_error=False)
    async def _load_diff(self, token: int, folder: Path, name: str) -> None:
        target = folder / layout.DIFFS_DIR_NAME / name
        shown = await asyncio.to_thread(read_shown_file, self._root, target)
        if token != self._token or self._shown_file != name or not self.is_attached:
            return
        self._show_diff_text(shown)

    @staticmethod
    def _report_content(report: ShownFile) -> Content:
        if report.missing is not None:
            return Content.styled(NO_REPORT_TEXT, "italic $foreground-muted")
        return shown_text(report)

    def _show_loading_diffs(self) -> None:
        self.query_one("#review-no-diffs").display = False
        self.query_one("#review-files-label").display = False
        diffs_list = self.query_one("#review-diffs-list", OptionList)
        diffs_list.clear_options()
        diffs_list.display = False
        self.query_one("#review-diff-pane").display = True
        self.query_one("#review-diff-content", Static).update(LOADING_TEXT)

    def _show_diffs(self, listing: DiffListing, shown_name: str | None, shown: ShownFile | None) -> None:
        """The file list (``shown_name`` marked) and ``shown``'s text; the no-diffs line when there is nothing."""
        self._shown_file = shown_name
        nothing = not listing.entries and listing.error is None
        no_diffs = self.query_one("#review-no-diffs", Static)
        no_diffs.display = nothing
        self.query_one("#review-files-label").display = not nothing
        diffs_list = self.query_one("#review-diffs-list", OptionList)
        diffs_list.display = not nothing
        self.query_one("#review-diff-pane").display = not nothing
        if nothing:
            if self._view is View.DIFFS:
                self.query_one("#review-diff-pane").focus()
            return
        options: list[Option] = []
        for entry in listing.entries:
            if entry.refused is None:
                mark = SHOWN_MARK if entry.name == shown_name else OTHER_MARK
                options.append(Option(Content(mark + printable(entry.name)), id=entry.name))
            else:
                options.append(Option(Content(f"✗ {printable(entry.name)} ({entry.refused})"), disabled=True))
        if listing.error is not None:
            options.append(Option(Content(f"✗ {literal(listing.error)}"), disabled=True))
        if listing.more:
            options.append(Option(Content(MORE_DIFFS_NOTE.format(count=len(listing.entries))), disabled=True))
        highlighted = diffs_list.highlighted
        diffs_list.clear_options()
        diffs_list.add_options(options)
        names = [entry.name for entry in listing.entries]
        if shown_name is not None:
            diffs_list.highlighted = names.index(shown_name) if highlighted is None else highlighted
        if shown is not None:
            self._show_diff_text(shown)
        else:
            self.query_one("#review-diff-content", Static).update("")
        if self._view is View.DIFFS and not isinstance(self.focused, Button):
            diffs_list.focus()

    def _show_diff_text(self, shown: ShownFile) -> None:
        if shown.missing is not None:
            text = Content.styled(f"{printable(shown.missing)} is no longer there.", "italic $foreground-muted")
        else:
            text = shown_text(shown)
        self.query_one("#review-diff-content", Static).update(text)
        self.query_one("#review-diff-pane", VerticalScroll).scroll_home(animate=False)

    # ------------------------------------------------------------------ actions

    def action_next(self) -> None:
        if not self.done:
            self._go(self._index + 1)

    def action_previous(self) -> None:
        if not self.done and self._index > 0:
            self._go(self._index - 1)

    def action_toggle_view(self) -> None:
        if self.done:
            return
        self._view = View.DIFFS if self._view is View.REPORT else View.REPORT
        self._show_view()
        self.refresh_bindings()

    def action_copy_path(self) -> None:
        """Copy the proxy's folder path (it is also shown, for terminals that ignore clipboard requests)."""
        if self.done:
            return
        self.app.copy_to_clipboard(str(self.proxy_path(self._proxies[self._index])))
        self.notify(PATH_COPIED, markup=False)

    def action_back(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "review-next":
            self.action_next()
        elif event.button.id == "review-previous":
            self.action_previous()
        elif event.button.id == "review-back":
            self.action_back()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Open the chosen diffs/ file."""
        event.stop()
        name = event.option.id
        loaded = self._loaded
        if name is None or loaded is None or self.done or name == self._shown_file:
            return
        proxy = self._proxies[self._index]
        if proxy.folder is None:
            return
        highlighted = event.option_index
        self._show_diffs(loaded.diffs, name, None)
        self.query_one("#review-diffs-list", OptionList).highlighted = highlighted
        self.query_one("#review-diff-content", Static).update(LOADING_TEXT)
        self._load_diff(self._token, proxy.folder, name)

    def on_click(self, event: events.Click) -> None:
        """A click on the inactive tab switches to it."""
        widget = event.widget
        if widget is None or self.done:
            return
        wanted = {"review-tab-report": View.REPORT, "review-tab-diffs": View.DIFFS}.get(widget.id or "")
        if wanted is not None and wanted is not self._view:
            self.action_toggle_view()
