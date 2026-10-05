"""Shared helpers for the CP9 tests (tests/test_e2e.py, tests/test_cp9_reports.py, tests/runtime/test_e2e_runtime.py).

Not a test module (pytest collects only ``test_*.py``). It holds:

* the fixture paths: the e2e input folder ``tests/fixtures/e2e/input`` (Test-API, GetSharedFlow, weather-api.zip,
  catalog-api, legacy-auth, js-transform, broken-proxy), the kitchen-sink proxy alone in
  ``tests/fixtures/e2e/kitchen-sink-input``, its copy with a step naming a missing policy file in
  ``tests/fixtures/e2e/kitchen-sink-missing-policy-input`` and the canned fake-AI answer folders
  ``tests/fixtures/llm/cp9-low`` and ``tests/fixtures/llm/cp9-high`` (``javascript.JS-Reshape.json`` with confidence low or high, used through
  ``A2M_FAKE_LLM_DIR``);
* :data:`EXPECTED_ROWS`, the literal per-kind row counts of every e2e proxy, written by hand once here and used by
  CP9-T06, CP9-T07 and CP9-T19; :func:`xml_counts` counts the same things straight from the bundle XML with
  ElementTree (never a2m's parser), so a fixture edit that breaks either side is caught;
* readers for what a2m writes (REPORT.md tables and sections, SUMMARY.md tables, summary.json, the results tree),
  and the golden-copy check (``tests/golden/e2e``, regenerated only with ``A2M_UPDATE_GOLDEN=1``);
* the fake runners (CP7's Runner protocol, :mod:`a2m.verify.model`): :class:`OracleRunner` answers every battery
  call with exactly what a2m's own battery expects (so "battery ran and every case passed"), or answers one chosen
  case wrong, or only builds; :class:`GoldenRunner` answers recorded golden exchanges;
* :func:`stages_with`: the default pipeline (``a2m.engine.default_stages()``) with only its verification stage
  replaced by one using the given runner, so whatever else the default pipeline does (reports, buckets) still runs.

Public contract the CP9 tests pin (CP9 plan; files_likely a2m/buckets.py, a2m/report.py, a2m/summary.py,
a2m/engine.py, README.md), all observed through ``a2m.cli.main`` and the files it writes:

    results/ holds SUMMARY.md, summary.json, run.log and the folders verified/, needs-review/, unsupported/ (always
    created), plus CP1's own dot files (.a2m-results, .a2m-lock). Each proxy has exactly one folder
    results/<bucket>/<proxy>/ with REPORT.md; verified and needs-review proxies also have mule-app/ (with pom.xml),
    needs-review proxies also have diffs/ (possibly empty); unsupported proxies have REPORT.md only. A finished proxy
    has results/<bucket>/<proxy>/.done; a crashed or refused one has none. Shared flow bundles get no folder.

    REPORT.md has one Markdown table whose header has (case-insensitive) an item column (header containing "item"),
    a "kind" column (values step, policy, condition), a policy "type" column, a "Mule" result column and a "method"
    column (values template, ai or skipped, optionally followed by detail such as "(low)"). Pipes inside cells are
    escaped as ``\\|``. It has a line with "Verification type" naming golden, battery, static or failed (on that line
    or the next non-empty one), a "Test results" section, an "AI fix attempts" section, and for needs-review proxies
    a "Question for a human" and a "Suggested fix"; a verified proxy's report has no "Question for a human".

    summary.json:
        {"buckets": {"verified": int, "needs-review": int, "unsupported": int},
         "policy_types": {"<Apigee policy type>": int, ...},
         "verification_types": {"golden": int, "battery": int, "static": int, "failed": int},
         "proxies": [{"name": str, "bucket": str, "verification_type": str | null,
                      "rows": {"step": int, "policy": int, "condition": int}}, ...]}
    (``proxies`` may also be an object keyed by proxy name holding the same fields.) Run-metadata timestamps may
    appear anywhere; they are removed before golden comparison.

    SUMMARY.md has three tables whose first header cell names "bucket", "policy type" and "verification type"; each
    row is a name and its count (the last cell that is a whole number).
"""

from __future__ import annotations

