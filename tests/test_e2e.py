"""CP9: the end-to-end fixture run (default suite: no Java, Maven, Mule, network or API key).

Every run goes through the public entry point ``a2m.cli.main`` with ``--llm fake`` (canned answers from
tests/fixtures/llm/cp9-low via A2M_FAKE_LLM_DIR) and either ``--no-runtime`` or a PATH that hides java and mvn.
The input is tests/fixtures/e2e/input (Test-API, GetSharedFlow, weather-api.zip, catalog-api, legacy-auth,
js-transform, broken-proxy; see tests/e2e_support.py for what each holds and for the results contract these tests
pin). Golden copies live in tests/golden/e2e and are only (re)written with A2M_UPDATE_GOLDEN=1; without it a missing
or different copy fails the test and the copy is left as it is.

The 'nothing silently dropped' counts are taken straight from the bundle XML with ElementTree
(:func:`e2e_support.xml_counts`), never through a2m's parser, and must also equal the hand-written literals in
:data:`e2e_support.EXPECTED_ROWS`.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest
from e2e_support import (
    ALL_PROXIES,
    BUCKETS,
    CP1_DOT_FILES,
    E2E_INPUT,
    E2E_POLICY_TYPES,
    EM_DASH,
    EXPECTED_ROWS,
    GOLDEN_E2E,
    LLM_LOW,
    NEEDS_TOOLS_QUESTION,
    PROXIES_WITH_PROJECT,
    SENTINEL,
    TOP_LEVEL,
    OracleRunner,
    Run,
    all_files,
    bucket_contents,
    bucket_of,
    check_golden,
    clean,
    clear_tool_env,
    count_map,
    input_with,
    item_rows,
    kind_counts,
    listing,
    method_of,
    migrate,
    mule_app_files,
    normalize_space,
    normalized_json,
    policy_table,
    proxy_folders,
    read_report,
    read_summary,
    row_text,
    run_log,
    section,
    stages_crashing,
    stages_with,
    strip_timestamps,
    summary_md_table,
    summary_proxies,
    unpacked,
    updating_golden,
    verification_type,
    xml_counts,
)


@pytest.fixture(scope="module")
def e2e(tmp_path_factory: pytest.TempPathFactory) -> Run:
    """The e2e input migrated once for the module: --llm fake --no-runtime into an empty tmp results folder."""
    base = tmp_path_factory.mktemp("cp9-e2e")
    return migrate(E2E_INPUT, base / "results", "--no-runtime", llm_dir=LLM_LOW)


def _layout(run: Run) -> str:
    return "\n".join(listing(run.results))


# ---------------------------------------------------------------- CP9-T01


def test_CP9_T01_end_to_end_run_produces_exactly_the_expected_results_folder(e2e: Run) -> None:
    """[CP9-T01] Exit 0; results/ holds exactly SUMMARY.md, summary.json, run.log and the three buckets (plus CP1's
    .a2m-results and .a2m-lock); verified/ is empty; needs-review/ holds the five proxies that produced a project,
    each with mule-app/pom.xml, REPORT.md and diffs/; unsupported/ holds broken-proxy with only REPORT.md;
    GetSharedFlow has no folder; the sorted tree equals tests/golden/e2e/tree.txt."""
    results = e2e.results
    assert e2e.code == 0, e2e.err

    entries = {entry.name for entry in results.iterdir()}
    visible = {name for name in entries if not name.startswith(".")}
    hidden = entries - visible
    assert visible == set(TOP_LEVEL), _layout(e2e)
    assert hidden <= CP1_DOT_FILES, f"unexpected hidden entries {sorted(hidden - CP1_DOT_FILES)}"
    for bucket in BUCKETS:
        assert (results / bucket).is_dir(), _layout(e2e)

    assert bucket_contents(results, "verified") == []
    assert bucket_contents(results, "needs-review") == sorted(PROXIES_WITH_PROJECT)
    for proxy in PROXIES_WITH_PROJECT:
        folder = results / "needs-review" / proxy
        assert (folder / "mule-app" / "pom.xml").is_file(), _layout(e2e)
        assert (folder / "REPORT.md").is_file(), _layout(e2e)
        assert (folder / "diffs").is_dir(), _layout(e2e)

    assert bucket_contents(results, "unsupported") == ["broken-proxy"]
    broken = results / "unsupported" / "broken-proxy"
    assert sorted(entry.name for entry in broken.iterdir()) == ["REPORT.md"]

    assert "GetSharedFlow" not in proxy_folders(results)
    assert not (results / "GetSharedFlow").exists()

    check_golden("tree.txt", "\n".join(listing(results)) + "\n")


# ---------------------------------------------------------------- CP9-T02


def _reports(results: Path) -> dict[str, str]:
    """{'<bucket>/<proxy>/REPORT.md': text} for every proxy report."""
    found = {}
    for bucket in BUCKETS:
        for path in sorted((results / bucket).glob("*/REPORT.md")) if (results / bucket).is_dir() else []:
            found[path.relative_to(results).as_posix()] = path.read_text(encoding="utf-8")
    return found


def _generated_files(results: Path) -> list[Path]:
    """Every report and project file a2m generated (everything but run.log and a2m's own dot files)."""
    return [
        p for p in all_files(results) if p.name != "run.log" and not p.relative_to(results).parts[0].startswith(".")
    ]


def test_CP9_T02_reports_match_the_saved_reference_copies_and_are_identical_on_a_second_run(
    e2e: Run, tmp_path: Path
) -> None:
    """[CP9-T02] A second run gives byte-identical REPORT.md and mule-app files; each REPORT.md, summary.json and
    SUMMARY.md equals its golden copy once timestamps are removed; no generated file holds the absolute tmp path;
    without A2M_UPDATE_GOLDEN=1 a mismatch fails and leaves the golden copy unchanged."""
    second = migrate(E2E_INPUT, tmp_path / "results", "--no-runtime", llm_dir=LLM_LOW)
    assert e2e.code == 0 and second.code == 0, (e2e.err, second.err)

    first_reports, second_reports = _reports(e2e.results), _reports(second.results)
    assert sorted(first_reports) == sorted(f"{b}/{p}/REPORT.md" for p, b in _expected_buckets().items())
    assert first_reports == second_reports
    for proxy in PROXIES_WITH_PROJECT:
        bucket = bucket_of(e2e.results, proxy)
        assert mule_app_files(e2e.results / bucket / proxy) == mule_app_files(second.results / bucket / proxy), proxy

    for run in (e2e, second):
        roots = {str(run.results), str(run.results.parent), str(run.results.resolve())}
        for path in _generated_files(run.results):
            text = path.read_bytes().decode("utf-8", errors="replace")
            leaked = [root for root in roots if root in text]
            assert not leaked, f"{path.relative_to(run.results)} contains the absolute path {leaked[0]}"

    if updating_golden():
        for bucket in BUCKETS:
            shutil.rmtree(GOLDEN_E2E / bucket, ignore_errors=True)
    else:
        golden_reports = sorted(
            p.relative_to(GOLDEN_E2E).as_posix() for bucket in BUCKETS for p in (GOLDEN_E2E / bucket).glob("*/REPORT.md")
        )
        assert golden_reports == sorted(first_reports), (
            "the golden REPORT.md copies under tests/golden/e2e do not match the proxies of this run; "
            "create or refresh them with A2M_UPDATE_GOLDEN=1"
        )
    for rel, text in sorted(first_reports.items()):
        check_golden(rel, strip_timestamps(text))
    summary_json = (e2e.results / "summary.json").read_text(encoding="utf-8")
    summary_md = (e2e.results / "SUMMARY.md").read_text(encoding="utf-8")
    check_golden("summary.json", normalized_json(summary_json))
    check_golden("SUMMARY.md", strip_timestamps(summary_md))
    assert normalized_json(summary_json) == normalized_json(
        (second.results / "summary.json").read_text(encoding="utf-8")
    )
    assert strip_timestamps(summary_md) == strip_timestamps((second.results / "SUMMARY.md").read_text(encoding="utf-8"))

    if not updating_golden():
        golden = GOLDEN_E2E / "SUMMARY.md"
        before = golden.read_bytes()
        with pytest.raises(pytest.fail.Exception):
            check_golden("SUMMARY.md", strip_timestamps(summary_md) + "\na deliberate difference\n")
        assert golden.read_bytes() == before, "a failed golden comparison changed the golden copy"


def _expected_buckets() -> dict[str, str]:
    return {**{proxy: "needs-review" for proxy in PROXIES_WITH_PROJECT}, "broken-proxy": "unsupported"}


# ---------------------------------------------------------------- CP9-T03


def test_CP9_T03_each_sample_proxy_lands_in_the_bucket_its_contents_call_for_when_nothing_can_run(e2e: Run) -> None:
    """[CP9-T03] With no runtime: weather-api and catalog-api are needs-review/static and ask to run with Java, Maven
    and Mule; legacy-auth's OA-Verify is skipped as unsupported; js-transform's JS-Reshape is ai with low confidence;
    Test-API is static with Get-Shared-Flow skipped; broken-proxy is unsupported naming the file and the parse error."""
    results = e2e.results
    for proxy, bucket in _expected_buckets().items():
        assert bucket_of(results, proxy) == bucket, proxy

    for proxy in ("weather-api", "catalog-api", "Test-API"):
        report = read_report(results, proxy)
        assert verification_type(report) == "static", proxy
    for proxy in ("weather-api", "catalog-api"):
        question = section(read_report(results, proxy), "Question for a human")
        assert question is not None, f"{proxy} REPORT.md has no 'Question for a human'"
        assert NEEDS_TOOLS_QUESTION in normalize_space(question), question

    legacy = policy_table(read_report(results, "legacy-auth"))
    oauth = item_rows(legacy, "OA-Verify")
    assert oauth, legacy.rows
    for row in oauth:
        assert method_of(row[legacy.col("method")]) == "skipped", row
        assert re.search(r"(?i)unsupported|not supported|not translated", row_text(row)), row

    js = policy_table(read_report(results, "js-transform"))
    reshape = item_rows(js, "JS-Reshape")
    assert reshape, js.rows
    assert all(method_of(row[js.col("method")]) == "ai" for row in reshape), reshape
    assert any(re.search(r"(?i)\blow\b", row_text(row)) for row in reshape), reshape

    test_api = policy_table(read_report(results, "Test-API"))
    kvm = item_rows(test_api, "Get-Shared-Flow")
    assert kvm, test_api.rows
    assert all(method_of(row[test_api.col("method")]) == "skipped" for row in kvm), kvm
    assert any(clean(row[test_api.col("type")]) == "KeyValueMapOperations" for row in kvm), kvm

    broken = read_report(results, "broken-proxy")
    assert "proxies/default.xml" in broken, broken
    assert "unclosed token" in broken, broken


# ---------------------------------------------------------------- CP9-T06


def test_CP9_T06_nothing_is_silently_dropped_input_items_equal_report_rows(e2e: Run) -> None:
    """[CP9-T06] For every proxy that produced a project, the step, policy and condition rows of its REPORT.md equal
    the counts taken from the raw bundle XML (shared flows it calls included), which equal the hand-written literals
    (weather-api 5/5/2, catalog-api 2/2/0, ...), and summary.json's per-proxy counts equal them too."""
    assert EXPECTED_ROWS["weather-api"].as_dict() == {"step": 5, "policy": 5, "condition": 2}
    assert EXPECTED_ROWS["catalog-api"].as_dict() == {"step": 2, "policy": 2, "condition": 0}
    proxies = summary_proxies(read_summary(e2e.results))
    for proxy in PROXIES_WITH_PROJECT:
        from_xml = xml_counts(E2E_INPUT, proxy)
        assert from_xml == EXPECTED_ROWS[proxy], f"{proxy}: XML count {from_xml} != literal {EXPECTED_ROWS[proxy]}"
        assert kind_counts(read_report(e2e.results, proxy)) == from_xml, proxy
        assert proxies[proxy]["rows"] == from_xml.as_dict(), (proxy, proxies[proxy])


# ---------------------------------------------------------------- CP9-T13


def test_CP9_T13_crashed_and_malformed_proxies_get_an_unsupported_report_and_no_done_marker(tmp_path: Path) -> None:
    """[CP9-T13] A stage raising RuntimeError('boom in generator') for js-transform gives unsupported/js-transform/
    REPORT.md with the error and no mule-app/; broken-proxy's report has the parse error; neither has a .done marker;
    catalog-api finishes in needs-review with its .done; the batch carries on with CP1's crash exit code 1."""
    exports = input_with(tmp_path, "catalog-api", "js-transform", "broken-proxy")
    run = migrate(exports, tmp_path / "results", "--no-runtime", stages=stages_crashing("js-transform", "boom in generator"))
    results = run.results

    assert run.code == 1, run.err
    assert bucket_of(results, "js-transform") == "unsupported"
    crashed = results / "unsupported" / "js-transform"
    assert "boom in generator" in (crashed / "REPORT.md").read_text(encoding="utf-8")
    assert not (crashed / "mule-app").exists()

    assert bucket_of(results, "broken-proxy") == "unsupported"
    assert "unclosed token" in (results / "unsupported" / "broken-proxy" / "REPORT.md").read_text(encoding="utf-8")
    for proxy in ("js-transform", "broken-proxy"):
        assert not list(results.glob(f"*/{proxy}/.done")), proxy

    assert bucket_of(results, "catalog-api") == "needs-review"
    assert (results / "needs-review" / "catalog-api" / ".done").is_file()
    log = run_log(results)
    assert any("js-transform" in line and "boom in generator" in line for line in log.splitlines()), log


# ---------------------------------------------------------------- CP9-T14


def test_CP9_T14_summary_md_and_summary_json_show_the_same_counts(e2e: Run) -> None:
    """[CP9-T14] summary.json: buckets verified 0, needs-review 5, unsupported 1 (the 6 folders on disk); the policy
    type counts of the fixture; verification types static 5, golden 0, battery 0, failed 0; one entry per proxy.
    SUMMARY.md shows the same numbers in bucket, policy type and verification type tables, with no em dash."""
    data = read_summary(e2e.results)
    buckets = count_map(data, "buckets")
    assert buckets == {"verified": 0, "needs-review": 5, "unsupported": 1}
    on_disk = sum(len(bucket_contents(e2e.results, bucket)) for bucket in BUCKETS)
    assert sum(buckets.values()) == on_disk == 6

    policy_types = count_map(data, "policy_types")
    for policy_type, count in E2E_POLICY_TYPES.items():
        assert policy_types.get(policy_type) == count, (policy_type, policy_types)
    types = count_map(data, "verification_types")
    assert types == {"golden": 0, "battery": 0, "static": 5, "failed": 0}

    proxies = summary_proxies(data)
    assert sorted(proxies) == sorted(ALL_PROXIES)
    for proxy, bucket in _expected_buckets().items():
        assert proxies[proxy]["bucket"] == bucket, proxies[proxy]
    for proxy in PROXIES_WITH_PROJECT:
        assert proxies[proxy]["verification_type"] == "static", proxies[proxy]

    md = (e2e.results / "SUMMARY.md").read_text(encoding="utf-8")
    assert EM_DASH not in md
    for title, counts in (("bucket", buckets), ("policy type", policy_types), ("verification type", types)):
        table = summary_md_table(md, title)
        for name, count in counts.items():
            assert table.get(name) == count, (title, name, table)


# ---------------------------------------------------------------- CP9-T15


def test_CP9_T15_resumed_and_forced_reruns_keep_the_summary_complete_and_each_proxy_in_one_bucket(
    tmp_path: Path,
) -> None:
    """[CP9-T15] (a) --resume after a full run still lists all 6 proxies with the same bucket counts; (b) --force
    --only catalog-api with the fake runner reporting battery passed moves catalog-api from needs-review to verified,
    summary.json shows verified 1, needs-review 4, and the other proxies' folders and reports are unchanged."""
    exports = E2E_INPUT
    results = tmp_path / "results"
    first = migrate(exports, results, "--no-runtime")
    assert first.code == 0, first.err
    for proxy in ("catalog-api", "legacy-auth"):
        assert (results / "needs-review" / proxy / ".done").is_file(), listing(results)
    before = count_map(read_summary(results), "buckets")

    resumed = migrate(exports, results, "--no-runtime", "--resume")
    assert resumed.code == 0, resumed.err
    data = read_summary(results)
    assert count_map(data, "buckets") == before
    assert sorted(summary_proxies(data)) == sorted(ALL_PROXIES)
    md = (results / "SUMMARY.md").read_text(encoding="utf-8")
    assert summary_md_table(md, "bucket").get("needs-review") == before["needs-review"]

    others = [p for p in ALL_PROXIES if p != "catalog-api"]
    snapshot = {p: (read_report(results, p), mule_app_files(results / bucket_of(results, p) / p)) for p in others}

    runner = OracleRunner({"catalog-api": unpacked(tmp_path / "bundles", "catalog-api")}, "pass")
    forced = migrate(
        exports, results, "--force", "--only", "catalog-api", "--mock-backends", "--max-fix-attempts", "0",
        stages=stages_with(runner),
    )
    assert forced.code == 0, forced.err
    assert runner.started == ["catalog-api"], runner.started
    assert bucket_of(results, "catalog-api") == "verified"
    assert not (results / "needs-review" / "catalog-api").exists()
    assert count_map(read_summary(results), "buckets") == {"verified": 1, "needs-review": 4, "unsupported": 1}
    for proxy in others:
        assert (read_report(results, proxy), mule_app_files(results / bucket_of(results, proxy) / proxy)) == snapshot[
            proxy
        ], proxy


# ---------------------------------------------------------------- CP9-T18


def test_CP9_T18_an_api_key_in_the_environment_never_appears_in_any_output_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[CP9-T18] With ANTHROPIC_API_KEY set to a sentinel (provider still fake), no file under results/ contains it."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", SENTINEL)
    run = migrate(E2E_INPUT, tmp_path / "results", "--no-runtime")
    assert run.code == 0, run.err
    files = all_files(run.results)
    names = {p.name for p in files}
    assert {"REPORT.md", "SUMMARY.md", "summary.json", "run.log", "pom.xml"} <= names, sorted(names)
    leaked = [str(p.relative_to(run.results)) for p in files if SENTINEL.encode() in p.read_bytes()]
    assert leaked == []
    assert SENTINEL not in run.out + run.err


