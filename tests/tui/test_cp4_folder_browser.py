"""TUI CP4 (Rahil's 2026-10-06 feedback): the folder browser, Browse… buttons, resolved paths and Tab completion.

Headless Textual tests through ``App.run_test()``/``Pilot`` against real folders under ``tmp_path`` and the
engine's own read-only checks (same technique as tests/tui/test_cp4_setup_folders.py). ``HOME`` points at a
folder under ``tmp_path`` so ``~`` never reads the real home folder.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from conftest import write_bundle_dir

from a2m import layout
from tui.screen import _flat, _run, _screen_text, _set_input, _settle


def _exports(parent: Path, name: str = "exports") -> Path:
    exports = parent / name
    exports.mkdir(parents=True)
    for proxy in ("alpha", "beta", "gamma"):
        write_bundle_dir(exports, proxy)
    return exports


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


async def _open_browser(pilot: object, field: str) -> object:
    """Press the field's Browse… button and return the folder browser screen."""
    from a2m.tui.picker import FolderPicker

    app = pilot.app  # type: ignore[attr-defined]
    await pilot.click(f"#browse-{field}")  # type: ignore[attr-defined]
    await _settle(pilot)
    assert isinstance(app.screen, FolderPicker), app.screen
    return app.screen


def test_TUI_CP4_B01_browse_opens_at_the_fields_folder_and_shows_its_full_path(tmp_path: Path) -> None:
    """[TUI-CP4-B01] Browse… opens at the field's folder when it exists, at the nearest existing folder above
    it when it does not, and at the home folder when the field is blank; the folder is shown in full at the
    top (with ~ for home), and the dialog fits 80x24 with its buttons in view."""
    exports = _exports(tmp_path / "work")

    async def body() -> None:
        from a2m.tui.app import A2MApp

        app = A2MApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            browser = await _open_browser(pilot, "exports")
            assert browser.current == Path.home()  # type: ignore[attr-defined]
            assert str(browser.query_one("#picker-path").render()) == "~"  # type: ignore[attr-defined]
            await pilot.press("escape")
            await _settle(pilot)

            await _set_input(pilot, "#input-exports", str(exports))
            browser = await _open_browser(pilot, "exports")
            assert browser.current == exports  # type: ignore[attr-defined]
            text = _screen_text(app)
            assert _flat(str(exports)) in _flat(text), text
            assert "Use this folder" in text and "Cancel" in text, text
            await pilot.press("escape")
            await _settle(pilot)

            await _set_input(pilot, "#input-results", str(tmp_path / "work" / "not-yet" / "deeper"))
            browser = await _open_browser(pilot, "results")
            assert browser.current == tmp_path / "work"  # type: ignore[attr-defined]

    _run(body)


def test_TUI_CP4_B02_backspace_enter_tilde_and_typing_navigate(tmp_path: Path, _home: Path) -> None:
    """[TUI-CP4-B02] Backspace goes to the parent with the folder just left highlighted, Enter opens the
    highlighted folder, typed letters jump to the first matching name, and ~ goes home."""
    exports = _exports(tmp_path / "work")
    (tmp_path / "work" / "zeta").mkdir()

    async def body() -> None:
        from a2m.tui.app import A2MApp
        from a2m.tui.picker import FolderList

        app = A2MApp(exports=str(exports))
        async with app.run_test(size=(80, 24)) as pilot:
            await _settle(pilot)
            browser = await _open_browser(pilot, "exports")
            folder_list = browser.query_one(FolderList)  # type: ignore[attr-defined]

            await pilot.press("backspace")
            await _settle(pilot)
            assert browser.current == tmp_path / "work"  # type: ignore[attr-defined]
            row = folder_list.rows[folder_list.highlighted]
            assert row is not None and row.path == exports

            await pilot.press("z")
            await _settle(pilot)
            row = folder_list.rows[folder_list.highlighted]
            assert row is not None and row.name == "zeta"

            await pilot.press("e", "x")  # "zex" matches nothing, so "x" alone is tried: still nothing
            await pilot.press("e")
            await _settle(pilot)
            row = folder_list.rows[folder_list.highlighted]
            assert row is not None and row.name == "exports"

            await pilot.press("enter")
            await _settle(pilot)
            assert browser.current == exports  # type: ignore[attr-defined]
            names = [row.name for row in folder_list.rows[1:] if row is not None]
            assert names == ["alpha", "beta", "gamma"]

            await pilot.press("tilde")
            await _settle(pilot)
            assert browser.current == _home  # type: ignore[attr-defined]

    _run(body)


