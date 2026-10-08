"""CP9: buckets, REPORT.md, SUMMARY.md / summary.json and the README (default suite: no Java, Maven, Mule, network).

Runs go through ``a2m.cli.main`` (``--llm fake``); where a test needs the apps to "run", the default pipeline's
verification stage is swapped for one using a fake runner (:func:`e2e_support.stages_with`, CP7's Runner
protocol): :class:`e2e_support.OracleRunner` answers every battery case as a2m's own battery expects (battery ran,
every case passed), answers one chosen case wrong, or only builds; :class:`e2e_support.GoldenRunner` answers
recorded golden exchanges. CP9-T21 uses CP7-style tool stubs on PATH (java, a failing mvn, mule) and the real
runner. The README cases read README.md at the project root. The results and report contract these tests pin is
documented at the top of tests/e2e_support.py.

Fixture notes: the kitchen-sink proxy lives alone in tests/fixtures/e2e/kitchen-sink-input; the canned fake-AI
answers for JS-Reshape are tests/fixtures/llm/cp9-low and cp9-high (selected per test through A2M_FAKE_LLM_DIR).
The canned fix answers of CP9-T12 are written per test into a tmp copy of cp9-low: an AI fix has to echo the
project a2m really generated (as CP8-T18 does), so each one is that generated flow file with one added XML comment,
a real change that cannot make the failing case pass.
"""

from __future__ import annotations

import json
import re
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest
from e2e_support import (
    EM_DASH,
    EXPECTED_ROWS,
    KITCHEN_INPUT,
    KITCHEN_MISSING,
    KITCHEN_MISSING_INPUT,
    LLM_HIGH,
    LLM_LOW,
    METHODS,
    NEEDS_TOOLS_QUESTION,
    README,
    GoldenRunner,
    OracleRunner,
    Run,
    bucket_contents,
    bucket_of,
    clean,
    clear_tool_env,
    count_map,
    input_with,
    item_rows,
    kind_counts,
    listing,
    method_of,
    migrate,
    normalize_space,
    policy_table,
    read_report,
    read_summary,
    row_text,
    rows_by_kind,
    run_log,
    section,
    split_row,
    stages_with,
    summary_proxies,
    tables,
    tool_calls,
    unpacked,
    verification_type,
    write_stub,
    xml_counts,
)

E2E_PROXY_REPORTS = ("weather-api", "js-transform", "legacy-auth")
TEMPLATE_ITEMS = ("SA-Limit", "VK-Check", "AM-AddHeader", "EV-City")
PINNED_VERSIONS = {
    "mule-maven-plugin": "4.10.1",
    "mule-http-connector": "1.11.3",
    "mule-objectstore-connector": "1.2.2",
    "mule-validation-module": "2.0.9",
}
MVN_MARKER = "MARKER-MVN-BUILDLOG-4417"


@pytest.fixture(scope="module")
def e2e_reports(tmp_path_factory: pytest.TempPathFactory) -> Run:
    """The e2e input migrated once for the module with --llm fake --no-runtime."""
    from e2e_support import E2E_INPUT

    base = tmp_path_factory.mktemp("cp9-reports")
    return migrate(E2E_INPUT, base / "results", "--no-runtime", llm_dir=LLM_LOW)


@pytest.fixture(scope="module")
def kitchen(tmp_path_factory: pytest.TempPathFactory) -> Run:
    """The kitchen-sink proxy alone, migrated once with --llm fake --no-runtime."""
    base = tmp_path_factory.mktemp("cp9-kitchen")
    return migrate(KITCHEN_INPUT, base / "results", "--no-runtime", llm_dir=LLM_LOW)


def _other_cells(table: Any, row: list[str], *skip: str) -> str:
    """The text of every cell of ``row`` except the columns named by ``skip`` (header substrings)."""
    skipped = {table.col(name) for name in skip}
    return " | ".join(cell for index, cell in enumerate(row) if index not in skipped)


def _question(report: str) -> str:
    question = section(report, "Question for a human")
    assert question is not None, f"REPORT.md has no 'Question for a human':\n{report}"
    return question


def _has_text_after_label(text: str | None, label: str) -> bool:
    if text is None:
        return False
    rest = re.sub(re.escape(label), "", text, flags=re.IGNORECASE)
    return len(re.sub(r"[\s#*:|_-]", "", rest)) >= 10


