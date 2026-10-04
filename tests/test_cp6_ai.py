"""CP6: JavaScript, Python and Java callouts and untranslatable conditions go to an AI provider.

Public entry points used here (CP6 plan; nothing else from a2m is imported):

    a2m.parser.read_bundle(path) -> Bundle                                             (CP2)
    a2m.conditions.translate_condition(text) -> Translation                            (CP5)
    a2m.generator.generate_project(bundle, dest, *, shared_flows=(), results_root=None,
                                   provider=None) -> result
        provider   NEW in CP6: any object with ``complete(request) -> str``. With None
                   (the default) nothing is sent anywhere and CP5's behaviour is kept.
            request.name      str  the callout's step (= policy) name
            request.original  str  the callout's source code, or the condition text, verbatim
            request.prompt    str  the whole text that would be sent to the model
            The return value is the model's raw answer text. Any exception raised
            by ``complete`` is a provider error for that item only.
        result.policies    one record per step (CP4), now with:
            .method        "template", "ai" or "skipped"
            .confidence    "high", "medium", "low" or None
            .notes         str, the AI's notes (or why its answer could not be used)
            .needs_review  bool
            .original      str | None, the callout's source code
            .reason        str, as in CP4 (also why an AI item needs review)
        result.conditions  one record per non-empty condition (CP5), now with
            .method, .confidence, .notes, .needs_review as above; .original and .dw
            as in CP5 (.dw holds a2m's rendering of the AI's condition when the AI translated it)
        result.unsupported as in CP3/CP4

    The model's answer (what the prompts ask for, what the fake providers return) is
    one JSON object:
        {"status": "translated", "confidence": "high"|"medium"|"low", "notes": str,
         "mule": "<Mule 4 processor XML>"}          for a callout; the snippet may use
                                                    the standard Mule prefixes (ee:, ...)
                                                    without declaring them
        {"status": "translated", "confidence": ..., "notes": str, "dataweave": str}
                                                    for a condition (no #[ ] wrapper)
        {"status": "cannot_translate", "reason": str}
    Anything else (prose, a missing or unknown confidence, empty or not well-formed
    Mule code) is an answer that could not be used: needs review, never success.

    Prompt files: a2m/prompts/translate_javascript.md, translate_python.md,
    translate_java.md and translate_expression.md, read with importlib.resources.
    The environment variable A2M_PROMPTS_DIR points a2m at another folder of them.

    a2m.cli.main(argv) -> int                                                          (CP1)
        --llm claude (the default) needs ANTHROPIC_API_KEY (unset, empty or blank =
        missing) and the Anthropic SDK (the a2m[claude] extra); either missing stops
        the run before any proxy is processed, with one stderr line and exit code 2
        (usage error). The SDK is imported lazily: ``import a2m`` never imports it.
        The model name comes from the environment variable A2M_MODEL (a current
        Claude model when unset). The claude provider calls
        ``anthropic.Anthropic(api_key=...).messages.create(model=..., messages=..., ...)``.
        --llm fake uses a2m's own fake provider (canned answers, no network).
        Each AI item's result (step name, confidence, notes) is logged in run.log.

How a generated step is found: as in CP4, a step's processor (or the try scope
holding its processors) is labelled doc:name="<step name>"; a skipped or declined
step has no labelled element. A conditional flow's ``when`` is the one holding
its steps. Order is document order in proxy.xml.

Network guard (tests/conftest.py, autouse for the whole suite): outbound
connections and name lookups fail at once with an error naming the destination,
except to 127.0.0.1, ::1 and the name localhost.

Fixtures: bundles in tests/fixtures/apigee/cp6/, canned answers in
tests/fixtures/llm/. The real Anthropic SDK, the network and API keys are never used.
"""

from __future__ import annotations

import _socket
import http.server
import importlib.resources
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import types
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

TESTS = Path(__file__).resolve().parent
CP6 = TESTS / "fixtures" / "apigee" / "cp6"
LLM = TESTS / "fixtures" / "llm"
A2M_PACKAGE = TESTS.parent / "a2m"

CORE = "http://www.mulesoft.org/schema/mule/core"
HTTP = "http://www.mulesoft.org/schema/mule/http"
EE = "http://www.mulesoft.org/schema/mule/ee/core"
DOC = "http://www.mulesoft.org/schema/mule/documentation"
DOC_NAME = f"{{{DOC}}}name"

ODD = 'request.header.User-Agent =| "curl"'
GET_ORDERS = 'request.verb = "GET" AND proxy.pathsuffix MatchesPath "/orders"'
DECLINE_REASON = "uses Apigee crypto API with no Mule equivalent"
PROMPT_FILES = {
    "javascript": "translate_javascript.md",
    "python": "translate_python.md",
    "java": "translate_java.md",
    "expression": "translate_expression.md",
}
TRUE_ISH = {"", "true", "(true)", "#[true]", "#[(true)]", "#[]"}
UNUSABLE = re.compile(
    r"could ?n[o']t be used|cannot be used|unusable|not usable|invalid|malformed|not well-formed"
    r"|could ?n[o']t (?:be )?(?:parse|parsed|read|understood)|unparseable|unreadable",
    re.IGNORECASE,
)
SECRET_KEY = "sk-ant-test-SECRET123"
TEST_NET = "192.0.2.1"  # RFC 5737 documentation range: never routed


def source_of(bundle: str, rel: str) -> str:
    return (CP6 / bundle / "apiproxy" / "resources" / rel).read_text(encoding="utf-8")


JS_SOURCE = source_of("js-callout", "jsc/add-correlation.js")
PY_SOURCE = source_of("py-callout", "py/mask_card.py")
SIGN_JAVA = source_of("java-src-callout", "java/Sign.java")
TRICKY_JS = "var t = '{request.header.x}' + `${a}` + '{{b}}' + '%s %(name)s';\n"


# ---------------------------------------------------------------- canned answers and the recording fake


def canned(name: str) -> str:
    return (LLM / name).read_text(encoding="utf-8")


def canned_json(name: str) -> dict[str, Any]:
    data = json.loads(canned(name))
    assert isinstance(data, dict)
    return data


def with_changes(name: str, **changes: Any) -> str:
    data = canned_json(name)
    data.update(changes)
    return json.dumps(data)


class FakeLLM:
    """A provider that answers from canned text, keyed by condition text or step name, and records every request."""

    def __init__(self, answers: dict[str, str] | None = None, errors: dict[str, BaseException] | None = None) -> None:
        self.answers = dict(answers or {})
        self.errors = dict(errors or {})
        self.requests: list[Any] = []

    def key(self, request: Any) -> str:
        original = str(request.original)
        return original if original in self.answers or original in self.errors else str(request.name)

    def complete(self, request: Any) -> str:
        self.requests.append(request)
        key = self.key(request)
        if key in self.errors:
            raise self.errors[key]
        return self.answers.get(key, canned("decline.json"))

    def asked(self) -> list[str]:
        return [self.key(r) for r in self.requests]

    def prompts(self, key: str) -> list[str]:
        return [str(r.prompt) for r in self.requests if self.key(r) == key]


DEFAULT_ANSWERS = {
    "JS-AddCorrelation": canned("javascript.JS-AddCorrelation.json"),
    "PY-MaskCard": canned("python.PY-MaskCard.json"),
    "JC-Sign": canned("java.JC-Sign.json"),
    "JS-One": canned("javascript.JS-One.json"),
    "JS-Two": canned("javascript.JS-Two.json"),
    "PY-One": canned("python.PY-One.json"),
    ODD: canned("expression.curl-clients.json"),
}


def fake(**overrides: str) -> FakeLLM:
    answers = dict(DEFAULT_ANSWERS)
    answers.update(overrides)
    return FakeLLM(answers)


# ---------------------------------------------------------------- a2m entry points


def read_bundle(path: Path) -> Any:
    from a2m.parser import read_bundle as _read_bundle

    return _read_bundle(path)


class Migration:
    def __init__(self, result: Any, dest: Path) -> None:
        self.result = result
        self.dest = dest
        self.proxy_xml = dest / "src" / "main" / "mule" / "proxy.xml"
        self.root = ET.parse(self.proxy_xml).getroot()
        self.elements = list(self.root.iter())


def migrate(bundle_dir: Path, out: Path, provider: Any) -> Migration:
    from a2m.generator import generate_project

    dest = out / bundle_dir.name / "mule-app"
    result = generate_project(read_bundle(bundle_dir), dest, shared_flows=(), results_root=out, provider=provider)
    return Migration(result, dest)


def copy_bundle(tmp_path: Path, name: str) -> Path:
    dest = tmp_path / "bundles" / name
    shutil.copytree(CP6 / name, dest)
    return dest


def step_record(result: Any, name: str) -> Any:
    found = [r for r in result.policies if str(r.name) == name]
    assert len(found) == 1, f"expected one record for step {name}, got {[str(r.name) for r in result.policies]}"
    return found[0]


def condition_record(result: Any, name: str) -> Any:
    found = [r for r in result.conditions if str(r.name) == name]
    assert len(found) == 1, f"expected one record for {name}, got {[str(r.name) for r in result.conditions]}"
    return found[0]


def why(record: Any) -> str:
    return f"{record.reason or ''} {record.notes or ''}"


def labelled(m: Migration, name: str) -> list[ET.Element]:
    return [el for el in m.elements if el.get(DOC_NAME) == name]


def index_where(m: Migration, what: str, pred: Callable[[ET.Element], bool]) -> int:
    for i, el in enumerate(m.elements):
        if pred(el):
            return i
    raise AssertionError(f"no element in proxy.xml for {what}:\n{m.proxy_xml.read_text(encoding='utf-8')}")


def index_of_step(m: Migration, name: str) -> int:
    return index_where(m, f"step {name}", lambda el: el.get(DOC_NAME) == name)


def local(el: ET.Element) -> str:
    return el.tag.rsplit("}", 1)[-1]


def when_holding(m: Migration, step: str) -> ET.Element:
    whens = [el for el in m.root.iter(f"{{{CORE}}}when") if any(child.get(DOC_NAME) == step for child in el.iter())]
    assert whens, f"no when holds step {step}:\n{m.proxy_xml.read_text(encoding='utf-8')}"
    return whens[-1]  # the innermost one


def unparen(text: str) -> str:
    """``text`` without whitespace at the ends and without parentheses that wrap all of it."""
    text = text.strip()
    while text.startswith("(") and text.endswith(")"):
        depth = 0
        for i, char in enumerate(text):
            depth += char == "("
            depth -= char == ")"
            if depth == 0 and i < len(text) - 1:
                return text
        text = text[1:-1].strip()
    return text


def expression_body(expression: str | None) -> str:
    assert expression is not None
    text = expression.strip()
    assert text.startswith("#[") and text.endswith("]"), expression
    return unparen(text[2:-1])


def snippet_text(mule: str, local_name: str) -> str:
    """The text of the first ``local_name`` element in a canned Mule snippet (standard prefixes declared here)."""
    root = ET.fromstring(f'<r xmlns="{CORE}" xmlns:ee="{EE}" xmlns:doc="{DOC}">{mule}</r>')
    found = [el for el in root.iter() if local(el) == local_name]
    assert found, mule
    return found[0].text or ""


def parse_all_xml(folder: Path) -> list[Path]:
    paths = sorted(folder.rglob("*.xml"))
    assert paths, f"no XML files under {folder}"
    for path in paths:
        ET.parse(path)
    return paths


# ---------------------------------------------------------------- CP6-T01 .. T04: translated callouts and conditions


def test_CP6_T01_javascript_callout_translated_by_ai_in_place(tmp_path: Path) -> None:
    """[CP6-T01] A JavaScript callout is translated by the AI and placed where it was in the flow."""
    llm = fake()

    m = migrate(CP6 / "js-callout", tmp_path / "out", llm)

    va = index_of_step(m, "VA-Key")
    corr = index_where(
        m,
        "the canned corr.id set-variable",
        lambda el: local(el) == "set-variable" and el.get("variableName") == "corr.id",
    )
    am = index_of_step(m, "AM-SetHeader")
    assert va < corr < am, (va, corr, am)
    assert m.elements[corr].get("value") == "#[%dw 2.0 output application/java --- uuid()]"
    assert len(labelled(m, "JS-AddCorrelation")) == 1

    rec = step_record(m.result, "JS-AddCorrelation")
    assert rec.method == "ai"
    assert rec.confidence == "high"
    assert rec.notes == "random() replaced by uuid()"
    assert rec.needs_review is False
    assert "JS-AddCorrelation" not in [str(u.name) for u in m.result.unsupported]
    assert llm.asked() == ["JS-AddCorrelation"]


def test_CP6_T02_python_callout_translated_with_confidence_and_notes(tmp_path: Path) -> None:
    """[CP6-T02] A Python callout is translated by the AI with its confidence and notes."""
    answer = canned_json("python.PY-MaskCard.json")
    expected_dw = snippet_text(answer["mule"], "set-payload")
    llm = fake()

    m = migrate(CP6 / "py-callout", tmp_path / "out", llm)

    target_call = index_where(m, "the target call", lambda el: el.tag == f"{{{HTTP}}}request")
    transform = index_where(
        m,
        "the canned ee:transform",
        lambda el: el.tag == f"{{{EE}}}set-payload" and "payload update" in (el.text or ""),
    )
    done = index_of_step(m, "AM-Done")
    assert target_call < transform < done, (target_call, transform, done)
    assert (m.elements[transform].text or "").strip() == expected_dw.strip()

    rec = step_record(m.result, "PY-MaskCard")
    assert rec.method == "ai"
    assert rec.confidence == "medium"
    assert rec.notes == answer["notes"]
    assert rec.needs_review is False
    assert llm.asked() == ["PY-MaskCard"]


def test_CP6_T03_untranslatable_condition_sent_to_ai_and_answer_used(tmp_path: Path) -> None:
    """[CP6-T03] A condition the translator cannot handle is sent to the AI and the AI's answer is used."""
    from a2m.conditions import translate_condition

    assert translate_condition(ODD).ok is False  # precondition: CP5 can't translate it
    cp5_get_orders = translate_condition(GET_ORDERS)
    assert cp5_get_orders.ok is True
    canned_dw = canned_json("expression.curl-clients.json")["dataweave"]
    llm = fake()

    m = migrate(CP6 / "odd-condition", tmp_path / "out", llm)

    assert expression_body(when_holding(m, "AM-Curl").get("expression")) == unparen(CURL_RENDERED) != unparen(canned_dw)
    rec = condition_record(m.result, "curl-clients")
    assert rec.method == "ai"
    assert rec.confidence == "high"
    assert rec.original == ODD
    assert unparen(str(rec.dw)) == unparen(CURL_RENDERED)
    assert rec.needs_review is False

    orders = condition_record(m.result, "get-orders")
    assert orders.method == "template"
    assert expression_body(when_holding(m, "AM-Orders").get("expression")) == unparen(str(cp5_get_orders.dw))
    assert [str(r.original) for r in llm.requests] == [ODD]


def test_CP6_T04_java_callout_goes_to_ai_only_with_java_source(tmp_path: Path) -> None:
    """[CP6-T04] Java callouts go to the AI only when the Java source is in the bundle."""
    llm = fake()

    with_source = migrate(CP6 / "java-src-callout", tmp_path / "out", llm)
    jar_only = migrate(CP6 / "java-jar-callout", tmp_path / "out", llm)

    sign = step_record(with_source.result, "JC-Sign")
    assert sign.method == "ai"
    assert sign.confidence == "low"
    prompts = llm.prompts("JC-Sign")
    assert len(prompts) == 1
    assert SIGN_JAVA in prompts[0]

    legacy = step_record(jar_only.result, "JC-Legacy")
    assert legacy.method == "skipped"
    reasons = [str(u.reason) for u in jar_only.result.unsupported if str(u.name) == "JC-Legacy"]
    assert len(reasons) == 1, jar_only.result.unsupported
    assert "java" in reasons[0].lower() and "source" in reasons[0].lower(), reasons[0]
    assert "JC-Legacy" not in llm.asked()
    assert llm.asked() == ["JC-Sign"]


# ---------------------------------------------------------------- CP6-T05 .. T08: review, declines, bad answers, errors


@pytest.mark.parametrize("confidence", ["low", "medium", "high"])
def test_CP6_T05_only_low_confidence_is_flagged_for_review(tmp_path: Path, confidence: str) -> None:
    """[CP6-T05] Low-confidence AI results are flagged for human review."""
    llm = fake(**{"JS-AddCorrelation": with_changes("javascript.JS-AddCorrelation.json", confidence=confidence)})

    m = migrate(CP6 / "js-callout", tmp_path / "out", llm)

    rec = step_record(m.result, "JS-AddCorrelation")
    assert rec.method == "ai"
    assert rec.confidence == confidence
    assert rec.notes == "random() replaced by uuid()"
    assert rec.needs_review is (confidence == "low")


def test_CP6_T06_declined_callout_flagged_for_review_and_nothing_invented(tmp_path: Path) -> None:
    """[CP6-T06] When the AI says it cannot translate a callout, it is flagged for review and nothing is invented."""
    llm = fake(**{"JS-AddCorrelation": canned("decline.json")})

    m = migrate(CP6 / "js-callout", tmp_path / "out", llm)

    rec = step_record(m.result, "JS-AddCorrelation")
    assert rec.needs_review is True
    assert DECLINE_REASON in why(rec)
    assert JS_SOURCE in str(rec.original)
    assert labelled(m, "JS-AddCorrelation") == []
    assert [el for el in m.elements if "uuid()" in " ".join(el.attrib.values()) + (el.text or "")] == []
    assert [str(r.name) for r in m.result.policies] == ["VA-Key", "JS-AddCorrelation", "AM-SetHeader"]


def test_CP6_T06_declined_condition_never_becomes_always_true(tmp_path: Path) -> None:
    """[CP6-T06] When the AI says it cannot translate a condition, it is flagged and never made always-true."""
    llm = fake(**{ODD: canned("decline.json")})

    m = migrate(CP6 / "odd-condition", tmp_path / "out", llm)

    rec = condition_record(m.result, "curl-clients")
    assert rec.needs_review is True
    assert DECLINE_REASON in why(rec)
    assert rec.original == ODD
    branch = when_holding(m, "AM-Curl")
    expression = branch.get("expression")
    assert expression is not None
    assert "".join(expression.split()) not in TRUE_ISH
    assert expression.strip() == "#[false]"
    assert [str(r.name) for r in m.result.conditions].count("curl-clients") == 1


@pytest.mark.parametrize(
    "answer_file",
    [
        "malformed.prose.txt",
        "malformed.no-confidence.json",
        "malformed.certain.json",
        "malformed.empty-mule.json",
        "malformed.bad-xml.json",
    ],
)
def test_CP6_T07_broken_ai_answers_need_review_never_success(tmp_path: Path, answer_file: str) -> None:
    """[CP6-T07] Broken AI answers are treated as needing review, never as success."""
    llm = fake(**{"JS-AddCorrelation": canned(answer_file)})

    m = migrate(CP6 / "js-callout", tmp_path / "out", llm)

    rec = step_record(m.result, "JS-AddCorrelation")
    assert rec.needs_review is True
    assert rec.confidence in ("low", None)
    assert UNUSABLE.search(why(rec)), why(rec)
    parse_all_xml(m.dest)


def test_CP6_T08_ai_error_on_one_item_does_not_stop_the_others(tmp_path: Path) -> None:
    """[CP6-T08] An AI error on one item does not stop the others."""
    llm = FakeLLM(dict(DEFAULT_ANSWERS), errors={"JS-One": TimeoutError("provider timeout after 30s")})

    m = migrate(CP6 / "mixed", tmp_path / "out", llm)

    one = step_record(m.result, "JS-One")
    assert one.needs_review is True
    assert "timeout" in why(one).lower(), why(one)
    for name, answer_file in (("JS-Two", "javascript.JS-Two.json"), ("PY-One", "python.PY-One.json")):
        rec = step_record(m.result, name)
        assert rec.method == "ai", name
        assert rec.confidence == canned_json(answer_file)["confidence"], name
        assert rec.notes == canned_json(answer_file)["notes"], name
    cond = condition_record(m.result, "curl-clients")
    assert cond.method == "ai"
    assert cond.confidence == "high"
    assert m.proxy_xml.is_file()
    parse_all_xml(m.dest)


# ---------------------------------------------------------------- CP6-T09 .. T12: prompts


PROMPT_CASES = {
    "javascript": ("js-callout", "JS-AddCorrelation", JS_SOURCE, "PreFlow", "request", ("VA-Key", "AM-SetHeader")),
    "python": ("py-callout", "PY-MaskCard", PY_SOURCE, "PostFlow", "response", ("AM-Done",)),
    "condition": ("odd-condition", ODD, ODD, "curl-clients", "request", ()),
}


def prompt_for(tmp_path: Path, case: str) -> str:
    bundle, key, _original, _flow, _side, _neighbours = PROMPT_CASES[case]
    llm = fake()
    migrate(CP6 / bundle, tmp_path / case, llm)
    prompts = llm.prompts(key)
    assert len(prompts) == 1, llm.asked()
    return prompts[0]


@pytest.mark.parametrize("case", sorted(PROMPT_CASES))
def test_CP6_T09_prompt_has_code_place_in_flow_and_mule_examples(tmp_path: Path, case: str) -> None:
    """[CP6-T09] Each prompt includes the original code, where it sits in the flow, and Mule 4 examples."""
    bundle, _key, original, flow, side, neighbours = PROMPT_CASES[case]

    prompt = prompt_for(tmp_path, case)

    assert original in prompt
    assert bundle in prompt
    assert "default" in prompt
    assert flow in prompt
    assert side in prompt.lower()
    for name in neighbours:
        assert name in prompt, name
    assert "%dw 2.0" in prompt or "ee:transform" in prompt


