"""``./install.sh --with-mule``: the Java, Maven and Mule toolchain, from a local file:// mirror.

Same sandbox as tests/test_install.py: the real ``install.sh`` runs under a temporary ``HOME`` with a
``PATH`` of symlinks to the system tools it needs plus a fake ``uv``. The download base URLs point
at a ``file://`` mirror under tmp_path holding tiny fake tarballs (one top-level folder with the
expected program) and checksum files with their real SHA-256/SHA-512 digests, laid out and named
like the real download hosts. The network is never used and --skip-check skips the self-check.

Case IDs:

* INSTALL-MULE-T01 - installs jdk, maven and mule under ~/.local/share/a2m/toolchain, writes
  toolchain.env and records the toolchain; a rerun downloads nothing (the mirror is gone) and
  reports each piece as already installed.
* INSTALL-MULE-T02 - a JDK whose checksum does not match stops with a non-zero exit before
  unpacking: no piece, no toolchain.env and no download folder is left.
* INSTALL-MULE-T03 - --uninstall removes the toolchain, the record and the state folder, and
  nothing else.
* INSTALL-MULE-T04 - with neither uv nor Python, uv is installed with its installer
  (UV_NO_MODIFY_PATH=1) into ~/.local/bin, the PATH advice is still printed, and a later
  --uninstall from a shell without ~/.local/bin on PATH still finds that uv and removes it.
"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO / "install.sh"
SYSTEM_TOOLS = (
    "bash", "env", "mkdir", "ln", "rm", "readlink", "dirname", "cat", "grep", "sed", "uname", "chmod",
    "curl", "tar", "gzip", "sha256sum", "sha512sum", "mktemp", "mv", "tr", "sh",
)
TIMEOUT_SEC = 60

FAKE_UV = """#!/usr/bin/env bash
set -euo pipefail
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

# Fake uv installer: refuses to run unless told not to touch shell files, then puts the fake uv
# (and a uvx) into $UV_INSTALL_DIR, as the real installer does.
FAKE_UV_INSTALLER = """#!/bin/sh
set -eu
[ "${UV_NO_MODIFY_PATH:-}" = 1 ] || { echo "would edit shell files" >&2; exit 1; }
mkdir -p "$UV_INSTALL_DIR"
cat > "$UV_INSTALL_DIR/uv" <<'UVEOF'
""" + FAKE_UV + """UVEOF
chmod +x "$UV_INSTALL_DIR/uv"
printf '#!/bin/sh\\n' > "$UV_INSTALL_DIR/uvx"
chmod +x "$UV_INSTALL_DIR/uvx"
"""

JDK_VERSION = "17.0.20.1+1"
MAVEN_VERSION = "3.9.16"
MULE_VERSION = "4.9.0"


def _jdk_platform() -> str:
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "aarch64", "arm64": "aarch64"}[platform.machine().lower()]
    return f"{arch}_{'mac' if platform.system() == 'Darwin' else 'linux'}"


def _tarball(top: str, program: str) -> bytes:
    """A .tar.gz holding one folder ``top`` with an executable ``top/program``."""
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as tar:
        script = b"#!/bin/sh\necho fake\n"
        info = tarfile.TarInfo(f"{top}/{program}")
        info.size, info.mode = len(script), 0o755
        tar.addfile(info, io.BytesIO(script))
        notice = b"fake\n"
        info = tarfile.TarInfo(f"{top}/NOTICE")
        info.size = len(notice)
        tar.addfile(info, io.BytesIO(notice))
    return data.getvalue()


