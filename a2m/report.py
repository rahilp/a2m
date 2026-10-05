"""The report stage: each proxy's REPORT.md, diffs/ and bucket, and the batch summary at the end.

:class:`ReportStage` is the last stage of the default pipeline. For each proxy
it joins the bundle's IR with the generator's records (:mod:`a2m.inventory`)
and the verification result the verification stage wrote (verification.json),
decides the bucket (:mod:`a2m.buckets`) and writes, in the proxy's folder:

* ``REPORT.md``: one table row per step, policy and condition with its Mule
  result and method, the verification type, the test results, the AI fix
  attempts, and for a needs-review proxy a question for a human and a
  suggested fix for each thing that keeps it out of verified;
* ``diffs/`` (needs-review only, possibly empty): the diff of each failing
  test, the end of the build or runtime log of a failed build or deploy, and
  the diff of each AI fix attempt;
* the facts the batch summary reads (:data:`a2m.layout.PROXY_SUMMARY_NAME`).

The engine finds this stage by duck typing (it has :meth:`ReportStage.finish_batch`),
moves the proxy's folder into ``<bucket>/`` once every stage succeeded, and
calls :meth:`ReportStage.write_unsupported` for a proxy it refused or that
failed, and :meth:`ReportStage.finish_batch` at the end of the batch.

Every text written goes through the proxy's :class:`~a2m.verify.masking.Masker`
(as verification.json does), the results folder's absolute path is shown as
``<results>``, a callout's source code is never written, and REPORT.md holds no
timestamp.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from a2m import layout, safefs
from a2m.buckets import Bucket, decide
from a2m.errors import BundleError
from a2m.inventory import GenerateRecords, Inventory, OtherItem, Row, RowKind, build_inventory
from a2m.ir import Bundle
from a2m.parser import read_bundle
from a2m.policies.common import Method
from a2m.runlog import get_logger
from a2m.summary import ProxySummary, proxy_facts_json, write_batch_summary
from a2m.verify.batteries import TIME_WINDOW_TYPES
from a2m.verify.fix_loop import FixAttempt
from a2m.verify.harness import VERIFICATION_FILE
from a2m.verify.masking import Masker
from a2m.verify.model import (
    CaseResult,
    ReviewFlag,
    UntestedPolicy,
    VerificationResult,
    VerificationType,
    fold,
    shout,
)

STAGE_NAME = "report"
VERIFICATION_LOG_NAME = "verification-log.txt"
UNSAFE_FILE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
TYPE_MEANING = {
    VerificationType.GOLDEN: "the app ran and every response matched the recorded Apigee responses",
    VerificationType.BATTERY: "the app ran and passed a2m's policy tests against a local mock backend",
    VerificationType.STATIC: "the app was generated, and maybe built, but never run against tests",
    VerificationType.FAILED: "the build, the deploy or at least one test failed",
}
NO_VERIFY_MESSAGE = "not verified: no verification stage ran for this proxy"
GOLDEN_COVERAGE = (
    "Not measured for golden runs: the recorded exchanges matched, but a2m does not know which policies those "
    "exchanges exercised, so a policy step the recordings never reached was not tested. The generated policy "
    "steps are listed below so a person can judge which of them the recordings cover."
)


class ReportError(Exception):
    """What the report needs from earlier stages is missing (the proxy then fails and is reported unsupported)."""


class ReportContext(Protocol):
    """What the engine passes each stage (see a2m.engine.ProxyContext)."""

    @property
    def name(self) -> str: ...

    @property
    def source_name(self) -> str: ...

    @property
    def bundle_dir(self) -> Path: ...

    @property
    def out_dir(self) -> Path: ...

    @property
    def shared_flows(self) -> tuple[Bundle, ...]: ...


@dataclass(frozen=True, slots=True)
class Question:
    question: str
    suggestion: str


class _Text:
    """Masks and cleans every text before it is written: credentials masked, the results path shown as <results>."""

    def __init__(self, masker: Masker, roots: Sequence[tuple[Path, str]]) -> None:
        self.masker = masker
        pairs: list[tuple[str, str]] = []
        for path, label in roots:
            for form in {str(path), str(path.absolute())}:
                pairs.append((form, label))
            try:
                pairs.append((str(path.resolve()), label))
            except (OSError, RuntimeError):
                pass
        self.roots = sorted(set(pairs), key=lambda pair: -len(pair[0]))

    def __call__(self, text: str) -> str:
        for path, label in self.roots:
            if path and path != "/":
                text = text.replace(path, label)
        return self.masker.mask(text)

    def cell(self, text: str) -> str:
        """``text`` as one Markdown table cell: one line, pipes escaped, '-' when empty."""
        shown = " ".join(self(text).split())
        return shown.replace("|", "\\|") if shown else "-"

    def line(self, text: str) -> str:
        return " ".join(self(text).split())


# ---------------------------------------------------------------- reading what earlier stages wrote


def load_records(results_root: Path, name: str) -> GenerateRecords:
    path = layout.generate_records_path(results_root, name)
    try:
        if safefs.is_link(path) or not path.is_file():
            raise ReportError(f"the generator's records for {name} are missing, so no report can be written")
        return GenerateRecords.from_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ReportError(f"the generator's records for {name} cannot be read: {exc}") from exc


def _cases(data: Any) -> tuple[CaseResult, ...]:
    if not isinstance(data, list):
        return ()
    return tuple(
        CaseResult(
            name=str(c.get("name", "")),
            situation=c.get("situation"),
            passed=bool(c.get("passed")),
            diff=str(c.get("diff", "")),
            backend_calls=int(c.get("backend_calls", 0) or 0),
            policy=c.get("policy"),
            policy_type=c.get("policy_type"),
        )
        for c in data
        if isinstance(c, dict)
    )


def load_verification(proxy_dir: Path) -> tuple[VerificationResult, tuple[FixAttempt, ...]]:
    """The verification result and fix attempts in ``proxy_dir``'s verification.json; static when there is none."""
    path = proxy_dir / VERIFICATION_FILE
    try:
        if safefs.is_link(path) or not path.is_file():
            return VerificationResult(VerificationType.STATIC, message=NO_VERIFY_MESSAGE), ()
        data = json.loads(path.read_text(encoding="utf-8"))
        vtype = VerificationType(data["type"])
    except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
        return VerificationResult(VerificationType.STATIC, message=f"not verified: verification.json unreadable ({exc})"), ()
    result = VerificationResult(
        type=vtype,
        ran=int(data.get("ran", 0) or 0),
        passed=int(data.get("passed", 0) or 0),
        failed=int(data.get("failed", 0) or 0),
        cases=_cases(data.get("cases")),
        untested=tuple(
            UntestedPolicy(str(u.get("name", "")), str(u.get("policy_type", "")), str(u.get("reason", "")))
            for u in data.get("untested", ()) if isinstance(u, dict)
        ),
        review_flags=tuple(
            ReviewFlag(str(f.get("policy", "")), str(f.get("policy_type", "")), str(f.get("reason", "")))
            for f in data.get("review_flags", ()) if isinstance(f, dict)
        ),
        message=str(data.get("message", "")),
        log_excerpt=str(data.get("log_excerpt", "")),
    )
    attempts = tuple(
        FixAttempt(
            number=int(a.get("number", index + 1)),
            changed_files=tuple(str(f) for f in a.get("changed_files", ()) or ()),
            diff=str(a.get("diff", "")),
            failing_before=int(a.get("failing_before", 0) or 0),
            failing_after=int(a.get("failing_after", 0) or 0),
            helped=bool(a.get("helped")),
            reason=str(a.get("reason", "")),
            changed_steps=tuple(str(s) for s in a.get("changed_steps", ()) or ()),
            confidence=a.get("confidence"),
        )
        for index, a in enumerate(data.get("attempts", ()) or ())
        if isinstance(a, dict)
    )
    return result, attempts


