"""TUI CP5: ``--llm none`` is a real, honest mode (checkpoint plan: tests/CP5.json in the TUI run folder).

This is the one "code" case of CP5 that is not a Textual Pilot test: it drives the public CLI entry point
``a2m.cli.main`` (through the shared ``run_cli`` fixture in tests/conftest.py, the same technique as
tests/test_cp6_ai.py) with ``--llm none`` on the CP6 JavaScript-callout fixture
(tests/fixtures/apigee/cp6/js-callout), with ``ANTHROPIC_API_KEY`` unset and the ``anthropic`` package made
unimportable (``sys.modules`` patch, same pattern as tests/test_cp6_ai.py's ``test_CP6_T16_...`` tests), to
prove ``--llm none`` never needs a key, the SDK or the network, and that the one item only AI could
translate (the JavaScript callout) is still accounted for: an honest "AI is turned off" skip, never a
silent drop, and the proxy lands in needs-review.

The report is read with tests/e2e_support.py's own table/reader helpers (already used by the CP9 suite), so
this test does not re-implement Markdown table parsing.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

CP6_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "apigee" / "cp6"


def _exports_with_js_callout(tmp_path: Path) -> Path:
    exports = tmp_path / "in"
    shutil.copytree(CP6_FIXTURES / "js-callout", exports / "js-callout")
    return exports


# ---------------------------------------------------------------- TUI-CP5-T01


def test_TUI_CP5_T01_llm_none_is_a_real_honest_mode_that_never_needs_a_key_or_the_ai_sdk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_cli: Any
) -> None:
    """[TUI-CP5-T01] `a2m migrate ... --llm none` completes with no AI call and no missing-key or import
    error, even with ANTHROPIC_API_KEY unset and the anthropic package unimportable: the js-callout proxy's
    JavaScript-callout step is reported as skipped with a reason naming AI being off and --llm none, and the
    proxy lands in needs-review."""
    from e2e_support import bucket_of, item_rows, method_of, policy_table, read_report

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setitem(sys.modules, "anthropic", None)
    exports = _exports_with_js_callout(tmp_path)
    results = tmp_path / "results"

    res = run_cli(["migrate", str(exports), "--out", str(results), "--llm", "none", "--no-runtime"])

    assert res.code == 0, (res.code, res.err)
    assert "Traceback" not in res.err + res.out, res.err + res.out

    assert bucket_of(results, "js-callout") == "needs-review"
    report = read_report(results, "js-callout")
    table = policy_table(report)
    rows = item_rows(table, "JS-AddCorrelation")
    assert len(rows) >= 1, report
    for row in rows:
        assert method_of(row[table.col("method")]) == "skipped", report

        notes = row[table.col("notes", "reason")].lower()
        assert "ai" in notes, report
        assert "off" in notes or "disabled" in notes or "turned off" in notes, report
        assert "--llm none" in notes or "llm none" in notes, report
