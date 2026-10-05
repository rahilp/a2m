"""CP10 adversarial round 5: the X42 number spelling generator agrees with real JavaScript and Java.

tests/test_cp10_leading_dot_numbers.py (CP10-X42) builds number spellings part by part and knows, from the language's
grammar, which ones the language accepts and what number each is; Python's own parser checks the Python part there.
This test checks the JavaScript and Java parts against the languages themselves: node evaluates every JavaScript
spelling, and javac compiles every Java spelling (each in its own file, so one syntax error hides no other) for java
to print. Every spelling the generator keeps must give exactly the value it expects (a double as its exact binary
value, a Java float as its exact float value), every one it expects a2m to refuse as no number must not be one, and
every combination it leaves out must be a syntax error (in JavaScript, a legacy octal before a ".", such as
``007.n``, reads a property of the number instead).

Marked ``runtime``: excluded from a plain ``pytest -q``. It needs node, javac and java on PATH (``mise exec``); when
one is missing it is skipped with a reason, and fails instead under A2M_REQUIRE_RUNTIME=1. Run it with::

    systemd-run --user --scope -q -p MemoryMax=3G -p MemorySwapMax=0 -- env A2M_REQUIRE_RUNTIME=1 \\
        mise exec -- .venv/bin/python -m pytest -q --color=no -m runtime tests/runtime/test_cp10_number_spellings_runtime.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import struct
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest

from a2m.ai.provider import ItemKind

from .conftest import REQUIRE_RUNTIME_ENV

pytestmark = pytest.mark.runtime

GENERATOR_FILE = Path(__file__).resolve().parents[1] / "test_cp10_leading_dot_numbers.py"
TIMEOUT = 120


def _generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("cp10_number_spellings", GENERATOR_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up there
    spec.loader.exec_module(module)
    return module


def _tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        message = f"{name} is not on PATH (run under mise exec)"
        if os.environ.get(REQUIRE_RUNTIME_ENV) == "1":
            pytest.fail(message)
        pytest.skip(message)
    return path


def _combinations(generator: ModuleType, kind: ItemKind) -> list[tuple[str, bool]]:
    """Every decimal combination the generator considers for ``kind`` as (text, whether the generator keeps it)."""
    kept = {item.text for item in generator.spellings(kind)}
    found: dict[str, bool] = {}
    for whole in generator.INTS:
        for fraction in generator.FRACTIONS:
            if whole == "" and not fraction:
                continue
            for exponent in generator.EXPONENTS:
                for suffix in generator.SUFFIXES[kind]:
                    for separator in (False, True):
                        int_text, fraction_text = whole, fraction
                        if separator:
                            if len(whole) >= 2:
                                int_text = whole[0] + "_" + whole[1:]
                            elif fraction is not None and len(fraction) >= 2:
                                fraction_text = fraction[0] + "_" + fraction[1:]
                            else:
                                continue
                        text = int_text + ("." + fraction_text if fraction_text is not None else "") + exponent + suffix
                        found[text] = text in kept
    for item in generator.spellings(kind):
        found.setdefault(item.text, True)
    return sorted(found.items())


def _float32(value: Decimal) -> Decimal:
    return Decimal(struct.unpack("<f", struct.pack("<f", float(value)))[0])


def test_CP10_X44_the_javascript_spellings_match_node() -> None:
    """[CP10-X44] Every JavaScript spelling the X42 generator keeps evaluates in node to the number it expects, and
    every one it leaves out is a SyntaxError (except a non-octal leading zero before a fraction or exponent)."""
    node = _tool("node")
    generator = _generator()
    combinations = _combinations(generator, ItemKind.JAVASCRIPT)
    script = (
        "const items = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
        "console.log(JSON.stringify(items.map(s => { try { const v = (0, eval)('(' + s + ')');"
        " return typeof v === 'bigint' ? v.toString() + 'n' : String(v); } catch (e) { return 'ERR ' + e.name; } })));"
    )
    done = subprocess.run(
        [node, "-e", script], input=json.dumps([text for text, _ in combinations]), capture_output=True, text=True,
        timeout=TIMEOUT, check=True,
    )
    results = dict(zip([text for text, _ in combinations], json.loads(done.stdout), strict=True))
    expected = {item.text: item for item in generator.spellings(ItemKind.JAVASCRIPT)}
    problems: list[str] = []
    for text, kept in combinations:
        result = results[text]
        if not kept:
            member = re.fullmatch(r"0[0-7]+\..*", text) is not None  # 007.n reads a property of 7
            if not result.startswith("ERR") and not member:
                problems.append(f"{text}: left out, but node reads {result}")
            continue
        item = expected[text]
        if item.expect == "refuse":
            if re.fullmatch(r"-?[0-9.e+]+n?|Infinity", result):
                problems.append(f"{text}: expected refused as no number, but node reads it as {result}")
            continue
        if result.startswith("ERR"):
            problems.append(f"{text}: kept, but node says {result}")
        elif result.endswith("n"):
            if Decimal(result[:-1]) != item.value:
                problems.append(f"{text}: node reads {result}, the generator expects {item.value}")
        elif float(result) != float(item.value):
            problems.append(f"{text}: node reads {result}, the generator expects {item.value}")

    assert len(combinations) > 100
    assert not problems, "\n".join(problems[:20])


def _java_values(javac: str, java: str, texts: list[str], folder: Path) -> dict[int, Decimal]:
    """The exact value Java gives each of ``texts`` (as a double: a float or an integer widened exactly), by index;
    one that does not compile is absent. Each literal is its own compilation unit, so one syntax error cannot hide
    another literal."""
    source = folder / "src"
    source.mkdir()
    for index, text in enumerate(texts):
        (source / f"S{index}.java").write_text(
            f"class S{index} {{ static String v() {{ return new java.math.BigDecimal((double) ({text}))"
            ".toPlainString(); } }\n"
        )
    files = sorted(str(path) for path in source.glob("S*.java"))
    first = subprocess.run(
        [javac, "-J-Xmx512m", "-Xmaxerrs", "100000", "-d", str(folder / "probe"), *files], capture_output=True,
        text=True, timeout=TIMEOUT, check=False,
    )
    broken = {int(number) for number in re.findall(r"S(\d+)\.java:\d+: error", first.stdout + first.stderr)}
    good = [index for index in range(len(texts)) if index not in broken]
    body = "".join(f'        System.out.println("K{index} " + S{index}.v());\n' for index in good)
    (source / "Main.java").write_text(f"class Main {{\n    public static void main(String[] a) {{\n{body}    }}\n}}\n")
    out = folder / "out"
    subprocess.run(
        [javac, "-J-Xmx512m", "-d", str(out), str(source / "Main.java"), *(str(source / f"S{i}.java") for i in good)],
        capture_output=True, text=True, timeout=TIMEOUT, check=True,
    )
    done = subprocess.run(
        [java, "-Xmx256m", "-cp", str(out), "Main"], capture_output=True, text=True, timeout=TIMEOUT, check=True
    )
    found = re.finditer(r"^K(\d+) (\S+)$", done.stdout, re.MULTILINE)
    return {int(match.group(1)): Decimal(match.group(2)) for match in found}


def test_CP10_X44_the_java_spellings_match_javac(tmp_path: Path) -> None:
    """[CP10-X44] Every Java spelling the X42 generator keeps compiles with javac and evaluates to the number it
    expects (a double as its exact binary value, a float as its exact float value, an int or long as the integer, a
    hexadecimal int as its two's complement value), and every one it leaves out does not compile."""
    javac, java = _tool("javac"), _tool("java")
    generator = _generator()
    combinations = _combinations(generator, ItemKind.JAVA)
    values = _java_values(javac, java, [text for text, _ in combinations], tmp_path)
    expected = {item.text: item for item in generator.spellings(ItemKind.JAVA)}
    problems: list[str] = []
    for index, (text, kept) in enumerate(combinations):
        value = values.get(index)
        if not kept:
            if value is not None:
                problems.append(f"{text}: left out, but Java reads {value}")
            continue
        item = expected[text]
        if value is None:
            problems.append(f"{text}: kept, but it does not compile")
        elif item.expect == "refuse":
            continue  # a hexadecimal float: a Java number, but no DataWeave literal a2m writes
        elif item.expect == "float32":
            if value != _float32(item.value):
                problems.append(f"{text}: Java reads {value}, the generator expects the float of {item.value}")
        elif re.search(r"[.eEpP]|[dD]$", text) and not text.startswith(("0x", "0X")):
            if value != Decimal(float(item.value)):
                problems.append(f"{text}: Java reads {value}, the generator expects the double of {item.value}")
        elif value != item.value:
            problems.append(f"{text}: Java reads {value}, the generator expects {item.value}")

    assert len(combinations) > 100 and len(values) > 100, len(values)
    assert not problems, "\n".join(problems[:20])
