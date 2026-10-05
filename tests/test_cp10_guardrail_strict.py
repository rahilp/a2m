"""CP10 adversarial round 1: stricter cases for the credential canary guardrail check.

Runs ``tools/checks/credential_canary.py <target>`` as a real subprocess, as
``tests/test_guardrail_credentials.py`` does, and adds what round 1 found
missing there (that file is locked; these cases are new):

* CP10-X01 - every bad example and bad variant is a detection (exit 1 with a
  finding line), never "could not check" (exit 2) passing as one.
* CP10-X02 - exit 2 output never uses the words the locked tests read as a
  detection, so an error cannot pass as one.
* CP10-X03 - a masker that hides nothing is caught: leaks are judged against
  the canary list, not against what the target's own masker can hide.
* CP10-X04 - a record written on a child logger is judged as a handler on the
  root logger gets it, so a masking filter on a parent logger does not hide it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
VENV_PYTHON = REPO / ".venv" / "bin" / "python"
CHECK_SCRIPT = REPO / "tools" / "checks" / "credential_canary.py"
GUARDRAILS_DIR = REPO / "tests" / "fixtures" / "guardrails"
BAD_EXAMPLE = GUARDRAILS_DIR / "credential-leak"
BAD_DIR = GUARDRAILS_DIR / "credential-variants" / "bad"
BAD_TARGETS = [BAD_EXAMPLE, *sorted((p for p in BAD_DIR.iterdir() if p.is_dir()), key=lambda p: p.name)]
# The words tests/test_guardrail_credentials.py reads as naming the problem (its PROBLEM_KEYWORDS).
DETECTION_WORDS = ("credential", "secret", "canary", "mask", "leak", "lookalike", "look-alike")
FINDING_MARKS = ("  - leak: ", "  - over-masked look-alike: ")


def _run_check(target: Path) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if key not in ("FORCE_COLOR", "PY_COLORS")}
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


@pytest.mark.parametrize("target", BAD_TARGETS, ids=lambda p: p.name)
def test_CP10_X01_bad_targets_are_detections_not_errors(target: Path) -> None:
    """[CP10-X01] Every bad example and bad variant exits 1 with at least one finding line, never 2."""
    result = _run_check(target)

    output = result.stdout + result.stderr
    assert result.returncode == 1, f"{target.name}: expected exit 1 (a detection), got {result.returncode}:\n{output}"
    assert any(mark in output for mark in FINDING_MARKS), f"{target.name}: no finding line:\n{output}"
    assert "could not check" not in output, f"{target.name}: part of it could not be checked:\n{output}"


@pytest.mark.parametrize(
    "init_text",
    [
        'raise ImportError("no a2m.verify.masking Masker: secret canary leak look-alike lookalike credential")\n',
        "this is not python\n",
    ],
    ids=["import-error-naming-the-words", "syntax-error"],
)
def test_CP10_X02_cannot_check_output_never_reads_as_a_detection(tmp_path: Path, init_text: str) -> None:
    """[CP10-X02] A target that cannot be checked exits 2 and its output uses none of the detection words."""
    holder = tmp_path / "credential-canary-target"
    (holder / "a2m").mkdir(parents=True)
    (holder / "a2m" / "__init__.py").write_text(init_text, encoding="utf-8")

    result = _run_check(holder)

    output = result.stdout + result.stderr
    assert result.returncode == 2, output
    assert "Traceback (most recent call last)" not in output, output
    found = [word for word in DETECTION_WORDS if word in output.lower()]
    assert found == [], f"exit 2 output uses {found}:\n{output}"


def test_CP10_X02_missing_target_output_never_reads_as_a_detection(tmp_path: Path) -> None:
    """[CP10-X02] A target that does not exist exits 2 and its output uses none of the detection words."""
    result = _run_check(tmp_path / "credential-canary-missing")

    output = result.stdout + result.stderr
    assert result.returncode == 2, output
    found = [word for word in DETECTION_WORDS if word in output.lower()]
    assert found == [], f"exit 2 output uses {found}:\n{output}"


def test_CP10_X03_a_masker_that_hides_nothing_is_caught() -> None:
    """[CP10-X03] bad/identity-masker: every planted credential is named, though its masker hides none of them."""
    result = _run_check(BAD_DIR / "identity-masker")

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    for key in ("b-policy-header", "b-policy-query", "b-policy-json", "b-mule", "b-diff-header", "b-diff-bearer"):
        assert f"canary {key} " in output, f"{key} not reported:\n{output}"


def test_CP10_X04_a_child_logger_record_is_judged_after_propagation() -> None:
    """[CP10-X04] bad/log-filter-on-root-only: the token logged on 'a2m.verify' is reported as reaching run.log."""
    result = _run_check(BAD_DIR / "log-filter-on-root-only")

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "canary b-token " in output and "'a2m.verify'" in output, output
