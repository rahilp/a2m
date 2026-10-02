"""Find Apigee bundles in an exports folder and copy them safely.

Terms, used the same way in code, docstrings and run.log: the *bundle root* is
the apiproxy/ or sharedflowbundle/ folder itself; its *wrapper* is the one top
folder it may sit in (alpha/ in alpha/apiproxy/). A stage's ``bundle_dir``
(:class:`a2m.engine.ProxyContext`) is the folder that holds the bundle root.

Discovery is read-only. :func:`discover` names every top-level item once with
:func:`item_name` and classifies it once with :func:`classify`, which uses
``lstat``, never follows a link, and returns exactly one of:

* a :class:`BundleSource`: a folder or regular ``.zip`` file holding a bundle;
* a :class:`SkippedItem`: only macOS metadata (:func:`is_macos_metadata`), a
  regular file that is not a ``.zip``, or a readable folder or zip with no
  folder (or link) named apiproxy or sharedflowbundle anywhere inside;
* a :class:`RejectedItem` (refused) for everything else.

Folders and zips share one layout rule, :func:`bundle_root`. Before a proxy is
processed, the engine copies only its bundle root (with its wrapper) into a
working copy: :func:`extract_zip` or :func:`copy_folder_bundle`. Both leave out
what lies beside it, copy only plain files and real folders, never through a
link, and share one set of limits (:class:`_Budget`). A zip's whole member list
is still checked by :func:`check_zip_members`.
"""

from __future__ import annotations

import errno
import os
import stat
import struct
import zipfile
import zlib
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PureWindowsPath
from typing import Any, BinaryIO

from a2m import safefs
from a2m.errors import BundleError, BundleLayoutError, UnsafeBundleError, UsageError
from a2m.layout import collision_key, unsafe_name_reason

PROXY_ROOT = "apiproxy"
SHARED_FLOW_ROOT = "sharedflowbundle"
BUNDLE_ROOTS = (PROXY_ROOT, SHARED_FLOW_ROOT)
ZIP_SUFFIX = ".zip"
# Apigee bundles are kilobytes to a few megabytes with tens to a few hundred
# files. A working copy (of a zip or a folder) is refused past these limits, so
# a crafted bundle cannot fill the results disk, its inodes or memory.
MAX_UNPACKED_BYTES = 1024 * 1024 * 1024
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_MEMBERS = 10_000
# A zip is refused past this size before it is opened. Its central directory
# is read whole into memory before any member check can run, so its declared
# size is checked next. 10,000 members with 1,024-character names fit in 11 MB.
MAX_ZIP_BYTES = 512 * 1024 * 1024
MAX_ZIP_DIRECTORY_BYTES = 16 * 1024 * 1024
# Limits on one member path. No common file system accepts a name part longer
# than 255 bytes; real bundles nest a few folders deep with short names.
MAX_MEMBER_PART_BYTES = 255
MAX_MEMBER_PARTS = 64
MAX_MEMBER_CHARS = 1024

# Errors zipfile can raise while opening or reading an archive. OSError here
# only ever comes from reading the input zip, never from writing results.
# ValueError covers member names flagged as UTF-8 that do not decode.
_ZIP_READ_ERRORS = (zipfile.BadZipFile, zlib.error, EOFError, OSError, NotImplementedError, ValueError)
# zipfile also raises RuntimeError for encrypted members.
_MEMBER_READ_ERRORS = (*_ZIP_READ_ERRORS, RuntimeError)
_COPY_CHUNK_BYTES = 1024 * 1024
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
# Opening a FIFO without a writer blocks; with O_NONBLOCK the open returns and
# the fstat check refuses it. No effect on plain files.
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_BINARY = getattr(os, "O_BINARY", 0)  # Windows only: no newline translation
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_READ_FLAGS = os.O_RDONLY | _NOFOLLOW | _NONBLOCK | _BINARY
# Where the system can, the folder copy walks by descriptor (openat), so a
# folder swapped for a link while it is copied is refused, not followed.
_WALK_BY_FD = (
    bool(_NOFOLLOW and _DIRECTORY)
    and os.open in os.supports_dir_fd
    and os.lstat in os.supports_dir_fd
    and os.scandir in os.supports_fd
)
# zipfile's own end-record reader and the two fields read from its result. They
# are private, and used deliberately: the pre-check in _check_zip_directory must
# find the end record (and zip64 record) byte-for-byte where zipfile.ZipFile
# will, and a second, hand-written search drifts from it (CP1 rounds 12-13).
_zip_end_record: Callable[[Any], list[Any] | None] = zipfile._EndRecData  # type: ignore[attr-defined]
_END_RECORD_MEMBERS: int = zipfile._ECD_ENTRIES_TOTAL  # type: ignore[attr-defined]
_END_RECORD_DIRECTORY_BYTES: int = zipfile._ECD_SIZE  # type: ignore[attr-defined]
_ZIP64_RECORD_SIGNATURE: bytes = zipfile.stringEndArchive64  # type: ignore[attr-defined]
_ZIP64_RECORD_BYTES: int = zipfile.sizeEndCentDir64  # type: ignore[attr-defined]
MACOS_METADATA = frozenset({"__MACOSX", ".DS_Store"})


class SourceKind(StrEnum):
    FOLDER = "folder"
    ZIP = "zip"