def test_CP6_T09_javascript_python_and_condition_prompts_differ(tmp_path: Path) -> None:
    """[CP6-T09] The JavaScript, Python and condition prompts differ from one another."""
    prompts = {case: prompt_for(tmp_path, case) for case in PROMPT_CASES}

    assert len(set(prompts.values())) == 3
    # Each prompt carries only its own item's code.
    assert PY_SOURCE not in prompts["javascript"] and ODD not in prompts["javascript"]
    assert JS_SOURCE not in prompts["python"] and ODD not in prompts["python"]
    assert JS_SOURCE not in prompts["condition"] and PY_SOURCE not in prompts["condition"]


def test_CP6_T10_code_with_braces_and_percent_signs_sent_exactly(tmp_path: Path) -> None:
    """[CP6-T10] Code with braces and percent signs is sent to the AI exactly as written."""
    bundle = copy_bundle(tmp_path, "js-callout")
    (bundle / "apiproxy" / "resources" / "jsc" / "add-correlation.js").write_text(TRICKY_JS, encoding="utf-8")
    llm = fake()

    migrate(bundle, tmp_path / "out", llm)

    prompts = llm.prompts("JS-AddCorrelation")
    assert len(prompts) == 1
    assert TRICKY_JS in prompts[0]
    assert "{{{{b}}}}" not in prompts[0] and "{{request.header.x}}" not in prompts[0]


def prompts_folder() -> Path:
    folder = Path(str(importlib.resources.files("a2m").joinpath("prompts")))
    assert folder.is_dir(), f"{folder} is missing"
    return folder


def test_CP6_T11_editing_a_prompt_file_changes_what_is_sent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[CP6-T11] Editing a prompt file changes what is sent, with no code change."""
    marker = "MARKER-CP6: prefer DataWeave over Groovy"
    edited = tmp_path / "my-prompts"
    shutil.copytree(prompts_folder(), edited)
    js_prompt = edited / PROMPT_FILES["javascript"]
    js_prompt.write_text(js_prompt.read_text(encoding="utf-8").rstrip("\n") + f"\n{marker}\n", encoding="utf-8")

    monkeypatch.delenv("A2M_PROMPTS_DIR", raising=False)
    default_llm = fake()
    migrate(CP6 / "js-callout", tmp_path / "default", default_llm)

    monkeypatch.setenv("A2M_PROMPTS_DIR", str(edited))
    edited_llm = fake()
    migrate(CP6 / "js-callout", tmp_path / "edited", edited_llm)

    (default_prompt,) = default_llm.prompts("JS-AddCorrelation")
    (edited_prompt,) = edited_llm.prompts("JS-AddCorrelation")
    assert marker in edited_prompt
    assert marker not in default_prompt


def test_CP6_T12_prompt_wording_lives_in_prompt_files(tmp_path: Path) -> None:
    """[CP6-T12] Prompt wording lives in files in a2m/prompts/, not in the Python code."""
    prompts = importlib.resources.files("a2m").joinpath("prompts")
    texts: dict[str, str] = {}
    for kind, name in PROMPT_FILES.items():
        resource = prompts.joinpath(name)
        assert resource.is_file(), f"a2m/prompts/{name} ({kind}) is missing"
        texts[name] = resource.read_text(encoding="utf-8")
        assert texts[name].strip(), f"a2m/prompts/{name} is empty"
        assert "—" not in texts[name], f"a2m/prompts/{name} contains an em dash"
    assert len(set(texts.values())) == len(texts), "two prompt files have the same text"

    sources = {
        path: path.read_text(encoding="utf-8")
        for path in sorted(A2M_PACKAGE.rglob("*.py"))
        if "__pycache__" not in path.parts
    }
    for name, text in texts.items():
        for line in text.splitlines():
            line = line.strip()
            if len(line) <= 40:
                continue
            holders = [str(path.relative_to(A2M_PACKAGE)) for path, src in sources.items() if line in src]
            assert holders == [], f"a line of {name} is also in {holders}: {line!r}"


# ---------------------------------------------------------------- CP6-T13 .. T15: what reaches the AI, escaping, determinism


def test_CP6_T13_only_custom_code_and_untranslatable_expressions_reach_the_ai(tmp_path: Path) -> None:
    """[CP6-T13] Only custom code and untranslatable expressions reach the AI, and every item gets one result."""
    llm = fake()

    m = migrate(CP6 / "mixed", tmp_path / "out", llm)

    assert sorted(llm.asked()) == sorted(["JS-One", "JS-Two", "PY-One", ODD])
    assert [str(r.name) for r in llm.requests if str(r.name) in ("SA-10ps", "AM-SetHeader")] == []
    assert step_record(m.result, "SA-10ps").method == "template"
    assert step_record(m.result, "AM-SetHeader").method == "template"
    assert sorted(str(r.name) for r in m.result.policies) == sorted(
        ["SA-10ps", "AM-SetHeader", "JS-One", "JS-Two", "PY-One"]
    )
    assert [str(r.name) for r in m.result.conditions] == ["curl-clients"]


def test_CP6_T14_ai_condition_text_is_escaped_in_the_xml(tmp_path: Path) -> None:
    """[CP6-T14] AI text is escaped when written into the Mule XML (a condition with < and &)."""
    hostile = canned_json("hostile.expression.json")["dataweave"]
    llm = fake(**{ODD: canned("hostile.expression.json")})

    m = migrate(CP6 / "odd-condition", tmp_path / "out", llm)

    parse_all_xml(m.dest)
    assert expression_body(when_holding(m, "AM-Curl").get("expression")) == unparen(HOSTILE_RENDERED) != unparen(hostile)
    assert [el for el in m.root.iter(f"{{{CORE}}}flow") if el.get("name") == "evil"] == []


def test_CP6_T14_ai_mule_code_that_closes_cdata_is_escaped(tmp_path: Path) -> None:
    """[CP6-T14] AI text is escaped when written into the Mule XML (DataWeave that closes a CDATA section)."""
    expected = '%dw 2.0 output text/plain --- "]]></ee:set-payload><flow name="evil"/>"'
    assert snippet_text(canned_json("hostile.cdata.json")["mule"], "set-payload") == expected
    llm = fake(**{"JS-AddCorrelation": canned("hostile.cdata.json")})

    m = migrate(CP6 / "js-callout", tmp_path / "out", llm)

    parse_all_xml(m.dest)
    payloads = [(el.text or "").strip() for el in m.root.iter(f"{{{EE}}}set-payload")]
    assert payloads == [expected]
    assert [el for el in m.root.iter() if local(el) == "flow" and el.get("name") == "evil"] == []


def test_CP6_T15_migrating_with_ai_steps_twice_gives_identical_files(tmp_path: Path) -> None:
    """[CP6-T15] Migrating with AI steps twice gives identical files."""
    first = migrate(CP6 / "mixed", tmp_path / "first", fake())
    second = migrate(CP6 / "mixed", tmp_path / "second", fake())

    def files(root: Path) -> dict[str, bytes]:
        return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}

    one, two = files(first.dest), files(second.dest)
    assert sorted(one) == sorted(two)
    assert one, "no files generated"
    for rel in one:
        assert one[rel] == two[rel], rel
    assert len(labelled(first, "JS-One")) == 1  # the AI steps are really in the files compared


# ---------------------------------------------------------------- network guard helpers


def attempt(action: Callable[[], object]) -> tuple[BaseException | None, float]:
    start = time.monotonic()
    try:
        action()
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # noqa: BLE001 (the guard picks its own error type)
        return exc, time.monotonic() - start
    return None, time.monotonic() - start


def _probe_guard() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(0.5)
        sock.connect((TEST_NET, 443))
    finally:
        sock.close()


@pytest.fixture
def network_guard() -> Callable[[], None]:
    """``network_guard()`` fails the test (before it can reach the internet) unless the suite's network guard
    refuses a connect to a non-routable address with its error naming the destination."""

    def check() -> None:
        exc, _ = attempt(_probe_guard)
        assert exc is not None and TEST_NET in str(exc), (
            f"the suite's network guard is not active: connecting to {TEST_NET}:443 gave {exc!r}, "
            "not the guard's error naming the destination"
        )

    return check


@pytest.fixture
def dns_lookups(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Records every real name lookup (numeric addresses are passed through, they need no DNS)."""
    real = _socket.getaddrinfo
    seen: list[str] = []

    def recording(host: Any, port: Any, family: int = 0, type: int = 0, proto: int = 0, flags: int = 0) -> Any:
        text = host.decode() if isinstance(host, bytes) else str(host)
        try:
            ipaddress.ip_address(text)
        except ValueError:
            seen.append(text)
            raise socket.gaierror(socket.EAI_NONAME, f"a2m test: a real DNS lookup of {text} was attempted") from None
        return real(host, port, family, type, proto, flags | socket.AI_NUMERICHOST)

    monkeypatch.setattr(_socket, "getaddrinfo", recording)
    return seen


@pytest.fixture
def socket_calls(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Records every socket connect and name lookup made through the socket module (passed on unchanged)."""
    calls: list[object] = []
    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo

    def connect(self: socket.socket, address: Any) -> None:
        calls.append(("connect", address))
        real_connect(self, address)

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(("getaddrinfo", host))
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return calls


# ---------------------------------------------------------------- CLI helpers and the fake anthropic module


def fake_anthropic(answer_text: str) -> tuple[types.ModuleType, types.SimpleNamespace]:
    """A stand-in for the anthropic package: records the client's key and every messages.create call."""
    calls = types.SimpleNamespace(clients=[], creates=[])
    module = types.ModuleType("anthropic")

    class APIError(Exception):
        pass

    class APIConnectionError(APIError):
        pass

    class APITimeoutError(APIConnectionError):
        pass

    class APIStatusError(APIError):
        pass

    class RateLimitError(APIStatusError):
        pass

    class AuthenticationError(APIStatusError):
        pass

    class Messages:
        def create(self, **kwargs: Any) -> Any:
            calls.creates.append(kwargs)
            return types.SimpleNamespace(
                id="msg_cp6_test",
                type="message",
                role="assistant",
                model=kwargs.get("model"),
                content=[types.SimpleNamespace(type="text", text=answer_text)],
                stop_reason="end_turn",
                usage=types.SimpleNamespace(input_tokens=100, output_tokens=50),
            )

    class Anthropic:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            calls.clients.append({"args": args, **kwargs})
            self.messages = Messages()

    for cls in (APIError, APIConnectionError, APITimeoutError, APIStatusError, RateLimitError, AuthenticationError):
        setattr(module, cls.__name__, cls)
    module.Anthropic = Anthropic  # type: ignore[attr-defined]
    module.__version__ = "0.0.0-cp6-test"  # type: ignore[attr-defined]
    return module, calls


def message_text(kwargs: dict[str, Any]) -> str:
    """All text in a messages.create call: the system prompt and every message's content."""
    parts: list[str] = []

    def add(content: Any) -> None:
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    parts.append(str(block.get("text", "")))
                else:
                    parts.append(str(getattr(block, "text", "")))

    add(kwargs.get("system"))
    for message in kwargs.get("messages") or []:
        add(message.get("content") if isinstance(message, dict) else getattr(message, "content", None))
    return "\n".join(parts)


def exports_with(tmp_path: Path, *names: str) -> Path:
    exports = tmp_path / "in"
    for name in names:
        shutil.copytree(CP6 / name, exports / name)
    return exports


def assert_nothing_processed(results: Path) -> None:
    if not results.exists():
        return
    names = {".done", "mule-app", "verified", "needs-review", "unsupported", "js-callout", "py-callout"}
    found = sorted(str(p.relative_to(results)) for p in results.rglob("*") if p.name in names)
    assert found == [], found


def stderr_lines(err: str) -> list[str]:
    return [line for line in err.splitlines() if line.strip()]


def mule_xml_of(results: Path, proxy: str) -> list[ET.Element]:
    apps = sorted(p for p in results.rglob("mule-app") if p.is_dir() and p.parent.name == proxy)
    assert len(apps) == 1, apps
    roots = [ET.parse(path).getroot() for path in parse_all_xml(apps[0] / "src" / "main" / "mule")]
    return [el for root in roots for el in root.iter()]


# ---------------------------------------------------------------- CP6-T16, T22, T23: the network guard


def test_CP6_T16_raw_socket_to_anthropic_is_blocked(network_guard: Callable[[], None]) -> None:
    """[CP6-T16] The test suite blocks real network calls: a raw socket to api.anthropic.com fails at once."""
    network_guard()

    def connect() -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(2)
            sock.connect(("api.anthropic.com", 443))
        finally:
            sock.close()

    exc, elapsed = attempt(connect)

    assert exc is not None, "the connection was let through"
    assert "api.anthropic.com" in str(exc), repr(exc)
    assert elapsed < 1.0, elapsed


def test_CP6_T16_migrate_with_fake_llm_needs_no_key_sdk_or_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_cli: Any, network_guard: Callable[[], None]
) -> None:
    """[CP6-T16] a2m migrate --llm fake runs with no key, no Anthropic SDK and the guard on."""
    network_guard()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setitem(sys.modules, "anthropic", None)
    exports = exports_with(tmp_path, "js-callout")
    results = tmp_path / "results"

    res = run_cli(["migrate", str(exports), "--out", str(results), "--llm", "fake", "--no-runtime"])

    assert res.code == 0, res.err
    assert len([el for el in mule_xml_of(results, "js-callout") if el.get(DOC_NAME) == "JS-AddCorrelation"]) == 1


IMPORT_PROBE = r"""
import importlib.abc, json, sys
attempts = []
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "anthropic" or name.startswith("anthropic."):
            attempts.append(name)
            raise ModuleNotFoundError("No module named 'anthropic' (blocked by the CP6 test)")
        return None
sys.meta_path.insert(0, Block())
import a2m, a2m.ai, a2m.cli, a2m.engine, a2m.generator
from a2m.cli import main
code = main(sys.argv[1:])
print(json.dumps({"code": code, "attempts": attempts}))
"""


def test_CP6_T16_importing_a2m_never_imports_anthropic(tmp_path: Path, subprocess_env: dict[str, str]) -> None:
    """[CP6-T16] import a2m (and a --llm fake run) never tries to import the Anthropic SDK."""
    exports = exports_with(tmp_path, "js-callout")
    env = {k: v for k, v in subprocess_env.items() if k not in ("FORCE_COLOR", "PY_COLORS", "PYTHONPATH")}
    argv = ["migrate", str(exports), "--out", str(tmp_path / "results"), "--llm", "fake", "--no-runtime"]

    proc = subprocess.run(
        [sys.executable, "-c", IMPORT_PROBE, *argv],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    assert report == {"code": 0, "attempts": []}, (report, proc.stderr)


class _EchoServer:
    def __init__(self, family: socket.AddressFamily, host: str) -> None:
        self.sock = socket.socket(family, socket.SOCK_STREAM)
        self.sock.bind((host, 0))
        self.sock.listen(8)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with conn:
                conn.settimeout(2)
                try:
                    conn.sendall(conn.recv(64))
                except OSError:
                    pass

    def close(self) -> None:
        self.stop.set()
        self.thread.join(timeout=5)
        self.sock.close()


def _ipv6_loopback() -> bool:
    if not socket.has_ipv6:
        return False
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
            sock.bind(("::1", 0))
    except OSError:
        return False
    return True


def echo(host: str, port: int) -> bytes:
    with socket.create_connection((host, port), timeout=2) as sock:
        sock.sendall(b"ping")
        return sock.recv(64)


@pytest.mark.parametrize(
    ("address", "family"),
    [
        pytest.param("127.0.0.1", socket.AF_INET, id="ipv4"),
        pytest.param(
            "::1",
            socket.AF_INET6,
            id="ipv6",
            marks=pytest.mark.skipif(not _ipv6_loopback(), reason="this machine has no IPv6 loopback (::1)"),
        ),
    ],
)
def test_CP6_T22_guard_lets_tests_reach_a_loopback_echo_server(
    address: str, family: socket.AddressFamily, network_guard: Callable[[], None]
) -> None:
    """[CP6-T22] The network guard lets tests talk to servers on this machine (plain sockets)."""
    network_guard()
    server = _EchoServer(family, address)
    try:
        assert echo(address, server.port) == b"ping"
        resolved = {info[4][0] for info in socket.getaddrinfo("localhost", server.port, type=socket.SOCK_STREAM)}
        if address in resolved:
            assert echo("localhost", server.port) == b"ping"
    finally:
        server.close()


class _Quiet(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


@pytest.fixture
def http_server() -> Iterator[int]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Quiet)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_CP6_T22_guard_lets_urllib_fetch_from_a_loopback_http_server(
    http_server: int, network_guard: Callable[[], None]
) -> None:
    """[CP6-T22] The network guard lets tests fetch from an http.server on 127.0.0.1, by address and as localhost."""
    network_guard()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    assert socket.getaddrinfo("localhost", http_server, type=socket.SOCK_STREAM)
    for host in ("127.0.0.1", "localhost"):
        with opener.open(f"http://{host}:{http_server}/", timeout=5) as response:
            assert response.status == 200, host
            assert response.read() == b"ok", host


@pytest.mark.parametrize("how", ["create_connection", "getaddrinfo"])
@pytest.mark.parametrize("host", [TEST_NET, "127.0.0.1.example.test", "api.anthropic.com"])
def test_CP6_T23_addresses_that_only_look_local_are_blocked(
    host: str, how: str, network_guard: Callable[[], None], dns_lookups: list[str]
) -> None:
    """[CP6-T23] Addresses that only look local are still blocked, through create_connection and getaddrinfo."""
    network_guard()

    def reach() -> None:
        if how == "create_connection":
            socket.create_connection((host, 443), timeout=2).close()
        else:
            socket.getaddrinfo(host, 443)

    exc, elapsed = attempt(reach)

    assert exc is not None, f"{how} to {host} was let through"
    assert host in str(exc), repr(exc)
    assert "a real DNS lookup" not in str(exc), repr(exc)
    assert dns_lookups == []
    assert elapsed < 1.0, elapsed


# ---------------------------------------------------------------- CP6-T17 .. T21: the claude provider from the CLI


def test_CP6_T17_claude_without_api_key_stops_at_the_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_cli: Any
) -> None:
    """[CP6-T17] Choosing Claude without an API key stops at the start with a clear message."""
    module, calls = fake_anthropic(canned("javascript.JS-AddCorrelation.json"))
    monkeypatch.setitem(sys.modules, "anthropic", module)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    exports = exports_with(tmp_path, "js-callout", "py-callout")
    results = tmp_path / "results"

    res = run_cli(["migrate", str(exports), "--out", str(results), "--llm", "claude"])

    assert res.code == 2, (res.code, res.err)
    lines = stderr_lines(res.err)
    assert len(lines) == 1, res.err
    assert "ANTHROPIC_API_KEY" in lines[0]
    assert "--llm fake" in lines[0]
    assert "Traceback" not in res.err + res.out
    assert_nothing_processed(results)
    assert calls.clients == [] and calls.creates == []


@pytest.mark.parametrize("key", [None, "", "   "], ids=["unset", "empty", "blank"])
def test_CP6_T18_claude_is_default_and_blank_key_counts_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_cli: Any, key: str | None
) -> None:
    """[CP6-T18] Claude is the default, and an empty or blank key counts as missing."""
    module, calls = fake_anthropic(canned("javascript.JS-AddCorrelation.json"))
    monkeypatch.setitem(sys.modules, "anthropic", module)
    if key is None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    else:
        monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    exports = exports_with(tmp_path, "js-callout", "py-callout")
    results = tmp_path / "results"

    res = run_cli(["migrate", str(exports), "--out", str(results)])

    assert res.code == 2, (res.code, res.err)
    lines = stderr_lines(res.err)
    assert len(lines) == 1, res.err
    assert "ANTHROPIC_API_KEY" in lines[0]
    assert "--llm fake" in lines[0]
    assert "Traceback" not in res.err + res.out
    assert_nothing_processed(results)
    assert calls.clients == [] and calls.creates == []


