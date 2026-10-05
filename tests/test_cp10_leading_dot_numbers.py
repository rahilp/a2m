"""CP10 adversarial round 5: a number literal that starts with its "." keeps its dot inside its placeholder.

Round 4 started a number of code only at a digit, so JavaScript ``.07`` was shown as ``.«n1»`` with the placeholder
standing for ``07``, which a2m read as legacy octal 7. The AI could only write ``0.«n1»`` (restored as 0.7, ten
times the tax) or ``«n1»`` (restored as 7). Now:

* in JavaScript, Java and Python a "." with a digit after it starts the number (``.07``, ``.05e3``, Java ``.1f``),
  so the whole literal, dot included, is one ``«nN»``; in DataWeave the same where the "." cannot select a field;
* a Java float or double suffix on such a literal goes through the same inexact float rule (``.1f`` is refused);
* a number placeholder the AI joins to digits, a letter or a "." (``0.«n1»``, ``.«n1»``, ``«n1»0``) is refused when
  that would make another number of it;
* a condition that holds a number placeholder is refused as CP5 refuses it without asking the AI at all.

Only public entry points are used: ``Translator``, ``Place``, ``ItemKind``, ``Placeholders`` and the module constant
``DATAWEAVE``.

Case IDs:

* CP10-X39 - (the X32 family) a leading-dot number of JavaScript, Java or Python is one number placeholder, and
  written bare in DataWeave it is the same number.
* CP10-X40 - a leading-dot literal a2m cannot write exactly is refused (Java ``.1f``, JavaScript ``.5n``).
* CP10-X41 - a number placeholder joined to digits, a letter or a "." is refused; an echo of what a2m showed of
  DataWeave (a field selector ``payload.5`` included) still restores exactly.
* CP10-X42 - a bounded generator over number spellings of JavaScript, Java and Python (leading dot, trailing dot,
  exponent, suffixes, separators, radix, leading zeros): the restored DataWeave number is the source language's
  value, or the answer is refused where the value cannot be written exactly. Python's own parser checks the oracle.
* CP10-X43 - a condition that holds a number placeholder never reaches the provider; it is refused as CP5 refuses it.
"""

from __future__ import annotations

import ast
import json
import re
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest

from a2m.ai.placeholders import DATAWEAVE, PlaceholderError, Placeholders
from a2m.ai.provider import ItemKind
from a2m.ai.translate import ExpressionTranslated, Place, Translator

ANY_TOKEN = re.compile(r"«[vn]\d+»")
NUMBER_TOKEN = re.compile(r"«n\d+»")
DW_NUMBER = re.compile(r"\(?(-?\d+(?:\.\d+)?(?:e[+-]?\d+)?)\)?")
PLACE = Place("dot-proxy", "default PreFlow request", "request", "AM-Before", "AM-After")


class Recorder:
    """A provider that records every request and answers with ``answer(request)``."""

    def __init__(self, answer: Any) -> None:
        self.requests: list[Any] = []
        self.answer = answer

    def complete(self, request: Any) -> str:
        self.requests.append(request)
        return str(self.answer(request))


def _only_number_token(shown: str) -> str:
    """The one placeholder of ``shown``: a number placeholder, with no "." or digit left next to it or anywhere."""
    tokens = ANY_TOKEN.findall(shown)
    assert len(tokens) == 1 and tokens[0].startswith("«n"), shown
    assert re.search(r"\d", ANY_TOKEN.sub("", shown)) is None, shown
    assert re.search(r"\.«", shown) is None, shown
    return tokens[0]


