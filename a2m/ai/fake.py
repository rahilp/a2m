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

A fix request (:mod:`a2m.verify.fix_loop`) is answered from the ``fix/``
folder there, by the proxy's name and the number of the fix asked for that
proxy so far: the first from ``fix/<proxy>.json``, the second from
``fix/<proxy>.2.json``, and so on. Without such a file the built-in answer
declines (``cannot_fix``), so the fake never changes a project on its own.
"""

from __future__ import annotations

import re
from importlib import resources
from pathlib import Path

from a2m.ai.provider import AiRequest, ItemKind

FAKE_ANSWERS_ENV = "A2M_FAKE_LLM_DIR"
# Names that can be part of a canned answer's file name: no path separators, no leading dot.
SAFE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")


FIX_FOLDER = "fix"
BUILTIN_ANSWERS = {ItemKind.EXPRESSION: "expression.json", ItemKind.FIX: "fix.json"}


class FakeProvider:
    """Answers from canned files; never opens a socket. It counts only the fix requests per proxy (to pick the
    numbered answer file), nothing else."""

    def __init__(self, answers_dir: Path | None = None) -> None:
        self.answers_dir = answers_dir
        self._fixes: dict[str, int] = {}

    def complete(self, request: AiRequest) -> str:
        path = self._canned_path(request)
        if path is not None and path.is_file():
            return path.read_text(encoding="utf-8")
        builtin = BUILTIN_ANSWERS.get(request.kind, "callout.json")
        return resources.files("a2m.ai").joinpath("canned", builtin).read_text(encoding="utf-8")

    def _canned_path(self, request: AiRequest) -> Path | None:
        if request.kind is ItemKind.FIX:
            number = self._fixes.get(request.name, 0) + 1
            self._fixes[request.name] = number
            if self.answers_dir is None or not SAFE_NAME.fullmatch(request.name):
                return None
            suffix = "" if number == 1 else f".{number}"
            return self.answers_dir / FIX_FOLDER / f"{request.name}{suffix}.json"
        if self.answers_dir is None or not SAFE_NAME.fullmatch(request.name):
            return None
        return self.answers_dir / f"{request.kind.value}.{request.name}.json"
