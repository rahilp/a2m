"""The placeholders of the AI fix loop: moved to :mod:`a2m.ai.placeholders`, which the AI translation of custom code
and conditions uses too (an AI module may not import from :mod:`a2m.verify`). This name keeps working."""

from __future__ import annotations

from a2m.ai.placeholders import (
    DATAWEAVE,
    JAVA,
    JAVASCRIPT,
    JSON_TEXT,
    PYTHON,
    TOKEN,
    TOKEN_SPLIT,
    PlaceholderError,
    Placeholders,
    code_shape,
    plain_visible,
)

__all__ = [
    "DATAWEAVE",
    "JAVA",
    "JAVASCRIPT",
    "JSON_TEXT",
    "PYTHON",
    "TOKEN",
    "TOKEN_SPLIT",
    "PlaceholderError",
    "Placeholders",
    "code_shape",
    "plain_visible",
]
