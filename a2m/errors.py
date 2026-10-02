"""Exception types shared across a2m.

This module imports nothing from the rest of the package, so any layer may
raise these without creating an upward dependency.
"""

from __future__ import annotations


class A2mError(Exception):
    """Base class for errors a2m reports to the user as one clear line."""


class UsageError(A2mError):
    """The command line or its inputs cannot be used; nothing was processed."""


class BundleError(A2mError):
    """One input bundle cannot be read; that proxy is refused, the batch goes on."""


class BundleLayoutError(BundleError):
    """An item holds a bundle root (apiproxy/) somewhere a2m does not accept: nested too deep, or several."""


class UnsafeBundleError(BundleError):
    """A zip or folder bundle is unsafe to copy, so that proxy is refused.

    For example: a zip member path that leaves its folder, a link or special
    file in the bundle root, names that collide, an item that changed after it
    was found, an end record that cannot be read, or a size or count over a
    limit.
    """


class UnsafePathError(A2mError):
    """A path a2m was about to write or delete would leave its own folder."""


class NotPlainFileError(UnsafePathError):
    """A file a2m opens in the results folder (run.log, the lock) is a FIFO, device, folder or link."""


class LockHeldError(A2mError):
    """Another process holds the lock a2m needs (another run uses the results folder)."""