def _mirror(tmp_path: Path) -> tuple[dict[str, str], dict[str, Path]]:
    """Writes the fake mirror; returns the base-URL environment and each piece's checksum file."""
    root = tmp_path / "mirror"
    root.mkdir()
    jdk = f"OpenJDK17U-jdk_{_jdk_platform()}_hotspot_17.0.20.1_1.tar.gz"
    maven = f"apache-maven-{MAVEN_VERSION}-bin.tar.gz"
    mule = f"mule-standalone-{MULE_VERSION}.tar.gz"
    pieces = {
        "jdk": (jdk, _tarball(f"jdk-{JDK_VERSION}", "bin/java"), ".sha256.txt", hashlib.sha256, True),
        "maven": (maven, _tarball(f"apache-maven-{MAVEN_VERSION}", "bin/mvn"), ".sha512", hashlib.sha512, False),
        "mule": (mule, _tarball(f"mule-standalone-{MULE_VERSION}", "bin/mule"), ".sha256", hashlib.sha256, False),
    }
    sums: dict[str, Path] = {}
    for key, (name, blob, suffix, algo, with_name) in pieces.items():
        (root / name).write_bytes(blob)
        digest = algo(blob).hexdigest()
        sums[key] = root / (name + suffix)
        sums[key].write_text(f"{digest}  {name}\n" if with_name else f"{digest}\n")
    url = root.as_uri()
    env = {"A2M_JDK_BASE_URL": url, "A2M_MAVEN_BASE_URL": url, "A2M_MULE_BASE_URL": url}
    return env, sums


def _sandbox(tmp_path: Path, *, with_uv: bool = True) -> tuple[Path, dict[str, str], dict[str, Path]]:
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "sandbox-bin"
    bin_dir.mkdir()
    for tool in SYSTEM_TOOLS:
        real = shutil.which(tool)
        if real is None:
            pytest.fail(f"required system tool {tool!r} was not found on this machine's PATH")
        (bin_dir / tool).symlink_to(real)
    if with_uv:
        (bin_dir / "uv").write_text(FAKE_UV)
        (bin_dir / "uv").chmod(0o755)
    env = dict(os.environ)
    for key in list(env):
        if key == "VIRTUAL_ENV" or key.startswith(("MISE", "UV_", "XDG_", "PIPX_", "A2M_")):
            env.pop(key, None)
    env.update(HOME=str(home), SHELL="/bin/bash", PATH=f"{bin_dir}:{home / '.local' / 'bin'}")
    mirror_env, sums = _mirror(tmp_path)
    env.update(mirror_env)
    return home, env, sums


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(INSTALL_SH), *args], cwd=REPO, env=env, capture_output=True, text=True, timeout=TIMEOUT_SEC, check=False
    )


