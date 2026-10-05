"""CP10 adversarial round 4: number placeholders written back bare into DataWeave read as the same numbers on Mule.

A number literal of a condition, of custom code (JavaScript, Python, Java) or of JSON text is shown to the AI as a
number placeholder (``«n1»``). When the AI writes one where a number goes in DataWeave, :meth:`Placeholders.restore`
writes the same number in a form DataWeave reads: separators, suffixes and a leading ``+`` dropped, hexadecimal,
octal and binary integers in decimal, a negative number in parentheses, a fraction and an exponent kept. This test
proves those forms on the local Mule runtime instead of trusting Python: a tiny generated app gets one flow (the AI's
answer, restored) that answers JSON built from each number written bare, the number in an arithmetic expression after
a minus, and whether each is a DataWeave Number. The response must hold every number exactly.

Marked ``runtime``: excluded from a plain ``pytest -q``, skipped with a reason when java, mvn or MULE_HOME is missing,
and failing instead under A2M_REQUIRE_RUNTIME=1 (see tests/runtime/conftest.py). The test starts its own Mule runtime
under a private MULE_BASE in pytest's tmp folder with conftest.py's plain helpers; teardown stops it through the
runner, kills only the PIDs the runner recorded and deletes the MULE_BASE. Run it with::

    systemd-run --user --scope -q -p MemoryMax=6G -p MemorySwapMax=0 -- env A2M_REQUIRE_RUNTIME=1 \\
        mise exec -- .venv/bin/python -m pytest -q --color=no -m runtime tests/runtime/test_cp10_number_restore_runtime.py
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
from collections.abc import Iterator
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any

import pytest

from .conftest import REQUIRE_RUNTIME_ENV, START_TIMEOUT, find_runtime_tools, free_port, stop_and_reap
from .test_cp8_restore_runtime import APP, BUILD_TIMEOUT, DEPLOY_TIMEOUT, FLOW_REL, call, set_listen_port, write_bundle

# (label, how the number is shown: (source kind, source text, the literal in it), the exact number it stands for)
NUMBERS: tuple[tuple[str, str, str, str, Decimal], ...] = (
    ("jsonNegative", "json", '{"a": -829406175302}', "-829406175302", Decimal(-829406175302)),
    ("jsonNegativeFractionExponent", "json", '{"a": -1.5e-3}', "-1.5e-3", Decimal("-0.0015")),
    ("jsonExponent", "json", '{"a": 1.5E+10}', "1.5E+10", Decimal(15000000000)),
    ("jsonPlus", "json", '{"a": +7}', "+7", Decimal(7)),
    ("conditionNegativeFraction", "condition", "request.header.x = -42.25", "-42.25", Decimal("-42.25")),
    ("javaLong", "java", "long a = 5000L;", "5000L", Decimal(5000)),
    ("javaHexInt", "java", "int a = 0xFFFFFFFF;", "0xFFFFFFFF", Decimal(-1)),
    ("javaDoubleExponent", "java", "double a = 2.5e-3d;", "2.5e-3d", Decimal("0.0025")),
    ("javascriptHex", "javascript", "var a = 0xFF;", "0xFF", Decimal(255)),
    ("javascriptBigInt", "javascript", "var a = 9007199254740993n;", "9007199254740993n", Decimal(9007199254740993)),
    ("javascriptExponent", "javascript", "var a = 1e-5;", "1e-5", Decimal("0.00001")),
    ("pythonSeparators", "python", "a = 1_000_000", "1_000_000", Decimal(1000000)),
    ("pythonOctal", "python", "a = 0o17", "0o17", Decimal(15)),
    (
        "pythonLargeInteger", "python", "a = 123456789012345678901234567890", "123456789012345678901234567890",
        Decimal(123456789012345678901234567890),
    ),
)


def number_tokens(table: Any) -> dict[str, str]:
    """Each label of :data:`NUMBERS` and the number placeholder the table showed for its literal."""
    from a2m.ai.provider import ItemKind

    kinds = {"java": ItemKind.JAVA, "javascript": ItemKind.JAVASCRIPT, "python": ItemKind.PYTHON}
    found: dict[str, str] = {}
    for label, kind, source, _literal, _value in NUMBERS:
        if kind == "json":
            shown = table.apigee(
                f'<AssignMessage name="AM-N"><Set><Payload contentType="application/json">{source}</Payload></Set>'
                "</AssignMessage>"
            )
        elif kind == "condition":
            shown = table.condition(source)
        else:
            shown = table.code(source, kinds[kind])
        tokens = re.findall(r"«n\d+»", shown or "")
        assert len(tokens) == 1, (label, shown)
        found[label] = tokens[0]
    return found


def answer_flow(tokens: dict[str, str]) -> str:
    """The flow the AI's answer adds: every number placeholder written bare where a number goes in DataWeave."""
    bare = ", ".join(f"{label}: {token}" for label, token in tokens.items())
    minus = ", ".join(f"{label}: 10 - {token}" for label, token in tokens.items())
    typed = ", ".join(f"{label}: {token} is Number" for label, token in tokens.items())
    return (
        '    <flow name="number-check">\n'
        '        <http:listener config-ref="http-listener-config" path="/number-check">\n'
        '            <http:response statusCode="200">\n'
        f"                <http:body>#[output application/json --- {{bare: {{{bare}}}, minus: {{{minus}}}, "
        f"typed: {{{typed}}}}}]</http:body>\n"
        "            </http:response>\n"
        "        </http:listener>\n"
        '        <set-payload value="#[\'unused\']" />\n'
        "    </flow>\n"
    )


