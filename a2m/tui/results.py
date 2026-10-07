"""The results screen: a run's SUMMARY.md and its proxies grouped by bucket.

The screen opens on a finished run (the run screen hands over to it) or on an earlier results folder
chosen from setup ("Open results"). It loads the folder in a worker thread through the read-only helper
(:mod:`a2m.tui.read`), so a large summary never stalls the app, and never writes to the folder. The bucket
counts and lists come from summary.json, in the fixed order verified, needs-review, unsupported, each list
in its own fixed-height pane that scrolls; SUMMARY.md is rendered below in its own scrolling pane, so the
screen itself fits 80x24. A folder that is not a2m results, or has no SUMMARY.md yet, shows one plain
message instead, with a way back to setup.

Everything read from the folder is shown as plain text: proxy names never as markup, and SUMMARY.md as
Markdown whose links are never followed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import ClassVar

from markdown_it import MarkdownIt
from textual import work
from textual.app import ComposeResult
from textual.binding import ActiveBinding, Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.geometry import Size
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Button, Markdown, Static

# The block classes Markdown.BLOCKS maps to; Textual 8 (pinned <9) defines them only in this private module.
from textual.widgets._markdown import MarkdownBlock, MarkdownH1, MarkdownTable, MarkdownTD

from a2m.layout import BUCKET_DIR_NAMES, NEEDS_REVIEW_DIR_NAME, UNSUPPORTED_DIR_NAME, VERIFIED_DIR_NAME
from a2m.tui.frame import AppFooter, AppHeader, EdgeButton, global_keys_first
from a2m.tui.picker import printable
from a2m.tui.read import Problem, Results, ResultsProblem, load_results
from a2m.tui.review import ReviewScreen
from a2m.tui.run import BUCKET_MARKS

# The label each bucket goes by on screen (DESIGN.md Color, Bucket colors).
BUCKET_LABELS: dict[str, str] = {
    VERIFIED_DIR_NAME: "verified",
    NEEDS_REVIEW_DIR_NAME: "needs review",
    UNSUPPORTED_DIR_NAME: "unsupported",
}
LOADING_TEXT = "Loading results…"
EMPTY_BUCKET = "None"
NOTHING_TO_REVIEW = "Nothing needs review. Every proxy is verified or unsupported."
SUMMARY_LABEL = "SUMMARY.md"
# Each bucket's theme color (the same colors as app.tcss's .bucket-* classes).
BUCKET_THEME_COLORS: dict[str, str] = {
    VERIFIED_DIR_NAME: "$success",
    NEEDS_REVIEW_DIR_NAME: "$warning",
    UNSUPPORTED_DIR_NAME: "$error",
}


def summary_parser() -> MarkdownIt:
    """Markdown for text read from the results folder: no raw HTML, and no bare word (``REPORT.md``) turned
    into a link."""
    return MarkdownIt("gfm-like", {"html": False}).disable("linkify")


class SummaryH1(MarkdownH1):
    """SUMMARY.md's title drawn as the prototype draws it: ``# `` + the heading text, flush left (DESIGN.md
    Markdown viewer)."""

    def set_content(self, content: Content) -> None:
        super().set_content(Content("# ") + content)


class SummaryTD(MarkdownTD):
    """A table cell that colors a bucket marker cell (``✓ verified``, ``! needs review``, ``✗ unsupported``) in
    its bucket color, as the bucket lists do (DESIGN.md Color, Bucket colors)."""

    def set_content(self, content: Content) -> None:
        color = BUCKET_CELL_COLORS.get(content.plain.strip())
        super().set_content(content.stylize(color) if color else content)


# A table cell holding exactly a bucket's marker and label, and the theme color it is drawn in.
BUCKET_CELL_COLORS: dict[str, str] = {
    f"{symbol} {BUCKET_LABELS[bucket]}": BUCKET_THEME_COLORS[bucket] for bucket, (symbol, _css) in BUCKET_MARKS.items()
}


# The light box the prototype's table is drawn in, its rules in the `border` color (DESIGN.md Markdown viewer).
RULE_STYLE = "$border"
HEADER_STYLE = "bold $foreground"
# The narrowest the last (widest) column is squeezed to before the other columns give up width.
MIN_LAST_COLUMN = 10


def table_column_widths(natural: list[int], width: int) -> list[int]:
    """Text widths for a table ``width`` cells wide whose columns would like ``natural`` cells: every column
    but the last as wide as its content, the last taking the rest; when that does not fit, the widest of the
    other columns narrow first. Each column also spends 2 cells on padding and the table 1 + N on rules."""
    count = len(natural)
    room = max(width - (count + 1) - 2 * count, count)
    widths = [max(n, 1) for n in natural[:-1]]
    keep_last = min(max(natural[-1], 1), MIN_LAST_COLUMN)
    while widths and sum(widths) + keep_last > room and max(widths) > 1:
        widths[widths.index(max(widths))] -= 1
    return [*widths, max(room - sum(widths), 1)]


class SummaryTableGrid(Widget):
    """A SUMMARY.md table drawn as a terminal box table: ``│`` column dividers, one rule under the bold
    centered header row, the data rows directly below one another (long cells wrap inside their column) and a
    bottom rule, every rule in the `border` color. There is no top rule, so at 80x24 the header, its rule and
    the first data row fit the rows the prototype shows them in. Cell text is the parsed Markdown content,
    never markup, and its links are reported to the table, never opened."""

    DEFAULT_CSS = """
    SummaryTableGrid {
        width: 1fr;
        height: auto;
    }
    """

    def __init__(self, headers: list[Content], rows: list[list[Content]]) -> None:
        super().__init__()
        columns = max(len(headers), *(len(row) for row in rows), 1) if rows else max(len(headers), 1)
        self._headers = self._pad(headers, columns)
        self._rows = [self._pad(row, columns) for row in rows]

    @staticmethod
    def _pad(cells: list[Content], columns: int) -> list[Content]:
        return [*cells, *[Content()] * (columns - len(cells))][:columns]

    def _lines(self, width: int) -> list[Content]:
        natural = [max(cell.cell_length for cell in column) for column in zip(self._headers, *self._rows, strict=True)]
        widths = table_column_widths(natural, width)

        def rule(left: str, middle: str, right: str) -> Content:
            return Content.styled(left + middle.join("─" * (w + 2) for w in widths) + right, RULE_STYLE)

        def row_lines(cells: list[Content], header: bool) -> list[Content]:
            wrapped = [cell.wrap(w) or [Content()] for cell, w in zip(cells, widths, strict=True)]
            divider = Content.styled("│", RULE_STYLE)
            lines = []
            for y in range(max(len(cell) for cell in wrapped)):
                line = divider
                for cell, w in zip(wrapped, widths, strict=True):
                    text = (cell[y] if y < len(cell) else Content()).rstrip()
                    spare = max(w - text.cell_length, 0)
                    left = spare // 2 if header else 0
                    text = text.pad(left, spare - left)
                    if header:
                        text = text.stylize_before(HEADER_STYLE)
                    line = line + Content(" ") + text + Content(" ") + divider
                lines.append(line)
            return lines

        lines = [*row_lines(self._headers, header=True), rule("├", "┼", "┤")]
        for row in self._rows:
            lines.extend(row_lines(row, header=False))
        lines.append(rule("└", "┴", "┘"))
        return lines

    def get_content_width(self, container: Size, viewport: Size) -> int:
        return container.width

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        return len(self._lines(width))

    def render(self) -> Content:
        return Content("\n").join(self._lines(self.content_size.width))

    async def action_link(self, href: str) -> None:
        """A link in a cell: hand it to the table, which reports it as clicked (never opened, see
        :meth:`ResultsScreen.on_markdown_link_clicked`)."""
        if isinstance(self.parent, MarkdownTable):
            await self.parent.action_link(href)


class SummaryTable(MarkdownTable):
    """A SUMMARY.md table drawn by :class:`SummaryTableGrid`."""

    def compose(self) -> ComposeResult:
        headers, rows = self._get_headers_and_rows()
        self._headers = headers
        self._rows = rows
        yield SummaryTableGrid(headers, rows)


class SummaryMarkdown(Markdown):
    """The SUMMARY.md viewer: real Markdown (headings, paragraphs, tables, lists), styled as the prototype."""

    # Markdown declares BLOCKS without ClassVar, so ClassVar here would clash with it under mypy.
    BLOCKS: dict[str, type[MarkdownBlock]] = {  # noqa: RUF012
        **Markdown.BLOCKS,
        "h1": SummaryH1,
        "td_open": SummaryTD,
        "table_open": SummaryTable,
    }


def counts_line(results: Results) -> str:
    """``Results · verified (N), needs review (N), unsupported (N)``."""
    parts = ", ".join(f"{BUCKET_LABELS[b]} ({results.count(b)})" for b in BUCKET_DIR_NAMES)
    return f"Results · {parts}"


class ResultsScreen(Screen[None]):
    """Show the results folder ``results``: SUMMARY.md and the proxies by bucket."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("r", "review", "Review")]
    AUTO_FOCUS = ""  # "" focuses nothing, so q still quits and the screen opens as the prototype shows it

    def __init__(self, results: Path) -> None:
        super().__init__()
        self._folder = results
        self._results: Results | None = None

    @property
    def folder(self) -> Path:
        return self._folder

    @property
    def results(self) -> Results | None:
        """What was loaded, once loading is done and the folder is a2m results with a summary."""
        return self._results

    @property
    def active_bindings(self) -> dict[str, ActiveBinding]:
        """The keys in reach, global keys (Quit, Help) first so the footer lists them first (DESIGN.md)."""
        return global_keys_first(self.app, super().active_bindings)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "review":
            return self._needs_review > 0
        return True

    @property
    def _needs_review(self) -> int:
        return 0 if self._results is None else self._results.count(NEEDS_REVIEW_DIR_NAME)

    # ------------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        yield AppHeader(id="hdr")
        with Vertical(id="body"):
            yield Static(LOADING_TEXT, id="results-loading", markup=False)
            with Vertical(id="results-problem"):
                yield Static("", id="results-error", markup=False)
                yield EdgeButton("Back to setup", id="back-to-setup", variant="primary", compact=True)
            with Vertical(id="results-content"):
                yield Static("", id="results-counts", classes="section-label", markup=False)
                for bucket in BUCKET_DIR_NAMES:
                    yield Static("", id=f"bucket-{bucket}-header", classes="section-label bucket-header", markup=False)
                    with VerticalScroll(id=f"bucket-{bucket}-pane", classes="bucket-pane"):
                        yield Static("", id=f"bucket-{bucket}-list", classes="bucket-list", markup=False)
                yield Static(NOTHING_TO_REVIEW, id="nothing-to-review", markup=False)
                with Horizontal(id="results-buttons"):
                    yield EdgeButton("Review", id="review", variant="primary", compact=True, disabled=True)
                    yield EdgeButton("Back to setup", id="results-back", compact=True)
                yield Static(SUMMARY_LABEL, id="results-summary-label", classes="section-label", markup=False)
                with VerticalScroll(id="results-summary-pane"):
                    yield SummaryMarkdown(id="results-summary", parser_factory=summary_parser, open_links=False)
        yield AppFooter(id="ftr", compact=True, show_command_palette=False)

    def on_mount(self) -> None:
        self.query_one("#results-problem").display = False
        self.query_one("#results-content").display = False
        self._load(self._folder)

    # ------------------------------------------------------------------ loading

    @work(exclusive=True, group="results-load", exit_on_error=False)
    async def _load(self, folder: Path) -> None:
        """Read the folder in a thread (never on the app's event loop), then show it."""
        loaded = await asyncio.to_thread(load_results, folder)
        await self._show(loaded)

    async def _show(self, loaded: Results | ResultsProblem) -> None:
        if not self.is_attached:
            return
        self.query_one("#results-loading").display = False
        if isinstance(loaded, ResultsProblem):
            self._show_problem(loaded)
        else:
            await self._show_results(loaded)
        self.refresh_bindings()

    def _show_problem(self, problem: ResultsProblem) -> None:
        line = self.query_one("#results-error", Static)
        expected = problem.problem is Problem.NO_SUMMARY  # an expected state (e.g. after Stop), not an error
        line.update(problem.message if expected else f"✗ {problem.message}")
        line.set_class(not expected, "-error")
        line.set_class(expected, "-muted")
        self.query_one("#results-problem").display = True

    async def _show_results(self, results: Results) -> None:
        self._results = results
        self.query_one("#results-counts", Static).update(counts_line(results))
        for bucket in BUCKET_DIR_NAMES:
            proxies = results.by_bucket[bucket]
            header = self.query_one(f"#bucket-{bucket}-header", Static)
            header.update(f"{BUCKET_LABELS[bucket]} ({len(proxies)})")
            listing = self.query_one(f"#bucket-{bucket}-list", Static)
            symbol, css = BUCKET_MARKS[bucket]
            if proxies:
                listing.update("\n".join(f"{symbol} {printable(p.name)}" for p in proxies))
            else:
                listing.update(EMPTY_BUCKET)
            listing.set_class(bool(proxies), css)
            listing.set_class(not proxies, "-empty")
        nothing = self._needs_review == 0
        self.query_one("#nothing-to-review").display = nothing
        self.query_one("#results-buttons").set_class(not nothing, "gap-1")
        self.query_one("#review", Button).disabled = nothing
        self.query_one("#results-content").display = True
        await self.query_one("#results-summary", Markdown).update(results.summary_md)

    # ------------------------------------------------------------------ actions

    def action_review(self) -> None:
        """Open the walkthrough of the needs-review proxies, on the first one (see :mod:`a2m.tui.review`)."""
        if self._results is not None and self._needs_review:
            self.app.push_screen(ReviewScreen(self._results))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "review":
            self.action_review()
        elif event.button.id in ("back-to-setup", "results-back"):
            self.dismiss(None)

    def on_markdown_link_clicked(self, event: Markdown.LinkClicked) -> None:
        """Links in SUMMARY.md are text from the results folder: never followed."""
        event.stop()
