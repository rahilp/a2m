"""TUI CP3: open the TUI with `a2m tui` (checkpoint plan: tests/CP3.json in the TUI run folder).

Two kinds of public entry point are exercised here:

* The Textual app itself, driven headless through Textual's own
  ``App.run_test()`` / ``Pilot`` (see https://textual.textualize.io testing
  guide). ``a2m.tui.app`` is imported lazily *inside* each test function
  (never at module import time), so a missing module or a missing ``tui``
  extra fails that one test with a clear ``ModuleNotFoundError`` instead of
  making the whole file fail to collect.
* ``a2m.cli.main(argv)``, exactly as every other CLI test in this repo
  (tests/test_cp1_cli.py, tests/test_tui_cp1_progress.py) calls it, with
  ``sys.stdin``/``sys.stdout``/``sys.stderr`` swapped for a small fake stream
  that reports ``isatty()`` as told and never has a real file descriptor.

Textual renders to an SVG "screenshot" even in headless tests
(``App.export_screenshot``); ``_screen_rows`` turns that into one plain-text
string per terminal row (row 0 is the top row, row ``height - 1`` is the
bottom row for an 80x24 session), without assuming anything about which
widget classes the app uses for its header, footer or placeholder.

No real interactive terminal, no real Textual ``textual serve`` process and
no asyncio pytest plugin are needed: each async Textual session runs inside
a small ``asyncio.run(...)`` wrapper around a plain, synchronous test
function, and the CLI wiring tests are fully synchronous.
"""

from __future__ import annotations

import asyncio
import html
import io
import re
import sys
from collections.abc import Awaitable, Callable

import pytest

from a2m import __version__
from a2m.cli import main as a2m_main

_TEXT_RE = re.compile(
    r'<text[^>]*\sx="([\d.]+)"[^>]*clip-path="url\(#[\w-]+-line-(\d+)\)"[^>]*>(.*?)</text>',
    re.DOTALL,
)


def _screen_rows(app: object) -> dict[int, str]:
    """Plain text per terminal row of ``app``'s current screenshot (no widget-class assumptions)."""
    svg = app.export_screenshot(simplify=True)  # type: ignore[attr-defined]
    cells: dict[int, list[tuple[float, str]]] = {}
    for x, line_no, text in _TEXT_RE.findall(svg):
        clean = html.unescape(text).replace("\xa0", " ")
        cells.setdefault(int(line_no), []).append((float(x), clean))
    return {line_no: "".join(text for _x, text in sorted(entries)) for line_no, entries in cells.items()}


def _run(coro_factory: Callable[[], Awaitable[None]]) -> None:
    """Run one async Textual test body to completion (no pytest-asyncio plugin installed)."""
    asyncio.run(coro_factory())


# ---------------------------------------------------------------- TUI-CP3-T01


def test_TUI_CP3_T01_app_frame_mounts_with_header_footer_and_placeholder_and_q_or_ctrl_q_quits() -> None:
    """[TUI-CP3-T01] The app frame mounts with a header, footer and a non-empty placeholder; pressing
    either 'q' or 'ctrl+q' exits the app cleanly, with no exception propagated."""

    async def check_one(key: str) -> None:
        from a2m.tui.app import A2MApp  # lazy import: a missing module fails only this test

        app = A2MApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            rows = _screen_rows(app)
            height = app.size.height
            header_text = rows.get(0, "")
            footer_text = rows.get(height - 1, "")
            main_rows = [rows.get(y, "") for y in range(1, height - 1)]

            assert "a2m" in header_text, (key, header_text)
            assert __version__ in header_text, (key, header_text)
            assert any(word in footer_text.lower() for word in ("quit", "q")), (key, footer_text)
            assert any(line.strip() for line in main_rows), (key, main_rows)

            await pilot.press(key)
            await pilot.pause()
            assert app.is_running is False, (key, "app did not quit")

    _run(lambda: check_one("q"))
    _run(lambda: check_one("ctrl+q"))


# ---------------------------------------------------------------- CLI wiring: shared fixtures/helpers