# ---------------------------------------------------------------- the report


@dataclass(frozen=True, slots=True)
class ProxyReport:
    name: str
    source: str
    bucket: Bucket
    inventory: Inventory
    result: VerificationResult
    attempts: tuple[FixAttempt, ...]

    def summary(self) -> ProxySummary:
        types: dict[str, int] = {}
        for kind in self.inventory.policy_types:
            types[kind] = types.get(kind, 0) + 1
        return ProxySummary(self.name, self.bucket.value, self.result.type.value, self.inventory.counts(), types)


def diff_files(report: ProxyReport) -> dict[str, str]:
    """{file name in diffs/: text} for a needs-review proxy: failing tests, the build or runtime log, fix diffs."""
    files: dict[str, str] = {}

    def add(stem: str, suffix: str, text: str) -> str:
        base = UNSAFE_FILE_CHARS.sub("-", stem).strip("-.") or "item"
        name, number = f"{base}{suffix}", 2
        while name in files:
            name, number = f"{base}-{number}{suffix}", number + 1
        files[name] = text if text.endswith("\n") else text + "\n"
        return name

    for case in report.result.cases:
        if not case.passed:
            head = f"test {case.name} failed" + (f" (policy {case.policy})" if case.policy else "")
            add(f"test-{case.name}", ".diff", f"{head}\n{case.diff}")
    if report.result.type is VerificationType.FAILED and report.result.log_excerpt.strip():
        add(
            VERIFICATION_LOG_NAME.rsplit(".", 1)[0],
            ".txt",
            f"{report.result.message}\n\n{report.result.log_excerpt}",
        )
    for attempt in report.attempts:
        if attempt.diff.strip():
            add(f"fix-attempt-{attempt.number}", ".diff", attempt.diff)
    return files