@pytest.mark.parametrize(
    ("kind", "code", "expected"),
    [
        pytest.param(ItemKind.JAVASCRIPT, "var tax = amount * .07;", "0.07", id="javascript-dot-07"),
        pytest.param(ItemKind.JAVASCRIPT, "var r = .05e3;", "0.05e3", id="javascript-dot-exponent"),
        pytest.param(ItemKind.JAVASCRIPT, "var r = .010;", "0.010", id="javascript-dot-010"),
        pytest.param(ItemKind.JAVASCRIPT, "var r = .08;", "0.08", id="javascript-dot-08"),
        pytest.param(ItemKind.JAVASCRIPT, "var r = a ?.5 : b;", "0.5", id="javascript-ternary-not-optional-chain"),
        pytest.param(ItemKind.JAVA, "double r = .05;", "0.05", id="java-dot-double"),
        pytest.param(ItemKind.JAVA, "float r = .5f;", "0.5", id="java-dot-exact-float"),
        pytest.param(ItemKind.JAVA, "double r = .25D;", "0.25", id="java-dot-double-suffix"),
        pytest.param(ItemKind.PYTHON, "r = .05", "0.05", id="python-dot-05"),
        pytest.param(ItemKind.PYTHON, "r = a if b else.5", "0.5", id="python-dot-after-keyword"),
    ],
)
def test_CP10_X39_a_leading_dot_number_is_one_placeholder_and_restores_as_the_same_number(
    kind: ItemKind, code: str, expected: str
) -> None:
    """[CP10-X39] (the X32 family, leading dot) A number of custom code written with a leading dot is one number
    placeholder, the dot included (no bare "." is left before it); written bare in DataWeave it is the same number,
    never the digits after the dot read as an integer (legacy octal ``07`` is 7)."""
    table = Placeholders()
    shown = table.code(code, kind)

    token = _only_number_token(shown)

    assert table.restore_code(f"vars.amount * {token}") == f"vars.amount * {expected}"


@pytest.mark.parametrize(
    ("kind", "code"),
    [
        pytest.param(ItemKind.JAVA, "float r = .1f;", id="java-dot-inexact-float"),
        pytest.param(ItemKind.JAVA, "float r = .07F;", id="java-dot-inexact-float-upper"),
        pytest.param(ItemKind.JAVASCRIPT, "var r = .5n;", id="javascript-dot-bigint"),
        pytest.param(ItemKind.PYTHON, "r = .5j", id="python-dot-imaginary"),
    ],
)
def test_CP10_X40_a_leading_dot_literal_that_cannot_be_written_exactly_is_refused(kind: ItemKind, code: str) -> None:
    """[CP10-X40] A leading-dot literal goes through the same rules as any number: a Java float that its decimal does
    not equal (``.1f`` is 0.100000001490116...) and a literal that is no number DataWeave can write are refused, not
    written as another number."""
    table = Placeholders()
    shown = table.code(code, kind)
    token = _only_number_token(shown)

    with pytest.raises(PlaceholderError):
        table.restore_code(f"vars.amount * {token}")


@pytest.mark.parametrize(
    "template",
    [
        pytest.param("vars.a * 0.{}", id="zero-dot-before"),
        pytest.param("vars.a * .{}", id="dot-before"),
        pytest.param("vars.a * 1{}", id="digit-before"),
        pytest.param("vars.a * {}0", id="digit-after"),
        pytest.param("vars.a * {}e3", id="exponent-after"),
        pytest.param("vars.a * {}.5", id="fraction-after"),
        pytest.param("vars.a * x{}", id="word-before"),
        pytest.param("payload.{}", id="selector-of-another-language"),
    ],
)
@pytest.mark.parametrize(
    ("kind", "code"),
    [
        pytest.param(ItemKind.JAVASCRIPT, "var r = a * .07;", id="javascript-dot"),
        pytest.param(ItemKind.JAVASCRIPT, "var r = a * 5;", id="javascript-integer"),
        pytest.param(ItemKind.PYTHON, "r = a * 2.5", id="python-fraction"),
    ],
)
def test_CP10_X41_a_number_placeholder_joined_into_another_number_is_refused(
    kind: ItemKind, code: str, template: str
) -> None:
    """[CP10-X41] A number placeholder of custom code written joined to digits, a letter or a "." would make another
    number of it (``0.«n1»`` for 5 is 0.5, for .07 it is not even a number): refused, whatever the literal."""
    table = Placeholders()
    token = _only_number_token(table.code(code, kind))

    with pytest.raises(PlaceholderError):
        table.restore_code(template.format(token))