@pytest.fixture(scope="module")
def number_runtime(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:
    """A Mule runtime of this module's own (skipped or failed like conftest's runtime_tools when tools are
    missing); teardown reaps only the recorded PIDs and deletes the MULE_BASE."""
    tools, missing = find_runtime_tools()
    if tools is None:
        reason = "Mule runtime tests not run, missing: " + "; ".join(missing)
        if os.environ.get(REQUIRE_RUNTIME_ENV) == "1":
            pytest.fail(f"{reason}. {REQUIRE_RUNTIME_ENV}=1 requires the runtime, so this is a failure.", pytrace=False)
        pytest.skip(reason)
    from a2m.verify.mule import MuleRunner

    mule_base = tmp_path_factory.mktemp("number-mule-base")
    runner = MuleRunner(mule_home=tools.mule_home, mule_base=mule_base)
    try:
        runner.start(timeout=START_TIMEOUT)
        yield runner
    finally:
        stop_and_reap(runner)
        shutil.rmtree(mule_base, ignore_errors=True)


@pytest.mark.runtime
def test_CP10_X38_number_placeholders_written_bare_read_as_the_same_numbers_on_mule(
    number_runtime: Any, tmp_path: Path
) -> None:
    """[CP10-X38] Number placeholders of JSON (negative, fraction with exponent, exponent, plus), a condition
    (negative fraction), Java (long, hexadecimal int, double with exponent), JavaScript (hexadecimal, BigInt,
    exponent) and Python (separators, octal, a large integer), written bare in DataWeave, are restored in forms that
    deploy on the real Mule runtime and evaluate to exactly the numbers they stand for, also after a minus."""
    from a2m.generator import generate_project
    from a2m.parser import read_bundle
    from a2m.verify.mule import BuildError, DeployError, package
    from a2m.verify.placeholders import Placeholders

    project = tmp_path / APP / "mule-app"
    generate_project(read_bundle(write_bundle(tmp_path / "in")), project)
    path = project / FLOW_REL
    original = path.read_text(encoding="utf-8")

    table = Placeholders()
    tokens = number_tokens(table)
    shown = table.mule({FLOW_REL: original})[FLOW_REL]
    assert shown is not None and shown.rstrip().endswith("</mule>"), (shown or "")[-200:]
    for _label, _kind, _source, literal, _value in NUMBERS:
        assert literal not in shown
    answer = shown[: shown.rindex("</mule>")] + answer_flow(tokens) + "</mule>\n"
    restored = table.restore(FLOW_REL, answer)
    assert restored.startswith(original[: original.rindex("</mule>")]), "the echoed part did not get its bytes back"
    path.write_text(restored, encoding="utf-8")

    port = free_port()
    set_listen_port(project, port)
    try:
        jar = package(project, timeout=BUILD_TIMEOUT)
    except BuildError as exc:
        pytest.fail(f"mvn package failed for {APP}: {exc}\n{exc.output}", pytrace=False)
    try:
        number_runtime.deploy(Path(jar), app_name=APP, timeout=DEPLOY_TIMEOUT)
    except DeployError as exc:
        pytest.fail(f"{APP} did not deploy with the restored numbers: {exc}\n{exc.log_excerpt}", pytrace=False)
    try:
        status, body = call(port, "/number-check")
    finally:
        with contextlib.suppress(Exception):
            number_runtime.undeploy(APP, timeout=60)

    assert status == 200, body
    data = json.loads(body, parse_float=Decimal, parse_int=Decimal)
    for label, _kind, _source, _literal, value in NUMBERS:
        assert data["bare"][label] == value, (label, data["bare"][label], value)
        with localcontext() as exact:
            exact.prec = 100  # Python's default 28 digits would round the large integer
            expected = 10 - value
        assert data["minus"][label] == expected, (label, data["minus"][label], expected)
        assert data["typed"][label] is True, (label, data["typed"][label])
