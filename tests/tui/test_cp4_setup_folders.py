"""TUI CP4: pick the input and results folders (checkpoint plan: tests/CP4.json in the TUI run folder).

Drives the real Setup screen headlessly through Textual's ``App.run_test()``/``Pilot`` (same technique
as tests/tui/test_cp3_launch.py): ``a2m.tui.app`` is imported lazily *inside* each test function, never
at module import time, so a missing module fails only that one test.

Folder fields are driven through the ``input-exports``/``input-results`` Input widgets CP3 already
mounted (the only ids this checkpoint's plan fixes); finishing typing a path is simulated by setting
``Input.value`` directly, which is exactly what Textual's own ``Input`` does on every keystroke (it posts
a ``Changed`` message from ``_watch_value``), so this is indistinguishable from a user typing the whole
path and is far less flaky than driving every character through ``Pilot.press``. Everything else this
checkpoint adds (the found-proxies/validation line, the command preview's live text, the Resume/Force
choice) is read back from the rendered screen (``_screen_text``, copied from CP3's ``_screen_rows``) or
found by its visible label (``_find_button``), never by guessing an id or method the Setup screen has not
been built yet to have.

Every fixture is a real folder under ``tmp_path`` (never mocked), built with tests/conftest.py's
``write_bundle_dir`` for proxies, a small local helper for shared-flow bundles, and ``a2m.layout`` directly
for "earlier results folder" states (the marker and ``.done`` file a real ``a2m migrate`` run would leave),
so validation exercises the real ``a2m.discovery``/``a2m.engine`` checks, never a re-implementation of them.
The expected messages below are quoted from those modules as they already exist (engine.py, discovery.py),
so a message assertion failing here means the TUI is not calling them, not that this test guessed wrong.
"""

from __future__ import annotations

import io
import re
import sys
from pathlib import Path

import pytest
from conftest import write_bundle_dir

from a2m import layout, safefs
from a2m.cli import main as a2m_main
from tui.screen import _copied_command_tokens, _normalize_ws, _run, _screen_text, _set_input, _settle


def _assert_preview_is_one_line(app: object) -> None:
    """The on-screen command preview occupies exactly one terminal row (it may end with "…")."""
    text = _screen_text(app)
    lines = [line for line in text.splitlines() if "a2m migrate" in line or "a2m  migrate" in line]
    assert len(lines) == 1, text
    assert "\n" not in lines[0].strip(), lines[0]


def _find_button(app: object, label_text: str) -> object:
    """The one Button on ``app``'s current screen whose visible label contains ``label_text``."""
    from textual.widgets import Button

    buttons = list(app.screen.query(Button))  # type: ignore[attr-defined]
    matches = [b for b in buttons if label_text.lower() in str(b.label).lower()]
    assert len(matches) == 1, (label_text, [str(b.label) for b in buttons])
    return matches[0]


def _write_sharedflow(parent: Path, name: str) -> Path:
    """A minimal sharedflowbundle/ bundle at parent/name, the shared-flow equivalent of write_bundle_dir."""
    root = parent / name / "sharedflowbundle"
    root.mkdir(parents=True)
    (root / f"{name}.xml").write_text(
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<SharedFlowBundle revision="1" name="{name}"/>\n',
        encoding="utf-8",
    )
    return parent / name


def _exports_3_proxies(tmp_path: Path, slug: str = "exports-3-proxies") -> Path:
    exports = tmp_path / slug
    exports.mkdir()
    write_bundle_dir(exports, "alpha")
    write_bundle_dir(exports, "beta")
    write_bundle_dir(exports, "gamma")
    return exports


def _exports_2_proxies_1_sharedflow(tmp_path: Path) -> Path:
    exports = tmp_path / "exports-2-1"
    exports.mkdir()
    write_bundle_dir(exports, "delta")
    write_bundle_dir(exports, "epsilon")
    _write_sharedflow(exports, "common-flow")
    return exports


def _exports_empty(tmp_path: Path) -> Path:
    exports = tmp_path / "exports-empty"
    exports.mkdir()
    (exports / "notes.txt").write_text("nothing here\n", encoding="utf-8")
    return exports