def test_TUI_CP4_B03_preview_counts_proxies_and_describes_results_folders(tmp_path: Path) -> None:
    """[TUI-CP4-B03] The preview line under the list reports what a2m finds in the highlighted folder: the
    exports fixture's 3 proxies and 0 shared flows, the CLI's own message for a single bundle, and for
    the results field an earlier a2m results folder versus an empty one; nothing is written."""
    exports = _exports(tmp_path / "work")
    earlier = tmp_path / "work" / "earlier"
    earlier.mkdir()
    (earlier / layout.RESULTS_MARKER_NAME).write_text(layout.RESULTS_MARKER_TEXT, encoding="utf-8")
    (tmp_path / "work" / "fresh").mkdir()
    before = sorted(str(p) for p in tmp_path.rglob("*"))

    async def body() -> None:
        from textual.widgets import Static

        from a2m.tui.app import A2MApp
        from a2m.tui.picker import FolderList

        app = A2MApp(exports=str(exports))
        async with app.run_test(size=(80, 24)) as pilot:
            await _settle(pilot)
            browser = await _open_browser(pilot, "exports")
            preview = browser.query_one("#picker-preview", Static)  # type: ignore[attr-defined]
            assert "✓ 3 proxies, 0 shared flows found" in str(preview.render()), preview.render()

            await pilot.press("a")  # highlight alpha: one bundle, not a folder of bundles
            await _settle(pilot)
            assert f"✗ {exports / 'alpha'} is itself a bundle" in str(preview.render()), preview.render()
            await pilot.press("escape")
            await _settle(pilot)

            await _set_input(pilot, "#input-results", str(tmp_path / "work" / "earlier"))
            browser = await _open_browser(pilot, "results")
            preview = browser.query_one("#picker-preview", Static)  # type: ignore[attr-defined]
            assert "earlier a2m run" in str(preview.render()), preview.render()

            await pilot.press("backspace", "f")
            await _settle(pilot)
            row = browser.query_one(FolderList).rows[browser.query_one(FolderList).highlighted]  # type: ignore[attr-defined]
            assert row is not None and row.name == "fresh"
            assert "✓ No results yet" in str(preview.render()), preview.render()

    _run(body)
    assert sorted(str(p) for p in tmp_path.rglob("*")) == before