@dataclass(frozen=True, slots=True)
class BundleSource:
    """A proxy or shared flow bundle found in the exports folder."""

    name: str
    path: Path
    kind: SourceKind
    # The bundle root: PROXY_ROOT for a proxy, SHARED_FLOW_ROOT for a shared flow.
    root: str = PROXY_ROOT
    # The one top folder the bundle root sits in ("alpha" for alpha.zip holding
    # alpha/apiproxy/), or None when the bundle root is at the item's top.
    wrapper: str | None = None


@dataclass(frozen=True, slots=True)
class BundleRoot:
    """Where an item's bundle root is: ``root`` is apiproxy or sharedflowbundle, inside ``wrapper`` if set."""

    root: str
    wrapper: str | None

    @property
    def parts(self) -> tuple[str, ...]:
        """The bundle root's path parts in the item, its wrapper first."""
        return (self.root,) if self.wrapper is None else (self.wrapper, self.root)


# A path inside an item, as parts ("alpha", "apiproxy"); () is the item's top.
Parts = tuple[str, ...]
# subfolders(parts) lists the real folders directly inside the item folder at
# parts, sorted by name. Links are never listed or followed (zips have none).
Subfolders = Callable[[Parts], Sequence[str]]


@dataclass(frozen=True, slots=True)
class SkippedItem:
    """A top-level item that is not a bundle (a stray plain file, a docs folder)."""

    name: str
    path: Path
    reason: str


@dataclass(frozen=True, slots=True)
class RejectedItem:
    """A proxy candidate that cannot be processed (unsafe, unreadable, conflicting)."""

    name: str
    path: Path
    reason: str


# What classify() returns for one input item: exactly one of these.
Classified = BundleSource | RejectedItem | SkippedItem


@dataclass(frozen=True, slots=True)
class Discovery:
    input_dir: Path
    proxies: tuple[BundleSource, ...]
    shared_flows: tuple[BundleSource, ...]
    rejected: tuple[RejectedItem, ...]
    skipped: tuple[SkippedItem, ...]

    def candidate_names(self) -> list[str]:
        """Every proxy name found, processable or not, sorted."""
        return sorted({p.name for p in self.proxies} | {r.name for r in self.rejected})


def discover(input_dir: Path) -> Discovery:
    """Classify every top-level item of ``input_dir`` with :func:`classify`.

    Raises :class:`UsageError` when ``input_dir`` is missing or not a folder.
    """
    try:
        if not input_dir.exists():
            if safefs.is_link(input_dir):
                raise UsageError(
                    f"input folder {input_dir} is a symbolic link to {os.readlink(input_dir)}, which does not exist"
                )
            raise UsageError(f"input folder {input_dir} does not exist")
        if not input_dir.is_dir():
            raise UsageError(f"input path {input_dir} is not a folder")
        entries = sorted(input_dir.iterdir(), key=lambda p: p.name)
    except OSError as exc:
        raise UsageError(f"cannot read input folder {input_dir}: {exc.strerror or exc}") from exc

    outcomes: list[Classified] = []
    for path in entries:
        name = item_name(path)
        outcomes.append(classify(path, name))

    bundles = [item for item in outcomes if isinstance(item, BundleSource)]
    candidates = [item for item in bundles if item.root == PROXY_ROOT]
    shared_flows = [item for item in bundles if item.root != PROXY_ROOT]
    unreadable = [item for item in outcomes if isinstance(item, RejectedItem)]
    skipped = [item for item in outcomes if isinstance(item, SkippedItem)]

    proxies, rejected = _split_conflicts(candidates, unreadable)
    rejected.sort(key=lambda r: (r.name, r.path.name))
    return Discovery(
        input_dir=input_dir,
        proxies=tuple(proxies),
        shared_flows=tuple(shared_flows),
        rejected=tuple(rejected),
        skipped=tuple(skipped),
    )


def item_name(path: Path) -> str:
    """The one naming rule for an input item, used whether or not it can be read.

    A regular file ending in ``.zip`` (any letter case) is named by its stem;
    everything else (folders, a folder called ``alpha.zip``, an item whose type
    cannot be read) keeps its full name. A link is named by what it points to,
    the name the user expects for the refusal. Never raises.
    """
    try:
        is_regular_file = stat.S_ISREG(os.stat(path).st_mode)
    except (OSError, ValueError):
        is_regular_file = False
    return path.stem if is_regular_file and path.suffix.lower() == ZIP_SUFFIX else path.name


def classify(path: Path, name: str) -> Classified:
    """The one total classification of an input item: a bundle, refused, or skipped.

    Never raises: any error while reading the item refuses it, so one bad item
    never stops the batch. See the module docstring for what may be skipped.
    """
    try:
        return _classify(path, name)
    except Exception as exc:  # noqa: BLE001  item boundary: one unreadable input item never stops the batch
        return RejectedItem(name, path, f"cannot read this item: {type(exc).__name__}: {exc}")


def is_macos_metadata(name: str) -> bool:
    """The one test for macOS archive metadata (__MACOSX, .DS_Store, AppleDouble ._*), never bundle content."""
    return name in MACOS_METADATA or name.startswith("._")


def _classify(path: Path, name: str) -> Classified:
    if is_macos_metadata(path.name):
        reason = "macOS archive metadata, not a bundle"
    else:
        info = os.lstat(path)
        mode = info.st_mode
        if safefs.is_link_like(info):
            return RejectedItem(name, path, _link_reason(path))
        if stat.S_ISDIR(mode):
            try:
                found = bundle_root(folder_subfolders(path))
            except BundleLayoutError as exc:
                return RejectedItem(name, path, str(exc))
            return _place(found, name, path, SourceKind.FOLDER)
        if not stat.S_ISREG(mode):
            return RejectedItem(
                name, path, f"{_special_kind(mode)}, not a folder or a plain file; a2m does not open special files"
            )
        if path.suffix.lower() == ZIP_SUFFIX:
            return _classify_zip(path, name)
        reason = "not a bundle (not a folder or a .zip file)"
    return SkippedItem(name, path, reason)