def _case_diff_names(report: ProxyReport) -> dict[str, str]:
    names = diff_files(report)
    found: dict[str, str] = {}
    for file_name, text in names.items():
        first = text.split("\n", 1)[0]
        for case in report.result.cases:
            if not case.passed and first.startswith(f"test {case.name} failed") and case.name not in found:
                found[case.name] = file_name
    return found


def questions(report: ProxyReport) -> list[Question]:
    """One question for a human (and a suggested fix) per thing that keeps a needs-review proxy out of verified."""
    name = report.name
    result = report.result
    rerun = f"then rerun a2m with --force --only {name}"
    found: list[Question] = []
    if result.type is VerificationType.STATIC:
        if "Enterprise" in result.message:
            found.append(
                Question(
                    f"This app was not run against tests ({result.message}), so nothing shows it behaves like the "
                    "Apigee proxy. Run on a Mule Enterprise runtime to verify.",
                    "Deploy mule-app/ on a Mule Enterprise runtime and test it there, or replace the Enterprise "
                    f"components so it runs on Mule Kernel (Community Edition), {rerun}.",
                )
            )
        else:
            found.append(
                Question(
                    f"This app was not run against tests ({result.message}), so nothing shows it behaves like the "
                    "Apigee proxy. Run with Java, Maven and Mule to verify.",
                    "Install the local toolchain (Temurin 17, Maven 3.9 and Mule Kernel CE 4.9.0, see the README), "
                    f"{rerun} --mock-backends (or --golden), without --no-runtime.",
                )
            )
    elif result.type is VerificationType.FAILED and not result.cases:
        log = f"diffs/{VERIFICATION_LOG_NAME}" if result.log_excerpt.strip() else "run.log"
        found.append(
            Question(
                f"The app could not be built or started: {result.message}. What in the generated project breaks "
                "the build or the deploy?",
                f"Read {log} (the end of the Maven or Mule log), fix the project in mule-app/, {rerun}.",
            )
        )
    if result.type is VerificationType.FAILED and result.cases:
        files = _case_diff_names(report)
        for case in result.cases:
            if case.passed:
                continue
            first = next((line.strip() for line in case.diff.splitlines() if line.strip()), "no details")
            what = f"policy {case.policy}" if case.policy else "a recorded Apigee exchange"
            found.append(
                Question(
                    f"Test {case.name} ({what}) still fails: {first}. Is the Mule behaviour wrong, or does the "
                    "test expect the wrong thing?",
                    (
                        f"Compare diffs/{files.get(case.name, '')} with "
                        + (f"the Apigee policy {case.policy}" if case.policy else "the recording")
                        + f", change the generated step in mule-app/src/main/mule/, {rerun}."
                    ),
                )
            )
    elif result.type in (VerificationType.GOLDEN, VerificationType.BATTERY) and result.failed:
        found.append(Question(f"{result.failed} tests failed: {result.message}. Why?", f"Fix mule-app/, {rerun}."))

    rows = report.inventory.rows
    for row in rows:
        if row.derived:
            continue
        if row.method is Method.SKIPPED:
            found.append(_skipped_question(row, rerun))
        elif row.ai_unsure:
            found.append(_ai_question(row, rerun))
    for other in report.inventory.others:
        found.append(_other_question(other, rows, rerun))
    for flag in result.review_flags:
        if flag.policy_type in TIME_WINDOW_TYPES:
            found.append(
                Question(
                    f"{flag.policy} ({flag.policy_type}) is a time-window policy: {flag.reason}. Its timing "
                    "behaviour was not proven by the tests; does the Mule app limit calls the same way over time?",
                    f"Check {flag.policy} by hand against the deployed app (for example a short load test at the "
                    "configured rate), and compare when the window starts and resets with Apigee.",
                )
            )
        else:
            found.append(
                Question(
                    f"{flag.policy} ({flag.policy_type}) needs review: {flag.reason}.",
                    f"Check {flag.policy} in mule-app/src/main/mule/ against the Apigee policy and test it by hand.",
                )
            )
    if not found and report.bucket is Bucket.NEEDS_REVIEW:
        found.append(
            Question(
                f"The app is not verified ({result.type.value}: {result.message}). Does it behave like the Apigee proxy?",
                f"Test mule-app/ by hand, {rerun}.",
            )
        )
    return found


