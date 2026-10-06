# DESIGN.md — a2m TUI
> Source of truth for the a2m terminal UI (Textual, Python). Values are exact. When code and this file disagree, this file wins unless listed under Known inconsistencies. Mode: **Create** (greenfield; no UI code exists yet, built from the TUI brief and `.ratchet/runs/a2m-tui-a-terminal-user-interface-so-users-pick/checkpoints.json`).

Everything here is a terminal token: colors are hex (rendered by Textual's `Theme` and by the HTML prototype), sizes and spacing are in **cells** (1 cell = 1 character column or 1 text row, fixed-width, fixed-height — there is no sub-cell unit), borders are Textual border-style names, and type has no size axis (the user's terminal font sets cell size; we only control weight, color and dim/italic).

## 1. Principles

1. Calm and dense: a screen never scrolls at 80x24 for its primary task; secondary detail (diffs, long logs) scrolls inside its own pane, not the whole screen.
2. Every screen is usable by keyboard alone; every interactive widget has a visible focus state and a Footer key hint.
3. Color never carries meaning alone: the three result buckets, and every status (success/warning/error), pair a color with a symbol and a text label, so the app reads correctly in a no-color or light terminal.
4. The Start button (or the next step) is enabled only when the current screen's state is genuinely ready; a disabled control always has a visible reason nearby, not just a dimmed look.
5. Nothing destructive (Stop, overwrite, force re-run) happens without an explicit confirm step naming the consequence.

## 2. Tokens

### Color

Two Textual themes, `a2m-dark` (default) and `a2m-light`, switched by the user's terminal background (Textual's `App.dark` toggle) or by the user via Textual's built-in command palette (`ctrl+p` -> "change theme"). There is no `--theme` CLI flag (resolved, see Open decisions): dark is always the default, light is opt-in through the palette. Every pair below passes WCAG AA (4.5:1) for its stated foreground/background use; ratios were computed and checked, not guessed.

| Token | Dark hex | Light hex | Usage |
|---|---|---|---|
| `background` | `#0C0E12` | `#F5F6F8` | App canvas, Header/Footer background |
| `surface` | `#12151B` | `#FFFFFF` | Screen body, default widget background |
| `panel` | `#1A1E26` | `#ECEEF2` | Raised containers: cards, list rows, Collapsible body, DataTable header row |
| `overlay` | `#20242E` | `#E3E6EC` | Modal dialogs, toast background (one step above panel) |
| `foreground` | `#E6E9EF` | `#1B1F27` | Primary text (15:1+ on background/surface/panel) |
| `foreground-muted` | `#8B93A7` | `#5B6472` | Secondary text, hints, timestamps, placeholder text (5.5:1+ on background) |
| `border` | `#2A2F3A` | `#D3D7DE` | Default (non-focused) widget border |
| `primary` | `#2F6FE4` | `#2563EB` | Primary action (Start, primary Button fill), links, selected tab |
| `accent` | `#1A8068` | `#0C7E69` | Focus ring, active tab underline, progress-bar fill |
| `success` | `#257337` | `#1A7F37` | Success text/badges; also the `verified` bucket color |
| `warning` | `#E3B341` | `#9A6700` | Warning text/badges; also the `needs-review` bucket color |
| `error` | `#C73E39` | `#CF222E` | Error text/badges; also the `unsupported` bucket color |

All five role colors (`primary`, `accent`, `success`, `warning`, `error`) are used as **solid backgrounds with white foreground text** on badges and filled buttons except `warning`, which uses `foreground: background` (dark text on the amber fill) because white-on-amber fails contrast. Measured contrast for white text: primary 4.65:1 (dark) / 5.17:1 (light), accent 4.85:1 / 5.00:1, success 5.86:1 / 5.08:1, error 5.03:1 / 5.36:1. Warning with dark text: 9.93:1 (dark theme) / 4.87:1 (light theme, white text, since amber is dark enough there — see Known inconsistencies).

**Bucket colors (never color-only):**

| Bucket | Color token | Symbol | Label text |
|---|---|---|---|
| Verified | `success` | `✓` | `verified` |
| Needs review | `warning` | `!` | `needs review` |
| Unsupported | `error` | `✗` | `unsupported` |

Symbol + label + color always appear together (e.g. `✓ verified (4)`); never the symbol or color alone.

### Typography

The terminal sets the font and its pixel size; a2m only controls **style** (bold/dim/italic) and **color token**, and every glyph occupies exactly 1 cell wide (2 for wide/CJK, not used here) by 1 cell tall — there is no line-height or letter-spacing axis.

