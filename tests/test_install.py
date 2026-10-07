"""CP1: ``./install.sh`` installs a2m with one command.

Runs the real ``install.sh`` (not written yet) as a subprocess under a
temporary ``HOME`` and a sandboxed ``PATH``. The sandboxed ``PATH`` holds
symlinks to only the system tools the script needs (bash, env, mkdir, ln,
rm, readlink, dirname, cat, grep, sed, uname, chmod) plus fake ``uv`` and
``pipx`` scripts that log their argv to a file under ``HOME`` and create (or
remove) a stub ``a2m`` command, exactly the way a real install would but
without ever touching the real HOME, the machine's real uv/pipx, or the
network. ``install.sh`` does not exist yet, so every case here fails now
because the subprocess can't find it, not because of a broken harness.

Case IDs:

* INSTALL-CP1-T01 - with uv available, install is an editable, forced/
  reinstalling install naming this checkout with the tui extra; rerunning
  upgrades in place (still exactly one a2m command) and prints no PATH
  advice once the bin folder is already on PATH.
* INSTALL-CP1-T02 - without uv, the script falls back to pipx; --no-tui
  drops the tui extra; uv is never invoked.
* INSTALL-CP1-T03 - when the install bin folder is not on PATH, the script
  names ~/.bashrc and prints an export PATH line, without editing any shell
  file.
* INSTALL-CP1-T04 - --uninstall removes only the a2m command and the
  script's install record, leaving unrelated files and ~/.bashrc alone; a
  second --uninstall says nothing is installed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO / "install.sh"

# The few real system tools install.sh needs; symlinked into the sandbox bin
# folder so the script can run without exposing anything else on PATH.
SYSTEM_TOOLS = ("bash", "env", "mkdir", "ln", "rm", "readlink", "dirname", "cat", "grep", "sed", "uname", "chmod")

# Fake uv: logs its argv (space-joined) to $HOME/.fake-uv.log, then on `tool
# install` writes an executable $HOME/.local/bin/a2m stub printing
# "a2m 0.1.0", on `tool dir --bin` prints that same folder, and on
# `tool uninstall a2m` removes the stub. Never touches anything real.
FAKE_UV = """#!/usr/bin/env bash
set -euo pipefail
LOG="$HOME/.fake-uv.log"
printf '%s\\n' "$*" >> "$LOG"
BIN_DIR="$HOME/.local/bin"
if [ "${1:-}" = "tool" ] && [ "${2:-}" = "install" ]; then
    mkdir -p "$BIN_DIR"
    printf '#!/usr/bin/env bash\\necho "a2m 0.1.0"\\n' > "$BIN_DIR/a2m"
    chmod +x "$BIN_DIR/a2m"
elif [ "${1:-}" = "tool" ] && [ "${2:-}" = "dir" ]; then
    echo "$BIN_DIR"
elif [ "${1:-}" = "tool" ] && [ "${2:-}" = "uninstall" ]; then
    rm -f "$BIN_DIR/a2m"
fi
"""

# Fake pipx: same contract as fake uv for `install` and `uninstall`, logging
# to $HOME/.fake-pipx.log and using $PIPX_BIN_DIR (unset in these tests) or
# $HOME/.local/bin.
FAKE_PIPX = """#!/usr/bin/env bash
set -euo pipefail
LOG="$HOME/.fake-pipx.log"
printf '%s\\n' "$*" >> "$LOG"
BIN_DIR="${PIPX_BIN_DIR:-$HOME/.local/bin}"
if [ "${1:-}" = "install" ]; then
    mkdir -p "$BIN_DIR"
    printf '#!/usr/bin/env bash\\necho "a2m 0.1.0"\\n' > "$BIN_DIR/a2m"
    chmod +x "$BIN_DIR/a2m"
elif [ "${1:-}" = "uninstall" ]; then
    rm -f "$BIN_DIR/a2m"
