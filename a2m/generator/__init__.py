"""The Mule 4 project generator: one proxy's IR in, a mule-app folder out.

The public entry point is :func:`generate_project`; see :mod:`a2m.generator.project`.
"""

from __future__ import annotations

from a2m.generator.project import (
    GenerateResult,
    GeneratorError,
    PendingCondition,
    UnsupportedItem,
    generate_project,
)

__all__ = ["GenerateResult", "GeneratorError", "PendingCondition", "UnsupportedItem", "generate_project"]
