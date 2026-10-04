"""The static checks a2m runs on what the AI answers and on what a callout reads, before anything is used.

AI conditions are structure, not code. The AI answers a condition as a small
tree (:func:`condition_from_json`)::

    {"and": [NODE, NODE, ...]}   {"or": [NODE, NODE, ...]}   {"not": NODE}
    {"variable": "request.header.User-Agent", "operator": "starts-with", "value": "curl"}

The operators are CP5's (:class:`a2m.conditions.parser.Operator`), the
variable is an Apigee variable name and the value a literal (text, a number or
null). a2m checks the tree (:func:`structured_condition`):

* every variable is resolved by CP5's own accessor
  (:func:`a2m.conditions.variables.accessor`), with its faithfulness,
  staleness and built-in rules, so an unmapped built-in or a value an earlier
  step may have changed is refused; a2m's own flow variables are refused too;
* literal kinds must suit the operator (numbers only with the ordering
  operators, null only with equals and not-equals, never true or false);
* the condition must not be a constant: it is evaluated for every assignment
  of a small domain of values to each read (null where the read can be
  missing, "", every literal of the condition with near misses, values derived
  from its patterns, and a fresh value no literal names), each taken as the
  written read gives it (a header's first value, trimmed; a verb is an HTTP
  token; values a read can never give are left out), and refused when the
  result is the same for all of them;
* the DataWeave is then written by a2m: CP5's emitter for the comparisons it
  translates, and the same accessor with CP5's output shape for StartsWith; the
  ordering operators are refused, as CP5 refuses them. The AI never writes
  condition DataWeave.

An answer in the earlier form, a DataWeave string, is accepted only when it
parses (:func:`condition_from_dataweave`) into the same tree, in a small
DataWeave subset: one read of an Apigee value (the forms a2m's own emitter
uses, the raw header or query parameter entry, a flow variable, the body and
its fields) compared with a literal, ``startsWith``, ``and``/``or`` (one kind
per bracket level) and ``not (...)``. The same checks then apply, and a2m
writes the tree itself, as for the structured form (:func:`dataweave_condition`):
the AI's string is never written.

:func:`fragment_writes` lists what an AI Mule fragment writes, by exact key, in
the forms a2m recognises (``set-variable``, ``remove-variable`` and
``ee:set-variable`` by variable name; a header or query parameter map written
with one literal key added or removed); any other write to those maps, to the
attributes or to the body means the step may change all of them.
:func:`declared_writes` accepts the AI's ``writes`` declaration as the step's
write model only when it names exactly those keys (header names compared
without case, query parameters and variables exactly). :func:`script_reads`
finds the Apigee variables a callout's code reads, when that can be told.
"""

from __future__ import annotations

import importlib
import itertools
import math
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from typing import Any

from a2m.ai.provider import ItemKind
from a2m.conditions.dataweave import MISSING_READS_AS, PATH_SUFFIX_ROOT, _python_regex, _regex, dw_string, emit
from a2m.conditions.lexer import ConditionError
from a2m.conditions.parser import (
    NUMBER,
    VARIABLE_NAME,
    Comparison,
    Connective,
    Literal,
    LiteralKind,
    Operator,
)
from a2m.conditions.variables import (
    ANY,
    EXACT_PATH_SUFFIX_DW,
    HEADER_PREFIX,
    NO_CHANGES,
    PATH_SUFFIX,
    QUERY_PREFIX,
    REQUEST_CONTENT,
    RESPONSE_CONTENT,
    RESPONSE_HEADER_PREFIX,
    RESPONSE_HEADERS_BASE,
    RESPONSE_HEADERS_VAR,
    SNAPSHOT_VAR,
    VERB,
    Accessor,
    RequestChanges,
    accessor,
    first_value,
    fold,
    is_custom_variable,
    stale_part,
)
from a2m.policies.common import REASON_PHRASE_VAR, REQUEST_HEADERS_VAR, REQUEST_QUERY_VAR, STATUS_VAR

CORE = "http://www.mulesoft.org/schema/mule/core"
EE = "http://www.mulesoft.org/schema/mule/ee/core"
RESPONSE = "response"

# a2m's own flow variables in the generated app: never read as the proxy's variables in an AI condition.
A2M_VARIABLES = frozenset(
    fold(name)
    for name in (
        REQUEST_HEADERS_VAR,
        REQUEST_QUERY_VAR,
        STATUS_VAR,
        REASON_PHRASE_VAR,
        RESPONSE_HEADERS_VAR,
        SNAPSHOT_VAR,
        "a2mFlow",
        "a2mTargetFlow",
    )
)
CONSTANT = "it would always give the same result (for example always true, letting every request in)"


class CheckError(ValueError):
    """What the AI answered cannot be used; the message says why."""


# ---------------------------------------------------------------- the condition tree


@dataclass(frozen=True, slots=True)
class Compare:
    """``variable`` (an Apigee variable name) compared with ``value``. ``path`` selects fields of the body (only in
    the DataWeave form); ``default`` is what a missing value reads as (DataWeave's ``default``), None for null.
    ``empty`` (only in a callout's choice guard, :func:`guard_condition`): the read is tested with ``isEmpty`` (null
    or ""), not compared; ``operator`` and ``value`` are then equals and null. ``raw`` (only in a callout's choice
    guard): a header read written as Mule's own entry (``attributes.headers['name']``), which gives the whole header
    value as sent, commas and spaces included, not Apigee's first value; a2m always writes its first-value read."""

    variable: str
    operator: Operator
    value: Literal
    path: tuple[str, ...] = ()
    default: str | None = None
    empty: bool = False
    raw: bool = False


@dataclass(frozen=True, slots=True)
class Junction:
    connective: Connective
    parts: tuple[Cond, ...]


@dataclass(frozen=True, slots=True)
class Negation:
    operand: Cond


Cond = Compare | Junction | Negation

ORDERING = {
    Operator.GREATER: ">",
    Operator.GREATER_OR_EQUAL: ">=",
    Operator.LESS: "<",
    Operator.LESS_OR_EQUAL: "<=",
}
PATTERNS = frozenset({Operator.MATCHES, Operator.MATCHES_PATH, Operator.JAVA_REGEX})
# The comparisons CP5's emitter writes; StartsWith and the ordering operators are written here, in the same shape.
CP5_OPERATORS = frozenset({Operator.EQUALS, Operator.NOT_EQUALS, Operator.EQUALS_IGNORE_CASE} | PATTERNS)
OPERATORS_BY_NAME = {operator.value: operator for operator in Operator}
MAX_DEPTH = 16
MAX_COMPARISONS = 32
MAX_ASSIGNMENTS = 100_000
COMPARE_KEYS = frozenset({"variable", "operator", "value"})


def _compares(node: Cond) -> Iterator[Compare]:
    if isinstance(node, Compare):
        yield node
    elif isinstance(node, Negation):
        yield from _compares(node.operand)
    else:
        for part in node.parts:
            yield from _compares(part)


def condition_from_json(value: Any) -> Cond:
    """The condition tree of the AI's structured answer (see the module docstring); :class:`CheckError` otherwise."""
    count = 0

    def node(item: Any, depth: int) -> Cond:
        nonlocal count
        if depth > MAX_DEPTH:
            raise CheckError("its condition is nested too deeply")
        if not isinstance(item, dict):
            raise CheckError("its condition has a part that is not a JSON object")
        keys = set(item)
        if keys in ({"and"}, {"or"}):
            (word,) = keys
            parts = item[word]
            if not isinstance(parts, list) or len(parts) < 2:
                raise CheckError(f"its condition has an {word!r} without a list of at least two parts")
            return Junction(Connective(word), tuple(node(part, depth + 1) for part in parts))
        if keys == {"not"}:
            return Negation(node(item["not"], depth + 1))
        if keys == COMPARE_KEYS:
            count += 1
            if count > MAX_COMPARISONS:
                raise CheckError(f"its condition has more than {MAX_COMPARISONS} comparisons")
            return _compare_from_json(item)
        raise CheckError(f"its condition has a part a2m does not know (keys {', '.join(sorted(map(str, keys)))})")

    return node(value, 0)