def _skipped_question(row: Row, rerun: str) -> Question:
    if row.kind is RowKind.CONDITION:
        return Question(
            f"The condition of {row.item} ({row.original}) was not translated: {row.reason}. What should it be in "
            "DataWeave?",
            f"Write the DataWeave expression by hand in the generated 'when' of {row.item} in "
            f"mule-app/src/main/mule/ (it is #[false] now, so what it guards never runs), {rerun}.",
        )
    if row.kind is RowKind.POLICY:
        return Question(
            f"Policy {row.item} ({row.type}) is not used by any step, so nothing was generated for it. Is it needed?",
            "If Apigee never runs it either, nothing needs to change; otherwise attach it to the right flow and "
            "migrate it by hand.",
        )
    if row.type == "OAuthV2":
        fix = (
            "a2m does not migrate OAuthV2: protect the API with a Mule OAuth 2.0 provider or an API Manager OAuth "
            "policy, or remove the step if it is not needed"
        )
    else:
        fix = (
            f"write the Mule equivalent of {row.type} {row.item} by hand in mule-app/src/main/mule/, or remove the "
            "step if it is not needed"
        )
    return Question(
        f"Step {row.item} ({row.type}) at {row.where} was not migrated: {row.reason}. How should it be done in Mule?",
        f"{fix if fix.startswith('a2m') else shout(fix[0]) + fix[1:]}, {rerun}.",
    )


def _ai_question(row: Row, rerun: str) -> Question:
    confidence = row.confidence.value if row.confidence is not None else "no"
    what = row.notes or row.reason or "no notes"
    if row.kind is RowKind.CONDITION:
        return Question(
            f"The condition of {row.item} ({row.original}) was translated by the AI with {confidence} confidence "
            f"({what}). Does the translation behave like the original?",
            f"Compare the generated 'when' of {row.item} in mule-app/src/main/mule/ with the Apigee condition and "
            f"correct it by hand if needed, {rerun}.",
        )
    return Question(
        f"{row.item} ({row.type}) was translated by the AI with {confidence} confidence ({what}). Does the "
        "translation behave like the original code?",
        f"Compare the generated step {row.item} in mule-app/src/main/mule/ with the original code in the bundle's "
        f"resources/, correct it by hand if needed and test it, {rerun}.",
    )


