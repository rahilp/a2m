"""TUI CP5: choose AI on or off, plus options (checkpoint plan: tests/CP5.json in the TUI run folder).

Drives the real Setup screen headlessly through Textual's ``App.run_test()``/``Pilot``, the same technique
as tests/tui/test_cp4_setup_folders.py and tests/tui/test_cp4_folder_browser.py: ``a2m.tui.app`` is imported
lazily *inside* each test function, never at module import time, so a missing module or attribute fails
only that one test, never a crash that stops the file from collecting.

CP3/CP4 already fixed the ids ``input-exports``, ``input-results``, ``browse-exports``, ``browse-results``,
``radio-ai``/``radio-ai-claude``/``radio-ai-none`` and ``collapsible-advanced`` (``a2m/tui/setup.py``, drawn
but disabled before this checkpoint). CP5 is the first checkpoint to give the Advanced section's own fields
a shape, so this file fixes their ids the same way CP4 fixed the folder fields' ids: ``input-only``,
``checkbox-mock-backends``, ``input-golden``, ``input-golden-ignore-header``, ``input-max-fix-attempts``,
``checkbox-no-runtime``.

A results folder is never given an earlier run here (CP4 already covers Resume/Force), so once the exports
and results fields hold valid folders and an AI choice is made, the screen is in CP4/CP5's "setup-ready"
state (checkpoints.json's ``ui_states``) and Start is enabled.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest
from conftest import write_bundle_dir

from tui.screen import _copied_command_tokens, _normalize_ws, _run, _screen_text, _set_input, _settle

SENTINEL_KEY = "sk-ant-test-CP5-SENTINEL-DO-NOT-LEAK"


def _exports_3_proxies(tmp_path: Path) -> Path:
    exports = tmp_path / "exports"
    exports.mkdir()
    write_bundle_dir(exports, "alpha")
    write_bundle_dir(exports, "beta")
    write_bundle_dir(exports, "gamma")
    return exports


def _fake_anthropic_module() -> types.ModuleType:
    """A bare stand-in for the ``anthropic`` package: enough for an import to succeed, nothing is ever called."""
    return types.ModuleType("anthropic")


async def _valid_folders(pilot: object, exports: Path, results: Path) -> None:
    """Fill in a valid exports and a fresh results folder (no earlier run: CP4 already covers Resume/Force)."""
    await _set_input(pilot, "#input-exports", str(exports))
    await _set_input(pilot, "#input-results", str(results))


async def _open_advanced(pilot: object) -> object:
    """Click the "Advanced options" header to open it, and return the Collapsible widget."""
    from textual.widgets import Collapsible
    from textual.widgets._collapsible import CollapsibleTitle

    app = pilot.app  # type: ignore[attr-defined]
    collapsible = app.screen.query_one("#collapsible-advanced", Collapsible)
    title = collapsible.query_one(CollapsibleTitle)
    await pilot.click(title)  # type: ignore[attr-defined]
    await _settle(pilot)
    return collapsible


# ---------------------------------------------------------------- TUI-CP5-T02


def test_TUI_CP5_T02_ai_choice_gates_start_on_the_key_and_never_shows_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[TUI-CP5-T02] The AI choice offers exactly 'Claude (uses ANTHROPIC_API_KEY)' and 'No AI'; with the key
    present, choosing Claude keeps Start enabled and the preview/copied command show --llm claude; with the
    key unset, choosing Claude disables Start and shows a message naming ANTHROPIC_API_KEY that also points
    at the No AI choice (or --llm none); choosing No AI always keeps Start enabled and shows --llm none; the
    sentinel key value never appears anywhere on screen, in the preview or in the copied command."""
    monkeypatch.setitem(sys.modules, "anthropic", _fake_anthropic_module())
    exports = _exports_3_proxies(tmp_path)

    async def body() -> None:
        from textual.widgets import Button, RadioButton, RadioSet

        from a2m.tui.app import A2MApp

        app = A2MApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await _valid_folders(pilot, exports, tmp_path / "results")
            start = app.screen.query_one("#start", Button)

            radio = app.screen.query_one("#radio-ai", RadioSet)
            buttons = list(radio.query(RadioButton))
            labels = sorted(str(b.label) for b in buttons)
            assert labels == sorted(["Claude (uses ANTHROPIC_API_KEY)", "No AI"]), labels
            assert radio.disabled is False, "the AI choice must be choosable once the folders are valid"

            # --- key present: Claude keeps Start enabled and is shown in the command.
            monkeypatch.setenv("ANTHROPIC_API_KEY", SENTINEL_KEY)
            await pilot.click("#radio-ai-claude")  # type: ignore[arg-type]
            await _settle(pilot)
            assert start.disabled is False, _normalize_ws(_screen_text(app))
            tokens = await _copied_command_tokens(pilot)
            assert tokens[tokens.index("--llm") + 1] == "claude", tokens
            assert SENTINEL_KEY not in _normalize_ws(_screen_text(app))
            assert SENTINEL_KEY not in pilot.app.clipboard  # type: ignore[attr-defined]

            # --- No AI: Start stays enabled and the command shows --llm none, key still present.
            await pilot.click("#radio-ai-none")  # type: ignore[arg-type]
            await _settle(pilot)
            assert start.disabled is False, _normalize_ws(_screen_text(app))
            tokens = await _copied_command_tokens(pilot)
            assert tokens[tokens.index("--llm") + 1] == "none", tokens
            assert SENTINEL_KEY not in _normalize_ws(_screen_text(app))

            # --- key unset: Claude disables Start with a message naming the key and the No AI/--llm none way out.
            monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
            await pilot.click("#radio-ai-claude")  # type: ignore[arg-type]
            await _settle(pilot)
            assert start.disabled is True, _normalize_ws(_screen_text(app))
            flat = _normalize_ws(_screen_text(app))
            assert "ANTHROPIC_API_KEY" in flat, flat
            assert "noai" in flat.lower() or "llmnone" in flat.lower(), flat
            assert SENTINEL_KEY not in flat

            # --- No AI again: Start re-enables even though the key is still unset.
            await pilot.click("#radio-ai-none")  # type: ignore[arg-type]
            await _settle(pilot)
            assert start.disabled is False, _normalize_ws(_screen_text(app))

    _run(body)