def test_CP6_T19_claude_without_the_sdk_stops_with_an_install_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_cli: Any
) -> None:
    """[CP6-T19] Choosing Claude when the Anthropic SDK is not installed stops with an install hint."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET_KEY)
    monkeypatch.setitem(sys.modules, "anthropic", None)
    exports = exports_with(tmp_path, "js-callout", "py-callout")
    results = tmp_path / "results"

    res = run_cli(["migrate", str(exports), "--out", str(results), "--llm", "claude"])

    assert res.code == 2, (res.code, res.err)
    lines = stderr_lines(res.err)
    assert len(lines) == 1, res.err
    assert "anthropic" in lines[0].lower()
    assert "a2m[claude]" in lines[0]
    assert "Traceback" not in res.err + res.out
    assert "SECRET123" not in res.err + res.out
    assert_nothing_processed(results)


def run_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_cli: Any, answer: str
) -> tuple[Any, types.SimpleNamespace, Path]:
    module, calls = fake_anthropic(answer)
    monkeypatch.setitem(sys.modules, "anthropic", module)
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET_KEY)
    monkeypatch.setenv("A2M_MODEL", "claude-test-model")
    monkeypatch.delenv("A2M_PROMPTS_DIR", raising=False)
    exports = exports_with(tmp_path, "js-callout")
    results = tmp_path / "results"
    res = run_cli(["migrate", str(exports), "--out", str(results), "--llm", "claude", "--no-runtime"])
    return res, calls, results


def test_CP6_T20_claude_provider_sends_prompt_and_reads_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_cli: Any,
    network_guard: Callable[[], None],
    socket_calls: list[object],
) -> None:
    """[CP6-T20] The Claude provider sends the prompt and reads the answer, with no network."""
    network_guard()
    socket_calls.clear()  # only what the migration itself does counts
    js_wording = max(
        (
            line.strip()
            for line in (prompts_folder() / PROMPT_FILES["javascript"]).read_text(encoding="utf-8").splitlines()
            if "{" not in line and "$" not in line
        ),
        key=len,
    )
    assert len(js_wording) > 20, js_wording

    res, calls, results = run_claude(tmp_path, monkeypatch, run_cli, canned("javascript.JS-AddCorrelation.json"))

    assert res.code == 0, res.err
    assert [c.get("api_key") for c in calls.clients] == [SECRET_KEY]
    assert len(calls.creates) == 1, calls.creates
    sent = calls.creates[0]
    assert sent.get("model") == "claude-test-model"
    text = message_text(sent)
    assert JS_SOURCE in text
    assert js_wording in text
    elements = mule_xml_of(results, "js-callout")
    assert len([el for el in elements if el.get(DOC_NAME) == "JS-AddCorrelation"]) == 1
    assert [el.get("value") for el in elements if el.get("variableName") == "corr.id"] == [
        "#[%dw 2.0 output application/java --- uuid()]"
    ]
    log_lines = (results / "run.log").read_text(encoding="utf-8").splitlines()
    assert any("JS-AddCorrelation" in line and "random() replaced by uuid()" in line for line in log_lines), log_lines
    assert any("JS-AddCorrelation" in line and "high" in line for line in log_lines), log_lines
    assert socket_calls == []


def test_CP6_T21_api_key_never_appears_in_logs_or_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_cli: Any, network_guard: Callable[[], None]
) -> None:
    """[CP6-T21] The API key never appears in logs or output."""
    network_guard()
    long_notes = "random() replaced by uuid(); " + (
        "the correlation id format changes from c-<hex> to a UUID, so log searches by prefix need updating. " * 4
    )
    answer = with_changes("javascript.JS-AddCorrelation.json", notes=long_notes)

    res, calls, results = run_claude(tmp_path, monkeypatch, run_cli, answer)

    assert res.code == 0, res.err
    assert len(calls.creates) == 1
    log = (results / "run.log").read_text(encoding="utf-8")
    assert "random() replaced by uuid()" in log
    files = [p for p in results.rglob("*") if p.is_file()]
    assert files
    leaked = [str(p.relative_to(results)) for p in files if b"SECRET123" in p.read_bytes()]
    assert leaked == []
    assert "SECRET123" not in res.out
    assert "SECRET123" not in res.err
    assert os.environ.get("ANTHROPIC_API_KEY") == SECRET_KEY  # the key really was available to the run


# ---------------------------------------------------------------- CP6-T24 .. T29: Transform Message needs Mule Enterprise
#
# Decision (Rahil, option B): an AI answer's ee:transform (Transform Message) is kept as written. ee: elements exist
# only in Mule Enterprise, so an app holding one is flagged: the EE namespace and mule-ee.xsd are declared,
# GenerateResult.requires_enterprise is True and names the components and steps, mule-artifact.json requires
# MULE_EE and run.log carries a warning. Apps without ee: elements stay as they were (requiredProduct MULE).

EE_SCHEMA = "http://www.mulesoft.org/schema/mule/ee/core/current/mule-ee.xsd"
XSI_LOCATION = "{http://www.w3.org/2001/XMLSchema-instance}schemaLocation"
EE_MSG = "<ee:transform><ee:message>{}</ee:message></ee:transform>"
ENTERPRISE = re.compile(r"enterprise|\bEE\b", re.IGNORECASE)


def callout_answer(mule: str, confidence: str = "high", notes: str = "uses Transform Message") -> str:
    return json.dumps({"status": "translated", "confidence": confidence, "notes": notes, "mule": mule})


def schema_locations(m: Migration) -> dict[str, str]:
    parts = (m.root.get(XSI_LOCATION) or "").split()
    assert len(parts) % 2 == 0, parts
    return dict(zip(parts[::2], parts[1::2], strict=True))


def required_product(m: Migration) -> str:
    artifact = json.loads((m.dest / "mule-artifact.json").read_text(encoding="utf-8"))
    assert isinstance(artifact, dict)
    return str(artifact["requiredProduct"])


def ee_tags(m: Migration) -> list[str]:
    """Every ee: element in every generated XML file, as ``ee:<name>``."""
    return [
        f"ee:{local(el)}"
        for path in parse_all_xml(m.dest)
        for el in ET.parse(path).getroot().iter()
        if el.tag.startswith(f"{{{EE}}}")
    ]


def assert_requires_enterprise(m: Migration, steps: tuple[str, ...]) -> None:
    assert m.result.requires_enterprise is True
    assert tuple(m.result.enterprise_components) == ("ee:transform",)
    assert tuple(m.result.enterprise_steps) == steps
    assert schema_locations(m).get(EE) == EE_SCHEMA
    assert required_product(m) == "MULE_EE"


def assert_runs_on_ce(m: Migration) -> None:
    assert m.result.requires_enterprise is False
    assert tuple(m.result.enterprise_components) == ()
    assert tuple(m.result.enterprise_steps) == ()
    assert ee_tags(m) == []
    for path in parse_all_xml(m.dest):
        assert "mule-ee.xsd" not in path.read_text(encoding="utf-8"), path
    assert EE not in schema_locations(m)
    assert required_product(m) == "MULE"


def transform_parts(transform: ET.Element) -> list[tuple[str, str | None, str]]:
    """(part/script, variableName, script text) for each script of an ee:transform, in document order."""
    assert transform.tag == f"{{{EE}}}transform", transform.tag
    return [
        (f"{local(section)}/{local(script)}", script.get("variableName"), script.text or "")
        for section in transform
        for script in section
    ]


def test_CP6_T24_python_ee_transform_answer_is_kept_and_needs_enterprise(tmp_path: Path) -> None:
    """[CP6-T24] The canned PY-MaskCard ee:transform is written as an ee:transform with the same DataWeave."""
    answer = canned_json("python.PY-MaskCard.json")
    expected_dw = snippet_text(answer["mule"], "set-payload")

    m = migrate(CP6 / "py-callout", tmp_path / "out", fake())

    (transform,) = labelled(m, "PY-MaskCard")
    assert transform_parts(transform) == [("message/set-payload", None, expected_dw)]
    assert [el for el in m.elements if el.tag == f"{{{CORE}}}set-payload"] == []
    target_call = index_where(m, "the target call", lambda el: el.tag == f"{{{HTTP}}}request")
    assert target_call < index_of_step(m, "PY-MaskCard") < index_of_step(m, "AM-Done")
    assert_requires_enterprise(m, ("PY-MaskCard",))
    assert ee_tags(m) == ["ee:transform", "ee:message", "ee:set-payload"]
    rec = step_record(m.result, "PY-MaskCard")
    assert (rec.method, rec.confidence, rec.needs_review) == ("ai", "medium", False)
    assert rec.notes == answer["notes"]


def test_CP6_T24_app_without_ee_elements_is_unchanged(tmp_path: Path) -> None:
    """[CP6-T24] A core-only AI answer leaves the app runnable on Mule Kernel CE: no EE flag, schema or product."""
    m = migrate(CP6 / "js-callout", tmp_path / "out", fake())

    (step,) = labelled(m, "JS-AddCorrelation")
    assert [(el.tag, el.get("variableName")) for el in step if local(el) != "error-handler"] == [
        (f"{{{CORE}}}set-variable", "corr.id"),
        (f"{{{CORE}}}set-variable", "a2mRequestHeaders"),
    ]
    assert_runs_on_ce(m)
    rec = step_record(m.result, "JS-AddCorrelation")
    assert (rec.method, rec.confidence, rec.needs_review) == ("ai", "high", False)


def migrate_with_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_cli: Any, name: str) -> tuple[Any, str]:
    monkeypatch.setenv("A2M_FAKE_LLM_DIR", str(LLM))
    monkeypatch.delenv("A2M_PROMPTS_DIR", raising=False)
    results = tmp_path / "results"
    res = run_cli(
        ["migrate", str(exports_with(tmp_path, name)), "--out", str(results), "--llm", "fake", "--no-runtime"]
    )
    return res, (results / "run.log").read_text(encoding="utf-8")


def test_CP6_T24_run_log_warns_that_the_app_needs_mule_enterprise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_cli: Any
) -> None:
    """[CP6-T24] run.log carries a warning naming the Enterprise component and the step that holds it."""
    res, log = migrate_with_cli(tmp_path, monkeypatch, run_cli, "py-callout")

    assert res.code == 0, res.err
    warnings = [
        line
        for line in log.splitlines()
        if "WARNING" in line and "ee:transform" in line and "PY-MaskCard" in line and ENTERPRISE.search(line)
    ]
    assert len(warnings) == 1, log


def test_CP6_T24_run_log_has_no_enterprise_warning_for_a_core_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_cli: Any
) -> None:
    """[CP6-T24] A core-only app gets no Enterprise warning in run.log."""
    res, log = migrate_with_cli(tmp_path, monkeypatch, run_cli, "js-callout")

    assert res.code == 0, res.err
    assert "JS-AddCorrelation" in log
    assert [line for line in log.splitlines() if ENTERPRISE.search(line)] == []


def test_CP6_T25_hostile_dataweave_in_a_kept_transform_stays_text(tmp_path: Path) -> None:
    """[CP6-T25] DataWeave that closes a CDATA section stays exactly that text inside the ee:set-payload."""
    expected = '%dw 2.0 output text/plain --- "]]></ee:set-payload><flow name="evil"/>"'
    llm = fake(**{"JS-AddCorrelation": canned("hostile.cdata.json")})

    m = migrate(CP6 / "js-callout", tmp_path / "out", llm)

    (transform,) = labelled(m, "JS-AddCorrelation")
    assert transform_parts(transform) == [("message/set-payload", None, expected)]
    assert [el for el in m.root.iter() if local(el) == "flow" and el.get("name") == "evil"] == []
    assert [local(el) for el in m.root.iter() if local(el) == "flow"] == ["flow"]
    assert_requires_enterprise(m, ("JS-AddCorrelation",))


def test_CP6_T26_multi_script_transform_is_kept_with_every_script_in_order(tmp_path: Path) -> None:
    """[CP6-T26] A transform setting the payload and variables (one reading vars) is kept whole, scripts in order."""
    mule = (
        "<ee:transform><ee:message><ee:set-payload>%dw 2.0\noutput application/json\n---\n{wrapped: payload}"
        "</ee:set-payload></ee:message><ee:variables>"
        '<ee:set-variable variableName="corr.id">uuid()</ee:set-variable>'
        '<ee:set-variable variableName="count">(vars.count default 0) + 1</ee:set-variable>'
        "</ee:variables></ee:transform>"
    )
    llm = fake(**{"JS-AddCorrelation": callout_answer(mule)})

    m = migrate(CP6 / "js-callout", tmp_path / "out", llm)

    (transform,) = labelled(m, "JS-AddCorrelation")
    assert transform_parts(transform) == [
        ("message/set-payload", None, "%dw 2.0\noutput application/json\n---\n{wrapped: payload}"),
        ("variables/set-variable", "corr.id", "uuid()"),
        ("variables/set-variable", "count", "(vars.count default 0) + 1"),
    ]
    assert index_of_step(m, "VA-Key") < index_of_step(m, "JS-AddCorrelation") < index_of_step(m, "AM-SetHeader")
    assert_requires_enterprise(m, ("JS-AddCorrelation",))
    rec = step_record(m.result, "JS-AddCorrelation")
    assert (rec.method, rec.confidence, rec.needs_review) == ("ai", "high", False)


def first_value_read(read: str) -> str:
    """``read`` (a Mule header entry) in a2m's first-value form: Apigee's first comma-separated value, trimmed.
    A guard reading a header raw is refused (CP6-T60); one already in this form is used."""
    return f"(if ({read} == null) null else trim(({read} splitBy ',')[0] default ''))"


def test_CP6_T27_a_transform_inside_a_choice_is_kept_in_that_branch(tmp_path: Path) -> None:
    """[CP6-T27] An ee:transform nested in a choice stays an ee:transform at the same place in that branch."""
    mule = (
        '<choice><when expression="#[isEmpty(' + first_value_read("attributes.headers['x-correlation-id']") + ')]">'
        '<ee:transform><ee:variables><ee:set-variable variableName="corr.id">uuid()</ee:set-variable>'
        '</ee:variables></ee:transform><logger level="INFO" message="#[vars.\'corr.id\']"/>'
        "</when></choice>"
    )
    llm = fake(**{"JS-AddCorrelation": callout_answer(mule)})

    m = migrate(CP6 / "js-callout", tmp_path / "out", llm)

    (choice,) = labelled(m, "JS-AddCorrelation")
    assert local(choice) == "choice"
    (when,) = list(choice)
    transform, logger = list(when)
    assert transform_parts(transform) == [("variables/set-variable", "corr.id", "uuid()")]
    assert (logger.tag, logger.get("message")) == (f"{{{CORE}}}logger", "#[vars.'corr.id']")
    assert_requires_enterprise(m, ("JS-AddCorrelation",))


@pytest.mark.parametrize(
    "mule",
    [
        pytest.param(EE_MSG.format('<ee:set-payload resource="dw/mask.dwl"/>'), id="script-in-a-file"),
        pytest.param(EE_MSG.format("<ee:set-payload>   </ee:set-payload>"), id="empty-script"),
        pytest.param("<ee:transform/>", id="empty-transform"),
        pytest.param(
            EE_MSG.format("<ee:set-payload>payload</ee:set-payload><ee:set-payload>vars.x</ee:set-payload>"),
            id="payload-twice",
        ),
        pytest.param('<ee:cache><logger level="INFO" message="x"/></ee:cache>', id="foreign-ee-cache"),
        pytest.param(
            EE_MSG.format("<ee:set-payload>payload</ee:set-payload>") + "<ee:cache/>", id="foreign-ee-beside-transform"
        ),
        pytest.param(EE_MSG.format("<ee:set-payload>payload</ee:set-payload>stray"), id="stray-text-in-message"),
        pytest.param(EE_MSG.format("<ee:set-payload>payload</ee:set-payload>") + " stray", id="stray-text-outside"),
        pytest.param(
            "<ee:transform><ee:set-payload>payload</ee:set-payload></ee:transform>", id="script-not-in-message"
        ),
        pytest.param(
            "<ee:transform><ee:variables><ee:set-payload>payload</ee:set-payload></ee:variables></ee:transform>",
            id="payload-inside-variables",
        ),
        pytest.param(
            "<ee:message><ee:set-payload>payload</ee:set-payload></ee:message>", id="message-without-transform"
        ),
        pytest.param(
            EE_MSG.format("<ee:set-payload>payload</ee:set-payload>").replace("<ee:message>", ""), id="not-well-formed"
        ),
    ],
)
def test_CP6_T28_ee_code_a2m_cannot_keep_needs_review(tmp_path: Path, mule: str) -> None:
    """[CP6-T28] Malformed or foreign ee: code is an unusable answer: review, nothing written, app stays CE."""
    llm = fake(**{"JS-AddCorrelation": callout_answer(mule)})

    m = migrate(CP6 / "js-callout", tmp_path / "out", llm)

    rec = step_record(m.result, "JS-AddCorrelation")
    assert rec.needs_review is True
    assert rec.confidence in ("low", None)
    assert UNUSABLE.search(why(rec)), why(rec)
    assert labelled(m, "JS-AddCorrelation") == []
    assert_runs_on_ce(m)
    assert [str(r.name) for r in m.result.policies] == ["VA-Key", "JS-AddCorrelation", "AM-SetHeader"]


@pytest.mark.parametrize(
    ("mule", "parts"),
    [
        pytest.param(
            EE_MSG.format("<ee:set-attributes>{status: 200}</ee:set-attributes>"),
            [("message/set-attributes", None, "{status: 200}")],
            id="set-attributes",
        ),
        pytest.param(
            "<ee:transform><ee:message><ee:set-payload>payload</ee:set-payload>"
            "<ee:set-attributes>{status: 201}</ee:set-attributes></ee:message><ee:variables>"
            '<ee:set-variable variableName="a">1</ee:set-variable>'
            '<ee:set-variable variableName="b">vars.a + 1</ee:set-variable>'
            "</ee:variables></ee:transform>",
            [
                ("message/set-payload", None, "payload"),
                ("message/set-attributes", None, "{status: 201}"),
                ("variables/set-variable", "a", "1"),
                ("variables/set-variable", "b", "vars.a + 1"),
            ],
            id="multi-script",
        ),
    ],
)
def test_CP6_T28_valid_transform_forms_are_accepted(
    tmp_path: Path, mule: str, parts: list[tuple[str, str | None, str]]
) -> None:
    """[CP6-T28] set-attributes and multi-script Transform Messages are usable answers, kept as written."""
    llm = fake(**{"JS-AddCorrelation": callout_answer(mule)})

    m = migrate(CP6 / "js-callout", tmp_path / "out", llm)

    rec = step_record(m.result, "JS-AddCorrelation")
    assert (rec.method, rec.confidence, rec.needs_review) == ("ai", "high", False)
    (transform,) = labelled(m, "JS-AddCorrelation")
    assert transform_parts(transform) == parts
    assert_requires_enterprise(m, ("JS-AddCorrelation",))


@pytest.mark.parametrize("case", ["javascript", "python"])
def test_CP6_T29_callout_prompts_show_transform_message_examples(tmp_path: Path, case: str) -> None:
    """[CP6-T29] The callout prompts sent to the AI show Transform Message (ee:transform) examples."""
    prompt = prompt_for(tmp_path, case)

    assert "%dw 2.0" in prompt
    assert "<ee:transform>" in prompt
    assert "</ee:transform>" in prompt


def test_CP6_T29_java_prompt_shows_transform_message_examples(tmp_path: Path) -> None:
    """[CP6-T29] The Java callout prompt sent to the AI shows a Transform Message example too."""
    llm = fake()
    migrate(CP6 / "java-src-callout", tmp_path / "out", llm)

    (prompt,) = llm.prompts("JC-Sign")
    assert "%dw 2.0" in prompt
    assert "<ee:transform>" in prompt
    assert "</ee:transform>" in prompt


# ---------------------------------------------------------------- CP6-T30 .. T37: adversarial round 1 (CP6 fixes)
#
# Orchestrator decisions for round 1: one redaction for every string a2m writes (T30); an AI condition reads only
# what a2m's own translator would read faithfully there and is never a constant (T31, T32); conditions a2m knows no
# translation of could be faithful are not sent to the AI, and callouts that may read stale values are flagged (T33 ..
# T35); whether a step runs is decided by the guard actually generated (T36); an AI-translated step is a faithful
# writer only of what it declared and its Mule code writes (T37).


def add_policy(bundle: Path, name: str, xml: str) -> None:
    (bundle / "apiproxy" / "policies" / f"{name}.xml").write_text(xml, encoding="utf-8")


def add_step(bundle: Path, where: str, name: str, condition: str | None = None, index: int | None = None) -> None:
    """Add step ``name`` to the element at ``where`` (e.g. ``PreFlow/Request``) of the bundle's ProxyEndpoint."""
    path = bundle / "apiproxy" / "proxies" / "default.xml"
    tree = ET.parse(path)
    holder = tree.getroot().find(where)
    assert holder is not None, where
    step = ET.Element("Step")
    ET.SubElement(step, "Name").text = name
    if condition is not None:
        ET.SubElement(step, "Condition").text = condition
    holder.insert(len(holder) if index is None else index, step)
    tree.write(path, encoding="UTF-8", xml_declaration=True)


def set_flow_condition(bundle: Path, flow: str, condition: str) -> None:
    path = bundle / "apiproxy" / "proxies" / "default.xml"
    tree = ET.parse(path)
    found = [f for f in tree.getroot().iter("Flow") if f.get("name") == flow]
    assert len(found) == 1, flow
    element = found[0].find("Condition")
    assert element is not None
    element.text = condition
    tree.write(path, encoding="UTF-8", xml_declaration=True)


def assign_header_policy(name: str, header: str, value: str = "gold") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<AssignMessage enabled="true" name="{name}"><Set><Headers><Header name="{header}">{value}</Header>'
        '</Headers></Set><AssignTo createNew="false" transport="http" type="request"/></AssignMessage>\n'
    )


def expression_answer(dataweave: str, confidence: str = "high") -> str:
    return json.dumps({"status": "translated", "confidence": confidence, "notes": "cp6 round 1", "dataweave": dataweave})


def raising_anthropic(make_error: Callable[[types.ModuleType], BaseException]) -> types.ModuleType:
    """A stand-in for the anthropic package whose messages.create raises ``make_error(module)``."""
    module = types.ModuleType("anthropic")

    class APIError(Exception):
        pass

    class AuthenticationError(APIError):
        pass

    class Messages:
        def create(self, **kwargs: Any) -> Any:
            raise make_error(module)

    class Anthropic:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.messages = Messages()

    module.APIError = APIError  # type: ignore[attr-defined]
    module.AuthenticationError = AuthenticationError  # type: ignore[attr-defined]
    module.Anthropic = Anthropic  # type: ignore[attr-defined]
    return module


class _KeyInStr(Exception):
    def __str__(self) -> str:
        return f"request failed; headers={{'x-api-key': '{SECRET_KEY}'}}; retry with key {SECRET_KEY}"