| Role | Style | Color token | Used for |
|---|---|---|---|
| Screen title | bold | `foreground` | Header title ("a2m"), screen headers ("Setup", "Run", "Results", "Review") |
| Section label | bold | `foreground` | Section labels only: "AI", "Command", Collapsible headers ("Advanced options"), table column headers, prompt labels |
| Field label | regular | `foreground` | A single field's own label, e.g. "Exports folder", "Results folder" — regular weight, not bold, so section labels keep the only bold emphasis at this level |
| Body | regular | `foreground` | Default widget text, list rows, Markdown body |
| Muted / hint | regular, dim | `foreground-muted` | Footer key hints, placeholder text, timestamps, file sizes |
| Mono / path / code | regular | `foreground` on `panel` | File paths, the command preview, diff and log content (always literal monospace, Rich markup disabled) |
| Emphasis | italic | `foreground-muted` | Secondary explanatory lines under a control (e.g. "0 proxies found") |
| Status | bold | bucket/role color | Badges, inline success/warning/error lines (always paired with symbol + label, see Color) |

No text is ALL CAPS (reads as shouting in a dense tool); sentence case throughout, matching Content rules.

### Spacing (cells)

Scale: `0, 1, 2, 3` cells. There is no finer unit; a 1-cell gap is already the minimum visible gap in a terminal grid.

| Token | Cells | Usage |
|---|---|---|
| `gap-0` | 0 | Inside a dense list/table; no blank row between rows |
| `gap-1` | 1 | Default gap between a label and its control, between buttons in a row, page gutter (left/right margin of screen content) |
| `gap-2` | 2 | Between unrelated sections on the same screen (e.g. folder pickers vs. AI choice) |
| `gap-3` | 3 | Above/below a screen's primary action row (Start, Stop) to set it apart |

Page gutter: 1 cell left/right, 0 top (Header fills row 0), 0 bottom (Footer fills the last row). At 80x24 this leaves a 78x22 content area; screens must fit a single primary task in that space without scrolling.

### Radius / Border / Elevation

Terminal "radius" is the border style's corner glyphs, not a pixel radius. "Elevation" is expressed as a background step (`background` → `surface` → `panel` → `overlay`), not a shadow.

| Level | Background token | Border style (Textual name) | Border color token | Used for |
|---|---|---|---|---|
| 0 (base) | `background` | `none` | — | App canvas behind Header/Footer |
| 1 (surface) | `surface` | `none` | — | Screen body |
| 2 (panel) | `panel` | `round` | `border` (default) / `accent` (focused) | Cards, DataTable, ListView, Collapsible body |

Input and any other validated field box live at level 1 (`surface` fill, one uniform tone, not `panel`) with a `round` border in Default/Focus. Valid/Invalid switch the border style to `heavy` (thick lines, in `success`/`error` color) instead of changing fill, since a 1-cell-thin line is hard to see and the field's pass/fail state must be obvious at a glance. See Components → Input.
| 3 (overlay) | `overlay` | `heavy` | `warning` for confirm, `error` for destructive-confirm, `accent` otherwise | Modal dialogs |
| toast | `overlay` | `round` | matches the notify severity (`accent` info, `warning` warning, `error` error) | Toast/notify popups |
| inline (no box) | `panel` | `none` | — | Command preview line only: a single row, no border glyphs at all (not even on the sides), truncated with an ellipsis when wider than the terminal (see Components → Command preview, Patterns) |

Buttons are **compact**: 1 cell tall, with only the thick-vertical edge glyph `┃` drawn on the left and right of the label (no top/bottom border row, never a 3-row box). This is Textual's `Button(compact=True)` (or equivalent CSS: `height: 1; border: none`, with the `┃` glyphs emitted as literal content), matching the prototype's single-line button rendering. The `┃` color swapping is the focus indicator (see Components → Button).

### Motion

Terminal UIs should not move more than necessary; the a2m TUI keeps motion to exactly three places:

| Element | Duration | Behavior |
|---|---|---|
| ProgressBar fill | continuous, no fixed duration | Fills to the reported percentage; no easing animation beyond the bar's own redraw |
| Indeterminate step spinner | 80ms per frame | Used only for a running step with no known percentage (e.g. "building") |
| Toast enter/exit | 150ms slide | Toast appears bottom-right, auto-dismiss per severity (see Patterns → Toasts) |

No screen transition animates (switching Setup → Run → Results is instant). Reduced motion follows `textual.constants.ANIMATIONS` (the `TEXTUAL_ANIMATIONS` environment variable): the spinner and toast motion are both turned off when it reports `NONE` or `BASIC` (resolved, see Open decisions).

## 3. Layout

