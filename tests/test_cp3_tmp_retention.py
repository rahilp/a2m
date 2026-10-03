"""CP3 adversarial round 3: pytest's tmp folders do not pile up (CP3-T44, CP3-T45).

Runtime tests leave about 1 GB per session under pytest's tmp folder and /tmp has a
small per-user quota. These cases run a tiny pytest session in a subprocess with the
repo's own pytest settings (pyproject.toml) and a private temp root, shaped like the
runtime tests: a session-scoped base from ``tmp_path_factory.mktemp("mule-base")`` and
a per-test ``tmp_path`` base. They check what is left on disk once each session ends.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
VENV_PYTHON = REPO / ".venv" / "bin" / "python"
STRIPPED_ENV = ("PYTEST_ADDOPTS", "FORCE_COLOR", "PY_COLORS", "PYTEST_DEBUG_TEMPROOT")

CHILD_TEST = """
import os

import pytest


@pytest.fixture(scope="session")
def mule_base(tmp_path_factory):
    base = tmp_path_factory.mktemp("mule-base")
    (base / "apps").mkdir()
    (base / "apps" / "app.jar").write_bytes(b"x" * 65536)
    return base


def test_runtime_like(mule_base, tmp_path):
    (tmp_path / "mule-base-2").mkdir()
    (tmp_path / "mule-base-2" / "mule.log").write_text("up and kicking", encoding="utf-8")
    assert os.environ.get("CHILD_FAIL") != "1", "this session is meant to fail"
"""


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "project"
    project.mkdir()
    (project / "test_child.py").write_text(CHILD_TEST, encoding="utf-8")
    temproot = tmp_path / "temproot"
    temproot.mkdir()
    return project, temproot


def _run_child(project: Path, temproot: Path, *, fail: bool) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if key not in STRIPPED_ENV}
    env["PYTEST_DEBUG_TEMPROOT"] = str(temproot)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["CHILD_FAIL"] = "1" if fail else "0"
    cmd = [
        str(VENV_PYTHON),
        "-m",
        "pytest",
        "-c",
        str(REPO / "pyproject.toml"),
        f"--rootdir={project}",
        f"--confcutdir={project}",
        "-p",
        "no:cacheprovider",
        "--color=no",
        "-q",
        str(project / "test_child.py"),
    ]
    return subprocess.run(cmd, cwd=project, env=env, capture_output=True, text=True, timeout=120, check=False)


def _session_dirs(temproot: Path) -> list[Path]:
    """Every per-session tmp folder (pytest-N) pytest left under the temp root."""
    return sorted(p for p in temproot.glob("pytest-of-*/pytest-*") if p.is_dir() and not p.is_symlink())


def _leftover_files(temproot: Path) -> list[str]:
    return sorted(p.relative_to(temproot).as_posix() for p in temproot.rglob("*") if p.is_file())


def test_CP3_T44_a_passing_session_leaves_no_tmp_folder_behind(tmp_path: Path) -> None:
    """[CP3-T44] After a passing session nothing it wrote stays in pytest's tmp folder, session-scoped bases included."""
    project, temproot = _setup(tmp_path)

    for attempt in (1, 2):
        run = _run_child(project, temproot, fail=False)
        assert run.returncode == 0, run.stdout + run.stderr
        assert _session_dirs(temproot) == [], f"run {attempt} left tmp folders behind"
        assert _leftover_files(temproot) == [], f"run {attempt} left files behind"


def test_CP3_T45_failing_sessions_keep_only_the_latest_failed_run(tmp_path: Path) -> None:
    """[CP3-T45] Failing sessions keep their tmp folder for debugging, but never more than one run's worth."""
    project, temproot = _setup(tmp_path)

    for attempt in (1, 2, 3):
        run = _run_child(project, temproot, fail=True)
        assert run.returncode == 1, run.stdout + run.stderr
        kept = _session_dirs(temproot)
        assert len(kept) == 1, f"failing run {attempt} kept {[p.name for p in kept]}"
        assert list(kept[0].glob("mule-base*/apps/app.jar")), "the failed run's own base was not kept for debugging"

    run = _run_child(project, temproot, fail=False)
    assert run.returncode == 0, run.stdout + run.stderr
    assert len(_session_dirs(temproot)) <= 1, [p.name for p in _session_dirs(temproot)]