SDK_ERRORS: dict[str, tuple[str, Callable[[types.ModuleType], BaseException]]] = {
    "sdk-api-error": ("AuthenticationError", lambda m: m.AuthenticationError(f"invalid x-api-key: {SECRET_KEY}")),
    "runtime-error": ("RuntimeError", lambda m: RuntimeError(f"connection reset; Authorization: Bearer {SECRET_KEY}")),
    "key-error": ("KeyError", lambda m: KeyError(SECRET_KEY)),
    "custom-str": ("_KeyInStr", lambda m: _KeyInStr()),
    "key-at-the-cut": ("ValueError", lambda m: ValueError("x" * 285 + SECRET_KEY + " and more text after it")),
}


def assert_no_key(text: str, where: str) -> None:
    assert SECRET_KEY not in text, where
    assert "SECRET123" not in text, where
    assert SECRET_KEY[:8] not in text, where  # no cut-off prefix of the key either


@pytest.mark.parametrize("case", sorted(SDK_ERRORS))
def test_CP6_T30_api_key_never_leaks_from_any_provider_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_cli: Any, network_guard: Callable[[], None], case: str
) -> None:
    """[CP6-T30] Whatever exception the SDK raises with the key in it, the key is masked in run.log, files and output."""
    network_guard()
    type_name, make_error = SDK_ERRORS[case]
    monkeypatch.setitem(sys.modules, "anthropic", raising_anthropic(make_error))
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET_KEY)
    monkeypatch.delenv("A2M_PROMPTS_DIR", raising=False)
    results = tmp_path / "results"

    res = run_cli(
        ["migrate", str(exports_with(tmp_path, "js-callout")), "--out", str(results), "--llm", "claude", "--no-runtime"]
    )

    assert res.code == 0, res.err
    log = (results / "run.log").read_text(encoding="utf-8")
    assert any("JS-AddCorrelation" in line and type_name in line for line in log.splitlines()), log
    assert_no_key(log, "run.log")
    for path in (p for p in results.rglob("*") if p.is_file()):
        assert_no_key(path.read_text(encoding="utf-8", errors="replace"), str(path))
    assert_no_key(res.out, "stdout")
    assert_no_key(res.err, "stderr")

    from a2m.ai import make_provider

    provider = make_provider("claude", {"ANTHROPIC_API_KEY": SECRET_KEY})
    m = migrate(CP6 / "js-callout", tmp_path / "direct", provider)
    rec = step_record(m.result, "JS-AddCorrelation")
    assert rec.needs_review is True
    assert type_name in why(rec), why(rec)
    assert_no_key(why(rec), "the step's result record")


DISGUISED_OR_FOREIGN = {
    "true-or-read": "true or vars.x",
    "self-comparison": "vars.x == vars.x",
    "self-comparison-of-a-header": "attributes.headers['user-agent'] == attributes.headers['user-agent']",
    "test-or-its-negation": "isEmpty(vars.x) or not isEmpty(vars.x)",
    "constant-operand": '"curl" == "curl" or vars.x == "a"',
    "default-true": "vars.x default true",
    "status-is-not-tracked": "vars.httpStatus >= 500",
    "built-in-as-flow-variable": "vars['verifyapikey.VA-Key.apiproduct.name'] != \"gold\"",
    "raw-path-suffix": 'attributes.maskedRequestPath == "/orders"',
    "unknown-function": "now() != null",
    "whole-header-map": "isEmpty(attributes.headers)",
    "lambda": "(attributes.headers pluck (v, k) -> k) contains \"user-agent\"",
    "string-interpolation": 'attributes.method == "$(vars.x)"',
}


@pytest.mark.parametrize("case", sorted(DISGUISED_OR_FOREIGN))
def test_CP6_T31_disguised_constant_or_foreign_read_is_never_used(tmp_path: Path, case: str) -> None:
    """[CP6-T31] An AI condition that is a disguised constant or reads outside a2m's vocabulary stays #[false]."""
    llm = fake(**{ODD: expression_answer(DISGUISED_OR_FOREIGN[case])})

    m = migrate(CP6 / "odd-condition", tmp_path / "out", llm)

    rec = condition_record(m.result, "curl-clients")
    assert rec.method == "ai"
    assert rec.ok is False
    assert rec.needs_review is True
    assert UNUSABLE.search(why(rec)), why(rec)
    assert str(when_holding(m, "AM-Curl").get("expression")).strip() == "#[false]"


def test_CP6_T31_canonical_first_value_header_read_is_accepted(tmp_path: Path) -> None:
    """[CP6-T31] The first-value header accessor a2m itself emits is accepted in an AI condition."""
    first_value = (
        '(if (attributes.headers[\'user-agent\'] == null) null else trim((attributes.headers[\'user-agent\'] splitBy ",")'
        '[0] default ""))'
    )
    dataweave = f'({first_value} default "") startsWith "curl"'
    llm = fake(**{ODD: expression_answer(dataweave)})

    m = migrate(CP6 / "odd-condition", tmp_path / "out", llm)

    rec = condition_record(m.result, "curl-clients")
    assert (rec.method, rec.ok, rec.needs_review) == ("ai", True, False)
    assert expression_body(when_holding(m, "AM-Curl").get("expression")) == unparen(dataweave)


def test_CP6_T32_ai_condition_reading_a_value_an_earlier_step_changed_is_refused(tmp_path: Path) -> None:
    """[CP6-T32] An AI condition reading a header an earlier step changes (stale in the app) stays #[false]."""
    bundle = copy_bundle(tmp_path, "odd-condition")
    add_policy(bundle, "AM-Tier", assign_header_policy("AM-Tier", "X-Tier"))
    add_step(bundle, "PreFlow/Request", "AM-Tier")
    llm = fake(**{ODD: expression_answer("(attributes.headers['x-tier'] default \"\") startsWith \"gold\"")})

    m = migrate(bundle, tmp_path / "out", llm)

    assert llm.asked() == [ODD]  # User-Agent itself is not changed, so the condition is sent
    rec = condition_record(m.result, "curl-clients")
    assert (rec.method, rec.ok, rec.needs_review) == ("ai", False, True)
    assert "AM-Tier" in why(rec), why(rec)
    assert str(when_holding(m, "AM-Curl").get("expression")).strip() == "#[false]"


VERIFY_KEY_POLICY = (
    '<?xml version="1.0" encoding="UTF-8"?>\n<VerifyAPIKey enabled="true" name="VA-Key">'
    '<APIKey ref="request.queryparam.apikey"/></VerifyAPIKey>\n'
)


@pytest.mark.parametrize(
    ("condition", "setup"),
    [
        pytest.param('verifyapikey.VA-Key.apiproduct.name != "gold"', "verify-key", id="unmapped-built-in"),
        pytest.param('request.header.X-Tier =| "gold"', "assign-tier", id="stale-header"),
    ],
)
def test_CP6_T33_condition_with_unknown_built_in_or_stale_read_is_not_sent_to_the_ai(
    tmp_path: Path, condition: str, setup: str
) -> None:
    """[CP6-T33] A condition reading an unmapped built-in or a stale value is not sent; it keeps CP5's reason."""
    from a2m.conditions import translate_condition

    bundle = copy_bundle(tmp_path, "odd-condition")
    if setup == "verify-key":
        add_policy(bundle, "VA-Key", VERIFY_KEY_POLICY)
        add_step(bundle, "PreFlow/Request", "VA-Key")
    else:
        add_policy(bundle, "AM-Tier", assign_header_policy("AM-Tier", "X-Tier"))
        add_step(bundle, "PreFlow/Request", "AM-Tier")
    set_flow_condition(bundle, "curl-clients", condition)
    trap = "vars['verifyapikey.VA-Key.apiproduct.name'] != \"gold\" and attributes.headers['x-tier'] != \"x\""
    llm = fake(**{condition: expression_answer(trap)})

    m = migrate(bundle, tmp_path / "out", llm)

    assert condition not in llm.asked()
    rec = condition_record(m.result, "curl-clients")
    assert rec.method == "skipped"
    assert rec.ok is False
    cp5_reason = str(translate_condition(condition).reason if setup == "verify-key" else "AM-Tier")
    assert cp5_reason in str(rec.reason), rec.reason
    assert str(when_holding(m, "AM-Curl").get("expression")).strip() == "#[false]"


TIER_SCRIPT = "var tier = context.getVariable('request.header.X-Tier');\ncontext.setVariable('route.tier', tier);\n"
CORR_SCRIPT = "var c = context.getVariable('corr.seed');\ncontext.setVariable('corr.id', c);\n"
OBJECT_SCRIPT = "var tier = request.headers['X-Tier'];\ncontext.setVariable('route.tier', tier);\n"


@pytest.mark.parametrize(
    ("script", "flagged"),
    [
        pytest.param(TIER_SCRIPT, True, id="reads-the-changed-header"),
        pytest.param(OBJECT_SCRIPT, True, id="reads-through-the-request-object"),
        pytest.param(CORR_SCRIPT, False, id="reads-an-unchanged-variable"),
    ],
)
def test_CP6_T34_callout_reading_a_value_an_earlier_step_changed_needs_review(
    tmp_path: Path, script: str, flagged: bool
) -> None:
    """[CP6-T34] A callout whose inputs may be stale is translated but flagged, and its prompt names the changes."""
    bundle = copy_bundle(tmp_path, "js-callout")
    add_policy(bundle, "AM-Tier", assign_header_policy("AM-Tier", "X-Tier"))
    add_step(bundle, "PreFlow/Request", "AM-Tier", index=1)
    (bundle / "apiproxy" / "resources" / "jsc" / "add-correlation.js").write_text(script, encoding="utf-8")
    llm = fake()

    m = migrate(bundle, tmp_path / "out", llm)

    rec = step_record(m.result, "JS-AddCorrelation")
    assert (rec.method, rec.confidence) == ("ai", "high")
    assert len(labelled(m, "JS-AddCorrelation")) == 1
    assert rec.needs_review is flagged
    if flagged:
        assert "AM-Tier" in why(rec), why(rec)
    (prompt,) = llm.prompts("JS-AddCorrelation")
    assert "x-tier" in prompt.lower() and "AM-Tier" in prompt


def test_CP6_T35_callout_answer_reading_a_built_in_as_a_flow_variable_needs_review(tmp_path: Path) -> None:
    """[CP6-T35] AI Mule code reading an Apigee built-in variable as a flow variable (always null) is not used."""
    mule = (
        "<set-variable variableName=\"tier\" value=\"#[vars['verifyapikey.VA-Key.apiproduct.name'] default 'none']\"/>"
    )
    llm = fake(**{"JS-AddCorrelation": callout_answer(mule)})

    m = migrate(CP6 / "js-callout", tmp_path / "out", llm)

    rec = step_record(m.result, "JS-AddCorrelation")
    assert rec.needs_review is True
    assert UNUSABLE.search(why(rec)), why(rec)
    assert labelled(m, "JS-AddCorrelation") == []


EV_CLIENT = (
    '<?xml version="1.0" encoding="UTF-8"?>\n<ExtractVariables enabled="true" name="EV-Client">'
    "<Source>request</Source><VariablePrefix>c</VariablePrefix>"
    '<Header name="X-Tier"><Pattern>{tier}</Pattern></Header></ExtractVariables>\n'
)


def test_CP6_T36_reader_after_an_ai_guarded_writer_uses_the_generated_guard(tmp_path: Path) -> None:
    """[CP6-T36] A writer whose condition the AI translated runs in the app, so a later reader of its variable works."""
    bundle = copy_bundle(tmp_path, "odd-condition")
    add_policy(bundle, "EV-Client", EV_CLIENT)
    add_policy(bundle, "AM-Gold", assign_header_policy("AM-Gold", "X-Gold", "yes"))
    add_step(bundle, "PreFlow/Request", "EV-Client", ODD)
    add_step(bundle, "PostFlow/Request", "AM-Gold", 'c.tier = "gold"')
    llm = fake()

    m = migrate(bundle, tmp_path / "out", llm)

    writer_guard = expression_body(when_holding(m, "EV-Client").get("expression"))
    assert writer_guard == unparen(CURL_RENDERED)
    gold = condition_record(m.result, "AM-Gold")
    assert (gold.method, gold.ok) == ("template", True), why(gold)
    expression = str(when_holding(m, "AM-Gold").get("expression"))
    assert expression.strip() != "#[false]"
    assert "c.tier" in expression


WRITES = {
    "request_headers": ["x-correlation-id"],
    "query_params": [],
    "verb": False,
    "payload": False,
    "response_headers": [],
    "variables": ["corr.id"],
}
VERB_FLOW = '(proxy.pathsuffix MatchesPath "/orders") and (request.verb = "GET")'


@pytest.mark.parametrize(
    ("writes", "confidence", "faithful"),
    [
        pytest.param(WRITES, "high", True, id="declared-and-matching"),
        pytest.param(None, "high", False, id="no-declaration"),
        pytest.param({**WRITES, "variables": ["other.id"]}, "high", False, id="variables-differ"),
        pytest.param({**WRITES, "verb": True}, "high", False, id="declares-a-verb-change"),
        pytest.param({**WRITES, "request_headers": []}, "high", False, id="header-write-not-declared"),
        pytest.param(WRITES, "low", False, id="low-confidence"),
    ],
)
def test_CP6_T37_ai_step_is_a_faithful_writer_only_of_its_checked_declaration(
    tmp_path: Path, writes: dict[str, Any] | None, confidence: str, faithful: bool
) -> None:
    """[CP6-T37] An AI-translated step with a matching writes declaration does not make a later verb check dead."""
    bundle = copy_bundle(tmp_path, "js-callout")
    shutil.copy(CP6 / "odd-condition" / "apiproxy" / "policies" / "AM-Orders.xml", bundle / "apiproxy" / "policies")
    path = bundle / "apiproxy" / "proxies" / "default.xml"
    tree = ET.parse(path)
    flows = tree.getroot().find("Flows")
    assert flows is not None
    flow = ET.SubElement(flows, "Flow", {"name": "get-orders"})
    request = ET.SubElement(flow, "Request")
    ET.SubElement(ET.SubElement(request, "Step"), "Name").text = "AM-Orders"
    ET.SubElement(flow, "Response")
    ET.SubElement(flow, "Condition").text = VERB_FLOW
    tree.write(path, encoding="UTF-8", xml_declaration=True)
    answer = canned_json("javascript.JS-AddCorrelation.json")
    answer["confidence"] = confidence
    if writes is not None:
        answer["writes"] = writes
    llm = fake(**{"JS-AddCorrelation": json.dumps(answer)})

    m = migrate(bundle, tmp_path / "out", llm)

    assert step_record(m.result, "JS-AddCorrelation").method == "ai"
    orders = condition_record(m.result, "get-orders")
    expression = str(when_holding(m, "AM-Orders").get("expression")).strip()
    if faithful:
        assert (orders.method, orders.ok) == ("template", True), why(orders)
        assert expression != "#[false]" and "attributes.method" in expression
    else:
        assert orders.ok is False
        assert expression == "#[false]"
        assert "JS-AddCorrelation" in why(orders), why(orders)


# ---------------------------------------------------------------- CP6-T38 .. T43: adversarial round 2 (CP6 fixes)
#
# Orchestrator decisions for round 2: the AI answers a condition as a structured tree that a2m checks (CP5's reads,
# not a constant over a finite domain of values) and writes as DataWeave itself; the earlier DataWeave string is
# accepted only when it parses into the same tree (T38 .. T40). An AI step's writes are read by exact key and must
# equal its declaration (T41, T42). A callout's included scripts count as its inputs (T43).


def condition_answer(condition: Any, confidence: str = "high") -> str:
    return json.dumps({"status": "translated", "confidence": confidence, "notes": "cp6 round 2", "condition": condition})


def compare(variable: str, operator: str, value: Any) -> dict[str, Any]:
    return {"variable": variable, "operator": operator, "value": value}


def assert_condition_refused(m: Migration) -> None:
    rec = condition_record(m.result, "curl-clients")
    assert rec.method == "ai"
    assert rec.ok is False
    assert rec.needs_review is True
    assert UNUSABLE.search(why(rec)), why(rec)
    assert str(when_holding(m, "AM-Curl").get("expression")).strip() == "#[false]"


@pytest.mark.parametrize(
    ("condition", "reads"),
    [
        pytest.param(compare("request.header.User-Agent", "starts-with", "curl"), ["attributes.headers"], id="odd"),
        pytest.param(
            {"and": [compare("request.verb", "equals", "GET"), compare("request.header.User-Agent", "starts-with", "curl")]},
            ["attributes.method", "attributes.headers"],
            id="and-of-two",
        ),
    ],
)
def test_CP6_T38_structured_condition_is_written_by_a2m(tmp_path: Path, condition: Any, reads: list[str]) -> None:
    """[CP6-T38] A structured AI condition is checked and written as DataWeave by a2m, and used in the when."""
    llm = fake(**{ODD: condition_answer(condition)})

    m = migrate(CP6 / "odd-condition", tmp_path / "out", llm)

    rec = condition_record(m.result, "curl-clients")
    assert (rec.method, rec.ok, rec.needs_review, rec.confidence) == ("ai", True, False, "high")
    body = expression_body(when_holding(m, "AM-Curl").get("expression"))
    assert unparen(str(rec.dw)) == body
    assert 'startsWith "curl"' in body
    assert "user-agent" in body
    for read in reads:
        assert read in body, body
    parse_all_xml(m.dest)


def test_CP6_T38_low_confidence_structured_condition_is_not_used(tmp_path: Path) -> None:
    """[CP6-T38] A low-confidence structured condition is shown to the reviewer but the when stays #[false]."""
    llm = fake(**{ODD: condition_answer(compare("request.header.User-Agent", "starts-with", "curl"), "low")})

    m = migrate(CP6 / "odd-condition", tmp_path / "out", llm)

    rec = condition_record(m.result, "curl-clients")
    assert (rec.method, rec.ok, rec.needs_review) == ("ai", False, True)
    assert 'startsWith "curl"' in str(rec.notes)
    assert str(when_holding(m, "AM-Curl").get("expression")).strip() == "#[false]"


CONSTANT_ANSWERS: dict[str, Any] = {
    # The round-2 bypass repros (host-alt-correctness A1), as DataWeave answers.
    "dw-dot-vs-bracket-self-comparison": "vars['x'] == vars.x",
    "dw-header-dot-vs-bracket-self-comparison": "attributes.headers['x'] == attributes.headers.x",
    "dw-equal-or-not-equal": "vars.x == 'a' or vars.x != 'a'",
    "dw-header-equal-or-not-equal": "attributes.headers.x == 'a' or attributes.headers.x != 'a'",
    "dw-equal-or-negation-other-spelling": "vars.x == 'a' or not (vars['x'] == 'a')",
    "dw-equal-or-bracketed-negation": "vars.x == 'a' or (not (vars['x'] == 'a'))",
    "dw-header-case-variants": "attributes.headers['X-Tier'] == 'a' or attributes.headers['x-tier'] != 'a'",
    "dw-contradiction": "vars.x == 'a' and vars.x == 'b'",
    "dw-mixed-and-or": "vars.x == 'a' and vars.y == 'b' or vars.z == 'c'",
    "dw-outside-the-subset": "contains(attributes.headers.'user-agent', \"curl\")",
    # The same classes as structured answers.
    "equal-or-not-equal-case-variants": {
        "or": [compare("request.header.X-Tier", "equals", "a"), compare("request.header.x-tier", "not-equals", "a")]
    },
    "test-or-its-negation": {"or": [compare("x", "equals", "a"), {"not": compare("x", "equals", "a")}]},
    "pattern-or-its-negation": {"or": [compare("x", "matches", "a*"), {"not": compare("x", "matches", "a*")}]},
    "starts-with-empty-text": compare("request.header.User-Agent", "starts-with", ""),
    "verb-contradiction": {"and": [compare("request.verb", "equals", "GET"), compare("request.verb", "equals", "POST")]},
    "null-or-not-null": {"or": [compare("x", "equals", None), compare("x", "not-equals", None)]},
}


@pytest.mark.parametrize("case", sorted(CONSTANT_ANSWERS))
def test_CP6_T39_constant_condition_in_any_spelling_is_never_used(tmp_path: Path, case: str) -> None:
    """[CP6-T39] An AI condition that is constant over the values it reads, however it is spelled, stays #[false]."""
    answer = CONSTANT_ANSWERS[case]
    text = expression_answer(answer) if isinstance(answer, str) else condition_answer(answer)
    llm = fake(**{ODD: text})

    m = migrate(CP6 / "odd-condition", tmp_path / "out", llm)

    assert_condition_refused(m)


FOREIGN_ANSWERS: dict[str, Any] = {
    "unmapped-built-in": compare("verifyapikey.VA-Key.apiproduct.name", "equals", "gold"),
    "a2m-own-variable": compare("httpStatus", "equals", "500"),
    "status-code": compare("response.status.code", "equals", "500"),
    "body": compare("request.content", "equals", "x"),
    "response-header-on-request-side": compare("response.header.X-Cache", "equals", "HIT"),
    "boolean-value": compare("x", "equals", True),
    "number-with-equals": compare("x", "equals", 5),
    "ordering": compare("request.header.X-Count", "greater", 5),
    "unknown-operator": compare("x", "contains", "a"),
    "path-suffix-root-ambiguity": compare("proxy.pathsuffix", "starts-with", "/"),
    "unknown-node": {"xor": [compare("x", "equals", "a"), compare("y", "equals", "b")]},
    "single-part-or": {"or": [compare("x", "equals", "a")]},
}


@pytest.mark.parametrize("case", sorted(FOREIGN_ANSWERS))
def test_CP6_T40_structured_condition_a2m_cannot_read_faithfully_is_refused(tmp_path: Path, case: str) -> None:
    """[CP6-T40] A structured condition reading what CP5 would not read faithfully, or in a form a2m does not know,
    stays #[false]."""
    llm = fake(**{ODD: condition_answer(FOREIGN_ANSWERS[case])})

    m = migrate(CP6 / "odd-condition", tmp_path / "out", llm)

    assert_condition_refused(m)