def _compare_from_json(item: dict[str, Any]) -> Compare:
    variable, name, value = item["variable"], item["operator"], item["value"]
    if not isinstance(variable, str) or not VARIABLE_NAME.fullmatch(variable):
        raise CheckError(f"its condition compares {variable!r}, which is not an Apigee variable name")
    operator = OPERATORS_BY_NAME.get(name) if isinstance(name, str) else None
    if operator is None:
        raise CheckError(f"its condition uses the operator {name!r}, which is not one of {', '.join(OPERATORS_BY_NAME)}")
    if isinstance(value, bool):
        raise CheckError("its condition compares with true or false, which can make a condition constant")
    if value is None:
        literal = Literal(LiteralKind.NULL, "null")
    elif isinstance(value, str):
        literal = Literal(LiteralKind.STRING, value)
    elif isinstance(value, int | float):
        text = str(value) if isinstance(value, int) else (repr(value) if math.isfinite(value) else "")
        if not NUMBER.fullmatch(text):
            raise CheckError(f"its condition compares with the number {value!r}, which a2m cannot write")
        literal = Literal(LiteralKind.NUMBER, text)
    else:
        raise CheckError("its condition compares with a value that is not text, a number or null")
    return Compare(variable, operator, literal, default=_missing_reads_as(operator))


def _missing_reads_as(operator: Operator) -> str | None:
    """What a missing value of an Apigee variable reads as in the comparison a2m writes: CP5's own table
    (:data:`a2m.conditions.dataweave.MISSING_READS_AS`) for the comparisons CP5 writes, and "" for StartsWith, which
    a2m writes here with ``default ""``; None for null."""
    return "" if operator is Operator.STARTS_WITH else MISSING_READS_AS.get(operator)


# ---------------------------------------------------------------- checking a tree


def _resolve(name: str, side: str, changes: RequestChanges, *, body: bool) -> Accessor:
    """CP5's accessor for ``name`` on ``side`` after ``changes``; :class:`CheckError` when CP5 would not read it
    faithfully. ``body`` allows the message body (``request.content``/``response.content``), read only in the
    DataWeave form, with CP5's staleness rule for it."""
    folded = fold(name.strip())
    if body and folded in (REQUEST_CONTENT, RESPONSE_CONTENT):
        own = RESPONSE_CONTENT if side == RESPONSE else REQUEST_CONTENT
        if folded != own:
            raise CheckError(f"its condition reads {name} on the {side} side, where the body is the other message")
        stale = stale_part(own, "body", changes)
        if stale is not None:
            raise CheckError(f"its condition reads a value a2m cannot vouch for: {stale}")
        return Accessor("payload", nullable=True)
    if is_custom_variable(name) and (folded in A2M_VARIABLES or folded.startswith("a2m")):
        raise CheckError(f"its condition reads a2m's own variable {name}, not one of the proxy's")
    try:
        return accessor(name, side, changes)
    except ConditionError as exc:
        raise CheckError(f"its condition reads a value a2m cannot vouch for: {exc}") from None


Key = tuple[str, tuple[str, ...]]


def _pattern(compare: Compare) -> re.Pattern[str]:
    where = f"{compare.variable} {compare.operator.value} {compare.value.text!r}"
    try:
        return re.compile(_python_regex(_regex(compare.operator, compare.value.text, where)))
    except ConditionError as exc:
        raise CheckError(f"its condition has a pattern a2m cannot use: {exc}") from None


def _check_compare(compare: Compare, side: str, changes: RequestChanges, *, body: bool) -> Accessor:
    """Check one comparison; the accessor a2m writes for the value it reads."""
    operator, kind = compare.operator, compare.value.kind
    if kind is LiteralKind.BOOLEAN:
        raise CheckError("its condition compares with true or false, which can make a condition constant")
    if operator in ORDERING:
        if kind is not LiteralKind.NUMBER:
            raise CheckError(f"its condition uses {operator.value} with a value that is not a number")
    elif kind is LiteralKind.NUMBER:
        raise CheckError(f"its condition compares with a number using {operator.value}; only ordering takes numbers")
    if kind is LiteralKind.NULL and operator not in (Operator.EQUALS, Operator.NOT_EQUALS):
        raise CheckError("its condition compares with null using an operator other than equals or not-equals")
    if operator in PATTERNS:
        _pattern(compare)
    read = _resolve(compare.variable, side, changes, body=body)
    if not _is_body(compare):
        # Every comparison a2m would refuse to write is refused here, whichever form the AI answered in. Only the
        # DataWeave form can read body fields, which a2m writes in its own spelling (:func:`_render_body`).
        _render(compare, side, changes)
    return read


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str) and NUMBER.fullmatch(value):
        return float(value)
    return None


def _holds(compare: Compare, value: object, patterns: dict[int, re.Pattern[str]]) -> bool:
    """``compare`` for ``value``, as the generated DataWeave reads it."""
    if compare.empty:
        return value is None or value == ""
    if value is None and compare.default is not None:
        value = compare.default
    operator, literal = compare.operator, compare.value
    if operator in (Operator.EQUALS, Operator.NOT_EQUALS):
        if literal.kind is LiteralKind.NULL:
            same = value is None
        else:
            same = isinstance(value, str) and value == literal.text
        return same if operator is Operator.EQUALS else not same
    if not isinstance(value, str | int | float) or isinstance(value, bool):
        return False
    if operator is Operator.EQUALS_IGNORE_CASE:
        return isinstance(value, str) and fold(value) == fold(literal.text)
    if operator is Operator.STARTS_WITH:
        return isinstance(value, str) and value.startswith(literal.text)
    if operator in PATTERNS:
        return isinstance(value, str) and patterns[id(compare)].fullmatch(value) is not None
    number, bound = _number(value), float(literal.text)
    if number is None:
        return False
    return {
        Operator.GREATER: number > bound,
        Operator.GREATER_OR_EQUAL: number >= bound,
        Operator.LESS: number < bound,
        Operator.LESS_OR_EQUAL: number <= bound,
    }[operator]


def _samples(compare: Compare) -> list[object]:
    """Values ``compare`` tells apart: its literal and near misses, and texts derived from its pattern."""
    literal = compare.value
    if literal.kind is LiteralKind.NUMBER:
        bound = float(literal.text)
        values: list[object] = []
        for number in (bound - 1, bound, bound + 1):
            values += [number, f"{number:g}"]
        return values
    if literal.kind is not LiteralKind.STRING:
        return []
    text = literal.text
    values = [text, text + "x", "x" + text, text.swapcase(), fold(text)]
    if compare.operator in PATTERNS:
        values += _regex_examples(_pattern(compare).pattern)
    return values


MAX_EXAMPLES = 16
# The longest example text, and all example texts of one pattern together, in characters: :func:`_regex_examples`
# refuses the condition (:data:`TOO_COMPLEX`) before building a text past either.
MAX_EXAMPLE_CHARS = 4_096
EXAMPLE_BUDGET = 65_536


def _regex_examples(regex: str) -> list[str]:
    """Texts built from the parts of ``regex`` (each alternative, the shortest repeats and one more), most of which
    it matches: values a pattern comparison tells apart. Empty when the regex cannot be read this way. The size of
    every text is known before it is built; past :data:`MAX_EXAMPLE_CHARS` for one text or :data:`EXAMPLE_BUDGET`
    for all of them, the condition is refused with :class:`CheckError`."""
    try:
        parsed = importlib.import_module("re._parser").parse(regex)
    except Exception:  # noqa: BLE001 (no examples: the domain keeps its other values)
        return []
    left = EXAMPLE_BUDGET

    def spend(size: int) -> None:
        nonlocal left
        left -= size
        if size > MAX_EXAMPLE_CHARS or left < 0:
            raise CheckError(TOO_COMPLEX)

    def joined(first: str, second: str) -> str:
        spend(len(first) + len(second))
        return first + second

    def repeated(text: str, count: int) -> str:
        spend(len(text) * count)
        return text * count

    def items(sequence: Any) -> list[str]:
        out = [""]
        for op, value in sequence:
            options = item(str(op), value)
            if options is None:
                return []
            out = list(itertools.islice((joined(a, b) for a in out for b in options), MAX_EXAMPLES))
        return out

    def item(op: str, value: Any) -> list[str] | None:
        if op == "LITERAL":
            return [chr(value)]
        if op == "NOT_LITERAL":
            return ["a" if value != ord("a") else "b"]
        if op == "ANY":
            return ["a"]
        if op == "IN":
            first, argument = value[0]
            if str(first) == "LITERAL":
                return [chr(argument)]
            if str(first) == "RANGE":
                return [chr(argument[0])]
            return ["a", "0", "-", " "]
        if op in ("AT", "ASSERT", "ASSERT_NOT"):
            return [""]
        if op == "BRANCH":
            return [text for branch in value[1] for text in items(branch)][:MAX_EXAMPLES]
        if op == "SUBPATTERN":
            return items(value[3])
        if op in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"):
            low, high, inner = value
            body = items(inner)
            counts = [n for n in (low, low + 1) if n <= high]
            return list(itertools.islice((repeated(text, n) for text in body for n in counts), MAX_EXAMPLES))
        return None

    return items(parsed)


