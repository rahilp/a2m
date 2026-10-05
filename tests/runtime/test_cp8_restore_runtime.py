"""CP8 adversarial round 6: values a2m writes back into DataWeave string literals read exactly on the real Mule runtime.

When the AI writes a placeholder inside a DataWeave string literal of a fix, :meth:`Placeholders.restore` spells the
value for that string: backslash, the quote, ``$`` (written ``\\$``, so it never starts an interpolation), newline
and tab escaped. This test proves the spelling on the local Mule runtime instead of trusting a Python round trip.

A tiny bundle is generated with a2m. Its proxy.xml is shown through a :class:`Placeholders` table, as a fix request
shows it, and the AI's answer adds one flow (listening on /restore-check of the generated listener) that answers
JSON built from DataWeave string literals: each value in a single-quoted and a double-quoted string, once in an
attribute (``set-payload value``) and once in element text (``http:body``). The answer goes through
``Placeholders.restore``, the restored proxy.xml is built with Maven, deployed to the session runtime and called;
the response must hold every value exactly.

Marked ``runtime``: excluded from a plain ``pytest -q``, skipped with a reason when java, mvn or MULE_HOME is missing,
and failing instead under A2M_REQUIRE_RUNTIME=1 (see tests/runtime/conftest.py). The test starts its own Mule runtime
under a private MULE_BASE in pytest's tmp folder, with conftest.py's plain helpers (not its fixtures, which pytest
cannot find for this file when the command line names another tests/runtime file before a file outside that folder):
teardown stops it through the runner, kills only the PIDs the runner recorded and deletes the MULE_BASE. Run it
with::

    systemd-run --user --scope -q -p MemoryMax=6G -p MemorySwapMax=0 -- env A2M_REQUIRE_RUNTIME=1 \\
        mise exec -- .venv/bin/python -m pytest -q --color=no -m runtime tests/runtime/test_cp8_restore_runtime.py
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import re
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from .conftest import REQUIRE_RUNTIME_ENV, START_TIMEOUT, find_runtime_tools, free_port, stop_and_reap

APP = "restore-check-proxy"
FLOW_REL = "src/main/mule/proxy.xml"
XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
DEPLOY_TIMEOUT = 180.0
BUILD_TIMEOUT = 900.0
# Each value holds what a DataWeave string literal must escape: $ (interpolation), both quotes, backslash, newline.
VALUES = (
    "cost $5",
    "$(vars.never)",
    "$name and $",
    "it's",
    'say "hi"',
    "back\\slash \\n \\$",
    "two\nlines\tand a tab",
    "all: $(x) 'a' \"b\" \\ \n end $",
)


def write_bundle(parent: Path) -> Path:
    """parent/restore-check-proxy/apiproxy/...: one proxy endpoint with one response header step."""
    root = parent / APP / "apiproxy"
    for sub in ("policies", "proxies", "targets"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    (root / f"{APP}.xml").write_text(
        XML_HEAD + f'<APIProxy revision="1" name="{APP}"><Policies><Policy>AM-Header</Policy></Policies>'
        "<ProxyEndpoints><ProxyEndpoint>default</ProxyEndpoint></ProxyEndpoints>"
        "<TargetEndpoints><TargetEndpoint>default</TargetEndpoint></TargetEndpoints></APIProxy>\n",
        encoding="utf-8",
    )
    (root / "policies" / "AM-Header.xml").write_text(
        XML_HEAD + '<AssignMessage name="AM-Header"><Set><Headers><Header name="X-Check">yes</Header></Headers></Set>'
        '<AssignTo createNew="false" transport="http" type="response"/></AssignMessage>\n',
        encoding="utf-8",
    )
    (root / "proxies" / "default.xml").write_text(
        XML_HEAD + '<ProxyEndpoint name="default"><PreFlow name="PreFlow"><Request/><Response><Step>'
        '<Name>AM-Header</Name></Step></Response></PreFlow><Flows/><PostFlow name="PostFlow"><Request/><Response/>'
        "</PostFlow><HTTPProxyConnection><BasePath>/main</BasePath><VirtualHost>default</VirtualHost>"
        '</HTTPProxyConnection><RouteRule name="default"><TargetEndpoint>default</TargetEndpoint></RouteRule>'
        "</ProxyEndpoint>\n",
        encoding="utf-8",
    )
    (root / "targets" / "default.xml").write_text(
        XML_HEAD + '<TargetEndpoint name="default"><PreFlow name="PreFlow"><Request/><Response/></PreFlow><Flows/>'
        '<PostFlow name="PostFlow"><Request/><Response/></PostFlow><HTTPTargetConnection>'
        "<URL>http://127.0.0.1:9/unused</URL></HTTPTargetConnection></TargetEndpoint>\n",
        encoding="utf-8",
    )
    return parent / APP


def set_listen_port(project: Path, port: int) -> None:
    found = sorted((project / "src" / "main" / "resources").glob("*.properties"))
    assert len(found) == 1, found
    text = found[0].read_text(encoding="utf-8")
    updated, count = re.subn(r"(?m)^http\.listener\.port=.*$", f"http.listener.port={port}", text)
    assert count == 1, text
    found[0].write_text(updated, encoding="utf-8")


def answer_flow(tokens: list[str]) -> str:
    """The flow the AI's answer adds, as the AI writes it: placeholders inside DataWeave string literals."""
    single = ", ".join(f"'{token}'" for token in tokens)
    double = ", ".join(f'"{token}"' for token in tokens)
    attribute_double = double.replace('"', "&quot;")
    return (
        '    <flow name="restore-check">\n'
        '        <http:listener config-ref="http-listener-config" path="/restore-check">\n'
        '            <http:response statusCode="200">\n'
        "                <http:body>#[output application/json --- {attribute: payload, text: "
        f"{{single: [{single}], double: [{double}]}}}}]</http:body>\n"
        "            </http:response>\n"
        "        </http:listener>\n"
        f'        <set-payload value="#[output application/json --- {{single: [{single}], double: '
        f'[{attribute_double}]}}]" mimeType="application/json" />\n'
        "    </flow>\n"
    )