import contextlib
import difflib
import http.client
import io
import json
import os
import re
import shutil
import threading
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import pytest

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures"
E2E_INPUT = FIXTURES / "e2e" / "input"
KITCHEN_INPUT = FIXTURES / "e2e" / "kitchen-sink-input"
# The same kitchen-sink proxy plus one step naming Missing-Policy, which has no file: the bundle reader refuses it
# (locked CP2-T13), so it must land in unsupported/ with a report naming Missing-Policy (CP9-T07).
KITCHEN_MISSING_INPUT = FIXTURES / "e2e" / "kitchen-sink-missing-policy-input"
KITCHEN_MISSING = "kitchen-sink-missing-policy"
LLM_LOW = FIXTURES / "llm" / "cp9-low"
LLM_HIGH = FIXTURES / "llm" / "cp9-high"
GOLDEN_E2E = REPO / "tests" / "golden" / "e2e"
README = REPO / "README.md"
UPDATE_GOLDEN_ENV = "A2M_UPDATE_GOLDEN"
FAKE_LLM_ENV = "A2M_FAKE_LLM_DIR"
SENTINEL = "sk-ant-SENTINEL-do-not-leak"
EM_DASH = chr(0x2014)  # the em dash, written as a code point so this file holds none

BUCKETS = ("verified", "needs-review", "unsupported")
VERIFICATION_TYPES = ("golden", "battery", "static", "failed")
METHODS = ("template", "ai", "skipped")
KINDS = ("step", "policy", "condition")
PROXIES_WITH_PROJECT = ("Test-API", "weather-api", "catalog-api", "legacy-auth", "js-transform")
ALL_PROXIES = (*PROXIES_WITH_PROJECT, "broken-proxy")
CP1_DOT_FILES = frozenset({".a2m-results", ".a2m-lock"})
TOP_LEVEL = frozenset({"SUMMARY.md", "summary.json", "run.log", *BUCKETS})
JS_LOW_NOTE = "CP9 canned answer: reshape.js read as one DataWeave variable step, field names guessed from the script"
NEEDS_TOOLS_QUESTION = "run with java, maven and mule to verify"
# Per-policy-type counts of the e2e input (CP9-T14): one SpikeArrest (weather-api), two VerifyAPIKey (weather-api,
# catalog-api), one OAuthV2 (legacy-auth), one Javascript (js-transform), one KeyValueMapOperations (GetSharedFlow,
# counted for Test-API, which calls it).
E2E_POLICY_TYPES = {"SpikeArrest": 1, "VerifyAPIKey": 2, "OAuthV2": 1, "Javascript": 1, "KeyValueMapOperations": 1}


# ================================================================ the literal row counts (CP9-T06, T07, T19)


@dataclass(frozen=True)
class RowCounts:
    step: int
    policy: int
    condition: int

    def as_dict(self) -> dict[str, int]:
        return {"step": self.step, "policy": self.policy, "condition": self.condition}


# Counted by hand from the fixture files. Test-API includes the GetSharedFlow items it calls through its FlowCallout
# (one step, one policy). weather-api: SA-Limit, VK-Check, AM-AddHeader (PreFlow), EV-City (Flow forecast),
# RF-NotFound (FaultRule) and the conditions of Flow forecast and RouteRule beta. legacy-auth: BA-Decode, OA-Verify
# (condition request.verb = "DELETE"). js-transform: AM-Stamp, JS-Reshape (Flow reshape and its condition).
# kitchen-sink: steps CT-Custom, AM-Form, AM-Verb; policy files CT-Custom, AM-Unused, AM-Form,
# AM-Verb; conditions of AM-Form and AM-Verb.
EXPECTED_ROWS: dict[str, RowCounts] = {
    "Test-API": RowCounts(step=2, policy=2, condition=0),
    "weather-api": RowCounts(step=5, policy=5, condition=2),
    "catalog-api": RowCounts(step=2, policy=2, condition=0),
    "legacy-auth": RowCounts(step=2, policy=2, condition=1),
    "js-transform": RowCounts(step=2, policy=2, condition=1),
    "kitchen-sink": RowCounts(step=3, policy=4, condition=2),
}


# ================================================================ counting straight from the bundle XML