SENTINEL = "\x01a2m-unnamed-value\x01"
# The most regex work the constant check may do: the sum over its patterns of an upper bound on the paths Python's
# backtracking matcher can try on one value (:func:`_regex_paths`), times the number of assignments it evaluates.
PATTERN_BUDGET = 20_000_000
TOO_COMPLEX = (
    "its condition has a pattern too complex for a2m to check quickly (too many wildcards or repeats, or repeats "
    "inside repeats, for the values it is compared with)"
)


# The shape every AI pattern must have before a2m builds any value from it (:func:`_check_pattern_shapes`).
MAX_PATTERN_CHARS = 256
MAX_REPEAT_COUNT = 50
MAX_WILDCARDS = 8
REPEATS = frozenset({"MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"})


def _too_complex(detail: str) -> CheckError:
    return CheckError(f"its condition has a pattern too complex for a2m to check quickly ({detail})")


def _check_pattern_shapes(node: Cond) -> None:
    """Refuse ``node`` when one of its patterns is longer than :data:`MAX_PATTERN_CHARS`, repeats a part more than
    :data:`MAX_REPEAT_COUNT` times by count, has more than :data:`MAX_WILDCARDS` open-ended repeats (``*``, ``+``,
    ``{n,}``, a glob ``*``), or a repeat inside a repeat. A cheap syntactic check on the pattern text, run before any
    value is built from it or any regex runs, so no pattern can make the later checks costly."""
    for compare in _compares(node):
        if compare.operator in PATTERNS and compare.value.kind is LiteralKind.STRING:
            _check_pattern_shape(compare)


def _check_pattern_shape(compare: Compare) -> None:
    if len(compare.value.text) > MAX_PATTERN_CHARS:
        raise _too_complex(f"longer than {MAX_PATTERN_CHARS} characters")
    try:
        parsed = importlib.import_module("re._parser").parse(_pattern(compare).pattern)
    except CheckError:
        raise
    except Exception:  # noqa: BLE001 (a regex a2m cannot read is never run)
        raise _too_complex("a form a2m cannot read") from None
    unbounded = importlib.import_module("re._constants").MAXREPEAT
    wildcards = 0

    def walk(items: Any, in_repeat: bool) -> None:
        nonlocal wildcards
        for op, value in items:
            name = str(op)
            if name in REPEATS:
                low, high, inner = value
                if in_repeat:
                    raise _too_complex("a repeat inside a repeat")
                if low > MAX_REPEAT_COUNT or (high != unbounded and high > MAX_REPEAT_COUNT):
                    raise _too_complex(f"a repeat count above {MAX_REPEAT_COUNT}")
                if high == unbounded:
                    wildcards += 1
                    if wildcards > MAX_WILDCARDS:
                        raise _too_complex(f"more than {MAX_WILDCARDS} wildcards or open-ended repeats")
                walk(inner, high > 1)
            elif name == "BRANCH":
                for branch in value[1]:
                    walk(branch, in_repeat)
            elif name == "SUBPATTERN":
                walk(value[3], in_repeat)
            elif name == "ATOMIC_GROUP":
                walk(value, in_repeat)
            elif name in ("ASSERT", "ASSERT_NOT"):
                walk(value[1], in_repeat)
            elif name == "GROUPREF_EXISTS":
                walk(value[1], in_repeat)
                if value[2] is not None:
                    walk(value[2], in_repeat)

    walk(parsed, False)


def _regex_paths(regex: str, length: int) -> int:
    """An upper bound on the paths a backtracking matcher can try when matching ``regex`` against a whole text of at
    most ``length`` characters (each repeat may stop at any count, each alternative may be tried), capped just above
    :data:`PATTERN_BUDGET`; a backreference or a form a2m does not know counts as over it."""
    cap = PATTERN_BUDGET + 1
    try:
        parsed = importlib.import_module("re._parser").parse(regex)
    except Exception:  # noqa: BLE001 (a regex a2m cannot read is never run)
        return cap
    single = {"LITERAL", "NOT_LITERAL", "ANY", "IN", "AT", "CATEGORY"}

    def sequence(items: Any) -> int:
        total = 1
        for op, value in items:
            total = min(cap, total * item(str(op), value))
        return total

    def item(op: str, value: Any) -> int:
        if op in single:
            return 1
        if op == "BRANCH":
            return min(cap, sum(sequence(branch) for branch in value[1]))
        if op == "SUBPATTERN":
            return sequence(value[3])
        if op in ("ATOMIC_GROUP", "ASSERT", "ASSERT_NOT"):
            return sequence(value if op == "ATOMIC_GROUP" else value[1])
        if op in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"):
            low, high, inner = value
            most = min(high, length)
            if most < low:
                return 1
            body = sequence(inner)
            if body > 1 and most * math.log2(body) > math.log2(cap):
                return cap
            return min(cap, (most - low + 1) * body**most)
        return cap

    return sequence(parsed)


# What the constant check uses for a value the written read can never give (:func:`_produced`).
NEVER = object()
# CP5's first-value header read (:func:`a2m.conditions.variables.first_value`) around a placeholder for the entry.
FIRST_VALUE_PARTS = tuple(first_value("\x00").split("\x00"))
VERB_READS = frozenset(accessor(VERB, side).dw for side in ("request", RESPONSE))
# An HTTP method is a token (RFC 9110 5.6.2); a header value holds visible ISO-8859-1 text, spaces and tabs.
HTTP_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
HEADER_TEXT = re.compile(r"[\t\x20-\x7e\x80-\xff]*")
# Characters any trim may remove: Java's (U+0000 to U+0020), and Python's whitespace on top in :func:`_trimmed`.
TRIMMED = "".join(chr(code) for code in range(0x21))
FRESH_TOKEN = "a2m-unnamed-value"


def _trimmed(text: str) -> str:
    while True:
        trimmed = text.strip().strip(TRIMMED)
        if trimmed == text:
            return text
        text = trimmed


def _produced(read: str, value: object) -> object:
    """``value`` as the written read ``read`` gives it, or :data:`NEVER` when that read can never give it.

    A header read (CP5's first value) gives null or the first comma-separated value, trimmed: the value becomes
    that, with every character any trim may remove taken off, and it is never a value a header cannot hold. The
    verb is always an HTTP token. Any other read gives the value as it is. The domain is therefore never wider than
    what the generated app can read, so a condition only those values would make vary is refused as constant."""
    if read in VERB_READS:
        if value == SENTINEL:
            return FRESH_TOKEN
        return value if isinstance(value, str) and HTTP_TOKEN.fullmatch(value) else NEVER
    prefix, middle, suffix = FIRST_VALUE_PARTS
    if read.startswith(prefix) and read.endswith(suffix) and middle in read:
        if value is None:
            return None
        if value == SENTINEL:
            return FRESH_TOKEN
        if not isinstance(value, str):
            return NEVER
        first = _trimmed(value.split(",", 1)[0])
        return first if HEADER_TEXT.fullmatch(first) else NEVER
    return value


Domains = tuple[list[Key], list[list[object]]]


def _check_not_constant(node: Cond, reads: dict[Key, tuple[bool, list[Compare]]], read_of: dict[int, Key]) -> Domains:
    """Refuse ``node`` when it gives the same result for every assignment of values to its reads (``read_of`` gives
    the read of each comparison, by its id: the DataWeave a2m writes for it, so two comparisons read the same value
    exactly when the written app reads the same value); the reads and the domain of values checked for each."""
    patterns = {id(c): _pattern(c) for c in _compares(node) if c.operator in PATTERNS}
    keys = list(reads)
    domains: list[list[object]] = []
    for key in keys:
        nullable, compares = reads[key]
        values: list[object] = ([None] if nullable else []) + ["", "/"]
        for compare in compares:
            values += _samples(compare)
            if compare.default is not None:
                values.append(compare.default)
        values.append(SENTINEL)
        unique: dict[tuple[str, object], object] = {}
        for value in values:
            # Only values the written read can give: a header's first value trimmed, a verb an HTTP token.
            value = _produced(key[0], value)
            if value is not NEVER:
                unique.setdefault((type(value).__name__, value), value)
        domains.append(list(unique.values()))
    assignments = math.prod(len(domain) for domain in domains)
    if assignments > MAX_ASSIGNMENTS:
        raise CheckError("its condition reads too many values for a2m to check that it is not a constant")
    longest = {
        key: max((len(value) for value in domain if isinstance(value, str)), default=0)
        for key, domain in zip(keys, domains, strict=True)
    }
    paths = sum(
        _regex_paths(patterns[id(compare)].pattern, longest[read_of[id(compare)]])
        for compare in _compares(node)
        if compare.operator in PATTERNS
    )
    if paths * assignments > PATTERN_BUDGET:
        raise CheckError(TOO_COMPLEX)

    seen: set[bool] = set()
    for combination in itertools.product(*domains):
        seen.add(_evaluate(node, dict(zip(keys, combination, strict=True)), read_of, patterns))
        if len(seen) == 2:
            return keys, domains
    shown = "true" if True in seen else "false"
    raise CheckError(f"its condition is {shown} whatever the values it reads, so {CONSTANT}")


