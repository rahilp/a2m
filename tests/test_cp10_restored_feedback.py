"""CP10 adversarial round 13: nothing a2m restored goes back to the AI.

A fix answer is restored (each placeholder written back as its value, spelled for where it stands) before it is
checked, built and deployed. A refusal that quotes the restored fix, or a build or deploy error that quotes the
restored code, becomes the attempt's reason, and the next fix request tells the AI that reason as "previous".

* CP10-X74 - end to end through ``run_with_fixes``: a policy value with ``$``, a quote or a backslash, and a first
  answer whose added guard a2m refuses while quoting it (with ``!r``, after its DataWeave escapes). No field of any
  request holds a piece of the value; the second request still says the guard was refused, quoting its placeholder.
* CP10-X75 - the same when the fix is written and the build or the deploy fails with an error that quotes the
  restored code (Maven quoting the XML line, Mule's "Caused by" quoting the DataWeave). An error that spells the
  value in a way a2m does not know is not repeated: the AI gets a2m's own words for what happened.
* CP10-X76 - the sweep replaces every spelling of a value (raw, XML, JSON, DataWeave and JavaScript strings of
  either quote, Java, Python repr, URL-encoded, and each of those repr-quoted), computed here independently, and
  ``holds_value`` notices a spelling the sweep does not know.
* CP10-X77 - a reason cut short in the middle of the value never shows the part before the cut: the AI's text is
  built from the whole reason.
"""

from __future__ import annotations

import dataclasses
import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

from a2m.ai.placeholders import Placeholders

CORE = "http://www.mulesoft.org/schema/mule/core"
FLOW_REL = "src/main/mule/proxy.xml"
FLOW_OPEN = '<flow name="proxy-default">'
SECRETS = ("S3cr3tKey$2026'x", "Pa$$w0rd123", 'Qz7Lm"Bq4ck\\sl9shW$Hk9Wx')
WINDOW = 6


def _pieces(secret: str) -> set[str]:
    """Every 6-character piece of ``secret`` and every run of 5 or more letters and digits in it."""
    found = {secret[i : i + WINDOW] for i in range(len(secret) - WINDOW + 1)}
    found |= {run for run in re.findall(r"[A-Za-z0-9]+", secret) if len(run) >= 5}
    return found


def _fields(request: Any) -> dict[str, str]:
    """Every text field of an AI request."""
    return {
        item.name: value for item in dataclasses.fields(request) if isinstance(value := getattr(request, item.name), str)
    }


def _leaks(requests: list[Any], secret: str) -> list[str]:
    pieces = _pieces(secret)
    return [
        f"request {number} field {name}: {piece!r}"
        for number, request in enumerate(requests, 1)
        for name, text in _fields(request).items()
        for piece in sorted(pieces)
        if piece in text
    ]


def _app(tmp_path: Path, secret: str) -> tuple[Any, Path]:
    """The CP8 rate-limit-proxy app with AM-AddHeader setting X-Env to ``secret``, marked wrong so its tests fail."""
    import test_cp8_fix_loop as cp8

    from a2m.generator import generate_project
    from a2m.parser import read_bundle

    bundle_dir = cp8.write_rate_limit_bundle(tmp_path / "bundle", "rate-limit-proxy")
    policy = bundle_dir / "apiproxy" / "policies" / "AM-AddHeader.xml"
    escaped = secret.replace("&", "&amp;").replace("<", "&lt;").replace("'", "&apos;").replace('"', "&quot;")
    policy.write_text(policy.read_text(encoding="utf-8").replace(">prod<", f">{escaped}<"), encoding="utf-8")
    bundle = read_bundle(bundle_dir)
    app_dir = tmp_path / "app" / "mule-app"
    generate_project(bundle, app_dir)
    flow = app_dir / FLOW_REL
    flow.write_text(cp8.marked(flow.read_text(encoding="utf-8"), rate_ok=False, header_ok=True), encoding="utf-8")
    return bundle, app_dir


def _shown(prompt: str) -> tuple[str, str]:
    """The placeholder of X-Env's value in the shown policy, and the shown proxy.xml."""
    token = re.search(r'<Header name="X-Env">(«v\d+»)</Header>', prompt)
    block = re.search(r"### src/main/mule/proxy\.xml\n\n```xml\n(.*?)\n```", prompt, re.DOTALL)
    assert token is not None and block is not None
    return token.group(1), block.group(1)


class _Provider:
    """First a fix that adds ``added`` (``{token}`` is X-Env's placeholder) at the start of the flow, then declines."""

    def __init__(self, added: str) -> None:
        self.added = added
        self.requests: list[Any] = []

    def complete(self, request: Any) -> str:
        self.requests.append(request)
        if len(self.requests) > 1:
            return json.dumps({"status": "cannot_fix", "reason": "done"})
        token, block = _shown(request.prompt)
        new = block.replace(FLOW_OPEN, FLOW_OPEN + self.added.format(token=token), 1)
        assert new != block
        return json.dumps({"status": "fixed", "files": {FLOW_REL: new}, "notes": "n", "confidence": "high"})


