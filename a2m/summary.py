"""The batch summary: SUMMARY.md and summary.json, built from what is on disk in the three buckets.

Each verified or needs-review proxy folder holds the facts the summary needs
(:data:`a2m.layout.PROXY_SUMMARY_NAME`, written by the report stage with its
REPORT.md), so a --resume or --only run that leaves earlier proxies in place
still summarizes every proxy in the results folder. An unsupported proxy has
only its REPORT.md, so its entry is its name and bucket. Items a2m refused
whose name cannot be a folder are listed too, so nothing is ever silently
absent. summary.json holds one run-metadata timestamp (``run.finished``);
SUMMARY.md holds none.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from a2m import __version__, layout, safefs
from a2m.verify.model import VerificationType

KINDS = ("step", "policy", "condition")


@dataclass(frozen=True, slots=True)
class ProxySummary:
    """One proxy in the summary; ``rows`` and ``policy_types`` are None/empty for an unsupported proxy."""

    name: str
    bucket: str
    verification_type: str | None = None
    rows: dict[str, int] | None = None
    policy_types: dict[str, int] = field(default_factory=dict)
    folder: bool = True
    reason: str = ""

    def to_json_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "bucket": self.bucket,
            "verification_type": self.verification_type,
            "rows": dict(self.rows) if self.rows is not None else None,
            "policy_types": dict(sorted(self.policy_types.items())),
            "report": f"{self.bucket}/{self.name}/{layout.REPORT_NAME}" if self.folder else None,
        }
        if self.reason:
            data["reason"] = self.reason
        return data


def proxy_facts_json(summary: ProxySummary) -> str:
    """The text of a proxy folder's :data:`a2m.layout.PROXY_SUMMARY_NAME`."""
    return json.dumps(summary.to_json_data(), indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def _read_facts(path: Path, name: str, bucket: str) -> ProxySummary:
    fallback = ProxySummary(name, bucket)
    try:
        if safefs.is_link(path) or not path.is_file():
            return fallback
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return fallback
    if not isinstance(data, dict) or data.get("name") != name:
        return fallback
    vtype = data.get("verification_type")
    if vtype not in {t.value for t in VerificationType}:
        vtype = None
    raw_rows = data.get("rows")
    rows = (
        {kind: int(raw_rows[kind]) for kind in KINDS}
        if isinstance(raw_rows, dict) and all(isinstance(raw_rows.get(k), int) for k in KINDS)
        else None
    )
    raw_types = data.get("policy_types")
    types = (
        {str(k): int(v) for k, v in raw_types.items() if isinstance(v, int)} if isinstance(raw_types, dict) else {}
    )
    return ProxySummary(name, bucket, vtype, rows, types)


def collect(out: Path) -> list[ProxySummary]:
    """Every proxy folder in the three buckets of ``out``, sorted by name then bucket."""
    found: list[ProxySummary] = []
    for bucket in layout.BUCKET_DIR_NAMES:
        folder = layout.bucket_dir(out, bucket)
        if safefs.is_link(folder) or not folder.is_dir():
            continue
        for entry in sorted(folder.iterdir()):
            if safefs.is_link(entry) or not entry.is_dir() or layout.unsafe_name_reason(entry.name) is not None:
                continue
            if bucket == layout.UNSUPPORTED_DIR_NAME:
                found.append(ProxySummary(entry.name, bucket))
            else:
                found.append(_read_facts(entry / layout.PROXY_SUMMARY_NAME, entry.name, bucket))
    return sorted(found, key=lambda s: (s.name, layout.BUCKET_DIR_NAMES.index(s.bucket)))


@dataclass(frozen=True, slots=True)
class BatchSummary:
    proxies: tuple[ProxySummary, ...]

    def buckets(self) -> dict[str, int]:
        return {bucket: sum(1 for p in self.proxies if p.bucket == bucket) for bucket in layout.BUCKET_DIR_NAMES}

    def verification_types(self) -> dict[str, int]:
        return {t.value: sum(1 for p in self.proxies if p.verification_type == t.value) for t in VerificationType}

    def policy_types(self) -> dict[str, int]:
        totals: dict[str, int] = {}
        for proxy in self.proxies:
            for name, count in proxy.policy_types.items():
                totals[name] = totals.get(name, 0) + count
        return dict(sorted(totals.items()))

    def to_json(self, finished: str) -> str:
        data = {
            "run": {"a2m_version": __version__, "finished": finished},
            "buckets": self.buckets(),
            "verification_types": self.verification_types(),
            "policy_types": self.policy_types(),
            "proxies": [p.to_json_data() for p in self.proxies],
        }
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"

    def to_markdown(self) -> str:
        buckets = self.buckets()
        total = len(self.proxies)
        lines = [
            "# a2m migration summary",
            "",
            f"{total} prox{'y' if total == 1 else 'ies'}: "
            + ", ".join(f"{count} {bucket}" for bucket, count in buckets.items())
            + ". Each proxy's REPORT.md says why it is in its bucket.",
            "",
            "## Buckets",
            "",
            "| Bucket | Proxies |",
            "| --- | --- |",
            *(f"| {bucket} | {count} |" for bucket, count in buckets.items()),
            "",
            (
                "- verified: the app ran and passed every test (golden or battery), and every step, policy and "
                "condition was mapped with a template or a confident AI translation."
            ),
            (
                "- needs-review: a2m produced a Mule project, but something must be checked by a person (see the "
                "proxy's REPORT.md)."
            ),
            "- unsupported: a2m could not produce a Mule project.",
            "",
            "## Verification types",
            "",
            "| Verification type | Proxies |",
            "| --- | --- |",
            *(f"| {name} | {count} |" for name, count in self.verification_types().items()),
            "",
            "- golden: the app ran and every response matched the recorded Apigee responses.",
            "- battery: the app ran and passed a2m's policy tests against a local mock backend.",
            "- static: the app was generated (and maybe built) but never run against tests.",
            "- failed: the build, the deploy or at least one test failed.",
            "",
            "## Policy types",
            "",
            (
                "Policy files of the proxies that have a Mule project, shared flow policies counted for each proxy "
                "that calls them."
            ),
            "",
            "| Policy type | Policies |",
            "| --- | --- |",
            *(f"| {_cell(name)} | {count} |" for name, count in self.policy_types().items()),
            "",
            "## Proxies",
            "",
            "| Proxy | Bucket | Verification type | Steps | Policies | Conditions | Report |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for proxy in self.proxies:
            rows = proxy.rows or {}
            counts = [str(rows[kind]) if kind in rows else "-" for kind in KINDS]
            report = f"{proxy.bucket}/{proxy.name}/{layout.REPORT_NAME}" if proxy.folder else _cell(proxy.reason or "-")
            lines.append(
                f"| {_cell(proxy.name)} | {proxy.bucket} | {proxy.verification_type or '-'} | "
                + " | ".join(counts)
                + f" | {_cell(report)} |"
            )
        return "\n".join(lines) + "\n"


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def write_batch_summary(out: Path, extra: Sequence[ProxySummary] = ()) -> BatchSummary:
    """Write SUMMARY.md and summary.json for every proxy on disk in ``out`` plus ``extra`` (refused items that
    have no folder), and return what was written."""
    proxies = sorted((*collect(out), *extra), key=lambda s: (s.name, layout.BUCKET_DIR_NAMES.index(s.bucket)))
    summary = BatchSummary(tuple(proxies))
    finished = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    safefs.write_text_atomic(out, layout.summary_json_path(out), summary.to_json(finished))
    safefs.write_text_atomic(out, layout.summary_md_path(out), summary.to_markdown())
    return summary


__all__ = ["BatchSummary", "ProxySummary", "collect", "proxy_facts_json", "write_batch_summary"]
