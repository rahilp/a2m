"""CP8-T18: on the real local Mule runtime, a wrong AI translation of a JavaScript callout fails its golden
test, the AI's fix corrects it and the re-test passes.

Marked ``runtime`` like the other files here: excluded from a plain ``pytest -q``, skipped with a reason
when java, mvn or MULE_HOME is missing, and failing instead under A2M_REQUIRE_RUNTIME=1 (see
tests/runtime/conftest.py). Run it with::

    A2M_REQUIRE_RUNTIME=1 mise exec -- .venv/bin/python -m pytest -q -m runtime \
        tests/runtime/test_cp8_fix_loop_runtime.py

The whole run goes through the public CLI entry point, in-process (``a2m.cli.main``), with the real
engine pipeline (no injected stages, no injected runner): ``a2m migrate <input> --out <out> --llm fake
--mock-backends --golden <golden> --max-fix-attempts 3``. ``--llm fake`` picks a2m's own
:class:`a2m.ai.fake.FakeProvider`, pointed at a tmp answers folder (``A2M_FAKE_LLM_DIR``) that holds:

* ``javascript.JS-SetVersion.json``: the CP6 canned translation answer for the proxy's one JavaScript
  callout, deliberately wrong (sets the response header to '1' instead of '2');
* ``fix/js-header-proxy.json``: the canned fix answer (CP8-T02's contract: a fix request's answer is
  looked up under a ``fix/`` folder, keyed by the proxy's name), correcting the header to '2';
* ``fix/js-header-proxy.2.json``: a second canned fix, which the first fix already fixing everything
  means is never read; its mere presence on disk is the trap (a loop that over-retries would consume it).

Nothing here records what the fake provider was asked directly (the production FakeProvider has no test
hook): the proof is in ``<out>/<bucket>/js-header-proxy/verification.json`` (exactly one recorded fix attempt that
helped, result type golden) together with run.log's one "sent to the AI" line for the translation, which
between them account for the exactly-two AI round trips the plan asks for.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .conftest import RuntimeTools

APP = "js-header-proxy"
XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
JS_SOURCE = "context.setVariable('response.header.X-Api-Version', '2');\n"
WRONG_MULE_STEP = (
    '<set-variable xmlns="http://www.mulesoft.org/schema/mule/core" '
    "variableName=\"responseHeaders\" "
    'value="#[vars.responseHeaders default {} ++ {\'X-Api-Version\': \'1\'}]"/>'
)


def _translation_answer(mule_xml: str) -> str:
    return json.dumps(
        {"status": "translated", "confidence": "high", "notes": "cp8-t18", "mule": mule_xml, "writes": None}
    )


def write_bundle(parent: Path) -> Path:
    """parent/js-header-proxy/apiproxy/...: one PostFlow response JavaScript step, JS-SetVersion."""
    root = parent / APP / "apiproxy"
    for sub in ("policies", "proxies", "targets", "resources/jsc"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    policy = (
        '<Javascript async="false" continueOnError="false" enabled="true" timeLimit="200" name="JS-SetVersion">\n'
        "    <DisplayName>JS-SetVersion</DisplayName>\n"
        "    <ResourceURL>jsc://set-version.js</ResourceURL>\n</Javascript>\n"
    )
    (root / f"{APP}.xml").write_text(
        XML_HEAD + f'<APIProxy revision="1" name="{APP}">\n    <DisplayName>{APP}</DisplayName>\n'
        "    <Policies><Policy>JS-SetVersion</Policy></Policies>\n"
        "    <ProxyEndpoints><ProxyEndpoint>default</ProxyEndpoint></ProxyEndpoints>\n"
        "    <TargetEndpoints><TargetEndpoint>default</TargetEndpoint></TargetEndpoints>\n</APIProxy>\n",
        encoding="utf-8",
    )
    (root / "policies" / "JS-SetVersion.xml").write_text(XML_HEAD + policy, encoding="utf-8")
    (root / "resources" / "jsc" / "set-version.js").write_text(JS_SOURCE, encoding="utf-8")
    (root / "proxies" / "default.xml").write_text(
        XML_HEAD + '<ProxyEndpoint name="default">\n'
        '    <PreFlow name="PreFlow"><Request/><Response/></PreFlow>\n    <Flows/>\n'
        '    <PostFlow name="PostFlow"><Request/><Response><Step><Name>JS-SetVersion</Name></Step></Response>'
        "</PostFlow>\n"
        "    <HTTPProxyConnection><BasePath>/js-header</BasePath><VirtualHost>default</VirtualHost>"
        "</HTTPProxyConnection>\n"
        '    <RouteRule name="default"><TargetEndpoint>default</TargetEndpoint></RouteRule>\n</ProxyEndpoint>\n',
        encoding="utf-8",
    )
    (root / "targets" / "default.xml").write_text(
        XML_HEAD + '<TargetEndpoint name="default">\n'
        '    <PreFlow name="PreFlow"><Request/><Response/></PreFlow>\n    <Flows/>\n'
        '    <PostFlow name="PostFlow"><Request/><Response/></PostFlow>\n'
        "    <HTTPTargetConnection><URL>http://backend.example/js-header</URL></HTTPTargetConnection>\n"
        "</TargetEndpoint>\n",
        encoding="utf-8",
    )
    return parent / APP


def write_golden(golden_root: Path) -> Path:
    folder = golden_root / APP
    folder.mkdir(parents=True)
    exchange = {
        "name": "version-header",
        "calls": [
            {
                "after_ms": 0,
                "request": {"method": "GET", "path": "/js-header", "headers": {}, "body": ""},
                "response": {
                    "status": 200,
                    "headers": {"X-Api-Version": "2", "Content-Type": "application/json"},
                    "body": '{"ok":true}',
                },
            }
        ],
        "backend_calls": [
            {
                "method": "GET",
                "path": "/js-header",
                "headers": {},
                "body": "",
                "response": {"status": 200, "headers": {"Content-Type": "application/json"}, "body": '{"ok":true}'},
            }
        ],
    }
    (folder / "version-header.json").write_text(json.dumps(exchange), encoding="utf-8")
    return golden_root


def proxy_dir(out_dir: Path) -> Path:
    """CP9 layout: <out>/<bucket>/<APP>/ in exactly one of verified, needs-review, unsupported."""
    homes = [out_dir / b / APP for b in ("verified", "needs-review", "unsupported") if (out_dir / b / APP).is_dir()]
    assert len(homes) == 1, f"{APP} must sit in exactly one of <out>/<bucket>/{APP}, found {homes}"
    return homes[0]


def generated_flow_text(out_dir: Path) -> str:
    return (proxy_dir(out_dir) / "mule-app" / "src" / "main" / "mule" / "proxy.xml").read_text(encoding="utf-8")


@pytest.mark.runtime
def test_CP8_T18_a_wrong_js_callout_translation_fails_and_the_ai_fix_corrects_it(
    runtime_tools: RuntimeTools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[CP8-T18] A deliberately wrong canned translation of a JavaScript callout fails its golden recording on
    the real runtime; the canned fix corrects it; the re-test passes; a second, unused fix stays queued."""
    from a2m.cli import main

    exports = tmp_path / "exports"
    exports.mkdir()
    write_bundle(exports)
    golden = tmp_path / "golden"
    write_golden(golden)
    out = tmp_path / "out"

    llm_dir = tmp_path / "llm"
    (llm_dir).mkdir()
    (llm_dir / "javascript.JS-SetVersion.json").write_text(
        _translation_answer(WRONG_MULE_STEP), encoding="utf-8"
    )
    fix_dir = llm_dir / "fix"
    fix_dir.mkdir()
    (fix_dir / f"{APP}.json").write_text(
        json.dumps(
            {
                "status": "fixed",
                "files": {"src/main/mule/proxy.xml": "__PLACEHOLDER__"},
                "notes": "cp8-t18 fix",
            }
        ),
        encoding="utf-8",
    )
    (fix_dir / f"{APP}.2.json").write_text(json.dumps({"status": "fixed", "files": {}}), encoding="utf-8")
    monkeypatch.setenv("A2M_FAKE_LLM_DIR", str(llm_dir))
    monkeypatch.setenv("A2M_MULE_HOME", str(runtime_tools.mule_home))

    # Build once with the wrong translation to learn exactly what text the fix must restore, since the fix
    # answer must be the REAL generated flow (an AI fix rewrites what the generator wrote, not a fixture a
    # test invented): generate the project the same way a2m's own generate stage would, read the wrong
    # text, and write the corrected text as the canned fix answer before the real run starts.
    from a2m.ai.fake import FakeProvider
    from a2m.generator import generate_project
    from a2m.parser import read_bundle

    probe_bundle = read_bundle(exports / APP)
    probe_dir = tmp_path / "probe" / APP / "mule-app"
    generate_project(probe_bundle, probe_dir, provider=FakeProvider(llm_dir))
    wrong_text = (probe_dir / "src" / "main" / "mule" / "proxy.xml").read_text(encoding="utf-8")
    assert "'1'" in wrong_text
    correct_text = wrong_text.replace("'1'", "'2'")
    (fix_dir / f"{APP}.json").write_text(
        json.dumps({"status": "fixed", "files": {"src/main/mule/proxy.xml": correct_text}, "notes": "cp8-t18 fix"}),
        encoding="utf-8",
    )

    code = main(
        [
            "migrate", str(exports), "--out", str(out), "--llm", "fake", "--mock-backends", "--golden", str(golden),
            "--max-fix-attempts", "3",
        ]
    )

    assert code == 0, (out / "run.log").read_text(encoding="utf-8", errors="replace")[-4000:]
    data = json.loads((proxy_dir(out) / "verification.json").read_text(encoding="utf-8"))
    assert data["type"] == "golden", data
    assert len(data["attempts"]) == 1, data["attempts"]
    attempt = data["attempts"][0]
    assert attempt["helped"] is True
    assert (attempt["failing_before"], attempt["failing_after"]) == (1, 0)
    assert attempt["changed_files"]

    run_log = (out / "run.log").read_text(encoding="utf-8", errors="replace")
    assert "sent to the AI" in run_log  # the one translation
    assert "Started app" in run_log or f"{APP}:" in run_log

    final_text = generated_flow_text(out)
    assert "'2'" in final_text
    assert "'1'" not in final_text

    # Clean teardown: the batch's private MULE_BASE under the results work area is gone.
    work_root = out / ".a2m-work"
    assert not any(work_root.rglob(".mule-base")) if work_root.exists() else True