def test_CP6_T40_answer_with_both_forms_is_refused(tmp_path: Path) -> None:
    """[CP6-T40] An answer holding both a structured condition and a DataWeave string is not used."""
    data = json.loads(condition_answer(compare("request.header.User-Agent", "starts-with", "curl")))
    data["dataweave"] = "startsWith(attributes.headers.'user-agent', \"curl\")"
    llm = fake(**{ODD: json.dumps(data)})

    m = migrate(CP6 / "odd-condition", tmp_path / "out", llm)

    assert_condition_refused(m)


def test_CP6_T40_structured_condition_reading_a_changed_header_is_refused(tmp_path: Path) -> None:
    """[CP6-T40] A structured condition reading a header an earlier step changes (stale in the app) stays #[false]."""
    bundle = copy_bundle(tmp_path, "odd-condition")
    add_policy(bundle, "AM-Tier", assign_header_policy("AM-Tier", "X-Tier"))
    add_step(bundle, "PreFlow/Request", "AM-Tier")
    llm = fake(**{ODD: condition_answer(compare("request.header.X-TIER", "starts-with", "gold"))})

    m = migrate(bundle, tmp_path / "out", llm)

    assert_condition_refused(m)
    assert "AM-Tier" in why(condition_record(m.result, "curl-clients"))


def add_flow(bundle: Path, flow_name: str, condition: str, step: str) -> None:
    """Add a conditional Flow ``flow_name`` running a new AssignMessage step ``step`` to the ProxyEndpoint."""
    add_policy(bundle, step, assign_header_policy(step, f"X-{step}", "yes"))
    path = bundle / "apiproxy" / "proxies" / "default.xml"
    tree = ET.parse(path)
    flows = tree.getroot().find("Flows")
    assert flows is not None
    flow = ET.SubElement(flows, "Flow", {"name": flow_name})
    ET.SubElement(ET.SubElement(ET.SubElement(flow, "Request"), "Step"), "Name").text = step
    ET.SubElement(flow, "Response")
    ET.SubElement(flow, "Condition").text = condition
    tree.write(path, encoding="UTF-8", xml_declaration=True)


def callout_with_writes(mule: str, writes: dict[str, Any]) -> str:
    return json.dumps({"status": "translated", "confidence": "high", "notes": "cp6 round 2", "mule": mule, "writes": writes})


HEADERS_BASE = "(vars.a2mRequestHeaders default attributes.headers)"
QUERY_BASE = "(vars.a2mRequestQuery default attributes.queryParams)"
SET_CORR = "<set-variable variableName=\"corr.id\" value=\"#[uuid()]\"/>"


def headers_write(expression: str) -> str:
    return f'<set-variable variableName="a2mRequestHeaders" value="#[{expression}]"/>'


def query_write(expression: str) -> str:
    return f'<set-variable variableName="a2mRequestQuery" value="#[{expression}]"/>'


def declared(**fields: Any) -> dict[str, Any]:
    return {**WRITES, **fields}


SET_ATTRIBUTES = (
    "<ee:transform><ee:message><ee:set-attributes><![CDATA[%dw 2.0\noutput application/java\n---\n"
    "attributes update { case h at .headers -> h ++ {'x-correlation-id': 'c'} }]]></ee:set-attributes></ee:message>"
    "</ee:transform>"
)
WRITE_MODEL_CASES = {
    "map-replaced": (headers_write("{'x-correlation-id': vars.'corr.id'}"), declared(), False),
    "substring-key-in-a-two-key-object": (
        headers_write(f"{HEADERS_BASE} ++ {{'id': 'x', 'clientid-secret': 'y'}}"),
        declared(request_headers=["id"]),
        False,
    ),
    "substring-key-in-two-writes": (
        headers_write(f"{HEADERS_BASE} ++ {{'id': 'x'}}") + headers_write(f"{HEADERS_BASE} ++ {{'clientid-secret': 'y'}}"),
        declared(request_headers=["id"]),
        False,
    ),
    "undeclared-extra-key": (
        headers_write(f"{HEADERS_BASE} ++ {{'x-correlation-id': 'c'}}") + headers_write(f"{HEADERS_BASE} ++ {{'x-extra': 'e'}}"),
        declared(),
        False,
    ),
    "both-keys-declared": (
        headers_write(f"{HEADERS_BASE} ++ {{'x-correlation-id': 'c'}}") + headers_write(f"{HEADERS_BASE} ++ {{'x-extra': 'e'}}"),
        declared(request_headers=["x-correlation-id", "X-Extra"]),
        True,
    ),
    "header-case-differs": (
        headers_write(f"{HEADERS_BASE} ++ {{'x-correlation-id': 'c'}}"),
        declared(request_headers=["X-Correlation-Id"]),
        True,
    ),
    "header-removed": (headers_write(f"{HEADERS_BASE} - 'x-correlation-id'"), declared(), True),
    "header-map-variable-removed": ('<remove-variable variableName="a2mRequestHeaders"/>', declared(), False),
    "attributes-set": (SET_ATTRIBUTES, declared(), False),
    "query-case-differs": (
        query_write(f"{QUERY_BASE} ++ {{'customerid': 'c'}}"),
        declared(request_headers=[], query_params=["CustomerId"]),
        False,
    ),
    "query-case-matches": (
        query_write(f"{QUERY_BASE} ++ {{'CustomerId': 'c'}}"),
        declared(request_headers=[], query_params=["CustomerId"]),
        True,
    ),
    "variable-case-differs": (
        headers_write(f"{HEADERS_BASE} ++ {{'x-correlation-id': 'c'}}"),
        declared(variables=["Corr.Id"]),
        False,
    ),
}


@pytest.mark.parametrize("case", sorted(WRITE_MODEL_CASES))
def test_CP6_T41_ai_step_write_model_is_read_by_exact_key(tmp_path: Path, case: str) -> None:
    """[CP6-T41] An AI step is a faithful writer only when its Mule code writes exactly the declared keys, each in a
    recognised single-key form; otherwise it may change anything (a later verb check stays #[false])."""
    writes_mule, writes, faithful = WRITE_MODEL_CASES[case]
    bundle = copy_bundle(tmp_path, "js-callout")
    add_flow(bundle, "get-orders", VERB_FLOW, "AM-Orders")
    llm = fake(**{"JS-AddCorrelation": callout_with_writes(SET_CORR + writes_mule, writes)})

    m = migrate(bundle, tmp_path / "out", llm)

    rec = step_record(m.result, "JS-AddCorrelation")
    assert (rec.method, rec.confidence) == ("ai", "high"), why(rec)
    orders = condition_record(m.result, "get-orders")
    expression = str(when_holding(m, "AM-Orders").get("expression")).strip()
    if faithful:
        assert (orders.method, orders.ok) == ("template", True), why(orders)
        assert expression != "#[false]" and "attributes.method" in expression
    else:
        assert orders.ok is False
        assert expression == "#[false]"
        assert "JS-AddCorrelation" in why(orders), why(orders)


@pytest.mark.parametrize(
    ("condition", "refused"),
    [
        pytest.param('request.queryparam.CustomerId = "42"', True, id="same-spelling"),
        pytest.param('request.queryparam.customerid = "42"', True, id="other-case-is-refused-too"),
        pytest.param('request.queryparam.OrderId = "42"', False, id="unchanged-parameter"),
    ],
)
def test_CP6_T42_mixed_case_query_parameter_changed_by_an_ai_step_is_never_read_stale(
    tmp_path: Path, condition: str, refused: bool
) -> None:
    """[CP6-T42] A later condition reading a mixed-case query parameter an AI step declared and writes is refused
    (stale in the app), whatever the case it is read in; an unchanged parameter is still translated."""
    bundle = copy_bundle(tmp_path, "js-callout")
    add_flow(bundle, "by-customer", condition, "AM-Customer")
    mule = SET_CORR + query_write(f"{QUERY_BASE} ++ {{'CustomerId': 'c'}}")
    llm = fake(**{"JS-AddCorrelation": callout_with_writes(mule, declared(request_headers=[], query_params=["CustomerId"]))})

    m = migrate(bundle, tmp_path / "out", llm)

    rec = condition_record(m.result, "by-customer")
    expression = str(when_holding(m, "AM-Customer").get("expression")).strip()
    if refused:
        assert rec.ok is False
        assert expression == "#[false]"
        assert "JS-AddCorrelation" in why(rec), why(rec)
    else:
        assert (rec.method, rec.ok) == ("template", True), why(rec)
        assert "OrderId" in expression


TIER_HELPER = "function tierOf() {\n  return context.getVariable('request.header.X-Tier');\n}\n"
OBJECT_HELPER = "function tierOf() {\n  return request.headers['X-Tier'];\n}\n"
SEED_HELPER = "function tierOf() {\n  return context.getVariable('corr.seed');\n}\n"
MAIN_CALLS_HELPER = "var t = tierOf();\ncontext.setVariable('corr.id', t);\n"
JS_WITH_INCLUDE = (
    '<?xml version="1.0" encoding="UTF-8"?>\n<Javascript enabled="true" timeLimit="200" name="JS-AddCorrelation">'
    "<IncludeURL>jsc://helper.js</IncludeURL><ResourceURL>jsc://add-correlation.js</ResourceURL></Javascript>\n"
)


@pytest.mark.parametrize(
    ("helper", "flagged"),
    [
        pytest.param(TIER_HELPER, True, id="helper-reads-the-changed-header"),
        pytest.param(OBJECT_HELPER, True, id="helper-reads-through-the-request-object"),
        pytest.param(SEED_HELPER, False, id="helper-reads-an-unchanged-variable"),
    ],
)
def test_CP6_T43_callout_whose_included_script_reads_a_changed_value_needs_review(
    tmp_path: Path, helper: str, flagged: bool
) -> None:
    """[CP6-T43] The reads of every IncludeURL script count as the callout's inputs: a helper reading a value an
    earlier step changed flags the translation for review."""
    bundle = copy_bundle(tmp_path, "js-callout")
    add_policy(bundle, "AM-Tier", assign_header_policy("AM-Tier", "X-Tier"))
    add_step(bundle, "PreFlow/Request", "AM-Tier", index=1)
    add_policy(bundle, "JS-AddCorrelation", JS_WITH_INCLUDE)
    scripts = bundle / "apiproxy" / "resources" / "jsc"
    (scripts / "add-correlation.js").write_text(MAIN_CALLS_HELPER, encoding="utf-8")
    (scripts / "helper.js").write_text(helper, encoding="utf-8")
    llm = fake()

    m = migrate(bundle, tmp_path / "out", llm)

    rec = step_record(m.result, "JS-AddCorrelation")
    assert (rec.method, rec.confidence) == ("ai", "high")
    (prompt,) = llm.prompts("JS-AddCorrelation")
    assert helper in prompt
    assert rec.needs_review is flagged
    if flagged:
        assert "AM-Tier" in why(rec), why(rec)


# ---------------------------------------------------------------- CP6 adversarial round 3
# a2m vouches for a callout's read set only when every use of the message API is a direct call with one plain name;
# anything else may read anything (T44). Both condition forms go through the same refusals (T45).

PY_TIER_POLICY = (
    '<?xml version="1.0" encoding="UTF-8"?>\n<Script enabled="true" name="PY-MaskCard">'
    "<ResourceURL>py://tier.py</ResourceURL></Script>\n"
)
UNVOUCHED_READS = {
    "js-method-reference": ("js", "var get = context.getVariable;\nvar t = get('request.header.X-Tier');\n"),
    "js-bracket-access": ("js", "var t = context['getVariable']('request.header.X-Tier');\n"),
    "js-template-literal": ("js", 'var t = `Tier ${context.getVariable("request.header.X-Tier")}`;\n'),
    "js-name-built-from-text": ("js", "var t = context.getVariable('request.header.' + 'X-Tier');\n"),
    "js-message-alias": ("js", "var t = context.getVariable('message.header.X-Tier');\n"),
    "js-global-object": ("js", "var c = this['con' + 'text'];\nvar t = c.getVariable('request.header.X-Tier');\n"),
    "js-eval": ("js", "var t = eval(\"context.getVariable('request.header.X-Tier')\");\n"),
    "py-method-reference": ("py", "get = flow.getVariable\ntier = get('request.header.X-Tier')\n"),
    "py-f-string": ("py", "tier = f\"Tier {flow.getVariable('request.header.X-Tier')}\"\n"),
    "py-getattr": ("py", "tier = getattr(flow, 'getVariable')('request.header.X-Tier')\n"),
    "py-request-object": ("py", "tier = request.headers['X-Tier']\n"),
    "py-non-ascii-name": ("py", "tier = \uff46low.getVariable('request.header.X-Tier')\n"),
    "js-escaped-name": ("js", "var t = \\u0063ontext.getVariable('request.header.X-Tier');\n"),
}
VOUCHED_READS = {
    "js-reads-the-changed-header": ("js", "var t = context.getVariable('request.header.X-Tier');\n", True),
    "js-reads-an-unchanged-variable": ("js", "var t = context.getVariable('corr.seed');\n", False),
    "py-reads-the-changed-header": ("py", "tier = flow.getVariable('request.header.X-Tier')\n", True),
    "py-reads-an-unchanged-variable": ("py", "seed = flow.getVariable('corr.seed')\nflow.setVariable('corr.id', seed)\n", False),
}


def callout_after_tier_change(tmp_path: Path, language: str, code: str) -> tuple[Migration, str]:
    """The js-callout bundle with AM-Tier changing X-Tier before a callout whose whole code is ``code``; returns the
    migration and the callout's step name."""
    bundle = copy_bundle(tmp_path, "js-callout")
    add_policy(bundle, "AM-Tier", assign_header_policy("AM-Tier", "X-Tier"))
    add_step(bundle, "PreFlow/Request", "AM-Tier", index=1)
    if language == "js":
        (bundle / "apiproxy" / "resources" / "jsc" / "add-correlation.js").write_text(
            code + "context.setVariable('corr.id', 'c');\n", encoding="utf-8"
        )
        name = "JS-AddCorrelation"
    else:
        add_policy(bundle, "PY-MaskCard", PY_TIER_POLICY)
        (bundle / "apiproxy" / "resources" / "py").mkdir()
        (bundle / "apiproxy" / "resources" / "py" / "tier.py").write_text(code, encoding="utf-8")
        add_step(bundle, "PreFlow/Request", "PY-MaskCard", index=2)
        name = "PY-MaskCard"
    return migrate(bundle, tmp_path / "out", fake()), name


@pytest.mark.parametrize("case", sorted(UNVOUCHED_READS))
def test_CP6_T44_callout_whose_reads_a2m_cannot_vouch_for_needs_review(tmp_path: Path, case: str) -> None:
    """[CP6-T44] A callout reading the message API any way but a direct call with one plain name (a method
    reference, bracket access, a template literal, an f-string, a built name, an alias, ...) may read anything: after
    an earlier change it needs review, naming that step."""
    language, code = UNVOUCHED_READS[case]

    m, name = callout_after_tier_change(tmp_path, language, code)

    rec = step_record(m.result, name)
    assert rec.method == "ai", why(rec)
    assert rec.confidence in ("high", "medium"), why(rec)
    assert rec.needs_review is True, why(rec)
    assert "AM-Tier" in why(rec), why(rec)


@pytest.mark.parametrize("case", sorted(VOUCHED_READS))
def test_CP6_T44_callout_with_direct_reads_is_judged_by_what_it_reads(tmp_path: Path, case: str) -> None:
    """[CP6-T44] Controls: direct reads with one plain name are known exactly, so only a read of the changed value
    flags the callout."""
    language, code, flagged = VOUCHED_READS[case]

    m, name = callout_after_tier_change(tmp_path, language, code)

    rec = step_record(m.result, name)
    assert rec.method == "ai", why(rec)
    assert rec.needs_review is flagged, why(rec)
    if flagged:
        assert "AM-Tier" in why(rec), why(rec)


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param("attributes.queryParams.count < 5", id="dataweave-query-ordering"),
        pytest.param("attributes.headers.'x-count' >= 5", id="dataweave-header-ordering"),
        pytest.param("vars.limit > 3", id="dataweave-variable-ordering"),
        pytest.param(compare("request.queryparam.count", "less", 5), id="structured-query-ordering"),
    ],
)
def test_CP6_T45_ordering_on_a_text_value_is_refused_in_either_condition_form(tmp_path: Path, answer: Any) -> None:
    """[CP6-T45] Both condition forms go through the same checks: an ordering comparison of a text value (a query
    parameter, a header, a flow variable) with a number is refused, whichever form the AI answered in."""
    text = expression_answer(answer) if isinstance(answer, str) else condition_answer(answer)
    llm = fake(**{ODD: text})

    m = migrate(CP6 / "odd-condition", tmp_path / "out", llm)

    assert_condition_refused(m)


# ---------------------------------------------------------------- CP6 adversarial round 4
# A callout whose inputs may be stale (it reads a value an earlier step changed, or a2m cannot tell what it reads) is
# not a faithful writer: its outputs may hold the caller's old value, so a later condition on them is refused (T46).

TIER_TO_VARIABLE = '<set-variable variableName="corr.id" value="#[attributes.headers.\'x-tier\']"/>'


@pytest.mark.parametrize(
    ("code", "tier_changed", "refused"),
    [
        pytest.param("var t = context.getVariable('request.header.X-Tier');\n", True, True, id="reads-the-changed-header"),
        pytest.param(
            "var get = context.getVariable;\nvar t = get('request.header.X-Tier');\n",
            True,
            True,
            id="cannot-tell-what-it-reads",
        ),
        pytest.param("var t = context.getVariable('request.header.X-Tier');\n", False, False, id="no-earlier-change"),
    ],
)
def test_CP6_T46_outputs_of_a_callout_with_stale_inputs_are_not_trusted(
    tmp_path: Path, code: str, tier_changed: bool, refused: bool
) -> None:
    """[CP6-T46] AssignMessage changes X-Tier and an AI-translated script copies it into a flow variable: the script
    needs review and is not a faithful writer, so a later condition on that variable stays #[false], naming it."""
    bundle = copy_bundle(tmp_path, "js-callout")
    if tier_changed:
        add_policy(bundle, "AM-Tier", assign_header_policy("AM-Tier", "X-Tier"))
        add_step(bundle, "PreFlow/Request", "AM-Tier", index=1)
    (bundle / "apiproxy" / "resources" / "jsc" / "add-correlation.js").write_text(
        code + "context.setVariable('corr.id', t);\n", encoding="utf-8"
    )
    add_flow(bundle, "gold-tier", 'corr.id = "gold"', "AM-Gold")
    llm = fake(**{"JS-AddCorrelation": callout_with_writes(TIER_TO_VARIABLE, declared(request_headers=[]))})

    m = migrate(bundle, tmp_path / "out", llm)

    rec = step_record(m.result, "JS-AddCorrelation")
    assert (rec.method, rec.confidence) == ("ai", "high"), why(rec)
    assert rec.needs_review is refused, why(rec)
    gold = condition_record(m.result, "gold-tier")
    expression = str(when_holding(m, "AM-Gold").get("expression")).strip()
    if refused:
        assert gold.ok is False
        assert expression == "#[false]"
        assert "JS-AddCorrelation" in why(gold), why(gold)
    else:
        assert (gold.method, gold.ok) == ("template", True), why(gold)
        assert expression != "#[false]" and "corr.id" in expression


# ---------------------------------------------------------------- CP6 adversarial round 4: the DataWeave form is written by a2m
# An AI condition in the earlier DataWeave form is checked as a tree and written by a2m, exactly as the structured form
# (T47): a header is read as its first comma-separated value, as Apigee reads it, never as the whole raw header.

CURL_RENDERED = (
    '((if (attributes.headers[\'user-agent\'] == null) null else trim((attributes.headers[\'user-agent\'] splitBy ",")'
    '[0] default "")) default "") startsWith "curl"'
)
HOSTILE_RENDERED = '(payload["total"] < 5) and (payload["name"] == "A&B")'


@pytest.mark.parametrize(
    ("dataweave", "structured"),
    [
        pytest.param(
            "attributes.headers['x-role'] == 'admin'",
            compare("request.header.X-Role", "equals", "admin"),
            id="raw-header-read-is-its-first-value",
        ),
        pytest.param(
            "attributes.headers.'x-role' != \"admin\"",
            compare("request.header.X-Role", "not-equals", "admin"),
            id="raw-header-read-with-not-equals",
        ),
        pytest.param(
            '(vars.tier default "gold") == "gold"',
            compare("tier", "equals", "gold"),
            id="a-default-the-ai-gave-is-not-written",
        ),
    ],
)
def test_CP6_T47_dataweave_condition_is_written_as_a2m_writes_the_structured_form(
    tmp_path: Path, dataweave: str, structured: Any
) -> None:
    """[CP6-T47] A DataWeave-form AI condition is written exactly as a2m writes the same condition in the structured
    form; the AI's string is never used as given (a raw header read would compare the whole header)."""
    legacy = migrate(CP6 / "odd-condition", tmp_path / "legacy", fake(**{ODD: expression_answer(dataweave)}))
    tree = migrate(CP6 / "odd-condition", tmp_path / "tree", fake(**{ODD: condition_answer(structured)}))

    rec = condition_record(legacy.result, "curl-clients")
    assert (rec.method, rec.ok, rec.needs_review) == ("ai", True, False), why(rec)
    body = expression_body(when_holding(legacy, "AM-Curl").get("expression"))
    assert body == expression_body(when_holding(tree, "AM-Curl").get("expression"))
    assert body != unparen(dataweave)
    assert unparen(str(rec.dw)) == body


