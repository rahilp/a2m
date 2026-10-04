"""The fake provider behind ``--llm fake``: canned answers, no network, no key.

With a folder of canned answers (the environment variable ``A2M_FAKE_LLM_DIR``,
or ``answers_dir``), the answer for an item is the file
``<kind>.<name>.json`` there (for example ``javascript.JS-AddCorrelation.json``
or ``expression.curl-clients.json``), read as is. Without one, or when no file
matches, it gives a2m's built-in answers (package data in ``a2m/ai/canned/``):
a callout becomes a low-confidence placeholder step that only logs a warning,
so it is always flagged for review, and a condition is declined, so it keeps
CP5's ``#[false]``. The fake never claims to have translated anything, and
its placeholder declares no writes (``"writes": null``), so the step keeps
"may change anything" for every later read.
"""

from __future__ import annotations

import re
from importlib import resources
from pathlib import Path

from a2m.ai.provider import AiRequest, ItemKind

FAKE_ANSWERS_ENV = "A2M_FAKE_LLM_DIR"
# Names that can be part of a canned answer's file name: no path separators, no leading dot.
SAFE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")


class FakeProvider:
    """Answers from canned files; records nothing and never opens a socket."""

    def __init__(self, answers_dir: Path | None = None) -> None:
        self.answers_dir = answers_dir

    def complete(self, request: AiRequest) -> str:
        if self.answers_dir is not None and SAFE_NAME.fullmatch(request.name):
            path = self.answers_dir / f"{request.kind.value}.{request.name}.json"
            if path.is_file():
                return path.read_text(encoding="utf-8")
        builtin = "expression.json" if request.kind is ItemKind.EXPRESSION else "callout.json"
        return resources.files("a2m.ai").joinpath("canned", builtin).read_text(encoding="utf-8")
