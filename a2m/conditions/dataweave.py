"""Turn a parsed Apigee condition, or an Apigee message template, into a DataWeave expression.

Output shape (CP5 plan): every comparison is ``(LHS OP RHS)`` and every
and/or/not node has its own brackets, so DataWeave's precedence (which
differs from Apigee's, notably for ``not``) can never change the meaning.

* ``=``, ``==``, ``Equals`` -> ``==``; ``!=``, ``NotEquals`` -> ``!=``.
* ``:=`` (EqualsCaseInsensitive) -> ``(lower(LHS) == lower("value"))``.
* ``Matches``/``Like``/``~``, ``MatchesPath``/``LikePath``/``~/`` and
  ``JavaRegex``/``~~`` -> ``((LHS default "") matches /REGEX/)``; the default
  keeps a missing variable from raising an error in DataWeave. DataWeave's
  ``matches`` tests the whole value, as Apigee does.
* String literals are double-quoted DataWeave strings with ``\\``, ``"`` and
  ``$`` escaped (``$`` right before ``{`` becomes ``\\u0024`` so Mule's
  ``${...}`` property placeholders never see it).

Pattern operators:

* ``Matches``: ``*`` is any run of characters; every other character is taken
  literally. ``?`` and backslashes are refused (their Apigee meaning is not
  certain). '%' escapes, in Matches and MatchesPath: ``%*`` is a literal '*'
  and ``%%`` a literal '%'; any other '%' is refused.
* ``MatchesPath``: ``*`` is one path segment (any characters but ``/``,
  possibly none), ``**`` any number of segments. a2m's choice for the trailing
  slash: one trailing slash on the request path is ignored, so ``/orders``
  matches ``/orders`` and ``/orders/``, and ``/orders/*`` matches
  ``/orders/1`` and ``/orders/1/``, but never ``/orders/1/items``. A trailing
  slash in the pattern is dropped first (``/orders/`` means ``/orders``).
  ``?``, ``{``, ``}`` and backslashes are refused.
* ``JavaRegex``: passed through, with every unescaped ``/`` written ``\\/``
  for the DataWeave regex literal. A regex that does not compile, uses a form
  Java reads differently, or could break the Mule file (``${``) is refused.

A check on ``proxy.pathsuffix`` whose result differs between '' and '/' is
refused: Mule reports '/' for both the base path and the base path with a
trailing slash. A pattern that matches the empty text is refused on a variable
that can be missing: DataWeave would see "" and match, where Apigee's result for a missing
variable is not documented.

Comparisons with "" and null: Apigee reads a missing variable as empty text in
some string comparisons, DataWeave reads it as null (``null == ""`` is false),
so ``=``, ``!=`` and ``:=`` between "" and a variable that can be missing (a
request header or query parameter, a flow variable) are refused. A variable
that is always there (``request.verb``, ``proxy.pathsuffix``) compared with
null would be a constant, so that is refused too (for ``proxy.pathsuffix`` it
is also the base path ambiguity above).

What is refused: numbers on the right (Apigee compares numbers and text by its
own rules), bare true/false, StartsWith and the ordering operators, null with
anything but = and !=, the "" and null cases above, and every unmapped variable
(see :mod:`a2m.conditions.variables`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from a2m.conditions.lexer import ConditionError, tokenize
from a2m.conditions.parser import Binary, Comparison, Literal, LiteralKind, Node, Not, Operator, parse
from a2m.conditions.template import template_parts
from a2m.conditions.variables import (
    NO_CHANGES,
    PATH_SUFFIX,
    REQUEST,
    SNAPSHOT_READ,
    RequestChanges,
    accessor,
    fold,
)

# Mule's maskedRequestPath is '/' both for the base path itself and for the base path with a trailing
# slash, where Apigee's proxy.pathsuffix is '' and '/'; a check that tells those two apart can't be kept.
PATH_SUFFIX_ROOT = (
    "the generated app sees the same path suffix '/' for the base path and for the base path with a trailing "
    "slash (Apigee has '' and '/'), so this check would not give Apigee's result"
)
PATTERN_OPERATORS = (Operator.MATCHES, Operator.MATCHES_PATH, Operator.JAVA_REGEX)
REGEX_SPECIAL = frozenset("\\.[]{}()*+?^$|")
# A {n}, {n,} or {n,m} quantifier.
QUANTIFIER = re.compile(r"\{\d+(?:,\d*)?\}")


@dataclass(frozen=True, slots=True)
class Translation:
    """The result of translating one condition or template.

    ``ok`` True with ``dw`` the DataWeave expression (without ``#[ ]``), or
    with ``dw`` None when there is nothing to translate (an empty condition,
    a template without variables). ``ok`` False: ``dw`` is None and
    ``reason`` says why. ``original`` is always the input, unchanged.
    ``reads_request_snapshot`` is True when ``dw`` reads the request snapshot
    the generator saves before the target call (response-side request reads).
    """

    original: str
    ok: bool
    dw: str | None
    reason: str | None = None
    reads_request_snapshot: bool = False


def _done(text: str, dw: str) -> Translation:
    return Translation(text, ok=True, dw=dw, reads_request_snapshot=SNAPSHOT_READ in dw)


def translate_condition(
    text: str, *, direction: str = REQUEST, changes: RequestChanges = NO_CHANGES
) -> Translation:
    """``text`` (an Apigee condition) as DataWeave, read on the ``direction`` side of a flow.

    ``changes`` are the request headers and query parameters that earlier
    steps on the same path may have changed; a condition reading one of them
    can't be translated (see :func:`a2m.conditions.variables.accessor`).

    An empty or whitespace-only condition is always true in Apigee: ok with no
    expression. Anything that cannot be translated exactly is ok False with
    the reason; a part of a condition is never translated on its own.
    """
    if not text.strip():
        return Translation(text, ok=True, dw=None)
    try:
        dw = emit(parse(tokenize(text)), direction, changes)
    except ConditionError as exc:
        return Translation(text, ok=False, dw=None, reason=str(exc))
    return _done(text, dw)


def translate_template(
    text: str,
    prefix: str = "{",
    suffix: str = "}",
    *,
    direction: str = REQUEST,
    changes: RequestChanges = NO_CHANGES,
) -> Translation:
    """``text`` (an Apigee message template such as ``Bearer {request.header.X-Token}``) as DataWeave.

    The template is split by :func:`a2m.conditions.template.template_parts`
    (the one template reader): a reference is ``prefix`` + a variable name +
    ``suffix``, optionally with ``:default``; JSON braces, ``price {`` and
    ``{}`` are literal text. Literal parts become DataWeave strings, variables
    ``(ACCESSOR default "")`` since Apigee writes an empty string for a missing
    template variable (``(ACCESSOR default "DEFAULT")`` with a default), joined
    with ``++``. Text without references is ok with no expression (use it as
    written). A reference to an unmapped variable, a template function, any
    other reference form a2m does not understand, or a request header or query
    parameter that ``changes`` says an earlier step may have changed makes the
    whole template ok False, naming it.
    """
    pieces: list[str] = []
    problems: list[str] = []
    references = 0
    for part in template_parts(text, prefix, suffix):
        if part.problem is not None:
            problems.append(part.problem)
        elif part.variable is None:
            pieces.append(dw_string(part.text))
        else:
            references += 1
            try:
                read = accessor(part.variable, direction, changes).dw
            except ConditionError as exc:
                problems.append(str(exc))
                continue
            pieces.append(f"({read} default {dw_string(part.default or '')})")
    if problems:
        return Translation(text, ok=False, dw=None, reason="; ".join(dict.fromkeys(problems)))
    if not references:
        return Translation(text, ok=True, dw=None)
    return _done(text, " ++ ".join(pieces))


# ---------------------------------------------------------------- conditions


def emit(node: Node, direction: str, changes: RequestChanges = NO_CHANGES) -> str:
    """DataWeave for ``node``; raises :class:`ConditionError` when any part of it cannot be translated."""
    if isinstance(node, Binary):
        left, right = emit(node.left, direction, changes), emit(node.right, direction, changes)
        return f"({left} {node.connective.value} {right})"
    if isinstance(node, Not):
        return f"(not {emit(node.operand, direction, changes)})"
    return _comparison(node, direction, changes)


def _comparison(node: Comparison, direction: str, changes: RequestChanges) -> str:
    where = f"{node.variable} {node.spelling} {_shown(node.value)}"
    value = node.value
    if node.operator is Operator.STARTS_WITH:
        raise ConditionError(f"{where}: the StartsWith operator ({node.spelling}) is not translated")
    if node.operator in (Operator.GREATER, Operator.GREATER_OR_EQUAL, Operator.LESS, Operator.LESS_OR_EQUAL):
        raise ConditionError(
            f"{where}: ordering comparisons ({node.spelling}) are not translated, since Apigee compares numbers "
            "and text by its own rules"
        )
    if value.kind is LiteralKind.NUMBER:
        raise ConditionError(
            f"{where}: comparing with the number {value.text} is not translated, since Apigee converts between "
            "numbers and text by its own rules"
        )
    if value.kind is LiteralKind.BOOLEAN:
        raise ConditionError(f"{where}: comparing with the bare value {value.text} is not translated")
    if value.kind is LiteralKind.NULL and node.operator not in (Operator.EQUALS, Operator.NOT_EQUALS):
        raise ConditionError(f"{where}: null can only be compared with = or !=")
    lhs = accessor(node.variable, direction, changes)
    if node.operator in PATTERN_OPERATORS:
        regex = _regex(node.operator, value.text, where)
        matches_empty = re.fullmatch(_python_regex(regex), "") is not None
        if lhs.nullable and matches_empty:
            raise ConditionError(
                f"{where}: the pattern also matches empty text, and Apigee's result for a missing {node.variable} "
                "is not documented"
            )
        if node.variable == PATH_SUFFIX and matches_empty != (re.fullmatch(_python_regex(regex), "/") is not None):
            raise ConditionError(f"{where}: {PATH_SUFFIX_ROOT}")
        return f'(({lhs.dw} default "") matches /{regex}/)'
    if node.variable == PATH_SUFFIX and (value.kind is LiteralKind.NULL or fold(value.text) in ("", "/")):
        raise ConditionError(f"{where}: {PATH_SUFFIX_ROOT}")
    if value.kind is LiteralKind.NULL and not lhs.nullable:
        raise ConditionError(
            f"{where}: {node.variable} always has a value, so comparing it with null would be a constant result"
        )
    if value.kind is LiteralKind.STRING and value.text == "" and lhs.nullable:
        raise ConditionError(
            f"{where}: {node.variable} can be missing; Apigee may read a missing variable as empty text here, where "
            "the generated app reads it as null, so the result could differ from Apigee's"
        )
    rhs = "null" if value.kind is LiteralKind.NULL else dw_string(value.text)
    if node.operator is Operator.EQUALS:
        return f"({lhs.dw} == {rhs})"
    if node.operator is Operator.NOT_EQUALS:
        return f"({lhs.dw} != {rhs})"
    return f"(lower({lhs.dw}) == lower({rhs}))"


def _shown(value: Literal) -> str:
    return f'"{value.text}"' if value.kind is LiteralKind.STRING and value.text else value.text or '""'


# ---------------------------------------------------------------- patterns


def _regex(operator: Operator, pattern: str, where: str) -> str:
    """The body of the DataWeave regex literal for ``pattern`` under ``operator``."""
    if not pattern:
        raise ConditionError(f"{where}: the pattern is empty")
    if operator is Operator.JAVA_REGEX:
        regex = _java_regex(pattern, where)
    elif operator is Operator.MATCHES:
        regex = _glob(pattern, where)
    else:
        regex = _path(pattern, where)
    return regex


def _escape(char: str) -> str:
    if char == "/":
        return "\\/"
    return "\\" + char if char in REGEX_SPECIAL else char


def _refuse_chars(pattern: str, chars: str, where: str) -> None:
    for char in chars:
        if char in pattern:
            raise ConditionError(f"{where}: the pattern holds {char!r}, whose meaning in Apigee is not certain")


def _glob_tokens(pattern: str, where: str) -> list[str]:
    """``pattern`` (Matches or MatchesPath) as tokens: ``*`` for a wildcard, every other token a literal character.

    Apigee escapes a special character with '%': ``%*`` is a literal '*' and
    ``%%`` a literal '%'. Any other use of '%' (before another character, or at
    the end) is refused, since its meaning in Apigee is not certain.
    """
    tokens: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "%":
            escaped = pattern[index + 1 : index + 2]
            if escaped not in ("*", "%"):
                shown = f"'%{escaped}'" if escaped else "a '%' at its end"
                raise ConditionError(
                    f"{where}: the pattern holds {shown}; only %* and %% are known escapes in Apigee, so its "
                    "meaning is not certain"
                )
            tokens.append("%" + escaped)
            index += 2
            continue
        tokens.append(char)
        index += 1
    return tokens


def _literal(token: str) -> str:
    """The regex for a literal token of :func:`_glob_tokens` (an escape stands for its second character)."""
    return _escape(token[-1])


def _glob(pattern: str, where: str) -> str:
    _refuse_chars(pattern, "?\\", where)
    return "".join(".*" if token == "*" else _literal(token) for token in _glob_tokens(pattern, where))


def _path(pattern: str, where: str) -> str:
    _refuse_chars(pattern, "?\\{}", where)
    tokens = _glob_tokens(pattern, where)
    if tokens != ["/"]:
        while tokens and tokens[-1] == "/":
            tokens.pop()
    else:
        tokens = []
    out: list[str] = []
    index = 0
    while index < len(tokens):
        if tokens[index : index + 2] == ["*", "*"]:
            out.append(".*")
            index += 2
        elif tokens[index] == "*":
            out.append("[^\\/]*")
            index += 1
        else:
            out.append(_literal(tokens[index]))
            index += 1
    if tokens[-2:] != ["*", "*"]:
        out.append("\\/?")
    return "".join(out)


def _java_regex(pattern: str, where: str) -> str:
    """``pattern`` with unescaped slashes escaped, after checking it is a regex Java and DataWeave accept."""
    if "${" in pattern:
        raise ConditionError(f"{where}: the regex holds '${{', which Mule would read as a property placeholder")
    if "(?P" in pattern:
        raise ConditionError(f"{where}: the regex uses a Python-only group form (?P...), which Java does not accept")
    out: list[str] = []
    in_class = False
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "\\":
            if index + 1 >= len(pattern):
                raise ConditionError(f"{where}: the regex ends with a lone backslash")
            if pattern[index + 1] in "pP":
                raise ConditionError(f"{where}: the regex uses a \\p{{...}} character class, which a2m cannot check")
            out.append(pattern[index : index + 2])
            index += 2
            continue
        if in_class:
            in_class = char != "]"
        elif char == "[":
            in_class = True
            # A ']' right after '[' (or '[^') is a literal member of the class, as in Java.
            lead = 2 if pattern.startswith("[^", index) else 1
            if pattern[index + lead : index + lead + 1] == "]":
                out.append(pattern[index : index + lead + 1])
                index += lead + 1
                continue
        elif char == "{" and not QUANTIFIER.match(pattern, index):
            raise ConditionError(f"{where}: the regex has a '{{' that is not a repeat count, which Java refuses")
        out.append("\\/" if char == "/" else char)
        index += 1
    regex = "".join(out)
    try:
        re.compile(_python_regex(regex))
    except re.error as exc:
        raise ConditionError(f"{where}: the regex is not valid ({exc})") from exc
    return regex


def _python_regex(regex: str) -> str:
    """A DataWeave regex body as Python reads it (only the slash escape differs)."""
    return regex.replace("\\/", "/")


# ---------------------------------------------------------------- strings


def dw_string(value: str) -> str:
    """``value`` as a double-quoted DataWeave string that decodes back to exactly ``value``."""
    out: list[str] = []
    for index, char in enumerate(value):
        if char == "\\":
            out.append("\\\\")
        elif char == '"':
            out.append('\\"')
        elif char == "$":
            out.append("\\u0024" if value[index + 1 : index + 2] == "{" else "\\$")
        elif char == "\n":
            out.append("\\n")
        elif char == "\r":
            out.append("\\r")
        elif char == "\t":
            out.append("\\t")
        elif ord(char) < 0x20:
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    return '"' + "".join(out) + '"'
