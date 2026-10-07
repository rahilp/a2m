"""Read an a2m results folder for the results screen; read-only, imports no Textual.

Everything is read through :mod:`a2m.safefs`'s own checks: a file is opened only when it is a plain file
reached without following a link on the way (never written, never created), and a proxy folder that is a
link, or sits in a bucket folder that is a link, is never treated as that proxy's folder, the same way a2m
itself refuses them. The bucket lists come from summary.json (or, when it is missing or unreadable, from
the bucket folders, read the way a2m builds its summary), never from parsing SUMMARY.md. Reads are capped,
so a huge file cannot stall the app: SUMMARY.md shows its first :data:`SUMMARY_MAX_LINES` lines and says
so. An optional sidecar that is there but cannot be used (summary.json unreadable, too large or malformed)
never rejects the folder: the bucket folders are read instead and the summary pane says so in one line.
An empty SUMMARY.md counts as no SUMMARY.md yet.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from a2m import layout, safefs
from a2m.errors import A2mError
from a2m.summary import collect

# SUMMARY.md beyond this many lines (or bytes) is cut, with a line saying so (DESIGN.md Markdown viewer).
SUMMARY_MAX_LINES = 2000
SUMMARY_MAX_BYTES = 2 * 1024 * 1024
# summary.json beyond this size is not parsed; the bucket folders are read instead.
SUMMARY_JSON_MAX_BYTES = 32 * 1024 * 1024
TRUNCATED_NOTE = f"Showing the first {SUMMARY_MAX_LINES} lines of a larger file."
# Said under SUMMARY.md when summary.json is there but cannot be used; {reason} is why.
SUMMARY_JSON_FALLBACK_NOTE = "summary.json could not be used ({reason}), so the proxy lists come from the bucket folders."

NOT_RESULTS_TEXT = "This folder does not look like an a2m results folder."
NO_SUMMARY_TEXT = "No SUMMARY.md yet, the run may still be in progress or was stopped before it finished."


class Problem(StrEnum):
    """Why a folder cannot be shown as results."""

    NOT_RESULTS = "not-results"  # no .a2m-results marker (or not a folder at all)
    NO_SUMMARY = "no-summary"  # an a2m results folder without SUMMARY.md yet (stopped or still running)
    UNREADABLE = "unreadable"  # the folder or its summary could not be read


@dataclass(frozen=True, slots=True)
class ResultsProblem:
    problem: Problem
    # One plain-language line, without a leading marker.
    message: str


@dataclass(frozen=True, slots=True)
class ResultsProxy:
    name: str
    bucket: str
    # The proxy's own folder, or None when it has none or it is refused (a link, or not a plain folder).
    folder: Path | None = None


@dataclass(frozen=True, slots=True)
class Results:
    folder: Path
    summary_md: str
    proxies: tuple[ResultsProxy, ...]
    # True when SUMMARY.md was longer than the cap and only its start is in ``summary_md``.
    truncated: bool = False
    # One plain line per optional file that could not be used and what was shown instead.
    notes: tuple[str, ...] = ()
    by_bucket: dict[str, tuple[ResultsProxy, ...]] = field(init=False)

    def __post_init__(self) -> None:
        grouped = {
            bucket: tuple(p for p in self.proxies if p.bucket == bucket) for bucket in layout.BUCKET_DIR_NAMES
        }
        object.__setattr__(self, "by_bucket", grouped)

    def count(self, bucket: str) -> int:
        return len(self.by_bucket[bucket])


def load_results(folder: Path) -> Results | ResultsProblem:
    """Read ``folder`` as an a2m results folder; never writes, never raises for a bad or unreadable folder."""
    out = folder.absolute()
    try:
        if not out.is_dir() or not safefs.is_regular_file(out, layout.results_marker_path(out)):
            return ResultsProblem(Problem.NOT_RESULTS, NOT_RESULTS_TEXT)
        summary_md = layout.summary_md_path(out)
        if not safefs.is_regular_file(out, summary_md):
            return ResultsProblem(Problem.NO_SUMMARY, NO_SUMMARY_TEXT)
        data, more = _read_capped(out, summary_md, SUMMARY_MAX_BYTES)
        if not data.strip():  # an empty SUMMARY.md says nothing yet: the same as none (DESIGN.md)
            return ResultsProblem(Problem.NO_SUMMARY, NO_SUMMARY_TEXT)
        text, truncated = _first_lines(data.decode("utf-8", errors="replace"), SUMMARY_MAX_LINES)
        proxies, notes = _proxies(out)
    except (OSError, RuntimeError, A2mError) as exc:  # RuntimeError: a symlink loop on Python 3.11
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
        return ResultsProblem(Problem.UNREADABLE, f"cannot read {out}: {reason}")
    truncated = truncated or more
    if truncated:
        text = text.rstrip("\n") + f"\n\n*{TRUNCATED_NOTE}*\n"
    for note in notes:
        text = text.rstrip("\n") + f"\n\n*{_escape_md(note)}*\n"
    return Results(out, text, proxies, truncated, notes)


def _escape_md(text: str) -> str:
    """``text`` as literal Markdown: every punctuation mark that could start markup is escaped."""
    return "".join(f"\\{c}" if c in "\\`*_{}[]<>()#+-.!|~" else c for c in text)


def _read_capped(root: Path, target: Path, limit: int) -> tuple[bytes, bool]:
    """Up to ``limit`` bytes of the plain file ``target`` (no link followed), and whether there was more."""
    fd = safefs.open_plain_file(root, target, os.O_RDONLY)
    try:
        chunks: list[bytes] = []
        size = 0
        while size <= limit:
            chunk = os.read(fd, min(1024 * 1024, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    return data[:limit], len(data) > limit


def _first_lines(text: str, limit: int) -> tuple[str, bool]:
    lines = text.splitlines(keepends=True)
    if len(lines) <= limit:
        return text, False
    return "".join(lines[:limit]), True


def _proxies(out: Path) -> tuple[tuple[ResultsProxy, ...], tuple[str, ...]]:
    """The proxies summary.json lists; the bucket folders (read as a2m reads them) when it cannot be used,
    with a note saying why when it was there but unusable."""
    listed, problem = _proxies_from_json(out)
    notes: tuple[str, ...] = ()
    if listed is None:
        listed = [(p.name, p.bucket, p.folder) for p in collect(out)]
        if problem is not None:
            notes = (SUMMARY_JSON_FALLBACK_NOTE.format(reason=problem),)
    proxies = tuple(
        ResultsProxy(name, bucket, _proxy_folder(out, bucket, name) if has else None) for name, bucket, has in listed
    )
    return proxies, notes


def _proxies_from_json(out: Path) -> tuple[list[tuple[str, str, bool]] | None, str | None]:
    """summary.json's proxy list, or None and why it cannot be used (no reason when it is simply not there).
    Never raises for a summary.json that cannot be read: it is optional, the bucket folders stand in for it."""
    target = layout.summary_json_path(out)
    try:
        if not safefs.is_regular_file(out, target):
            return None, None
        data, more = _read_capped(out, target, SUMMARY_JSON_MAX_BYTES)
    except (OSError, RuntimeError, A2mError) as exc:  # RuntimeError: a symlink loop on Python 3.11
        return None, exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc) or type(exc).__name__
    if more:
        return None, "too large"
    try:
        parsed: Any = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, "not valid JSON"
    entries = parsed.get("proxies") if isinstance(parsed, dict) else None
    if not isinstance(entries, list):
        return None, "no proxy list"
    found: list[tuple[str, str, bool]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            return None, "unexpected proxy entry"
        name, bucket = entry.get("name"), entry.get("bucket")
        if not isinstance(name, str) or bucket not in layout.BUCKET_DIR_NAMES:
            return None, "unexpected proxy entry"
        found.append((name, str(bucket), entry.get("report") is not None))
    return found, None


def _proxy_folder(out: Path, bucket: str, name: str) -> Path | None:
    """``out/<bucket>/<name>`` when it is a real folder reached without a link; None otherwise."""
    if layout.unsafe_name_reason(name) is not None:
        return None
    bucket_dir = layout.bucket_dir(out, bucket)
    folder = layout.bucket_proxy_dir(out, bucket, name)
    try:
        if safefs.is_link(bucket_dir) or safefs.is_link(folder) or not folder.is_dir():
            return None
    except OSError:
        return None
    return folder


def preview_label(folder: Path) -> tuple[Problem | None, str]:
    """What the folder browser says about ``folder`` when opening results: the problem (None when it can be
    opened) and one line. Reads only the marker and whether SUMMARY.md is there."""
    out = folder.absolute()
    try:
        if not out.is_dir() or not safefs.is_regular_file(out, layout.results_marker_path(out)):
            return Problem.NOT_RESULTS, NOT_RESULTS_TEXT
        if not safefs.is_regular_file(out, layout.summary_md_path(out)):
            return Problem.NO_SUMMARY, NO_SUMMARY_TEXT
    except (OSError, RuntimeError) as exc:
        return Problem.UNREADABLE, f"cannot read {out}: {exc.strerror if isinstance(exc, OSError) else exc}"
    return None, "a2m results with a SUMMARY.md"


__all__ = [
    "NOT_RESULTS_TEXT",
    "NO_SUMMARY_TEXT",
    "Problem",
    "Results",
    "ResultsProblem",
    "ResultsProxy",
    "load_results",
    "preview_label",
]