def test_TUI_CP4_B04_use_this_folder_fills_the_field_and_escape_cancels(tmp_path: Path) -> None:
    """[TUI-CP4-B04] ctrl+u (or the Use this folder button) fills the field with the highlighted folder and
    closes the browser, and the field is checked as if typed; Escape closes it leaving the field as it was.
    Markup-like and unreadable folder names show literally and do not crash."""
    work = tmp_path / "work"
    exports = _exports(work)
    (work / "[red]odd[bold]").mkdir()
    locked = work / "locked"
    locked.mkdir()
    locked.chmod(0)

    async def body() -> None:
        from textual.widgets import Input

        from a2m.tui.app import A2MApp
        from a2m.tui.setup import SetupScreen

        app = A2MApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await _settle(pilot)
            await _set_input(pilot, "#input-exports", str(work))
            await _open_browser(pilot, "exports")
            text = _screen_text(app)
            assert "[red]odd[bold]" in text, text
            if os.geteuid() != 0:
                assert "locked (cannot read)" in text, text
                await pilot.press("l", "enter")  # an unreadable folder does not open, and nothing crashes
                await _settle(pilot)
                assert "cannot read" in _screen_text(app)

            await pilot.press("escape")
            await _settle(pilot)
            assert isinstance(app.screen, SetupScreen)
            assert app.screen.query_one("#input-exports", Input).value == str(work)

            await _open_browser(pilot, "exports")
            await pilot.press("e", "ctrl+u")
            await _settle(pilot)
            assert isinstance(app.screen, SetupScreen)
            assert app.screen.query_one("#input-exports", Input).value == str(exports)
            assert re.search(r"\b3 proxies\b", _screen_text(app)), _screen_text(app)

            await _set_input(pilot, "#input-results", str(work / "out"))  # not there yet: opens at work
            await _open_browser(pilot, "results")
            await pilot.click("#picker-use")
            await _settle(pilot)
            assert app.screen.query_one("#input-results", Input).value == str(work)

    try:
        _run(body)
    finally:
        locked.chmod(0o700)


