"""The prompt files: data in ``a2m/prompts/``, never text in code.

There is one file per item kind (:data:`PROMPT_FILES` for translations,
:data:`FIX_PROMPT_FILE` for the fix loop of :mod:`a2m.verify.fix_loop`), read
with :mod:`importlib.resources`. The environment variable ``A2M_PROMPTS_DIR``
points a2m at another folder holding the same files, so a prompt can be edited
without touching Python. The translation prompts are read together
(:func:`load_prompts`); the fix prompt is read on its own when a fix is asked
for (:func:`load_prompt`), so a folder used only for translations need not hold
it.

A prompt holds placeholders such as ``{{original}}`` or ``{{location}}``.
:func:`render` replaces each in one pass over the prompt file only: the values
(source code, conditions) are inserted verbatim and never scanned again, so
braces, ``${...}`` and ``%s`` in them stay exactly as written. A placeholder
with no value is left as it is.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from importlib import resources
from pathlib import Path

from a2m.ai.provider import ItemKind
from a2m.errors import A2mError

PROMPTS_ENV = "A2M_PROMPTS_DIR"
PROMPT_FILES: dict[ItemKind, str] = {
    ItemKind.JAVASCRIPT: "translate_javascript.md",
    ItemKind.PYTHON: "translate_python.md",
    ItemKind.JAVA: "translate_java.md",
    ItemKind.EXPRESSION: "translate_expression.md",
}
FIX_PROMPT_FILE = "fix.md"
PLACEHOLDER = re.compile(r"\{\{([a-z_]+)\}\}")
# Every prompt must send the item itself.
REQUIRED_PLACEHOLDER = "original"


class PromptError(A2mError):
    """A prompt file is missing, unreadable or does not include the item's code."""


def prompts_folder(environ: Mapping[str, str] | None = None) -> Path | None:
    """The folder named by ``A2M_PROMPTS_DIR``, or None for the packaged prompts."""
    env = os.environ if environ is None else environ
    value = env.get(PROMPTS_ENV, "")
    return Path(value) if value.strip() else None


def load_prompts(environ: Mapping[str, str] | None = None) -> dict[ItemKind, str]:
    """Every translation prompt file's text, by kind; :class:`PromptError` when one cannot be used."""
    return {kind: load_prompt(name, environ, what=kind.value) for kind, name in PROMPT_FILES.items()}


def load_prompt(name: str, environ: Mapping[str, str] | None = None, *, what: str | None = None) -> str:
    """The text of the prompt file ``name`` (from ``A2M_PROMPTS_DIR`` or the package); :class:`PromptError` when it
    cannot be read or has no ``{{original}}`` placeholder. ``what`` names the prompt in the error."""
    folder = prompts_folder(environ)
    label = what or name
    try:
        if folder is None:
            text = resources.files("a2m").joinpath("prompts", name).read_text(encoding="utf-8")
            shown = f"a2m/prompts/{name}"
        else:
            path = folder / name
            shown = str(path)
            text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        where = f"{PROMPTS_ENV}={folder}" if folder is not None else "the a2m package"
        raise PromptError(f"cannot read the {label} prompt {name} from {where}: {exc}") from None
    if "{{" + REQUIRED_PLACEHOLDER + "}}" not in text:
        raise PromptError(
            f"the prompt {shown} has no {{{{{REQUIRED_PLACEHOLDER}}}}} placeholder, so the item's code "
            "would not be sent"
        )
    return text


def render(template: str, values: Mapping[str, str]) -> str:
    """``template`` with each ``{{name}}`` replaced by ``values[name]``, in one pass (see the module docstring)."""
    return PLACEHOLDER.sub(lambda match: values.get(match.group(1), match.group(0)), template)