def _case_lines(report: str) -> list[str]:
    """The test cases listed in the 'Test results' section: its table rows, else its bullet lines."""
    results = section(report, "Test results")
    assert results is not None, f"REPORT.md has no 'Test results' section:\n{report}"
    found = tables(results)
    if found:
        return [row_text(row) for table in found for row in table.rows]
    return [line for line in results.splitlines() if re.match(r"\s*[-*]\s+\S", line)]


def _assert_all_cases_passed(report: str) -> None:
    lines = _case_lines(report)
    assert lines, f"the 'Test results' section lists no case:\n{report}"
    for line in lines:
        assert re.search(r"(?i)\bpass(ed)?\b", line), line
        assert not re.search(r"(?i)\bfail(ed|ing)?\b", line), line


def _single(results: Path, proxy: str, bucket: str) -> None:
    """``proxy`` is in ``bucket`` and nowhere else, with REPORT.md (and mule-app/ unless unsupported)."""
    assert bucket_of(results, proxy) == bucket, listing(results)
    folder = results / bucket / proxy
    assert (folder / "REPORT.md").is_file(), listing(results)
    if bucket != "unsupported":
        assert (folder / "mule-app").is_dir(), listing(results)


# ---------------------------------------------------------------- CP9-T04


def test_CP9_T04_every_report_has_all_the_required_sections(e2e_reports: Run) -> None:
    """[CP9-T04] legacy-auth's REPORT.md has the policy table (item, kind, type, Mule result, method columns), a
    'Test results' section saying no tests ran because verification was static, an 'AI fix attempts' section saying
    none were made, 'Verification type' static, a 'Question for a human' and a 'Suggested fix' for OA-Verify, and no
    em dash."""
    report = read_report(e2e_reports.results, "legacy-auth")
    table = policy_table(report)
    for column in ("item", "kind", "type", "mule", "method"):
        table.col(column)

    tests_section = section(report, "Test results")
    assert tests_section is not None, report
    assert re.search(r"(?i)\bno tests?\b", tests_section) and "static" in tests_section.lower(), tests_section
    attempts = section(report, "AI fix attempts")
    assert attempts is not None, report
    assert re.search(r"(?i)\bnone\b|no (ai )?fix attempts|no attempts|were not made|no fix was", attempts), attempts
    assert verification_type(report) == "static"
    assert "OA-Verify" in _question(report)
    assert _has_text_after_label(section(report, "Suggested fix"), "Suggested fix"), report
    assert EM_DASH not in report


# ---------------------------------------------------------------- CP9-T05


def test_CP9_T05_the_method_column_tells_the_truth_about_how_each_item_was_handled(e2e_reports: Run) -> None:
    """[CP9-T05] SA-Limit, VK-Check, AM-AddHeader and EV-City say template; RF-NotFound (a FaultRule step, which a2m
    does not generate yet) has a skipped row whose reason names the fault rule, and no row of it says template;
    JS-Reshape says ai with confidence low and the fake AI's note; OA-Verify says skipped with a reason; every method
    is template, ai or skipped, and no AI-handled row is labelled template."""
    from e2e_support import JS_LOW_NOTE

    results = e2e_reports.results
    weather = policy_table(read_report(results, "weather-api"))
    for item in TEMPLATE_ITEMS:
        rows = item_rows(weather, item)
        assert rows, f"weather-api has no row for {item}"
        assert all(method_of(row[weather.col("method")]) == "template" for row in rows), (item, rows)
    fault = item_rows(weather, "RF-NotFound")
    assert fault, "weather-api has no row for RF-NotFound (silently dropped)"
    assert all(method_of(row[weather.col("method")]) != "template" for row in fault), fault
    assert any(
        method_of(row[weather.col("method")]) == "skipped"
        and re.search(r"(?i)fault rule", _other_cells(weather, row, "item", "kind", "type", "method"))
        and "not-found" in _other_cells(weather, row, "item", "kind", "type", "method")
        for row in fault
    ), fault

    js_report = read_report(results, "js-transform")
    js = policy_table(js_report)
    reshape = item_rows(js, "JS-Reshape")
    assert reshape and all(method_of(row[js.col("method")]) == "ai" for row in reshape), reshape
    assert any(re.search(r"(?i)\blow\b", row_text(row)) for row in reshape), reshape
    assert JS_LOW_NOTE in js_report

    legacy = policy_table(read_report(results, "legacy-auth"))
    oauth = item_rows(legacy, "OA-Verify")
    assert oauth, legacy.rows
    for row in oauth:
        assert method_of(row[legacy.col("method")]) == "skipped", row
        reason = _other_cells(legacy, row, "item", "kind", "type", "method")
        assert re.search(r"[A-Za-z]{3,}", reason), row

    for proxy in E2E_PROXY_REPORTS:
        table = policy_table(read_report(results, proxy))
        for row in table.rows:
            assert method_of(row[table.col("method")]) in METHODS, (proxy, row)


