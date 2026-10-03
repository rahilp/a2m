"""Names and paths inside the results folder, kept in one place."""

from __future__ import annotations

import unicodedata
from pathlib import Path

from a2m.errors import UnsafePathError

RUN_LOG_NAME = "run.log"
DONE_MARKER_NAME = ".done"
WORK_DIR_NAME = ".a2m-work"
# Written first into every results folder a2m creates. Only a folder holding
# this marker may be reused with --resume or --force.
RESULTS_MARKER_NAME = ".a2m-results"
RESULTS_MARKER_TEXT = "a2m results folder, format 1\n"
# Held (locked) for the whole of a run, so two runs never share a results folder.
# The file stays in place after the run; only the lock on it is released.
LOCK_NAME = ".a2m-lock"
# Written by a --force run before it removes any earlier .done marker and
# removed once every one is gone. While it exists --resume is refused, since
# some .done markers may still be from before the forced run.
FORCE_PENDING_NAME = ".a2m-force-pending"
FORCE_PENDING_TEXT = "a2m --force run clearing earlier .done markers\n"
# Each proxy's generated Mule project, inside the proxy's folder.
MULE_APP_DIR_NAME = "mule-app"
# Inside the work area: the working copies of shared flow bundles. Proxy names
# never start with a dot, so this never collides with a proxy's work folder.
SHARED_FLOWS_WORK_NAME = ".shared-flows"



def collision_key(name: str) -> str:
    """The key under which two names land on the same file on some file system.

    Every name-collision check in a2m (proxy folders, zips, zip members,
    refused items) compares these keys, so they all agree on what collides:

    * Unicode normalization: macOS treats NFC 'caf\u00e9' and NFD 'cafe\u0301' as one name;
    * trailing dots and spaces: Windows drops them, so 'x.xml.' lands on 'x.xml';
    * letter case: macOS and Windows file systems ignore it.
    """
    stripped = unicodedata.normalize("NFC", name).rstrip(". ")
    return unicodedata.normalize("NFC", stripped.casefold())


# Proxy names that would collide with files a2m owns in the results folder.
# Compared by collision_key, since macOS and Windows file systems ignore case.
RESERVED_NAMES: frozenset[str] = frozenset(
    {RUN_LOG_NAME, WORK_DIR_NAME, RESULTS_MARKER_NAME, LOCK_NAME, FORCE_PENDING_NAME}
)
_RESERVED_KEYS: frozenset[str] = frozenset(collision_key(name) for name in RESERVED_NAMES)
_FORBIDDEN_CHARS = ("/", "\\", "\x00")
# Unicode categories that must never appear in a name: control, format,
# surrogate (undecodable bytes in a file name), private-use and unassigned
# characters, plus line and paragraph separators. Names flow into run.log,
# reports and Mule XML, where any of these could break or forge a line.
_FORBIDDEN_CATEGORY_PREFIXES = ("C", "Zl", "Zp")


def unsafe_name_reason(name: str) -> str | None:
    """Why ``name`` cannot be used as a folder directly under the results folder, or None."""
    if name in ("", ".", ".."):
        return f"name {name!r} is not a usable proxy name"
    if any(char in name for char in _FORBIDDEN_CHARS):
        return f"name {name!r} contains a path separator or NUL byte"
    if any(unicodedata.category(char).startswith(_FORBIDDEN_CATEGORY_PREFIXES) for char in name):
        return f"name {name!r} contains a control, line-break or undecodable character"
    # Windows drops trailing dots and spaces, so 'run.log.' would land on run.log.
    if collision_key(name) in _RESERVED_KEYS:
        return f"name {name!r} is reserved for a2m's own files"
    if name.startswith("."):
        return f"name {name!r} starts with a dot, which a2m keeps for its own files"
    return None


def _child(parent: Path, name: str) -> Path:
    """``parent / name``, refusing any name that would not be a direct child of ``parent``."""
    reason = unsafe_name_reason(name)
    if reason is not None:
        raise UnsafePathError(f"refusing to use {name!r} under {parent}: {reason}")
    return parent / name


def run_log_path(out_dir: Path) -> Path:
    return out_dir / RUN_LOG_NAME


def results_marker_path(out_dir: Path) -> Path:
    return out_dir / RESULTS_MARKER_NAME


def lock_path(out_dir: Path) -> Path:
    return out_dir / LOCK_NAME


def force_pending_path(out_dir: Path) -> Path:
    return out_dir / FORCE_PENDING_NAME


def proxy_out_dir(out_dir: Path, name: str) -> Path:
    return _child(out_dir, name)


def done_marker_path(out_dir: Path, name: str) -> Path:
    return proxy_out_dir(out_dir, name) / DONE_MARKER_NAME


def work_root(out_dir: Path) -> Path:
    return out_dir / WORK_DIR_NAME


def proxy_work_dir(out_dir: Path, name: str) -> Path:
    return _child(work_root(out_dir), name)


def mule_app_dir(proxy_dir: Path) -> Path:
    return proxy_dir / MULE_APP_DIR_NAME


def shared_flows_work_root(out_dir: Path) -> Path:
    return work_root(out_dir) / SHARED_FLOWS_WORK_NAME


def shared_flow_work_dir(out_dir: Path, name: str) -> Path:
    return _child(shared_flows_work_root(out_dir), name)
