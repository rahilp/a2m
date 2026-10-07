"""CP1 adversarial round 1: ``./install.sh`` failure paths.

Same sandbox as tests/test_install.py: the real ``install.sh`` runs as a
subprocess under a temporary ``HOME`` and a ``PATH`` holding only symlinks to
the system tools it needs plus fake ``uv``, ``pipx`` and ``python`` scripts.
Nothing real is installed and the network is never used.

Case IDs:

* INSTALL-CP1-X01 - uv fails to uninstall while it still lists a2m: non-zero
  exit, the install record and the command are kept, no success message.
* INSTALL-CP1-X02 - pipx fails to uninstall and its listing fails too (state
  unknown): non-zero exit, record kept, no success message; a retry once pipx
  works removes it.
* INSTALL-CP1-X03 - uv fails to uninstall but positively reports a2m absent
  and the recorded command is gone: treated as already uninstalled.
* INSTALL-CP1-X04 - pipx present with only an old Python (3.10, also pipx's
  own default): stops with the one-line "install Python 3.11+ or uv" message
  and never calls ``pipx install``.
* INSTALL-CP1-X05 - the private venv cannot be fully removed (read-only
  folder inside it): non-zero exit, record kept, no success message; a retry
  once the folder is writable removes the venv, the link and the record.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO / "install.sh"
SYSTEM_TOOLS = ("bash", "env", "mkdir", "ln", "rm", "readlink", "dirname", "cat", "grep", "sed", "uname", "chmod")
TIMEOUT_SEC = 30

# Fake uv: installs a stub a2m, lists a2m only while the stub exists, and fails every
# `tool uninstall` (as on a permissions error) without removing anything.
FAKE_UV_FAILING_UNINSTALL = """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$HOME/.fake-uv.log"
BIN_DIR="$HOME/.local/bin"
if [ "${1:-}" = "tool" ] && [ "${2:-}" = "install" ]; then
    mkdir -p "$BIN_DIR"
    printf '#!/usr/bin/env bash\\necho "a2m 0.1.0"\\n' > "$BIN_DIR/a2m"
    chmod +x "$BIN_DIR/a2m"
elif [ "${1:-}" = "tool" ] && [ "${2:-}" = "dir" ]; then
    echo "$BIN_DIR"
elif [ "${1:-}" = "tool" ] && [ "${2:-}" = "list" ]; then
    if [ -e "$BIN_DIR/a2m" ]; then
        printf 'a2m v0.1.0\\n- a2m\\n'
    else
        echo "No tools installed"
    fi
elif [ "${1:-}" = "tool" ] && [ "${2:-}" = "uninstall" ]; then
    echo "error: failed to remove the a2m environment: Permission denied (os error 13)" >&2
    exit 1
fi
"""

# Fake pipx: installs a stub a2m; reports $FAKE_PIPX_PYTHON as its default interpreter;
# `uninstall` and `list` fail unless $HOME/.pipx-works exists (then uninstall removes the stub).
FAKE_PIPX = """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$HOME/.fake-pipx.log"
BIN_DIR="${PIPX_BIN_DIR:-$HOME/.local/bin}"
if [ "${1:-}" = "install" ]; then
    mkdir -p "$BIN_DIR"
    printf '#!/usr/bin/env bash\\necho "a2m 0.1.0"\\n' > "$BIN_DIR/a2m"
    chmod +x "$BIN_DIR/a2m"
elif [ "${1:-}" = "environment" ]; then
    echo "${FAKE_PIPX_PYTHON:-}"
elif [ "${1:-}" = "uninstall" ] || [ "${1:-}" = "list" ]; then
    if [ ! -e "$HOME/.pipx-works" ]; then
        echo "pipx: cannot write to the pipx home: Permission denied" >&2
        exit 1
    fi
    if [ "${1:-}" = "uninstall" ]; then
        rm -f "$BIN_DIR/a2m"
    fi
fi
"""

# A fake Python 3.12: every check passes.
FAKE_PYTHON_OK = """#!/usr/bin/env bash
[ "${1:-}" = "--version" ] && echo "Python 3.12.1"
exit 0
"""

# A fake Python 3.10: `--version` works, the `-c` version check (and anything else) fails.
FAKE_PYTHON_OLD = """#!/usr/bin/env bash
if [ "${1:-}" = "--version" ]; then
    echo "Python 3.10.12"
    exit 0
