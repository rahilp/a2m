"""TUI CP5 (adversarial round 1, A3): the setup check says which AI problem it found as a typed code.

The Setup screen picks its one-line Start reason from ``SetupCheck.ai_problem``, never by parsing the message
text, so rewording a2m's Claude messages cannot swap the no-key and no-SDK reasons.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


def test_TUI_CP5_X02_setup_check_reports_the_ai_problem_as_a_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[TUI-CP5-X02] Claude with no key reports NO_KEY, with a key but no SDK reports NO_SDK, with both
    reports none; No AI reports none. Each problem's line is a2m's own message for it."""
    from a2m.ai.claude import MISSING_KEY, MISSING_SDK, SetupProblem
    from a2m.engine import LlmChoice
    from a2m.tui.command import SetupChoices
    from a2m.tui.folders import Status, check_setup

    def check(llm: LlmChoice) -> tuple[SetupProblem | None, Status, str]:
        found = check_setup(SetupChoices(exports=str(tmp_path), results=str(tmp_path / "out"), llm=llm))
        return found.ai_problem, found.ai.status, found.ai.text

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    problem, status, text = check(LlmChoice.CLAUDE)
    assert (problem, status) == (SetupProblem.NO_KEY, Status.INVALID) and MISSING_KEY in text, text

    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setitem(sys.modules, "anthropic", None)  # marked unimportable
    problem, status, text = check(LlmChoice.CLAUDE)
    assert (problem, status) == (SetupProblem.NO_SDK, Status.INVALID) and MISSING_SDK in text, text

    monkeypatch.setitem(sys.modules, "anthropic", type(sys)("anthropic"))
    assert check(LlmChoice.CLAUDE) == (None, Status.NONE, "")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert check(LlmChoice.NONE) == (None, Status.NONE, "")