def _run(bundle: Any, app_dir: Path, provider: Any, runner: Any) -> Any:
    import test_cp8_fix_loop as cp8

    from a2m.verify.fix_loop import run_with_fixes
    from a2m.verify.mock_backend import MockBackend

    mock = MockBackend(default_status=200, default_headers=dict(cp8.JSON_HEADERS), default_body=b'{"id":7}')
    mock.start()
    try:
        return run_with_fixes(bundle, app_dir, runner=runner, provider=provider, max_fix_attempts=2, backend=mock)
    finally:
        mock.stop()


def _previous(prompt: str) -> str:
    start = prompt.find("Attempt 1 was")
    assert start >= 0, "the second request does not say what came of the first attempt"
    return prompt[start : prompt.find("\n", start)]


REFUSED_GUARD = (
    "<choice><when expression=\"#[upper(attributes.headers.'{header}') == '{{token}}']\">"
    '<set-variable variableName="ok" value="#[true]"/></when></choice>'
)


# ------------------------------------------------------------------------------------------------ X74: refused guard


@pytest.mark.parametrize("secret", SECRETS, ids=["dollar-quote", "dollars", "dquote-backslash"])
def test_CP10_X74_a_refused_guard_quoting_the_restored_value_never_reaches_the_next_request(
    tmp_path: Path, secret: str
) -> None:
    """[CP10-X74] The guard a2m refuses quotes the value as restored (DataWeave escapes, then ``!r``); the second
    request names the refusal with the value's placeholder and holds no piece of the value in any field."""
    import test_cp8_fix_loop as cp8

    bundle, app_dir = _app(tmp_path, secret)
    provider = _Provider(REFUSED_GUARD.format(header="x-tag"))
    loop = _run(bundle, app_dir, provider, cp8.ContentRunner())

    assert len(provider.requests) == 2
    assert "has a guard" in loop.attempts[0].reason  # the scenario really quotes the restored guard
    assert _leaks(provider.requests, secret) == []
    previous = _previous(provider.requests[1].prompt)
    token, _ = _shown(provider.requests[0].prompt)
    assert "has a guard" in previous and token in previous


# ------------------------------------------------------------------------------------------------ X75: build, deploy


PROBE = '<set-variable variableName="cp10Probe" value="#[\'{token}\']"/>'


class _FailingAfterFixRunner:
    """The CP8 content runner until the fix is written; then the build or deploy fails with an error quoting the
    probe's restored code: ``build`` (Maven quotes the XML line), ``deploy`` (Mule's Caused by quotes the DataWeave
    with ``!r``), ``deploy-unicode`` (the DataWeave with every character outside letters and digits as \\uXXXX)."""

    def __init__(self, how: str) -> None:
        import test_cp8_fix_loop as cp8

        self.how = how
        self.inner = cp8.ContentRunner()
        self.starts = 0

    def start(self, app: Any, *, backend_url: str) -> Any:
        from a2m.verify.mule import BuildError, DeployError

        self.starts += 1
        text = (Path(app.app_dir) / FLOW_REL).read_text(encoding="utf-8")
        if "cp10Probe" not in text:
            return self.inner.start(app, backend_url=backend_url)
        line = next(line for line in text.splitlines() if "cp10Probe" in line).strip()
        expression = next(
            node.get("value", "")
            for node in ET.fromstring(text).iter(f"{{{CORE}}}set-variable")
            if node.get("variableName") == "cp10Probe"
        )
        if self.how == "build":
            raise BuildError(f"mvn package failed: [ERROR] {FLOW_REL}: invalid element {line}", line)
        quoted = repr(expression)
        if self.how == "deploy-unicode":
            quoted = "".join(char if char.isalnum() else f"\\u{ord(char):04x}" for char in expression)
        log = (
            f"ERROR Failed to deploy artifact [{app.name}]\n"
            f"Caused by: org.mule.runtime.api.el.ExpressionExecutionException: while evaluating {quoted}\n"
        )
        raise DeployError(f"{app.name} failed to deploy", log)


@pytest.mark.parametrize("how", ["build", "deploy", "deploy-unicode"])
@pytest.mark.parametrize("secret", SECRETS, ids=["dollar-quote", "dollars", "dquote-backslash"])
def test_CP10_X75_a_build_or_deploy_error_quoting_the_restored_code_never_reaches_the_next_request(
    tmp_path: Path, secret: str, how: str
) -> None:
    """[CP10-X75] The fix is written, and the build or deploy fails with an error quoting the restored code: the
    second request holds no piece of the value; an error a2m cannot sweep is replaced by a2m's own words."""
    bundle, app_dir = _app(tmp_path, secret)
    provider = _Provider(PROBE)
    runner = _FailingAfterFixRunner(how)
    loop = _run(bundle, app_dir, provider, runner)

    assert len(provider.requests) == 2
    assert runner.starts >= 2  # the fix passed the checks, was written and built
    assert "did not build or start" in loop.attempts[0].reason
    assert _leaks(provider.requests, secret) == []
    previous = _previous(provider.requests[1].prompt)
    assert "the fixed project did not build or start" in previous
    if how == "deploy-unicode":
        assert "does not repeat the rest of the reason" in previous


# ------------------------------------------------------------------------------------------------ X76: the sweep


def _string_content(value: str, quote: str, *, dollar: bool = False) -> str:
    out = []
    for char in value:
        if char == "\\":
            out.append("\\\\")
        elif char == quote:
            out.append("\\" + char)
        elif dollar and char == "$":
            out.append("\\$")
        elif char == "\n":
            out.append("\\n")
        elif char == "\t":
            out.append("\\t")
        else:
            out.append(char)
    return "".join(out)


