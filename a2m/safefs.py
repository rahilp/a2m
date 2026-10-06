"""Guarded file-system changes inside the results folder.

Every delete and every overwrite a2m makes goes through this module. Each
operation takes the results folder (``root``) and a ``target`` path built from
it, and raises :class:`UnsafePathError` (changing nothing) unless:

* ``target`` is strictly inside ``root``: never ``root`` itself, no ``.`` or
  ``..`` parts, never a path that only looks similar; and
* no existing folder between ``root`` and ``target`` is a symbolic link or a
  Windows junction (any reparse point), so a link planted inside the results
  folder can never redirect a delete or a write to somewhere else.

:func:`is_link_like` is the one test for "is this a link" in a2m: every module
asks it (or :func:`is_link`) instead of ``is_symlink``, ``islink`` or
``S_ISLNK``, which do not see directory junctions.

``root`` itself may be a link the user chose (``--out`` on another disk); it is
resolved once. A link at ``target`` itself is removed as a link and never
followed.
"""

from __future__ import annotations

import errno
import os
import shutil
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from a2m.errors import LockHeldError, NotPlainFileError, UnsafePathError
from a2m.redaction import redact

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
# Opening a FIFO without a reader blocks; with O_NONBLOCK the open returns and
# the fstat check below refuses it. No effect on plain files.
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
# Windows marks symbolic links, directory junctions and every other reparse
# point with this attribute; CPython sets S_IFLNK only for true symlinks.
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

if sys.platform == "win32":
    import msvcrt

    def _try_lock(fd: int) -> bool:
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True


def _reparse_attributes(info: os.stat_result) -> int:
    """The Windows file attributes of ``info``; 0 on other systems. The probe for junctions."""
    return int(getattr(info, "st_file_attributes", 0))


def is_link_like(info: os.stat_result) -> bool:
    """True when the ``lstat`` result ``info`` is a symbolic link, a junction or another reparse point."""
    return stat.S_ISLNK(info.st_mode) or bool(_reparse_attributes(info) & _REPARSE_POINT)


def is_link(path: Path) -> bool:
    """:func:`is_link_like` for ``path`` itself, never following it; False when it does not exist."""
    try:
        info = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError, ValueError):
        return False
    return is_link_like(info)


def _parts(root: Path, target: Path) -> tuple[str, ...]:
    try:
        parts = target.relative_to(root).parts
    except ValueError:
        raise UnsafePathError(f"refusing to change {target}: it is not inside {root}") from None
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise UnsafePathError(f"refusing to change {target}: it is not strictly inside {root}")
    return parts


def _real_root(root: Path) -> Path:
    real = root.resolve(strict=True)
    if not real.is_dir():
        raise UnsafePathError(f"refusing to change anything under {root}: it is not a folder")
    return real


def _checked(root: Path, target: Path) -> Path:
    """The real path of ``target`` after checking every folder on the way to it."""
    parts = _parts(root, target)
    current = _real_root(root)
    for part in parts[:-1]:
        current = current / part
        if is_link(current):
            raise UnsafePathError(f"refusing to change {target}: {current} is a symbolic link or junction")
    return current / parts[-1]


def remove(root: Path, target: Path) -> bool:
    """Delete ``target`` (a folder tree, a file or a link). Returns False when it did not exist."""
    path = _checked(root, target)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISDIR(info.st_mode) and not is_link_like(info):
        shutil.rmtree(path)
    else:
        path.unlink()
    return True


def remove_empty_dir(root: Path, target: Path) -> bool:
    """Remove ``target`` only when it is a real, empty folder. Returns True when removed."""
    path = _checked(root, target)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(info.st_mode) or is_link_like(info):
        return False
    try:
        path.rmdir()
    except OSError as exc:
        if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
            return False
        raise
    return True