# ---------------------------------------------------------------- TUI-CP5-T03


def test_TUI_CP5_T03_advanced_is_closed_by_default_and_reveals_its_fields(tmp_path: Path) -> None:
    """[TUI-CP5-T03] Advanced starts closed; opening it is choosable (not disabled) once the folders are
    valid and reveals: limiting to one proxy, mock backends, a golden recordings folder with ignored
    headers, AI fix attempts, and skipping the Mule runtime."""
    exports = _exports_3_proxies(tmp_path)

    async def body() -> None:
        from textual.widgets import Collapsible

        from a2m.tui.app import A2MApp

        app = A2MApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await _valid_folders(pilot, exports, tmp_path / "results")

            collapsible = app.screen.query_one("#collapsible-advanced", Collapsible)
            assert collapsible.disabled is False, "Advanced must be choosable once the folders are valid"
            assert collapsible.collapsed is True, "Advanced must start closed"
            before = _normalize_ws(_screen_text(app)).lower()
            assert "mockbackend" not in before, before

            opened = await _open_advanced(pilot)
            assert opened.collapsed is False
            flat = _normalize_ws(_screen_text(app)).lower()

            assert "proxy" in flat, flat  # limit to one proxy
            assert "mock" in flat and "backend" in flat, flat
            assert "golden" in flat, flat
            assert "ignor" in flat and "header" in flat, flat
            assert "fix" in flat and "attempt" in flat, flat
            assert ("skip" in flat and ("mule" in flat or "runtime" in flat)), flat

    _run(body)


# ---------------------------------------------------------------- TUI-CP5-T04


def test_TUI_CP5_T04_advanced_choices_appear_in_the_preview_and_an_invalid_golden_folder_is_caught(
    tmp_path: Path,
) -> None:
    """[TUI-CP5-T04] With Advanced open, choosing only one proxy, mock backends, a golden folder, an ignored
    header, 5 AI fix attempts and skipping the Mule runtime all show up as the matching flags in the command
    preview; a golden folder that does not exist shows a clear message and disables Start, and replacing it
    with a real folder clears the message and re-enables Start."""
    exports = _exports_3_proxies(tmp_path)
    golden_ok = tmp_path / "golden-ok"
    golden_ok.mkdir()
    golden_missing = tmp_path / "golden-does-not-exist"

    async def body() -> None:
        from textual.widgets import Button, Checkbox

        from a2m.tui.app import A2MApp

        app = A2MApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await _valid_folders(pilot, exports, tmp_path / "results")
            await pilot.click("#radio-ai-none")  # type: ignore[arg-type]
            await _settle(pilot)
            start = app.screen.query_one("#start", Button)
            assert start.disabled is False, _normalize_ws(_screen_text(app))  # sanity: setup-ready reached

            await _open_advanced(pilot)
            await _set_input(pilot, "#input-only", "alpha")
            await pilot.click("#checkbox-mock-backends")  # type: ignore[arg-type]
            await _settle(pilot)
            await _set_input(pilot, "#input-golden", str(golden_ok))
            await _set_input(pilot, "#input-golden-ignore-header", "X-Request-ID")
            await _set_input(pilot, "#input-max-fix-attempts", "5")
            await pilot.click("#checkbox-no-runtime")  # type: ignore[arg-type]
            await _settle(pilot)

            assert app.screen.query_one("#checkbox-mock-backends", Checkbox).value is True
            assert app.screen.query_one("#checkbox-no-runtime", Checkbox).value is True
            assert start.disabled is False, _normalize_ws(_screen_text(app))

            tokens = await _copied_command_tokens(pilot)
            assert tokens[tokens.index("--only") + 1] == "alpha", tokens
            assert "--mock-backends" in tokens, tokens
            assert tokens[tokens.index("--golden") + 1] == str(golden_ok), tokens
            assert tokens[tokens.index("--golden-ignore-header") + 1] == "X-Request-ID", tokens
            assert tokens[tokens.index("--max-fix-attempts") + 1] == "5", tokens
            assert "--no-runtime" in tokens, tokens

            # A golden folder that does not exist: a2m's own message, Start disabled.
            await _set_input(pilot, "#input-golden", str(golden_missing))
            flat = _normalize_ws(_screen_text(app))
            assert _normalize_ws(f"golden recordings folder {golden_missing} does not exist") in flat, flat
            assert start.disabled is True, flat

            # Back to a real folder: the message clears, Start re-enables.
            await _set_input(pilot, "#input-golden", str(golden_ok))
            flat = _normalize_ws(_screen_text(app))
            assert _normalize_ws(f"golden recordings folder {golden_missing} does not exist") not in flat, flat
            assert start.disabled is False, flat

    _run(body)