def _xml(value: str, quote: str | None) -> str:
    text = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if quote == '"':
        text = text.replace('"', "&quot;")
    elif quote == "'":
        text = text.replace("'", "&apos;")
    return text


def _spellings(value: str) -> dict[str, str]:
    base = {
        "raw": value,
        "xml-text": _xml(value, None),
        "xml-attr-double": _xml(value, '"'),
        "xml-attr-single": _xml(value, "'"),
        "json": json.dumps(value)[1:-1],
        "dw-single": _string_content(value, "'", dollar=True),
        "dw-double": _string_content(value, '"', dollar=True),
        "js-single": _string_content(value, "'"),
        "js-double": _string_content(value, '"'),
        "java": _string_content(value, '"'),
        "python-repr": repr(value)[1:-1],
        "url": urllib.parse.quote(value, safe=""),
    }
    out = dict(base)
    for name, form in base.items():
        out[f"{name}-repr"] = repr(form)[1:-1]
        out[f"{name}-repr-in-single-quotes"] = _string_content(form, "'")
    return out


SWEPT = ("Pa$$w0rd123", "S3cr3tKey$2026'x", 'Qz7Lm"Bq4ck\\sl9shW$Hk9Wx', "tab\there&<more>")


def _table(value: str) -> tuple[Placeholders, str]:
    table = Placeholders(["f", "v"])
    attribute = _xml(value, '"').replace("\t", "&#9;")
    shown = table.mule({"a.xml": f'<mule xmlns="{CORE}"><flow name="f"><set-variable variableName="v" value="{attribute}"/></flow></mule>'})["a.xml"]
    assert shown is not None
    token = next(t for t, v in table.values.items() if v == value)
    return table, token


@pytest.mark.parametrize("value", SWEPT, ids=["dollars", "dollar-quote", "dquote-backslash", "tab-markup"])
def test_CP10_X76_the_sweep_replaces_every_escaped_spelling_of_a_value(value: str) -> None:
    """[CP10-X76] Every spelling of a value a2m showed, in any text, becomes its placeholder."""
    table, token = _table(value)
    missed = {
        name: swept
        for name, form in _spellings(value).items()
        if (swept := table.sweep(f"quoted: [{form}] here")) != f"quoted: [{token}] here"
    }
    assert missed == {}


def test_CP10_X76_holds_value_notices_a_spelling_the_sweep_does_not_know() -> None:
    """[CP10-X76] A text that spells the value in a way the sweep does not know (every other character as \\uXXXX,
    or as %XX) still holds it; a text without it does not."""
    value = "S3cr3tKey$2026'x"
    table, _ = _table(value)
    unicode_spelled = "".join(char if char.isalnum() else f"\\u{ord(char):04x}" for char in value)
    percent_spelled = "".join(char if char.isalnum() else f"%{ord(char):02X}" for char in value).replace("3t", "%33t")
    for text in (f"Caused by: {unicode_spelled}", f"at {percent_spelled}!"):
        assert table.holds_value(table.sweep(text)), text
    assert not table.holds_value("Caused by: the expression is not valid near «v1» (line 3)")


# ------------------------------------------------------------------------------------------------ X77: the cut


def test_CP10_X77_a_reason_cut_in_the_middle_of_the_value_shows_no_part_of_it(tmp_path: Path) -> None:
    """[CP10-X77] The recorded reason is cut short at 500 characters; when the cut falls inside the quoted value, the
    second request still holds no piece of it (its text is built from the whole reason)."""
    import test_cp8_fix_loop as cp8

    secret = "S3cr3tKey$2026'x"
    bundle, app_dir = _app(tmp_path / "probe", secret)
    provider = _Provider(REFUSED_GUARD.format(header="x-tag"))
    loop = _run(bundle, app_dir, provider, cp8.ContentRunner())
    reason = loop.attempts[0].reason
    at = reason.find("S3cr3tKey")
    assert at > 0
    # A longer header name moves the value so that the cut (at 500) falls 8 characters into it.
    header = "x-tag" + "a" * (500 - 8 - at)
    bundle, app_dir = _app(tmp_path / "cut", secret)
    provider = _Provider(REFUSED_GUARD.format(header=header))
    loop = _run(bundle, app_dir, provider, cp8.ContentRunner())

    assert loop.attempts[0].reason.endswith("S3cr3tKe...")
    assert len(provider.requests) == 2
    assert _leaks(provider.requests, secret) == []


# ------------------------------------------------------------------------------------------------ round 15
#
# * CP10-X78 - end to end through ``run_with_fixes``: a credential-shaped value (``Bearer``/``Basic``, with a
#   character a token cannot hold inside, and one of token characters only) set on a header whose battery case passes
#   (so no failing diff quotes it), a fix that holds it by placeholder, and a build or deploy error quoting the restored
#   code. The masker would keep the token's first characters and everything after the first character a token cannot
#   hold; the AI-facing reason is built from the unmasked text, so no piece and no masked prefix reaches any request,
#   while everything recorded stays masked.
# * CP10-X79 - a reason quoting a credential the table does not know is not repeated to the AI (its masked form would
#   still show its first characters and its tail).
# * CP10-X80 - the sweep leaves existing placeholders alone: sweeping a swept text changes nothing.
# * CP10-X81 - the credential canary check catches a2m whose fix loop builds the AI's text from masked results.
# * CP10-X82 - the check's guard scenario that is refused for another reason is "could not check", never a pass.