@dataclass
class _BundleFiles:
    """The files of one bundle (folder or zip) as {posix path relative to the bundle root folder: bytes}."""

    root: str  # "apiproxy" or "sharedflowbundle"
    files: dict[str, bytes]

    def xml(self, rel: str) -> ET.Element:
        return ET.fromstring(self.files[rel].decode("utf-8-sig"))

    def under(self, folder: str) -> list[str]:
        prefix = f"{folder}/"
        return sorted(rel for rel in self.files if rel.startswith(prefix) and "/" not in rel[len(prefix):])


def _load_bundle(path: Path) -> _BundleFiles:
    files: dict[str, bytes] = {}
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if not info.is_dir():
                    files[info.filename] = zf.read(info)
    else:
        for item in sorted(path.rglob("*")):
            if item.is_file():
                files[item.relative_to(path).as_posix()] = item.read_bytes()
    roots = {PurePosixPath(rel).parts[0] for rel in files}
    root = "apiproxy" if "apiproxy" in roots else "sharedflowbundle"
    return _BundleFiles(root, {rel[len(root) + 1:]: data for rel, data in files.items() if rel.startswith(root + "/")})


def _find_bundle(input_dir: Path, name: str) -> Path:
    for candidate in (input_dir / name, input_dir / f"{name}.zip"):
        if candidate.exists():
            return candidate
    raise AssertionError(f"no bundle named {name} in {input_dir}")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _counts_of(bundle: _BundleFiles, endpoint_folders: Sequence[str]) -> tuple[int, int, int, list[str]]:
    steps = conditions = 0
    callouts: list[str] = []
    for folder in endpoint_folders:
        for rel in bundle.under(folder):
            if not rel.endswith(".xml"):
                continue
            for element in bundle.xml(rel).iter():
                tag = _local(element.tag)
                if tag == "Step":
                    steps += 1
                elif tag == "Condition" and (element.text or "").strip():
                    conditions += 1
    policy_files = [rel for rel in bundle.under("policies") if rel.endswith(".xml")]
    for rel in policy_files:
        root = bundle.xml(rel)
        if _local(root.tag) == "FlowCallout":
            for element in root.iter():
                if _local(element.tag) == "SharedFlowBundle" and (element.text or "").strip():
                    callouts.append((element.text or "").strip())
    return steps, len(policy_files), conditions, callouts


def xml_counts(input_dir: Path, name: str) -> RowCounts:
    """Steps, policy files and non-empty conditions of proxy ``name`` in ``input_dir``, plus those of every shared
    flow it calls through a FlowCallout (found in the same folder), counted from the raw XML with ElementTree."""
    bundle = _load_bundle(_find_bundle(input_dir, name))
    steps, policies, conditions, callouts = _counts_of(bundle, ("proxies", "targets"))
    seen: set[str] = set()
    while callouts:
        shared = callouts.pop()
        if shared in seen:
            continue
        seen.add(shared)
        flow = _load_bundle(_find_bundle(input_dir, shared))
        s, p, c, more = _counts_of(flow, ("sharedflows",))
        steps, policies, conditions = steps + s, policies + p, conditions + c
        callouts.extend(more)
    return RowCounts(step=steps, policy=policies, condition=conditions)


# ================================================================ Markdown readers


_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")


def split_row(line: str) -> list[str]:
    """The cells of one Markdown table row, split only on unescaped pipes; ``\\|`` is kept as a literal ``|``."""
    text = line.strip()
    text = text.removeprefix("|")
    if text.endswith("|") and not text.endswith("\\|"):
        text = text[:-1]
    return [cell.strip().replace("\\|", "|") for cell in _UNESCAPED_PIPE.split(text)]