@pytest.mark.parametrize(
    "expression",
    [
        pytest.param("payload.5 + vars.a", id="field-selector"),
        pytest.param("if (vars.a) .5 else .25", id="dot-after-parenthesis"),
        pytest.param("vars.a * .07", id="dot-after-operator"),
        pytest.param("[1, .5, 2.]", id="array"),
    ],
)
def test_CP10_X41_an_echo_of_dataweave_a2m_showed_restores_exactly(expression: str) -> None:
    """[CP10-X41] What a2m shows of DataWeave (the fix loop's view), echoed back unchanged, restores byte for byte:
    a number of the same language is written back exactly as it was, so a field selector ``payload.5`` and a "."
    a2m left visible are not refused. A leading-dot number after an operator is one placeholder."""
    table = Placeholders()
    xml = f'<set-variable variableName="r" value="#[{expression}]"/>'
    shown = table.mule({"flow.xml": xml})["flow.xml"]

    assert shown is not None
    assert re.search(r"\d", ANY_TOKEN.sub("", shown)) is None, shown
    assert table.restore("flow.xml", shown) == xml
    if expression in ("vars.a * .07", "[1, .5, 2.]"):
        assert ".«" not in shown, shown  # a "." after an operator or a comma starts the number


# ------------------------------------------------------------------------------------------------ X42: generator


@dataclass(frozen=True, slots=True)
class Spelling:
    """A number literal of ``kind`` and what the language reads it as: ``expect`` is "exact" (the restored number
    must equal ``value``), "float32" (a Java float: restored as ``value`` when a float holds it exactly, else refused)
    or "refuse" (a2m cannot write it as a DataWeave number). ``python`` is Python's own reading, to check the oracle."""

    kind: ItemKind
    text: str
    expect: str
    value: Decimal | None


INTS = ("", "0", "7", "10", "007", "08", "019")
FRACTIONS: tuple[str | None, ...] = (None, "", "5", "07", "05", "010", "25")
EXPONENTS = ("", "e3", "E-2", "e+1")
SUFFIXES = {ItemKind.JAVASCRIPT: ("", "n"), ItemKind.JAVA: ("", "L", "f", "F", "d", "D"), ItemKind.PYTHON: ("", "j")}
RADIX: dict[ItemKind, tuple[tuple[str, str, int | None], ...]] = {
    ItemKind.JAVASCRIPT: (
        ("0x1F", "exact", 31), ("0X1f", "exact", 31), ("0o17", "exact", 15), ("0O17", "exact", 15),
        ("0b101", "exact", 5), ("0B1_01", "exact", 5), ("0x1F_FF", "exact", 0x1FFF), ("0x1Fn", "exact", 31),
        ("0b101n", "exact", 5),
    ),
    ItemKind.JAVA: (
        ("0x1F", "exact", 31), ("0x1FL", "exact", 31), ("0b101", "exact", 5), ("0B1_01", "exact", 5),
        ("0x7FFF_FFFF", "exact", 2147483647), ("0xFFFFFFFF", "exact", -1), ("0xFFFFFFFFL", "exact", 4294967295),
        ("0x1p3", "refuse", None), ("0x1.8p1", "refuse", None),
    ),
    ItemKind.PYTHON: (
        ("0x1F", "exact", 31), ("0o17", "exact", 15), ("0b101", "exact", 5), ("0x_1f", "exact", 31),
        ("0B1_01", "exact", 5),
    ),
}


def _separated(digits: str) -> str:
    return digits[0] + "_" + digits[1:]


def _decimal_spellings(kind: ItemKind) -> Iterator[Spelling]:
    """Every combination of integer part, fraction, exponent, suffix and separator the language accepts, with the
    value it gives; combinations it rejects are left out (by the language's grammar, built here part by part)."""
    for whole in INTS:
        for fraction in FRACTIONS:
            if whole == "" and not fraction:
                continue  # no digits: not a number
            for exponent in EXPONENTS:
                for suffix in SUFFIXES[kind]:
                    for separator in (False, True):
                        spelled = _spelling(kind, whole, fraction, exponent, suffix, separator)
                        if spelled is not None:
                            yield spelled