def _make_earlier_results(tmp_path: Path, slug: str, *, force_pending: bool = False) -> Path:
    """A results folder a real `a2m migrate` run would have left: the marker plus one proxy's .done."""
    out = tmp_path / slug
    out.mkdir()
    (out / layout.RESULTS_MARKER_NAME).write_text(layout.RESULTS_MARKER_TEXT, encoding="utf-8")
    proxy_dir = out / "alpha"
    proxy_dir.mkdir()
    (proxy_dir / layout.DONE_MARKER_NAME).write_text("done\n", encoding="utf-8")
    if force_pending:
        (out / layout.FORCE_PENDING_NAME).write_text(layout.FORCE_PENDING_TEXT, encoding="utf-8")
    return out


def _results_non_a2m(tmp_path: Path) -> Path:
    out = tmp_path / "results-non-a2m"
    out.mkdir()
    (out / "notes.txt").write_text("some other tool's output\n", encoding="utf-8")
    return out


def _snapshot(root: Path) -> set[tuple[str, bool, int]]:
    """(relative path, is_dir, size) for everything under ``root``, to prove validation wrote nothing."""
    return {
        (str(p.relative_to(root)), p.is_dir(), 0 if p.is_dir() else p.stat().st_size)
        for p in root.rglob("*")
    }


# ---------------------------------------------------------------- plain-CLI regression helper (mirrors
# tests/tui/test_cp3_launch.py's _Stream/run_main, so the same technique proves the same thing: these
# checks run a2m.cli.main exactly as a non-TUI caller would, with no real file descriptor involved).


class _Stream:
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