def _is_separator(cells: Sequence[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells)


@dataclass
class Table:
    header: list[str]
    rows: list[list[str]]
    raw_rows: list[str] = field(default_factory=list)

    def col(self, *needles: str) -> int:
        """The index of the first header cell containing any of ``needles`` (case-insensitive)."""
        for index, cell in enumerate(self.header):
            low = cell.lower()
            if any(needle in low for needle in needles):
                return index
        raise AssertionError(f"no column matching {needles} in table header {self.header}")


def tables(text: str) -> list[Table]:
    """Every Markdown table in ``text`` (a header row, a separator row, then rows)."""
    lines = text.splitlines()
    found: list[Table] = []
    i = 0
    while i < len(lines) - 1:
        line = lines[i]
        if line.strip().startswith("|") and _is_separator(split_row(lines[i + 1])):
            header = split_row(line)
            rows: list[list[str]] = []
            raw: list[str] = []
            j = i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                rows.append(split_row(lines[j]))
                raw.append(lines[j])
                j += 1
            found.append(Table(header, rows, raw))
            i = j
        else:
            i += 1
    return found


def policy_table(report_text: str) -> Table:
    """The report's policy table: the one table whose header has both a kind and a method column."""
    found = [
        t
        for t in tables(report_text)
        if any("kind" in c.lower() for c in t.header) and any("method" in c.lower() for c in t.header)
    ]
    assert len(found) == 1, f"expected exactly one table with 'kind' and 'method' columns, found {len(found)}:\n{report_text}"
    return found[0]


def clean(cell: str) -> str:
    return re.sub(r"[`*_]", "", cell).strip()


def method_of(cell: str) -> str:
    """The method word of a method cell ('ai (low)' -> 'ai'); the raw first word when it is not a known method."""
    words = re.findall(r"[a-z-]+", clean(cell).lower())
    return words[0] if words else ""


def kind_of(cell: str) -> str:
    return clean(cell).lower()


def rows_by_kind(table: Table) -> dict[str, list[list[str]]]:
    kind = table.col("kind")
    grouped: dict[str, list[list[str]]] = {k: [] for k in KINDS}
    for row in table.rows:
        grouped.setdefault(kind_of(row[kind]), []).append(row)
    return grouped


def kind_counts(report_text: str) -> RowCounts:
    grouped = rows_by_kind(policy_table(report_text))
    return RowCounts(step=len(grouped["step"]), policy=len(grouped["policy"]), condition=len(grouped["condition"]))


def item_rows(table: Table, item: str) -> list[list[str]]:
    """Rows whose item cell names ``item`` (exact, ignoring Markdown emphasis)."""
    col = table.col("item")
    return [row for row in table.rows if clean(row[col]) == item]


def row_text(row: Sequence[str]) -> str:
    return " | ".join(row)


_TYPE_WORDS = re.compile(r"\b(golden|battery|static|failed)\b", re.IGNORECASE)


def verification_type(report_text: str) -> str | None:
    """The verification type the report states: the first of golden/battery/static/failed after 'Verification type'
    on its line, or on the next non-empty line when that line only holds the label (a heading)."""
    lines = report_text.splitlines()
    for index, line in enumerate(lines):
        low = line.lower()
        if "verification type" not in low:
            continue
        after = line[low.index("verification type") + len("verification type"):]
        match = _TYPE_WORDS.search(after)
        if match:
            return match.group(1).lower()
        for following in lines[index + 1:]:
            if following.strip():
                match = _TYPE_WORDS.search(following)
                return match.group(1).lower() if match else None
        return None
    return None


_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


def section(text: str, title: str) -> str | None:
    """The text under the first heading (or label line) containing ``title`` (case-insensitive), up to the next
    heading of the same or a higher level (any heading, when the title is a label line, not a heading)."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if title.lower() not in line.lower():
            continue
        heading = _HEADING.match(line.strip())
        level = len(heading.group(1)) if heading else 7
        body = [] if heading else [line]
        for following in lines[index + 1:]:
            other = _HEADING.match(following.strip())
            if other and len(other.group(1)) <= level:
                break
            body.append(following)
        return "\n".join(body)
    return None


def normalize_space(text: str) -> str:
    return " ".join(text.split()).lower()


# ================================================================ results readers


def proxy_folders(results: Path) -> dict[str, list[str]]:
    """{proxy name: [bucket, ...]} for every folder under the three buckets (a proxy in two buckets shows twice)."""
    found: dict[str, list[str]] = {}
    for bucket in BUCKETS:
        folder = results / bucket
        if not folder.is_dir():
            continue
        for entry in sorted(folder.iterdir()):
            if entry.is_dir():
                found.setdefault(entry.name, []).append(bucket)
    return found


def bucket_of(results: Path, proxy: str) -> str:
    buckets = proxy_folders(results).get(proxy, [])
    assert len(buckets) == 1, f"{proxy} is in {buckets or 'no bucket'}; layout: {listing(results)}"
    return buckets[0]


def bucket_contents(results: Path, bucket: str) -> list[str]:
    folder = results / bucket
    assert folder.is_dir(), f"missing bucket folder {bucket}/; layout: {listing(results)}"
    return sorted(entry.name for entry in folder.iterdir() if entry.is_dir())


def report_path(results: Path, proxy: str) -> Path:
    return results / bucket_of(results, proxy) / proxy / "REPORT.md"


def read_report(results: Path, proxy: str) -> str:
    path = report_path(results, proxy)
    assert path.is_file(), f"missing {path}; layout: {listing(results)}"
    return path.read_text(encoding="utf-8")


def read_summary(results: Path) -> dict[str, Any]:
    path = results / "summary.json"
    assert path.is_file(), f"missing {path}; layout: {listing(results)}"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), data
    return data


def summary_proxies(data: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    proxies = data.get("proxies")
    if isinstance(proxies, dict):
        return {str(k): dict(v) for k, v in proxies.items()}
    assert isinstance(proxies, list), f"summary.json has no 'proxies' list or object: {data}"
    return {str(entry["name"]): dict(entry) for entry in proxies}


def count_map(data: Mapping[str, Any], key: str) -> dict[str, int]:
    value = data.get(key)
    assert isinstance(value, dict), f"summary.json has no '{key}' object: {data}"
    return {str(k): int(v) for k, v in value.items()}


def summary_md_table(text: str, first_header: str) -> dict[str, int]:
    """{name: count} from the SUMMARY.md table whose first header cell contains ``first_header``."""
    for table in tables(text):
        if table.header and first_header.lower() in table.header[0].lower():
            counts: dict[str, int] = {}
            for row in table.rows:
                numbers = [clean(cell) for cell in row[1:] if re.fullmatch(r"\d+", clean(cell))]
                assert numbers, f"row {row} of the {first_header} table has no count"
                counts[clean(row[0])] = int(numbers[-1])
            return counts
    raise AssertionError(f"SUMMARY.md has no table whose first header cell names {first_header!r}:\n{text}")


def run_log(results: Path) -> str:
    return (results / "run.log").read_text(encoding="utf-8", errors="replace")


def listing(results: Path) -> list[str]:
    """Sorted relative paths of every file and folder under ``results`` (folders end with '/')."""
    if not results.exists():
        return []
    paths = []
    for path in results.rglob("*"):
        rel = path.relative_to(results).as_posix()
        paths.append(rel + "/" if path.is_dir() and not path.is_symlink() else rel)
    return sorted(paths)


def all_files(results: Path) -> list[Path]:
    return sorted(p for p in results.rglob("*") if p.is_file() and not p.is_symlink())


def mule_app_files(folder: Path) -> dict[str, bytes]:
    app = folder / "mule-app"
    return {p.relative_to(app).as_posix(): p.read_bytes() for p in all_files(app)} if app.is_dir() else {}


# ================================================================ golden copies


_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?(?:\s?(?:Z|UTC|[+-]\d{2}:?\d{2}))?"
)


def strip_timestamps(text: str) -> str:
    """``text`` with run-metadata timestamps (ISO-8601 date and time, optional seconds, fraction and zone) replaced."""
    return _TIMESTAMP.sub("<timestamp>", text)


def normalized_json(text: str) -> str:
    """summary.json with timestamps removed, as canonical JSON (sorted keys, 2-space indent, trailing newline)."""
    return json.dumps(json.loads(strip_timestamps(text)), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def updating_golden() -> bool:
    return os.environ.get(UPDATE_GOLDEN_ENV) == "1"


def check_golden(rel: str, actual: str) -> None:
    """Compare ``actual`` with tests/golden/e2e/<rel>. With A2M_UPDATE_GOLDEN=1 the copy is (re)written instead;
    otherwise a missing copy or a mismatch fails, and the copy is never touched."""
    path = GOLDEN_E2E / rel
    if updating_golden():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8")
        return
    if not path.is_file():
        pytest.fail(
            f"golden copy {path.relative_to(REPO)} is missing; run the test once with {UPDATE_GOLDEN_ENV}=1 to create "
            "it, then review and commit it",
            pytrace=False,
        )
    expected = path.read_text(encoding="utf-8")
    if actual != expected:
        diff = "".join(
            difflib.unified_diff(
                expected.splitlines(keepends=True), actual.splitlines(keepends=True), f"golden/{rel}", f"actual/{rel}"
            )
        )
        pytest.fail(
            f"{rel} differs from its golden copy (rerun with {UPDATE_GOLDEN_ENV}=1 only if the change is intended):\n"
            + diff[:6000],
            pytrace=False,
        )


# ================================================================ running a2m


@dataclass
class Run:
    code: int
    out: str
    err: str
    results: Path


@contextlib.contextmanager
def fake_llm(folder: Path | None) -> Iterator[None]:
    """A2M_FAKE_LLM_DIR set to ``folder`` (unset when None) for the block, restored afterwards."""
    with pytest.MonkeyPatch.context() as patch:
        if folder is None:
            patch.delenv(FAKE_LLM_ENV, raising=False)
        else:
            patch.setenv(FAKE_LLM_ENV, str(folder))
        yield


def migrate(
    input_dir: Path,
    results: Path,
    *extra: str,
    stages: Sequence[Callable[[Any], None]] | None = None,
    llm_dir: Path | None = LLM_LOW,
) -> Run:
    """``a2m migrate <input_dir> --out <results> --llm fake <extra...>`` in-process through ``a2m.cli.main``, with
    stdout and stderr captured, and A2M_FAKE_LLM_DIR pointed at ``llm_dir``."""
    from a2m.cli import main

    out, err = io.StringIO(), io.StringIO()
    argv = ["migrate", str(input_dir), "--out", str(results), "--llm", "fake", *extra]
    with fake_llm(llm_dir), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main(argv) if stages is None else main(argv, stages=list(stages))
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
    return Run(code, out.getvalue(), err.getvalue(), results)


def input_with(tmp: Path, *names: str, source: Path = E2E_INPUT) -> Path:
    """A fresh input folder under ``tmp`` holding real copies (never links) of the named e2e items."""
    folder = tmp / "input"
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        for candidate in (source / name, source / f"{name}.zip"):
            if candidate.is_dir():
                shutil.copytree(candidate, folder / candidate.name)
            elif candidate.is_file():
                shutil.copyfile(candidate, folder / candidate.name)
    return folder


def unpacked(tmp: Path, name: str) -> Path:
    """An unzipped copy of e2e item ``name`` (folder or zip) at tmp/<name>/apiproxy/..., returned as tmp/<name>."""
    src = _find_bundle(E2E_INPUT, name)
    dest = tmp / name
    if src.suffix == ".zip":
        with zipfile.ZipFile(src) as zf:
            zf.extractall(dest)
    else:
        shutil.copytree(src, dest)
    return dest


def stages_with(runner: Any) -> list[Callable[[Any], None]]:
    """The default per-proxy pipeline with only its verification stage swapped for one that uses ``runner``."""
    from a2m.engine import default_stages
    from a2m.verify import VerifyStage, make_verify_stage

    stages = list(default_stages())
    assert any(isinstance(stage, VerifyStage) for stage in stages), stages
    return [make_verify_stage(runner=runner) if isinstance(stage, VerifyStage) else stage for stage in stages]


def stages_crashing(proxy: str, message: str) -> list[Callable[[Any], None]]:
    """The default pipeline with one extra stage, right before generation, that raises RuntimeError(message) for
    ``proxy`` only (CP1's stage hook)."""
    from a2m.engine import default_stages

    def boom(context: Any) -> None:
        if context.name == proxy:
            raise RuntimeError(message)

    boom.__name__ = "boom"
    stages = list(default_stages())
    names = [getattr(stage, "__name__", "") for stage in stages]
    at = names.index("generate") if "generate" in names else 0
    return [*stages[:at], boom, *stages[at:]]


# ================================================================ fake runners (CP7 Runner protocol)


JSON_HEADERS = {"Content-Type": "application/json"}
HOP_HEADERS = frozenset({"host", "content-length", "connection", "transfer-encoding"})
SPIKE_FAULT = json.dumps(
    {
        "fault": {
            "faultstring": "Spike arrest violation. Allowed rate : 600pm",
            "detail": {"errorcode": "policies.ratelimit.SpikeArrestViolation"},
        }
    }
).encode()


def _signature(request: Any) -> tuple[str, str, bytes, tuple[tuple[str, str], ...]]:
    headers = tuple(sorted((str(k).lower(), str(v)) for k, v in dict(request.headers or {}).items()))
    return (str(request.method).upper(), str(request.path), bytes(request.body or b""), headers)


class _BuiltOnly:
    running = False
    base_url = None

    def send(self, request: Any) -> Any:
        raise AssertionError(f"the harness sent {request.method} {request.path} to an app that was only built")

    def stop(self) -> None:
        pass


@dataclass
class _AppState:
    flat: list[tuple[int, int, Any, Any]] = field(default_factory=list)  # (case index, call index, case, call)
    cursor: int = 0
    backend_sent: dict[int, int] = field(default_factory=dict)


class OracleRunner:
    """A Runner whose apps answer every battery call exactly as a2m's own battery expects.

    ``bundles`` maps each proxy name to a folder holding its unzipped bundle (``<folder>/apiproxy/...``). On start
    it reads the bundle with a2m's parser and builds the battery a2m builds for that app folder (with the generated
    steps the generate stage saved, when they can be found), then answers each incoming call with the expected
    status and body, forwarding to the mock backend (with the headers the case expects there) only for the calls
    that should reach it. Modes:

    * ``"pass"``: every case passes (battery ran, every case passed);
    * ``"built"``: the app is built but never started (``running`` False);
    * ``"fail-one"``: like pass, except the first call that expects 200 in the case of policy ``fail_policy`` and
      situation ``fail_situation`` gets 429 with a spike-arrest fault and never reaches the backend, on every run.
    """

    def __init__(
        self,
        bundles: Mapping[str, Path],
        mode: str = "pass",
        *,
        fail_policy: str | None = None,
        fail_situation: str | None = None,
    ) -> None:
        assert mode in ("pass", "built", "fail-one"), mode
        self.bundles = dict(bundles)
        self.mode = mode
        self.fail_policy = fail_policy
        self.fail_situation = fail_situation
        self.started: list[str] = []
        self.unmatched: list[str] = []
        self.failed_calls = 0
        self._states: dict[str, _AppState] = {}
        self._lock = threading.Lock()

    def start(self, app: Any, *, backend_url: str) -> Any:
        self.started.append(app.name)
        if self.mode == "built":
            return _BuiltOnly()
        from a2m.parser import read_bundle
        from a2m.verify.batteries import build_battery

        bundle = read_bundle(self.bundles[app.name], label=app.name)
        battery = build_battery(bundle, Path(app.app_dir), generated=_saved_generated(Path(app.app_dir), app.name))
        state = self._states.setdefault(app.name, _AppState())
        state.flat = [
            (case_index, call_index, case, call)
            for case_index, case in enumerate(battery.cases)
            for call_index, call in enumerate(case.calls)
        ]
        return _OracleHandle(self, state, backend_url)

    def _fails(self, case: Any, call_index: int, call: Any) -> bool:
        if self.mode != "fail-one" or case.policy != self.fail_policy or case.situation != self.fail_situation:
            return False
        first_ok = next((i for i, c in enumerate(case.calls) if c.expected_status == 200), None)
        return first_ok == call_index


def _saved_generated(app_dir: Path, name: str) -> Any:
    """The generated steps the generate stage saved for ``name``, found from the app's working copy, or None."""
    from a2m import layout
    from a2m.verify.generated import GeneratedSteps

    for parent in app_dir.parents:
        if (parent / layout.WORK_DIR_NAME).is_dir():
            path = layout.generated_steps_path(parent, name)
            if path.is_file():
                return GeneratedSteps.from_json(path.read_text(encoding="utf-8"))
            return None
    return None


class _OracleHandle:
    def __init__(self, runner: OracleRunner, state: _AppState, backend_url: str) -> None:
        self.runner = runner
        self.state = state
        self.backend_url = backend_url
        self.running = True
        self.base_url = "http://oracle.invalid/app"

    def send(self, request: Any) -> Any:
        from a2m.verify import HttpResponse

        with self.runner._lock:
            index = self._match(request)
            if index is None:
                self.runner.unmatched.append(f"{request.method} {request.path}")
                return HttpResponse(500, dict(JSON_HEADERS), b'{"oracle":"no battery call matches this request"}')
            self.state.cursor = index + 1
            case_index, call_index, case, call = self.state.flat[index]
            if call_index == 0:
                self.state.backend_sent[case_index] = 0
            if self.runner._fails(case, call_index, call):
                self.runner.failed_calls += 1
                return HttpResponse(429, dict(JSON_HEADERS), SPIKE_FAULT)
            sent = self.state.backend_sent.get(case_index, 0)
            forward = call.expected_status == 200 and sent < case.expected_backend_calls
            if forward:
                self.state.backend_sent[case_index] = sent + 1
        backend_body = self._forward(request, case.expected_backend_headers) if forward else b""
        body = call.expected_body if call.expected_body is not None else backend_body
        return HttpResponse(call.expected_status, dict(JSON_HEADERS), body)

    def _match(self, request: Any) -> int | None:
        flat = self.state.flat
        wanted = _signature(request)
        order = list(range(self.state.cursor, len(flat))) + list(range(min(self.state.cursor, len(flat))))
        return next((i for i in order if _signature(flat[i][3].request) == wanted), None)

    def _forward(self, request: Any, expected: Mapping[str, str | None]) -> bytes:
        target = urlsplit(self.backend_url)
        headers = {k: v for k, v in dict(request.headers or {}).items() if str(k).lower() not in HOP_HEADERS}
        for name, value in expected.items():
            for key in [k for k in headers if k.lower() == name.lower()]:
                del headers[key]
            if value is not None:
                headers[name] = value
        conn = http.client.HTTPConnection(target.hostname or "127.0.0.1", target.port or 80, timeout=10)
        try:
            conn.request(request.method, request.path, body=request.body or None, headers=headers)
            return conn.getresponse().read()
        finally:
            conn.close()

    def stop(self) -> None:
        self.running = False


class GoldenRunner:
    """A Runner whose apps answer each recorded golden request with its recorded response and never call the
    backend (the recordings used with it have no backend calls)."""

    def __init__(self, answers: Mapping[tuple[str, str], tuple[int, Mapping[str, str], bytes]]) -> None:
        self.answers = dict(answers)
        self.started: list[str] = []
        self.unmatched: list[str] = []

    def start(self, app: Any, *, backend_url: str) -> Any:
        self.started.append(app.name)
        runner = self

        class _Handle:
            running = True
            base_url = "http://golden.invalid/app"

            def send(self, request: Any) -> Any:
                from a2m.verify import HttpResponse

                key = (str(request.method).upper(), str(request.path))
                if key not in runner.answers:
                    runner.unmatched.append(f"{key[0]} {key[1]}")
                    return HttpResponse(500, dict(JSON_HEADERS), b'{"golden":"no recording for this request"}')
                status, headers, body = runner.answers[key]
                return HttpResponse(status, dict(headers), body)

            def stop(self) -> None:
                pass

        return _Handle()


# ================================================================ tool stubs (CP7 style)


def write_stub(path: Path, calls: Path, extra_lines: Sequence[str] = (), exit_code: int = 0) -> None:
    lines = ["#!/bin/sh", f'printf \'%s\\t%s\\t%s\\n\' "${{0##*/}}" "$PWD" "$*" >> \'{calls}\'', *extra_lines]
    lines.append(f"exit {exit_code}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o755)


def tool_calls(calls: Path) -> list[tuple[str, str, str]]:
    if not calls.exists():
        return []
    rows = []
    for line in calls.read_text(encoding="utf-8").splitlines():
        tool, cwd, args = (line.split("\t") + ["", ""])[:3]
        rows.append((tool, cwd, args))
    return rows


def clear_tool_env(patch: pytest.MonkeyPatch) -> None:
    for name in ("MULE_HOME", "A2M_MULE_HOME", "JAVA_HOME", "MAVEN_HOME", "M2_HOME", "MULE_BASE"):
        patch.delenv(name, raising=False)