def _link_reason(path: Path) -> str:
    """Why a symbolic link in the exports folder is refused, naming its target. Never raises."""
    try:
        target = os.readlink(path)
    except OSError as exc:
        state = f"symbolic link that cannot be read ({exc.strerror or exc})"
    else:
        try:
            os.stat(path)  # only to describe the target; the outcome is already decided
        except FileNotFoundError:
            state = f"symbolic link to {target}, which does not exist"
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                state = f"symbolic link loop (it points to {target})"
            else:
                state = f"symbolic link to {target}, which cannot be read ({exc.strerror or exc})"
        else:
            state = f"symbolic link to {target}"
    return f"{state}; a2m does not follow links in the input folder, so put the bundle itself there"


def _special_kind(mode: int) -> str:
    if stat.S_ISFIFO(mode):
        return "a named pipe (FIFO)"
    if stat.S_ISSOCK(mode):
        return "a socket"
    if stat.S_ISBLK(mode) or stat.S_ISCHR(mode):
        return "a device"
    return "a special file"


def _classify_zip(path: Path, name: str) -> Classified:
    try:
        infos = _read_zip_infos(path)
        check_zip_members(infos)
        found = bundle_root(zip_subfolders(infos))
    except UnsafeBundleError as exc:
        return RejectedItem(name, path, f"unsafe zip: {exc}")
    except BundleLayoutError as exc:
        return RejectedItem(name, path, str(exc))
    except _ZIP_READ_ERRORS as exc:
        return RejectedItem(name, path, f"not a readable zip file ({exc})")
    return _place(found, name, path, SourceKind.ZIP)


def _read_zip_infos(path: Path) -> list[zipfile.ZipInfo]:
    """The members of the zip at ``path``, read through :func:`_open_input_zip`."""
    with _open_input_zip(path) as fh, zipfile.ZipFile(fh) as zf:
        return zf.infolist()


def _open_input_zip(path: Path) -> BinaryIO:
    """Open the input zip at ``path`` for reading: the one way a2m opens an input zip.

    Opened by descriptor without following a link or blocking on a FIFO, then
    checked with ``fstat``: a zip swapped for a link or a special file, one over
    :data:`MAX_ZIP_BYTES`, or one :func:`_check_zip_directory` refuses raises
    :class:`UnsafeBundleError`. Other read errors raise :class:`OSError`.
    """
    try:
        fd = os.open(path, _READ_FLAGS)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):  # EMLINK: O_NOFOLLOW on a link, on FreeBSD
            raise UnsafeBundleError(f"{path.name} is now a symbolic link; a2m does not follow links") from exc
        raise
    fh = os.fdopen(fd, "rb")
    try:
        info = os.fstat(fh.fileno())
        if not stat.S_ISREG(info.st_mode) or (not _NOFOLLOW and safefs.is_link(path)):
            raise UnsafeBundleError(f"{path.name} is no longer a plain file (it changed after it was found)")
        if info.st_size > MAX_ZIP_BYTES:
            raise UnsafeBundleError(f"is {info.st_size} bytes, more than the {MAX_ZIP_BYTES} byte limit for a zip")
        _check_zip_directory(fh)
        fh.seek(0)
    except BaseException:
        fh.close()
        raise
    return fh


def _check_zip_directory(fh: BinaryIO) -> None:
    """Raise :class:`UnsafeBundleError` when the zip's end record declares too many members or too big a directory.

    The end record is read by zipfile's own reader (``zipfile._EndRecData``,
    which follows a zip64 locator as ``_EndRecData64``), so this check reads
    exactly the record zipfile.ZipFile will trust. A file it cannot read an end
    record from is refused here: zipfile would refuse it too. When the reader
    rejects a zip64 record, the refusal quotes that record's declared sizes if
    they are over a limit, read from the very bytes zipfile read.
    """
    seen = _ReadLog(fh)
    try:
        record = _zip_end_record(seen)
    except (*_ZIP_READ_ERRORS, struct.error) as exc:  # zipfile.ZipFile would fail the same way: refuse now
        zip64 = next((block for block in seen.blocks if block.startswith(_ZIP64_RECORD_SIGNATURE)), None)
        if zip64 is not None and len(zip64) >= _ZIP64_RECORD_BYTES:
            _check_declared(int.from_bytes(zip64[32:40], "little"), int.from_bytes(zip64[40:48], "little"))
        raise UnsafeBundleError(f"its end record cannot be read, so its member list is not checked ({exc})") from exc
    if record is None:
        raise zipfile.BadZipFile("File is not a zip file")  # zipfile's own words for this case
    _check_declared(int(record[_END_RECORD_MEMBERS]), int(record[_END_RECORD_DIRECTORY_BYTES]))


def _check_declared(members: int, directory: int) -> None:
    """Raise :class:`UnsafeBundleError` when a declared member count or directory size is over its limit."""
    if members > MAX_MEMBERS:
        raise UnsafeBundleError(f"has {members} members, more than the {MAX_MEMBERS} limit")
    if directory > MAX_ZIP_DIRECTORY_BYTES:
        raise UnsafeBundleError(
            f"its member list is {directory} bytes, more than the {MAX_ZIP_DIRECTORY_BYTES} byte limit"
        )