def test_TUI_CP4_B05_relative_path_shows_its_absolute_folder_and_tab_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[TUI-CP4-B05] A relative path shows the absolute folder it names on a dim line under the field (an
    absolute path shows none), and Tab completes a folder name as it is typed, then moves on."""
    _exports(tmp_path, "exports-3-proxies")
    monkeypatch.chdir(tmp_path)

    async def body() -> None:
        from textual.widgets import Input, Static

        from a2m.tui.app import A2MApp

        app = A2MApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await _settle(pilot)
            await _set_input(pilot, "#input-exports", "exports-3-proxies")
            resolved = app.screen.query_one("#input-exports-resolved", Static)
            assert resolved.display is True
            assert _flat(f"→ {tmp_path / 'exports-3-proxies'}") in _flat(_screen_text(app)), _screen_text(app)

            await _set_input(pilot, "#input-exports", str(tmp_path / "exports-3-proxies"))
            assert resolved.display is False

            field = app.screen.query_one("#input-exports", Input)
            field.focus()
            await _set_input(pilot, "#input-exports", "exp")
            field.cursor_position = len(field.value)
            await pilot.press("tab")
            await _settle(pilot)
            assert field.value == f"exports-3-proxies{os.sep}"
            assert app.focused is field

            await pilot.press("tab")  # nothing left to complete: focus moves on
            await _settle(pilot)
            assert app.focused is not field

    _run(body)


def test_TUI_CP4_B06_both_relative_with_earlier_results_fits_80x24(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[TUI-CP4-B06] With relative paths in both folder fields (so both absolute folder lines show) and a
    results folder from an earlier run, the whole form down to the Pick Resume or Force line fits 80x24
    without scrolling."""
    _exports(tmp_path, "ex")
    earlier = tmp_path / "out"
    earlier.mkdir()
    (earlier / layout.RESULTS_MARKER_NAME).write_text(layout.RESULTS_MARKER_TEXT, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    async def body() -> None:
        from textual.widgets import Static

        from a2m.tui.app import A2MApp
        from a2m.tui.setup import Body

        app = A2MApp(exports="ex", results="out")
        async with app.run_test(size=(80, 24)) as pilot:
            await _settle(pilot)
            screen = app.screen
            assert screen.query_one("#input-exports-resolved", Static).display is True
            assert screen.query_one("#input-results-resolved", Static).display is True
            assert screen.query_one("#resume-prompt", Static).display is True
            assert screen.query_one(Body).max_scroll_y == 0
            text = _screen_text(app)
            assert "Pick Resume or Force to continue" in text, text

    _run(body)


def test_TUI_CP4_B07_force_pending_and_locked_resume_gives_the_cli_message(tmp_path: Path) -> None:
    """[TUI-CP4-B07] A results folder that is both force-pending and held locked by another run: the TUI's
    Resume check gives the same message `a2m migrate --resume` gives (prepare_run's force-pending refusal,
    which runs before the run's lock attempt), not the in-use message."""
    from a2m import engine, safefs
    from a2m.engine import LlmChoice

    exports = _exports(tmp_path)
    out = tmp_path / "results"
    out.mkdir()
    (out / layout.RESULTS_MARKER_NAME).write_text(layout.RESULTS_MARKER_TEXT, encoding="utf-8")
    (out / layout.FORCE_PENDING_NAME).write_text(layout.FORCE_PENDING_TEXT, encoding="utf-8")
    names = engine.discover(exports).candidate_names()

    # Held only for the two checks; the with block releases it (in its finally) even when a check fails.
    with safefs.exclusive_lock(out, layout.lock_path(out)):
        with pytest.raises(engine.UsageError) as cli:
            engine.prepare_run(
                engine.RunOptions(input_dir=exports, out_dir=out, resume=True, llm=LlmChoice.FAKE, no_runtime=True)
            )
        with pytest.raises(engine.UsageError) as tui:
            engine.check_results_folder(out, input_dir=exports, names=names, resume=True)
    assert "--force run that stopped" in str(cli.value), cli.value
    assert str(tui.value) == str(cli.value)


def test_TUI_CP4_B08_force_pending_and_locked_still_offers_the_rerun_choice(tmp_path: Path) -> None:
    """[TUI-CP4-B08] A results folder that is both force-pending and held locked by another run, checked
    before any Resume or Force pick: the lock (checked last) does not hide the folder's state. The setup
    check still reports FORCE_PENDING and offers the choice, alongside the in-use message; Resume then gives
    the CLI's force-pending message and Force the in-use message; the browser preview says both."""
    from a2m import engine, safefs
    from a2m.engine import LlmChoice, ResultsFolder
    from a2m.tui.command import RerunChoice, SetupChoices
    from a2m.tui.folders import Status, check_setup, preview_results

    exports = _exports(tmp_path)
    out = tmp_path / "results"
    out.mkdir()
    (out / layout.RESULTS_MARKER_NAME).write_text(layout.RESULTS_MARKER_TEXT, encoding="utf-8")
    (out / layout.FORCE_PENDING_NAME).write_text(layout.FORCE_PENDING_TEXT, encoding="utf-8")
    in_use = f"results folder {out.absolute()} is in use by another a2m run"

    # Held only for the checks; the with block releases it (in its finally) even when a check fails.
    with safefs.exclusive_lock(out, layout.lock_path(out)):
        first = check_setup(SetupChoices(exports=str(exports), results=str(out)))
        resume = check_setup(SetupChoices(exports=str(exports), results=str(out), rerun=RerunChoice.RESUME))
        force = check_setup(SetupChoices(exports=str(exports), results=str(out), rerun=RerunChoice.FORCE))
        preview = preview_results(out, exports=exports)
        with pytest.raises(engine.UsageError) as cli_resume:
            engine.prepare_run(
                engine.RunOptions(input_dir=exports, out_dir=out, resume=True, llm=LlmChoice.FAKE, no_runtime=True)
            )

    assert first.results_folder is ResultsFolder.FORCE_PENDING, first
    assert first.needs_rerun_choice is True
    assert first.results.status is Status.INVALID and in_use in first.results.text, first.results
    assert first.ready is False

    assert resume.results_folder is ResultsFolder.FORCE_PENDING
    assert "--force run that stopped" in str(cli_resume.value), cli_resume.value
    assert resume.rerun_error == str(cli_resume.value), resume.rerun_error
    assert resume.ready is False

    assert force.results_folder is ResultsFolder.FORCE_PENDING
    assert force.rerun_error is not None and in_use in force.rerun_error, force.rerun_error
    assert force.ready is False

    assert preview.status is Status.INVALID
    assert in_use in preview.text and "only Force can continue it" in preview.text, preview.text