CREDENTIAL_SECRETS = (
    "Bearer abcd1234efgh5678:SecretPart9876",
    "Basic dXNlcjpwYXNz$TopSecretTail42",
    "Bearer QWxhZGRpbjpvcGVuc2VzYW1l",
)
CREDENTIAL_IDS = ["bearer-colon", "basic-dollar", "bearer-token-only"]


def _credential_marks(secret: str) -> set[str]:
    """What of credential ``secret`` may never reach a request: every 6-character piece of its token (the scheme word
    aside) and the masked form of its first 4 characters."""
    token = secret.split(" ", 1)[1]
    return {token[i : i + WINDOW] for i in range(len(token) - WINDOW + 1)} | {token[:4] + "***"}


def _credential_app(tmp_path: Path, secret: str, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Path]:
    """The CP8 rate-limit-proxy app with AM-AddHeader also setting X-Auth to ``secret``; the fake app forwards X-Auth
    as the policy says, so that battery case passes and no failing diff quotes the value."""
    import test_cp8_fix_loop as cp8

    from a2m.generator import generate_project
    from a2m.parser import read_bundle

    forward = cp8.ContentHandle._forward

    def forwarding(self: Any, request: Any, extra: Any) -> Any:
        extra = dict(extra)
        if extra:
            extra["X-Auth"] = secret
        return forward(self, request, extra)

    monkeypatch.setattr(cp8.ContentHandle, "_forward", forwarding)
    bundle_dir = cp8.write_rate_limit_bundle(tmp_path / "bundle", "rate-limit-proxy")
    policy = bundle_dir / "apiproxy" / "policies" / "AM-AddHeader.xml"
    escaped = secret.replace("&", "&amp;").replace("<", "&lt;")
    policy.write_text(
        policy.read_text(encoding="utf-8").replace(
            '<Header name="X-Env">prod</Header>', f'<Header name="X-Env">prod</Header><Header name="X-Auth">{escaped}</Header>'
        ),
        encoding="utf-8",
    )
    bundle = read_bundle(bundle_dir)
    app_dir = tmp_path / "app" / "mule-app"
    generate_project(bundle, app_dir)
    flow = app_dir / FLOW_REL
    flow.write_text(cp8.marked(flow.read_text(encoding="utf-8"), rate_ok=False, header_ok=True), encoding="utf-8")
    return bundle, app_dir


class _AuthProvider(_Provider):
    """:class:`_Provider` with X-Auth's placeholder as ``{token}``."""

    def complete(self, request: Any) -> str:
        self.requests.append(request)
        if len(self.requests) > 1:
            return json.dumps({"status": "cannot_fix", "reason": "done"})
        token = re.search(r'<Header name="X-Auth">(«v\d+»)</Header>', request.prompt)
        _, block = _shown(request.prompt)
        assert token is not None
        new = block.replace(FLOW_OPEN, FLOW_OPEN + self.added.format(token=token.group(1)), 1)
        return json.dumps({"status": "fixed", "files": {FLOW_REL: new}, "notes": "n", "confidence": "high"})


@pytest.mark.parametrize("how", ["build", "deploy"])
@pytest.mark.parametrize("secret", CREDENTIAL_SECRETS, ids=CREDENTIAL_IDS)
def test_CP10_X78_a_credential_quoted_by_a_build_or_deploy_error_never_reaches_the_next_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, secret: str, how: str
) -> None:
    """[CP10-X78] The build or deploy error quotes the restored credential; the masker alone would leave its first 4
    characters and its tail. No piece and no masked prefix of it reaches any request; the second request quotes the
    error with the value's placeholder; what is recorded (the attempt's reason, the result) stays masked."""
    bundle, app_dir = _credential_app(tmp_path, secret, monkeypatch)
    provider = _AuthProvider(PROBE)
    runner = _FailingAfterFixRunner(how)
    loop = _run(bundle, app_dir, provider, runner)

    assert len(provider.requests) == 2
    assert runner.starts >= 2  # the fix was written, and its start failed quoting the restored code
    first = provider.requests[0].prompt
    token = re.search(r'<Header name="X-Auth">(«v\d+»)</Header>', first)
    assert token is not None
    failing = first[first.find("- test ") :]
    assert not any(mark in failing for mark in _credential_marks(secret))  # no failing diff quotes it
    leaks = [
        f"request {number} field {name}: {mark!r}"
        for number, request in enumerate(provider.requests, 1)
        for name, text in _fields(request).items()
        for mark in sorted(_credential_marks(secret))
        if mark in text
    ]
    assert leaks == []
    previous = _previous(provider.requests[1].prompt)
    assert "the fixed project did not build or start" in previous and token.group(1) in previous
    raw_token = secret.split(" ", 1)[1]
    recorded = json.dumps([loop.result.to_json_data(), [attempt.to_json_data() for attempt in loop.attempts]])
    assert raw_token not in recorded
    assert "did not build or start" in loop.attempts[0].reason and "*** (" in loop.attempts[0].reason