def _run_cli(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> tuple[int, str, str]:
    out_stream = _Stream(False)
    err_stream = _Stream(False)
    monkeypatch.setattr(sys, "stdin", _Stream(False))
    monkeypatch.setattr(sys, "stdout", out_stream)
    monkeypatch.setattr(sys, "stderr", err_stream)
    try:
        code = a2m_main(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 2
    return code, out_stream.text, err_stream.text


# ---------------------------------------------------------------- TUI-CP4-T01


def test_TUI_CP4_T01_folders_validate_the_same_way_the_cli_would(tmp_path: Path) -> None:
    """[TUI-CP4-T01] Exports and results folders validate the same way the CLI would: nothing picked shows
    no count and a disabled Start; a valid exports folder reports its real proxy/shared-flow counts and
    enables Start once results is also valid; an empty or missing exports folder, a results folder inside
    exports, a non-a2m results folder and an in-use results folder each show the CLI's own message and keep
    Start disabled; the app never raises."""
    proxies_3 = _exports_3_proxies(tmp_path)
    proxies_2_sf_1 = _exports_2_proxies_1_sharedflow(tmp_path)
    empty_exports = _exports_empty(tmp_path)
    missing_exports = tmp_path / "does-not-exist"
    results_empty = tmp_path / "results-empty"
    results_non_a2m = _results_non_a2m(tmp_path)
    results_locked = _make_earlier_results(tmp_path, "results-locked")

    async def body() -> None:
        from textual.widgets import Button

        from a2m.tui.app import A2MApp

        app = A2MApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            # Nothing picked: no count shown anywhere, Start stays disabled.
            text = _screen_text(app).lower()
            assert "proxies" not in text, text
            start = app.screen.query_one("#start", Button)
            assert start.disabled is True

            # A valid exports folder: real counts shown, Start still disabled (no results folder yet).
            await _set_input(pilot, "#input-exports", str(proxies_3))
            text = _screen_text(app)
            assert re.search(r"\b3 proxies\b", text), text
            assert start.disabled is True

            # Results also valid: Start enables.
            await _set_input(pilot, "#input-results", str(results_empty))
            assert start.disabled is False, _screen_text(app)

            # Re-picking the same exports folder is idempotent (stands in for "typed, then re-picked"
            # per CP4.json T01: whichever path the value came from, re-validating it changes nothing).
            await _set_input(pilot, "#input-exports", str(proxies_3))
            text = _screen_text(app)
            assert re.search(r"\b3 proxies\b", text), text
            assert start.disabled is False

            # A different exports folder (2 proxies, 1 shared flow): counts update, old count is gone.
            await _set_input(pilot, "#input-exports", str(proxies_2_sf_1))
            text = _screen_text(app)
            assert re.search(r"\b2 proxies\b", text), text
            assert re.search(r"\b1 shared flow", text, re.IGNORECASE), text
            assert "3 proxies" not in text
            assert start.disabled is False

            # Exports folder with no bundles: the CLI's own "no proxies found" message, Start disabled.
            await _set_input(pilot, "#input-exports", str(empty_exports))
            text = _screen_text(app)
            assert _normalize_ws(f"no proxies found in {empty_exports}") in _normalize_ws(text), text
            assert start.disabled is True

            # Exports folder that does not exist: the CLI's own message, Start disabled.
            await _set_input(pilot, "#input-exports", str(missing_exports))
            text = _screen_text(app)
            assert _normalize_ws(f"input folder {missing_exports} does not exist") in _normalize_ws(text), text
            assert start.disabled is True

            # Back to a valid exports folder, then a results folder inside it: the CLI's own message.
            await _set_input(pilot, "#input-exports", str(proxies_3))
            inside = proxies_3 / "sub"
            await _set_input(pilot, "#input-results", str(inside))
            text = _screen_text(app)
            assert (
                _normalize_ws(f"results folder {inside} must not be the input folder {proxies_3}")
                in _normalize_ws(text)
            ), text
            assert start.disabled is True

            # A non-empty results folder with no a2m marker: the CLI's own message.
            await _set_input(pilot, "#input-results", str(results_non_a2m))
            text = _screen_text(app)
            assert (
                _normalize_ws(
                    f"results folder {results_non_a2m} is not empty and has no "
                    f"{layout.RESULTS_MARKER_NAME} marker"
                )
                in _normalize_ws(text)
            ), text
            assert start.disabled is True

            # A results folder another a2m run currently holds locked: the CLI's own "in use" message.
            with safefs.exclusive_lock(results_locked, layout.lock_path(results_locked)):
                await _set_input(pilot, "#input-results", str(results_locked))
                text = _screen_text(app)
                assert (
                    _normalize_ws(f"results folder {results_locked} is in use by another a2m run")
                    in _normalize_ws(text)
                ), text
                assert start.disabled is True

    _run(body)


# ---------------------------------------------------------------- TUI-CP4-T02


def test_TUI_CP4_T02_earlier_results_offers_resume_or_force_and_force_pending_blocks_resume(
    tmp_path: Path,
) -> None:
    """[TUI-CP4-T02] An earlier results folder offers a Resume/Force choice, not a raw usage error, with
    Start disabled until one is picked; Resume adds --resume and Force adds --force to the command preview;
    a force-pending folder refuses --resume with the CLI's own message and only Force enables Start."""
    exports = _exports_3_proxies(tmp_path)
    earlier = _make_earlier_results(tmp_path, "results-earlier-run")
    force_pending = _make_earlier_results(tmp_path, "results-earlier-run-force-pending", force_pending=True)

    async def body() -> None:
        from textual.widgets import Button

        from a2m.tui.app import A2MApp

        app = A2MApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            start = app.screen.query_one("#start", Button)

            await _set_input(pilot, "#input-exports", str(exports))
            await _set_input(pilot, "#input-results", str(earlier))
            text = _screen_text(app).lower()
            assert "usage error" not in text, text
            resume_button = _find_button(app, "resume")
            force_button = _find_button(app, "force")
            assert start.disabled is True

            await pilot.click(resume_button)  # type: ignore[arg-type]
            await _settle(pilot)
            assert start.disabled is False
            _assert_preview_is_one_line(app)
            tokens = await _copied_command_tokens(pilot)
            assert "--resume" in tokens, tokens

            await pilot.click(force_button)  # type: ignore[arg-type]
            await _settle(pilot)
            assert start.disabled is False
            _assert_preview_is_one_line(app)
            tokens = await _copied_command_tokens(pilot)
            assert "--force" in tokens, tokens
            assert "--resume" not in tokens, tokens

            # A force-pending folder: Resume is refused with the CLI's own message, Start stays disabled.
            await _set_input(pilot, "#input-results", str(force_pending))
            resume_button = _find_button(app, "resume")
            force_button = _find_button(app, "force")
            await pilot.click(resume_button)  # type: ignore[arg-type]
            await _settle(pilot)
            text = _normalize_ws(_screen_text(app))
            assert _normalize_ws("cannot tell finished proxies from old ones") in text, text
            assert _normalize_ws("rerun with --force") in text, text
            assert start.disabled is True

            await pilot.click(force_button)  # type: ignore[arg-type]
            await _settle(pilot)
            assert start.disabled is False
            _assert_preview_is_one_line(app)
            tokens = await _copied_command_tokens(pilot)
            assert "--force" in tokens, tokens

    _run(body)


# ---------------------------------------------------------------- TUI-CP4-T03


def test_TUI_CP4_T03_command_preview_matches_shlex_and_stays_live(tmp_path: Path) -> None:
    """[TUI-CP4-T03] The command preview, parsed with shlex.split, is exactly the current exports path,
    results path and --out, with no quoting bug for paths containing spaces, and updates live (no leftover
    text from the previous exports folder) when the exports choice changes."""
    exports_space = tmp_path / "My Exports"
    exports_space.mkdir()
    write_bundle_dir(exports_space, "alpha")
    write_bundle_dir(exports_space, "beta")
    write_bundle_dir(exports_space, "gamma")
    results_space = tmp_path / "My Results"
    other_exports = _exports_2_proxies_1_sharedflow(tmp_path)

    async def body() -> None:
        from textual.widgets import Button

        from a2m.tui.app import A2MApp

        app = A2MApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            start = app.screen.query_one("#start", Button)

            await _set_input(pilot, "#input-exports", str(exports_space))
            await _set_input(pilot, "#input-results", str(results_space))
            assert start.disabled is False, _screen_text(app)

            _assert_preview_is_one_line(app)
            tokens = await _copied_command_tokens(pilot)
            assert tokens[:2] == ["a2m", "migrate"], tokens
            assert tokens[2] == str(exports_space), tokens
            out_index = tokens.index("--out")
            assert tokens[out_index + 1] == str(results_space), tokens

            # Change exports: counts and the command preview both update, nothing from the old folder lingers.
            await _set_input(pilot, "#input-exports", str(other_exports))
            text = _screen_text(app)
            assert re.search(r"\b2 proxies\b", text), text
            assert re.search(r"\b1 shared flow", text, re.IGNORECASE), text
            assert str(exports_space) not in text, text

            _assert_preview_is_one_line(app)
            tokens = await _copied_command_tokens(pilot)
            assert tokens[2] == str(other_exports), tokens
            out_index = tokens.index("--out")
            assert tokens[out_index + 1] == str(results_space), tokens

    _run(body)


# ---------------------------------------------------------------- TUI-CP4-T04


def test_TUI_CP4_T04_validating_never_writes_and_markup_like_names_render_literally(
    tmp_path: Path,
) -> None:
    """[TUI-CP4-T04] Validating, including re-validating the same folder more than once, never creates,
    modifies or deletes anything under the candidate exports or results folders; a proxy folder name with
    Rich/Textual markup-like characters is shown literally and does not crash the app; and the plain CLI's
    exit code and message for an earlier-results, force-pending, non-a2m and locked results folder are
    unchanged from before this checkpoint."""
    exports = _exports_3_proxies(tmp_path)
    results_empty = tmp_path / "results-empty"
    earlier = _make_earlier_results(tmp_path, "results-earlier-run")
    non_a2m = _results_non_a2m(tmp_path)

    exports_snapshot_before = _snapshot(exports)
    earlier_snapshot_before = _snapshot(earlier)
    non_a2m_snapshot_before = _snapshot(non_a2m)

    async def body() -> None:
        from textual.widgets import Button

        from a2m.tui.app import A2MApp

        app = A2MApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            start = app.screen.query_one("#start", Button)

            # Several validation passes, including re-validating the same results folder twice.
            await _set_input(pilot, "#input-exports", str(exports))
            await _set_input(pilot, "#input-results", str(results_empty))
            assert start.disabled is False
            await _set_input(pilot, "#input-results", str(earlier))
            await _set_input(pilot, "#input-results", str(earlier))
            await _set_input(pilot, "#input-results", str(non_a2m))
            await _set_input(pilot, "#input-results", str(results_empty))
            assert start.disabled is False

            # A proxy folder name with Rich/Textual markup-like characters: no crash, counted normally,
            # and a results path with the same kind of characters renders literally in the command preview.
            bracket_exports = tmp_path / "exports-bracket-name"
            bracket_exports.mkdir()
            write_bundle_dir(bracket_exports, "orders-[v2]")
            bracket_results = tmp_path / "results-[v2]"

            await _set_input(pilot, "#input-exports", str(bracket_exports))
            text = _screen_text(app)
            assert re.search(r"\b1 proxy\b", text), text

            await _set_input(pilot, "#input-results", str(bracket_results))
            assert start.disabled is False, _screen_text(app)
            text = _screen_text(app)
            assert "[v2]" in text, text
            _assert_preview_is_one_line(app)
            tokens = await _copied_command_tokens(pilot)
            out_index = tokens.index("--out")
            assert tokens[out_index + 1] == str(bracket_results), tokens

    _run(body)

    assert _snapshot(exports) == exports_snapshot_before
    assert _snapshot(earlier) == earlier_snapshot_before
    assert _snapshot(non_a2m) == non_a2m_snapshot_before


def test_TUI_CP4_T04_plain_cli_messages_for_results_fixtures_are_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[TUI-CP4-T04] The plain `a2m migrate` CLI (no TUI involved) keeps the exact exit code and message it
    had before this checkpoint for an earlier-results folder, a force-pending folder, a non-a2m folder and
    a locked folder, proving the Setup screen's new read-only validation is additive, not a behavior change."""
    exports = _exports_3_proxies(tmp_path)
    earlier = _make_earlier_results(tmp_path, "results-earlier-run")
    force_pending = _make_earlier_results(tmp_path, "results-earlier-run-force-pending", force_pending=True)
    non_a2m = _results_non_a2m(tmp_path)
    locked = _make_earlier_results(tmp_path, "results-locked")

    code, out, err = _run_cli(monkeypatch, ["migrate", str(exports), "--out", str(earlier), "--llm", "fake"])
    assert code == 2, (out, err)
    assert out == ""
    assert err == (
        f"a2m migrate: usage error: results folder {earlier} already has output from an earlier run; "
        "use --resume to continue it or --force to redo every proxy\n"
    )

    code, out, err = _run_cli(monkeypatch, ["migrate", str(exports), "--out", str(force_pending), "--resume", "--llm", "fake"])
    assert code == 2, (out, err)
    assert out == ""
    assert err == (
        f"a2m migrate: usage error: results folder {force_pending} has a --force run that stopped before it "
        "removed every earlier .done marker, so --resume cannot tell finished proxies from old ones; "
        "rerun with --force to redo every proxy\n"
    )

    code, out, err = _run_cli(monkeypatch, ["migrate", str(exports), "--out", str(non_a2m), "--llm", "fake"])
    assert code == 2, (out, err)
    assert out == ""
    assert err == (
        f"a2m migrate: usage error: results folder {non_a2m} is not empty and has no "
        f"{layout.RESULTS_MARKER_NAME} marker, so it is not an a2m results folder; choose a new or empty "
        "folder\n"
    )

    with safefs.exclusive_lock(locked, layout.lock_path(locked)):
        code, out, err = _run_cli(monkeypatch, ["migrate", str(exports), "--out", str(locked), "--resume", "--llm", "fake"])
    assert code == 2, (out, err)
    assert out == ""
    assert err == (
        f"a2m migrate: usage error: results folder {locked} is in use by another a2m run; "
        "wait for it to finish, then run again\n"
    )