def _check(node: Cond, side: str, changes: RequestChanges, *, body: bool, original: Cond | None = None) -> None:
    """Check ``node`` (what a2m writes): its comparisons, then that it is not a constant. With ``original`` (the tree
    as parsed, of which ``node`` differs only in defaults), ``node`` must also give ``original``'s result for every
    value of the same domain (:func:`_check_same_result`)."""
    reads: dict[Key, tuple[bool, list[Compare]]] = {}
    keys: dict[int, Key] = {}
    for compare in _compares(node):
        read = _check_compare(compare, side, changes, body=body)
        key = keys[id(compare)] = (read.dw, compare.path)
        known, compares = reads.get(key, (False, []))
        reads[key] = (known or read.nullable, [*compares, compare])
    if original is None or original == node:
        _check_not_constant(node, reads, keys)
        return
    raw: set[Key] = set()
    for before, after in zip(_compares(original), _compares(node), strict=True):
        key = keys[id(before)] = keys[id(after)]
        known, compares = reads[key]
        reads[key] = (known, [*compares, before])
        if before.raw:
            raw.add(key)
    domains = _check_not_constant(node, reads, keys)
    _check_same_result(original, node, _with_raw_header_values(domains, raw, original, keys), keys, frozenset(raw))


# Texts around a value that a whole header value may hold but Apigee's first value never shows: another value after
# or before a comma, and the spaces a first-value read trims (:func:`_with_raw_header_values`).
RAW_HEADER_SHAPES = ("{0},other", "other,{0}", "{0}, {0}", " {0}", "{0} ", ",{0}")


def _with_raw_header_values(domains: Domains, raw: set[Key], original: Cond, read_of: dict[int, Key]) -> Domains:
    """``domains`` with, for each header read in ``raw`` (one the guard writes as Mule's own entry, which gives the
    whole header value), the whole values that read can give: each checked value and each literal and default of the
    guard's raw reads, alone and in :data:`RAW_HEADER_SHAPES` (with a comma or the spaces a first-value read trims),
    and a lone comma; every value one a header can hold. The other reads keep their domain."""
    if not raw:
        return domains
    keys, values = domains
    widened: list[list[object]] = []
    for key, domain in zip(keys, values, strict=True):
        if key not in raw:
            widened.append(domain)
            continue
        texts = [value for value in domain if isinstance(value, str)]
        for compare in _compares(original):
            if compare.raw and read_of[id(compare)] == key:
                if compare.value.kind is LiteralKind.STRING:
                    texts.append(compare.value.text)
                if compare.default is not None:
                    texts.append(compare.default)
        whole: dict[object, None] = dict.fromkeys(domain)
        whole[","] = None
        for text in texts:
            for shape in ("{0}", *RAW_HEADER_SHAPES):
                value = shape.format(text)
                if HEADER_TEXT.fullmatch(value):
                    whole[value] = None
        widened.append(list(whole))
    if math.prod(len(domain) for domain in widened) > MAX_ASSIGNMENTS:
        raise CheckError("its condition reads too many values for a2m to check that it keeps its result")
    return keys, widened


def _check_same_result(
    original: Cond, node: Cond, domains: Domains, read_of: dict[int, Key], raw: frozenset[Key] = frozenset()
) -> None:
    """Refuse ``node`` (what a2m writes) when, for some assignment of the checked domain (the one
    :func:`_check_not_constant` returned, which holds every default of both trees), it gives a different result
    from ``original`` (the expression as parsed, each read with the default it gives, or none). For a header read in
    ``raw`` the domain holds whole header values (:func:`_with_raw_header_values`): the guard's raw read of it gives
    the value as it is, every other read of it (a2m's, always) Apigee's first value of it, trimmed."""
    keys, values = domains
    patterns = {id(c): _pattern(c) for tree in (original, node) for c in _compares(tree) if c.operator in PATTERNS}
    for combination in itertools.product(*values):
        assignment = dict(zip(keys, combination, strict=True))
        before = _evaluate(original, assignment, read_of, patterns, raw)
        if before != _evaluate(node, assignment, read_of, patterns, raw):
            names = {read_of[id(c)]: c.variable for c in _compares(node)}
            shown = " and ".join(
                f"{names[key]} is {'missing' if value is None else repr(value)}" for key, value in assignment.items()
            )
            read = ""
            if raw:
                headers = ", ".join(sorted({names[key] for key in raw}))
                read = (
                    f" (the guard reads the whole value of {headers} as Mule gives it, commas and spaces included, "
                    "while a2m would read the header as Apigee's first value: the text before the first comma, "
                    "trimmed)"
                )
            raise CheckError(
                f"a2m cannot write it with exactly its result: written as a2m writes it, it gives a different "
                f"result when {shown}{read}"
            )


def _evaluate(
    item: Cond,
    assignment: dict[Key, object],
    read_of: dict[int, Key],
    patterns: dict[int, re.Pattern[str]],
    raw: frozenset[Key] = frozenset(),
) -> bool:
    """``item``'s result for ``assignment`` (a value for each read), as the generated DataWeave reads it. A read in
    ``raw`` is assigned a whole header value: a comparison written as Mule's raw entry reads it as it is, any other
    one Apigee's first value of it (:func:`_produced`)."""
    if isinstance(item, Compare):
        key = read_of[id(item)]
        value = assignment[key]
        if key in raw and not item.raw:
            value = _produced(key[0], value)
        return _holds(item, value, patterns)
    if isinstance(item, Negation):
        return not _evaluate(item.operand, assignment, read_of, patterns, raw)
    results = (_evaluate(part, assignment, read_of, patterns, raw) for part in item.parts)
    return all(results) if item.connective is Connective.AND else any(results)


def _render(node: Cond, side: str, changes: RequestChanges) -> str:
    if isinstance(node, Junction):
        out = _render(node.parts[0], side, changes)
        for part in node.parts[1:]:
            out = f"({out} {node.connective.value} {_render(part, side, changes)})"
        return out
    if isinstance(node, Negation):
        return f"(not {_render(node.operand, side, changes)})"
    if node.empty:
        return f"isEmpty({_resolve(node.variable, side, changes, body=False).dw})"
    if _is_body(node):
        return _render_body(node, side, changes)
    operator, literal = node.operator, node.value
    if operator in CP5_OPERATORS:
        try:
            written = emit(Comparison(node.variable, operator, operator.value, literal), side, changes)
        except ConditionError as exc:
            raise CheckError(f"a2m cannot write its condition faithfully: {exc}") from None
        if node.default == _missing_reads_as(operator):
            return written
        return _render_defaulted(node, side, changes)
    if node.variable.strip() == PATH_SUFFIX and _holds(node, "", {}) != _holds(node, "/", {}):
        raise CheckError(f"a2m cannot write its condition faithfully: {PATH_SUFFIX_ROOT}")
    if operator is not Operator.STARTS_WITH:
        raise CheckError(
            f"a2m writes no {operator.value} comparison: Apigee and DataWeave compare text and numbers by different "
            "rules, so it could not give Apigee's result"
        )
    lhs = _resolve(node.variable, side, changes, body=False).dw
    if node.default is not None:
        lhs = f"({lhs} default {dw_string(node.default)})"
    return f"({lhs} startsWith {dw_string(literal.text)})"


def _render_defaulted(node: Compare, side: str, changes: RequestChanges) -> str:
    """An equals or not-equals comparison whose read has a default CP5 does not write (a callout guard keeps the
    default its expression gives, :func:`guard_condition`), written with that default, after CP5's own checks of the
    same comparison have passed; :class:`CheckError` for any other comparison, which a2m cannot write exactly."""
    operator, literal, default = node.operator, node.value, node.default
    symbol = {Operator.EQUALS: "==", Operator.NOT_EQUALS: "!="}.get(operator)
    if symbol is None or default is None:
        raise CheckError(
            f"a2m cannot write its {operator.value} comparison of {node.variable} with the default {default!r} it "
            "gives, so it could not keep its result"
        )
    lhs = _resolve(node.variable, side, changes, body=False).dw
    rhs = "null" if literal.kind is LiteralKind.NULL else dw_string(literal.text)
    return f"(({lhs} default {dw_string(default)}) {symbol} {rhs})"


def structured_condition(value: Any, side: str, changes: RequestChanges = NO_CHANGES) -> str:
    """The DataWeave a2m writes for the AI's structured condition ``value``, read on ``side`` after ``changes``;
    :class:`CheckError` when it cannot be used (see the module docstring)."""
    return _written(condition_from_json(value), side, changes, body=False)