fi
exit 1
"""


def _make_sandbox_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "sandbox-bin"
    bin_dir.mkdir()
    for tool in SYSTEM_TOOLS:
        real = shutil.which(tool)
        if real is None:
            pytest.fail(f"required system tool {tool!r} was not found on this machine's PATH")
        (bin_dir / tool).symlink_to(real)
    return bin_dir


def _write_exe(path: Path, script: str) -> Path:
    path.write_text(script)
    path.chmod(0o755)
    return path


def _env(home: Path, bin_dir: Path) -> dict[str, str]:
    env = dict(os.environ)
    for key in list(env):
        if key == "VIRTUAL_ENV" or key.startswith(("MISE", "UV_", "XDG_", "PIPX_")):
            env.pop(key, None)
    for key in ("FORCE_COLOR", "PY_COLORS", "ANTHROPIC_API_KEY"):
        env.pop(key, None)
    env["HOME"] = str(home)
    env["SHELL"] = "/bin/bash"
    env["PATH"] = str(bin_dir)
    return env


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(INSTALL_SH), *args], cwd=REPO, env=env, capture_output=True, text=True, timeout=TIMEOUT_SEC, check=False
    )


def _out(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


def _sandbox(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    home.mkdir()
    return home, _make_sandbox_bin(tmp_path)


def _record(home: Path) -> Path:
    return home / ".local" / "share" / "a2m" / "install-record"


def _assert_failed_uninstall_kept_everything(result: subprocess.CompletedProcess[str], home: Path) -> None:
    output = _out(result)
    assert result.returncode != 0, f"a failed uninstall must exit non-zero:\n{output}"
    assert _record(home).exists(), "the install record must be kept so the uninstall can be retried"
    assert (home / ".local" / "bin" / "a2m").exists(), "the a2m command really is still installed"
    assert "is uninstalled" not in output.lower(), f"must not claim success:\n{output}"
    assert "nothing to uninstall" not in output.lower(), output


def test_INSTALL_CP1_X01_uv_uninstall_failure_keeps_record_and_exits_nonzero(tmp_path: Path) -> None:
    """[INSTALL-CP1-X01] uv fails to uninstall and still lists a2m: non-zero exit, record kept."""
    home, bin_dir = _sandbox(tmp_path)
    _write_exe(bin_dir / "uv", FAKE_UV_FAILING_UNINSTALL)
    env = _env(home, bin_dir)
    setup = _run([], env)
    assert setup.returncode == 0, _out(setup)

    result = _run(["--uninstall"], env)
    _assert_failed_uninstall_kept_everything(result, home)
    assert "tool uninstall a2m" in (home / ".fake-uv.log").read_text()

    again = _run(["--uninstall"], env)
    _assert_failed_uninstall_kept_everything(again, home)


def test_INSTALL_CP1_X02_pipx_uninstall_failure_keeps_record_then_retry_works(tmp_path: Path) -> None:
    """[INSTALL-CP1-X02] pipx fails to uninstall and cannot list (state unknown): non-zero exit and
    record kept; once pipx works again, a retry removes a2m and the record."""
    home, bin_dir = _sandbox(tmp_path)
    _write_exe(bin_dir / "pipx", FAKE_PIPX)
    env = _env(home, bin_dir)
    env["FAKE_PIPX_PYTHON"] = str(_write_exe(tmp_path / "python3.12-ok", FAKE_PYTHON_OK))
    setup = _run([], env)
    assert setup.returncode == 0, _out(setup)

    result = _run(["--uninstall"], env)
    _assert_failed_uninstall_kept_everything(result, home)
    assert "uninstall a2m" in (home / ".fake-pipx.log").read_text()

    (home / ".pipx-works").write_text("")
    retry = _run(["--uninstall"], env)
    assert retry.returncode == 0, _out(retry)
    assert not _record(home).exists(), "a successful retry removes the record"
    assert not (home / ".local" / "bin" / "a2m").exists()


def test_INSTALL_CP1_X03_uv_failure_with_positively_absent_a2m_counts_as_uninstalled(tmp_path: Path) -> None:
    """[INSTALL-CP1-X03] uv's uninstall fails but uv lists no a2m and the recorded command is gone:
    already uninstalled, so exit 0 and the record is removed."""
    home, bin_dir = _sandbox(tmp_path)
    _write_exe(bin_dir / "uv", FAKE_UV_FAILING_UNINSTALL)
    env = _env(home, bin_dir)
    setup = _run([], env)
    assert setup.returncode == 0, _out(setup)
    (home / ".local" / "bin" / "a2m").unlink()

    result = _run(["--uninstall"], env)
    assert result.returncode == 0, _out(result)
    assert not _record(home).exists(), "the record goes once absence is confirmed"


def test_INSTALL_CP1_X04_pipx_with_only_old_python_stops_with_one_prerequisite_line(tmp_path: Path) -> None:
    """[INSTALL-CP1-X04] pipx with only Python 3.10 (on PATH and as pipx's default): one clear line
    naming Python 3.11+ and uv, non-zero exit, and pipx install is never called."""
    home, bin_dir = _sandbox(tmp_path)
    _write_exe(bin_dir / "pipx", FAKE_PIPX)
    old = _write_exe(bin_dir / "python3.10", FAKE_PYTHON_OLD)
    _write_exe(bin_dir / "python3", FAKE_PYTHON_OLD)
    env = _env(home, bin_dir)
    env["FAKE_PIPX_PYTHON"] = str(old)

    result = _run([], env)
    output = _out(result)
    assert result.returncode != 0, output
    lines = [line for line in output.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one line of output:\n{output}"
    assert "3.11" in lines[0] and "uv" in lines[0], lines[0]
    log = home / ".fake-pipx.log"
    calls = log.read_text().splitlines() if log.exists() else []
    assert not [c for c in calls if c.startswith("install")], f"pipx install must not run:\n{calls}"
    assert not _record(home).exists()
    assert not (home / ".local" / "bin" / "a2m").exists()


# A fake Python 3.12 for the private-venv path: `-m venv DIR` makes a fake venv (pyvenv.cfg, a copy
# of this script as bin/python, a stub bin/a2m, a lib folder); everything else, pip included, passes.
FAKE_PYTHON_VENV = """#!/usr/bin/env bash
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "venv" ]; then
    mkdir -p "$3/bin" "$3/lib/site-packages"
    printf 'home = /fake\\n' > "$3/pyvenv.cfg"
    cat "$0" > "$3/bin/python"
    chmod +x "$3/bin/python"
    printf '#!/usr/bin/env bash\\necho "a2m 0.1.0"\\n' > "$3/bin/a2m"
    chmod +x "$3/bin/a2m"
    printf 'x\\n' > "$3/lib/site-packages/a2m.pth"
