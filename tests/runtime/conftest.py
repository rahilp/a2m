"""Shared fixtures for the Mule runtime tests (tests marked ``runtime``).

Runtime tests need java and mvn on PATH and a Mule standalone install named by
A2M_MULE_HOME or MULE_HOME (an existing folder). When any is missing, every
test that uses :func:`runtime_tools` (or :func:`mule_runtime`) is skipped with
a reason naming each missing item; with A2M_REQUIRE_RUNTIME=1 it fails
instead, so a checkpoint gate can never pass on skipped runtime proof.

Runner contract (a2m.verify.mule, CP3 plan):

    MuleRunner(mule_home: Path, mule_base: Path)
        .start(timeout: float) -> None      start Mule headless under mule_base
        .pids -> Sequence[int]              every PID the runner started
        .deploy(jar: Path, *, app_name: str, timeout: float) -> None
                                            hot-deploy into <mule_base>/apps as app_name,
                                            return once it started; raise DeployError
        .undeploy(app_name: str, *, timeout: float) -> None
        .stop(timeout: float) -> None       safe to call twice
    package(project_dir: Path, *, timeout: float) -> Path
                                            mvn package; the built *-mule-application.jar
    DeployError(Exception).log_excerpt: str

Nothing here is autouse. Teardown stops Mule through the runner and then kills
only PIDs the runner recorded that are still alive; nothing else is touched.
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

REQUIRE_RUNTIME_ENV = "A2M_REQUIRE_RUNTIME"
START_TIMEOUT = 240.0
STOP_TIMEOUT = 60.0


def free_port() -> int:
    """A TCP port on 127.0.0.1 that is free right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(name="free_port", scope="session")
def free_port_fixture() -> Callable[[], int]:
    return free_port


def mule_home_setting() -> tuple[str, str | None]:
    """(variable name, value) of the Mule install setting; A2M_MULE_HOME wins over MULE_HOME."""
    for name in ("A2M_MULE_HOME", "MULE_HOME"):
        value = os.environ.get(name)
        if value:
            return name, value
    return "MULE_HOME", None


@dataclass(frozen=True)
class RuntimeTools:
    java: Path
    mvn: Path
    mule_home: Path


def find_runtime_tools() -> tuple[RuntimeTools | None, list[str]]:
    """The tools, or None and the list of what is missing (each item names the thing to install or set)."""
    missing: list[str] = []
    java = shutil.which("java")
    mvn = shutil.which("mvn")
    if java is None:
        missing.append("java (not on PATH)")
    if mvn is None:
        missing.append("mvn (not on PATH)")
    variable, value = mule_home_setting()
    if value is None:
        missing.append("MULE_HOME (A2M_MULE_HOME and MULE_HOME are not set)")
    elif not Path(value).is_dir():
        missing.append(f"MULE_HOME ({variable}={value} is not an existing folder)")
    if missing or java is None or mvn is None or value is None:
        return None, missing
    return RuntimeTools(Path(java), Path(mvn), Path(value)), missing


@pytest.fixture(scope="session")
def runtime_tools() -> RuntimeTools:
    tools, missing = find_runtime_tools()
    if tools is None:
        reason = "Mule runtime tests not run, missing: " + "; ".join(missing)
        if os.environ.get(REQUIRE_RUNTIME_ENV) == "1":
            pytest.fail(f"{reason}. {REQUIRE_RUNTIME_ENV}=1 requires the runtime, so this is a failure.", pytrace=False)
        pytest.skip(reason)
    return tools


def snapshot(mule_home: Path) -> dict[str, list[str]]:
    """Relative file lists of $MULE_HOME/apps and $MULE_HOME/logs."""
    found: dict[str, list[str]] = {}
    for part in ("apps", "logs"):
        folder = mule_home / part
        found[part] = sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*")) if folder.is_dir() else []
    return found


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_and_reap(runner: Any) -> None:
    """Stop through the runner, then SIGKILL any recorded PID that is still alive; never touch other processes."""
    try:
        runner.stop(timeout=STOP_TIMEOUT)
    finally:
        for pid in list(getattr(runner, "pids", ())):
            if pid_alive(int(pid)):
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass


@dataclass
class MuleSession:
    runner: Any
    mule_home: Path
    mule_base: Path
    tmp_root: Path
    home_before: dict[str, list[str]]


@pytest.fixture(scope="session")
def mule_runtime(runtime_tools: RuntimeTools, tmp_path_factory: pytest.TempPathFactory) -> Iterator[MuleSession]:
    """One Mule runtime for the session, under a private MULE_BASE in pytest's tmp folder."""
    from a2m.verify.mule import MuleRunner

    mule_base = tmp_path_factory.mktemp("mule-base")
    before = snapshot(runtime_tools.mule_home)
    runner = MuleRunner(mule_home=runtime_tools.mule_home, mule_base=mule_base)
    try:
        runner.start(timeout=START_TIMEOUT)
        yield MuleSession(runner, runtime_tools.mule_home, mule_base, tmp_path_factory.getbasetemp(), before)
    finally:
        stop_and_reap(runner)
