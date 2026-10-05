"""CP9 runtime: the e2e fixture folder on the real local Mule runtime (Mule Kernel CE 4.9.0).

Marked ``runtime``: excluded from a plain ``pytest -q``, skipped with a reason when java, mvn or MULE_HOME is
missing, and failing instead under A2M_REQUIRE_RUNTIME=1 (tests/runtime/conftest.py). Run with::

    A2M_REQUIRE_RUNTIME=1 mise exec -- .venv/bin/python -m pytest -q -m runtime tests/runtime/test_e2e_runtime.py

Everything goes through ``a2m.cli.main`` with the real pipeline (no injected stages or runner): ``a2m migrate
<tests/fixtures/e2e/input> --out <tmp>/results --llm fake --mock-backends`` (A2M_FAKE_LLM_DIR =
tests/fixtures/llm/cp9-low, A2M_MULE_HOME = the runtime_tools Mule folder). One module-scoped run is shared by
CP9-T19 and CP9-T20; CP9-T22 makes its own run with MAVEN_ARGS pointing Maven at an empty offline repository, so
the real ``mvn package`` fails on dependency resolution (Maven 3.9 reads MAVEN_ARGS; a2m passes its environment to
mvn). Assertions are bucket membership, verification types, counts and report rows, never timing text. The
results contract is the one documented at the top of tests/e2e_support.py.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from e2e_support import (
    ALL_PROXIES,
    BUCKETS,
    E2E_INPUT,
    E2E_POLICY_TYPES,
    EXPECTED_ROWS,
    LLM_LOW,
    PROXIES_WITH_PROJECT,
    VERIFICATION_TYPES,
    Run,
    bucket_contents,
    bucket_of,
    count_map,
    input_with,
    item_rows,
    kind_counts,
    listing,
    method_of,
    migrate,
    policy_table,
    read_report,
    read_summary,
    row_text,
    run_log,
    section,
    summary_md_table,
    summary_proxies,
    tables,
    verification_type,
)

from .conftest import RuntimeTools

pytestmark = pytest.mark.runtime


@pytest.fixture(scope="module")
def runtime_e2e(runtime_tools: RuntimeTools, tmp_path_factory: pytest.TempPathFactory) -> Run:
    """The e2e input migrated once on the real runtime: --llm fake --mock-backends, no --no-runtime."""
    base = tmp_path_factory.mktemp("cp9-runtime-e2e")
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("A2M_MULE_HOME", str(runtime_tools.mule_home))
        patch.delenv("MAVEN_ARGS", raising=False)
        patch.delenv("ANTHROPIC_API_KEY", raising=False)
        return migrate(E2E_INPUT, base / "results", "--mock-backends", llm_dir=LLM_LOW)


def _question(report: str) -> str:
    question = section(report, "Question for a human")
    assert question is not None, f"REPORT.md has no 'Question for a human':\n{report}"
    return question


def _case_lines(report: str) -> list[str]:
    results = section(report, "Test results")
    assert results is not None, report
    found = tables(results)
    if found:
        return [row_text(row) for table in found for row in table.rows]
    return [line for line in results.splitlines() if re.match(r"\s*[-*]\s+\S", line)]


def test_CP9_T19_on_the_real_runtime_a_clean_proxy_is_verified_as_battery_and_the_rest_land_where_they_belong(
    runtime_e2e: Run,
) -> None:
    """[CP9-T19] Exit 0; catalog-api in verified/ with mule-app/ and REPORT.md, labelled battery, at least one case
    (a missing-API-key case among them), every case passed, no question; every verified proxy is battery or golden;
    weather-api in needs-review asking about SA-Limit as a time-window policy; legacy-auth, Test-API, js-transform in
    needs-review for their skipped / ai rows; broken-proxy unsupported with only REPORT.md; no GetSharedFlow folder;
    per-kind row counts equal the CP9-T06 literals; run.log never says catalog-api's build was skipped."""
    results = runtime_e2e.results
    assert runtime_e2e.code == 0, runtime_e2e.err + run_log(results)[-4000:]

    assert bucket_of(results, "catalog-api") == "verified", listing(results)
    catalog = results / "verified" / "catalog-api"
    assert (catalog / "mule-app").is_dir() and (catalog / "REPORT.md").is_file()
    report = read_report(results, "catalog-api")
    assert verification_type(report) == "battery", report
    cases = _case_lines(report)
    assert cases, report
    assert any(re.search(r"(?i)missing[- ]key", line) for line in cases), cases
    assert all(re.search(r"(?i)\bpass(ed)?\b", line) and not re.search(r"(?i)\bfail", line) for line in cases), cases
    assert "question for a human" not in report.lower()

    for proxy in bucket_contents(results, "verified"):
        assert verification_type(read_report(results, proxy)) in ("battery", "golden"), proxy

    assert bucket_of(results, "weather-api") == "needs-review"
    question = _question(read_report(results, "weather-api"))
    assert "SA-Limit" in question and re.search(r"(?i)time[- ]window", question), question

    for proxy, item, method in (
        ("legacy-auth", "OA-Verify", "skipped"),
        ("Test-API", "Get-Shared-Flow", "skipped"),
        ("js-transform", "JS-Reshape", "ai"),
    ):
        assert bucket_of(results, proxy) == "needs-review", proxy
        table = policy_table(read_report(results, proxy))
        rows = item_rows(table, item)
        assert rows and all(method_of(row[table.col("method")]) == method for row in rows), (proxy, rows)
    reshape = item_rows(policy_table(read_report(results, "js-transform")), "JS-Reshape")
    assert any(re.search(r"(?i)\blow\b", row_text(row)) for row in reshape), reshape

    assert bucket_contents(results, "unsupported") == ["broken-proxy"]
    assert sorted(p.name for p in (results / "unsupported" / "broken-proxy").iterdir()) == ["REPORT.md"]
    assert not any(name == "GetSharedFlow" for bucket in BUCKETS for name in bucket_contents(results, bucket))

    for proxy in PROXIES_WITH_PROJECT:
        assert kind_counts(read_report(results, proxy)) == EXPECTED_ROWS[proxy], proxy

    skipped = [
        line for line in run_log(results).splitlines()
        if re.search(r"(?i)skipping build|build and run skipped|not installed", line)
    ]
    assert skipped == [], skipped