# ---------------------------------------------------------------- CP9-T26


def test_CP9_T26_with_java_and_maven_missing_a_normal_run_gives_the_same_tree_as_no_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[CP9-T26] PATH without java or mvn, MULE_HOME and A2M_MULE_HOME unset, --mock-backends and no --no-runtime:
    exit 0, the tree equals tests/golden/e2e/tree.txt, every project proxy is needs-review/static, verified/ is
    empty, and run.log says plainly that the missing tool was not found so the build was skipped (no traceback)."""
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    clear_tool_env(monkeypatch)
    monkeypatch.setenv("PATH", str(empty_bin))

    run = migrate(E2E_INPUT, tmp_path / "results", "--mock-backends")
    assert run.code == 0, run.err
    golden_tree = GOLDEN_E2E / "tree.txt"
    if not golden_tree.is_file():
        pytest.fail("tests/golden/e2e/tree.txt is missing; create it with A2M_UPDATE_GOLDEN=1 (CP9-T01)", pytrace=False)
    assert "\n".join(listing(run.results)) + "\n" == golden_tree.read_text(encoding="utf-8")
    assert bucket_contents(run.results, "verified") == []
    for proxy in PROXIES_WITH_PROJECT:
        assert bucket_of(run.results, proxy) == "needs-review", proxy
        assert verification_type(read_report(run.results, proxy)) == "static", proxy

    log = run_log(run.results)
    skip_lines = [
        line
        for line in log.splitlines()
        if re.search(r"(?i)not found|not installed", line)
        and re.search(r"(?i)skip", line)
        and re.search(r"(?i)\bjava\b", line)
        and re.search(r"(?i)\bmaven\b|\bmvn\b", line)
    ]
    assert skip_lines, log
    assert "Traceback" not in log + run.out + run.err