class _ReadLog:
    """``fh`` for zipfile's end-record reader, keeping every block it reads (a few, under 64 KiB each)."""

    def __init__(self, fh: BinaryIO) -> None:
        self._fh = fh
        self.blocks: list[bytes] = []

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        return self._fh.seek(offset, whence)

    def tell(self) -> int:
        return self._fh.tell()

    def read(self, size: int = -1) -> bytes:
        block = self._fh.read(size)
        self.blocks.append(block)
        return block


def _place(found: BundleRoot | None, name: str, path: Path, kind: SourceKind) -> BundleSource | SkippedItem:
    """The outcome for an item whose layout :func:`bundle_root` accepted."""
    if found is None:
        return SkippedItem(name, path, f"not a bundle (no {PROXY_ROOT}/ or {SHARED_FLOW_ROOT}/ folder anywhere inside)")
    return BundleSource(name, path, kind, found.root, found.wrapper)


def bundle_root(subfolders: Subfolders) -> BundleRoot | None:
    """The one rule for what an input item is, shared by folders and zips.

    A plain file named apiproxy does not count; at one place ``apiproxy`` wins
    over ``sharedflowbundle``.

    - A bundle root at the item's top: the item is that bundle.
    - Otherwise a bundle root inside exactly one top folder: the item is that
      bundle, with that folder as its ``wrapper``.
    - Bundle roots inside several top folders, nested deeper, or folders too
      deep to search: :class:`BundleLayoutError` naming what was found.
    - No folder named apiproxy or sharedflowbundle anywhere: ``None``.
    """
    top = _root_in(subfolders, ())
    if top is not None:
        return BundleRoot(top, None)
    wrapped = [(folder, root) for folder in subfolders(()) if (root := _root_in(subfolders, (folder,))) is not None]
    if len(wrapped) == 1:
        folder, root = wrapped[0]
        return BundleRoot(root, folder)
    if wrapped:
        found = ", ".join(f"{folder}/{root}/" for folder, root in wrapped)
        raise BundleLayoutError(
            f"{len(wrapped)} bundles one folder down ({found}); a2m takes one bundle per item, "
            f"so give each bundle its own zip or folder with {PROXY_ROOT}/ at its top"
        )
    nested = _nested_bundle_root(subfolders)
    if nested is not None:
        raise BundleLayoutError(
            f"no {PROXY_ROOT}/ folder at its top or inside a single top folder (found {'/'.join(nested)}/, "
            f"{len(nested) - 1} folders down); zip or copy the folder that holds {nested[-1]}/ itself"
        )
    return None


def _root_in(subfolders: Subfolders, parts: tuple[str, ...]) -> str | None:
    """The bundle root directly inside the folder at ``parts``, apiproxy first, or None."""
    here = set(subfolders(parts))
    return next((root for root in BUNDLE_ROOTS if root in here), None)


def _nested_bundle_root(subfolders: Subfolders) -> tuple[str, ...] | None:
    """The shallowest folder named apiproxy or sharedflowbundle, by path parts, first by name; or None.

    Searched level by level. Folders nested more than :data:`MAX_MEMBER_PARTS`
    deep (also as deep as an accepted zip member can go) cannot be searched to
    the end, so they raise :class:`BundleLayoutError` rather than letting the
    item be skipped as "not a bundle".
    """
    level: list[tuple[str, ...]] = [()]
    while level:
        deeper: list[tuple[str, ...]] = []
        for parts in level:
            for child in subfolders(parts):
                if child in BUNDLE_ROOTS:
                    return (*parts, child)
                deeper.append((*parts, child))
        if deeper and len(deeper[0]) > MAX_MEMBER_PARTS:
            raise BundleLayoutError(
                f"folders nest more than {MAX_MEMBER_PARTS} deep (for example {'/'.join(deeper[0][:3])}/...), "
                "too deep to tell whether a bundle is inside"
            )
        level = deeper
    return None


def folder_subfolders(path: Path) -> Subfolders:
    """:data:`Subfolders` for a folder item, never following a link.

    A folder that cannot be listed raises :class:`OSError` (the item is
    refused). A link or junction named apiproxy or sharedflowbundle raises
    :class:`BundleLayoutError`, since a2m cannot tell what the item is without
    following it. macOS metadata (:func:`is_macos_metadata`) is never listed.
    """

    def subfolders(parts: tuple[str, ...]) -> list[str]:
        names: list[str] = []
        with os.scandir(path.joinpath(*parts)) as entries:
            for entry in entries:
                if is_macos_metadata(entry.name):
                    continue
                info = os.lstat(entry.path)
                if safefs.is_link_like(info):
                    if entry.name in BUNDLE_ROOTS:
                        raise _link_root_error((*parts, entry.name))
                elif stat.S_ISDIR(info.st_mode):
                    names.append(entry.name)
        return sorted(names)

    return subfolders


def _link_root_error(parts: tuple[str, ...]) -> BundleLayoutError:
    """A link named apiproxy or sharedflowbundle met while listing an item: folder or zip alike."""
    return BundleLayoutError(
        f"{'/'.join(parts)} is a symbolic link; a2m does not follow links "
        "inside an input item, so put the folder itself there"
    )