# ---------------------------------------------------------------- CP6 adversarial round 5
# One pipeline for both condition forms: parse, make canonical (every default a2m writes is set in the tree), check,
# and write that same tree, so a default a2m adds can never turn a checked condition into a constant one (T48). Any
# AI text a2m cannot read is a refusal sent to review, never an exception, and the other items carry on (T49, T50).

TIER_ODD = 'request.header.X-Tier =| "gold"'


@pytest.mark.parametrize(
    "dataweave",
    [
        pytest.param('startsWith(payload.name, "")', id="call-form-empty-prefix-on-a-body-field"),
        pytest.param('payload.name startsWith ""', id="infix-empty-prefix-on-a-body-field"),
        pytest.param('(payload.name default "") startsWith ""', id="defaulted-empty-prefix-on-a-body-field"),
        pytest.param('startsWith(payload, "")', id="empty-prefix-on-the-whole-body"),
        pytest.param("attributes.headers.'user-agent' startsWith \"\"", id="empty-prefix-on-a-header"),
    ],
)
def test_CP6_T48_empty_prefix_is_refused_as_constant_with_the_default_a2m_writes(tmp_path: Path, dataweave: str) -> None:
    """[CP6-T48] startsWith(payload.name, "") is written with a2m's default "", so a missing field reads as "" and
    the written condition is true for every request: it is refused as constant and sent to review."""
    m = migrate(CP6 / "odd-condition", tmp_path / "out", fake(**{ODD: expression_answer(dataweave)}))

    assert_condition_refused(m)
    assert "same result" in why(condition_record(m.result, "curl-clients"))


def test_CP6_T48_non_empty_prefix_on_a_body_field_is_written_with_the_checked_default(tmp_path: Path) -> None:
    """[CP6-T48] Control: a body field startsWith a non-empty prefix is used, written with the default "" that was
    checked (a missing field reads as "", which does not start with the prefix)."""
    m = migrate(CP6 / "odd-condition", tmp_path / "out", fake(**{ODD: expression_answer('startsWith(payload.name, "A")')}))

    rec = condition_record(m.result, "curl-clients")
    assert (rec.method, rec.ok, rec.needs_review) == ("ai", True, False), why(rec)
    body = expression_body(when_holding(m, "AM-Curl").get("expression"))
    assert 'default "")' in body and body.endswith('startsWith "A"'), body
    assert unparen(str(rec.dw)) == body


def two_ai_conditions(tmp_path: Path, first_answer: str) -> Migration:
    """The odd-condition proxy with a second untranslatable condition after curl-clients (Flow gold-tier, step
    AM-Gold), whose AI answer is a usable structured condition; curl-clients gets ``first_answer``."""
    bundle = copy_bundle(tmp_path, "odd-condition")
    add_flow(bundle, "gold-tier", TIER_ODD, "AM-Gold")
    second = condition_answer(compare("request.header.X-Tier", "starts-with", "gold"))
    return migrate(bundle, tmp_path / "out", fake(**{ODD: first_answer, TIER_ODD: second}))


def assert_later_items_carry_on(m: Migration) -> None:
    gold = condition_record(m.result, "gold-tier")
    assert (gold.method, gold.ok, gold.needs_review) == ("ai", True, False), why(gold)
    assert 'startsWith "gold"' in expression_body(when_holding(m, "AM-Gold").get("expression"))
    orders = condition_record(m.result, "get-orders")
    assert (orders.method, orders.ok) == ("template", True), why(orders)
    parse_all_xml(m.dest)


@pytest.mark.parametrize(
    "dataweave",
    [
        pytest.param('payload.café == "premium"', id="non-ascii-field-name"),
        pytest.param('vars.naïve == "a"', id="non-ascii-variable-name"),
        pytest.param("attributes.headers.x-ñ == 'a'", id="non-ascii-header-selector"),
        pytest.param('payload.count > ٣', id="non-ascii-digit"),
        pytest.param('payload.count > ²', id="superscript-digit"),
        pytest.param('ｖａｒｓ.x == "a"', id="full-width-letters"),
        pytest.param('vars.x\u200b == "a"', id="zero-width-space"),
    ],
)
def test_CP6_T49_non_ascii_dataweave_needs_review_and_later_items_carry_on(tmp_path: Path, dataweave: str) -> None:
    """[CP6-T49] An AI DataWeave answer with a non-ASCII name or digit outside a string is refused with a reason
    (needs review, the when stays #[false]); it never aborts the proxy, and the next AI condition is still used."""
    m = two_ai_conditions(tmp_path, expression_answer(dataweave))

    assert_condition_refused(m)
    assert_later_items_carry_on(m)


ODD_DATAWEAVE = [
    "(" * 5000 + 'vars.x == "a"' + ")" * 5000,
    "not (" * 3000 + 'vars.x == "a"' + ")" * 3000,
    " and ".join(['vars.x == "a"'] * 5000),
    " or ".join(f'vars.v{i} == "a"' for i in range(200)),
    'vars.x == "\\',
    'vars.x == "\\q"',
    'vars.x == "a',
    "\x00",
    '"' * 1001,
    'vars["x"',
    "vars.",
    "startsWith(",
    "startsWith(vars.x)",
    ")",
    "((((",
    "attributes.headers[",
    "vars.x == 1e999",
    "vars.x == -",
    "vars.x == - 5",
    "payload[0] == 'a'",
    'vars."" == "a"',
    "payload.x startsWith 5",
    'vars.x == "a" and',
    '(vars.x default 5) == "a"',
    "payload.n > " + "9" * 5000,
    'vars.x == "a" ' * 20000,
    "a" * 100000,
    "\ud800",
    "#[",
    "vars.x == $(vars.y)",
    'vars.x == "$(vars.y)"',
]
ODD_STRUCTURED: list[Any] = [
    None,
    1,
    [],
    "x",
    {"and": []},
    {"not": None},
    {"not": {"not": {"not": {"not": {"not": {"not": {"not": {"not": {"not": {"not": {"not": {"not": {"not": {"not": {"not": {"not": {"not": {"not": compare("x", "equals", "a")}}}}}}}}}}}}}}}}}},
    compare("x", "equals", 10**4000),
    compare("x", "greater", 1e308),
    compare("café", "equals", "a"),
    compare("x", "matches", "["),
    compare("x", "java-regex", "(?P<"),
    compare("x", "java-regex", "\\"),
    compare(["x"], "equals", "a"),  # type: ignore[arg-type]
    compare("x", ["equals"], "a"),  # type: ignore[arg-type]
    compare("x", "equals", {"a": 1}),
    {"or": [compare(f"v{i}", "equals", "a") for i in range(40)]},
]
ODD_RAW = [
    '{"status": "translated", "confidence": "high", "notes": "n", "condition": ' + "9" * 5000 + "}",
    '{"status": "translated", "confidence": "high", "notes": "n", "dataweave": "x", "n": ' + "9" * 5000 + "}",
    '{"status": "translated", "confidence": "high", "notes": "n", "condition": ' + '{"not": ' * 100000 + "}",
    "[" * 100000,
    '{"a":' * 100000,
    "\x00",
    "",
    "\ud800",
]
ODD_ANSWERS = (
    [pytest.param(expression_answer(text), id=f"dataweave-{i}") for i, text in enumerate(ODD_DATAWEAVE)]
    + [pytest.param(condition_answer(value), id=f"structured-{i}") for i, value in enumerate(ODD_STRUCTURED)]
    + [pytest.param(text, id=f"raw-{i}") for i, text in enumerate(ODD_RAW)]
)


@pytest.mark.parametrize("answer", ODD_ANSWERS)
def test_CP6_T50_no_ai_condition_answer_crashes_the_proxy(tmp_path: Path, answer: str) -> None:
    """[CP6-T50] Guard: malformed and odd AI condition answers (bad characters, deep nesting, huge input, numbers
    too long to convert, wrong JSON types) go through the full path without an exception escaping. Each is either
    used or refused for review with a reason, with the when #[false]; the next AI condition is still used."""
    m = two_ai_conditions(tmp_path, answer)

    rec = condition_record(m.result, "curl-clients")
    assert rec.method == "ai", why(rec)
    expression = str(when_holding(m, "AM-Curl").get("expression")).strip()
    if rec.ok:
        assert rec.needs_review is False and expression != "#[false]"
    else:
        assert rec.needs_review is True and expression == "#[false]"
        assert why(rec).strip(), "a refusal names its reason"
    assert_later_items_carry_on(m)


# ---------------------------------------------------------------- CP6 adversarial round 5: bounded pattern work
# A pattern whose backtracking work could blow up (many stacked wildcards, repeats inside repeats) is refused for
# review before any regex runs, so every AI pattern is either checked quickly or refused (T51).


@pytest.mark.parametrize(
    "condition",
    [
        pytest.param(compare("request.header.X-Path", "matches-path", "**/" * 32), id="stacked-path-wildcards"),
        pytest.param(compare("request.header.X-Path", "matches", "*a" * 40 + "*"), id="stacked-glob-wildcards"),
        pytest.param(compare("request.header.X-Path", "java-regex", "(a+)+b"), id="nested-repeats"),
        pytest.param(
            {"or": [compare("x", "matches", "*a*a*a*a*a*"), compare("x", "equals", "a" * 200)]},
            id="wildcards-against-a-long-value",
        ),
    ],
)
def test_CP6_T51_pattern_too_complex_to_check_quickly_is_refused_fast(tmp_path: Path, condition: Any) -> None:
    """[CP6-T51] An AI pattern whose regex work could blow up is refused with a reason within seconds, and the next
    AI condition is still used."""
    started = time.monotonic()
    m = two_ai_conditions(tmp_path, condition_answer(condition))
    elapsed = time.monotonic() - started

    assert_condition_refused(m)
    assert "too complex" in why(condition_record(m.result, "curl-clients"))
    assert elapsed < 10, f"took {elapsed:.1f}s"
    assert_later_items_carry_on(m)


def test_CP6_T51_ordinary_patterns_are_still_checked_and_used(tmp_path: Path) -> None:
    """[CP6-T51] Control: an ordinary MatchesPath and a Java regex with a few repeats are still used."""
    for name, condition in (
        ("path", compare("request.header.X-Path", "matches-path", "/api/**/orders")),
        ("regex", compare("request.header.User-Agent", "java-regex", "[a-z]+/[0-9.]+ .*")),
    ):
        m = migrate(CP6 / "odd-condition", tmp_path / name, fake(**{ODD: condition_answer(condition)}))
        rec = condition_record(m.result, "curl-clients")
        assert (rec.method, rec.ok, rec.needs_review) == ("ai", True, False), why(rec)


# ---------------------------------------------------------------- CP6 adversarial round 6: bound before any work
# Every AI pattern gets a cheap syntactic check before a2m builds any value from it or runs any regex: counted repeats
# above a small limit, a repeat inside a repeat, too many open-ended repeats or a pattern that is too long are refused
# for review at once (T52). The example texts built from a pattern have a size budget of their own, so even a pattern
# that got past the shape check could never allocate without bound.

NESTED_COUNTED = ["(a{5000}){5000}", "((a{3000}){3000}){3000}"]
BOUNDED_CHILD = r"""
import json, resource, sys, time
from a2m.ai import checks
out = {"condition": {}, "examples": {}}
for pattern in json.loads(sys.argv[1]):
    started = time.monotonic()
    try:
        checks.structured_condition({"variable": "request.header.X-Foo", "operator": "java-regex", "value": pattern}, "request")
        reason = None
    except checks.CheckError as exc:
        reason = str(exc)
    out["condition"][pattern] = [reason, time.monotonic() - started]
    started = time.monotonic()
    try:
        checks._regex_examples(pattern)
        reason = None
    except checks.CheckError as exc:
        reason = str(exc)
    out["examples"][pattern] = [reason, time.monotonic() - started]
out["max_rss_kb"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
print(json.dumps(out))
"""
BOUNDED_RSS_KB = 128 * 1024


def _limited_memory() -> None:
    import resource

    # A guard for the machine running the test: a regression fails with MemoryError, never degrades the host.
    resource.setrlimit(resource.RLIMIT_AS, (1536 * 1024 * 1024, 1536 * 1024 * 1024))