- **Minimum supported size: 80 columns x 24 rows.** Every `ui_states` screen in checkpoints.json must render without clipping **and without the screen itself scrolling** at exactly 80x24; larger terminals get more breathing room (wider folder-tree pane, more visible list rows) but no new controls. Long content that cannot be summarized to fit — SUMMARY.md, proxy/bucket lists, REPORT.md, diffs — scrolls inside its own fixed-height pane instead of the whole screen scrolling; the pane's height is fixed so the rest of the screen (Header, Footer, primary action) never moves off-screen.
- **Header**: row 0, full width, `background` fill, bold title "a2m" left-aligned, version string right-aligned (e.g. "a2m  ·  v0.1.0"), no clock (keeps it calm).
- **Footer**: last row, full width, `background` fill, Textual's standard key-hint strip: each binding shown as `key` (bold, `accent`) + a space + its label (regular, `foreground-muted`), separated by 2 cells. Global bindings always present: `q` Quit (or `ctrl+q`), `?` Help. Screen-specific bindings are appended (see Components → Footer).
- **Body grid**: rows 1 through (height-2). **Single column at every width, on every screen** (Setup, Run, Results, Review) — the earlier two-pane layout at ≥100 columns is removed; a wider terminal gives the one column more room and taller inner scroll panes, never a second column. This keeps the layout identical at 80 and 100+ columns, so a reviewer checking either width sees the same structure.
- **Screen anatomy, Setup** (CP3-CP5): Header / `Exports folder` field (Input + `Browse…` button, `ctrl+o` opens the same browser from the field) + dim resolved-path line (shown only when the typed text differs from its resolved absolute folder) + found-proxies line / `Results folder` field (Input + `Browse…` button, `ctrl+o`) + dim resolved-path line + validation line / AI choice RadioSet / Advanced Collapsible (closed by default; open, its short fields pair two per row so the open section still fits 24 rows — see Components → Collapsible) / command preview (single borderless line, `panel` background, truncated with an ellipsis if too wide) / Start button row / Footer. Typing, Tab path completion, and choosing a folder in the browser all write the same field value, so the resolved-path line, the validation line and the command preview update identically regardless of input method.
- **Screen anatomy, Run** (CP6): Header / overall ProgressBar + "N of M proxies" + elapsed time / current proxy name + current step / finished-proxies ListView (bucket symbol + name only, newest at top — no inline reason, see Components → DataTable/ListView) in its own fixed-height scrollable pane / Stop button / Footer.
- **Screen anatomy, Results** (CP7): Header / single column: bucket counts + three collapsible lists (verified/needs-review/unsupported, name + bucket marker only) in a fixed-height pane, then rendered SUMMARY.md in its own fixed-height scrollable Markdown pane below / "Review" button (enabled only if needs-review is non-empty) / Footer.
- **Screen anatomy, Review** (CP8): Header / "proxy K of N" position line / tab bar (Report, Diffs) / content pane (Markdown for REPORT.md, or file list + diff text for Diffs) in its own fixed-height scrollable pane / folder path line with copy hint / Previous·Next·Back-to-summary button row / Footer.

## 4. Components

Every component lists **every** state it can be in; a state missing here is a spec gap, not an implementation choice.