def _spelling(
    kind: ItemKind, whole: str, fraction: str | None, exponent: str, suffix: str, separator: bool
) -> Spelling | None:
    int_text, fraction_text = whole, fraction
    if separator:
        if len(whole) >= 2:
            int_text = _separated(whole)
        elif fraction is not None and len(fraction) >= 2:
            fraction_text = _separated(fraction)
        else:
            return None
    text = int_text + ("." + fraction_text if fraction_text is not None else "") + exponent + suffix
    decimal = Decimal((whole or "0") + "." + (fraction or "0") + ("e" + exponent[1:] if exponent else ""))
    integer_only = fraction is None and not exponent
    leading_zero = len(whole) > 1 and whole[0] == "0"
    octal = leading_zero and set(whole) <= set("01234567")
    if kind is ItemKind.JAVASCRIPT:
        if suffix == "n":
            if not integer_only or leading_zero:
                return None
            return Spelling(kind, text, "exact", Decimal(int(whole)))
        if leading_zero and separator and int_text != whole:
            return None  # 0_7 and 0_8.5 are syntax errors
        if leading_zero and not integer_only:
            # 07.5 is a syntax error and 007.e3 reads a property of 7: no number; 08.5 is 8.5 (sloppy mode)
            return Spelling(kind, text, "refuse", None) if octal else Spelling(kind, text, "exact", decimal)
        if integer_only:
            return Spelling(kind, text, "exact", Decimal(int(whole, 8) if octal else int(whole)))
        return Spelling(kind, text, "exact", decimal)
    if kind is ItemKind.JAVA:
        if suffix in ("", "L") and integer_only:
            if leading_zero and not octal:
                return None  # 08 and 019 are not Java integers
            return Spelling(kind, text, "exact", Decimal(int(whole, 8) if leading_zero else int(whole)))
        if suffix == "L":
            return None
        if suffix in ("f", "F"):
            return Spelling(kind, text, "float32", decimal)
        return Spelling(kind, text, "exact", decimal)
    # Python
    if suffix == "j":
        return Spelling(kind, text, "refuse", None)
    if integer_only:
        if leading_zero and set(whole) != {"0"}:
            return None  # 007 is a syntax error in Python 3
        return Spelling(kind, text, "exact", Decimal(int(whole)))
    return Spelling(kind, text, "exact", decimal)


def spellings(kind: ItemKind) -> list[Spelling]:
    """The bounded set of number spellings of ``kind``: the decimal combinations and the radix literals."""
    radix = [
        Spelling(kind, text, expect, None if value is None else Decimal(value)) for text, expect, value in RADIX[kind]
    ]
    return list(_decimal_spellings(kind)) + radix


TEMPLATES = {
    ItemKind.JAVASCRIPT: ("var r = a * {};", "var r=[{}];", "var r = b ?{} : c;"),
    ItemKind.JAVA: ("double r = a * {};", "Object[] r = {{{}}};"),
    ItemKind.PYTHON: ("r = a * {}", "r = [{}]", "r = (b,{})"),
}


def _float32_exact(value: Decimal) -> bool:
    packed = struct.unpack("<f", struct.pack("<f", float(value)))[0]
    return Decimal(packed) == value


def _check(spelled: Spelling, template: str) -> str | None:
    """None when a2m shows ``spelled`` (in ``template``) as one number placeholder and restores it right; else what
    went wrong."""
    table = Placeholders()
    code = template.format(spelled.text)
    shown = table.code(code, spelled.kind)
    tokens = ANY_TOKEN.findall(shown)
    if len(tokens) != 1 or not tokens[0].startswith("«n") or re.search(r"[\d.]«|»[\w.]", shown):
        return f"{code!r} was shown as {shown!r}, not as one number placeholder standing alone"
    try:
        restored = table.restore_code(f"{tokens[0]} + 0", DATAWEAVE)
    except PlaceholderError:
        restored = None
    must_refuse = spelled.expect == "refuse" or (
        spelled.expect == "float32" and spelled.value is not None and not _float32_exact(spelled.value)
    )
    if restored is None:
        return None if must_refuse else f"{code!r} was refused, but {spelled.value} can be written exactly"
    if must_refuse:
        return f"{code!r} was restored as {restored!r}, but it is no number DataWeave can write exactly"
    found = re.fullmatch(r"(.+) \+ 0", restored)
    number = DW_NUMBER.fullmatch(found.group(1)) if found is not None else None
    if number is None:
        return f"{code!r} was restored as {restored!r}, which is not a DataWeave number"
    if Decimal(number.group(1)) != spelled.value:
        return f"{code!r} was restored as {number.group(1)}, but the language reads {spelled.value}"
    return None