def dataweave_condition(text: str, side: str, changes: RequestChanges = NO_CHANGES) -> str:
    """The DataWeave a2m writes for ``text`` (an AI condition in the earlier DataWeave form, without ``#[ ]``): it
    must parse into a condition tree, which is checked and written by a2m exactly as the structured form is (a read
    of an Apigee value with a2m's own accessor, so a header is its first value); :class:`CheckError` otherwise. A
    ``default`` the AI gave an Apigee value is dropped for the one a2m writes (an Apigee condition has no default:
    Apigee's own reading of a missing value decides), so what is checked is what is written; a read of the body,
    which only this form allows, keeps its fields and default, in a2m's own spelling."""
    return _written(condition_from_dataweave(text, side), side, changes, body=True)


def guard_condition(text: str, side: str, changes: RequestChanges = NO_CHANGES) -> str:
    """The DataWeave a2m writes for ``text``, a choice guard (or another expression that decides control flow) in a
    callout's Mule code, without ``#[ ]``: the same pipeline as :func:`dataweave_condition` (the same subset, parsed,
    made canonical, its reads checked, refused when constant, and written by a2m), with one more form,
    ``isEmpty(READ)`` of a header or query parameter; :class:`CheckError` otherwise.

    A guard is the callout's own Mule code, not a translation of an Apigee condition, so a ``default`` it gives any
    read is its meaning and is kept: checked (the constant check reads a missing value as that default) and written.
    What a2m writes must give the guard's own result for every value of the domain the constant check uses
    (:func:`_check_same_result`); a guard a2m cannot write that way is refused."""
    return _written(condition_from_dataweave(text, side, guard=True), side, changes, body=True, faithful=True)


def _written(parsed: Cond, side: str, changes: RequestChanges, *, body: bool, faithful: bool = False) -> str:
    """The one pipeline both condition forms go through: the parsed tree is bounded (its size, and the shape of
    every pattern, before anything is built from it), made canonical (every default
    a2m writes is set in the tree), checked, and that same tree is written; :func:`_render` adds nothing, so what is
    checked is exactly what is written. ``faithful`` (a callout guard): every default the expression gives is kept,
    and the written tree must give the parsed tree's result for every value of the checked domain."""
    _bounded(parsed)
    _check_pattern_shapes(parsed)
    node = _canonical(parsed, keep_defaults=faithful)
    _check(node, side, changes, body=body, original=parsed if faithful else None)
    return _render(node, side, changes)


def _bounded(node: Cond) -> None:
    """Refuse a tree nested deeper than :data:`MAX_DEPTH` or with more than :data:`MAX_COMPARISONS` comparisons."""
    count = 0
    stack: list[tuple[Cond, int]] = [(node, 0)]
    while stack:
        item, depth = stack.pop()
        if depth > MAX_DEPTH:
            raise CheckError("its condition is nested too deeply")
        if isinstance(item, Compare):
            count += 1
            if count > MAX_COMPARISONS:
                raise CheckError(f"its condition has more than {MAX_COMPARISONS} comparisons")
        elif isinstance(item, Negation):
            stack.append((item.operand, depth + 1))
        else:
            stack.extend((part, depth + 1) for part in item.parts)


def _is_body(compare: Compare) -> bool:
    return fold(compare.variable.strip()) in (REQUEST_CONTENT, RESPONSE_CONTENT)


def _canonical(node: Cond, *, keep_defaults: bool = False) -> Cond:
    """``node`` with every default a2m writes set in the tree, so :func:`_render` writes it as it is: StartsWith and
    the patterns read a missing value as ``""``, other reads of an Apigee value have none (the AI's is dropped), and
    a read of the body keeps the default the AI gave. ``keep_defaults`` (a callout guard): every read keeps the
    default the expression gives; a read without one gets a2m's."""
    if isinstance(node, Junction):
        return Junction(node.connective, tuple(_canonical(part, keep_defaults=keep_defaults) for part in node.parts))
    if isinstance(node, Negation):
        return Negation(_canonical(node.operand, keep_defaults=keep_defaults))
    if _is_body(node):
        if node.operator is Operator.STARTS_WITH and node.default is None:
            return replace(node, default="")
        return node
    if keep_defaults and node.default is not None:
        return replace(node, raw=False)
    return replace(node, default=_missing_reads_as(node.operator), raw=False)


def _render_body(node: Compare, side: str, changes: RequestChanges) -> str:
    """The DataWeave a2m writes for a comparison of the body (or a field of it), read only in the DataWeave form."""
    lhs = _resolve(node.variable, side, changes, body=True).dw + "".join(f"[{dw_string(name)}]" for name in node.path)
    if node.default is not None:
        lhs = f"({lhs} default {dw_string(node.default)})"
    literal = node.value
    if node.operator is Operator.STARTS_WITH:
        return f"({lhs} startsWith {dw_string(literal.text)})"
    symbol = {Operator.EQUALS: "==", Operator.NOT_EQUALS: "!=", **ORDERING}.get(node.operator)
    if symbol is None:
        raise CheckError(f"a2m writes no {node.operator.value} comparison of the body")
    rhs = {LiteralKind.NULL: "null", LiteralKind.NUMBER: literal.text}.get(literal.kind, dw_string(literal.text))
    return f"({lhs} {symbol} {rhs})"


# ---------------------------------------------------------------- the DataWeave subset

STR, NUM, IDENT, OP = "str", "num", "ident", "op"
# ASCII only: any other character outside a string (a non-ASCII letter or digit) is refused by :func:`tokenize`.
NUM_TOKEN = re.compile(r"[0-9]+(?:\.[0-9]+)?")
IDENT_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
TWO_CHAR = ("==", "!=", "<=", ">=", "++", "--")
ONE_CHAR = "()[]{},.:+-*/<>!?"
COMPARISONS = {
    "==": Operator.EQUALS,
    "!=": Operator.NOT_EQUALS,
    "<": Operator.LESS,
    "<=": Operator.LESS_OR_EQUAL,
    ">": Operator.GREATER,
    ">=": Operator.GREATER_OR_EQUAL,
}
SUBSET = (
    "its DataWeave is not in the small form a2m can check (one read of an Apigee value compared with a literal, "
    "startsWith, and/or and not (...)); a structured condition is expected"
)


@dataclass(frozen=True, slots=True)
class _Token:
    kind: str
    text: str


def _string(text: str, start: int) -> tuple[str, int]:
    """The decoded string literal starting at ``start`` and the index after it."""
    quote = text[start]
    out: list[str] = []
    index = start + 1
    escapes = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "'": "'", "$": "$", "/": "/"}
    while index < len(text):
        char = text[index]
        if char == "\\":
            code = text[index + 1 : index + 2]
            if code == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", text[index + 2 : index + 6]):
                out.append(chr(int(text[index + 2 : index + 6], 16)))
                index += 6
                continue
            if code not in escapes:
                raise CheckError("its DataWeave has a string escape a2m does not know")
            out.append(escapes[code])
            index += 2
            continue
        if char == "$":
            raise CheckError("its DataWeave has a string with $ interpolation, which can read anything")
        if char == quote:
            return "".join(out), index + 1
        out.append(char)
        index += 1
    raise CheckError("its DataWeave has an unclosed string")


def tokenize(text: str) -> list[_Token]:
    """The DataWeave tokens of ``text``; :class:`CheckError` for anything a2m does not recognise."""
    tokens: list[_Token] = []
    index = 0
    while index < len(text):
        char = text[index]
        if not char.isascii():
            raise CheckError(
                f"its DataWeave uses {char!r} outside a string; a2m reads only ASCII names and numbers in a condition"
            )
        if char.isspace():
            index += 1
        elif char in "\"'":
            value, index = _string(text, index)
            tokens.append(_Token(STR, value))
        elif (match := NUM_TOKEN.match(text, index)) is not None:
            tokens.append(_Token(NUM, match.group()))
            index = match.end()
        elif (match := IDENT_TOKEN.match(text, index)) is not None:
            tokens.append(_Token(IDENT, match.group()))
            index = match.end()
        elif text[index : index + 2] in TWO_CHAR:
            tokens.append(_Token(OP, text[index : index + 2]))
            index += 2
        elif char in ONE_CHAR:
            tokens.append(_Token(OP, char))
            index += 1
        else:
            raise CheckError(f"its DataWeave uses {char!r}, which a2m does not recognise in a condition")
    return tokens


EXACT_PATH_TOKENS = (_Token(OP, "("), *tokenize(EXACT_PATH_SUFFIX_DW), _Token(OP, ")"))
RESPONSE_MAP_TOKENS = tuple(tokenize(RESPONSE_HEADERS_BASE))
# CP5's first-value header read around a placeholder for the header entry (which it holds twice).
FIRST_VALUE_TOKENS = tuple(tuple(tokenize(part)) for part in first_value("\x00").split("\x00"))