fi
"""

TIMEOUT_SEC = 30


# ---------------------------------------------------------------- sandbox helpers


def _make_sandbox_bin(tmp_path: Path) -> Path:
    """A fresh bin folder holding symlinks to only the real tools install.sh needs."""
    bin_dir = tmp_path / "sandbox-bin"
    bin_dir.mkdir()
    for tool in SYSTEM_TOOLS:
        real = shutil.which(tool)
        if real is None:
            pytest.fail(f"required system tool {tool!r} was not found on this machine's PATH")
        (bin_dir / tool).symlink_to(real)
    return bin_dir


def _install_fake_tool(bin_dir: Path, name: str, script: str) -> None:
    path = bin_dir / name
    path.write_text(script)
    path.chmod(0o755)


def _base_env(home: Path) -> dict[str, str]:
    """A minimal, deterministic env: the real HOME, mise/uv/pipx/venv state stripped."""
    env = dict(os.environ)
    drop_prefixes = ("MISE", "UV_", "XDG_", "PIPX_")
    for key in list(env):
        if key == "VIRTUAL_ENV" or key.startswith(drop_prefixes):
            env.pop(key, None)
    for key in ("FORCE_COLOR", "PY_COLORS", "ANTHROPIC_API_KEY"):
        env.pop(key, None)
    env["HOME"] = str(home)
    env["SHELL"] = "/bin/bash"
    return env


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(INSTALL_SH), *args],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SEC,
        check=False,
    )


def _combined(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


def _seed_untouched(install_bin: Path, home: Path) -> dict[Path, bytes]:
    """An unrelated executable and a sentinel file in the install bin folder, plus a
    ~/.bashrc with known content, to prove uninstall and PATH advice leave them alone."""
    install_bin.mkdir(parents=True, exist_ok=True)
    other = install_bin / "other-tool"
    other.write_text("#!/bin/sh\necho unrelated\n")
    other.chmod(0o755)
    sentinel = install_bin / ".sentinel"
    sentinel.write_text("sentinel-contents-do-not-touch\n")
    bashrc = home / ".bashrc"
    bashrc.write_text("# pre-existing bashrc, must not change\nexport EDITOR=vim\n")
    return {other: other.read_bytes(), sentinel: sentinel.read_bytes(), bashrc: bashrc.read_bytes()}


def _assert_untouched(snapshot: dict[Path, bytes]) -> None:
    for path, original in snapshot.items():
        assert path.exists(), f"{path} was removed but should be untouched"
        assert path.read_bytes() == original, f"{path} content changed but should be untouched"


# ---------------------------------------------------------------- INSTALL-CP1-T01


def test_INSTALL_CP1_T01_uv_editable_install_with_tui_and_rerun_upgrades(tmp_path: Path) -> None:
    """[INSTALL-CP1-T01] With uv available, install is editable/forced and names this checkout
    with the tui extra; rerunning upgrades in place and prints no PATH advice."""
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = _make_sandbox_bin(tmp_path)
    _install_fake_tool(bin_dir, "uv", FAKE_UV)
    env = _base_env(home)
    install_bin = home / ".local" / "bin"
    env["PATH"] = f"{bin_dir}:{install_bin}"

    first = _run([], env)
    out1 = _combined(first)
    assert first.returncode == 0, f"first run: expected exit 0, got {first.returncode}:\n{out1}"

    second = _run([], env)
    out2 = _combined(second)
    assert second.returncode == 0, f"second run: expected exit 0, got {second.returncode}:\n{out2}"

    log = (home / ".fake-uv.log").read_text()
    install_lines = [line for line in log.splitlines() if line.startswith("tool install")]
    assert len(install_lines) >= 2, f"expected a `tool install` call on each run:\n{log}"
    for line in install_lines:
        assert "--editable" in line, line
        assert "--force" in line or "--reinstall" in line, f"expected a forced/reinstalling install: {line}"
        assert str(REPO) in line, f"expected this checkout's path in the install target: {line}"
        assert "tui" in line, f"expected the tui extra by default: {line}"

    assert out2.rstrip().endswith("a2m 0.1.0"), f"expected the output to end with the installed version:\n{out2}"

    a2m_entries = sorted(p.name for p in install_bin.glob("a2m*"))
    assert a2m_entries == ["a2m"], f"expected exactly one a2m command, found {a2m_entries}"

    assert re.search(r"export\s+PATH=", out2) is None, (
        f"no PATH advice should be printed once the bin folder is already on PATH:\n{out2}"
    )


# ---------------------------------------------------------------- INSTALL-CP1-T02


def test_INSTALL_CP1_T02_pipx_fallback_without_tui_extra_and_uv_never_called(tmp_path: Path) -> None:
    """[INSTALL-CP1-T02] Without uv the script falls back to pipx; --no-tui drops the tui
    extra and uv is never invoked."""
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = _make_sandbox_bin(tmp_path)
    _install_fake_tool(bin_dir, "pipx", FAKE_PIPX)
    env = _base_env(home)
    env["PATH"] = str(bin_dir)

    result = _run(["--no-tui"], env)
    output = _combined(result)
    assert result.returncode == 0, f"expected exit 0, got {result.returncode}:\n{output}"

    uv_log = home / ".fake-uv.log"
    assert not uv_log.exists(), (
        f"uv should never be invoked when it is absent:\n{uv_log.read_text() if uv_log.exists() else ''}"
    )

    pipx_log = (home / ".fake-pipx.log").read_text()
    install_lines = [line for line in pipx_log.splitlines() if line.startswith("install")]
    assert install_lines, f"expected a pipx `install` call:\n{pipx_log}"
    for line in install_lines:
        assert "--editable" in line, line
        assert str(REPO) in line, f"expected this checkout's path in the install target: {line}"
        assert "tui" not in line, f"--no-tui should drop the tui extra: {line}"

    assert "a2m 0.1.0" in output, f"expected the version line to be printed:\n{output}"


# ---------------------------------------------------------------- INSTALL-CP1-T03


def test_INSTALL_CP1_T03_path_advice_names_bashrc_without_editing_any_shell_file(tmp_path: Path) -> None:
    """[INSTALL-CP1-T03] When the install bin folder is not on PATH, the script names
    ~/.bashrc and prints an export PATH line, without editing any shell file."""
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = _make_sandbox_bin(tmp_path)
    _install_fake_tool(bin_dir, "uv", FAKE_UV)
    env = _base_env(home)
    env["PATH"] = str(bin_dir)  # deliberately: <HOME>/.local/bin is NOT on PATH
    env["SHELL"] = "/bin/bash"

    bashrc = home / ".bashrc"
    known_content = "# pre-existing bashrc, must not change\nexport EDITOR=vim\n"
    bashrc.write_text(known_content)

    result = _run([], env)
    output = _combined(result)
    assert result.returncode == 0, f"expected exit 0, got {result.returncode}:\n{output}"

    install_bin = home / ".local" / "bin"
    assert ".bashrc" in output, f"expected the output to name ~/.bashrc:\n{output}"
    assert ".zshrc" not in output, f"wrong shell file named for SHELL=/bin/bash:\n{output}"
    assert "config.fish" not in output, f"wrong shell file named for SHELL=/bin/bash:\n{output}"
    assert re.search(r"export\s+PATH=.*" + re.escape(str(install_bin)), output), (
        f"expected an export PATH line naming {install_bin}:\n{output}"
    )

    assert bashrc.read_bytes() == known_content.encode(), "~/.bashrc must stay byte-for-byte unchanged"
    other_shell_files = [p for p in home.iterdir() if p.name in {".zshrc", ".profile", ".bash_profile"}]
    assert other_shell_files == [], f"no other shell file should be created: {other_shell_files}"
    fish_config = home / ".config" / "fish" / "config.fish"
    assert not fish_config.exists(), "no fish config should be created for SHELL=/bin/bash"


# ---------------------------------------------------------------- INSTALL-CP1-T04


def test_INSTALL_CP1_T04_uninstall_removes_only_what_the_script_installed(tmp_path: Path) -> None:
    """[INSTALL-CP1-T04] --uninstall removes only the a2m command and the script's install
    record; a second --uninstall says nothing is installed."""
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = _make_sandbox_bin(tmp_path)
    _install_fake_tool(bin_dir, "uv", FAKE_UV)
    env = _base_env(home)
    env["PATH"] = str(bin_dir)

    setup = _run([], env)
    assert setup.returncode == 0, f"setup install failed: {_combined(setup)}"

    install_bin = home / ".local" / "bin"
    record = home / ".local" / "share" / "a2m" / "install-record"
    assert (install_bin / "a2m").exists(), "setup install should have created the a2m command"
    assert record.exists(), "setup install should have written the install record"

    snapshot = _seed_untouched(install_bin, home)

    first = _run(["--uninstall"], env)
    out1 = _combined(first)
    assert first.returncode == 0, f"expected exit 0, got {first.returncode}:\n{out1}"

    uv_log = (home / ".fake-uv.log").read_text()
    assert "tool uninstall a2m" in uv_log, f"expected uv to be asked to uninstall a2m:\n{uv_log}"

    assert not (install_bin / "a2m").exists(), "the a2m command should be gone after uninstall"
    assert not record.exists(), "the install record should be gone after uninstall"
    _assert_untouched(snapshot)

    second = _run(["--uninstall"], env)
    out2 = _combined(second)
    assert second.returncode == 0, f"expected exit 0 on a second uninstall, got {second.returncode}:\n{out2}"
    low = out2.lower()
    assert "nothing" in low and "install" in low, f"expected a message saying nothing is installed:\n{out2}"