def test_CP6_T52_nested_counted_repeats_are_refused_in_well_under_a_second_with_bounded_memory() -> None:
    """[CP6-T52] The reviewers' repro: (a{5000}){5000} and ((a{3000}){3000}){3000} as an AI java-regex are refused
    as too complex in well under a second, and the process stays small. The example builder refuses them on its own
    too (its size budget), so the bound holds even without the shape check."""
    env = {k: v for k, v in os.environ.items() if k not in ("FORCE_COLOR", "PY_COLORS")}
    done = subprocess.run(
        [sys.executable, "-c", BOUNDED_CHILD, json.dumps(NESTED_COUNTED)],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        cwd=str(TESTS.parent),
        preexec_fn=_limited_memory,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    out = json.loads(done.stdout)
    for pattern in NESTED_COUNTED:
        for where in ("condition", "examples"):
            reason, seconds = out[where][pattern]
            assert reason is not None and "too complex" in reason, (where, pattern, reason)
            assert seconds < 0.5, (where, pattern, seconds)
    assert out["max_rss_kb"] < BOUNDED_RSS_KB, f"peak RSS {out['max_rss_kb'] // 1024} MB"


@pytest.mark.parametrize("pattern", NESTED_COUNTED)
def test_CP6_T52_nested_counted_repeats_need_review_and_later_items_carry_on(tmp_path: Path, pattern: str) -> None:
    """[CP6-T52] Through the full path: the nested counted repeat is refused for review naming "too complex", the
    when stays #[false], and the next AI condition is still used."""
    started = time.monotonic()
    m = two_ai_conditions(tmp_path, condition_answer(compare("request.header.X-Path", "java-regex", pattern)))
    elapsed = time.monotonic() - started

    assert_condition_refused(m)
    assert "too complex" in why(condition_record(m.result, "curl-clients"))
    assert elapsed < 10, f"took {elapsed:.1f}s"
    assert_later_items_carry_on(m)


@pytest.mark.parametrize(
    ("operator", "pattern"),
    [
        pytest.param("java-regex", "a{51}", id="counted-repeat-above-the-limit"),
        pytest.param("java-regex", "x{0,51}y", id="counted-range-above-the-limit"),
        pytest.param("java-regex", "a{51,}", id="open-repeat-with-a-high-minimum"),
        pytest.param("java-regex", "(ab?)+c", id="repeat-inside-a-repeat"),
        pytest.param("java-regex", "(?:(?:a|b{2})x)*", id="counted-repeat-inside-a-star"),
        pytest.param("java-regex", "a.*" * 9, id="nine-open-ended-repeats"),
        pytest.param("matches", "*x" * 9 + "*", id="ten-glob-wildcards"),
        pytest.param("matches", "a" * 257, id="pattern-longer-than-the-cap"),
    ],
)
def test_CP6_T52_pattern_shape_past_the_limits_is_refused_before_any_work(operator: str, pattern: str) -> None:
    """[CP6-T52] A pattern past the shape limits is refused for review as too complex, at once."""
    started = time.monotonic()
    with pytest.raises(checks_module().CheckError, match="too complex"):
        checks_module().structured_condition(
            compare("request.header.X-Foo", operator, pattern), "request"
        )
    assert time.monotonic() - started < 0.5


@pytest.mark.parametrize(
    ("operator", "pattern"),
    [
        pytest.param("java-regex", "a{50}b", id="counted-repeat-at-the-limit"),
        pytest.param("java-regex", "(ab){1,50}", id="counted-range-at-the-limit"),
        pytest.param("java-regex", "^(GET|POST)$", id="alternation"),
        pytest.param("java-regex", "[a-z]+@[a-z]+\\.com", id="email-like"),
        pytest.param("matches", "*curl*", id="glob"),
        pytest.param("matches-path", "/api/**/orders", id="path"),
    ],
)
def test_CP6_T52_ordinary_patterns_pass_the_shape_check(operator: str, pattern: str) -> None:
    """[CP6-T52] Control: ordinary patterns within the limits are still checked and written."""
    written = checks_module().structured_condition(compare("request.header.X-Foo", operator, pattern), "request")
    assert "matches" in written, written


def checks_module() -> Any:
    from a2m.ai import checks

    return checks


# ---------------------------------------------------------------- CP6 adversarial round 6: CP5's semantics for every operator
# An AI condition is written by CP5's emitter from the same comparison, so it gets exactly CP5's refusals and null
# handling for every operator CP5 writes (T53): the reviewer's equals-ignore-case repro is written exactly as CP5
# writes request.header.X-Foo := "bar" (a missing header reads lower(null), which is null in DataWeave, so the
# comparison is false, as in Apigee; proven on the runtime in tests/runtime/test_cp6_ai_runtime.py, T54).

APIGEE_SPELLING = {
    "equals": "=",
    "not-equals": "!=",
    "equals-ignore-case": ":=",
    "matches": "~",
    "matches-path": "~/",
    "java-regex": "~~",
    "greater": ">",
    "greater-or-equal": ">=",
    "less": "<",
    "less-or-equal": "<=",
}
PARITY_VARIABLES = [
    "request.header.X-Foo",
    "request.queryparam.q",
    "tier",
    "request.verb",
    "proxy.pathsuffix",
    "response.header.X-R",
]
PARITY_VALUES: list[Any] = ["bar", "BAR", "", None, 5, "/orders/*", "a.*", "*"]


def test_CP6_T53_equals_ignore_case_on_a_nullable_header_is_written_as_cp5_writes_it() -> None:
    """[CP6-T53] The reviewer's repro: an AI equals-ignore-case against a header that can be missing is written
    exactly as CP5 writes request.header.X-Foo := "bar", and refused wherever CP5 refuses it (null, "")."""
    from a2m.conditions.dataweave import translate_condition

    checks = checks_module()
    for side, variable in (("request", "request.header.X-Foo"), ("request", "request.queryparam.q"), ("request", "tier")):
        cp5 = translate_condition(f'{variable} := "bar"', direction=side)
        assert cp5.ok, cp5.reason
        assert checks.structured_condition(compare(variable, "equals-ignore-case", "bar"), side) == cp5.dw
        for value in ("", None):
            with pytest.raises(checks.CheckError):
                checks.structured_condition(compare(variable, "equals-ignore-case", value), side)


def test_CP6_T53_every_operator_has_cp5s_refusals_and_output() -> None:
    """[CP6-T53] Class sweep: for every operator CP5 writes, every kind of variable (one that can be missing, one
    that is always there, the response side) and every kind of literal, an AI comparison is refused whenever CP5
    refuses the same Apigee condition, and otherwise written exactly as CP5 writes it (or refused as constant).
    StartsWith is not in the sweep: CP5 refuses it, a2m writes it for an AI condition in CP5's shape."""
    from a2m.conditions.dataweave import translate_condition

    checks = checks_module()
    mismatches: list[str] = []
    for operator, spelling in APIGEE_SPELLING.items():
        for variable in PARITY_VARIABLES:
            side = "response" if variable.startswith("response.") else "request"
            for value in PARITY_VALUES:
                literal = "null" if value is None else (str(value) if isinstance(value, int) else f'"{value}"')
                cp5 = translate_condition(f"{variable} {spelling} {literal}", direction=side)
                try:
                    written: str | None = checks.structured_condition(compare(variable, operator, value), side)
                    reason = ""
                except checks.CheckError as exc:
                    written, reason = None, str(exc)
                if not cp5.ok:
                    same = written is None
                else:
                    same = written == cp5.dw or (written is None and "same result" in reason)
                if not same:
                    mismatches.append(f"{variable} {spelling} {literal}: CP5 {cp5.dw or cp5.reason!r}, AI {written or reason!r}")
    assert not mismatches, "\n".join(mismatches)


# ---------------------------------------------------------------- CP6 adversarial round 7: callout structure, accessor domains


class _OneAnswer:
    """A provider giving one fixed answer to every item."""

    def __init__(self, answer: str) -> None:
        self.answer = answer

    def complete(self, request: Any) -> str:
        return self.answer


def translate_callout(mule: str) -> Any:
    """``mule`` as a high-confidence AI callout answer, through Translator.callout."""
    from a2m.ai.provider import ItemKind
    from a2m.ai.sources import CalloutSource
    from a2m.ai.translate import Place, Translator

    translator = Translator(_OneAnswer(callout_answer(mule)))
    source = CalloutSource(ItemKind.JAVASCRIPT, "var x = 1;", "jsc/x.js")
    return translator.callout(source, "JS-X", "Javascript", "<Javascript/>", Place("p", "endpoint default", "request"))


STANDALONE_WHEN = '<when expression="#[true]"><logger message="translated"/></when>'
GUARDED = '<choice><when expression="{guard}"><logger level="INFO" message="x"/></when></choice>'
UNSTRUCTURED_FRAGMENTS = {
    "standalone-when-reviewer-repro": STANDALONE_WHEN,
    "standalone-otherwise": '<otherwise><logger message="x"/></otherwise>',
    "when-beside-a-processor": '<set-variable variableName="a" value="1"/>' + STANDALONE_WHEN,
    "choice-without-when": '<choice><otherwise><logger message="x"/></otherwise></choice>',
    "empty-choice": "<choice/>",
    "otherwise-before-when": (
        '<choice><otherwise><logger message="x"/></otherwise>'
        '<when expression="#[vars.x == 1]"><logger message="y"/></when></choice>'
    ),
    "two-otherwise": (
        '<choice><when expression="#[vars.x == 1]"><logger message="y"/></when>'
        '<otherwise><logger message="x"/></otherwise><otherwise><logger message="z"/></otherwise></choice>'
    ),
    "processor-directly-in-choice": (
        '<choice><logger message="x"/><when expression="#[vars.x == 1]"><logger message="y"/></when></choice>'
    ),
    "empty-when": '<choice><when expression="#[vars.x == 1]"/></choice>',
    "when-without-expression": '<choice><when><logger message="y"/></when></choice>',
    "when-inside-when": (
        '<choice><when expression="#[vars.x == 1]"><when expression="#[vars.y == 1]"><logger message="y"/></when>'
        "</when></choice>"
    ),
    "error-handler-before-processors": '<try><error-handler/><logger message="x"/></try>',
    "on-error-outside-error-handler": '<try><logger message="x"/><on-error-continue/></try>',
    "set-variable-without-variable-name": '<set-variable value="#[1]"/>',
    "unknown-core-element": '<scatter-gather><route><logger message="x"/></route></scatter-gather>',
    "unknown-attribute": '<logger message="x" colour="red"/>',
    "logger-level-mule-rejects": '<logger level="LOUD" message="x"/>',
    "text-inside-a-when": '<choice><when expression="#[vars.x == 1]">stray<logger message="y"/></when></choice>',
    "transform-in-a-choice-directly": (
        '<choice><ee:transform><ee:message><ee:set-payload>payload</ee:set-payload></ee:message></ee:transform>'
        "</choice>"
    ),
}
CONSTANT_GUARDS = {
    "true": "#[true]",
    "false": "#[false]",
    "true-in-brackets": "#[(true)]",
    "literal-comparison": "#[1 == 1]",
    "string-comparison": "#['payload' == 'payload']",
    "app-wide-value": "#[app.name == 'orders']",
    "read-only-in-a-comment": "#[true // payload]",
    "not-an-expression": "vars.x == 1",
}


@pytest.mark.parametrize("case", sorted(UNSTRUCTURED_FRAGMENTS))
def test_CP6_T55_callout_fragment_mule_would_reject_is_refused_by_the_translator(case: str) -> None:
    """[CP6-T55] The reviewer's repro (a standalone <when expression="#[true]">) and other structures Mule's core
    schema rejects, as a high-confidence answer through Translator.callout: unusable, low confidence, never used."""
    result = translate_callout(UNSTRUCTURED_FRAGMENTS[case])

    assert type(result).__name__ == "NotTranslated", result
    assert result.confidence is not None and result.confidence.value == "low"
    assert UNUSABLE.search(result.reason), result.reason


@pytest.mark.parametrize("case", sorted(CONSTANT_GUARDS))
def test_CP6_T55_constant_choice_guard_is_refused_by_the_translator(case: str) -> None:
    """[CP6-T55] A choice guard that reads nothing that changes per request (#[true] and the like) is refused."""
    result = translate_callout(GUARDED.format(guard=CONSTANT_GUARDS[case]))

    assert type(result).__name__ == "NotTranslated", result
    assert UNUSABLE.search(result.reason), result.reason


def test_CP6_T55_standalone_when_needs_review_and_nothing_is_written(tmp_path: Path) -> None:
    """[CP6-T55] Through the full path: the reviewer's standalone <when> needs review, nothing of it is written,
    and every generated XML file still parses."""
    llm = fake(**{"JS-AddCorrelation": callout_answer(STANDALONE_WHEN)})

    m = migrate(CP6 / "js-callout", tmp_path / "out", llm)

    rec = step_record(m.result, "JS-AddCorrelation")
    assert rec.needs_review is True
    assert UNUSABLE.search(why(rec)), why(rec)
    assert labelled(m, "JS-AddCorrelation") == []
    parse_all_xml(m.dest)


WELL_FORMED_FRAGMENTS = {
    "choice-with-otherwise": (
        '<choice><when expression="#[isEmpty(' + first_value_read("attributes.headers['x-api-key']") + ')]">'
        '<set-variable variableName="httpStatus" value="401"/>'
        '<raise-error type="A2M:POLICY_FAULT" description="missing API key"/></when>'
        '<otherwise><logger level="DEBUG" message="ok"/></otherwise></choice>'
    ),
    "try-with-error-handler": (
        '<try><set-payload value="#[payload]" mimeType="application/json"/>'
        '<error-handler><on-error-continue type="ANY"><logger message="x"/></on-error-continue></error-handler></try>'
    ),
    "foreach": '<foreach collection="#[payload.items]"><logger message="#[payload]"/></foreach>',
    "remove-variable": '<remove-variable variableName="tmp"/>',
}


@pytest.mark.parametrize("case", sorted(WELL_FORMED_FRAGMENTS))
def test_CP6_T55_well_formed_fragments_are_still_used(case: str) -> None:
    """[CP6-T55] Control: well-formed fragments of allowed processors, with guards that read the request, are used."""
    result = translate_callout(WELL_FORMED_FRAGMENTS[case])

    assert type(result).__name__ == "CalloutTranslated", result


def test_CP6_T56_negated_starts_with_space_on_a_header_is_refused_as_constant() -> None:
    """[CP6-T56] The reviewer's repro: not (request.header.X-Foo starts-with " "). The written read trims the
    header's first value and a missing header reads as "", so it can never start with a space and the negation is
    always true: refused as constant."""
    checks = checks_module()
    for side, variable in (("request", "request.header.X-Foo"), ("response", "response.header.X-R")):
        for operand in (compare(variable, "starts-with", " "), compare(variable, "starts-with", "\t")):
            for condition in ({"not": operand}, operand):
                with pytest.raises(checks.CheckError, match="same result"):
                    checks.structured_condition(condition, side)


@pytest.mark.parametrize(
    ("variable", "operator", "value"),
    [
        pytest.param("request.header.X-Foo", "not-equals", "a,b", id="header-never-holds-a-comma"),
        pytest.param("request.header.X-Foo", "not-equals", " a", id="header-never-has-a-leading-space"),
        pytest.param("request.header.X-Foo", "not-equals", "a ", id="header-never-has-a-trailing-space"),
        pytest.param("request.verb", "not-equals", "", id="verb-never-empty"),
        pytest.param("request.verb", "not-equals", "GE T", id="verb-never-holds-a-space"),
    ],
)
def test_CP6_T56_comparison_only_impossible_values_would_change_is_refused(variable: str, operator: str, value: Any) -> None:
    """[CP6-T56] A comparison with a value the written read can never give is constant and refused."""
    checks = checks_module()
    with pytest.raises(checks.CheckError, match="same result"):
        checks.structured_condition(compare(variable, operator, value), "request")


def test_CP6_T56_negated_starts_with_space_needs_review_and_the_guard_stays_false(tmp_path: Path) -> None:
    """[CP6-T56] Through the full path: the reviewer's repro needs review and leaves the generated guard #[false];
    the next AI condition and the template condition still work."""
    answer = condition_answer({"not": compare("request.header.X-Foo", "starts-with", " ")})

    m = two_ai_conditions(tmp_path, answer)

    assert_condition_refused(m)
    assert "same result" in why(condition_record(m.result, "curl-clients"))
    assert_later_items_carry_on(m)


@pytest.mark.parametrize(
    "condition",
    [
        pytest.param(compare("request.header.X-Foo", "starts-with", "curl"), id="header-starts-with"),
        pytest.param({"not": compare("request.header.X-Foo", "starts-with", "curl")}, id="negated-header-starts-with"),
        pytest.param(compare("request.queryparam.q", "starts-with", " "), id="query-parameter-keeps-spaces"),
        pytest.param(compare("request.verb", "equals", "GET"), id="verb-equals"),
    ],
)
def test_CP6_T56_conditions_the_read_can_make_vary_are_still_used(condition: Any) -> None:
    """[CP6-T56] Control: comparisons whose result the written read can change are still written."""
    assert checks_module().structured_condition(condition, "request")


# ---------------------------------------------------------------- CP6 adversarial round 8: one guard validator, empty values


def translate_callout_on(mule: str, original: str, side: str = "request") -> Any:
    """``mule`` as a high-confidence AI answer for the callout ``original``, through Translator.callout on ``side``."""
    from a2m.ai.provider import ItemKind
    from a2m.ai.sources import CalloutSource
    from a2m.ai.translate import Place, Translator

    translator = Translator(_OneAnswer(callout_answer(mule)))
    source = CalloutSource(ItemKind.JAVASCRIPT, original, "jsc/x.js")
    return translator.callout(source, "JS-X", "Javascript", "<Javascript/>", Place("p", "endpoint default", side))


def guards_of(processors: Any) -> list[str]:
    """Every guard (a when's expression, an error handler's when) in ``processors``, in document order."""
    found: list[str] = []
    for processor in processors:
        for el in processor.iter():
            if local(el) == "when" and el.get("expression") is not None:
                found.append(str(el.get("expression")))
            elif local(el) in ("on-error-continue", "on-error-propagate") and el.get("when") is not None:
                found.append(str(el.get("when")))
    return found


REVIEWER_GUARD = "#[vars.allowed == 'yes' or true]"
DISGUISED_CONSTANT_GUARDS = {
    "reviewer-runtime-read-or-true": REVIEWER_GUARD,
    "true-or-runtime-read": "#[true or vars.allowed == 'yes']",
    "runtime-read-and-not-false": "#[vars.allowed == 'yes' or (not (false))]",
    "runtime-read-or-literal-comparison": "#[vars.allowed == 'yes' or 1 == 1]",
    "runtime-read-or-app-wide-value": "#[vars.allowed == 'yes' or app.name == 'orders']",
    "read-equals-itself": "#[vars.allowed == vars.allowed]",
    "bang-negation-or-true": "#[!(vars.allowed == 'yes') or true]",
    "if-else-both-true": "#[if (vars.allowed == 'yes') true else true]",
    "value-or-its-negation": "#[vars.allowed == 'x' or (not (vars.allowed == 'x'))]",
    "defaulted-value-or-not-it": "#[(vars.allowed default 'q') == 'q' or vars.allowed != 'q']",
    "header-two-values-at-once": "#[attributes.headers.x == 'a' and attributes.headers.x == 'b']",
    "verb-never-empty": "#[attributes.method != '']",
    "empty-header-or-not": (
        "#[isEmpty(attributes.headers['x-api-key']) or (not (isEmpty(attributes.headers['x-api-key'])))]"
    ),
    "unknown-function-of-a-runtime-read": "#[sizeOf(vars.allowed) >= 0]",
    "default-of-runtime-read-to-true": "#[vars.allowed default true]",
    "isempty-of-a-variable": "#[isEmpty(vars.allowed) or true]",
}


@pytest.mark.parametrize("case", sorted(DISGUISED_CONSTANT_GUARDS))
def test_CP6_T57_disguised_constant_choice_guard_is_refused_by_the_translator(case: str) -> None:
    """[CP6-T57] The reviewer's repro (vars.allowed == 'yes' or true) and other guards that mention a runtime value but
    always give the same result, or that a2m cannot read: unusable through Translator.callout, low confidence."""
    result = translate_callout(GUARDED.format(guard=DISGUISED_CONSTANT_GUARDS[case]))

    assert type(result).__name__ == "NotTranslated", result
    assert result.confidence is not None and result.confidence.value == "low"
    assert UNUSABLE.search(result.reason), result.reason


@pytest.mark.parametrize(
    "mule",
    [
        pytest.param(
            '<try><logger message="x"/><error-handler><on-error-continue when="#[true]"><logger message="y"/>'
            "</on-error-continue></error-handler></try>",
            id="error-handler-when-true",
        ),
        pytest.param(
            '<try><logger message="x"/><error-handler><on-error-propagate when="'
            + REVIEWER_GUARD
            + '"><logger message="y"/></on-error-propagate></error-handler></try>',
            id="error-handler-when-reviewer-guard",
        ),
        pytest.param(
            '<choice><when expression="#[vars.a == \'b\']"><logger message="x"/></when>'
            '<when expression="' + REVIEWER_GUARD + '"><logger message="y"/></when></choice>',
            id="second-when-of-a-choice",
        ),
        pytest.param(
            '<foreach collection="#[payload.items]"><choice><when expression="' + REVIEWER_GUARD + '">'
            '<logger message="y"/></when></choice></foreach>',
            id="nested-choice",
        ),
    ],
)
def test_CP6_T57_every_guard_in_a_fragment_goes_through_the_validator(mule: str) -> None:
    """[CP6-T57] Every expression that decides which processors run is checked, wherever it sits."""
    result = translate_callout(mule)

    assert type(result).__name__ == "NotTranslated", result
    assert UNUSABLE.search(result.reason), result.reason


USABLE_GUARDS = {
    "flow-variable": "#[vars.allowed == 'yes']",
    "missing-api-key": "#[isEmpty(" + first_value_read("attributes.headers['x-api-key']") + ")]",
    "query-parameter-starts-with": "#[attributes.queryParams.q startsWith 'a']",
    "body-field": "#[payload.kind == 'order']",
    "header-or-variable": f"#[({first_value_read('attributes.headers.x')} == 'a') or (vars.b != null)]",
    "negated-empty-header": "#[not (isEmpty(" + first_value_read("attributes.headers['x-api-key']") + "))]",
}


@pytest.mark.parametrize("case", sorted(USABLE_GUARDS))
def test_CP6_T57_guard_is_written_as_a2m_checked_it(case: str) -> None:
    """[CP6-T57] Control: a guard in the subset is used, and what is written is exactly what a2m's guard pipeline
    checked and wrote, not the AI's text."""
    guard = USABLE_GUARDS[case]

    result = translate_callout(GUARDED.format(guard=guard))

    assert type(result).__name__ == "CalloutTranslated", result
    assert guards_of(result.processors) == [f"#[{checks_module().guard_condition(guard[2:-1], 'request')}]"]


@pytest.mark.parametrize(
    "text",
    [
        "vars.allowed == 'yes'",
        first_value_read("attributes.headers['x-foo']") + " == 'bar'",
        "(attributes.queryParams.q startsWith 'a') and (vars.b != null)",
        "not (attributes.method == 'GET')",
        "payload.kind == 'order'",
    ],
)
def test_CP6_T57_guard_pipeline_is_the_ai_condition_pipeline(text: str) -> None:
    """[CP6-T57] For every form both accept, a guard is written exactly as the same AI condition is."""
    checks = checks_module()

    assert checks.guard_condition(text, "request") == checks.dataweave_condition(text, "request")


def test_CP6_T57_raw_header_guard_is_refused_while_the_ai_condition_reads_the_first_value() -> None:
    """[CP6-T57] A raw header read is no longer a form both accept: as a guard it is refused, because the guard reads
    the whole value while a2m reads Apigee's first value (CP6-T60); as an AI condition it is written as that first
    value, as before."""
    checks = checks_module()
    text = "attributes.headers['x-foo'] == 'bar'"

    with pytest.raises(checks.CheckError, match="Apigee's first value"):
        checks.guard_condition(text, "request")
    assert checks.dataweave_condition(text, "request") == checks.dataweave_condition(
        first_value_read("attributes.headers['x-foo']") + " == 'bar'", "request"
    )


@pytest.mark.parametrize("text", ["vars.allowed == 'yes' or true", "isEmpty(attributes.headers.x)"])
def test_CP6_T57_ai_conditions_are_unchanged_by_the_guard_form(text: str) -> None:
    """[CP6-T57] AI conditions keep their own subset: neither a constant nor the guard-only isEmpty form is
    accepted there."""
    checks = checks_module()

    with pytest.raises(checks.CheckError):
        checks.dataweave_condition(text, "request")


def test_CP6_T57_response_side_guard_reads_the_sent_request(tmp_path: Path) -> None:
    """[CP6-T57] On the response side Mule's attributes hold the target's response: a guard reading them is refused,
    one reading the sent-request snapshot is written by a2m."""
    refused = translate_callout_on(GUARDED.format(guard="#[attributes.headers.x == 'a']"), "var x;", "response")
    snapshot_guard = first_value_read("vars.a2mSentRequest.headers.x") + " == 'a'"
    used = translate_callout_on(GUARDED.format(guard=f"#[{snapshot_guard}]"), "var x;", "response")

    assert type(refused).__name__ == "NotTranslated", refused
    assert type(used).__name__ == "CalloutTranslated", used
    expected = checks_module().guard_condition(snapshot_guard, "response")
    assert guards_of(used.processors) == [f"#[{expected}]"]


def test_CP6_T57_reviewer_guard_needs_review_and_nothing_is_written(tmp_path: Path) -> None:
    """[CP6-T57] Through project generation: the reviewer's guard needs review, none of the fragment is written,
    and every generated XML file still parses."""
    mule = (
        f'<choice><when expression="{REVIEWER_GUARD}">'
        '<set-variable variableName="httpStatus" value="403"/>'
        '<raise-error type="A2M:POLICY_FAULT" description="not allowed"/></when></choice>'
    )
    llm = fake(**{"JS-AddCorrelation": callout_answer(mule)})

    m = migrate(CP6 / "js-callout", tmp_path / "out", llm)

    rec = step_record(m.result, "JS-AddCorrelation")
    assert rec.needs_review is True
    assert UNUSABLE.search(why(rec)), why(rec)
    assert labelled(m, "JS-AddCorrelation") == []
    assert all("or true" not in guard for guard in guards_of(m.root))
    parse_all_xml(m.dest)


def test_CP6_T57_checked_guard_is_written_in_the_generated_project(tmp_path: Path) -> None:
    """[CP6-T57] Through project generation: a usable guard is written as a2m checked it."""
    guard = "#[isEmpty(" + first_value_read("attributes.headers['x-api-key']") + ")]"
    mule = (
        f'<choice><when expression="{guard}">'
        '<set-variable variableName="httpStatus" value="401"/>'
        '<raise-error type="A2M:POLICY_FAULT" description="missing API key"/></when></choice>'
    )
    llm = fake(**{"JS-AddCorrelation": callout_answer(mule)})

    m = migrate(CP6 / "js-callout", tmp_path / "out", llm)

    (choice,) = labelled(m, "JS-AddCorrelation")
    assert guards_of([choice]) == [f"#[{checks_module().guard_condition(guard[2:-1], 'request')}]"]
    assert step_record(m.result, "JS-AddCorrelation").needs_review is False
    parse_all_xml(m.dest)


CLEARING_SCRIPT = "context.setVariable('result', '');\n"
EMPTY_VALUES = {
    "set-variable-empty": ('<set-variable variableName="result" value=""/>', ""),
    "set-variable-spaces": ('<set-variable variableName="result" value="  "/>', "  "),
    "set-payload-empty": ('<set-payload value=""/>', ""),
    "set-payload-empty-with-mime-type": ('<set-payload value="" mimeType="text/plain"/>', ""),
}


@pytest.mark.parametrize("case", sorted(EMPTY_VALUES))
def test_CP6_T58_empty_literal_value_is_accepted(case: str) -> None:
    """[CP6-T58] The reviewer's repro: context.setVariable('result', '') as set-variable value="" (and set-payload
    value=""), which Mule's schema allows (value is an attributeType, not nonEmptyAttributeType), is used as it is."""
    mule, value = EMPTY_VALUES[case]

    result = translate_callout_on(mule, CLEARING_SCRIPT)

    assert type(result).__name__ == "CalloutTranslated", result
    (processor,) = result.processors
    assert processor.get("value") == value


@pytest.mark.parametrize(
    "mule",
    [
        pytest.param('<set-variable variableName="" value="x"/>', id="blank-variable-name"),
        pytest.param('<set-variable variableName="  " value="x"/>', id="spaces-variable-name"),
        pytest.param('<remove-variable variableName=""/>', id="blank-remove-variable-name"),
        pytest.param('<raise-error type=""/>', id="blank-error-type"),
        pytest.param('<set-variable variableName="result"/>', id="missing-value"),
        pytest.param("<set-payload/>", id="missing-payload-value"),
        pytest.param('<choice><when expression=""><logger message="x"/></when></choice>', id="blank-guard"),
    ],
)
def test_CP6_T58_blank_identifiers_and_missing_attributes_are_still_refused(mule: str) -> None:
    """[CP6-T58] Control: names, types and guards may not be blank, and a missing value is still refused."""
    result = translate_callout_on(mule, CLEARING_SCRIPT)

    assert type(result).__name__ == "NotTranslated", result
    assert UNUSABLE.search(result.reason), result.reason


def test_CP6_T58_clearing_a_variable_is_kept_in_the_generated_project(tmp_path: Path) -> None:
    """[CP6-T58] Through project generation: the clearing script's set-variable value="" is written at the step's
    place and the step does not need review."""
    bundle = copy_bundle(tmp_path, "js-callout")
    (bundle / "apiproxy" / "resources" / "jsc" / "add-correlation.js").write_text(CLEARING_SCRIPT, encoding="utf-8")
    llm = fake(**{"JS-AddCorrelation": callout_answer('<set-variable variableName="result" value=""/>')})

    m = migrate(bundle, tmp_path / "out", llm)

    (step,) = labelled(m, "JS-AddCorrelation")
    assert (local(step), step.get("variableName"), step.get("value")) == ("set-variable", "result", "")
    rec = step_record(m.result, "JS-AddCorrelation")
    assert (rec.method, rec.needs_review) == ("ai", False)
    parse_all_xml(m.dest)


# ---------------------------------------------------------------- CP6 adversarial round 9: a guard's default is kept
# A callout guard is the callout's own Mule code: a default it gives a read is its meaning. a2m keeps it in what it
# checks and in what it writes, and refuses a guard it cannot write with exactly the guard's own result (T59).

MODE_SCRIPT = (
    "var mode = context.getVariable('mode') || 'fallback';\n"
    "if (mode == 'fallback') { context.setVariable('result', 'fb'); }\n"
)
FALLBACK_GUARD = "#[(vars.mode default 'fallback') == 'fallback']"
FALLBACK_CHOICE = (
    '<choice><when expression="' + FALLBACK_GUARD + '"><set-variable variableName="result" value="fb"/></when></choice>'
)


def _guard_tree(text: str, side: str = "request") -> Any:
    """``text`` (a guard, with or without ``#[ ]``) parsed into a2m's condition tree with the defaults it gives."""
    body = text.strip()
    if body.startswith("#[") and body.endswith("]"):
        body = body[2:-1].strip()
    return checks_module().condition_from_dataweave(_bracket_negated_is_empty(body), side, guard=True)


def _bracket_negated_is_empty(text: str) -> str:
    """a2m writes a negated isEmpty as ``(not isEmpty(...))``; the subset reads ``not (...)``: add the brackets."""
    marker = "not isEmpty("
    start = text.find(marker)
    while start != -1:
        depth, index = 0, start + len("not ")
        while True:
            depth += {"(": 1, ")": -1}.get(text[index], 0)
            if text[index] == ")" and depth == 0:
                break
            index += 1
        text = text[: start + 4] + "(" + text[start + 4 : index + 1] + ")" + text[index + 1 :]
        start = text.find(marker, start + 1)
    return text


def _tree_compares(node: Any) -> list[Any]:
    if hasattr(node, "parts"):
        return [c for part in node.parts for c in _tree_compares(part)]
    if hasattr(node, "operand"):
        return _tree_compares(node.operand)
    return [node]


def _read_key(compare: Any) -> tuple[str, tuple[str, ...]]:
    return (compare.variable.strip().lower(), tuple(compare.path))


def _dw_result(node: Any, values: dict[tuple[str, tuple[str, ...]], Any]) -> bool:
    """``node`` evaluated as DataWeave reads it, independently of a2m's checker: a missing read (None) takes the
    read's own default when it has one; == and != compare text; startsWith of null is false; isEmpty is null or ""."""
    if hasattr(node, "parts"):
        results = [_dw_result(part, values) for part in node.parts]
        return all(results) if node.connective.value == "and" else any(results)
    if hasattr(node, "operand"):
        return not _dw_result(node.operand, values)
    value = values[_read_key(node)]
    if node.empty:
        return value is None or value == ""
    if value is None and node.default is not None:
        value = node.default
    operator, literal = node.operator.value, node.value
    if operator in ("equals", "not-equals"):
        same = value is None if literal.kind.value == "null" else value == literal.text
        return same if operator == "equals" else not same
    if operator == "starts-with":
        return isinstance(value, str) and value.startswith(literal.text)
    raise AssertionError(f"no model for {operator}")


def _representative_domain(*trees: Any) -> dict[tuple[str, tuple[str, ...]], list[Any]]:
    """Every read of ``trees`` with: missing, "", each literal and near misses, each default, and a fresh value."""
    domain: dict[tuple[str, tuple[str, ...]], list[Any]] = {}
    for tree in trees:
        for compare in _tree_compares(tree):
            values = domain.setdefault(_read_key(compare), [None, "", "a2m-fresh"])
            if compare.value.kind.value == "string":
                values += [compare.value.text, compare.value.text + "x", "x" + compare.value.text]
            if compare.default is not None:
                values += [compare.default, compare.default + "x"]
    return {key: list(dict.fromkeys(values)) for key, values in domain.items()}


def assert_same_results(original: str, written: str, side: str = "request") -> None:
    """The differential check: ``original`` (what the AI wrote) and ``written`` (what a2m writes) give the same
    result for every assignment of the representative domain."""
    import itertools

    before, after = _guard_tree(original, side), _guard_tree(written, side)
    domain = _representative_domain(before, after)
    keys = list(domain)
    for combination in itertools.product(*(domain[key] for key in keys)):
        values = dict(zip(keys, combination, strict=True))
        assert _dw_result(before, values) == _dw_result(after, values), (original, written, values)


def only_guard(result: Any) -> str:
    assert type(result).__name__ == "CalloutTranslated", result
    (guard,) = guards_of(result.processors)
    return guard


DEFAULTED_GUARDS = {
    "reviewer-mode-fallback": "#[(vars.mode default 'fallback') == 'fallback']",
    "reviewer-tier-free": "#[(vars.tier default 'free') == 'free']",
    "defaulted-not-equals": "#[(vars.mode default 'off') != 'off']",
    "defaulted-header": (
        "#[(" + first_value_read("attributes.headers['x-mode']") + " default 'fallback') == 'fallback']"
    ),
    "defaulted-query-parameter": "#[(attributes.queryParams.mode default 'fallback') == 'fallback']",
    "defaulted-starts-with": "#[(vars.mode default 'fallback') startsWith 'fall']",
    "default-that-does-not-match": "#[(vars.mode default 'other') == 'fallback']",
    "defaulted-in-a-junction": "#[((vars.mode default 'fallback') == 'fallback') and (vars.b != null)]",
    "defaulted-negated": "#[not ((vars.mode default 'fallback') == 'fallback')]",
    "defaulted-body-field": "#[(payload.kind default 'order') == 'order']",
}


@pytest.mark.parametrize("case", sorted(DEFAULTED_GUARDS))
def test_CP6_T59_guard_default_is_kept_with_the_guards_result(case: str) -> None:
    """[CP6-T59] The reviewers' repro and its class: a guard whose read has a default is used, and the guard a2m
    writes gives the AI's guard's result for every value, a missing value included (it reads as the default)."""
    guard = DEFAULTED_GUARDS[case]

    written = only_guard(translate_callout_on(GUARDED.format(guard=guard), MODE_SCRIPT))

    assert_same_results(guard, written)


@pytest.mark.parametrize(("mode", "branch"), [(None, True), ("fallback", True), ("other", False), ("", False)])
def test_CP6_T59_reviewer_repro_missing_mode_still_takes_the_fallback_branch(mode: Any, branch: bool) -> None:
    """[CP6-T59] The reviewers' repro through Translator.callout: with mode missing, the written guard still selects
    the fallback branch (it used to compare vars.mode with 'fallback' directly and skip it); a present value that is
    not 'fallback' still skips it."""
    result = translate_callout_on(FALLBACK_CHOICE, MODE_SCRIPT)

    assert result.confidence is not None and result.confidence.value == "high"
    assert _dw_result(_guard_tree(only_guard(result)), {("mode", ()): mode}) is branch


@pytest.mark.parametrize(
    "text", ["(vars.mode default 'fallback') == 'fallback'", "(vars.tier default 'free') == 'free'"]
)
def test_CP6_T59_guard_condition_keeps_the_default(text: str) -> None:
    """[CP6-T59] The reviewers' direct repro: guard_condition no longer drops the default, so the written guard is
    true when the variable is missing."""
    written = checks_module().guard_condition(text, "request")

    (compare,) = _tree_compares(_guard_tree(written))
    assert _dw_result(compare, {_read_key(compare): None}) is True
    assert_same_results(text, written)


def test_CP6_T59_project_keeps_the_guard_default(tmp_path: Path) -> None:
    """[CP6-T59] Through project generation: the generated app's guard selects the fallback branch when mode is
    missing, as the AI's guard and the original script do, and the step does not need review."""
    bundle = copy_bundle(tmp_path, "js-callout")
    (bundle / "apiproxy" / "resources" / "jsc" / "add-correlation.js").write_text(MODE_SCRIPT, encoding="utf-8")
    llm = fake(**{"JS-AddCorrelation": callout_answer(FALLBACK_CHOICE)})

    m = migrate(bundle, tmp_path / "out", llm)

    (step,) = labelled(m, "JS-AddCorrelation")
    (written,) = [str(el.get("expression")) for el in step.iter() if local(el) == "when"]
    rec = step_record(m.result, "JS-AddCorrelation")
    assert (rec.method, rec.needs_review) == ("ai", False)
    assert _dw_result(_guard_tree(written), {("mode", ()): None}) is True
    assert _dw_result(_guard_tree(written), {("mode", ()): "other"}) is False
    assert_same_results(FALLBACK_GUARD, written)
    parse_all_xml(m.dest)


def test_CP6_T59_error_handler_when_keeps_the_default() -> None:
    """[CP6-T59] The same for an error handler's when: its default is kept with the guard's result."""
    guard = "#[(vars.mode default 'fallback') == 'fallback']"
    mule = (
        '<try><logger message="x"/><error-handler><on-error-continue when="' + guard + '"><logger message="y"/>'
        "</on-error-continue></error-handler></try>"
    )

    written = only_guard(translate_callout_on(mule, MODE_SCRIPT))

    assert _dw_result(_guard_tree(written), {("mode", ()): None}) is True
    assert_same_results(guard, written)


ACCEPTED_GUARDS = sorted({*USABLE_GUARDS.values(), *DEFAULTED_GUARDS.values()})


@pytest.mark.parametrize("guard", ACCEPTED_GUARDS)
def test_CP6_T59_every_accepted_guard_is_written_with_the_guards_own_result(guard: str) -> None:
    """[CP6-T59] Structural differential check: for every accepted guard (the round-8 controls and the defaulted
    ones), the guard a2m writes and the AI's guard give identical results over the representative domain."""
    written = f"#[{checks_module().guard_condition(guard[2:-1], 'request')}]"

    assert only_guard(translate_callout_on(GUARDED.format(guard=guard), MODE_SCRIPT)) == written
    assert_same_results(guard, written)


@pytest.mark.parametrize(
    "text",
    [
        "vars.allowed == 'yes'",
        "attributes.headers['x-foo'] == 'bar'",
        "(attributes.queryParams.q startsWith 'a') and (vars.b != null)",
        "not (attributes.method == 'GET')",
        "payload.kind == 'order'",
        "(vars.tier default 'silver') == 'gold'",
    ],
)
def test_CP6_T59_accepted_conditions_whose_defaults_agree_keep_their_result(text: str) -> None:
    """[CP6-T59] The same differential check for AI conditions in the DataWeave form that carry no default, or one
    that cannot change the result: what a2m writes gives the AI's result for every value."""
    assert_same_results(text, checks_module().dataweave_condition(text, "request"))


@pytest.mark.parametrize(
    "guard",
    [
        pytest.param("#[(attributes.queryParams.q startsWith '') and (vars.a == 'b')]", id="empty-prefix-and-a-read"),
        pytest.param("#[(startsWith(attributes.headers.x, '')) and (vars.a == 'b')]", id="call-form-empty-prefix"),
    ],
)
def test_CP6_T59_guard_a2m_cannot_write_with_its_result_needs_review(guard: str) -> None:
    """[CP6-T59] The safety net: startsWith "" of a read without a default is false in DataWeave when the read is
    missing, but a2m writes startsWith with default "" (true), so the written guard would differ from the AI's when
    q is missing: refused, low confidence, never a guard with another result."""
    result = translate_callout_on(GUARDED.format(guard=guard), MODE_SCRIPT)

    assert type(result).__name__ == "NotTranslated", result
    assert result.confidence is not None and result.confidence.value == "low"
    assert UNUSABLE.search(result.reason), result.reason
    assert "different result" in result.reason


@pytest.mark.parametrize(
    "guard",
    [
        pytest.param("#[(vars.mode default 'fallback') == null]", id="defaulted-read-equals-null"),
        pytest.param("#[(vars.mode default 'q') == 'q' or vars.mode != 'q']", id="defaulted-tautology"),
        pytest.param("#[(vars.mode default 'q') != 'q' and vars.mode == 'q']", id="defaulted-contradiction"),
    ],
)
def test_CP6_T59_constant_check_reads_a_missing_value_as_the_guards_default(guard: str) -> None:
    """[CP6-T59] The constant check evaluates the guard with its own default, so a guard the default makes constant
    is refused."""
    result = translate_callout_on(GUARDED.format(guard=guard), MODE_SCRIPT)

    assert type(result).__name__ == "NotTranslated", result
    assert "same result" in result.reason


# ---------------------------------------------------------------- CP6 adversarial round 10: raw header reads in guards

HEADER_SCRIPT = (
    "var mode = context.getVariable('request.header.x-mode');\n"
    "if (mode == 'fallback') { context.setVariable('result', 'fb'); }\n"
)
RAW_HEADER_GUARDS = {
    "reviewer-repro-bracket": ("#[attributes.headers['x-mode'] == 'fallback']", "request"),
    "dot-quoted": ("#[attributes.headers.'x-mode' == 'fallback']", "request"),
    "not-equals": ("#[attributes.headers['x-mode'] != 'fallback']", "request"),
    "starts-with": ("#[attributes.headers['x-mode'] startsWith 'fall']", "request"),
    "starts-with-call": ("#[startsWith(attributes.headers['x-mode'], 'fall')]", "request"),
    "is-empty": ("#[isEmpty(attributes.headers['x-mode'])]", "request"),
    "defaulted": ("#[(attributes.headers['x-mode'] default 'fallback') == 'fallback']", "request"),
    "in-a-junction": ("#[(attributes.headers['x-mode'] == 'fallback') or (vars.b != null)]", "request"),
    "sent-request-snapshot": ("#[vars.a2mSentRequest.headers.'x-mode' == 'fallback']", "response"),
    "response-headers": ("#[vars.responseHeaders.'x-mode' == 'fallback']", "response"),
}


@pytest.mark.parametrize("case", sorted(RAW_HEADER_GUARDS))
def test_CP6_T60_raw_header_guard_needs_review(case: str) -> None:
    """[CP6-T60] The reviewers' repro and its class: a guard reading a header as Mule's raw entry gets the whole value
    ('fallback,other'), but a2m writes Apigee's first value ('fallback'), so the branch could flip. The callout is
    refused at low confidence, and the reason says a2m would read the header as Apigee's first value."""
    guard, side = RAW_HEADER_GUARDS[case]

    result = translate_callout_on(GUARDED.format(guard=guard), HEADER_SCRIPT, side)

    assert type(result).__name__ == "NotTranslated", result
    assert result.confidence is not None and result.confidence.value == "low"
    assert UNUSABLE.search(result.reason), result.reason
    assert "Apigee's first value" in result.reason, result.reason


def test_CP6_T60_reviewer_repro_guard_condition_is_refused() -> None:
    """[CP6-T60] The reviewers' direct repro: guard_condition no longer rewrites the raw read to the first value; it
    refuses, naming a comma-bearing value on which the two differ."""
    checks = checks_module()

    with pytest.raises(checks.CheckError, match="first value") as raised:
        checks.guard_condition("attributes.headers['x-mode'] == 'fallback'", "request")
    assert "," in str(raised.value)


def test_CP6_T60_reviewer_repro_project_needs_review(tmp_path: Path) -> None:
    """[CP6-T60] Through project generation: the reviewers' guard needs review and none of its fragment is written."""
    bundle = copy_bundle(tmp_path, "js-callout")
    (bundle / "apiproxy" / "resources" / "jsc" / "add-correlation.js").write_text(HEADER_SCRIPT, encoding="utf-8")
    mule = GUARDED.format(guard="#[attributes.headers['x-mode'] == 'fallback']")
    llm = fake(**{"JS-AddCorrelation": callout_answer(mule)})

    m = migrate(bundle, tmp_path / "out", llm)

    rec = step_record(m.result, "JS-AddCorrelation")
    assert rec.needs_review is True
    assert "Apigee's first value" in why(rec), why(rec)
    assert labelled(m, "JS-AddCorrelation") == []
    parse_all_xml(m.dest)


FIRST_VALUE_MODE = '(if (attributes.headers[\'x-mode\'] == null) null else trim((attributes.headers[\'x-mode\'] splitBy ",")[0] default ""))'
FIRST_VALUE_GUARDS = {
    "first-value-equals": f"#[{FIRST_VALUE_MODE} == 'fallback']",
    "first-value-defaulted": f"#[({FIRST_VALUE_MODE} default 'fallback') == 'fallback']",
    "raw-read-is-null": "#[attributes.headers['x-mode'] == null]",
}


@pytest.mark.parametrize("case", sorted(FIRST_VALUE_GUARDS))
def test_CP6_T60_guard_already_reading_the_first_value_is_accepted(case: str) -> None:
    """[CP6-T60] Control: a guard written with a2m's own first-value read keeps its meaning when a2m writes it, and so
    does a raw read compared with null (missing either way): used at high confidence, written by a2m's pipeline."""
    guard = FIRST_VALUE_GUARDS[case]

    result = translate_callout_on(GUARDED.format(guard=guard.replace('"', "&quot;")), HEADER_SCRIPT)

    assert type(result).__name__ == "CalloutTranslated", result
    assert result.confidence is not None and result.confidence.value == "high"
    assert only_guard(result) == f"#[{checks_module().guard_condition(guard[2:-1], 'request')}]"


def test_CP6_T60_first_value_guard_keeps_the_first_value_read() -> None:
    """[CP6-T60] The accepted first-value guard is written with that same first-value read, so 'fallback,other' still
    reads as 'fallback', as the AI wrote it and as the script's request.header.x-mode does."""
    guard = FIRST_VALUE_GUARDS["first-value-equals"]

    written = only_guard(translate_callout_on(GUARDED.format(guard=guard.replace('"', "&quot;")), HEADER_SCRIPT))

    assert FIRST_VALUE_MODE in written


@pytest.mark.parametrize(
    "guard",
    ["#[vars.mode == 'fallback']", "#[(vars.mode default 'fallback') == 'fallback']", "#[isEmpty(attributes.queryParams.q)]"],
)
def test_CP6_T60_guard_without_a_header_read_is_unaffected(guard: str) -> None:
    """[CP6-T60] Control: a guard reading only flow variables (or a query parameter, which a2m reads as Mule's own
    entry) is used as before, at high confidence."""
    result = translate_callout_on(GUARDED.format(guard=guard), MODE_SCRIPT)

    assert type(result).__name__ == "CalloutTranslated", result
    assert result.confidence is not None and result.confidence.value == "high"
    assert only_guard(result) == f"#[{checks_module().guard_condition(guard[2:-1], 'request')}]"


# ---------------------------------------------------------------- CP6 adversarial round 11: stray text anywhere in a fragment

STRAY = "@STRAY@"
MODE_GUARD = "#[vars.mode == 'fallback']"
STRAY_TEXT_FRAGMENTS = {
    "after-message": EE_MSG.format("<ee:set-payload>payload</ee:set-payload>").replace(
        "</ee:message>", "</ee:message>" + STRAY
    ),
    "after-variables": (
        '<ee:transform><ee:variables><ee:set-variable variableName="result">"fb"</ee:set-variable></ee:variables>'
        + STRAY
        + "<ee:message><ee:set-payload>payload</ee:set-payload></ee:message></ee:transform>"
    ),
    "after-message-in-a-choice": (
        '<choice><when expression="' + MODE_GUARD + '">'
        + EE_MSG.format("<ee:set-payload>payload</ee:set-payload>").replace("</ee:message>", "</ee:message>" + STRAY)
        + "</when></choice>"
    ),
    "between-processors": '<logger level="INFO" message="a"/>' + STRAY + '<logger level="INFO" message="b"/>',
    "between-processors-in-a-when": (
        '<choice><when expression="' + MODE_GUARD + '"><logger level="INFO" message="a"/>' + STRAY
        + '<logger level="INFO" message="b"/></when></choice>'
    ),
    "inside-choice-before-when": (
        "<choice>" + STRAY + '<when expression="' + MODE_GUARD + '"><logger level="INFO" message="a"/></when></choice>'
    ),
    "inside-choice-between-routes": (
        '<choice><when expression="' + MODE_GUARD + '"><logger level="INFO" message="a"/></when>' + STRAY
        + '<otherwise><logger level="INFO" message="b"/></otherwise></choice>'
    ),
}


@pytest.mark.parametrize("case", sorted(STRAY_TEXT_FRAGMENTS))
def test_CP6_T61_stray_text_anywhere_in_the_fragment_is_refused(case: str) -> None:
    """[CP6-T61] The reviewer's repro (text after </ee:message>) and its class: non-whitespace text anywhere in a
    callout's Mule code other than inside a script (after an ee: part, between processors, inside a choice) makes a
    high-confidence answer unusable, at low confidence."""
    result = translate_callout_on(STRAY_TEXT_FRAGMENTS[case].replace(STRAY, "stray"), MODE_SCRIPT)

    assert type(result).__name__ == "NotTranslated", result
    assert result.confidence is not None and result.confidence.value == "low"
    assert UNUSABLE.search(result.reason), result.reason


@pytest.mark.parametrize("case", sorted(STRAY_TEXT_FRAGMENTS))
def test_CP6_T61_same_fragment_with_only_whitespace_is_used(case: str) -> None:
    """[CP6-T61] Control: the same fragments with whitespace (newlines and indentation) in place of the stray text
    are used at high confidence."""
    result = translate_callout_on(STRAY_TEXT_FRAGMENTS[case].replace(STRAY, "\n    "), MODE_SCRIPT)

    assert type(result).__name__ == "CalloutTranslated", result
    assert result.confidence is not None and result.confidence.value == "high"


def test_CP6_T61_reviewer_repro_text_after_message_needs_review_in_the_project(tmp_path: Path) -> None:
    """[CP6-T61] Through project generation: a transform with text after </ee:message> needs review, none of it is
    written, and the generated XML still parses."""
    mule = STRAY_TEXT_FRAGMENTS["after-message"].replace(STRAY, "stray")
    llm = fake(**{"JS-AddCorrelation": callout_answer(mule)})

    m = migrate(CP6 / "js-callout", tmp_path / "out", llm)

    rec = step_record(m.result, "JS-AddCorrelation")
    assert rec.needs_review is True
    assert rec.confidence in ("low", None)
    assert UNUSABLE.search(why(rec)), why(rec)
    assert labelled(m, "JS-AddCorrelation") == []
    assert_runs_on_ce(m)
    parse_all_xml(m.dest)