fi
exit 0
"""


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores directory permissions")
def test_INSTALL_CP1_X05_venv_removal_failure_keeps_record_then_retry_works(tmp_path: Path) -> None:
    """[INSTALL-CP1-X05] the private venv cannot be fully removed (a read-only folder inside it):
    non-zero exit, record kept, no success line; once the folder is writable a retry removes it all."""
    home, bin_dir = _sandbox(tmp_path)
    _write_exe(bin_dir / "python3", FAKE_PYTHON_VENV)
    env = _env(home, bin_dir)
    setup = _run([], env)
    assert setup.returncode == 0, _out(setup)
    venv = home / ".local" / "share" / "a2m" / "venv"
    assert (venv / "pyvenv.cfg").exists(), _out(setup)
    assert (home / ".local" / "bin" / "a2m").is_symlink(), _out(setup)

    locked = venv / "lib" / "site-packages"
    locked.chmod(0o555)
    try:
        result = _run(["--uninstall"], env)
        output = _out(result)
        assert result.returncode != 0, f"a failed venv removal must exit non-zero:\n{output}"
        assert _record(home).exists(), "the install record must be kept so the uninstall can be retried"
        assert venv.exists(), "the venv really is still there"
        assert "is uninstalled" not in output.lower(), f"must not claim success:\n{output}"
    finally:
        locked.chmod(0o755)

    retry = _run(["--uninstall"], env)
    output = _out(retry)
    assert retry.returncode == 0, output
    assert "is uninstalled" in output.lower(), output
    assert not venv.exists(), f"a successful retry removes the whole venv:\n{output}"
    assert not _record(home).exists(), "a successful retry removes the record"
    assert not (home / ".local" / "bin" / "a2m").exists()
    assert not (home / ".local" / "bin" / "a2m").is_symlink()


def _printed_path_line(output: str, marker: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip().startswith(marker)]
    assert len(lines) == 1, f"expected one printed {marker!r} line:\n{output}"
    return lines[0]


def test_INSTALL_CP1_X06_printed_path_line_works_for_a_folder_with_spaces_and_quotes(tmp_path: Path) -> None:
    """[INSTALL-CP1-X06] the command folder (here a pipx bin dir) holds a space, a single quote, a
    double quote, a $ and a backslash: the printed bash line, run in a real bash, puts the whole folder
    first on PATH and keeps the old PATH; the printed fish line does the same in fish when installed."""
    home, bin_dir = _sandbox(tmp_path)
    _write_exe(bin_dir / "pipx", FAKE_PIPX)
    _write_exe(bin_dir / "python3", FAKE_PYTHON_OK)
    odd_bin = tmp_path / "Application Support" / 'it\'s "a2m" $HOME \\bin'
    env = _env(home, bin_dir)
    env["PIPX_BIN_DIR"] = str(odd_bin)

    result = _run([], env)
    output = _out(result)
    assert result.returncode == 0, output
    assert (odd_bin / "a2m").exists(), output
    line = _printed_path_line(output, "export PATH=")
    bash = shutil.which("bash")
    assert bash is not None
    shown = subprocess.run(
        [bash, "--norc", "--noprofile", "-c", line + '\nprintf "%s" "$PATH"'],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SEC,
        check=False,
    )
    assert shown.returncode == 0, shown.stderr
    assert shown.stdout == f"{odd_bin}:/usr/bin:/bin", f"printed line {line!r} gave PATH {shown.stdout!r}"

    fish = shutil.which("fish")
    if fish is None:
        return  # fish is not installed here: the fish half of this case cannot run.
    env["SHELL"] = fish
    result = _run([], env)
    output = _out(result)
    assert result.returncode == 0, output
    line = _printed_path_line(output, "fish_add_path ")
    shown = subprocess.run(
        [fish, "--no-config", "-c", line + "\nprintf '%s\\n' $PATH"],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SEC,
        check=False,
    )
    assert shown.returncode == 0, shown.stderr
    assert str(odd_bin) in shown.stdout.splitlines(), f"printed line {line!r} gave PATH {shown.stdout!r}"