def _other_question(other: OtherItem, rows: Sequence[Row], rerun: str) -> Question:
    steps = [row.item for row in rows if row.kind is RowKind.STEP and row.where.endswith(f"fault rule {other.name}")]
    holds = f" (steps: {', '.join(steps)})" if steps else ""
    if "fault rule" in other.reason:
        return Question(
            f"{other.name}{holds} was not generated: {other.reason}. How should this error handling be done in Mule?",
            f"a2m does not generate fault rules: add an on-error handler to the generated flow in "
            f"mule-app/src/main/mule/ that does what {other.name} does, {rerun}.",
        )
    return Question(
        f"{other.name} was not generated: {other.reason}. How should it be done in Mule?",
        f"Add it to the generated project in mule-app/ by hand, {rerun}.",
    )


def _method_cell(row: Row) -> str:
    if row.method is Method.AI:
        return f"ai ({row.confidence.value if row.confidence is not None else 'no confidence'})"
    return row.method.value


def _notes(row: Row) -> str:
    parts = []
    if row.kind is RowKind.CONDITION:
        parts.append(f"condition: {row.original}")
    if row.method is Method.AI and row.ai_unsure:
        parts.append("needs review" + (f": {row.reason}" if row.reason else ""))
    elif row.reason:
        parts.append(row.reason)
    if row.notes:
        parts.append(row.notes)
    return "; ".join(parts)


def _generated_steps(report: ProxyReport, text: _Text) -> list[str]:
    """One list line per generated (not skipped) policy step, each step and place once, in report order."""
    seen: set[tuple[str, str, str]] = set()
    found: list[str] = []
    for row in report.inventory.rows:
        key = (row.item, row.type, row.where)
        if row.kind is not RowKind.STEP or row.method is Method.SKIPPED or key in seen:
            continue
        seen.add(key)
        found.append(f"- {text.line(row.item)} ({text.line(row.type)}) at {text.line(row.where)}")
    return found


