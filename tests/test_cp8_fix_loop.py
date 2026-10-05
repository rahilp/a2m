"""CP8: the AI fix loop for failing tests (default suite, no Mule).

Every test here runs without Java, Maven or Mule: the real a2m generator (CP3/CP4) builds a real,
well-formed Mule project (so :func:`a2m.verify.runner.redirect_plan` accepts it), but a content-driven
fake :class:`~a2m.verify.model.Runner` stands in for the real one. Its handle re-reads the proxy's
generated flow file on every call (never a cached snapshot), so whatever the AI's fix actually wrote to
disk is what decides pass or fail; "helped" and "passed" are real outcomes of running the battery twice,
never a scripted flag. The AI itself is one of the small local fake providers below (a plain object with
``complete(request) -> str``, a canned-answer queue, no network, no key); CP6's network guard
(tests/conftest.py, autouse) would fail any test that tried to reach a real model anyway.

Public contract this file pins (CP8 plan, files_likely a2m/verify/fix_loop.py, a2m/prompts/fix.md):

    a2m.verify.fix_loop (new module)

        FixAttempt(number: int, changed_files: tuple[str, ...], diff: str, failing_before: int,
                   failing_after: int, helped: bool, reason: str)
            One try: ``changed_files`` are the paths (relative to the app folder, forward slashes) the
            attempt actually rewrote (empty when nothing changed: a no-op fix, a malformed fix that was
            reverted, a fix refused for writing outside the project, an unusable or missing AI answer);
            ``diff`` is readable text of what changed (empty when ``changed_files`` is empty).
            ``.to_json_data() -> dict`` returns exactly these fields as plain JSON types (round-trips
            through ``json.dumps``/``json.loads`` unchanged).

        FixLoopResult(result: VerificationResult, attempts: tuple[FixAttempt, ...])
            ``result`` is the final (best) verification result, exactly as
            :func:`a2m.verify.verify_proxy` would return it for the attempt that is kept; ``attempts`` is
            empty when the AI was never asked (passed first time, static, max_fix_attempts=0).

        run_with_fixes(bundle, app_dir, *, runner, provider, max_fix_attempts=3, backend=None,
                       golden=None, config=None, generated=None, masker=None) -> FixLoopResult
            Verifies ``bundle``'s app in ``app_dir`` (same arguments as ``verify_proxy``); while the
            result is VerificationType.FAILED with at least one test that ran and failed, and attempts
            made so far are fewer than ``max_fix_attempts``, it:
              1. builds one fix request and calls ``provider.complete(request)`` once; the object passed
                 has a ``.prompt`` attribute (str, the whole text sent) containing the raw Apigee XML
                 (``Policy.raw_xml``) of every policy named by a failing case's ``.policy``, the full
                 text of every current ``*.xml`` file under ``app_dir/src/main/mule``, and the ``.diff``
                 text of every failing case;
              2. parses the answer as one JSON object: ``{"status": "fixed", "files": {"<path under
                 app_dir>": "<new full file text>"}, "notes": str}`` (``files`` may be empty: a no-op) or
                 ``{"status": "cannot_fix", "reason": str}``; anything else (empty text, invalid JSON, an
                 unknown status) counts as a provider answer that could not be used;
              3. refuses (without writing anything) a path that resolves outside ``app_dir``, or a
                 "fixed" file whose text is not well-formed XML (when its name ends .xml);
              4. applies a usable, safe, well-formed answer, by writing exactly the files it names, and
                 re-verifies;
              5. keeps the new attempt as the current best only when it has strictly fewer failing tests
                 than the one it replaces; otherwise it reverts the files to what they were right before
                 this attempt and keeps the previous best's result and diff for the next request;
              6. stops, keeping the best result so far, as soon as a re-verify has zero failing tests, or
                 once ``max_fix_attempts`` attempts have been made.
            A provider exception, for any attempt, is caught and recorded as that attempt's reason
            (``reason`` includes the exception's text); it never escapes ``run_with_fixes``.
            With ``max_fix_attempts <= 0``, or when the initial result is not VerificationType.FAILED, or
            is FAILED with zero tests ran (static-like), the AI is never asked and ``attempts`` is ``()``.

    a2m/prompts/fix.md (new packaged file, read like the other prompts: a2m.ai.prompts, A2M_PROMPTS_DIR)
        Exists, is non-empty, holds no em dash (standards rule).

    Engine wiring (a2m.engine / a2m.verify.harness), exercised only through ``a2m migrate``:
        --max-fix-attempts (already parsed by CP1's CLI) and the chosen provider reach the verify stage
        (``StageOptions.max_fix_attempts``, ``StageOptions.provider``, already plumbed since CP1/CP6);
        the stage runs the fix loop when a provider is set and writes the resulting attempts into
        <proxy>/verification.json as a top-level "attempts" list (one object per FixAttempt, same fields).

Fixture proxy 'rate-limit-proxy' (built fresh per test by :func:`write_app`, real CP3/CP4 output): one
SpikeArrest 'SA-Limit' (<Rate>600pm</Rate>, chosen only to keep the real :class:`~a2m.verify.harness._Pacer`'s
gap between calls tiny while staying above its 100ms minimum timeable interval; the over-limit case still
sends one too-soon call right after the paced one and still expects 429) on the ProxyEndpoint PreFlow, one
AssignMessage 'AM-AddHeader' (sets header X-Env: prod) on the PostFlow guarded by a condition the SpikeArrest
calls never satisfy, so a2m's battery gives four independent cases: SA-Limit-under-limit, SA-Limit-over-limit,
AM-AddHeader-header-set, AM-AddHeader-condition-false. The fake runner reads one marker comment this file
inserts right after ``<flow name="proxy-default">`` (``<!-- a2m-test rate="ok|bad" header="ok|bad" -->``) to
decide, on every call, whether the rate is really limited to 600pm and whether the header step really
runs; the marker is the only thing a canned fix answer needs to change to "pass".
"""

