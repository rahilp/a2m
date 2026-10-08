"""The Java, Maven and Mule toolchain that ``./install.sh --with-mule`` installs.

The installer writes ``~/.local/share/a2m/toolchain/toolchain.env``: one ``KEY=value`` per line naming
``JAVA_HOME``, ``MAVEN_HOME`` and ``MULE_HOME``. :func:`apply` reads it once at start-up, before any
command runs, so ``a2m migrate``, ``a2m tui`` and the TUI's own child process all find the tools
without a shell file being edited: it sets ``JAVA_HOME``, puts ``$JAVA_HOME/bin`` and ``$MAVEN_HOME/bin``
first on ``PATH``, and sets ``A2M_MULE_HOME`` unless the user already set it (their choice wins).

``A2M_NO_TOOLCHAIN=1`` turns this off. A missing file is ignored without a word; a file that cannot
be read or parsed changes nothing, and :func:`apply` returns one warning line for the caller to print
(cli.main prints it on stderr, as it prints every message).
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from pathlib import Path

DISABLE_VARIABLE = "A2M_NO_TOOLCHAIN"
MULE_VARIABLE = "A2M_MULE_HOME"
REQUIRED_KEYS = ("JAVA_HOME", "MAVEN_HOME", "MULE_HOME")


def toolchain_file(env: MutableMapping[str, str]) -> Path | None:
    """Where the installer writes the toolchain file, under ``HOME``; None when HOME is not set."""
    home = env.get("HOME")
    if not home:
        return None
    return Path(home) / ".local" / "share" / "a2m" / "toolchain" / "toolchain.env"


def _parse(text: str) -> dict[str, str]:
    """``KEY=value`` lines; blank lines and ``#`` comments are skipped. Raises ValueError on anything else."""
    values: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, value = stripped.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"line {number} is not KEY=value")
        values[key.strip()] = value.strip()
    missing = [key for key in REQUIRED_KEYS if not values.get(key)]
    if missing:
        raise ValueError(f"it does not name {', '.join(missing)}")
    return values


def apply(env: MutableMapping[str, str] | None = None) -> str | None:
    """Apply the installed toolchain to ``env`` (default ``os.environ``).

    Returns None, or a one-line warning when the file is there but unusable (``env`` is then unchanged).
    """
    env = os.environ if env is None else env
    if env.get(DISABLE_VARIABLE, "") not in ("", "0"):
        return None
    path = toolchain_file(env)
    try:
        if path is None or not path.is_file():
            return None
        values = _parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return f"a2m: ignoring the toolchain file {path}: {exc}"
    java_home = values["JAVA_HOME"]
    env["JAVA_HOME"] = java_home
    first = [str(Path(java_home) / "bin"), str(Path(values["MAVEN_HOME"]) / "bin")]
    rest = [entry for entry in env.get("PATH", "").split(os.pathsep) if entry and entry not in first]
    env["PATH"] = os.pathsep.join(first + rest)
    if not env.get(MULE_VARIABLE):
        env[MULE_VARIABLE] = values["MULE_HOME"]
    return None