# The reads ``isEmpty`` may test in a guard: always text or null in the generated app.
EMPTY_READS = (HEADER_PREFIX, QUERY_PREFIX, RESPONSE_HEADER_PREFIX)


class _NoMatch(Exception):
    """The tokens at this point are not the form being tried."""


@dataclass(frozen=True, slots=True)
class _Read:
    variable: str
    path: tuple[str, ...] = ()
    default: str | None = None
    raw: bool = False


class _Subset:
    """A recursive-descent parser of the DataWeave subset (see the module docstring) into a condition tree.

    and/or: one kind per bracket level (DataWeave's precedence between them is
    not relied on); comparisons bind tighter than and/or. ``not`` and an infix
    ``startsWith`` must be bracketed when joined with and/or, so no precedence
    a2m is unsure of decides the meaning.
    """

    def __init__(self, tokens: list[_Token], side: str, *, guard: bool = False) -> None:
        self.tokens = tokens
        self.side = side
        self.guard = guard
        self.pos = 0
        self.hint: str | None = None
        self.depth = 0

    def peek(self, offset: int = 0) -> _Token | None:
        index = self.pos + offset
        return self.tokens[index] if index < len(self.tokens) else None

    def take(self) -> _Token:
        token = self.peek()
        if token is None:
            raise _NoMatch
        self.pos += 1
        return token

    def expect(self, text: str, kind: str = OP) -> None:
        token = self.take()
        if token.kind != kind or token.text != text:
            raise _NoMatch

    def at(self, text: str, kind: str = OP, offset: int = 0) -> bool:
        token = self.peek(offset)
        return token is not None and token.kind == kind and token.text == text

    def refuse(self, hint: str) -> _NoMatch:
        self.hint = hint
        return _NoMatch()

    def chain(self) -> tuple[Cond, bool]:
        """A condition: terms joined by one connective; the flag is True for a term that must be bracketed."""
        if self.depth >= MAX_DEPTH:
            raise CheckError("its condition is nested too deeply")
        self.depth += 1
        try:
            return self.terms()
        finally:
            self.depth -= 1

    def terms(self) -> tuple[Cond, bool]:
        terms = [self.term()]
        word: str | None = None
        while (token := self.peek()) is not None and token.kind == IDENT and token.text in ("and", "or"):
            if word is not None and token.text != word:
                raise CheckError("its DataWeave mixes and and or without brackets")
            word = token.text
            self.pos += 1
            terms.append(self.term())
        if word is None:
            return terms[0]
        if any(loose for _, loose in terms):
            raise self.refuse("a not or an infix startsWith joined with and/or must be in its own brackets")
        return Junction(Connective(word), tuple(node for node, _ in terms)), False

    def term(self) -> tuple[Cond, bool]:
        if self.at("not", IDENT):
            self.pos += 1
            self.expect("(")
            inner, _ = self.chain()
            self.expect(")")
            return Negation(inner), True
        start = self.pos
        try:
            return self.comparison()
        except _NoMatch:
            self.pos = start
        self.expect("(")
        inner, _ = self.chain()
        self.expect(")")
        return inner, False

    def comparison(self) -> tuple[Cond, bool]:
        if self.guard and self.at("isEmpty", IDENT) and self.at("(", OP, 1):
            self.pos += 2
            read = self.operand()
            self.expect(")")
            if read.default is not None or read.path or not fold(read.variable).startswith(EMPTY_READS):
                raise self.refuse("isEmpty may test only a header or a query parameter, without a default")
            null = Literal(LiteralKind.NULL, "null")
            return Compare(read.variable, Operator.EQUALS, null, empty=True, raw=read.raw), False
        if self.at("startsWith", IDENT) and self.at("(", OP, 1):
            self.pos += 2
            read = self.operand()
            self.expect(",")
            prefix = self.string()
            self.expect(")")
            return Compare(read.variable, Operator.STARTS_WITH, prefix, read.path, read.default, raw=read.raw), False
        read = self.operand()
        token = self.take()
        if token.kind == OP and token.text in COMPARISONS:
            operator = COMPARISONS[token.text]
            return Compare(read.variable, operator, self.literal(), read.path, read.default, raw=read.raw), False
        if token.kind == IDENT and token.text == "startsWith":
            prefix = self.string()
            return Compare(read.variable, Operator.STARTS_WITH, prefix, read.path, read.default, raw=read.raw), True
        raise _NoMatch

    def string(self) -> Literal:
        token = self.take()
        if token.kind != STR:
            raise _NoMatch
        return Literal(LiteralKind.STRING, token.text)

    def literal(self) -> Literal:
        token = self.take()
        if token.kind == STR:
            return Literal(LiteralKind.STRING, token.text)
        if token.kind == NUM:
            return Literal(LiteralKind.NUMBER, token.text)
        following = self.peek()
        if token.kind == OP and token.text == "-" and following is not None and following.kind == NUM:
            return Literal(LiteralKind.NUMBER, "-" + self.take().text)
        if token.kind == IDENT and token.text == "null":
            return Literal(LiteralKind.NULL, "null")
        if token.kind == IDENT and token.text in ("true", "false"):
            raise CheckError(f"its DataWeave uses the literal {token.text}, which can make a condition constant")
        raise _NoMatch

    def operand(self) -> _Read:
        start = self.pos
        for form in (self.base, self.defaulted, self.first_value, self.path_suffix):
            self.pos = start
            try:
                return form()
            except _NoMatch:
                continue
        self.pos = start
        raise _NoMatch

    def defaulted(self) -> _Read:
        """``(READ default "text")``."""
        self.expect("(")
        start = self.pos
        try:
            read = self.base()
        except _NoMatch:
            self.pos = start
            read = self.first_value()
        self.expect("default", IDENT)
        default = self.string().text
        self.expect(")")
        return replace(read, default=default)

    def first_value(self) -> _Read:
        """CP5's first-value header read (:func:`a2m.conditions.variables.first_value`) of one header entry."""
        before, middle, after = FIRST_VALUE_TOKENS
        self.tokens_at(before)
        first = self.base()
        self.tokens_at(middle)
        second = self.base()
        self.tokens_at(after)
        if first != second or not fold(first.variable).startswith((HEADER_PREFIX, RESPONSE_HEADER_PREFIX)):
            raise _NoMatch
        return replace(first, raw=False)

    def tokens_at(self, expected: tuple[_Token, ...]) -> None:
        if tuple(self.tokens[self.pos : self.pos + len(expected)]) != expected:
            raise _NoMatch
        self.pos += len(expected)

    def path_suffix(self) -> _Read:
        self.tokens_at(EXACT_PATH_TOKENS)
        if self.side == RESPONSE:
            raise self.refuse("it reads the path suffix on the response side, where a2m cannot read it")
        return _Read(PATH_SUFFIX)

    def selector(self) -> str:
        """The name a selector picks: ``.name``, ``.'name'`` or ``['name']``."""
        name = self.peek(1)
        if name is not None and self.at(".") and name.kind in (IDENT, STR):
            self.pos += 2
            return name.text
        if name is not None and self.at("[") and name.kind == STR and self.at("]", OP, 2):
            self.pos += 3
            return name.text
        raise _NoMatch

    def base(self) -> _Read:
        start = self.pos
        try:
            self.tokens_at(RESPONSE_MAP_TOKENS)
            return self.response_header()
        except _NoMatch:
            self.pos = start
        token = self.take()
        if token.kind != IDENT:
            raise _NoMatch
        if token.text == "attributes":
            if self.side == RESPONSE:
                raise self.refuse(
                    "it reads attributes on the response side, where Mule's attributes hold the target's response"
                )
            return self.request_part()
        if token.text == "payload":
            path: list[str] = []
            while True:
                try:
                    path.append(self.selector())
                except _NoMatch:
                    break
            return _Read(RESPONSE_CONTENT if self.side == RESPONSE else REQUEST_CONTENT, tuple(path))
        if token.text != "vars":
            raise _NoMatch
        name = self.selector()
        if name == SNAPSHOT_VAR:
            if self.side != RESPONSE:
                raise self.refuse("it reads the sent-request snapshot on the request side, where it does not exist")
            return self.request_part()
        if name == RESPONSE_HEADERS_VAR:
            return self.response_header()
        if not is_custom_variable(name):
            raise self.refuse(
                f"it reads the Apigee built-in variable {name} as a flow variable; the generated app never sets it"
            )
        return _Read(name)

    def request_part(self) -> _Read:
        part = self.selector()
        if part == "method":
            return _Read(VERB)
        if part == "headers":
            # In a guard, Mule's own entry: the whole header value (:attr:`Compare.raw`).
            return _Read(HEADER_PREFIX + self.selector(), raw=self.guard)
        if part == "queryParams":
            return _Read(QUERY_PREFIX + self.selector())
        raise self.refuse(f"it reads {part}, which a2m does not map from an Apigee variable in this form")

    def response_header(self) -> _Read:
        if self.side != RESPONSE:
            raise self.refuse("it reads the response headers on the request side, before there is a response")
        return _Read(RESPONSE_HEADER_PREFIX + self.selector(), raw=self.guard)