def zip_subfolders(infos: list[zipfile.ZipInfo]) -> Subfolders:
    """:data:`Subfolders` for a zip item, from :func:`zip_folder_tree` built once.

    Like :func:`folder_subfolders`, listing a folder that holds a link member
    (:func:`member_kind`) named apiproxy or sharedflowbundle raises
    :class:`BundleLayoutError`.
    """
    tree = zip_folder_tree(infos)
    link_roots = {
        parts
        for info in infos
        if member_kind(info) is MemberKind.LINK
        and (parts := member_parts(info.filename))
        and parts[-1] in BUNDLE_ROOTS
        and not _has_metadata(parts)
    }

    def subfolders(parts: tuple[str, ...]) -> list[str]:
        for root in BUNDLE_ROOTS:
            if (*parts, root) in link_roots:
                raise _link_root_error((*parts, root))
        return sorted(tree.get(parts, ()))

    return subfolders


def zip_folder_tree(infos: list[zipfile.ZipInfo]) -> dict[tuple[str, ...], set[str]]:
    """Every folder a zip unpacks (by the parts extract_zip uses), mapped to the names of its subfolders.

    ``()`` is the zip root. A folder exists when a member lies inside it or is
    a folder entry for it; a member that is a plain file, a link or
    a special file (:func:`member_kind`) is not a folder. Members with macOS
    metadata in their path are left out.
    """
    tree: dict[tuple[str, ...], set[str]] = {(): set()}
    for info in infos:
        parts = member_parts(info.filename)
        if _has_metadata(parts):
            continue
        depth = len(parts) if member_kind(info) is MemberKind.FOLDER else len(parts) - 1
        for i in range(depth):
            tree.setdefault(parts[:i], set()).add(parts[i])
            tree.setdefault(parts[: i + 1], set())
    return tree


def zip_top_folders(infos: list[zipfile.ZipInfo]) -> set[str]:
    """Names of the folders a zip unpacks at its top (by the same parts extract_zip uses).

    './apiproxy/x.xml' gives 'apiproxy'; a member that is a plain file at the
    top (a file named 'apiproxy') gives nothing, and neither does macOS metadata.
    """
    return {
        parts[0]
        for info in infos
        if (parts := member_parts(info.filename)) and not _has_metadata(parts)
        and (len(parts) > 1 or member_kind(info) is MemberKind.FOLDER)
    }


def _has_metadata(parts: Parts) -> bool:
    return any(is_macos_metadata(part) for part in parts)


def _left_out(parts: Parts, keep: Parts, left_out: set[str]) -> bool:
    """Whether a working copy skips ``parts``: macOS metadata, or a path beside the bundle root ``keep``.

    A path beside ``keep`` (or beside its wrapper) is added to ``left_out``.
    """
    if _has_metadata(parts):
        return True
    for i, part in enumerate(parts[: len(keep)]):
        if part != keep[i]:
            left_out.add("/".join(parts[: i + 1]))
            return True
    return False


def _split_conflicts(
    candidates: list[BundleSource], unreadable: list[RejectedItem]
) -> tuple[list[BundleSource], list[RejectedItem]]:
    """Refuse unsafe or reserved names, and names that collide (see :func:`a2m.layout.collision_key`).

    The one place proxy names are validated, so every name reaching the engine
    is a plain folder name. Items refused while reading still take part: a
    valid ``alpha/`` next to a broken ``alpha.zip`` is a conflict, so neither is
    processed.
    """
    everything: list[BundleSource | RejectedItem] = [*candidates, *unreadable]
    groups: dict[str, list[BundleSource | RejectedItem]] = {}
    for item in everything:
        groups.setdefault(collision_key(item.name), []).append(item)
    proxies: list[BundleSource] = []
    rejected: list[RejectedItem] = []
    for key in sorted(groups):
        group = groups[key]
        reasons_for_all: list[str] = []
        if len(group) > 1:
            items = ", ".join(sorted(item.path.name for item in group))
            reasons_for_all.append(f"name conflict: {items} share the same proxy name")
        for item in group:
            reasons = list(reasons_for_all)
            if isinstance(item, RejectedItem):
                reasons.append(item.reason)
            elif (unsafe := unsafe_name_reason(item.name)) is not None:
                reasons.append(f"unsafe proxy name: {unsafe}")
            if reasons:
                rejected.append(RejectedItem(item.name, item.path, "; ".join(reasons)))
            elif isinstance(item, BundleSource):
                proxies.append(item)
    return proxies, rejected


def unsafe_member_reason(name: str) -> str | None:
    """Why a zip member name is unsafe to extract, or None when it is safe."""
    if "\x00" in name:
        return f"member {name!r} contains a NUL byte"
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or PureWindowsPath(name).drive:
        return f"member {name!r} is an absolute path"
    if ".." in normalized.split("/"):
        return f"member {name!r} points outside the bundle folder"
    if len(name) > MAX_MEMBER_CHARS:
        return f"member {name[:80]!r}... is {len(name)} characters long, more than the {MAX_MEMBER_CHARS} limit"
    parts = member_parts(name)
    if len(parts) > MAX_MEMBER_PARTS:
        return f"member {name!r} is {len(parts)} folders deep, more than the {MAX_MEMBER_PARTS} limit"
    for part in parts:
        size = len(part.encode("utf-8", "surrogatepass"))
        if size > MAX_MEMBER_PART_BYTES:
            return (
                f"member {name!r} has a name part of {size} bytes, more than the {MAX_MEMBER_PART_BYTES} "
                "bytes any common file system accepts"
            )
    return None


def member_parts(name: str) -> tuple[str, ...]:
    """The path parts a member name unpacks to (backslashes as separators, no empty or '.' parts)."""
    return tuple(part for part in name.replace("\\", "/").split("/") if part not in ("", "."))