@pytest.mark.parametrize(
    "credential",
    ["Bearer Zx9qUnknownTok3n:TailPart7788", "Basic WnhRdW5rbm93bg$TailPart7788", "eyJhbGciOiJub25l.eyJzdWIiOjF9.c2ln$TailPart7788"],
    ids=["bearer", "basic", "jwt"],
)
def test_CP10_X79_a_reason_quoting_a_credential_the_table_does_not_know_is_not_repeated(credential: str) -> None:
    """[CP10-X79] A deploy error quoting a credential the table does not know: masking alone would show its first
    characters and the part after the first character a token cannot hold, so the AI gets a2m's own words."""
    from a2m.verify.fix_loop import WITHHELD, _FixLoop
    from a2m.verify.masking import Masker

    loop = _FixLoop.__new__(_FixLoop)
    loop.table = Placeholders()
    loop.masker = Masker()
    reason = (
        "the fixed project did not build or start (app failed to deploy: while evaluating "
        f"\"#['{credential}']\"); it was undone"
    )
    told = loop._for_ai(reason)
    assert "TailPart7788" not in told and credential[7:11] not in told
    assert told == f"the fixed project did not build or start: {WITHHELD}"


def test_CP10_X80_the_sweep_leaves_placeholders_alone_and_is_idempotent() -> None:
    """[CP10-X80] A value spelled like a placeholder's inside (``v10``) never rewrites ``«v10»``; sweeping a swept text
    changes nothing."""
    table = Placeholders()
    assert table.token("v10") == "«v1»"
    for number in range(2, 10):
        table.token(f"filler-value-{number}")
    assert table.token("value8abc") == "«v10»"
    for text in (
        "guard == 'value8abc'",
        "v10 and value8abc and «v10» and «v1»",
        "it compared filler-value-3 with 'value8abc' (v10)",
    ):
        once = table.sweep(text)
        assert table.sweep(once) == once, (text, once)
        assert "««" not in once and "»»" not in once
    assert table.sweep("guard == '«v10»'") == "guard == '«v10»'"


