"""The one reader of Apigee message templates (``Bearer {request.header.X-Token}``).

Every place a2m reads a message template (policy values, payloads, target
URLs, the AssignVariable check for references) splits it with
:func:`template_parts`, so they all agree on what is a reference.

A reference is the variable prefix, the text up to the first variable suffix
after it, and that suffix. What sits between them decides what it is:

* a variable name (``{request.header.X-Token}``): a reference;
* a variable name, ':' and a default (``{request.header.X-Id:unknown}``): a
  reference with a default, which Apigee writes when the variable is missing
  or null. Only the default ``{`` ``}`` delimiters are read this way, and
  only a default without ':', braces, delimiters or surrounding spaces;
  anything else is not certain;
* anything else that starts like a variable name or a function call
  (``{toUpperCase(request.verb)}``, ``{ request.verb }``, ``{a:b:c}``,
  ``{request.header.X[0]}``): a part a2m does not understand, so the template
  can't be translated (never written out as literal text);
* text that does not start like a variable name (JSON such as
  ``{"id": 1}``, ``{}``, a lone ``{``): literal text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]*")
# A variable name, ':' and a default: no ':', braces or surrounding spaces in the default.
WITH_DEFAULT = re.compile(r"(?P<name>[A-Za-z_][A-Za-z0-9_.\-]*):(?P<default>(?:[^:{}\s](?:[^:{}]*[^:{}\s])?)?)")
FUNCTION = re.compile(r"\s*([A-Za-z_]\w*)\s*\(")
LOOKS_LIKE_REFERENCE = re.compile(r"\s*[A-Za-z_]")
DEFAULT_PREFIX = "{"
DEFAULT_SUFFIX = "}"


@dataclass(frozen=True, slots=True)
class TemplatePart:
    """One part of a message template, in order.

    Literal text has ``variable`` and ``problem`` None. A reference has
    ``variable`` (and ``default`` when it gives one, else None: Apigee writes
    an empty string for a missing variable). A part a2m does not understand
    has ``problem``, saying why. ``text`` is the part as written.
    """

    text: str
    variable: str | None = None
    default: str | None = None
    problem: str | None = None


def template_parts(text: str, prefix: str = DEFAULT_PREFIX, suffix: str = DEFAULT_SUFFIX) -> list[TemplatePart]:
    """``text`` split into literal text, references and parts a2m does not understand (see the module docstring).

    Neighbouring literal characters are joined into one part.
    """
    if not prefix or not suffix:
        raise ValueError("a template needs a non-empty variable prefix and suffix")
    parts: list[TemplatePart] = []
    literal: list[str] = []

    def flush() -> None:
        if literal:
            parts.append(TemplatePart("".join(literal)))
            literal.clear()

    index = 0
    while index < len(text):
        if text.startswith(prefix, index):
            start = index + len(prefix)
            end = text.find(suffix, start)
            if end >= 0:
                part = _reference(text[index : end + len(suffix)], text[start:end], prefix, suffix)
                if part is not None:
                    flush()
                    parts.append(part)
                    index = end + len(suffix)
                    continue
        literal.append(text[index])
        index += 1
    flush()
    return parts


def _reference(written: str, inner: str, prefix: str, suffix: str) -> TemplatePart | None:
    """The reference ``written`` (``prefix`` + ``inner`` + ``suffix``) as a part, or None when it is literal text."""
    if VARIABLE.fullmatch(inner):
        return TemplatePart(written, variable=inner)
    function = FUNCTION.match(inner)
    if function is not None:
        return TemplatePart(
            written,
            problem=f"it calls the message template function {function.group(1)}, which a2m does not translate",
        )
    default = WITH_DEFAULT.fullmatch(inner)
    if default is not None and prefix == DEFAULT_PREFIX and suffix == DEFAULT_SUFFIX:
        return TemplatePart(written, variable=default.group("name"), default=default.group("default"))
    if LOOKS_LIKE_REFERENCE.match(inner):
        return TemplatePart(
            written,
            problem=f"it holds {written}, a reference form a2m does not understand, so it can't be read exactly",
        )
    return None


def has_reference(text: str, prefix: str = DEFAULT_PREFIX, suffix: str = DEFAULT_SUFFIX) -> bool:
    """True when ``text`` holds a reference, or a part a2m does not understand, that a literal copy would not
    resolve."""
    return any(part.variable is not None or part.problem is not None for part in template_parts(text, prefix, suffix))
