"""Read Apigee bundles into the IR (:mod:`a2m.ir`).

The one public entry point is :func:`read_bundle`: a bundle folder or a
``.zip`` export in, a :class:`a2m.ir.Bundle` out, or
:class:`a2m.errors.BundleError` for a bundle that cannot be read.
"""

from __future__ import annotations

from a2m.parser.bundle import read_bundle

__all__ = ["read_bundle"]