def _out(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


def _toolchain(home: Path) -> Path:
    return home / ".local" / "share" / "a2m" / "toolchain"


def test_INSTALL_MULE_T01_installs_toolchain_writes_env_and_rerun_is_idempotent(tmp_path: Path) -> None:
    """[INSTALL-MULE-T01] Pieces, toolchain.env and the record; a rerun downloads nothing."""
    home, env, _ = _sandbox(tmp_path)
    first = _run(["--with-mule", "--skip-check"], env)
    assert first.returncode == 0, _out(first)

    toolchain = _toolchain(home)
    for piece, program, version in (
        ("jdk", "bin/java", JDK_VERSION), ("maven", "bin/mvn", MAVEN_VERSION), ("mule", "bin/mule", MULE_VERSION)
    ):
        assert os.access(toolchain / piece / program, os.X_OK), f"{piece}/{program} missing:\n{_out(first)}"
        assert (toolchain / piece / ".a2m-installed").read_text().strip() == version
    values = dict(
        line.split("=", 1) for line in (toolchain / "toolchain.env").read_text().splitlines() if "=" in line
    )
    assert values["JAVA_HOME"] == str(toolchain / "jdk")
    assert values["MAVEN_HOME"] == str(toolchain / "maven")
    assert values["MULE_HOME"] == str(toolchain / "mule")
    assert values["MULE_VERSION"] == MULE_VERSION
    record = (home / ".local" / "share" / "a2m" / "install-record").read_text()
    assert f"toolchain={toolchain}\n" in record, record
    assert not list(toolchain.glob(".download.*")), "the download folder must be cleaned up"
    assert not (home / ".bashrc").exists() and not (home / ".profile").exists(), "no shell file may be written"

    shutil.rmtree(tmp_path / "mirror")  # a rerun that tried to download anything would now fail
    second = _run(["--with-mule", "--skip-check"], env)
    assert second.returncode == 0, _out(second)
    assert _out(second).count("is already installed") == 3, _out(second)
    assert "Downloading" not in _out(second)

    # A plain rerun (no --with-mule) keeps the toolchain in the record, so --uninstall still knows it.
    third = _run([], env)
    assert third.returncode == 0, _out(third)
    assert "toolchain=" in (home / ".local" / "share" / "a2m" / "install-record").read_text()


def test_INSTALL_MULE_T02_checksum_mismatch_stops_before_unpacking(tmp_path: Path) -> None:
    """[INSTALL-MULE-T02] A wrong JDK checksum: non-zero exit, nothing unpacked or installed."""
    home, env, sums = _sandbox(tmp_path)
    sums["jdk"].write_text("0" * 64 + "  wrong.tar.gz\n")

    result = _run(["--with-mule", "--skip-check"], env)
    output = _out(result)
    assert result.returncode != 0, output
    assert "does not match" in output and "checksum" in output, output

    toolchain = _toolchain(home)
    leftovers = sorted(p.relative_to(toolchain).as_posix() for p in toolchain.rglob("*")) if toolchain.exists() else []
    assert leftovers == [], f"nothing may be left in the toolchain folder: {leftovers}"


def test_INSTALL_MULE_T03_uninstall_removes_the_toolchain(tmp_path: Path) -> None:
    """[INSTALL-MULE-T03] --uninstall removes the toolchain, the record and the state folder only."""
    home, env, _ = _sandbox(tmp_path)
    setup = _run(["--with-mule", "--skip-check"], env)
    assert setup.returncode == 0, _out(setup)
    unrelated = home / ".local" / "share" / "other-app" / "keep.txt"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("keep\n")

    result = _run(["--uninstall"], env)
    assert result.returncode == 0, _out(result)
    assert not (home / ".local" / "share" / "a2m").exists(), _out(result)
    assert not (home / ".local" / "bin" / "a2m").exists()
    assert unrelated.read_text() == "keep\n"


def test_INSTALL_MULE_T04_bootstraps_uv_and_uninstall_finds_it_off_path(tmp_path: Path) -> None:
    """[INSTALL-MULE-T04] No uv and no Python: uv is installed without shell edits; uninstall finds it."""
    home, env, _ = _sandbox(tmp_path, with_uv=False)
    env["PATH"] = str(tmp_path / "sandbox-bin")  # no ~/.local/bin, no Python, no uv
    installer = tmp_path / "uv-install.sh"
    installer.write_text(FAKE_UV_INSTALLER)
    env["A2M_UV_INSTALLER_URL"] = installer.as_uri()

    result = _run(["--with-mule", "--skip-check"], env)
    output = _out(result)
    assert result.returncode == 0, output
    local_bin = home / ".local" / "bin"
    assert os.access(local_bin / "uv", os.X_OK) and os.access(local_bin / "a2m", os.X_OK), output
    assert "uv_installed=1" in (home / ".local" / "share" / "a2m" / "install-record").read_text()
    assert f'export PATH="{local_bin}:$PATH"' in output, f"PATH advice must still be printed:\n{output}"
    assert not (home / ".bashrc").exists() and not (home / ".profile").exists()

    removed = _run(["--uninstall"], env)
    assert removed.returncode == 0, _out(removed)
    assert not (home / ".local" / "share" / "a2m").exists(), _out(removed)
    assert sorted(p.name for p in local_bin.iterdir()) == [], _out(removed)