def _check_variant(tmp_path: Path, patch_file: str, appended: str) -> Any:
    """Run the credential canary check on a copy of a2m with ``appended`` added to the end of ``patch_file``."""
    import os
    import shutil
    import subprocess

    repo = Path(__file__).resolve().parents[1]
    package = tmp_path / "variant" / "a2m"
    shutil.copytree(repo / "a2m", package, ignore=shutil.ignore_patterns("__pycache__"))
    target = package / patch_file
    target.write_text(target.read_text(encoding="utf-8") + appended, encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if key not in ("FORCE_COLOR", "PY_COLORS", "ANTHROPIC_API_KEY")}
    return subprocess.run(
        [str(repo / ".venv" / "bin" / "python"), str(repo / "tools" / "checks" / "credential_canary.py"), str(package)],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


MASKED_FIRST = '''

_a2m_variant_unmasked = verify_proxy_unmasked


def verify_proxy_unmasked(bundle, app_dir, *, runner, masker, **options):  # type: ignore[no-redef]
    """The leaking variant: the fix loop gets the result already masked, as verify_proxy returns it."""
    return masker.mask_result(_a2m_variant_unmasked(bundle, app_dir, runner=runner, masker=masker, **options))
'''


def test_CP10_X81_the_check_catches_a_fix_loop_that_sweeps_masked_text(tmp_path: Path) -> None:
    """[CP10-X81] a2m whose fix loop builds what the AI is told from masked results (the round 15 leak): the check
    reports the credential canaries' tails and masked prefixes reaching the second fix request (exit 1)."""
    proc = _check_variant(tmp_path, "verify/harness.py", MASKED_FIRST)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    found = [line for line in proc.stdout.splitlines() if "leak: canary cred-" in line]
    assert {re.search(r"canary (cred-[a-z]+)", line).group(1) for line in found if re.search(r"canary (cred-[a-z]+)", line)} == {
        "cred-bearer", "cred-basic", "cred-token", "cred-jwt", "cred-apikey"
    }, proc.stdout
    assert all("fix request 2" in line for line in found), proc.stdout


REFUSED_OTHERWISE = '''

_a2m_variant_check_new = _check_new


def _check_new(element, side=REQUEST):  # type: ignore[no-redef]
    """The variant: a <choice> is refused before its condition is ever read."""
    if element.tag == CHOICE_TAG:
        return "adds a <choice> element, which this variant refuses"
    return _a2m_variant_check_new(element, side)
'''


def test_CP10_X82_a_guard_scenario_refused_for_another_reason_cannot_pass(tmp_path: Path) -> None:
    """[CP10-X82] When a2m refuses the guard scenario's first fix for another reason, the refused-guard path was never
    tested: the check says it could not check (exit 2), never that it passed."""
    proc = _check_variant(tmp_path, "verify/fix_loop.py", REFUSED_OTHERWISE)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "guard scenario" in proc.stdout and "not refused for its guard" in proc.stdout, proc.stdout


# ------------------------------------------------------------------------------------------------ round 16
#
# * CP10-X83 - a failing-test diff of two JSON bodies shows the AI no number and no key of the body: each number is a
#   number placeholder (the same as the same JSON number in the policy), a key with a digit or any other character is
#   a placeholder, true and false are placeholders (data) and null stays.
# * CP10-X84 - what stays readable in a diff: a2m's words, status codes and counts, header and query names, a plain
#   word key; an array index is a number placeholder.
# * CP10-X85 - a JSON key holding a line end, and a key path holding a masked value, show no piece of the key.
# * CP10-X86 - end to end through ``run_with_fixes`` (golden run): a recorded body with a number and a key the app does
#   not answer; no request holds either, and the prompt shows their lines by placeholder.
# * CP10-X87 - a build or deploy reason that starts with the proxy's name is told to the AI even when a value's letters
#   (the base path ``/orders``) stand inside that name; a value anywhere else still withholds it.
# * CP10-X88 - end to end: proxy ``orders-api`` with base path ``/orders``, a fix whose build or deploy fails; request
#   2 quotes the Maven or Mule cause and still holds no piece of the policy's value.
# * CP10-X89 - a value with two blanks or a tab is swept before the blanks are collapsed, and a value with one blank
#   is found where the text has two.
# * CP10-X90 - the check fails (could not check, exit 2) on a2m that withholds every build and deploy reason.
# * CP10-X91 - the check catches a2m whose diff shows JSON numbers and keys (exit 1, json-num and json-key).


def _body_diff(expected: dict[str, Any], actual: dict[str, Any]) -> str:
    from a2m.verify import compare

    return "\n".join(compare.body_diffs(json.dumps(expected).encode(), json.dumps(actual).encode()))


def _unplaced(text: str) -> str:
    """``text`` without its placeholders."""
    return re.sub(r"«[vn]\d+»", " ", text)


def test_CP10_X83_a_json_body_diff_shows_no_number_and_no_key_of_the_body() -> None:
    """[CP10-X83] The reviewers' repro: ``body field pin: expected 482193, actual 0`` and a key ``CNRY-JSONKEY-7f3a9b``
    reached the AI as they were. Every number is a number placeholder, the same one as the policy's JSON number; a key
    with a digit is a placeholder; true and false are placeholders and null stays."""
    table = Placeholders(["AM-Set"])
    policy = table.apigee(
        '<AssignMessage name="AM-Set"><Set><Payload contentType="application/json">'
        '{"pin": 482193, "card": 4111111111111111, "CNRY-JSONKEY-7f3a9b": "x"}</Payload></Set></AssignMessage>'
    )
    assert policy is not None and "482193" not in policy
    numbers = re.findall(r"«n\d+»", policy)
    assert len(numbers) == 2, policy
    text = _body_diff(
        {"pin": 482193, "card": 4111111111111111, "CNRY-JSONKEY-7f3a9b": "x", "ok": True, "gone": None, "neg": -75.5e3},
        {"pin": 0, "ok": False, "gone": 7, "neg": 1},
    )
    shown = table.diff(text)
    assert "482193" in text and "CNRY-JSONKEY-7f3a9b" in text  # the compare lines really carry them
    assert not re.search(r"\d", _unplaced(shown)), shown
    for piece in ("CNRY", "JSONKEY", "7f3a9b", "4111", "75", "true", "false"):
        assert piece not in shown, (piece, shown)
    assert f"body field pin: expected {numbers[0]}, actual «n" in shown, shown
    assert f"body field card: expected {numbers[1]}, actual missing" in shown, shown
    assert re.search(r"body field gone: expected null, actual «n\d+»", shown), shown
    assert re.search(r"body field ok: expected «v\d+», actual «v\d+»", shown), shown
    assert re.search(r"body field «v\d+»: expected '«v\d+»', actual missing", shown), shown


def test_CP10_X84_a2m_words_status_codes_counts_and_names_stay_and_an_index_is_a_placeholder() -> None:
    """[CP10-X84] a2m's own lines stay readable: status codes, counts of calls and fields, header and query names, an
    HTTP method, a plain word key, the ``call N`` prefix; an array index and a number in the HTTP client's error are
    number placeholders."""
    table = Placeholders()
    lines = [
        "- test AM-Set-header (policy AM-Set, AssignMessage):",
        "    call 2 status: expected 200, actual 500",
        "    backend calls: expected 1, got 0",
        "    body: 3 more fields differ",
        "    header X-Served-By: expected 'a', actual missing",
        "    backend call 1 header x-api-version: expected absent, actual '2'",
        "    backend call 1: expected GET /orders/42?region=eu, got POST /orders",
        "    backend call 1 query region: expected 'eu', actual absent",
        "    call 1: no valid response from the app: ConnectionRefusedError: [Errno 111] Connection refused",
        "    " + _body_diff({"items": [{"qty": 3}]}, {"items": [{"qty": 4}]}),
    ]
    shown = table.diff("\n".join(lines)).splitlines()
    assert shown[:4] == lines[:4], shown
    assert shown[4] == "    header X-Served-By: expected '«v1»', actual missing", shown
    assert shown[5] == "    backend call 1 header x-api-version: expected absent, actual '«v2»'", shown
    assert re.fullmatch(r"    backend call 1: expected GET «v\d+»\?region=«v\d+», got POST «v\d+»", shown[6]), shown
    assert re.fullmatch(r"    backend call 1 query region: expected '«v\d+»', actual absent", shown[7]), shown
    assert re.fullmatch(
        r"    call 1: no valid response from the app: ConnectionRefusedError: \[Errno «n\d+»\] Connection refused",
        shown[8],
    ), shown
    assert re.fullmatch(r"    body field items\[«n\d+»\]\.qty: expected «n\d+», actual «n\d+»", shown[9]), shown


def test_CP10_X85_a_key_with_a_line_end_or_a_masked_value_shows_no_piece_of_it() -> None:
    """[CP10-X85] A JSON key holding a line end splits its diff line; the parts are read as one line, so no part of
    the key shows. A masked value in a key path (``eyJh*** (40 chars)``) is a placeholder whole."""
    table = Placeholders()
    text = _body_diff({"a\nCNRYLINEKEY9x\nb": 1, "tokens": {"k": 2}}, {"tokens": {"k": 3}})
    assert "CNRYLINEKEY9x" in text and text.count("\n") >= 2
    shown = table.diff(text + "\nbody field tokens.eyJhbGc*** (40 chars): expected 'x', actual missing")
    assert "CNRYLINEKEY9x" not in shown and "eyJh" not in shown and "40" not in _unplaced(shown), shown
    assert re.search(r"body field «v\d+»: expected «n\d+», actual missing", shown), shown
    assert re.search(r"body field tokens\.«v\d+»: expected '«v\d+»', actual missing", shown), shown


def test_CP10_X86_a_recorded_body_number_and_key_never_reach_a_fix_request(tmp_path: Path) -> None:
    """[CP10-X86] Golden run of the CP8 secret proxy whose recorded answer holds a PIN, an account number in a nested
    object and a key with digits that the app does not answer: the run's diff carries them, no field of any request
    holds them, and the prompt shows their lines by placeholder."""
    import test_cp8_fix_checks as cp8c

    app = cp8c.secret_app(tmp_path)
    golden = cp8c.write_secret_golden(tmp_path / "golden")
    recording = golden / "secret-proxy" / "partner-call.json"
    exchange = json.loads(recording.read_text(encoding="utf-8"))
    body = {"ok": True, "pin": 482193071, "CNRYKEY9x7Q": "v", "nested": {"acct": 5512345678}}
    exchange["calls"][0]["response"]["body"] = json.dumps(body)
    recording.write_text(json.dumps(exchange), encoding="utf-8")
    provider = cp8c.ScriptedProvider([lambda request: json.dumps({"status": "cannot_fix", "reason": "cp10-x86"})])

    loop = cp8c.run(app, provider, cp8c.CanaryRunner(), golden=golden)

    diffs = "\n".join(case.diff for case in loop.result.cases if not case.passed)
    for planted in ("482193071", "CNRYKEY9x7Q", "5512345678"):
        assert planted in diffs, (planted, diffs)  # the failing diff really carries it
        for request in provider.requests:
            for name, text in _fields(request).items():
                assert planted not in text, (planted, name)
    prompt = provider.requests[0].prompt
    assert re.search(r"body field pin: expected «n\d+», actual missing", prompt), prompt
    assert re.search(r"body field nested: expected \{\"«v\d+»\": «n\d+»\}, actual missing", prompt), prompt
    assert re.search(r"body field «v\d+»: expected '«v\d+»', actual missing", prompt), prompt


def _loop_for(table: Placeholders) -> Any:
    from a2m.verify.fix_loop import _FixLoop
    from a2m.verify.masking import Masker

    loop = _FixLoop.__new__(_FixLoop)
    loop.table = table
    loop.masker = Masker()
    return loop


ORDERS_REASONS = (
    (
        "the fixed project did not build or start (orders-api failed to deploy on Mule runtime 4.9.0: "
        "org.mule.runtime.core.api.config.ConfigurationException: Invalid content was found starting with element "
        "'set-variable'); it was undone"
    ),
    (
        "the fixed project did not build or start (orders-api: build failed: mvn package failed: [ERROR] proxy.xml: "
        "cvc-complex-type.2.4.a: Invalid content was found starting with element 'set-variable'); it was undone"
    ),
)


@pytest.mark.parametrize("reason", ORDERS_REASONS, ids=["deploy", "build"])
def test_CP10_X87_a_reason_starting_with_the_proxy_name_is_told_when_a_value_only_stands_inside_it(reason: str) -> None:
    """[CP10-X87] The reviewer's repro (/tmp/hs16/held.py): the base path ``/orders`` stands inside the proxy name
    ``orders-api`` that every build and deploy reason starts with, so the AI was never told why. The name is in the
    prompt anyway: the reason is told whole. The base path anywhere else (escaped, joined to a word), or a value that
    holds the name spelled another way, still withholds it."""
    from a2m.verify.fix_loop import WITHHELD

    table = Placeholders(["default", "AM-Set", "orders-api"])
    shown = table.apigee(
        '<ProxyEndpoint name="default"><HTTPProxyConnection><BasePath>/orders</BasePath></HTTPProxyConnection>'
        "</ProxyEndpoint>"
    )
    assert shown is not None and "/orders" not in shown
    assert table.token("orders-api Zq7Tk9Wm")
    loop = _loop_for(table)
    assert loop._for_ai(reason) == reason
    opening = "the fixed project did not build or start"
    for hidden in ("\\u002forders", "ordersX", "orders-api  Zq7Tk9Wm"):
        told = loop._for_ai(reason.replace("); it was undone", f" at {hidden}); it was undone"))
        assert told == f"{opening}: {WITHHELD}", (hidden, told)


@pytest.mark.parametrize("how", ["build", "deploy"])
def test_CP10_X88_orders_api_with_base_path_orders_is_told_why_its_start_failed(tmp_path: Path, how: str) -> None:
    """[CP10-X88] End to end: proxy ``orders-api``, base path ``/orders`` (CP8's rate-limit bundle under that name),
    a fix that is written and whose build or deploy fails. Request 2 quotes the Maven or Mule cause, never a2m's
    withheld words, and no request holds a piece of the policy's value."""
    import test_cp8_fix_loop as cp8

    from a2m.generator import generate_project
    from a2m.parser import read_bundle
    from a2m.verify.fix_loop import WITHHELD

    secret = SECRETS[1]
    bundle_dir = cp8.write_rate_limit_bundle(tmp_path / "bundle", "orders-api")
    policy = bundle_dir / "apiproxy" / "policies" / "AM-AddHeader.xml"
    policy.write_text(policy.read_text(encoding="utf-8").replace(">prod<", f">{secret}<"), encoding="utf-8")
    endpoints = "".join(path.read_text(encoding="utf-8") for path in (bundle_dir / "apiproxy").rglob("*.xml"))
    assert "<BasePath>/orders</BasePath>" in endpoints
    bundle = read_bundle(bundle_dir)
    assert bundle.name == "orders-api"
    app_dir = tmp_path / "app" / "mule-app"
    generate_project(bundle, app_dir)
    flow = app_dir / FLOW_REL
    flow.write_text(cp8.marked(flow.read_text(encoding="utf-8"), rate_ok=False, header_ok=True), encoding="utf-8")
    provider = _Provider(PROBE)
    runner = _FailingAfterFixRunner(how)

    loop = _run(bundle, app_dir, provider, runner)

    assert len(provider.requests) == 2 and runner.starts >= 2
    assert "orders-api" in loop.attempts[0].reason
    previous = _previous(provider.requests[1].prompt)
    assert WITHHELD not in previous, previous
    cause = "mvn package failed: [ERROR]" if how == "build" else "while evaluating"
    assert cause in previous and "orders-api" in previous, previous
    assert _leaks(provider.requests, secret) == []


@pytest.mark.parametrize("value", ["Pw  9x!z", "Pw\t9x!z", "Pw 9x!z"], ids=["two-blanks", "tab", "one-blank"])
def test_CP10_X89_blanks_are_collapsed_only_after_the_sweep(value: str) -> None:
    """[CP10-X89] A short value (under 6 letters and digits, which holds_value does not look for) with two blanks or a
    tab was collapsed before the sweep, so the sweep never found it. The reason is swept as it is and again collapsed:
    the value is never told, whether the text spells it with the same blanks or with two where it has one."""
    table = Placeholders()
    token = table.token(value)
    loop = _loop_for(table)
    for written in sorted({value, value.replace("Pw 9", "Pw  9")}):
        told = loop._for_ai(f"the fix was refused, nothing was written: guard x == '{written}' is not allowed")
        assert "9x!z" not in told and told.endswith(f"guard x == '{token}' is not allowed"), (written, told)


NAMES_HIDDEN = '''

def _a2m_variant_unshown(self, text, names=()):  # type: ignore[no-untyped-def]
    """The variant: a value whose letters stand inside the proxy's name still withholds the reason."""
    return self.holds_value(text)


Placeholders.holds_unshown_value = _a2m_variant_unshown
'''


def test_CP10_X90_the_check_cannot_pass_on_a2m_that_withholds_every_build_and_deploy_reason(tmp_path: Path) -> None:
    """[CP10-X90] The escapes proxy's base path stands inside its name again, and the check requires the build or
    deploy cause in fix request 2: on a2m that withholds such reasons, the check says it could not check (exit 2) for
    every build and deploy scenario, never that it passed."""
    proc = _check_variant(tmp_path, "ai/placeholders.py", NAMES_HIDDEN)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    # Exit 2 output abbreviates the words a detection uses ("credentials" is "creds").
    for scenario in ("build", "deploy", "build-creds", "deploy-creds"):
        assert f"{scenario} scenario: fix request 2 does not say why the start failed" in proc.stdout, proc.stdout


DIFF_SHOWN = '''

_a2m_variant_values = Placeholders._diff_values


def _a2m_variant_shown_values(self, text):  # type: ignore[no-untyped-def]
    """The variant: a number of a diff is copied through, as before round 16."""
    return re.sub(r"«n\\d{1,7}»", lambda found: self.values[found.group(0)], _a2m_variant_values(self, text))


Placeholders._diff_values = _a2m_variant_shown_values
Placeholders._diff_key = lambda self, name: name
'''


def test_CP10_X91_the_check_catches_a2m_whose_diff_shows_json_numbers_and_keys(tmp_path: Path) -> None:
    """[CP10-X91] The golden recording of the check's policies proxy answers a JSON number and a JSON key canary the
    fake app does not: on a2m whose diff copies numbers and keys through, the check names both (exit 1)."""
    proc = _check_variant(tmp_path, "ai/placeholders.py", DIFF_SHOWN)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "leak: canary json-num" in proc.stdout and "leak: canary json-key" in proc.stdout, proc.stdout
