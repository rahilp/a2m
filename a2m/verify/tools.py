"""Find Java, Maven and the Mule runtime; what is missing makes verification a clean skip, never a failure.

Java and Maven are found on PATH. The Mule runtime is the folder A2M_MULE_HOME
names, else MULE_HOME (A2M_MULE_HOME wins when set); it counts only when it is
a Mule 4 standalone install a2m can start: its ``lib/boot`` holds the jar of the
``org.mule.boot`` module (``mule-module-reboot-<version>.jar``), which a2m runs
directly on Java (see :func:`a2m.verify.mule.jvm_command`; ``bin/mule`` is not used).
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

MULE_HOME_VARIABLES = ("A2M_MULE_HOME", "MULE_HOME")
# The org.mule.boot module's jar in a Mule 4 install's lib/boot: the one a2m starts Mule from.
MULE_BOOT_JARS = "mule-module-reboot-*.jar"
JAVA, MAVEN, MULE = "Java", "Maven", "Mule"
HINTS = {
    JAVA: "no java on PATH",
    MAVEN: "no mvn on PATH",
    MULE: "set A2M_MULE_HOME or MULE_HOME to a Mule 4 standalone folder (one with lib/boot/mule-module-reboot-*.jar)",
}


@dataclass(frozen=True, slots=True)
class ToolStatus:
    """``missing`` names each missing tool ("Java", "Maven", "Mule"), in that order."""

    missing: tuple[str, ...]
    mule_home: Path | None
    java: Path | None = None
    mvn: Path | None = None

    @property
    def ready(self) -> bool:
        return not self.missing


def mule_home_setting() -> str | None:
    """The Mule install folder setting: A2M_MULE_HOME when set, else MULE_HOME, else None."""
    for name in MULE_HOME_VARIABLES:
        value = os.environ.get(name)
        if value:
            return value
    return None


def find_mule_home() -> Path | None:
    """The configured Mule install folder (made absolute), only when its lib/boot holds the org.mule.boot jar."""
    value = mule_home_setting()
    if value is None:
        return None
    home = Path(value).absolute()
    try:
        found = any(jar.is_file() for jar in (home / "lib" / "boot").glob(MULE_BOOT_JARS))
    except OSError:
        return None
    return home if found else None


def detect_tools() -> ToolStatus:
    java = shutil.which("java")
    mvn = shutil.which("mvn")
    mule_home = find_mule_home()
    missing = tuple(
        name for name, found in ((JAVA, java), (MAVEN, mvn), (MULE, mule_home)) if found is None
    )
    return ToolStatus(
        missing=missing,
        mule_home=mule_home,
        java=Path(java) if java else None,
        mvn=Path(mvn) if mvn else None,
    )


def missing_message(status: ToolStatus) -> str:
    """One line saying the run is skipped because these tools are not installed, naming each one."""
    names = list(status.missing)
    joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    verb = "is" if len(names) == 1 else "are"
    hints = "; ".join(HINTS[name] for name in names)
    return f"build and run skipped: {joined} {verb} not installed ({hints})"