# ---------------------------------------------------------------- CP9-T07


def test_CP9_T07_unknown_missing_unused_and_untranslatable_items_still_get_rows(kitchen: Run, tmp_path: Path) -> None:
    """[CP9-T07] The copy whose step names Missing-Policy (no file) is refused by the bundle reader (locked CP2-T13)
    and lands in unsupported/ with only REPORT.md, whose reason names Missing-Policy. kitchen-sink itself: CT-Custom
    (type CustomThing, skipped, reason names the type), AM-Unused (present but attached to no step) and the =|
    condition (kept verbatim, ai with low confidence or skipped, never template) all have rows; per-kind counts equal
    the XML counts; the proxy is in needs-review."""
    results = kitchen.results
    assert kitchen.code == 0, kitchen.err
    assert bucket_of(results, "kitchen-sink") == "needs-review", listing(results)
    report = read_report(results, "kitchen-sink")
    table = policy_table(report)
    method = table.col("method")

    custom = item_rows(table, "CT-Custom")
    assert custom, table.rows
    assert any(
        clean(row[table.col("type")]) == "CustomThing"
        and method_of(row[method]) == "skipped"
        and "CustomThing" in _other_cells(table, row, "item", "type")
        for row in custom
    ), custom

    unused = item_rows(table, "AM-Unused")
    assert unused, table.rows
    assert any(
        re.search(r"(?i)not attached|unattached|not used|unused|not referenced|no step", _other_cells(table, row, "item"))
        for row in unused
    ), unused

    original = 'request.formparam.a =| "x"'
    conditions = [row for row in rows_by_kind(table)["condition"] if original in row_text(row)]
    assert len(conditions) == 1, rows_by_kind(table)["condition"]
    form = conditions[0]
    assert method_of(form[method]) in ("ai", "skipped"), form
    if method_of(form[method]) == "ai":
        assert re.search(r"(?i)\blow\b", row_text(form)), form

    from_xml = xml_counts(KITCHEN_INPUT, "kitchen-sink")
    assert from_xml == EXPECTED_ROWS["kitchen-sink"]
    assert kind_counts(report) == from_xml

    refused = migrate(KITCHEN_MISSING_INPUT, tmp_path / "missing", "--no-runtime", llm_dir=LLM_LOW)
    assert refused.code == 0, refused.err
    assert bucket_of(refused.results, KITCHEN_MISSING) == "unsupported", listing(refused.results)
    folder = refused.results / "unsupported" / KITCHEN_MISSING
    assert sorted(entry.name for entry in folder.iterdir()) == ["REPORT.md"], listing(refused.results)
    assert "Missing-Policy" in (folder / "REPORT.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------- CP9-T08


def test_CP9_T08_a_condition_containing_a_pipe_does_not_break_the_report_table(kitchen: Run) -> None:
    """[CP9-T08] Splitting only on unescaped pipes, every row of kitchen-sink's table has as many cells as the
    header, the JavaRegex condition row shows ^(GET|HEAD)$ in full, and the per-kind counts are unchanged."""
    report = read_report(kitchen.results, "kitchen-sink")
    table = policy_table(report)
    for raw in table.raw_rows:
        assert len(split_row(raw)) == len(table.header), raw
    regex_rows = [row for row in rows_by_kind(table)["condition"] if "^(GET|HEAD)$" in row_text(row)]
    assert len(regex_rows) == 1, rows_by_kind(table)["condition"]
    assert 'request.verb JavaRegex "^(GET|HEAD)$"' in row_text(regex_rows[0]), regex_rows[0]
    assert kind_counts(report) == EXPECTED_ROWS["kitchen-sink"]


# ---------------------------------------------------------------- CP9-T09


def _catalog(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    return input_with(tmp_path, "catalog-api"), {"catalog-api": unpacked(tmp_path / "bundles", "catalog-api")}


MISSING_KEY_BODY = json.dumps(
    {
        "fault": {
            "faultstring": "Failed to resolve API Key variable request.header.x-api-key",
            "detail": {"errorcode": "steps.oauth.v2.FailedToResolveAPIKey"},
        }
    }
)


def _catalog_golden(root: Path) -> GoldenRunner:
    """One recorded catalog-api exchange (a call with no API key, answered 401, the backend never called) and a
    runner whose app answers it exactly as recorded."""
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


def test_CP9_T09_a_fully_mapped_proxy_whose_tests_all_pass_goes_to_verified(tmp_path: Path) -> None:
    """[CP9-T09] catalog-api with every battery case passing lands in verified/ (only there) labelled battery, all
    cases passed, no 'Question for a human', summary verified 1 and battery 1; with --golden and every recording
    matched it is verified labelled golden, summary golden 1."""
    exports, bundles = _catalog(tmp_path)
    runner = OracleRunner(bundles, "pass")
    run = migrate(
        exports, tmp_path / "battery", "--mock-backends", "--max-fix-attempts", "0", stages=stages_with(runner)
    )
    assert run.code == 0, run.err
    assert runner.started and runner.unmatched == [], (runner.started, runner.unmatched)
    _single(run.results, "catalog-api", "verified")
    report = read_report(run.results, "catalog-api")
    assert verification_type(report) == "battery"
    _assert_all_cases_passed(report)
    assert "question for a human" not in report.lower()
    data = read_summary(run.results)
    assert count_map(data, "buckets")["verified"] == 1
    assert count_map(data, "verification_types")["battery"] == 1

    golden_runner = _catalog_golden(tmp_path / "golden")
    run = migrate(
        exports, tmp_path / "golden-run", "--mock-backends", "--golden", str(tmp_path / "golden"),
        "--max-fix-attempts", "0", stages=stages_with(golden_runner),
    )
    assert run.code == 0, run.err
    assert golden_runner.unmatched == [], golden_runner.unmatched
    _single(run.results, "catalog-api", "verified")
    report = read_report(run.results, "catalog-api")
    assert verification_type(report) == "golden"
    _assert_all_cases_passed(report)
    assert "question for a human" not in report.lower()
    data = read_summary(run.results)
    assert count_map(data, "buckets")["verified"] == 1
    assert count_map(data, "verification_types")["golden"] == 1


# ---------------------------------------------------------------- CP9-T10


def test_CP9_T10_a_proxy_that_was_only_built_is_never_put_in_verified(tmp_path: Path) -> None:
    """[CP9-T10] catalog-api built but not run: needs-review, static, asks to run with Java, Maven and Mule to
    verify; summary verified 0 and static 1."""
    exports, bundles = _catalog(tmp_path)
    run = migrate(
        exports, tmp_path / "results", "--mock-backends", "--max-fix-attempts", "0",
        stages=stages_with(OracleRunner(bundles, "built")),
    )
    assert run.code == 0, run.err
    _single(run.results, "catalog-api", "needs-review")
    assert bucket_contents(run.results, "verified") == []
    report = read_report(run.results, "catalog-api")
    assert verification_type(report) == "static"
    assert NEEDS_TOOLS_QUESTION in normalize_space(_question(report))
    data = read_summary(run.results)
    assert count_map(data, "buckets")["verified"] == 0
    assert count_map(data, "verification_types")["static"] == 1


# ---------------------------------------------------------------- CP9-T11


@pytest.mark.parametrize(
    ("proxy", "llm_dir", "bucket", "named"),
    [
        pytest.param("legacy-auth", LLM_LOW, "needs-review", "OA-Verify", id="a-unsupported-row"),
        pytest.param("js-transform", LLM_LOW, "needs-review", "JS-Reshape", id="b-low-confidence-ai"),
        pytest.param("js-transform", LLM_HIGH, "verified", None, id="c-high-confidence-ai"),
    ],
)
def test_CP9_T11_passing_tests_are_not_enough_when_an_item_is_unsupported_or_the_ai_was_unsure(
    tmp_path: Path, proxy: str, llm_dir: Path, bucket: str, named: str | None
) -> None:
    """[CP9-T11] Every battery case passes: (a) legacy-auth (skipped OAuthV2) and (b) js-transform with the low
    confidence answer land in needs-review labelled battery with a question naming OA-Verify / JS-Reshape; (c)
    js-transform with the high confidence answer lands in verified labelled battery, JS-Reshape still method ai."""
    exports = input_with(tmp_path, proxy)
    runner = OracleRunner({proxy: unpacked(tmp_path / "bundles", proxy)}, "pass")
    run = migrate(
        exports, tmp_path / "results", "--mock-backends", "--max-fix-attempts", "0", stages=stages_with(runner),
        llm_dir=llm_dir,
    )
    assert run.code == 0, run.err
    assert runner.unmatched == [], runner.unmatched
    _single(run.results, proxy, bucket)
    report = read_report(run.results, proxy)
    assert verification_type(report) == "battery"
    if named is not None:
        assert named in _question(report)
    else:
        table = policy_table(report)
        reshape = item_rows(table, "JS-Reshape")
        assert reshape and all(method_of(row[table.col("method")]) == "ai" for row in reshape), reshape


# ---------------------------------------------------------------- CP9-T12


def _write_useless_fixes(llm_dir: Path, bundle_dir: Path, proxy: str, count: int) -> None:
    """Canned fix answers 1..count for ``proxy``: the flow file a2m generates for it with one XML comment added
    right after the first <flow> tag (a real change, so each attempt records what changed, that cannot help)."""
    from a2m.ai.fake import FakeProvider
    from a2m.generator import generate_project
    from a2m.parser import read_bundle

    probe = llm_dir.parent / "fix-probe" / proxy / "mule-app"
    generate_project(read_bundle(bundle_dir), probe, provider=FakeProvider(llm_dir))
    flow = (probe / "src" / "main" / "mule" / "proxy.xml").read_text(encoding="utf-8")
    opening = re.search(r"<flow\b[^>]*>", flow)
    assert opening is not None, flow
    fix_dir = llm_dir / "fix"
    fix_dir.mkdir(parents=True, exist_ok=True)
    for number in range(1, count + 1):
        text = flow[: opening.end()] + f"<!-- a2m cp9 fix attempt {number} -->" + flow[opening.end():]
        answer = {"status": "fixed", "files": {"src/main/mule/proxy.xml": text}, "notes": f"cp9 attempt {number}"}
        suffix = "" if number == 1 else f".{number}"
        (fix_dir / f"{proxy}{suffix}.json").write_text(json.dumps(answer), encoding="utf-8")


def test_CP9_T12_a_proxy_still_failing_after_ai_fix_attempts_goes_to_review_with_its_diff_and_history(
    tmp_path: Path,
) -> None:
    """[CP9-T12] weather-api with SA-Limit's under-limit case failing (expected 200, got 429) on every attempt and
    --max-fix-attempts 2: needs-review, verification type failed, the failing case with 200 and 429 in the test
    results, exactly 2 attempts each saying what changed and 'did not help', a question naming the failing case and a
    suggested fix, a non-empty diff file in diffs/, and summary.json counting it under failed."""
    bundle = unpacked(tmp_path / "bundles", "weather-api")
    llm_dir = tmp_path / "llm"
    shutil.copytree(LLM_LOW, llm_dir)
    _write_useless_fixes(llm_dir, bundle, "weather-api", 2)
    exports = input_with(tmp_path, "weather-api")
    runner = OracleRunner(
        {"weather-api": bundle}, "fail-one", fail_policy="SA-Limit", fail_situation="under-limit"
    )
    run = migrate(
        exports, tmp_path / "results", "--mock-backends", "--max-fix-attempts", "2", stages=stages_with(runner),
        llm_dir=llm_dir,
    )
    assert run.code == 0, run.err
    assert runner.failed_calls >= 3,"the failing case should run once, then once per fix attempt"
    _single(run.results, "weather-api", "needs-review")
    report = read_report(run.results, "weather-api")
    assert verification_type(report) == "failed"

    failing = [line for line in _case_lines(report) if "SA-Limit" in line and re.search(r"(?i)\bfail", line)]
    assert failing, _case_lines(report)
    test_results = section(report, "Test results") or ""
    assert "200" in test_results and "429" in test_results, test_results

    attempts = section(report, "AI fix attempts")
    assert attempts is not None, report
    assert len(re.findall(r"(?i)did not help", attempts)) == 2, attempts
    assert attempts.count("proxy.xml") >= 2, attempts
    assert "SA-Limit" in _question(report)
    assert _has_text_after_label(section(report, "Suggested fix"), "Suggested fix"), report

    diffs = run.results / "needs-review" / "weather-api" / "diffs"
    files = [p for p in sorted(diffs.rglob("*")) if p.is_file() and p.stat().st_size > 0]
    assert any("SA-Limit" in p.name or "SA-Limit" in p.read_text(encoding="utf-8", errors="replace") for p in files), (
        listing(run.results)
    )

    data = read_summary(run.results)
    types = count_map(data, "verification_types")
    assert types["failed"] == 1 and types["battery"] == 0, types
    assert summary_proxies(data)["weather-api"]["verification_type"] == "failed"


# ---------------------------------------------------------------- CP9-T16 / T17 / T24 / T25: README.md


def _readme() -> str:
    assert README.is_file(), f"{README} does not exist"
    return README.read_text(encoding="utf-8")


def _heading_section(text: str, pattern: str) -> str:
    """The README section under the first Markdown heading matching ``pattern`` (case-insensitive), up to the next
    heading of the same or a higher level."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        heading = re.match(r"^(#{1,6})\s+(.*)$", line.strip())
        if heading and re.search(pattern, heading.group(2), re.IGNORECASE):
            level = len(heading.group(1))
            body = []
            for following in lines[index + 1:]:
                other = re.match(r"^(#{1,6})\s+", following.strip())
                if other and len(other.group(1)) <= level:
                    break
                body.append(following)
            return "\n".join(body)
    raise AssertionError(f"README.md has no heading matching {pattern!r}")


def _blocks(text: str, names: tuple[str, ...]) -> dict[str, str]:
    """{name: the text from its first word-bounded mention up to the first mention of the next name found after it}."""
    starts = {}
    for name in names:
        match = re.search(rf"(?i)\b{re.escape(name)}\b", text)
        assert match is not None, f"{name!r} is not mentioned:\n{text}"
        starts[name] = match.start()
    order = sorted(names, key=lambda n: starts[n])
    return {
        name: text[starts[name]: starts[order[i + 1]] if i + 1 < len(order) else len(text)]
        for i, name in enumerate(order)
    }


def test_CP9_T16_the_readme_explains_install_usage_buckets_and_verification_types() -> None:
    """[CP9-T16] README.md: install (pip or uv command, the claude extra, ANTHROPIC_API_KEY), usage (a2m migrate with
    --out), the three buckets with their rules, what golden/battery/static prove and do not prove (static: built
    only or not built, never run), failed always going to needs-review, OAuthV2 listed as unsupported, no em dash."""
    text = _readme()
    install = _heading_section(text, r"install")
    assert re.search(r"\b(uv pip|pip|pipx|uv tool) install\b", install), install
    assert "[claude]" in install, install
    assert "ANTHROPIC_API_KEY" in install, install

    usage = _heading_section(text, r"usage")
    assert "a2m migrate" in usage and "--out" in usage, usage

    buckets = _heading_section(text, r"bucket|results")
    for name, block in _blocks(buckets, ("verified", "needs-review", "unsupported")).items():
        assert len(block.strip()) > len(name) + 20, f"no rule given for {name}: {block!r}"

    kinds = _heading_section(text, r"verification type")
    blocks = _blocks(kinds, ("golden", "battery", "static", "failed"))
    for name in ("golden", "battery", "static"):
        assert re.search(r"(?i)\bproves?\b", blocks[name]), (name, blocks[name])
        assert re.search(r"(?i)\b(not|never|doesn't|does not)\b", blocks[name]), (name, blocks[name])
    assert re.search(r"(?i)built only|only built|not built", blocks["static"]), blocks["static"]
    assert re.search(r"(?i)never run|not run|never ran|was not run", blocks["static"]), blocks["static"]
    assert re.search(r"(?i)\btests?\b", blocks["failed"]) and re.search(r"(?i)build|deploy", blocks["failed"]), blocks[
        "failed"
    ]
    assert "needs-review" in blocks["failed"], blocks["failed"]

    oauth_lines = [line for line in text.splitlines() if "OAuthV2" in line]
    assert oauth_lines, "README.md does not mention OAuthV2"
    assert any(re.search(r"(?i)unsupported|not supported", line) for line in oauth_lines) or any(
        "OAuthV2" in _heading_section(text, pattern) for pattern in (r"unsupported", r"not supported", r"limitation")
        if re.search(rf"(?im)^#+\s.*{pattern}", text)
    ), oauth_lines
    assert EM_DASH not in text


def test_CP9_T17_every_command_line_option_is_documented_in_the_readme(run_cli: Any) -> None:
    """[CP9-T17] Every long option `a2m migrate --help` prints (except --help) appears in README.md."""
    res = run_cli(["migrate", "--help"])
    assert res.code == 0, res.err
    options = sorted(set(re.findall(r"(?<![\w-])--[a-z][a-z0-9-]*", res.out)) - {"--help"})
    for expected in ("--out", "--only", "--resume", "--force", "--golden", "--mock-backends", "--max-fix-attempts",
                     "--llm", "--no-runtime"):
        assert expected in options, (expected, res.out)
    text = _readme()
    missing = [option for option in options if not re.search(rf"(?<![\w-]){re.escape(option)}(?![\w-])", text)]
    assert missing == [], f"options not documented in README.md: {', '.join(missing)}"


def _pom_versions(pom: Path) -> dict[str, str]:
    ns = {"m": "http://maven.apache.org/POM/4.0.0"}
    root = ET.parse(pom).getroot()
    found = {}
    for element in root.iter():
        if element.tag.endswith("}dependency") or element.tag.endswith("}plugin"):
            artifact = element.find("m:artifactId", ns)
            version = element.find("m:version", ns)
            if artifact is not None and version is not None and artifact.text in PINNED_VERSIONS:
                found[artifact.text] = (version.text or "").strip()
    return found


def test_CP9_T24_the_readme_explains_how_to_install_the_local_mule_toolchain(tmp_path: Path) -> None:
    """[CP9-T24] The toolchain section names mise and mise.toml (Temurin 17, Maven 3.9), `mise exec --`, the Mule
    Kernel CE 4.9.0 download and MULE_HOME (A2M_MULE_HOME overrides it), the four pinned versions with the reason,
    and that a2m still runs without them and labels proxies static; each pinned version equals the one in the pom.xml
    a2m generates for catalog-api; no em dash."""
    from a2m.generator import generate_project
    from a2m.parser import read_bundle

    text = _readme()
    toolchain = _heading_section(text, r"toolchain")
    for needle in ("mise.toml", "mise exec --", "Temurin", "MULE_HOME", "A2M_MULE_HOME", "4.9.0"):
        assert needle in toolchain, needle
    assert re.search(r"\bmise\b", toolchain)
    assert re.search(r"(?i)temurin[^\n]*\b17\b|\b17\b[^\n]*temurin|java[^\n]*\b17\b", toolchain), toolchain
    assert re.search(r"(?i)maven[^\n]*3\.9", toolchain), toolchain
    assert re.search(r"(?i)mule kernel", toolchain) and re.search(r"(?i)\bCE\b|community edition", toolchain)
    assert re.search(r"(?i)download", toolchain), toolchain
    assert re.search(r"(?i)A2M_MULE_HOME[^\n]*(override|wins|takes precedence)", toolchain), toolchain
    readme_versions = {}
    for artifact, version in PINNED_VERSIONS.items():
        lines = [line for line in toolchain.splitlines() if artifact in line]
        assert lines, f"{artifact} is not listed in the toolchain section"
        found = re.search(r"\b(\d+\.\d+\.\d+)\b", " ".join(lines))
        assert found is not None and found.group(1) == version, (artifact, lines)
        readme_versions[artifact] = found.group(1)
    assert re.search(r"(?i)newer[^.]*(fail|do not|don't)[^.]*deploy|fail[^.]*deploy[^.]*4\.9\.0", toolchain), toolchain
    assert re.search(r"(?i)without[^.]*(java|maven|mule|these tools|the toolchain)[^.]*static", toolchain) or re.search(
        r"(?i)static[^.]*without[^.]*(java|maven|mule|these tools|the toolchain)", toolchain
    ), toolchain

    app = tmp_path / "catalog-api" / "mule-app"
    generate_project(read_bundle(unpacked(tmp_path / "bundles", "catalog-api")), app)
    generated = _pom_versions(app / "pom.xml")
    assert {"mule-maven-plugin", "mule-http-connector"} <= set(generated), generated
    for artifact, version in generated.items():
        assert readme_versions[artifact] == version, (artifact, readme_versions[artifact], version)
    assert EM_DASH not in text


def test_CP9_T25_the_readme_says_what_a_local_ce_runtime_check_proves_and_what_it_does_not() -> None:
    """[CP9-T25] The local runtime verification section says battery or golden means built with Maven, deployed on
    Mule Kernel CE 4.9.0 and passed against a local mock backend, and names the five things it does not prove:
    Mule Enterprise runtimes, CloudHub, API Manager policies, real backends, production load and timing."""
    text = _readme()
    part = _heading_section(text, r"(runtime|local).*(verif|prove|check)|(verif|check).*(runtime|local)")
    for needle in ("battery", "golden", "Maven", "4.9.0"):
        assert needle in part, needle
    assert re.search(r"(?i)deploy", part) and re.search(r"(?i)mock backend", part), part
    assert re.search(r"(?i)mule kernel|\bCE\b|community edition", part), part
    assert re.search(r"(?i)(does not|doesn't|do not|not) prove", part), part
    for item in (r"enterprise", r"cloudhub", r"api manager", r"real backends?", r"production"):
        assert re.search(rf"(?i){item}", part), item
    assert re.search(r"(?i)\bload\b", part) and re.search(r"(?i)\btiming\b", part), part
    assert EM_DASH not in text


# ---------------------------------------------------------------- CP9-T21


def test_CP9_T21_a_failed_maven_build_sends_the_proxy_to_review_as_failed_with_the_build_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[CP9-T21] With a java stub, a Mule home and an mvn stub that prints BUILD FAILURE and a marker line then
    exits 1: exit 0, both proxies processed; catalog-api in needs-review with mule-app/ kept, verification type
    failed, the report says the Maven build failed and asks about it; diffs/ holds the build log (BUILD FAILURE and
    the marker); mvn was called with package and the java stub (Mule's JVM) never deployed catalog-api; summary
    failed 1, battery 0, verified 0; catalog-api has its .done marker; no traceback."""
    calls = tmp_path / "tool-calls.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_stub(bin_dir / "java", calls)
    write_stub(
        bin_dir / "mvn",
        calls,
        ["echo '[INFO] Scanning for projects...'", "echo '[ERROR] BUILD FAILURE'", f"echo '{MVN_MARKER}'"],
        exit_code=1,
    )
    home = tmp_path / "mule-home"
    for sub in ("services", "conf", "lib/boot"):
        (home / sub).mkdir(parents=True)
    (home / "lib" / "boot" / "mule-module-reboot-4.9.0.jar").write_bytes(b"")
    clear_tool_env(monkeypatch)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("A2M_MULE_HOME", str(home))

    exports = input_with(tmp_path, "catalog-api", "legacy-auth")
    run = migrate(exports, tmp_path / "results", "--mock-backends", "--max-fix-attempts", "0")
    results = run.results
    assert run.code == 0, run.err
    for proxy in ("catalog-api", "legacy-auth"):
        assert read_report(results, proxy)

    _single(results, "catalog-api", "needs-review")
    report = read_report(results, "catalog-api")
    assert verification_type(report) == "failed"
    assert re.search(r"(?i)maven build failed|build failed", report), report
    assert re.search(r"(?i)\bbuild\b", _question(report)), _question(report)

    diffs = results / "needs-review" / "catalog-api" / "diffs"
    logs = [p.read_text(encoding="utf-8", errors="replace") for p in sorted(diffs.rglob("*")) if p.is_file()]
    assert any("BUILD FAILURE" in text and MVN_MARKER in text for text in logs), listing(results)

    rows = tool_calls(calls)
    assert any(tool == "mvn" and "package" in args.split() for tool, _, args in rows), rows
    assert not any(tool == "java" and "catalog-api" in args for tool, _, args in rows), rows

    data = read_summary(results)
    assert summary_proxies(data)["catalog-api"]["verification_type"] == "failed"
    types = count_map(data, "verification_types")
    assert types["battery"] == 0 and types["failed"] >= 1, types
    assert count_map(data, "buckets")["verified"] == 0
    assert (results / "needs-review" / "catalog-api" / ".done").is_file()
    assert "Traceback" not in run.out + run.err + run_log(results)


# ---------------------------------------------------------------- CP9-T23


def test_CP9_T23_a_time_window_policy_keeps_a_proxy_out_of_verified_even_when_all_its_tests_pass(
    tmp_path: Path,
) -> None:
    """[CP9-T23] weather-api with every battery case passing: needs-review (not verified), verification type
    battery, every case passed, a question naming SA-Limit as a time-window policy; summary verified 0, battery 1."""
    exports = input_with(tmp_path, "weather-api")
    runner = OracleRunner({"weather-api": unpacked(tmp_path / "bundles", "weather-api")}, "pass")
    run = migrate(
        exports, tmp_path / "results", "--mock-backends", "--max-fix-attempts", "0", stages=stages_with(runner)
    )
    assert run.code == 0, run.err
    assert runner.unmatched == [], runner.unmatched
    _single(run.results, "weather-api", "needs-review")
    report = read_report(run.results, "weather-api")
    assert verification_type(report) == "battery"
    _assert_all_cases_passed(report)
    question = _question(report)
    assert "SA-Limit" in question and re.search(r"(?i)time[- ]window", question), question
    data = read_summary(run.results)
    assert count_map(data, "buckets")["verified"] == 0
    assert count_map(data, "verification_types")["battery"] == 1