def condition_from_dataweave(text: str, side: str, *, guard: bool = False) -> Cond:
    """The condition tree of ``text`` in the DataWeave subset (see the module docstring), with ``isEmpty(READ)``
    too when ``guard``; :class:`CheckError` otherwise."""
    parser = _Subset(tokenize(text), side, guard=guard)
    try:
        node, _ = parser.chain()
        if parser.pos != len(parser.tokens):
            raise _NoMatch
    except _NoMatch:
        raise CheckError(SUBSET + (f" ({parser.hint})" if parser.hint else "")) from None
    return node


# ---------------------------------------------------------------- Mule fragments: reads and writes

# A flow variable named in DataWeave or an expression: vars.name, vars.'name', vars."name", vars['name'].
VARS_SELECTOR = re.compile(
    r"""\bvars\s*(?:\.\s*(?:'([^']*)'|"([^"]*)"|([A-Za-z_][A-Za-z0-9_]*))|\[\s*(?:'([^']*)'|"([^"]*)")\s*\])"""
)


def _texts(processors: Sequence[ET.Element]) -> list[str]:
    return [
        part
        for processor in processors
        for element in processor.iter()
        if isinstance(element.tag, str)
        for part in (element.text or "", *element.attrib.values())
    ]


def builtin_reads(processors: Sequence[ET.Element]) -> list[str]:
    """The Apigee built-in variables the fragment reads as flow variables (always null in the generated app)."""
    found: list[str] = []
    for text in _texts(processors):
        for match in VARS_SELECTOR.finditer(text):
            name = next(group for group in match.groups() if group is not None)
            if not is_custom_variable(name) and name not in found:
                found.append(name)
    return found


# The processors whose writes a2m can list exactly; any other element may write something a2m cannot see.
SAFE_ELEMENTS = frozenset(
    {
        f"{{{CORE}}}{name}"
        for name in (
            "set-variable",
            "remove-variable",
            "set-payload",
            "logger",
            "choice",
            "when",
            "otherwise",
            "raise-error",
            "try",
            "error-handler",
            "on-error-continue",
            "on-error-propagate",
        )
    }
    | {f"{{{EE}}}{name}" for name in ("transform", "message", "variables", "set-payload", "set-variable")}
)
VARIABLE_WRITERS = frozenset({f"{{{CORE}}}set-variable", f"{{{CORE}}}remove-variable", f"{{{EE}}}set-variable"})
PAYLOAD_WRITERS = frozenset({f"{{{CORE}}}set-payload", f"{{{EE}}}set-payload"})
REMOVE_VARIABLE = f"{{{CORE}}}remove-variable"
# a2m's variables an AI step may write without a declaration: the status and reason phrase are not tracked.
UNTRACKED_WRITES = frozenset({fold(STATUS_VAR), fold(REASON_PHRASE_VAR)})
# a2m's map variables an AI step may change key by key: (what, the map's base before any change, header names).
MAP_VARIABLES = {
    fold(REQUEST_HEADERS_VAR): ("request headers", r"attributes\s*\.\s*headers", True),
    fold(REQUEST_QUERY_VAR): ("query parameters", r"attributes\s*\.\s*queryParams", False),
    fold(RESPONSE_HEADERS_VAR): ("response headers", r"\{\s*\}", True),
}
DW_HEADER = re.compile(r"\s*(?:%dw\s+2\.0\s+)?(?:output\s+[\w./+-]+\s+)?---")


@dataclass(frozen=True, slots=True)
class FragmentWrites:
    """What a fragment writes: flow variables (exact names, a2m's own excluded), whether it sets the payload, and
    the request headers (lower case), query parameters (exact) and response headers (lower case) it changes."""

    variables: frozenset[str]
    payload: bool
    request_headers: frozenset[str]
    query_params: frozenset[str]
    response_headers: frozenset[str]


def _closing(text: str, start: int) -> int:
    """The index of the bracket closing the one at ``start``, skipping string literals; -1 when there is none or
    the text holds something a2m does not scan (a backtick, ``$(`` interpolation, a comment or a regex)."""
    stack: list[str] = []
    pairs = {"(": ")", "[": "]", "{": "}"}
    index = start
    while index < len(text):
        char = text[index]
        if char in "\"'":
            end = index + 1
            while end < len(text) and text[end] != char:
                if text[end] == "\\":
                    end += 1
                elif text[end] == "$":
                    return -1
                end += 1
            if end >= len(text):
                return -1
            index = end + 1
            continue
        if char in "`/#$":
            return -1
        if char in pairs:
            stack.append(pairs[char])
        elif char in pairs.values():
            if not stack or stack.pop() != char:
                return -1
            if not stack:
                return index
        index += 1
    return -1


def _top_level_commas(text: str) -> bool:
    """True when ``text`` (the inside of an object) has a comma outside every bracket and string."""
    depth = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char in "\"'":
            end = index + 1
            while end < len(text) and text[end] != char:
                end += 2 if text[end] == "\\" else 1
            index = end + 1
            continue
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            return True
        index += 1
    return False


def _single_key(script: str, variable: str, base: str) -> str | None:
    """The one literal key the map write ``script`` adds or removes, in a2m's form ``(vars.V default BASE) ++
    {'key': value}`` or ``(vars.V default BASE) - 'key'``; None for any other form (it may change every key)."""
    body = script.strip()
    if body.startswith("#[") and body.endswith("]"):
        body = body[2:-1]
    header = DW_HEADER.match(body)
    if header is not None:
        body = body[header.end() :]
    body = body.strip()
    while body.startswith("(") and _closing(body, 0) == len(body) - 1:
        body = body[1:-1].strip()
    prefix = re.match(rf"\(\s*vars\s*\.\s*{re.escape(variable)}\s+default\s+{base}\s*\)\s*", body)
    if prefix is None:
        return None
    rest = body[prefix.end() :]
    try:
        if rest.startswith("++"):
            rest = rest[2:].lstrip()
            if not rest.startswith("{") or _closing(rest, 0) != len(rest) - 1:
                return None
            inner = rest[1:-1].strip()
            if not inner or inner[0] not in "\"'":
                return None
            key, after = _string(inner, 0)
            value = inner[after:].lstrip()
            if not value.startswith(":") or not value[1:].strip() or _top_level_commas(value[1:]):
                return None
            return key
        if rest.startswith("-") and not rest.startswith("--"):
            rest = rest[1:].lstrip()
            if not rest or rest[0] not in "\"'":
                return None
            key, after = _string(rest, 0)
            return key if not rest[after:].strip() else None
    except CheckError:
        return None
    return None


def fragment_writes(processors: Sequence[ET.Element]) -> FragmentWrites | str:
    """What ``processors`` write, by exact key, or why a2m cannot list it."""
    variables: set[str] = set()
    payload = False
    keys: dict[str, set[str]] = {name: set() for name in MAP_VARIABLES}
    for processor in processors:
        for element in processor.iter():
            if not isinstance(element.tag, str):
                continue
            if element.tag not in SAFE_ELEMENTS:
                return f"it uses <{element.tag.rpartition('}')[2]}>, whose writes a2m cannot list"
            if element.tag in PAYLOAD_WRITERS:
                payload = True
            if element.tag not in VARIABLE_WRITERS:
                continue
            name = element.get("variableName", "").strip()
            folded = fold(name)
            if folded in MAP_VARIABLES:
                what, base, header_names = MAP_VARIABLES[folded]
                script = element.text if element.tag == f"{{{EE}}}set-variable" else element.get("value")
                key = None
                if element.tag != REMOVE_VARIABLE and script is not None:
                    key = _single_key(script, name, base)
                if key is None:
                    return f"it writes the {what} in a form a2m cannot list key by key, so it may change all of them"
                keys[folded].add(fold(key) if header_names else key)
            elif folded in UNTRACKED_WRITES:
                continue
            elif folded in A2M_VARIABLES or not name:
                return f"it writes a2m's own variable {name or '(no name)'}"
            else:
                variables.add(name)
    return FragmentWrites(
        frozenset(variables),
        payload,
        frozenset(keys[fold(REQUEST_HEADERS_VAR)]),
        frozenset(keys[fold(REQUEST_QUERY_VAR)]),
        frozenset(keys[fold(RESPONSE_HEADERS_VAR)]),
    )