@pytest.mark.parametrize("kind", [ItemKind.JAVASCRIPT, ItemKind.JAVA, ItemKind.PYTHON], ids=lambda kind: kind.value)
def test_CP10_X42_every_generated_number_spelling_restores_as_the_languages_value_or_is_refused(
    kind: ItemKind,
) -> None:
    """[CP10-X42] A bounded generator over the number spellings of the language (leading dot, trailing dot,
    exponents with and without a sign, suffixes, separators, radix prefixes, leading zeros): in every template each
    literal is one number placeholder with nothing joined to it, and written bare in DataWeave it is exactly the
    number the language reads; only a literal DataWeave cannot hold exactly (an inexact Java float, a hexadecimal
    float, a Python imaginary number) is refused, and such a literal is always refused."""
    generated = spellings(kind)
    assert len(generated) > 100 and any(item.text.startswith(".") for item in generated)
    problems = [
        problem
        for spelled in generated
        for template in TEMPLATES[kind]
        if (problem := _check(spelled, template)) is not None
    ]

    assert not problems, f"{len(problems)} problems, first ones:\n" + "\n".join(problems[:15])


def test_CP10_X42_the_python_oracle_agrees_with_pythons_own_parser() -> None:
    """[CP10-X42] The generator's Python values and its grammar agree with Python itself: every spelling it keeps
    parses, to the value it expects, and every one it leaves out is a syntax error."""
    kept = {item.text: item for item in spellings(ItemKind.PYTHON)}
    for item in kept.values():
        parsed = ast.literal_eval(item.text)
        if item.expect == "refuse":
            assert isinstance(parsed, complex), item
        elif isinstance(parsed, float):
            assert item.value is not None and float(item.value) == parsed, item
        else:
            assert item.value == Decimal(parsed), item
    for whole in INTS:
        for fraction in FRACTIONS:
            for exponent in EXPONENTS:
                text = whole + ("." + fraction if fraction is not None else "") + exponent
                if text in kept or (whole == "" and not fraction):
                    continue
                with pytest.raises(SyntaxError):
                    ast.literal_eval(text)


# ------------------------------------------------------------------------------------------------ X43: no request


@pytest.mark.parametrize(
    "condition",
    [
        pytest.param("request.header.x-pin = 265018394712", id="equals"),
        pytest.param('(request.header.accept =| "application/json") and (request.queryparam.v = 2)', id="mixed"),
        pytest.param("request.queryparam.n > -7.5", id="signed-fraction"),
    ],
)
def test_CP10_X43_a_condition_with_a_number_placeholder_never_reaches_the_provider(condition: str) -> None:
    """[CP10-X43] a2m refuses every translation of a condition that compares with a number, so it does not ask the
    AI: the fake provider receives no request, and the result is CP5's refusal, which names the placeholder, never
    the number."""

    def answer(request: Any) -> str:
        return json.dumps({"status": "declined", "reason": "never asked"})

    provider = Recorder(answer)

    result = Translator(provider).expression("Step Pin", "Pin", condition, PLACE, "comparing with a number")

    assert provider.requests == []
    assert not isinstance(result, ExpressionTranslated), result
    assert "compares with a number" in result.reason and "not translated" in result.reason, result.reason
    assert NUMBER_TOKEN.search(result.reason) is not None, result.reason
    for number in ("265018394712", "7.5", "= 2"):
        assert number not in result.reason, result.reason


def test_CP10_X43_a_condition_without_a_number_is_still_sent_to_the_provider() -> None:
    """[CP10-X43] Only a condition with a number placeholder skips the provider: one that compares only with text is
    still sent, once."""
    provider = Recorder(lambda request: json.dumps({"status": "declined", "reason": "no"}))

    Translator(provider).expression("Step Ua", "Ua", 'request.header.ua =| "curl"', PLACE, "StartsWith")

    assert len(provider.requests) == 1
