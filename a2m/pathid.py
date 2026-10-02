"""How two paths relate on disk, decided by file identity rather than spelling.

Path text is not enough to tell whether two paths are the same folder: on a
case-insensitive file system (macOS APFS by default, Windows, FAT or exFAT
disks on Linux) ``Exports`` and ``exports`` are one folder, and
``os.path.realpath`` does not fix the letter case. :func:`is_same_or_inside`
is the one helper for that question: every check of the input folder against
the results folder uses it.
"""

from __future__ import annotations

import os
from pathlib import Path


def file_identity(path: Path) -> tuple[int, int] | None:
    """``(st_dev, st_ino)`` of what ``path`` names, following links; None when it cannot be read or is unknown."""
    try:
        info = os.stat(path)
    except (OSError, ValueError):
        return None
    if info.st_ino == 0:  # some file systems on Windows report no file index
        return None
    return (info.st_dev, info.st_ino)


def is_same_or_inside(path: Path, folder: Path) -> bool:
    """Whether ``path`` is ``folder`` or lies anywhere inside it, compared by file identity.

    ``path`` is resolved, then it and each of its ancestors that exists is
    compared with ``folder``: equal when both have the same file identity
    (:func:`file_identity`), or, as a fast path, the same resolved spelling.
    Parts of ``path`` that do not exist yet are skipped, so a results folder
    that is still to be created is checked too. May raise :class:`OSError` or
    :class:`RuntimeError` (a link loop) from resolving.
    """
    target = folder.resolve()
    target_id = file_identity(target)
    resolved = path.resolve()
    for candidate in (resolved, *resolved.parents):
        if os.path.normcase(candidate) == os.path.normcase(target):
            return True
        if target_id is not None and file_identity(candidate) == target_id:
            return True
    return False
