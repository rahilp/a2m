"""a2m.toolchain: the toolchain.env written by ``./install.sh --with-mule`` is applied at start-up.

Case IDs:

* TOOLCHAIN-T01 - a valid file sets JAVA_HOME, puts the JDK and Maven bin folders first on PATH
  (once, without duplicates) and sets A2M_MULE_HOME; cli.main applies it before running a command.
* TOOLCHAIN-T02 - an A2M_MULE_HOME the user already set wins; A2M_NO_TOOLCHAIN=1 turns it all off.
* TOOLCHAIN-T03 - a missing file changes nothing and gives no warning; a malformed one gives one
  warning line naming the file and changes nothing, and cli.main prints that line on stderr.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from a2m import cli, toolchain


def _write_toolchain(home: Path, text: str) -> Path:
    path = home / ".local" / "share" / "a2m" / "toolchain" / "toolchain.env"
    path.parent.mkdir(parents=True)
    path.write_text(text)
    return path


def _valid(home: Path) -> str:
    base = home / ".local" / "share" / "a2m" / "toolchain"
    return (
        "# written by install.sh\n"
        f"JAVA_HOME={base / 'jdk'}\n"
        f"MAVEN_HOME={base / 'maven'}\n"
        f"MULE_HOME={base / 'mule'}\n"
        "JAVA_VERSION=17.0.20.1+1\n"
    )


def test_TOOLCHAIN_T01_valid_file_sets_java_maven_and_mule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[TOOLCHAIN-T01] JAVA_HOME, PATH (JDK then Maven first, no duplicates) and A2M_MULE_HOME."""
    _write_toolchain(tmp_path, _valid(tmp_path))
    base = tmp_path / ".local" / "share" / "a2m" / "toolchain"
    jdk_bin, maven_bin = str(base / "jdk" / "bin"), str(base / "maven" / "bin")
    env = {"HOME": str(tmp_path), "PATH": os.pathsep.join(["/usr/bin", maven_bin, "/bin"])}

    assert toolchain.apply(env) is None
    assert env["JAVA_HOME"] == str(base / "jdk")
    assert env["PATH"].split(os.pathsep) == [jdk_bin, maven_bin, "/usr/bin", "/bin"]
    assert env["A2M_MULE_HOME"] == str(base / "mule")

    # cli.main applies it to os.environ before running any command.
    monkeypatch.delenv("A2M_NO_TOOLCHAIN")
    monkeypatch.setenv("A2M_MULE_HOME", "")  # recorded, so the value apply() sets is undone after the test
    monkeypatch.delenv("A2M_MULE_HOME")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("JAVA_HOME", "/somewhere/else")
    assert cli.main(["--version"]) == 0
    assert os.environ["JAVA_HOME"] == str(base / "jdk")
    assert os.environ["A2M_MULE_HOME"] == str(base / "mule")
    assert os.environ["PATH"].split(os.pathsep)[:2] == [jdk_bin, maven_bin]


def test_TOOLCHAIN_T02_user_mule_home_wins_and_opt_out(tmp_path: Path) -> None:
    """[TOOLCHAIN-T02] An explicit A2M_MULE_HOME is kept; A2M_NO_TOOLCHAIN=1 leaves env untouched."""
    _write_toolchain(tmp_path, _valid(tmp_path))
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin", "A2M_MULE_HOME": "/opt/my-mule"}
    assert toolchain.apply(env) is None
    assert env["JAVA_HOME"].endswith("jdk")
    assert env["A2M_MULE_HOME"] == "/opt/my-mule"

    off = {"HOME": str(tmp_path), "PATH": "/usr/bin", "A2M_NO_TOOLCHAIN": "1"}
    before = dict(off)
    assert toolchain.apply(off) is None
    assert off == before


def test_TOOLCHAIN_T03_missing_is_silent_and_malformed_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """[TOOLCHAIN-T03] No file: nothing changes, no warning. A bad file: one warning line, nothing changes."""
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin"}
    assert toolchain.apply(env) is None
    assert env == {"HOME": str(tmp_path), "PATH": "/usr/bin"}

    path = _write_toolchain(tmp_path, "JAVA_HOME=/x\nthis line has no equals sign\n")
    warning = toolchain.apply(env)
    assert warning is not None and str(path) in warning and "\n" not in warning, warning
    assert env == {"HOME": str(tmp_path), "PATH": "/usr/bin"}

    path.write_text("JAVA_HOME=/x\n")  # well formed but MAVEN_HOME and MULE_HOME are missing
    warning = toolchain.apply(env)
    assert warning is not None and "MAVEN_HOME" in warning, warning
    assert "JAVA_HOME" not in env

    # cli.main prints the warning as one stderr line and still runs the command.
    monkeypatch.delenv("A2M_NO_TOOLCHAIN")
    monkeypatch.setenv("HOME", str(tmp_path))
    capsys.readouterr()
    assert cli.main(["--version"]) == 0
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and str(path) in err, err