@pytest.fixture(scope="module")
def restore_runtime(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:
    """A Mule runtime of this module's own (skipped or failed like conftest's runtime_tools when tools are
    missing); teardown reaps only the recorded PIDs and deletes the MULE_BASE."""
    tools, missing = find_runtime_tools()
    if tools is None:
        reason = "Mule runtime tests not run, missing: " + "; ".join(missing)
        if os.environ.get(REQUIRE_RUNTIME_ENV) == "1":
            pytest.fail(f"{reason}. {REQUIRE_RUNTIME_ENV}=1 requires the runtime, so this is a failure.", pytrace=False)
        pytest.skip(reason)
    from a2m.verify.mule import MuleRunner

    mule_base = tmp_path_factory.mktemp("restore-mule-base")
    runner = MuleRunner(mule_home=tools.mule_home, mule_base=mule_base)
    try:
        runner.start(timeout=START_TIMEOUT)
        yield runner
    finally:
        stop_and_reap(runner)
        shutil.rmtree(mule_base, ignore_errors=True)


def call(port: int, path: str) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


@pytest.mark.runtime
def test_CP8_X39_restored_dataweave_strings_with_dollar_quotes_backslashes_and_newlines_read_exactly_on_mule(
    restore_runtime: Any, tmp_path: Path
) -> None:
    """[CP8-X39] A value holding $, $(...), quotes, backslashes, a newline and a tab, written back by
    Placeholders.restore into single- and double-quoted DataWeave strings (in an attribute and in element text),
    deploys on the real Mule runtime and reads as exactly that value."""
    from a2m.generator import generate_project
    from a2m.parser import read_bundle
    from a2m.verify.mule import BuildError, DeployError, package
    from a2m.verify.placeholders import Placeholders

    project = tmp_path / APP / "mule-app"
    generate_project(read_bundle(write_bundle(tmp_path / "in")), project)
    path = project / FLOW_REL
    original = path.read_text(encoding="utf-8")

    table = Placeholders()
    shown = table.mule({FLOW_REL: original})[FLOW_REL]
    assert shown is not None
    tokens = [table.token(value) for value in VALUES]
    assert shown.rstrip().endswith("</mule>"), shown[-200:]
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
        restore_runtime.deploy(Path(jar), app_name=APP, timeout=DEPLOY_TIMEOUT)
    except DeployError as exc:
        pytest.fail(f"{APP} did not deploy with the restored strings: {exc}\n{exc.log_excerpt}", pytrace=False)
    try:
        status, body = call(port, "/restore-check")
    finally:
        with contextlib.suppress(Exception):
            restore_runtime.undeploy(APP, timeout=60)

    assert status == 200, body
    expected = {"single": list(VALUES), "double": list(VALUES)}
    assert json.loads(body) == {"attribute": expected, "text": expected}, body.decode("utf-8", "replace")