def member_is_dir(info: zipfile.ZipInfo) -> bool:
    """Whether a member is a folder entry, by the same rules as :func:`member_parts`.

    Backslashes are separators on every platform (zipfile rewrites them only
    where ``os.sep`` is one), and a last part of '.' names the folder too.
    """
    last = info.filename.replace("\\", "/").rsplit("/", 1)[-1]
    return last in ("", ".")


class MemberKind(StrEnum):
    """What a zip member unpacks as, by :func:`member_kind`."""

    FILE = "file"
    FOLDER = "folder"
    LINK = "link"
    SPECIAL = "special"


def member_kind(info: zipfile.ZipInfo) -> MemberKind:
    """The one rule for what a zip member is, the zip side of the folder copy's ``lstat`` check.

    A member made on Unix (``create_system`` 3) carries its file type in the
    top bits of ``external_attr``: a symbolic link (what ``zip -y`` stores) is
    :attr:`MemberKind.LINK`; a FIFO, socket or device is
    :attr:`MemberKind.SPECIAL`. Otherwise (type 0, as DOS-made zips store, or a
    plain file or folder type) the name decides, by :func:`member_is_dir`.
    """
    if info.create_system == 3:
        mode = info.external_attr >> 16
        # A type field read from the zip, not a file-system entry, so no junction test (safefs) applies.
        if stat.S_IFMT(mode) == stat.S_IFLNK:
            return MemberKind.LINK
        if stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode) or stat.S_ISBLK(mode) or stat.S_ISCHR(mode):
            return MemberKind.SPECIAL
    return MemberKind.FOLDER if member_is_dir(info) else MemberKind.FILE


def _odd_member_reason(info: zipfile.ZipInfo, kind: MemberKind) -> str:
    """Why a link or special member inside the bundle root is refused, in the folder copy's words."""
    if kind is MemberKind.LINK:
        return (
            f"member {info.filename!r} is a symbolic link; a2m does not follow links inside an input item, "
            "so put the file or folder itself there"
        )
    return (
        f"member {info.filename!r} is {_special_kind(info.external_attr >> 16)}, not a plain file or folder; "
        "a2m does not open special files"
    )


class _NameKeys:
    """The one name-collision check for a working copy, shared by zips and folders.

    Paths are compared part by part with :func:`a2m.layout.collision_key`: two
    spellings that differ only in letter case, Unicode normalization or trailing
    dots and spaces land on one file on macOS, Windows, FAT or exFAT, so the
    bundle is refused wherever the copy is written.
    """

    __slots__ = ("_spellings",)

    def __init__(self) -> None:
        self._spellings: dict[tuple[str, ...], tuple[str, ...]] = {}

    def key(self, parts: tuple[str, ...]) -> tuple[str, ...]:
        """The collision key of ``parts``; raise :class:`UnsafeBundleError` if another spelling has it."""
        key = tuple(collision_key(part) for part in parts)
        seen = self._spellings.setdefault(key, parts)
        if seen != parts:
            raise UnsafeBundleError(
                f"members {'/'.join(seen)!r} and {'/'.join(parts)!r} differ only in letter case, "
                "Unicode normalization or trailing dots and spaces, "
                "so one would overwrite the other on macOS or Windows"
            )
        return key

    def spelling(self, key: tuple[str, ...]) -> tuple[str, ...]:
        """The first spelling seen for ``key``."""
        return self._spellings[key]


def check_zip_members(infos: list[zipfile.ZipInfo]) -> None:
    """Raise :class:`UnsafeBundleError` unless every member stays inside the bundle.

    Also refused, before anything is unpacked: members that would collide
    (the same file twice, a path that is both a file and a folder, or two paths
    :class:`_NameKeys` folds to one), member paths too long for any common file
    system, and zips past the limits every working copy shares (:class:`_Budget`).
    A link or special member (:func:`member_kind`) in the bundle root or on the
    way to it is refused too; one beside it is left out, as the folder copy
    does. Finding the bundle root may raise :class:`BundleLayoutError`.
    """
    if len(infos) > MAX_MEMBERS:
        raise UnsafeBundleError(f"has {len(infos)} members, more than the {MAX_MEMBERS} limit")
    total = 0
    files: set[tuple[str, ...]] = set()
    folders: set[tuple[str, ...]] = set()
    names = _NameKeys()
    folded = names.key

    for info in infos:
        reason = unsafe_member_reason(info.filename)
        if reason is not None:
            raise UnsafeBundleError(reason)
        if info.file_size > MAX_FILE_BYTES:
            raise UnsafeBundleError(
                f"member {info.filename!r} would unpack to {info.file_size} bytes, "
                f"more than the {MAX_FILE_BYTES} byte limit for one file"
            )
        total += info.file_size
        parts = member_parts(info.filename)
        if not parts:
            continue
        folders.update(folded(parts[:i]) for i in range(1, len(parts)))
        key = folded(parts)
        if member_is_dir(info):
            folders.add(key)
        elif key in files:
            raise UnsafeBundleError(f"member {info.filename!r} appears more than once")
        else:
            files.add(key)
    clashes = files & folders
    if clashes:
        raise UnsafeBundleError(f"member {'/'.join(names.spelling(min(clashes)))!r} is both a file and a folder")
    if total > MAX_UNPACKED_BYTES:
        raise UnsafeBundleError(f"would unpack to {total} bytes, more than the {MAX_UNPACKED_BYTES} byte limit")
    odd = [(info, kind) for info in infos if (kind := member_kind(info)) in (MemberKind.LINK, MemberKind.SPECIAL)]
    if odd:
        found = bundle_root(zip_subfolders(infos))
        for info, kind in odd:
            if found is not None and not _left_out(member_parts(info.filename), found.parts, set()):
                raise UnsafeBundleError(_odd_member_reason(info, kind))