def make_dirs(root: Path, target: Path) -> Path:
    """Create ``target`` and any missing folders above it, refusing links on the way."""
    parts = _parts(root, target)
    current = _real_root(root)
    for part in parts:
        current = current / part
        try:
            os.mkdir(current)
        except FileExistsError:
            if is_link(current):
                raise UnsafePathError(f"refusing to create {target}: {current} is a symbolic link or junction") from None
            if not current.is_dir():
                raise
    return current


def write_text_atomic(root: Path, target: Path, text: str) -> None:
    """Write ``text`` to ``target`` through a temp file renamed into place.

    The temp file is created fresh and never through a link; the rename
    replaces a link at ``target`` instead of writing through it.
    """
    path = _checked(root, target)
    tmp_target = target.with_name(target.name + ".tmp")
    remove(root, tmp_target)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o666)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(redact(text))
    os.replace(tmp, path)


def write_bytes_atomic(root: Path, target: Path, data: bytes) -> None:
    """:func:`write_text_atomic` for bytes written exactly as given: for putting back a file a2m read before (an
    undone AI fix restores the project byte for byte), never for new text, which goes through the redacting
    :func:`write_text_atomic`."""
    path = _checked(root, target)
    tmp_target = target.with_name(target.name + ".tmp")
    remove(root, tmp_target)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o666)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.replace(tmp, path)


def move(root: Path, source: Path, target: Path) -> None:
    """Move ``source`` to ``target`` (replacing a file or empty folder there), both strictly inside ``root``.

    Neither path is reached through a link; a link at ``target`` itself is
    replaced, never followed.
    """
    os.replace(_checked(root, source), _checked(root, target))


def is_regular_file(root: Path, target: Path) -> bool:
    """True only when ``target`` is a plain file reached without following any link."""
    try:
        path = _checked(root, target)
    except (UnsafePathError, FileNotFoundError):
        return False
    try:
        mode = path.lstat().st_mode
    except (FileNotFoundError, NotADirectoryError):
        return False
    return stat.S_ISREG(mode)


def open_plain_file(root: Path, target: Path, flags: int) -> int:
    """Open ``target`` with ``flags`` and return the descriptor, only if it is a plain file.

    Never follows a link (at ``target`` or on the way) and never blocks on a
    FIFO or device: anything but a plain file raises :class:`NotPlainFileError`
    and leaves nothing open.
    """
    path = _checked(root, target)
    try:
        fd = os.open(path, flags | _NOFOLLOW | _NONBLOCK, 0o666)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EISDIR, errno.ENXIO, errno.EMLINK):
            raise NotPlainFileError(f"{target.name} is not a plain file") from exc
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise NotPlainFileError(f"{target.name} is not a plain file")
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextmanager
def exclusive_lock(root: Path, target: Path) -> Iterator[None]:
    """Hold an exclusive, non-blocking lock on the file ``target`` while the block runs.

    The file is created when missing and opened without following a link or
    blocking; anything but a plain file raises :class:`NotPlainFileError`.
    Raises :class:`LockHeldError` at once when another process (or another
    open of the same file) holds the lock. The operating system releases the
    lock when the process ends, even after a crash, so no stale lock remains.
    """
    fd = open_plain_file(root, target, os.O_RDWR | os.O_CREAT)
    try:
        if not _try_lock(fd):
            raise LockHeldError(f"{target} is locked by another process")
        yield
    finally:
        os.close(fd)


def lock_held(root: Path, target: Path) -> bool:
    """Whether the lock :func:`exclusive_lock` takes on ``target`` is held right now; read-only.

    Never creates ``target``: a missing file is not held. The lock is tried without waiting and released
    at once. Anything but a plain file raises :class:`NotPlainFileError`, like :func:`exclusive_lock`.
    """
    try:
        fd = open_plain_file(root, target, os.O_RDONLY)
    except (FileNotFoundError, NotADirectoryError):
        return False
    try:
        return not _try_lock(fd)
    finally:
        os.close(fd)