def test_CP9_T20_after_a_real_runtime_run_summary_md_and_summary_json_agree(runtime_e2e: Run) -> None:
    """[CP9-T20] summary.json bucket counts equal the proxy folders on disk (verified at least 1, unsupported 1,
    total 6); verification types include battery at least 1 and sum to the 5 project proxies; each proxy entry's
    bucket and type equal its folder and REPORT.md; policy type counts equal CP9-T14's; SUMMARY.md tables hold
    exactly the summary.json numbers."""
    results = runtime_e2e.results
    data = read_summary(results)
    buckets = count_map(data, "buckets")
    for bucket in BUCKETS:
        assert buckets.get(bucket) == len(bucket_contents(results, bucket)), (bucket, buckets, listing(results))
    assert buckets["verified"] >= 1 and buckets["unsupported"] == 1 and sum(buckets.values()) == 6, buckets

    types = count_map(data, "verification_types")
    assert set(types) == set(VERIFICATION_TYPES), types
    assert types["battery"] >= 1 and sum(types.values()) == 5, types

    proxies = summary_proxies(data)
    assert sorted(proxies) == sorted(ALL_PROXIES)
    for proxy in ALL_PROXIES:
        assert proxies[proxy]["bucket"] == bucket_of(results, proxy), proxies[proxy]
    for proxy in PROXIES_WITH_PROJECT:
        assert proxies[proxy]["verification_type"] == verification_type(read_report(results, proxy)), proxy

    policy_types = count_map(data, "policy_types")
    for policy_type, count in E2E_POLICY_TYPES.items():
        assert policy_types.get(policy_type) == count, (policy_type, policy_types)

    md = (results / "SUMMARY.md").read_text(encoding="utf-8")
    for title, counts in (("bucket", buckets), ("policy type", policy_types), ("verification type", types)):
        assert summary_md_table(md, title) == counts, title


def test_CP9_T22_a_real_maven_failure_lands_in_review_as_failed_with_the_real_build_log(
    runtime_tools: RuntimeTools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[CP9-T22] catalog-api alone with MAVEN_ARGS='--offline -Dmaven.repo.local=<empty dir>': needs-review, verification
    type failed (never battery or static), diffs/ holds a non-empty build log with at least one '[ERROR]' line and
    an offline or dependency-resolution message, nothing was deployed, summary failed 1 and verified 0."""
    empty_repo = tmp_path / "empty-m2"
    empty_repo.mkdir()
    monkeypatch.setenv("MAVEN_ARGS", f"--offline -Dmaven.repo.local={empty_repo}")
    monkeypatch.setenv("A2M_MULE_HOME", str(runtime_tools.mule_home))

    exports = input_with(tmp_path, "catalog-api")
    run = migrate(exports, tmp_path / "results", "--mock-backends", "--max-fix-attempts", "0", llm_dir=LLM_LOW)
    results = run.results
    assert run.code == 0, run.err

    assert bucket_of(results, "catalog-api") == "needs-review", listing(results)
    report = read_report(results, "catalog-api")
    assert verification_type(report) == "failed", report

    diffs = results / "needs-review" / "catalog-api" / "diffs"
    logs = [p.read_text(encoding="utf-8", errors="replace") for p in sorted(diffs.rglob("*")) if p.is_file()]
    assert any(
        re.search(r"(?m)^\s*\[ERROR\]", text)
        and re.search(r"(?i)offline|could not resolve|dependenc|failed to read artifact", text)
        for text in logs
    ), logs

    log = run_log(results)
    assert not re.search(r"(?i)Started app '?catalog-api|deployed catalog-api|catalog-api[^\n]*\bdeployed\b", log), log

    data = read_summary(results)
    assert count_map(data, "verification_types")["failed"] == 1
    assert count_map(data, "verification_types")["battery"] == 0
    assert count_map(data, "buckets")["verified"] == 0