@dataclass(slots=True)
class _Budget:
    """The limits every working copy shares, zip or folder, counted while it is written."""

    members: int = 0
    total: int = 0

    def entry(self, shown: str, depth: int) -> None:
        """Count one file or folder ``depth`` parts below the bundle's top."""
        self.members += 1
        if self.members > MAX_MEMBERS:
            raise UnsafeBundleError(f"has more than {MAX_MEMBERS} files and folders, the limit")
        if depth > MAX_MEMBER_PARTS:
            raise UnsafeBundleError(f"{shown} is {depth} folders deep, more than the {MAX_MEMBER_PARTS} limit")

    def data(self, shown: str, file_bytes: int, chunk: int) -> None:
        """Count ``chunk`` more bytes written, ``file_bytes`` of them so far into ``shown``."""
        self.total += chunk
        if file_bytes > MAX_FILE_BYTES:
            raise UnsafeBundleError(f"{shown} is more than the {MAX_FILE_BYTES} byte limit for one file")
        if self.total > MAX_UNPACKED_BYTES:
            raise UnsafeBundleError(f"would unpack to more than the {MAX_UNPACKED_BYTES} byte limit")


def _copy_stream(read: Callable[[int], bytes], write: Callable[[bytes], object], shown: str, budget: _Budget) -> None:
    copied = 0
    while True:
        chunk = read(_COPY_CHUNK_BYTES)
        if not chunk:
            return
        copied += len(chunk)
        budget.data(shown, copied, len(chunk))
        write(chunk)


# Errors creating an entry in the working copy that only the bundle's own names
# can cause (it starts empty under the run lock): a name that already exists,
# lands on the other kind of entry, or that the file system cannot store.
_NAME_ERRNOS = frozenset({errno.EEXIST, errno.EISDIR, errno.ENOTDIR, errno.EINVAL, errno.EILSEQ})


@contextmanager
def _writing(what: str) -> Iterator[None]:
    """An error caused by the bundle's own names while writing the working copy refuses it.

    That is ``ENAMETOOLONG`` or any error in :data:`_NAME_ERRNOS`. Every other
    error (disk full, permissions) is not the bundle's fault and propagates as
    :class:`OSError` (the proxy fails).
    """
    try:
        yield
    except OSError as exc:
        if exc.errno == errno.ENAMETOOLONG:
            raise UnsafeBundleError(f"{what} has a path too long to unpack here: {exc.strerror}") from exc
        if exc.errno in _NAME_ERRNOS:
            raise UnsafeBundleError(
                f"{what} cannot be written to the working copy because of its name "
                f"(it clashes with another name or this file system cannot store it): {exc.strerror or exc}"
            ) from exc
        raise


def extract_zip(zip_path: Path, dest: Path) -> list[str]:
    """Unpack the bundle root of ``zip_path`` into ``dest``, checking every member; return what was left out.

    Only the bundle root (by :func:`bundle_root`, with its wrapper) is unpacked;
    the paths beside it are returned, sorted (see :func:`_left_out`). Raises
    :class:`UnsafeBundleError` for a zip :func:`_open_input_zip` or
    :func:`check_zip_members` refuses, a limit breach or a name that cannot be
    created here (:func:`_writing`), and :class:`BundleError` when the archive
    cannot be read. Other write errors propagate as :class:`OSError`.
    """
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    try:
        fh = _open_input_zip(zip_path)
    except _ZIP_READ_ERRORS as exc:
        raise BundleError(f"cannot read zip {zip_path.name}: {exc}") from exc
    with fh:
        try:
            zf = zipfile.ZipFile(fh)
            infos = zf.infolist()
        except _ZIP_READ_ERRORS as exc:
            raise BundleError(f"cannot read zip {zip_path.name}: {exc}") from exc
        with zf:
            check_zip_members(infos)
            found = bundle_root(zip_subfolders(infos))
            budget, left_out = _Budget(), set[str]()
            for info in infos:
                if found is None or _left_out(member_parts(info.filename), found.parts, left_out):
                    continue
                with _writing(f"member {info.filename!r}"):
                    _extract_member(zf, info, dest, root, zip_path, budget)
    return sorted(left_out)


def _extract_member(
    zf: zipfile.ZipFile, info: zipfile.ZipInfo, dest: Path, root: Path, zip_path: Path, budget: _Budget
) -> None:
    parts = member_parts(info.filename)
    if not parts:
        return
    budget.entry(info.filename, len(parts))
    target = dest.joinpath(*parts)
    if not target.resolve().is_relative_to(root):
        raise UnsafeBundleError(f"member {info.filename!r} points outside the bundle folder")
    if member_is_dir(info):
        target.mkdir(parents=True, exist_ok=True)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        src = zf.open(info)
    except _MEMBER_READ_ERRORS as exc:
        raise BundleError(f"cannot read zip {zip_path.name}: {exc}") from exc

    def read(size: int) -> bytes:
        try:
            return src.read(size)
        except _MEMBER_READ_ERRORS as exc:
            raise BundleError(f"cannot read zip {zip_path.name}: {exc}") from exc

    with src, target.open("wb") as dst:
        _copy_stream(read, dst.write, f"member {info.filename!r}", budget)