class _Stream:
    """A fake text stream standing in for sys.stdin/stdout/stderr: records writes, reports ``isatty`` as
    told, and never has a real file descriptor (matches tests/test_tui_cp1_progress.py's ``_Stream``)."""

    def __init__(self, isatty: bool) -> None:
        self.encoding = "utf-8"
        self._isatty = isatty
        self._chunks: list[str] = []

    def write(self, text: str) -> int:
        self._chunks.append(text)
        return len(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return self._isatty

    def fileno(self) -> int:
        raise io.UnsupportedOperation("fileno")

    @property
    def text(self) -> str:
        return "".join(self._chunks)


def run_main(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    *,
    stdin_isatty: bool = False,
    stdout_isatty: bool = False,
    stderr_isatty: bool = False,
) -> tuple[int, str, str]:
    in_stream = _Stream(stdin_isatty)
    out_stream = _Stream(stdout_isatty)
    err_stream = _Stream(stderr_isatty)
    monkeypatch.setattr(sys, "stdin", in_stream)
    monkeypatch.setattr(sys, "stdout", out_stream)
    monkeypatch.setattr(sys, "stderr", err_stream)
    try:
        code = a2m_main(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 2
    return code, out_stream.text, err_stream.text


def _simulate_missing_tui_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `import textual` (and therefore `import a2m.tui`) raise ImportError, simulating a2m
    installed without the `tui` extra, regardless of whether textual is actually installed here."""
    for name in list(sys.modules):
        if name in ("textual", "a2m.tui") or name.startswith(("textual.", "a2m.tui.")):
            monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "textual", None)


# ---------------------------------------------------------------- TUI-CP3-T02


def test_TUI_CP3_T02_tui_subcommand_and_interactive_bare_a2m_open_the_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """[TUI-CP3-T02] `a2m tui` and bare `a2m` on an interactive terminal both open the app and exit 0,
    invoking the stubbed run_app exactly once each, with no usage-error line printed."""
    import a2m.tui.app as tui_app  # lazy import: a missing module fails only this test

    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_run_app(*args: object, **kwargs: object) -> int:
        calls.append((args, kwargs))
        return 0

    monkeypatch.setattr(tui_app, "run_app", fake_run_app)

    code_sub, out_sub, err_sub = run_main(monkeypatch, ["tui"])
    assert code_sub == 0, (out_sub, err_sub)
    assert len(calls) == 1, calls
    assert "usage error" not in err_sub.lower(), err_sub

    code_bare, out_bare, err_bare = run_main(monkeypatch, [], stdin_isatty=True, stdout_isatty=True)
    assert code_bare == 0, (out_bare, err_bare)
    assert len(calls) == 2, calls
    assert "usage error" not in err_bare.lower(), err_bare


# ---------------------------------------------------------------- TUI-CP3-T03


def test_TUI_CP3_T03_missing_extra_prints_one_line_and_exits_2_without_opening_the_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[TUI-CP3-T03] Without the tui extra, both `a2m tui` and an interactive bare `a2m` exit 2: `tui`
    writes exactly one stderr line naming pip install a2m[tui]; bare `a2m` keeps its usual no-subcommand
    usage error but with a hint mentioning `a2m tui`."""
    _simulate_missing_tui_extra(monkeypatch)
    code_sub, out_sub, err_sub = run_main(monkeypatch, ["tui"])
    assert code_sub == 2, (out_sub, err_sub)
    sub_lines = [line for line in err_sub.splitlines() if line.strip()]
    assert len(sub_lines) == 1, sub_lines
    assert "pip install" in sub_lines[0], sub_lines
    assert "a2m[tui]" in sub_lines[0], sub_lines

    _simulate_missing_tui_extra(monkeypatch)
    code_bare, out_bare, err_bare = run_main(monkeypatch, [], stdin_isatty=True, stdout_isatty=True)
    assert code_bare == 2, (out_bare, err_bare)
    bare_lines = [line for line in err_bare.splitlines() if line.strip()]
    assert len(bare_lines) == 1, bare_lines
    assert "a2m tui" in bare_lines[0], bare_lines


# ---------------------------------------------------------------- TUI-CP3-T04


EXPECTED_BARE_PIPED_USAGE_ERROR = "a2m: usage error: the following arguments are required: COMMAND (see 'a2m --help')\n"
EXPECTED_VERSION_OUTPUT = f"a2m {__version__}\n"
EXPECTED_MIGRATE_NO_OUT_USAGE_ERROR = (
    "a2m migrate: usage error: the following arguments are required: EXPORTS, --out (see 'a2m migrate --help')\n"
)


def test_TUI_CP3_T04_piped_a2m_and_a2m_with_arguments_are_unaffected(monkeypatch: pytest.MonkeyPatch) -> None:
    """[TUI-CP3-T04] Regression guard: a piped bare `a2m`, `a2m --version` and `a2m migrate` (no --out) on
    an interactive terminal all behave exactly as they did before this checkpoint; none opens the TUI."""
    # (a) piped bare `a2m` (e.g. `a2m | cat`): the usual required-subcommand usage error, unchanged.
    code_a, out_a, err_a = run_main(monkeypatch, [], stdin_isatty=False, stdout_isatty=False)
    assert code_a == 2, (out_a, err_a)
    assert out_a == ""
    assert err_a == EXPECTED_BARE_PIPED_USAGE_ERROR

    # (b) `a2m --version` on an interactive terminal: prints the version exactly as before, exits 0.
    code_b, out_b, err_b = run_main(monkeypatch, ["--version"], stdin_isatty=True, stdout_isatty=True)
    assert code_b == 0, (out_b, err_b)
    assert out_b == EXPECTED_VERSION_OUTPUT
    assert err_b == ""

    # (c) `a2m migrate` with no --out, on an interactive terminal: the usual usage error, unchanged.
    code_c, out_c, err_c = run_main(monkeypatch, ["migrate"], stdin_isatty=True, stdout_isatty=True)
    assert code_c == 2, (out_c, err_c)
    assert out_c == ""
    assert err_c == EXPECTED_MIGRATE_NO_OUT_USAGE_ERROR


# ---------------------------------------------------------------- TUI-CP3-X01


def test_TUI_CP3_X01_a2m_tui_without_an_interactive_terminal_refuses_promptly_with_one_line_and_exit_2() -> None:
    """[CP3-X01] `a2m tui` with stdin and stdout redirected (a script, CI, `ssh` without a pty) does not
    open the full-screen app and wait forever: it prints one stderr line naming the need for an interactive
    terminal and exits 2. Runs the real command in a child process, bounded by a timeout."""
    import os
    import subprocess

    pytest.importorskip("textual")
    env = {k: v for k, v in os.environ.items() if k not in ("FORCE_COLOR", "PY_COLORS")}
    result = subprocess.run(
        [sys.executable, "-m", "a2m", "tui"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        check=False,
    )
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert result.stdout == ""
    err_lines = [line for line in result.stderr.splitlines() if line.strip()]
    assert len(err_lines) == 1, err_lines
    assert "interactive terminal" in err_lines[0], err_lines