from __future__ import annotations

import http.client
import json
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

REPO = Path(__file__).resolve().parents[1]
XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
FLOW_REL = "src/main/mule/proxy.xml"
JSON_HEADERS = {"Content-Type": "application/json"}
FLOW_OPEN = '<flow name="proxy-default">'
MARKER = re.compile(r'<!-- a2m-test rate="(ok|bad)" header="(ok|bad)" -->')


def _fault(faultstring: str, errorcode: str) -> bytes:
    return json.dumps({"fault": {"faultstring": faultstring, "detail": {"errorcode": errorcode}}}).encode()


SPIKE_BODY = _fault("Spike arrest violation. Allowed rate : 600pm", "policies.ratelimit.SpikeArrestViolation")


# ---------------------------------------------------------------- fixture bundle


def _step(name: str, condition: str | None = None) -> str:
    cond = f"<Condition>{condition}</Condition>" if condition else ""
    return f"<Step><Name>{name}</Name>{cond}</Step>"


def write_rate_limit_bundle(parent: Path, name: str = "rate-limit-proxy", base_path: str = "/orders") -> Path:
    """parent/<name>/apiproxy/...: SA-Limit (600pm, tiny real pacing gap, see module docstring) on the
    PreFlow, AM-AddHeader guarded by X-Tag=yes on the PostFlow (so its own battery case is independent of
    SpikeArrest's, as pinned in the module docstring)."""
    root = parent / name / "apiproxy"
    for sub in ("policies", "proxies", "targets"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    spike = '<SpikeArrest name="SA-Limit">\n    <Rate>600pm</Rate>\n</SpikeArrest>\n'
    header = (
        '<AssignMessage name="AM-AddHeader">\n    <Set>\n        <Headers>\n'
        '            <Header name="X-Env">prod</Header>\n        </Headers>\n    </Set>\n'
        "    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n"
        '    <AssignTo createNew="false" transport="http" type="request"/>\n</AssignMessage>\n'
    )
    (root / f"{name}.xml").write_text(
        XML_HEAD + f'<APIProxy revision="1" name="{name}">\n    <DisplayName>{name}</DisplayName>\n'
        "    <Policies><Policy>SA-Limit</Policy><Policy>AM-AddHeader</Policy></Policies>\n"
        "    <ProxyEndpoints><ProxyEndpoint>default</ProxyEndpoint></ProxyEndpoints>\n"
        "    <TargetEndpoints><TargetEndpoint>default</TargetEndpoint></TargetEndpoints>\n</APIProxy>\n",
        encoding="utf-8",
    )
    (root / "policies" / "SA-Limit.xml").write_text(XML_HEAD + spike, encoding="utf-8")
    (root / "policies" / "AM-AddHeader.xml").write_text(XML_HEAD + header, encoding="utf-8")
    header_condition = 'request.header.X-Tag = "yes"'
    header_step = _step("AM-AddHeader", header_condition)
    (root / "proxies" / "default.xml").write_text(
        XML_HEAD + '<ProxyEndpoint name="default">\n'
        f'    <PreFlow name="PreFlow"><Request>{_step("SA-Limit")}</Request><Response/></PreFlow>\n'
        "    <Flows/>\n"
        f'    <PostFlow name="PostFlow"><Request>{header_step}</Request><Response/></PostFlow>\n'
        f"    <HTTPProxyConnection><BasePath>{base_path}</BasePath><VirtualHost>default</VirtualHost>"
        "</HTTPProxyConnection>\n"
        '    <RouteRule name="default"><TargetEndpoint>default</TargetEndpoint></RouteRule>\n</ProxyEndpoint>\n',
        encoding="utf-8",
    )
    (root / "targets" / "default.xml").write_text(
        XML_HEAD + '<TargetEndpoint name="default">\n'
        '    <PreFlow name="PreFlow"><Request/><Response/></PreFlow><Flows/>\n'
        '    <PostFlow name="PostFlow"><Request/><Response/></PostFlow>\n'
        f"    <HTTPTargetConnection><URL>http://backend.example{base_path}</URL></HTTPTargetConnection>\n"
        "</TargetEndpoint>\n",
        encoding="utf-8",
    )
    return parent / name


def marked(base_text: str, *, rate_ok: bool, header_ok: bool) -> str:
    """``base_text`` (a real generated proxy.xml) with the one test marker set as asked; see module docstring."""
    stripped = MARKER.sub("", base_text)
    comment = f'<!-- a2m-test rate="{"ok" if rate_ok else "bad"}" header="{"ok" if header_ok else "bad"}" -->'
    assert FLOW_OPEN in stripped
    return stripped.replace(FLOW_OPEN, FLOW_OPEN + comment, 1)


@dataclass
class App:
    bundle: Any
    app_dir: Path
    base_text: str  # the real generated proxy.xml, unmarked


def write_app(tmp_path: Path, name: str = "rate-limit-proxy", *, rate_ok: bool, header_ok: bool) -> App:
    """A real CP3/CP4 generated app for ``name`` under tmp_path, marked as asked (see module docstring)."""
    from a2m.generator import generate_project
    from a2m.parser import read_bundle

    bundle = read_bundle(write_rate_limit_bundle(tmp_path / f"bundle-{name}", name))
    app_dir = tmp_path / name / "mule-app"
    generate_project(bundle, app_dir)
    flow_path = app_dir / FLOW_REL
    base_text = flow_path.read_text(encoding="utf-8")
    assert FLOW_OPEN in base_text
    flow_path.write_text(marked(base_text, rate_ok=rate_ok, header_ok=header_ok), encoding="utf-8")
    return App(bundle, app_dir, base_text)


def flow_text(app_dir: Path) -> str:
    return (app_dir / FLOW_REL).read_text(encoding="utf-8")


def fix_answer(files: Mapping[str, str], *, status: str = "fixed", reason: str = "", notes: str = "fix") -> str:
    if status != "fixed":
        return json.dumps({"status": status, "reason": reason or "cannot fix it"})
    return json.dumps({"status": "fixed", "files": dict(files), "notes": notes})


def no_op_answer() -> str:
    """A well-formed, usable answer that changes nothing."""
    return fix_answer({})


def marker_fix(base_text: str, *, rate_ok: bool, header_ok: bool, notes: str = "fix") -> str:
    return fix_answer({FLOW_REL: marked(base_text, rate_ok=rate_ok, header_ok=header_ok)}, notes=notes)


def malformed_fix(base_text: str) -> str:
    broken = marked(base_text, rate_ok=True, header_ok=True).replace("</mule>", "")
    return fix_answer({FLOW_REL: broken})


def traversal_fix() -> str:
    return fix_answer({"../../outside.xml": "pwned"})


# ---------------------------------------------------------------- the content-driven fake runner


class ContentHandle:
    """Re-reads ``app_dir``'s flow file on every call; see the module docstring for the exact sequence of
    calls this proxy's battery sends (1: under-limit, 2-3: over-limit, 4: header-set, 5: condition-false)."""

    def __init__(self, app_dir: Path, backend_url: str) -> None:
        self.app_dir = app_dir
        self.backend_url = backend_url
        self.running = True
        self.base_url = "http://fake-content.invalid/app"
        self.calls = 0
        self.stopped = False

    def send(self, request: Any) -> Any:
        from a2m.verify import HttpResponse

        assert not self.stopped
        self.calls += 1
        text = flow_text(self.app_dir)
        match = MARKER.search(text)
        rate_ok = match is not None and match.group(1) == "ok"
        header_ok = match is not None and match.group(2) == "ok"
        headers_in = {str(k).lower(): v for k, v in dict(request.headers or {}).items()}
        if self.calls == 3 and rate_ok:
            return HttpResponse(429, dict(JSON_HEADERS), SPIKE_BODY)
        extra = {"X-Env": "prod"} if "x-tag" in headers_in and header_ok else {}
        return self._forward(request, extra)

    def _forward(self, request: Any, extra: Mapping[str, str]) -> Any:
        from a2m.verify import HttpResponse

        target = urlsplit(self.backend_url)
        sent = {
            k: v
            for k, v in dict(request.headers or {}).items()
            if str(k).lower() not in ("host", "content-length", "connection", "transfer-encoding")
        }
        sent.update(extra)
        conn = http.client.HTTPConnection(target.hostname or "127.0.0.1", target.port or 80, timeout=10)
        try:
            conn.request(request.method, request.path, body=request.body or None, headers=sent)
            got = conn.getresponse()
            body = got.read()
            return HttpResponse(got.status, dict(got.getheaders()), body)
        finally:
            conn.close()

    def stop(self) -> None:
        self.stopped = True


class ContentRunner:
    """A Runner whose handle is content-driven (see :class:`ContentHandle`); records every start()."""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.unavailable: str | None = None

    def start(self, app: Any, *, backend_url: str) -> ContentHandle:
        self.started.append(app.name)
        if self.unavailable is not None:
            from a2m.verify.mule import RuntimeUnavailableError

            raise RuntimeUnavailableError(self.unavailable)
        return ContentHandle(app.app_dir, backend_url)


@pytest.fixture
def backend() -> Any:
    from a2m.verify.mock_backend import MockBackend

    mock = MockBackend(default_status=200, default_headers=dict(JSON_HEADERS), default_body=b'{"id":7}')
    mock.start()
    try:
        yield mock
    finally:
        mock.stop()


# ---------------------------------------------------------------- the local fake AI


@dataclass
class QueueProvider:
    """A Provider (``complete(request) -> str``) answering from a fixed queue, recording every request."""

    answers: list[str] = field(default_factory=list)
    requests: list[Any] = field(default_factory=list)

    def complete(self, request: Any) -> str:
        self.requests.append(request)
        assert self.answers, "the fix loop asked the AI more times than this test queued answers for"
        return self.answers.pop(0)

    @property
    def calls(self) -> int:
        return len(self.requests)


@dataclass
class RaisingProvider:
    """A Provider whose every call raises (simulating a provider/network error)."""

    message: str = "simulated provider timeout"
    calls: int = 0

    def complete(self, request: Any) -> str:
        self.calls += 1
        raise TimeoutError(self.message)


def run_loop(app: App, provider: Any, *, runner: Any = None, backend: Any = None, max_fix_attempts: int = 3) -> Any:
    from a2m.verify.fix_loop import run_with_fixes

    return run_with_fixes(
        app.bundle, app.app_dir, runner=runner or ContentRunner(), provider=provider,
        max_fix_attempts=max_fix_attempts, backend=backend,
    )


def failing_cases(result: Any) -> list[Any]:
    return [c for c in result.cases if not c.passed]


# ---------------------------------------------------------------- CP8-T01


def test_CP8_T01_a_failing_test_sends_the_ai_the_original_policy_the_attempt_and_the_diff(tmp_path: Path) -> None:
    """[CP8-T01] One fix request reaches the AI with the original Apigee SA-Limit XML, the current (wrong) Mule
    flow, and the diff of the failing test."""
    app = write_app(tmp_path, rate_ok=False, header_ok=True)
    before = flow_text(app.app_dir)
    provider = QueueProvider([marker_fix(app.base_text, rate_ok=True, header_ok=True)])
    loop = run_loop(app, provider)

    assert provider.calls == 1
    sent = provider.requests[0].prompt
    assert "<Rate>600pm</Rate>" in sent
    assert "SA-Limit" in sent
    assert before in sent or 'rate="bad"' in sent
    assert "429" in sent and "SpikeArrestViolation" in sent or "status" in sent.lower()
    assert loop.result.type.value == "battery"


# ---------------------------------------------------------------- CP8-T02


def test_CP8_T02_the_fix_prompt_is_a_file_in_a2m_prompts_and_editing_it_changes_what_is_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[CP8-T02] a2m/prompts/fix.md ships with the package (non-empty, no em dash); pointing A2M_PROMPTS_DIR at an
    edited copy changes the text the fake AI receives, with no Python code changed."""
    from importlib import resources

    shipped = resources.files("a2m").joinpath("prompts", "fix.md").read_text(encoding="utf-8")
    assert shipped.strip()
    assert "—" not in shipped

    override = tmp_path / "prompts"
    override.mkdir()
    (override / "fix.md").write_text("MARKER-FIX-PROMPT-7731 {{original}} {{diff}}\n", encoding="utf-8")
    monkeypatch.setenv("A2M_PROMPTS_DIR", str(override))

    app = write_app(tmp_path, rate_ok=False, header_ok=True)
    provider = QueueProvider([marker_fix(app.base_text, rate_ok=True, header_ok=True)])
    run_loop(app, provider)
    assert "MARKER-FIX-PROMPT-7731" in provider.requests[0].prompt


# ---------------------------------------------------------------- CP8-T03


def test_CP8_T03_the_loop_stops_as_soon_as_the_tests_pass(tmp_path: Path) -> None:
    """[CP8-T03] A good fix on the first try stops the loop at once: one AI call, two verify rounds, one
    attempt recorded, two canned fixes left unused in the queue."""
    app = write_app(tmp_path, rate_ok=False, header_ok=True)
    good = marker_fix(app.base_text, rate_ok=True, header_ok=True)
    never_1 = marker_fix(app.base_text, rate_ok=True, header_ok=False)
    never_2 = marker_fix(app.base_text, rate_ok=False, header_ok=False)
    provider = QueueProvider([good, never_1, never_2])
    runner = ContentRunner()

    loop = run_loop(app, provider, runner=runner, max_fix_attempts=3)

    assert provider.calls == 1
    assert len(runner.started) == 2
    assert loop.result.type.value == "battery"
    assert len(loop.attempts) == 1
    assert loop.attempts[0].helped is True
    assert provider.answers == [never_1, never_2]


# ---------------------------------------------------------------- CP8-T04


def test_CP8_T04_with_no_setting_the_loop_gives_up_after_3_tries(tmp_path: Path) -> None:
    """[CP8-T04] A no-op fix every time: 3 AI calls, 4 verify rounds, 3 attempts recorded, default max attempts."""
    app = write_app(tmp_path, rate_ok=False, header_ok=True)
    provider = QueueProvider([no_op_answer(), no_op_answer(), no_op_answer()])
    runner = ContentRunner()

    loop = run_loop(app, provider, runner=runner, max_fix_attempts=3)

    assert provider.calls == 3
    assert len(runner.started) == 4
    assert len(loop.attempts) == 3
    assert loop.result.type.value == "failed"


# ---------------------------------------------------------------- CP8-T05 (CLI, --max-fix-attempts)


class AlwaysFailRunner:
    """A Runner whose app always answers every call with a status that never matches what the battery
    expects, so the proxy fails no matter what the AI writes; used only for the CLI-driven cases, where
    content does not matter."""

    def __init__(self) -> None:
        self.started: list[str] = []

    def start(self, app: Any, *, backend_url: str) -> Any:
        from a2m.verify import HttpResponse

        self.started.append(app.name)

        class _Handle:
            running = True
            base_url = "http://fake-fail.invalid/app"

            def send(self, request: Any) -> Any:
                return HttpResponse(500, dict(JSON_HEADERS), b'{"fail":true}')

            def stop(self) -> None:
                pass

        return _Handle()


def stages_with(runner: Any) -> list[Any]:
    from a2m.engine import generate, parse
    from a2m.verify import make_verify_stage

    return [parse, generate, make_verify_stage(runner=runner)]


def verification_json(results: Path, proxy: str) -> dict[str, Any]:
    return json.loads((results / proxy / "verification.json").read_text(encoding="utf-8"))


def write_fix_answer_file(llm_dir: Path, proxy: str, answer: str) -> None:
    folder = llm_dir / "fix"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{proxy}.json").write_text(answer, encoding="utf-8")


def test_CP8_T05_max_fix_attempts_on_the_command_line_sets_the_number_of_tries(
    tmp_path: Path, run_cli: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[CP8-T05] --max-fix-attempts 2 gives exactly 2 attempts through the engine; --max-fix-attempts 5 gives 5."""
    exports = tmp_path / "exports"
    write_rate_limit_bundle(exports)
    llm_dir = tmp_path / "llm"
    write_fix_answer_file(llm_dir, "rate-limit-proxy", no_op_answer())
    monkeypatch.setenv("A2M_FAKE_LLM_DIR", str(llm_dir))

    for attempts in (2, 5):
        out = tmp_path / f"out-{attempts}"
        run_cli(
            [
                "migrate", str(exports), "--out", str(out), "--mock-backends", "--llm", "fake",
                "--max-fix-attempts", str(attempts),
            ],
            stages=stages_with(AlwaysFailRunner()),
        )
        data = verification_json(out, "rate-limit-proxy")
        assert len(data["attempts"]) == attempts, data


# ---------------------------------------------------------------- CP8-T06


def test_CP8_T06_max_fix_attempts_0_turns_the_fix_loop_off(tmp_path: Path, run_cli: Any) -> None:
    """[CP8-T06] --max-fix-attempts 0: no attempts recorded, the app's files are untouched, and the proxy's
    verification stays failed (for review), never passed."""
    app = write_app(tmp_path, rate_ok=False, header_ok=True)
    before = flow_text(app.app_dir)
    provider = QueueProvider([])  # must never be asked

    loop = run_loop(app, provider, max_fix_attempts=0)

    assert provider.calls == 0
    assert loop.attempts == ()
    assert flow_text(app.app_dir) == before
    assert loop.result.type.value == "failed"

    # And through the engine: --max-fix-attempts 0 with --llm fake, no fix answer fixture needed at all.
    exports = tmp_path / "exports2"
    write_rate_limit_bundle(exports)
    out = tmp_path / "out2"
    run_cli(
        ["migrate", str(exports), "--out", str(out), "--mock-backends", "--llm", "fake", "--max-fix-attempts", "0"],
        stages=stages_with(AlwaysFailRunner()),
    )
    data = verification_json(out, "rate-limit-proxy")
    assert data["attempts"] == []
    assert data["type"] == "failed"


# ---------------------------------------------------------------- CP8-T07


def test_CP8_T07_each_attempt_records_what_changed_and_whether_it_helped(tmp_path: Path) -> None:
    """[CP8-T07] A no-op, then a header-only fix, then the good fix: three attempts, numbered 1-3, failing
    counts 2->2, 2->1, 1->0, helped False/True/True, outcome passed, attempts survive a JSON round trip."""
    app = write_app(tmp_path, rate_ok=False, header_ok=False)
    answers = [
        no_op_answer(),
        marker_fix(app.base_text, rate_ok=False, header_ok=True),
        marker_fix(app.base_text, rate_ok=True, header_ok=True),
    ]
    provider = QueueProvider(list(answers))

    loop = run_loop(app, provider, max_fix_attempts=3)

    assert [a.number for a in loop.attempts] == [1, 2, 3]
    assert next(a.changed_files for a in loop.attempts) == ()
    assert loop.attempts[0].diff == ""
    for attempt in loop.attempts[1:]:
        assert attempt.changed_files and attempt.diff
    assert [(a.failing_before, a.failing_after) for a in loop.attempts] == [(2, 2), (2, 1), (1, 0)]
    assert [a.helped for a in loop.attempts] == [False, True, True]
    assert loop.result.type.value == "battery"

    roundtripped = json.loads(json.dumps([a.to_json_data() for a in loop.attempts]))
    assert roundtripped == [a.to_json_data() for a in loop.attempts]


# ---------------------------------------------------------------- CP8-T08


def test_CP8_T08_a_proxy_still_failing_after_the_last_attempt_goes_to_review_never_passed(tmp_path: Path) -> None:
    """[CP8-T08] After 3 no-op attempts the result stays failed (never battery/golden), says so plainly, and
    keeps the last failing diff."""
    app = write_app(tmp_path, rate_ok=False, header_ok=True)
    provider = QueueProvider([no_op_answer()] * 3)

    loop = run_loop(app, provider, max_fix_attempts=3)

    assert loop.result.type.value == "failed"
    assert "3" in loop.result.message and "fix attempt" in loop.result.message
    failing = failing_cases(loop.result)
    assert failing and "429" in failing[0].diff


# ---------------------------------------------------------------- CP8-T09


def test_CP8_T09_a_fix_that_breaks_the_project_is_counted_as_failed_and_undone(tmp_path: Path) -> None:
    """[CP8-T09] A malformed fix (unclosed tag) is reverted at once, never tested, and attempt 2's good fix
    still gets applied and passes."""
    app = write_app(tmp_path, rate_ok=False, header_ok=True)
    provider = QueueProvider([malformed_fix(app.base_text), marker_fix(app.base_text, rate_ok=True, header_ok=True)])
    runner = ContentRunner()

    loop = run_loop(app, provider, runner=runner, max_fix_attempts=3)

    assert len(loop.attempts) == 2
    first = loop.attempts[0]
    assert first.helped is False
    assert first.changed_files == ()
    assert "malformed" in first.reason.lower() or "well-formed" in first.reason.lower() or "xml" in first.reason.lower()
    # the broken attempt is never tested on the runner: only the initial round + attempt 2's round happened.
    assert len(runner.started) == 2
    assert loop.result.type.value == "battery"
    assert loop.attempts[1].helped is True


# ---------------------------------------------------------------- CP8-T10


def test_CP8_T10_an_unusable_ai_answer_is_a_failed_attempt_not_a_crash(tmp_path: Path) -> None:
    """[CP8-T10a] An empty, unparseable answer never crashes the loop: not helped, files unchanged, ends in
    review after the attempts run out."""
    app = write_app(tmp_path, rate_ok=False, header_ok=True)
    before = flow_text(app.app_dir)
    provider = QueueProvider(["", "not json either", "{}"])

    loop = run_loop(app, provider, max_fix_attempts=3)

    assert len(loop.attempts) == 3
    assert all(not a.helped for a in loop.attempts)
    assert all(a.changed_files == () for a in loop.attempts)
    assert flow_text(app.app_dir) == before
    assert loop.result.type.value == "failed"


def test_CP8_T10_a_provider_error_is_a_failed_attempt_and_the_batch_carries_on(tmp_path: Path, run_cli: Any) -> None:
    """[CP8-T10b] A provider exception (simulated timeout) never escapes the loop, and through the engine the
    next proxy in the batch still finishes with its .done marker."""
    app = write_app(tmp_path, rate_ok=False, header_ok=True)
    provider = RaisingProvider()

    loop = run_loop(app, provider, max_fix_attempts=2)
    assert len(loop.attempts) == 2
    assert all(not a.helped for a in loop.attempts)
    assert all("timeout" in a.reason.lower() or "timeouterror" in a.reason.lower() for a in loop.attempts)
    assert loop.result.type.value == "failed"

    # Through the engine: the batch has rate-limit-proxy (erroring provider) and healthy-proxy (passes first
    # try), via a custom per-proxy stage so this test controls the provider directly.
    from a2m.layout import mule_app_dir
    from a2m.parser import read_bundle
    from a2m.verify.fix_loop import run_with_fixes

    exports = tmp_path / "exports"
    write_rate_limit_bundle(exports, "rate-limit-proxy")
    write_rate_limit_bundle(exports, "healthy-proxy", base_path="/healthy")
    out = tmp_path / "out"

    def verify_stage(context: Any) -> None:
        bundle = read_bundle(context.bundle_dir, label=context.name)
        app_dir = mule_app_dir(context.out_dir)
        from a2m.generator import generate_project

        generate_project(bundle, app_dir)
        rate_ok = header_ok = context.name != "rate-limit-proxy"
        flow_path = app_dir / FLOW_REL
        flow_path.write_text(marked(flow_path.read_text(encoding="utf-8"), rate_ok=rate_ok, header_ok=header_ok))
        chosen = RaisingProvider() if context.name == "rate-limit-proxy" else QueueProvider([])
        run_with_fixes(bundle, app_dir, runner=ContentRunner(), provider=chosen, max_fix_attempts=2)

    from a2m.engine import generate as _unused_generate  # noqa: F401  (stage kept minimal, generation inline above)
    from a2m.engine import parse

    run_cli(
        ["migrate", str(exports), "--out", str(out), "--mock-backends", "--llm", "fake", "--max-fix-attempts", "2"],
        stages=[parse, verify_stage],
    )
    assert (out / "healthy-proxy" / ".done").is_file()


# ---------------------------------------------------------------- CP8-T11


def test_CP8_T11_an_ai_fix_cannot_write_files_outside_the_proxys_mule_project(tmp_path: Path) -> None:
    """[CP8-T11] A fix naming '../../outside.xml' is refused: nothing is written anywhere outside the app, the
    app itself is untouched, and the attempt names the refused path."""
    app = write_app(tmp_path, rate_ok=False, header_ok=True)
    before = flow_text(app.app_dir)
    provider = QueueProvider([traversal_fix(), marker_fix(app.base_text, rate_ok=True, header_ok=True)])

    loop = run_loop(app, provider, max_fix_attempts=3)

    assert not any(tmp_path.rglob("outside.xml"))
    assert flow_text(app.app_dir) == before  # restored before attempt 2 is even considered
    assert loop.attempts[0].helped is False
    assert "outside.xml" in loop.attempts[0].reason or ".." in loop.attempts[0].reason


# ---------------------------------------------------------------- CP8-T12


def test_CP8_T12_when_the_app_could_only_be_built_the_fix_loop_does_not_run(tmp_path: Path) -> None:
    """[CP8-T12] A runner that reports the runtime as unavailable (static) never gets an AI call."""
    app = write_app(tmp_path, rate_ok=False, header_ok=True)
    before = flow_text(app.app_dir)
    runner = ContentRunner()
    runner.unavailable = "java, maven and mule are not installed"
    provider = QueueProvider([])

    loop = run_loop(app, provider, runner=runner, max_fix_attempts=3)

    assert provider.calls == 0
    assert loop.attempts == ()
    assert flow_text(app.app_dir) == before
    assert loop.result.type.value == "static"


# ---------------------------------------------------------------- CP8-T13


def test_CP8_T13_each_new_try_sends_the_latest_attempt_and_the_latest_diff(tmp_path: Path) -> None:
    """[CP8-T13] The second fix request carries attempt 1's (header-fixed) flow and the second run's diff
    (only over-limit left), and still the original SA-Limit Apigee XML."""
    app = write_app(tmp_path, rate_ok=False, header_ok=False)
    after_attempt_1 = marker_fix(app.base_text, rate_ok=False, header_ok=True)
    good = marker_fix(app.base_text, rate_ok=True, header_ok=True)
    provider = QueueProvider([after_attempt_1, good])

    run_loop(app, provider, max_fix_attempts=3)

    second_prompt = provider.requests[1].prompt
    assert "<Rate>600pm</Rate>" in second_prompt
    assert 'header="ok"' in second_prompt
    assert 'rate="bad"' in second_prompt
    assert "header-set" not in second_prompt.lower().replace("am-addheader-header-set", "")


# ---------------------------------------------------------------- CP8-T14


def test_CP8_T14_a_proxy_that_passes_first_time_never_calls_the_ai(tmp_path: Path) -> None:
    """[CP8-T14] Nothing to fix: no AI request, no attempts, battery as CP7 decided."""
    app = write_app(tmp_path, rate_ok=True, header_ok=True)
    provider = QueueProvider([])
    runner = ContentRunner()

    loop = run_loop(app, provider, runner=runner, max_fix_attempts=3)

    assert provider.calls == 0
    assert loop.attempts == ()
    assert loop.result.type.value == "battery"
    assert loop.result.failed == 0


# ---------------------------------------------------------------- CP8-T15


@pytest.mark.parametrize("value", ["-1", "three"])
def test_CP8_T15_a_bad_max_fix_attempts_value_stops_with_a_clear_message(
    tmp_path: Path, run_cli: Any, value: str
) -> None:
    """[CP8-T15] A negative or non-numeric --max-fix-attempts exits non-zero with one clear line, no proxy
    output written."""
    exports = tmp_path / "exports"
    write_rate_limit_bundle(exports)
    out = tmp_path / "out"

    result = run_cli(["migrate", str(exports), "--out", str(out), "--max-fix-attempts", value])

    assert result.code != 0
    assert "max-fix-attempts" in result.err
    assert "Traceback" not in result.err
    assert len(result.err.splitlines()) <= 1
    assert not out.exists() or not any(out.iterdir())


# ---------------------------------------------------------------- CP8-T16


def test_CP8_T16_the_fix_loop_runs_with_no_network_no_api_key_and_no_java(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[CP8-T16] The T07 scenario (three attempts ending in a pass) needs no Java/Maven/Mule, no API key and
    opens no socket beyond loopback; the claude provider module is never even imported."""
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    (tmp_path / "empty-path").mkdir()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sys.modules.pop("a2m.ai.claude", None)

    app = write_app(tmp_path, rate_ok=False, header_ok=False)
    answers = [
        no_op_answer(),
        marker_fix(app.base_text, rate_ok=False, header_ok=True),
        marker_fix(app.base_text, rate_ok=True, header_ok=True),
    ]
    provider = QueueProvider(list(answers))

    loop = run_loop(app, provider, max_fix_attempts=3)

    assert loop.result.type.value == "battery"
    assert "a2m.ai.claude" not in sys.modules


# ---------------------------------------------------------------- CP8-T17


def test_CP8_T17_a_worse_fix_is_discarded_and_the_next_try_starts_from_the_best_version_so_far(
    tmp_path: Path,
) -> None:
    """[CP8-T17] A fix that makes more tests fail (1->2) is thrown away at once; the next request still carries
    the pre-attempt-1 flow and diff, not attempt 1's; a queue of only worse fixes leaves the project exactly as
    the generator made it and keeps the initial diff for review."""
    app = write_app(tmp_path, rate_ok=False, header_ok=True)
    worse = marker_fix(app.base_text, rate_ok=False, header_ok=False)
    good = marker_fix(app.base_text, rate_ok=True, header_ok=True)
    provider = QueueProvider([worse, good])

    loop = run_loop(app, provider, max_fix_attempts=3)

    assert (loop.attempts[0].failing_before, loop.attempts[0].failing_after) == (1, 2)
    assert loop.attempts[0].helped is False
    assert "more" in loop.attempts[0].reason.lower() or "worse" in loop.attempts[0].reason.lower()
    assert loop.attempts[1].helped is True
    assert loop.result.type.value == "battery"

    # Separately: three worse fixes in a row leave the project byte-identical to the generator's output.
    app2 = write_app(tmp_path, "rate-limit-proxy-2", rate_ok=False, header_ok=True)
    before2 = flow_text(app2.app_dir)
    worse2 = marker_fix(app2.base_text, rate_ok=False, header_ok=False)
    provider2 = QueueProvider([worse2, worse2, worse2])
    loop2 = run_loop(app2, provider2, max_fix_attempts=3)
    assert flow_text(app2.app_dir) == before2
    assert loop2.result.type.value == "failed"
    kept_diff = failing_cases(loop2.result)
    assert kept_diff and "429" in kept_diff[0].diff