### Button
Compact: 1 cell tall, no top/bottom border row. Anatomy is `┃` + 1-cell padding + label + 1-cell padding + `┃`, the same thick-vertical edge characters the prototype draws (`a2m/tui`'s Textual implementation uses `Button(compact=True)`, or equivalent CSS `height: 1; border: none` with the `┃` glyphs as literal content). Every state below is expressed purely through color and text style, never through height or border shape, since there is only one row to work with.
- **Default**: `┃` glyphs in `border` color, `foreground` label text, `surface` background.
- **Focus**: `┃` glyphs → `accent` color; this is the *only* focus cue needed (no underline, no background change) so it reads in no-color terminals too via position (Tab order).
- **Primary** (Start, Stop-confirm's destructive action): label background fills solid with `primary` (or `error` for a destructive confirm action), white label text; `┃` glyphs in the same role color.
- **Disabled**: `┃` glyphs in `border` color, `foreground-muted` label text, `background`-tinted fill (no hover/focus possible); a disabled primary action always has a one-line reason directly below it in `foreground-muted` italic (e.g. "Set ANTHROPIC_API_KEY to use Claude").
- **Hover** (mouse-enabled terminals only, genuinely optional — resolved, see Open decisions): `┃` glyphs → `accent`, label text style stays regular (not bold) so it is visually lighter than true focus; never relied upon since the app must work keyboard-only, and may be deferred past CP3 if time is short.

### Input
Applies to Input and any other validated field box (e.g. the folder-path fields). Inner fill is always `surface` — one uniform tone — never `panel`, at every state below.
- **Default**: `round` border, `border` color, `surface` background, `foreground` text, placeholder text in `foreground-muted` italic.
- **Focus**: `round` border, color → `accent`.
- **Valid** (after a check ran, e.g. folder exists and is usable): border style switches to `heavy` (thick lines) in `success` color — a thin 1-cell line is easy to miss, so the pass state must read as a visibly heavier box, not just a color change; a one-line confirmation appears below in `success` text with no symbol needed (the border already signals it, but a trailing `✓` is added when the result matters, e.g. "✓ 14 proxies, 2 shared flows found").
- **Invalid**: border style switches to `heavy` in `error` color, same reasoning as Valid; one line below in `error` text, prefixed `✗`, naming the exact problem using a2m's own CLI wording verbatim (may start lowercase, may include full paths — the TUI never shortens or rewrites it, e.g. "✗ results folder is inside /home/user/exports"); the screen's Start/primary action stays disabled while any Input is invalid.
- **Disabled**: `round` border, `foreground-muted` text and border, not focusable.

### Folder field (Input + Browse button)
Each folder field (Exports folder, Results folder) pairs a `FolderInput` (an `Input` subclass, `a2m/tui/setup.py`) with a compact `Browse…` button on the same row, 2 cells short of the right gutter (`.field-row` in `app.tcss`). The Input and the button open the identical folder browser dialog below.
- **Typed value**: standard Input states apply, see Components → Input (Default/Focus/Valid/Invalid/Disabled).
- **Tab path completion**: typing a partial name and pressing Tab (or Right at end of line) accepts a live suggestion for the first sub-folder, case-sensitively matched, whose name starts with the last path segment typed (`a2m/tui/picker.py: FolderSuggester`/`complete_folder`); `~` alone completes to `~/`; a blank field or one already ending in a path separator suggests nothing, so Tab moves focus on as usual; a hidden sub-folder is only suggested when the typed segment itself starts with a dot.
- **Browse… button**: compact `EdgeButton` labelled "Browse…" (see Components → Button), opens the folder browser dialog at this field's current value.
- **ctrl+o**: bound on the Input itself, opens the identical dialog without a mouse; `ctrl+q` stays bound on the field too (a bare `q` types a literal q while a field has focus).
- **Resolved-path line**: a dim line directly under the field (`.resolved` class: `foreground-muted`, dim text-style, not italic), reading `→ /absolute/path`. Shown only when the field's typed text differs from its own resolved absolute folder (e.g. a relative path, or one using `~`); hidden (zero height) when the typed text already equals its resolved folder, including when the field is empty. Sits above the field's validation hint line (Components → Input: Valid/Invalid).

### Folder browser dialog (FolderPicker)
A modal screen (`a2m/tui/picker.py`) opened by a field's Browse… button or `ctrl+o`; browses one folder at a time and dismisses with the chosen folder, filling the field exactly as typing that path would, or with nothing on cancel. Chrome: `overlay` background, `heavy` `accent` border, fixed 76x21 cells, centered over a 60%-dimmed background; fits above its own Footer at 80x24 (see Layout, 80x24 no-scroll rule).
Anatomy, top to bottom: title line (e.g. "Choose the exports folder") / current folder's full path (wraps rather than truncating) / the folder's sub-folder list / a live preview line for the highlighted row / a button row ("Use this folder", "Cancel").
- **Opens at**: the field's current folder if it exists, else the nearest existing parent folder above it, else the user's home folder; this is the dialog's only entry state.
- **Current folder path**: shown in full at the top with `~` for the home folder, never truncated, wraps to a second line if needed.
- **Sub-folder list**: only that folder's own sub-folders (not files, not recursive), sorted case-insensitively by name; row 0 is always `. (this folder)`, standing for the folder shown at the top itself, so it can be chosen without descending; a readable sub-folder shows `▸ name`.
- **Unreadable sub-folder row**: `✗ name (cannot read)`, still listed rather than hidden; selecting it does nothing and its preview reports "cannot read `<path>`: permission denied".
- **Empty folder**: a disabled "No subfolders" row, not an error state (an empty folder is not a failure).
- **Unreadable current folder** (listing the folder itself errors, e.g. permission denied): a disabled row "✗ cannot read this folder: `<reason>`" in place of the list.
- **Truncated listing**: past 2000 sub-folders, a disabled row "Showing the first 2000 folders" is appended, so the cap is visible, never silent.
- **Navigation**: Enter opens the highlighted sub-folder (or, on row 0, immediately uses the current folder, same as "Use this folder"); Backspace goes up one folder, highlighting the folder just left; `~` goes straight to the home folder; typing printable characters is type-ahead that jumps the highlight to the first row whose name starts with what was typed within the last second (letters never reach the app's own quit binding here, so the Footer shows `ctrl+q` for Quit instead of `q`).
- **Live preview line**: follows the highlighted row, run through a2m's own read-only checks (the same checks `a2m migrate` uses: proxy/shared-flow counts for an exports folder, writability for a results folder) in a background worker, debounced 150ms after the highlight stops moving so holding an arrow key does not check every row passed over.
- **Loading** (check in flight or not yet started after a highlight move): "Checking…" in `foreground-muted`, no symbol, no italic.
- **Valid**: `success` color, no italic, a2m's own check wording verbatim (e.g. "14 proxies, 2 shared flows found").
- **Invalid**: `error` color, no italic, a2m's own check wording verbatim, including the permission-denied wording for an unreadable row.
- **Use this folder**: `ctrl+u`, or the "Use this folder" button, dismisses with the highlighted row's folder (row 0 means the folder shown at the top), filling the field and re-running that field's validation.
- **Cancel**: `Esc`, or the "Cancel" button, dismisses with nothing chosen; the field is left exactly as it was.
- **Hostile folder names**: rendered as plain text (never markup); a control character such as a stray newline in a folder name is shown as `?`, so no row ever grows past 1 line.

### RadioSet (AI on/off choice)
Each option is a Textual `RadioButton` rendered with its compact `ToggleButton` glyph, label to the right with a 1-cell gap: `Claude (uses ANTHROPIC_API_KEY)` / `No AI`.
- **Off (default)**: `▐●▌` glyph with the inner `●` dimmed (`foreground-muted`), `foreground` label text, regular weight.
- **On (selected)**: `▐●▌` glyph with the inner `●` in `primary` (not `accent` — `accent` is reserved for the focus meaning so the two never collide on one glyph), label text bold.
- **Focused option**: the whole glyph+label row outlined in `accent` (RadioSet draws its own focus ring per item, not just the set); this combines with whichever on/off glyph already applies.
- **Disabled option**: none in this app (both options are always choosable; the *consequence* of choosing Claude without a key is a message plus a disabled Start, not a disabled radio item — see brief CP5).

### Checkbox (Advanced section: "Mock backends", "Skip Mule runtime")
Textual's compact `ToggleButton` glyph, label to the right with a 1-cell gap (e.g. `▐X▌ mock-backends`).
- **Off (default)**: `▐X▌` glyph with the inner `X` dimmed (`foreground-muted`), `foreground` label text.
- **On**: `▐X▌` glyph with the inner `X` in `primary` (the same on-color as RadioButton, so every toggle control in the app means "selected" with one consistent color), `foreground` label text.
- **Focus**: the glyph's outer `▐▌` brackets → `accent` color (the only focus cue; a Checkbox has no border to recolor).
- **Disabled**: both brackets and label in `foreground-muted`, not focusable (not currently used by any screen, listed for completeness).
- **Hover** (mouse-enabled terminals only, genuinely optional — resolved, see Open decisions): outer `▐▌` brackets → `accent`, same lighter-than-focus treatment as Button's hover.

### Collapsible (Advanced section)
- **Closed** (default): header row only, `▸ Advanced options`, `foreground-muted` chevron, `panel` background.
- **Open**: `▾ Advanced options`, body expands below showing its fields (single proxy only, Mock backends checkbox, golden recordings folder, ignored headers, AI fix attempts, Skip Mule runtime checkbox), body background `panel`, 1-cell padding. Short fields pair two per row (e.g. "Mock backends" checkbox next to "Skip Mule runtime" checkbox, "AI fix attempts" next to a short numeric/text field) so the open section still fits within 24 rows; fields that need the full row width (golden recordings folder path, ignored headers list) stay one per row.
- **Focus**: header border color → `accent` when the header itself is focused (collapsing/expanding is Enter/Space on the header).

### ProgressBar
- **Running (determinate)**: `accent` fill over `border`-colored track, percentage and "N of M proxies" text alongside, not inside, the bar (so it still reads at 80 columns with a long label).
- **Running (indeterminate, a sub-step like "building")**: same bar shown as a moving 3-cell-wide `accent` segment (the 80ms spinner), label swaps to the step name ("building", "deploying", "running tests", "AI fix 1 of 3").
- **Complete**: fill color → `success`, frozen at 100%.
- **Stopped**: fill color → `warning`, frozen at the point it stopped, label "stopped at N of M".
- **Failed to start**: bar not shown at all; replaced by the start-failed message (see Patterns → Empty/error states).

### DataTable / ListView rows (finished-proxies list, bucket lists)
- **Default row**: bucket symbol + color (per Color → Bucket colors) + proxy name only — never an inline reason, even for needs-review/unsupported; the reason lives in SUMMARY.md and REPORT.md, not the row — on `panel` background, `gap-0` between rows (dense list).
- **Cursor / focused row**: `accent` background, inverted (`background`-token) text.
- **Alternating rows**: no zebra striping (keeps it calm and avoids a 4th implied color); grouping is by bucket header instead.
- **Empty bucket**: the bucket's section shows "None" in `foreground-muted` italic instead of being hidden, so the user can confirm nothing is missing.
- **Loading** (results still loading from disk): 3 skeleton rows of `foreground-muted` dashes `— — —` on `panel`, replaced in place once loaded.

### Markdown viewer (SUMMARY.md, REPORT.md)
- **Loaded**: standard Markdown rendering (headings bold `foreground`, tables with `border`-colored rules, code spans on `panel`), scrollable within its pane only.
- **Loading** (worker reading a large file): centered "Loading…" with the spinner, pane otherwise empty.
- **Empty / missing file**: "No SUMMARY.md yet — the run may still be in progress or was stopped before it finished." in `foreground-muted`, no error color (this is an expected state, e.g. after Stop).
- **Error** (folder is not a2m results, outside the allowed read root, or unreadable): `✗` in `error` plus one plain-language line ("This folder does not look like an a2m results folder.").
- **Truncated** (file over the size cap): content shows the first N lines plus a trailing `foreground-muted` line "Showing the first 2000 lines of a larger file." — never silently cut.

### Modal confirm (Stop, quit-while-running)
- **Default**: `overlay` background, `heavy` border in `warning` (Stop) or `error` (quit-while-running, since it also implies Stop), 1 title line + 1-2 body lines naming the exact consequence ("Stop now? Finished proxies stay in the results folder; the current proxy is marked needs-review."), two buttons.
- **Focus**: the safer action (Cancel) is focused by default; Tab/Shift+Tab move between the two buttons; each follows Button's focus state.
- **Dismiss**: `escape` always triggers Cancel; `enter` triggers whichever button is focused.

### Toast / notify
- **Info**: `overlay` background, `accent` left border (1 cell), auto-dismiss after 4s.
- **Warning**: same shape, `warning` border, auto-dismiss after 6s.
- **Error**: same shape, `error` border, does not auto-dismiss; dismissed with Enter/Escape or by reading the next one.
- **Position**: bottom-right, stacked newest-on-top, max 3 visible (older ones queue).
- **Content**: always plain text, one line preferred, two lines max; the same wording a plain-CLI user would see for the same condition (per brief: TUI messages mirror the CLI's own checks).

### Header / Footer (key-binding conventions)
- Footer always lists global keys first (`q` Quit, `?` Help), then screen keys, in the order a user would use them left to right: primary action keys before secondary, destructive last.
- Per-screen keys: Setup — none extra beyond Tab/Enter (folder pickers and RadioSet use native Tab/Enter/Space); Run — `s` Stop; Run (stopped) — `r` Resume; Results — `r` Review (opens the walkthrough); Review — `n` Next, `p` Previous, `d`/`tab` switch Report/Diffs tabs, `c` Copy path.
- A key already bound globally is never reused for a different action on a screen.

## 5. Patterns

- **Forms & validation**: validation runs on blur/change, never only on submit, so a user sees the Input state before they reach Start. Every validation and error message shows a2m's own CLI wording verbatim after the `✓`/`✗` marker — it may start lowercase and may include full paths, exactly as the CLI prints it; the TUI never invents shorter or rewritten copy (brief CP4/CP5), never a generic "Invalid input".
- **Command preview**: a single borderless line (no box, unlike Input) on `panel` background showing the literal `a2m migrate ...` command for the current choices, updated live, quoted with `shlex`-equivalent quoting so it is copy-pasteable. If the full command is wider than the terminal, it is truncated with a trailing honest ellipsis (`…`, never silently cut with no marker) rather than wrapped; the full, untruncated text is still reachable through the Copy action/key even when the visible line is truncated.
- **Lists grouped by bucket**: always in the fixed order verified → needs-review → unsupported, with a visible count next to each group header, and an explicit "None" for an empty group (never a hidden/missing group).
- **Empty states**: every list/viewer that can legitimately be empty (0 proxies found, no diffs, nothing needs review) says so in one plain sentence in `foreground-muted`, no error color, no symbol (not an error).
- **Error states**: every error (bad folder, missing key, not-a2m-results, start-failed) shows a2m's own CLI wording (see Forms & validation) plus the `✗`/`error` treatment, with a way back (a button or key) to the screen that can fix it; never a raw traceback, never a TUI-invented rewrite of the CLI's message.
- **Confirmations**: Stop, quit-while-running, and force-redo-an-existing-results-folder are the only three actions that confirm; everything else (choosing a radio option, opening a Collapsible, Next/Previous in Review) is instant.
- **Loading / skeleton**: any read that touches disk (folder scan, SUMMARY.md load, diff load) goes through a worker and shows a loading state within 100ms if it isn't done yet, so the app never appears frozen.

## 6. Content

- Sentence case everywhere; no ALL CAPS, no title-case headers. Validation and error messages are the one deliberate exception to "sentence case": they show a2m's own CLI wording verbatim (see Patterns → Forms & validation), which may start lowercase and may include full paths — the TUI never shortens or recases it.
- No em dashes anywhere in app text (matches standards.md rule 35); use a comma or a plain hyphen.
- Copy is honest and matches behavior exactly (standards.md rule 36): never say "verified" for a build that only compiled, never say a key is "set" without checking, never claim a count the engine didn't report.
- Button verbs are imperative and specific: "Start", "Stop", "Resume", "Review", "Copy path" — never "OK"/"Submit"/"Go".
- Counts are always "N of M" (e.g. "5 of 12 proxies", "proxy 3 of 7"), never a bare percentage alone.
- Paths are shown in full (not truncated with `...`) when they fit; when they don't fit 1 line at the current width, wrap once, never ellipsize a path silently since the user may need to copy it exactly.
- File and folder names from user data are rendered as plain text, never interpreted as Rich/Textual markup (brief CP4/CP8 requirement) — this is a security/display rule, not a style choice.

## 7. Accessibility

- **Focus indication**: a focused control's border color becomes `accent` (never color alone — focus also moves the Tab order visibly, and the Footer hints always match the currently reachable actions). No focus state relies on a background color change alone for text-only rows (see the Folder browser dialog's list/DataTable, which invert instead).
- **Minimum hit area**: in a terminal, the unit is cells, not pixels — every interactive control is at least 1 row tall and wide enough to show its full label with 1 cell of padding each side; Button's compact 1-row height already meets this exactly (a terminal's minimum hit area is a single cell row, so a taller button buys nothing).
- **Contrast**: every foreground/background pairing in the Color table was measured at ≥4.5:1 (body text/background) — see the Color section for exact ratios; bucket symbols add a second channel so color-blind and no-color terminals are never left guessing.
- **Motion-reduce**: the only animated elements (spinner, toast slide) are disabled when Textual reports a reduced-animation preference (see Motion); nothing else moves.
- **Keyboard-only**: every `ui_states` scenario in checkpoints.json (CP3-CP8) must be fully operable without a mouse; this is a gate, not a nice-to-have (CP8's done_when explicitly re-checks all earlier screens at 80x24, keyboard-only).

## 8. Implementation map

| Token / spec | Implementation |
|---|---|
| `a2m-dark` / `a2m-light` Theme (Color table) | `a2m/tui/app.tcss` (Textual CSS variables) + a `Theme` registration in `a2m/tui/app.py` |
| App frame, Header/Footer, screen routing | `a2m/tui/app.py` |
| Setup screen (folder fields, Browse… button, resolved-path line, found-proxies, validation) | `a2m/tui/setup.py` |
| Folder browser dialog (FolderPicker), Tab path completion | `a2m/tui/picker.py` |
| Read-only folder/field checks (validation, previews) | `a2m/tui/folders.py` |
| Command preview build | `a2m/tui/command.py` |
| AI choice, Advanced section | `a2m/tui/setup.py` (same screen, CP5 extends CP4's widgets) |
| Run screen (progress, finished list, Stop/Resume) | `a2m/tui/run.py` |
| Child process + progress-event parsing | `a2m/tui/child.py`, event schema in `a2m/progress.py` |
| Results screen (SUMMARY.md, bucket lists) | `a2m/tui/results.py` |
| Read-only results access (safefs-equivalent for reads) | `a2m/tui/read.py` |
| Review walkthrough (Report/Diffs tabs, next/previous, copy path) | `a2m/tui/review.py` |
| UI-state capture harness (fixture data per `ui_states` name, must match the prototype's per-state data exactly — see section 9) | `tools/tui_states.py` |
| Package entry / lazy import boundary | `a2m/cli.py` (only the `tui` subcommand imports `a2m.tui`) |

## 9. Rendering in the HTML prototype

The HTML prototype and `textual serve` captures must be visually comparable, so the prototype renders a literal character grid, not a responsive web layout:

- **Grid unit**: 1 cell = 1 `ch` wide by 1 line tall, using a single monospace font stack (`"Cascadia Code", "DejaVu Sans Mono", monospace`) at a fixed line-height of `1` (no extra leading — matches the terminal's fixed row height).
- **Canvas size**: the prototype's root container is sized in exact cells (`width: calc(80ch); height: calc(24 * 1lh)` or an explicit `<pre>`-like grid), mirroring the 80x24 minimum from Layout; wider states (100+ columns) get a second fixed-width variant, not a fluid one.
- **Colors**: the same hex values from the Color table, applied as literal CSS colors (`background-color`, `color`) — no theming abstraction, no opacity tricks, so a human comparing a screenshot to a `textual serve` capture sees the same hex.
- **Borders**: Textual border-style names are approximated with Unicode box-drawing characters matching that style (`round` → `╭─╮│╰─╯`, `heavy` → `┏━┓┃┗━┛`, `tall` → thick left/right verticals only), drawn as text content inside the grid, not CSS `border` (so corner glyphs match exactly).
- **States**: each `ui_states` entry from checkpoints.json gets its own prototype route/query (`?state=<name>`, e.g. `setup-ready`, `run-stop-confirm`), matching `tools/tui_states.py`'s state names exactly so a reviewer can diff the same state across prototype and real app.
- **Sample data**: each `ui_states` entry gets its own fixture matching that state's exact description from checkpoints.json (e.g. `review-report` is "proxy 3 of 7" — the fixture's REVIEW_SET must put the reviewer on index 2 of a 7-item set, not just any proxy). The dev capture harness (`tools/tui_states.py`) is the real app's equivalent of the prototype's `STATES` registry: it must load exactly the same per-state data (names, counts, positions, reasons) that the prototype uses for that state name, so a reviewer diffing a prototype screenshot against a real-app capture is comparing the same content, not just the same layout.

## 10. Known inconsistencies

- The original DirectoryTree-based folder-picker spec (section 4, written 2026-10-05 as a greenfield guess) is superseded: `a2m/tui/picker.py` builds a custom `ModalScreen` + `OptionList` dialog, not Textual's `DirectoryTree` widget, matching the engine's folder-at-a-time browsing model (full current-folder path, a `. (this folder)` row, a live read-only preview) rather than a multi-level expandable tree. See Components → Folder browser dialog (FolderPicker).
- One deliberate asymmetry: `warning`'s safe text color differs by theme (dark foreground in both themes, since white-on-amber fails contrast in the dark theme's brighter amber and is unnecessary in the light theme too) — this is intentional, not a bug, and is documented in the Color section rather than listed here as a defect.

## 11. Open decisions (resolved 2026-10-05, orchestrator)

1. Reduced-motion API: resolved as `textual.constants.ANIMATIONS` (reads the `TEXTUAL_ANIMATIONS` environment variable); see Motion. No longer open.
2. Mouse hover (Button, Checkbox, RadioButton): resolved as optional. It is specified above for completeness, but the brief's keyboard-only parity is the requirement; implement hover only if CP3 time allows, never block a checkpoint on it.
3. `--theme` flag: resolved as "no flag". Dark is always the default theme; light is reachable only through Textual's built-in command palette (`App.dark` toggle), not a CLI/TUI flag. See Color section intro.

## Changelog

- 2026-10-05: initial version, created from the TUI brief and checkpoints.json (CP3-CP8 ui_states) by ratchet-design-md. No prior DESIGN.md existed.
- 2026-10-05: orchestrator decisions. Button is now compact (1 cell tall, `┃` edge glyphs, no 3-row box) in Components, Border/Elevation table and Accessibility. Command preview is a single borderless line on `panel`, truncated with an ellipsis, full text via copy (Border/Elevation table, Patterns, Layout). Added a Checkbox component spec (compact `ToggleButton` glyph `▐X▌` on/off, inner `X` dimmed when off) and updated RadioSet to the same compact glyph style (`▐●▌` on/off, inner `●` dimmed when off, on-color `primary`, kept distinct from the `accent` focus color). Noted in section 9 that each `ui_states` fixture (`tools/tui_states.py`) must match the prototype's own per-state data exactly. Resolved all three Open decisions: no `--theme` flag (command palette instead), reduced motion via `textual.constants.ANIMATIONS`, hover optional everywhere.
- 2026-10-06: Rahil-approved changes, matching what CP4-CP6 already built. Input (and any validated field box): Valid/Invalid now draw a `heavy` (thick) border in success/error color instead of just recoloring the thin `round` border, since a 1-cell line is hard to see and field status must be obvious; Default/Focus keep `round`. Field labels ("Exports folder", "Results folder", other single-field labels) are regular weight; only section labels (AI, Command, Collapsible headers, prompts) stay bold — added a Field label row to Typography and corrected Input's inner fill to `surface` (one uniform tone) everywhere, not `panel`. Layout: every screen fits 80x24 without the screen itself scrolling; long content (SUMMARY.md, proxy/bucket lists, REPORT.md, diffs) scrolls inside its own fixed-height pane; removed the two-pane layout at >=100 columns in favor of a single column at every width (Layout, Screen anatomy for Setup/Run/Results/Review). Bucket list rows show name + bucket marker/label only, no inline one-line reason (the reason is in SUMMARY.md/REPORT.md) — updated DataTable/ListView rows and Results screen anatomy. Advanced options: short fields now pair two per row so the open Collapsible section fits 24 rows. Validation and error messages show a2m's own CLI wording verbatim after the ✓/✗ marker (may start lowercase, may include full paths); the TUI never invents shorter copy — updated Patterns (Forms & validation, Error states) and Content's sentence-case rule.
- 2026-10-06: documented the folder picker built for CP4, replacing the earlier speculative DirectoryTree-based spec with what `a2m/tui/picker.py`, `setup.py` and `app.tcss` actually built. Components: new "Folder field (Input + Browse button)" (compact `Browse…` button beside each field, `ctrl+o` on the field itself, Tab path completion, the dim `→ /absolute/path` resolved-path line shown only when it differs from what was typed) and "Folder browser dialog (FolderPicker)" (opens at the field's folder, nearest existing parent, or home; full current-folder path with `~`; sub-folders only, with a `. (this folder)` row; Enter opens, Backspace goes up, `~` goes home, typing jumps by name; a live read-only preview line with its own Loading/Valid/Invalid states; `ctrl+u`/"Use this folder" fills the field; `Esc` cancels; an unreadable row shows `✗ name (cannot read)`; empty-folder and truncated-listing states), replacing the old DirectoryTree entry. Updated the Setup screen anatomy (Layout) and the Implementation map (`a2m/tui/picker.py`, `a2m/tui/folders.py`) to match; noted the DirectoryTree-to-FolderPicker change under Known inconsistencies. The dialog's fixed 76x21 size still fits the 80x24 no-scroll rule.
