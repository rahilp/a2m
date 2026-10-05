"""CP10: the credential canary guardrail check.

Runs ``tools/checks/credential_canary.py <target>`` as a real subprocess (the
way Ratchet's guardrail gate runs it) against: the deliberately bad example
(``tests/fixtures/guardrails/credential-leak``), every disguised variant
under ``tests/fixtures/guardrails/credential-variants/{bad,good}``, and the
real project package (``a2m``). The check does not exist yet, so every case
here fails now because the script (and, for CP10-T01's main case, the bad
example fixture) is missing, not because of a broken test harness.

Case IDs:

* CP10-T01 - the check fails on the deliberately bad example and names the
  problem; it also fails on every disguised bad variant and passes on every
  look-alike good variant.
* CP10-T02 - the check passes on the real project (``a2m``) and never
  modifies any file it looked at.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
VENV_PYTHON = REPO / ".venv" / "bin" / "python"
CHECK_SCRIPT = REPO / "tools" / "checks" / "credential_canary.py"

GUARDRAILS_DIR = REPO / "tests" / "fixtures" / "guardrails"
BAD_EXAMPLE = GUARDRAILS_DIR / "credential-leak"
VARIANTS_DIR = GUARDRAILS_DIR / "credential-variants"

STRIPPED_ENV = ("FORCE_COLOR", "PY_COLORS")

# Domain vocabulary a useful failure message must touch: either the leak
# itself (credential/secret/canary reaching somewhere unmasked) or the
# over-masking of a harmless look-alike. Not exact wording, just the subject.
PROBLEM_KEYWORDS = ("credential", "secret", "canary", "mask", "leak", "lookalike", "look-alike")


def _bad_variants() -> list[Path]:
    root = VARIANTS_DIR / "bad"
    if not root.is_dir():
        return []
    return sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name)


def _good_variants() -> list[Path]:
    root = VARIANTS_DIR / "good"
    if not root.is_dir():
        return []
    return sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name)


BAD_VARIANTS = _bad_variants()
GOOD_VARIANTS = _good_variants()


def _run_check(target: Path) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if key not in STRIPPED_ENV}
    env.pop("ANTHROPIC_API_KEY", None)
    return subprocess.run(
        [str(VENV_PYTHON), str(CHECK_SCRIPT), str(target)],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _combined(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


def _names_the_problem(output: str) -> bool:
    low = output.lower()
    return any(word in low for word in PROBLEM_KEYWORDS)


def _assert_no_traceback(output: str) -> None:
    assert "Traceback (most recent call last)" not in output, output


def _snapshot(root: Path) -> dict[str, str]:
    """sha256 of every file's bytes under root, keyed by its path relative to root.

    Walks the real filesystem (not git), so it covers tracked and untracked
    files alike. Bytecode caches are excluded: they can appear or disappear
    from unrelated imports during the test run and are not source the check
    is supposed to leave alone.
    """
    digest: dict[str, str] = {}
    if not root.is_dir():
        return digest
    for path in sorted(root.rglob("*")):
        if path.is_dir() or path.is_symlink():
            continue
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        rel = str(path.relative_to(root))
        digest[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest


def _snapshot_working_tree() -> dict[str, dict[str, str]]:
    return {
        "a2m": _snapshot(REPO / "a2m"),
        "tests/fixtures": _snapshot(REPO / "tests" / "fixtures"),
    }


# ---------------------------------------------------------------- CP10-T01


def test_CP10_T01_bad_example_credential_leak_fails_and_names_the_problem() -> None:
    """[CP10-T01] The check fails on tests/fixtures/guardrails/credential-leak and names the problem."""
    result = _run_check(BAD_EXAMPLE)

    output = _combined(result)
    assert result.returncode == 1, f"expected exit 1 (leak detected), got {result.returncode}:\n{output}"
    _assert_no_traceback(output)
    assert _names_the_problem(output), f"check failure does not name the credential problem:\n{output}"


@pytest.mark.skipif(not BAD_VARIANTS, reason="no bad variants found under credential-variants/bad")
@pytest.mark.parametrize("variant", BAD_VARIANTS, ids=lambda p: p.name)
def test_CP10_T01_bad_variant_is_caught(variant: Path) -> None:
    """[CP10-T01] Every disguised bad variant under credential-variants/bad is caught."""
    before = _snapshot(variant)

    result = _run_check(variant)

    output = _combined(result)
    assert result.returncode == 1, f"{variant.name}: expected exit 1 (leak detected), got {result.returncode}:\n{output}"
    _assert_no_traceback(output)
    assert _names_the_problem(output), f"{variant.name}: failure does not name the credential problem:\n{output}"
    assert _snapshot(variant) == before, f"{variant.name}: the check modified files under the fixture"


@pytest.mark.skipif(not GOOD_VARIANTS, reason="no good variants found under credential-variants/good")
@pytest.mark.parametrize("variant", GOOD_VARIANTS, ids=lambda p: p.name)
def test_CP10_T01_good_variant_is_not_flagged(variant: Path) -> None:
    """[CP10-T01] No look-alike good variant under credential-variants/good is flagged."""
    before = _snapshot(variant)

    result = _run_check(variant)

    output = _combined(result)
    assert result.returncode == 0, f"{variant.name}: expected a clean pass, got exit {result.returncode}:\n{output}"
    _assert_no_traceback(output)
    assert _snapshot(variant) == before, f"{variant.name}: the check modified files under the fixture"


# ---------------------------------------------------------------- CP10-T02


def test_CP10_T02_real_project_passes_and_tree_is_unchanged() -> None:
    """[CP10-T02] The check passes on a2m and leaves the working tree unchanged."""
    before = _snapshot_working_tree()

    result = _run_check(REPO / "a2m")

    output = _combined(result)
    assert result.returncode == 0, f"expected a clean pass on a2m, got exit {result.returncode}:\n{output}"
    _assert_no_traceback(output)
    after = _snapshot_working_tree()
    assert after == before, "the check modified files under a2m/ or tests/fixtures while running"