@dataclass(slots=True)
class _InputDir:
    """A folder inside an input folder bundle, read by descriptor where the system allows, else by path."""

    path: Path
    fd: int | None

    def names(self) -> list[str]:
        with os.scandir(self.path if self.fd is None else self.fd) as entries:
            return sorted(entry.name for entry in entries)

    def lstat(self, name: str) -> os.stat_result:
        return os.lstat(self.path / name) if self.fd is None else os.lstat(name, dir_fd=self.fd)

    def open(self, name: str, flags: int) -> int:
        return os.open(self.path / name, flags) if self.fd is None else os.open(name, flags, dir_fd=self.fd)

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)


@contextmanager
def _reading(shown: str) -> Iterator[None]:
    """Errors reading the input bundle refuse it: a link or a swap is unsafe, anything else unreadable."""
    try:
        yield
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):  # O_NOFOLLOW met a link (EMLINK on FreeBSD)
            raise UnsafeBundleError(
                f"{shown} is a symbolic link; a2m does not follow links inside an input item"
            ) from exc
        if exc.errno == errno.ENOTDIR:
            raise UnsafeBundleError(f"{shown} is no longer a folder (it changed while a2m copied it)") from exc
        raise BundleError(f"cannot read {shown}: {exc.strerror or exc}") from exc


def copy_folder_bundle(src: Path, dest: Path) -> list[str]:
    """Copy the bundle root of the folder item ``src`` into ``dest``; return what was left out.

    Only the bundle root (by :func:`bundle_root`, with its wrapper) is copied;
    the paths beside it are never opened and are returned, sorted (see
    :func:`_left_out`). Only plain files and real folders are copied: each entry
    is checked with ``lstat``, opened with ``O_NOFOLLOW`` (by its folder's
    descriptor where the system allows) and checked again with ``fstat``.
    Raises :class:`UnsafeBundleError` for a link, junction, FIFO, socket or
    device in the bundle root or on the way to it (so an item swapped after
    discovery is refused), a limit breach (:class:`_Budget`), colliding names
    (:class:`_NameKeys`) or a name that cannot be created here
    (:func:`_writing`), and :class:`BundleError` when ``src`` cannot be read.
    Other write errors propagate as :class:`OSError`.
    """
    dest.mkdir(parents=True, exist_ok=True)
    with _reading(src.name):
        top = _open_input_dir(None, src, src.name)
    try:
        with _reading(src.name):
            found = bundle_root(folder_subfolders(src))
        left_out: set[str] = set()
        if found is not None:
            _copy_folder(top, dest, (), _Budget(), _NameKeys(), found.parts, left_out)
        return sorted(left_out)
    finally:
        top.close()


def _open_input_dir(parent: _InputDir | None, path: Path, shown: str) -> _InputDir:
    if not _WALK_BY_FD:
        info = os.lstat(path)
        if safefs.is_link_like(info) or not stat.S_ISDIR(info.st_mode):
            raise UnsafeBundleError(f"{shown} is no longer a real folder (it changed after it was found)")
        return _InputDir(path, None)
    flags = os.O_RDONLY | _NOFOLLOW | _NONBLOCK | _DIRECTORY
    fd = os.open(path, flags) if parent is None else parent.open(path.name, flags)
    if not stat.S_ISDIR(os.fstat(fd).st_mode):
        os.close(fd)
        raise UnsafeBundleError(f"{shown} is no longer a folder (it changed while a2m copied it)")
    return _InputDir(path, fd)


def _copy_folder(
    folder: _InputDir, dest: Path, parts: Parts, budget: _Budget, keys: _NameKeys, keep: Parts, left_out: set[str]
) -> None:
    with _reading("/".join(parts) or folder.path.name):
        names = folder.names()
    for name in names:
        rel = (*parts, name)
        if _left_out(rel, keep, left_out):
            continue
        shown = "/".join(rel)
        with _reading(shown):
            info = folder.lstat(name)
        budget.entry(shown, len(rel))
        keys.key(rel)
        target = dest.joinpath(*rel)
        if safefs.is_link_like(info):
            raise UnsafeBundleError(
                f"{shown} is a symbolic link or junction; a2m does not follow links inside an input item, "
                "so put the file or folder itself there"
            )
        if stat.S_ISDIR(info.st_mode):
            with _writing(shown):
                os.mkdir(target)
            with _reading(shown):
                child = _open_input_dir(folder, folder.path / name, shown)
            try:
                _copy_folder(child, dest, rel, budget, keys, keep, left_out)
            finally:
                child.close()
        elif stat.S_ISREG(info.st_mode):
            _copy_input_file(folder, name, target, shown, budget)
        else:
            raise UnsafeBundleError(
                f"{shown} is {_special_kind(info.st_mode)}, not a plain file or folder; a2m does not open special files"
            )


def _copy_input_file(folder: _InputDir, name: str, target: Path, shown: str, budget: _Budget) -> None:
    with _reading(shown):
        fd = folder.open(name, _READ_FLAGS)
    try:
        with _reading(shown):
            swapped = not stat.S_ISREG(os.fstat(fd).st_mode) or (not _NOFOLLOW and safefs.is_link(folder.path / name))
        if swapped:
            raise UnsafeBundleError(f"{shown} is no longer a plain file (it changed while a2m copied it)")
        with _writing(shown):
            out_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _BINARY, 0o666)

        def read(size: int) -> bytes:
            with _reading(shown):
                return os.read(fd, size)

        with os.fdopen(out_fd, "wb") as dst:
            _copy_stream(read, dst.write, shown, budget)
    finally:
        os.close(fd)