@dataclass(frozen=True, slots=True)
class DeclaredWrites:
    """What the original code writes, as the AI declared it and a2m checked against the fragment: request headers
    (lower case) and query parameters (exact) changed, flow variables (exact), the body, response headers (lower
    case)."""

    request_headers: frozenset[str]
    query_params: frozenset[str]
    variables: frozenset[str]
    payload: bool
    response_headers: frozenset[str]


WRITE_FIELDS = ("request_headers", "query_params", "verb", "payload", "response_headers", "variables")


def _names(value: Any, *, header_names: bool) -> frozenset[str] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        return None
    return frozenset(fold(item.strip()) if header_names else item.strip() for item in value)


def declared_writes(declaration: Any, processors: Sequence[ET.Element]) -> DeclaredWrites | str:
    """The write model of an AI-translated step: the AI's ``writes`` declaration when the fragment writes exactly
    those keys, or why not (the step then may change anything)."""
    if declaration is None:
        return "the AI did not declare what the original code writes"
    if not isinstance(declaration, dict) or set(declaration) - set(WRITE_FIELDS):
        return "the AI's declaration of what the original code writes has a form a2m does not know"
    request_headers = _names(declaration.get("request_headers", []), header_names=True)
    query_params = _names(declaration.get("query_params", []), header_names=False)
    response_headers = _names(declaration.get("response_headers", []), header_names=True)
    variables = _names(declaration.get("variables", []), header_names=False)
    verb, payload = declaration.get("verb", False), declaration.get("payload", False)
    if (
        request_headers is None
        or query_params is None
        or response_headers is None
        or variables is None
        or not isinstance(verb, bool)
        or not isinstance(payload, bool)
    ):
        return "the AI's declaration of what the original code writes has a form a2m does not know"
    if verb:
        return "the original code changes the request verb, which the generated step cannot do"
    if any(not is_custom_variable(name) for name in variables):
        return "the AI declared an Apigee built-in variable as a flow variable the code writes"
    found = fragment_writes(processors)
    if isinstance(found, str):
        return found
    if found.variables != variables:
        return "the flow variables the generated step writes differ from the ones the AI declared"
    if found.payload != payload:
        return "whether the generated step sets the body differs from the AI's declaration"
    for written, names, what in (
        (found.request_headers, request_headers, "request headers"),
        (found.query_params, query_params, "query parameters"),
        (found.response_headers, response_headers, "response headers"),
    ):
        if written != names:
            return f"the {what} the generated step changes differ from the ones the AI declared"
    return DeclaredWrites(request_headers, query_params, variables, payload, response_headers)


# ---------------------------------------------------------------- what a callout's code reads

# a2m vouches for a script's read set only when every mention of the Apigee message API is one of these calls, on
# the API object, with one plain name in quotes (a getVariable call is a read; setVariable and removeVariable are
# writes, and the value written is checked as the rest of the code). The source is matched as text, comments and
# strings included: a call written in a comment or a string only adds a read, never hides one.
_RECEIVER = r"\b(?:context|flow)\s*\.\s*"
_NAME = r"\(\s*(['\"])([A-Za-z0-9_.\-]+)\1\s*"
SAFE_READ = re.compile(_RECEIVER + r"getVariable\s*" + _NAME + r"\)", re.ASCII)
SAFE_WRITE = re.compile(_RECEIVER + r"(?:setVariable\s*" + _NAME + r",|removeVariable\s*" + _NAME + r"\))", re.ASCII)
# Left after those calls are taken out, any of these words means the code may read anything: the message API used
# another way (an alias, a method reference, bracket access, another object), or a way to reach it without naming it
# (the global object, eval, Function, constructors, Java, Python's introspection).
UNVOUCHED = re.compile(
    r"\b(?:context|flow|request|response|message|messageContext|proxyRequest|targetRequest|proxyResponse"
    r"|targetResponse|session|getVariable|setVariable|removeVariable|getVariables|getFlowVariables|getMessage"
    r"|getRequestMessage|getResponseMessage|getErrorMessage"
    r"|this|globalThis|global|self|window|eval|Function|constructor|Packages|java|javax|importPackage|importClass"
    r"|JavaImporter|with|exec|execfile|compile|globals|locals|vars|getattr|setattr|delattr|sys|inspect|importlib"
    r"|builtins|__builtin__|func_globals|func_code|f_globals|f_locals|f_back|gi_frame|tb_frame)\b"
    r"|__\w+__"
)
# Code a2m would not see as written: inside JavaScript template literals and Python f-strings, and names spelled
# with escapes (JavaScript) or non-ASCII letters (Python 3 folds them to ASCII ones).
JS_HIDDEN = re.compile(r"`|\\u")
PY_HIDDEN = re.compile(r"(?<!\w)(?:[fF][rRbB]?|[rRbB][fF])['\"]|[^\x00-\x7f]")


def script_reads(original: str, kind: ItemKind) -> frozenset[str] | None:
    """The Apigee variables the callout's code reads, or None when it may read anything.

    The set is given only when a2m can vouch that it is complete: every mention of the message API is a direct
    ``getVariable``, ``setVariable`` or ``removeVariable`` call on ``context`` (or Python's ``flow``) with one plain
    name in quotes, and nothing else could reach that API (see :data:`UNVOUCHED`), and no template literal or
    f-string could hide a call. Java callouts may read anything."""
    if kind not in (ItemKind.JAVASCRIPT, ItemKind.PYTHON):
        return None
    if (PY_HIDDEN if kind is ItemKind.PYTHON else JS_HIDDEN).search(original):
        return None
    names = {match.group(2) for match in SAFE_READ.finditer(original)}
    rest = SAFE_WRITE.sub(" ", SAFE_READ.sub(" ", original))
    if UNVOUCHED.search(rest) or not all(_vouched_name(name) for name in names):
        return None
    return frozenset(names)


def _vouched_name(name: str) -> bool:
    """False for a name that reads the message in a way :func:`changed_steps` does not track (the message objects
    themselves, ``message.*``, a header's values, the query string, the URL, ...)."""
    folded = fold(name)
    if folded in (VERB, REQUEST_CONTENT, RESPONSE_CONTENT):
        return True
    for prefix in (HEADER_PREFIX, QUERY_PREFIX, RESPONSE_HEADER_PREFIX):
        if folded.startswith(prefix):
            return bool(folded[len(prefix) :]) and "." not in folded[len(prefix) :]
    return folded.split(".", 1)[0] not in ("request", "response", "message")


def callout_reads(sources: Sequence[str], kind: ItemKind) -> frozenset[str] | None:
    """What a callout reads across all its scripts (the main one and every included one): the union of their
    :func:`script_reads`, or None when any of them may read anything."""
    reads: set[str] = set()
    for text in sources:
        found = script_reads(text, kind)
        if found is None:
            return None
        reads |= found
    return frozenset(reads)


def changed_steps(name: str, changes: RequestChanges) -> list[str]:
    """The earlier steps whose change of Apigee variable ``name`` the generated app may not carry over."""
    lowered = fold(name.strip())
    if lowered == "request.verb":
        return changes.verb_steps()
    if lowered.startswith("request.header."):
        return changes.header_steps(lowered[len("request.header.") :])
    if lowered.startswith("request.queryparam."):
        return changes.query_steps(name.strip()[len("request.queryparam.") :])
    if lowered.startswith(RESPONSE_HEADER_PREFIX) or lowered in (REQUEST_CONTENT, RESPONSE_CONTENT):
        return changes.variable_steps(lowered)
    if is_custom_variable(name):
        return changes.variable_steps(name)
    return []


def all_steps(changes: RequestChanges) -> list[str]:
    """Every earlier step ``changes`` names."""
    steps = {step for _, step in changes.headers} | {step for _, step in changes.queries} | set(changes.verb)
    steps |= {step for _, step in changes.variables}
    return sorted(steps)


def describe_changes(changes: RequestChanges) -> list[str]:
    """One line per value earlier steps may have changed, for the callout prompt."""
    lines: list[str] = []

    def entry(name: str, every: str) -> str:
        return every if name == ANY else name

    for name, step in sorted(changes.headers):
        lines.append(f"request header {entry(name, 'every request header')} (by {step})")
    for name, step in sorted(changes.queries):
        lines.append(f"query parameter {entry(name, 'every query parameter')} (by {step})")
    for step in sorted(changes.verb):
        lines.append(f"the request verb (by {step})")
    for name, step in sorted(changes.variables):
        shown = "every flow variable" if name == ANY else (f"every variable under {name}" if name.endswith(".") else name)
        lines.append(f"{shown} (by {step})")
    return lines
