"""CP9 fix checks (adversarial round 1): a golden run's report never claims more than the recorded exchanges.

A golden replay compares the recorded exchanges end to end; a2m does not know which policies those exchanges
reached. So a golden REPORT.md must say policy coverage is not measured (and list the generated policy steps)
instead of "every generated policy step had a test", and the README's golden paragraph must say it does not prove
anything about policies the recordings never reached. Runs go through ``a2m.cli.main`` (``--llm fake``) with the
verification stage's runner swapped for :class:`e2e_support.GoldenRunner` or :class:`e2e_support.OracleRunner`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from e2e_support import (
    EM_DASH,
    README,
    GoldenRunner,
    OracleRunner,
    bucket_of,
    input_with,
    migrate,
    normalize_space,
    read_report,
    section,
    stages_with,
    unpacked,
    verification_type,
)

MISSING_KEY_BODY = json.dumps(
    {
        "fault": {
            "faultstring": "Failed to resolve API Key variable request.header.x-api-key",
            "detail": {"errorcode": "steps.oauth.v2.FailedToResolveAPIKey"},
        }
    }
)
EVERY_STEP_TESTED = re.compile(r"(?i)every\s+(generated\s+)?policy(\s+step)?\s+had\s+a\s+test")


def _partial_golden(root: Path) -> GoldenRunner:
    """One recorded catalog-api exchange that only reaches VK-Key: no API key, answered 401 before AM-Tag runs and
    before the backend is called. The runner's app answers it exactly as recorded."""
    folder = root / "catalog-api"
    folder.mkdir(parents=True)
    exchange = {
        "name": "missing-key",
        "calls": [
            {
                "after_ms": 0,
                "request": {"method": "GET", "path": "/catalog/items", "headers": {}, "body": ""},
                "response": {"status": 401, "headers": {"Content-Type": "application/json"}, "body": MISSING_KEY_BODY},
            }
        ],
        "backend_calls": [],
    }
    (folder / "01-missing-key.json").write_text(json.dumps(exchange, indent=2), encoding="utf-8")
    return GoldenRunner({("GET", "/catalog/items"): (401, {"Content-Type": "application/json"}, MISSING_KEY_BODY.encode())})


def test_CP9_X01_a_golden_report_says_policy_coverage_is_not_measured(tmp_path: Path) -> None:
    """[CP9-X01] catalog-api with --golden and one recording that only reaches VK-Key (AM-Tag never runs): the
    proxy stays verified labelled golden (the bucket rule is unchanged), and REPORT.md never says every policy step
    had a test; its 'Untested policies' section says coverage is not measured for golden runs and lists both
    generated policy steps, VK-Key and AM-Tag, so a person can judge."""
    exports = input_with(tmp_path, "catalog-api")
    runner = _partial_golden(tmp_path / "golden")
    run = migrate(
        exports, tmp_path / "results", "--mock-backends", "--golden", str(tmp_path / "golden"),
        "--max-fix-attempts", "0", stages=stages_with(runner),
    )
    assert run.code == 0, run.err
    assert runner.unmatched == [], runner.unmatched
    assert bucket_of(run.results, "catalog-api") == "verified"
    report = read_report(run.results, "catalog-api")
    assert verification_type(report) == "golden"

    assert not EVERY_STEP_TESTED.search(report), report
    untested = section(report, "Untested policies")
    assert untested is not None, report
    flat = normalize_space(untested)
    assert re.search(r"(?i)not measured", flat), untested
    assert re.search(r"(?i)golden", flat), untested
    for step, kind in (("VK-Key", "VerifyAPIKey"), ("AM-Tag", "AssignMessage")):
        assert re.search(rf"(?m)^- {re.escape(step)} \({kind}\)", untested), (step, untested)
    assert EM_DASH not in report


def test_CP9_X02_a_battery_report_still_says_every_policy_step_had_a_test(tmp_path: Path) -> None:
    """[CP9-X02] catalog-api with every battery case passing: battery does measure coverage, so its 'Untested
    policies' section still says every generated policy step had a test and does not use the golden wording."""
    exports = input_with(tmp_path, "catalog-api")
    runner = OracleRunner({"catalog-api": unpacked(tmp_path / "bundles", "catalog-api")}, "pass")
    run = migrate(exports, tmp_path / "results", "--mock-backends", "--max-fix-attempts", "0", stages=stages_with(runner))
    assert run.code == 0, run.err
    report = read_report(run.results, "catalog-api")
    assert verification_type(report) == "battery"
    untested = section(report, "Untested policies")
    assert untested is not None, report
    assert EVERY_STEP_TESTED.search(untested), untested
    assert not re.search(r"(?i)not measured", untested), untested


def test_CP9_X03_the_readme_says_golden_does_not_prove_policies_the_recordings_never_reached() -> None:
    """[CP9-X03] README.md's golden paragraph says what golden does not prove includes policies the recorded
    requests never reached; no em dash."""
    text = README.read_text(encoding="utf-8")
    golden = next(
        (block for block in re.split(r"\n(?=- \*\*)", text) if block.startswith("- **golden**")), None
    )
    assert golden is not None, "README.md has no '- **golden**' paragraph"
    flat = normalize_space(golden)
    assert re.search(r"(?i)does not prove", flat), flat
    assert re.search(r"(?i)polic(y|ies) the record\w* (requests |exchanges )?never reached", flat), flat
    assert EM_DASH not in text