def render_report(report: ProxyReport, text: _Text, *, no_fix_reason: str) -> str:
    result = report.result
    vtype = result.type
    lines = [
        f"# a2m report: {text.line(report.name)}",
        "",
        f"Source: {text.line(report.source)}",
        f"Bucket: {report.bucket.value}",
        f"Verification type: {vtype.value} ({TYPE_MEANING[vtype]})",
        "",
    ]
    if report.bucket is Bucket.VERIFIED:
        lines.append(
            "Verified: the app ran and passed every test, and every step, policy and condition below was mapped "
            "by a template or a confident AI translation."
        )
    else:
        lines.append("Needs review: a person must check the items in the questions below before this project is used.")
    lines += [
        "",
        "## Policies, steps and conditions",
        "",
        (
            "One row per step, policy file and condition in the bundle and in the shared flows it calls. Method: "
            "template (a2m's own template), ai (translated by the AI, with its confidence) or skipped (not "
            "generated, with the reason)."
        ),
        "",
        "| Item | Kind | Type | Where | Mule result | Method | Notes |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in report.inventory.rows:
        cells: tuple[str, ...] = (
            row.item, row.kind.value, row.type, row.where, row.result, _method_cell(row), _notes(row)
        )
        lines.append("| " + " | ".join(text.cell(cell) for cell in cells) + " |")
    lines += ["", "## Test results", ""]
    if result.cases:
        lines.append(f"{result.passed} of {result.ran} tests passed.")
        if result.message:
            lines += ["", f"Result: {text.line(result.message)}"]
        lines += ["", "| Test | Policy | Result | Details |", "| --- | --- | --- | --- |"]
        for case in result.cases:
            details = "-" if case.passed else "; ".join(line.strip() for line in case.diff.splitlines() if line.strip())
            cells = (case.name, case.policy or "-", "passed" if case.passed else "failed", details)
            lines.append("| " + " | ".join(text.cell(cell) for cell in cells) + " |")
    else:
        lines.append(f"No tests ran: verification type {vtype.value}.")
        if result.message:
            lines += ["", f"Result: {text.line(result.message)}"]
        if vtype is VerificationType.FAILED and result.log_excerpt.strip():
            lines += ["", f"The end of the build or runtime log is in diffs/{VERIFICATION_LOG_NAME}."]
    lines += ["", "## Untested policies", ""]
    if vtype is VerificationType.GOLDEN:
        lines += [GOLDEN_COVERAGE, ""]
        lines += _generated_steps(report, text) or ["- None: no policy step was generated."]
    elif result.untested:
        lines += [f"- {text.line(u.name)} ({text.line(u.type)}): {text.line(u.reason)}" for u in result.untested]
    elif vtype is VerificationType.BATTERY:
        lines.append("None: every generated policy step had a test.")
    else:
        lines.append("Not known: the tests were not run.")
    lines += ["", "## AI fix attempts", ""]
    if report.attempts:
        diffs = {a.number: f"fix-attempt-{a.number}.diff" for a in report.attempts if a.diff.strip()}
        for attempt in report.attempts:
            changed = ", ".join(attempt.changed_files) or "nothing"
            steps = f" (steps: {', '.join(attempt.changed_steps)})" if attempt.changed_steps else ""
            reason = text.line(attempt.reason)
            if attempt.helped:
                outcome = f"helped and was kept: {reason}"
            elif "did not help" in fold(reason):
                outcome = reason
            else:
                outcome = f"did not help: {reason}"
            confidence = f" AI confidence: {attempt.confidence}." if attempt.confidence else ""
            diff = f" Diff: diffs/{diffs[attempt.number]}." if attempt.number in diffs else ""
            lines.append(
                f"{attempt.number}. Attempt {attempt.number}: changed {text.line(changed)}{text.line(steps)}; "
                f"{outcome}. Failing tests: {attempt.failing_before} before, {attempt.failing_after} after."
                f"{confidence}{diff}"
            )
    else:
        lines.append(f"None: no AI fix attempts were made ({no_fix_reason}).")
    if report.bucket is not Bucket.VERIFIED:
        found = questions(report)
        lines += ["", "## Question for a human", ""]
        lines += [f"{i}. {text.line(q.question)}" for i, q in enumerate(found, 1)]
        lines += ["", "## Suggested fix", ""]
        lines += [f"{i}. {text.line(q.suggestion)}" for i, q in enumerate(found, 1)]
    return "\n".join(lines) + "\n"


def _no_fix_reason(result: VerificationResult, max_attempts: int, provider: object) -> str:
    if result.type in (VerificationType.GOLDEN, VerificationType.BATTERY):
        return "every test passed, so there was nothing to fix"
    if result.type is VerificationType.STATIC:
        return "nothing was run, so there was nothing to fix"
    if max_attempts <= 0:
        return "--max-fix-attempts is 0"
    if provider is None:
        return "no AI provider was given"
    return "no fix was asked for this result"


def render_unsupported(name: str, source: str, cause: str, text: _Text, *, refused: bool) -> str:
    if refused:
        why = f"a2m refused the bundle: {text.line(cause)}"
        fix = (
            "Fix the bundle so it reads cleanly (the cause above names the file and the problem), or export it "
            f"again from Apigee, then rerun a2m with --force --only {text.line(name)}."
        )
    else:
        why = f"a2m failed while processing it: {text.line(cause)}"
        fix = (
            "This is an error inside a2m, not necessarily in the bundle; run.log has the full error. Rerun a2m "
            f"with --force --only {text.line(name)}, and report the error with run.log if it fails again."
        )
    lines = [
        f"# a2m report: {text.line(name)}",
        "",
        f"Source: {text.line(source)}",
        f"Bucket: {Bucket.UNSUPPORTED.value}",
        "",
        "a2m could not produce a Mule project for this proxy, so nothing was built or tested.",
        "",
        "## Why it is unsupported",
        "",
        why,
        "",
        "## Suggested fix",
        "",
        fix,
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- the stage


class ReportStage:
    """The per-proxy report stage; one per batch. It remembers each proxy's bucket for the engine."""

    __name__ = STAGE_NAME

    def __init__(self) -> None:
        self._buckets: dict[str, Bucket] = {}

    def bucket_for(self, name: str) -> str | None:
        """The bucket this stage chose for ``name`` in this batch, or None when it did not report it."""
        bucket = self._buckets.get(name)
        return bucket.value if bucket is not None else None

    def __call__(self, context: ReportContext) -> None:
        name = context.name
        self._buckets.pop(name, None)
        bundle = read_bundle(context.bundle_dir, label=name)
        proxy_dir = context.out_dir
        results_root = proxy_dir.absolute().parent
        records = load_records(results_root, name)
        inventory = build_inventory(bundle, context.shared_flows, records)
        result, attempts = load_verification(proxy_dir)
        bucket = decide(inventory.rows, inventory.others, result)
        report = ProxyReport(name, context.source_name, bucket, inventory, result, attempts)
        options = getattr(context, "options", None)
        roots: list[tuple[Path, str]] = [(results_root, "<results>")]
        golden = getattr(options, "golden", None)
        if isinstance(golden, Path):
            roots.append((golden, "<golden>"))
        text = _Text(Masker.for_bundle(bundle), roots)
        no_fix = _no_fix_reason(
            result, int(getattr(options, "max_fix_attempts", 0) or 0), getattr(options, "provider", None)
        )
        safefs.write_text_atomic(
            results_root, proxy_dir / layout.REPORT_NAME, render_report(report, text, no_fix_reason=no_fix)
        )
        diffs = proxy_dir / layout.DIFFS_DIR_NAME
        safefs.remove(results_root, diffs)
        if bucket is Bucket.NEEDS_REVIEW:
            safefs.make_dirs(results_root, diffs)
            for file_name, body in diff_files(report).items():
                safefs.write_text_atomic(results_root, diffs / file_name, text(body))
        safefs.write_text_atomic(
            results_root, proxy_dir / layout.PROXY_SUMMARY_NAME, proxy_facts_json(report.summary())
        )
        self._buckets[name] = bucket
        counts = inventory.counts()
        get_logger().info(
            "%s: %s, verification type %s; report rows: %d steps, %d policies, %d conditions",
            name,
            bucket.value,
            result.type.value,
            counts["step"],
            counts["policy"],
            counts["condition"],
        )

    def write_unsupported(
        self,
        out: Path,
        name: str,
        source: str,
        cause: str,
        *,
        refused: bool,
        bundle_dir: Path | None = None,
        roots: Sequence[tuple[Path, str]] = (),
    ) -> Path:
        """Write ``unsupported/<name>/REPORT.md`` naming ``cause``; the caller removed the proxy's other folders."""
        masker = Masker()
        if bundle_dir is not None:
            try:
                masker = Masker.for_bundle(read_bundle(bundle_dir, label=name))
            except (BundleError, OSError, ValueError):  # a bundle that cannot be read is masked with the default rules
                masker = Masker()
        text = _Text(masker, [(out, "<results>"), *roots])
        folder = layout.bucket_proxy_dir(out, layout.UNSUPPORTED_DIR_NAME, name)
        safefs.make_dirs(out, folder)
        path = folder / layout.REPORT_NAME
        safefs.write_text_atomic(out, path, render_unsupported(name, source, cause, text, refused=refused))
        self._buckets[name] = Bucket.UNSUPPORTED
        return path

    def finish_batch(self, out: Path, extra: Sequence[tuple[str, str]] = (), roots: Sequence[tuple[Path, str]] = ()) -> None:
        """Create the three bucket folders and write SUMMARY.md and summary.json for everything on disk, plus
        ``extra`` (name, reason) items that were refused and have no folder."""
        for bucket in layout.BUCKET_DIR_NAMES:
            safefs.make_dirs(out, layout.bucket_dir(out, bucket))
        text = _Text(Masker(), [(out, "<results>"), *roots])
        listed = [
            ProxySummary(name, Bucket.UNSUPPORTED.value, folder=False, reason=text.line(reason)) for name, reason in extra
        ]
        summary = write_batch_summary(out, listed)
        counts = summary.buckets()
        get_logger().info(
            "wrote %s and %s: %s",
            layout.SUMMARY_MD_NAME,
            layout.SUMMARY_JSON_NAME,
            ", ".join(f"{count} {bucket}" for bucket, count in counts.items()),
        )


def make_report_stage() -> ReportStage:
    return ReportStage()


__all__ = [
    "ProxyReport",
    "Question",
    "ReportError",
    "ReportStage",
    "diff_files",
    "load_records",
    "load_verification",
    "make_report_stage",
    "questions",
    "render_report",
    "render_unsupported",
]
