"""The verification harness: run a proxy's Mule app against a mock backend and label it honestly.

:func:`verify_proxy` starts the app through a :class:`~a2m.verify.model.Runner`
pointed at a mock backend (:mod:`a2m.verify.mock_backend`), replays the golden
recordings for the proxy when a --golden folder holds some, else runs its test
battery (:mod:`a2m.verify.batteries`), compares every response and the calls
the backend received, and returns a :class:`VerificationResult`.

The verification type never claims more than happened:

* ``golden``: the app ran and every recorded exchange matched;
* ``battery``: the app ran and every battery case passed (at least one ran);
* ``failed``: the build, deploy or start failed (the runtime stopping while
  the app deployed included), or a test ran and failed; a call that gets no
  valid HTTP answer is a failed test, never a crash. When the runtime itself
  stopped during the tests (the runner's ``runtime_problem`` says so after a
  call got no answer), the tests stop and the result says "the Mule runtime
  stopped during the tests" instead of blaming the app, keeps the results of
  the tests that finished before, and says whether the runner restarts the
  runtime for the next proxy (``restart_available``, once per batch);
* ``static``: nothing was run (no runtime, no case to run, an app that needs
  Mule Enterprise, an app a2m cannot point at the mock, built but not started,
  or a local Mule runtime that is installed but cannot be started: that is a
  problem of the machine, not of the app, so it is never ``failed``).

A golden replay compares every recorded response (status, headers minus the
ignore list, body) and every call the backend received in full (method, path
with query, request body, headers minus the backend ignore list, which also
holds Host). Headers are compared exactly: one the app sent or answered that
the recording does not have (an API key Apigee removed before the backend, a
backend header Apigee stripped from the response) is a mismatch. A client header
the recording need not repeat is tolerated per backend call, with the value of
the client call that backend call was made for only. Credentials (API keys,
Authorization, cookies) are masked (:mod:`a2m.verify.masking`) on the whole value before a diff cuts it
short, and every text of the result (message, log excerpt, diffs, reasons) and every run.log line of the
run goes through the same masker; it knows the keys of the recordings and of the battery before the app is
deployed, so a deploy failure that echoes one is masked too. VerifyAPIKey keys the run needs are set in the deployed copy only
(the battery's test key, or the keys the recorded requests presented), and
the result message says so.

Apps that need Mule Enterprise (``requiredProduct`` MULE_EE, ``ee:``
components) are never started on the local runtime, which is Mule Kernel
(Community Edition). SpikeArrest and Quota always carry a review flag: a
short test cannot prove a time window.

:func:`make_verify_stage` is the engine stage: it decides whether the app can
run at all (--no-runtime, tools installed, --mock-backends or --golden),
verifies a working copy of the generated project, and writes
``verification.json`` and one run.log line per proxy. With the AI provider
--llm picked and --max-fix-attempts above 0, a proxy whose tests ran and
failed goes through the AI fix loop (:mod:`a2m.verify.fix_loop`) on that
working copy; the Mule configuration files of the version it kept are copied
back into ``mule-app/``, and ``verification.json`` lists every attempt under
``"attempts"`` (an empty list when no fix was asked for).
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from a2m import layout, progress, safefs
from a2m.ai.provider import Provider
from a2m.ir import Bundle
from a2m.parser import read_bundle
from a2m.progress import Step
from a2m.runlog import get_logger
from a2m.verify import compare
from a2m.verify.batteries import (
    Battery,
    BatteryCase,
    HeaderChanges,
    build_battery,
    header_changes,
    recorded_key_properties,
    time_window_flags,
)
from a2m.verify.fix_loop import FixAttempt, mask_attempts, run_with_fixes
from a2m.verify.generated import GeneratedSteps
from a2m.verify.golden import Exchange, RecordingError, load_exchanges, recordings_dir
from a2m.verify.masking import Masker
from a2m.verify.mock_backend import MockBackend, RecordedCall
from a2m.verify.model import (
    AppHandle,
    AppUnderTest,
    CaseResult,
    HttpResponse,
    ReviewFlag,
    Runner,
    UntestedPolicy,
    VerificationResult,
    VerificationType,
    fold,
)
from a2m.verify.mule import (
    DEPLOY_FAILED_SIGNAL,
    BuildError,
    DeployError,
    MuleError,
    RuntimeStoppedError,
    RuntimeUnavailableError,
    deploy_failure_message,
    failure_excerpt,
    log_tail,
)
from a2m.verify.runner import MuleAppRunner, UnsafeAppError, redirect_plan
from a2m.verify.tools import detect_tools, missing_message

ENTERPRISE_MESSAGE = "requires Mule Enterprise runtime; local runtime is Community Edition"
EE_NAMESPACE = "http://www.mulesoft.org/schema/mule/ee/core"
VERIFICATION_FILE = "verification.json"
MOCK_DEFAULT_HEADERS = {"Content-Type": "application/json"}
MOCK_DEFAULT_BODY = b'{"a2m":"mock backend"}'
STAGE_NAME = "verify"
# The folder of the Mule configuration files, the only files an AI fix may change (see a2m.verify.fix_loop).
FIXABLE_DIR = layout.MULE_CONFIG_DIR
# Anything a call to the app can raise when the app answers with no valid HTTP response.
SEND_ERRORS = (OSError, MuleError, http.client.HTTPException)
RUNTIME_STOPPED_MESSAGE = "not tested: the Mule runtime stopped during the tests ({problem})"
# Added to RUNTIME_STOPPED_MESSAGE by what the runner says it does next (nothing when it cannot tell).
RESTART_NEXT = "; it is restarted for the next proxy"
NO_RESTART = "; it is not restarted again in this run, so the later proxies are not run"


class _RuntimeStopped(Exception):
    """The runner's runtime stopped while the tests ran (a call got no answer and the runner says why).

    ``results`` are the tests that finished before it stopped (the one it stopped in is not among them).
    """

    def __init__(self, problem: str, results: Sequence[CaseResult] = ()) -> None:
        super().__init__(problem)
        self.problem = problem
        self.results = tuple(results)


def _restart_clause(runner: Runner) -> str:
    """What happens to the stopped runtime next, when the runner can tell (an optional ``restart_available``)."""
    probe = getattr(runner, "restart_available", None)
    if not callable(probe):
        return ""
    try:
        available = probe()
    except (MuleError, OSError):
        return ""
    return RESTART_NEXT if available else NO_RESTART


def _runtime_problem(runner: Runner) -> str | None:
    """Why ``runner``'s runtime stopped, when the runner can tell (an optional ``runtime_problem`` method)."""
    probe = getattr(runner, "runtime_problem", None)
    if not callable(probe):
        return None
    try:
        problem = probe()
    except (MuleError, OSError):
        return None
    return str(problem) if problem else None


@dataclass(frozen=True, slots=True)
class VerifyConfig:
    """Harness settings: the headers a golden replay never compares (names, any case).

    ``ignore_headers`` applies to the app's responses, ``backend_ignore_headers`` to the calls the
    backend received (it adds Host and the other forwarding headers a2m's redirect changes).
    """

    ignore_headers: Sequence[str] = compare.DEFAULT_IGNORED_HEADERS
    backend_ignore_headers: Sequence[str] = compare.BACKEND_IGNORED_HEADERS

    def ignoring(self, names: Sequence[str]) -> VerifyConfig:
        """This configuration with ``names`` (e.g. from --golden-ignore-header) added to both lists."""
        if not names:
            return self
        return VerifyConfig(
            ignore_headers=(*self.ignore_headers, *names),
            backend_ignore_headers=(*self.backend_ignore_headers, *names),
        )


def requires_enterprise(app_dir: Path) -> bool:
    """True when the generated app needs a Mule Enterprise runtime (MULE_EE product or ee: components)."""
    try:
        artifact = json.loads((app_dir / "mule-artifact.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        artifact = {}
    if isinstance(artifact, dict) and artifact.get("requiredProduct") == "MULE_EE":
        return True
    for path in sorted(app_dir.joinpath(*layout.MULE_CONFIG_DIR).glob("*.xml")):
        try:
            if EE_NAMESPACE in path.read_text(encoding="utf-8", errors="replace"):
                return True
        except OSError:
            continue
    return False


def explain_deploy_failure(app_name: str, log_text: str, *, mule_version: str | None) -> VerificationResult:
    """The result for an app whose deployment failed, read from mule.log: plain words and the relevant lines.

    The harness reports every logged deploy failure through this function.
    """
    excerpt = failure_excerpt(log_text, app_name) or log_tail(log_text)
    message = deploy_failure_message(app_name, excerpt, mule_version=mule_version)
    return VerificationResult(VerificationType.FAILED, message=message, log_excerpt=excerpt)


def _static(message: str, *, flags: tuple[ReviewFlag, ...] = (), untested: tuple[UntestedPolicy, ...] = ()) -> VerificationResult:
    return VerificationResult(VerificationType.STATIC, message=message, review_flags=flags, untested=untested)


def verify_proxy(
    bundle: Bundle,
    app_dir: Path,
    *,
    runner: Runner,
    backend: MockBackend | None = None,
    golden: Path | None = None,
    config: VerifyConfig | None = None,
    generated: GeneratedSteps | None = None,
    masker: Masker | None = None,
) -> VerificationResult:
    """Verify ``bundle``'s generated app in ``app_dir`` (see the module docstring).

    ``golden`` is the --golden folder (one sub-folder per proxy). A ``backend`` passed in is used and left
    running; otherwise the harness starts its own on 127.0.0.1 and stops it afterwards. ``generated`` is what
    the generator made of each step (see :mod:`a2m.verify.generated`); the battery tests only the steps it
    generated and that run in the app. ``masker`` (default: one for ``bundle``) masks every text of the result
    and every run.log line written meanwhile.
    """
    masker = masker or Masker.for_bundle(bundle)
    return masker.mask_result(
        verify_proxy_unmasked(
            bundle, app_dir, runner=runner, backend=backend, golden=golden, config=config, generated=generated,
            masker=masker,
        )
    )


def verify_proxy_unmasked(
    bundle: Bundle,
    app_dir: Path,
    *,
    runner: Runner,
    masker: Masker,
    backend: MockBackend | None = None,
    golden: Path | None = None,
    config: VerifyConfig | None = None,
    generated: GeneratedSteps | None = None,
) -> VerificationResult:
    """:func:`verify_proxy`'s result before ``masker`` masks its texts (the test diffs are masked all the same, before
    a long value is cut; run.log lines are masked as they are written).

    Only for the AI fix loop (:func:`a2m.verify.fix_loop.run_with_fixes`): it builds what the AI is told from the
    unmasked text (every value of the proxy replaced by its placeholder in any spelling, which a value the masker
    already changed defeats), and masks everything it records, logs or returns. Never write or log this result as
    it is."""
    with masker.active(), masker.logging():
        return _verify(bundle, app_dir, runner, backend, golden, config or VerifyConfig(), generated, masker)


def _verify(
    bundle: Bundle,
    app_dir: Path,
    runner: Runner,
    backend: MockBackend | None,
    golden: Path | None,
    config: VerifyConfig,
    generated: GeneratedSteps | None,
    masker: Masker,
) -> VerificationResult:
    name = bundle.name
    flags = time_window_flags(bundle)
    if requires_enterprise(app_dir):
        return _static(f"not run: {ENTERPRISE_MESSAGE}", flags=flags)
    plan = redirect_plan(app_dir)
    if isinstance(plan, str):
        return _static(f"not run, so no real backend is called: {plan}", flags=flags)

    exchanges: tuple[Exchange, ...] | None = None
    note = ""
    if golden is not None:
        try:
            exchanges = load_exchanges(golden, name)
        except RecordingError as exc:
            return _static(f"not run: the golden recordings of {name} can't be used: {exc}", flags=flags)
        if exchanges is None:
            note = f"no golden recordings found in {recordings_dir(golden, name)}; ran its battery instead. "
    battery: Battery | None = None
    properties: dict[str, str] = {}
    key_notes: tuple[str, ...] = ()
    if exchanges is not None:
        for exchange in exchanges:
            _learn_exchange(masker, exchange)
        recorded = [(call.request, call.response.status) for exchange in exchanges for call in exchange.calls]
        properties, key_notes = recorded_key_properties(bundle, app_dir, recorded)
    if exchanges is None:
        battery = build_battery(bundle, app_dir, generated=generated)
        for case in battery.cases:
            for battery_call in case.calls:
                masker.learn_request(battery_call.request)
        if not battery.cases:
            return _static(
                f"{note}not run: the battery has no case for this proxy's policies, so nothing was tested",
                flags=flags,
                untested=battery.untested,
            )

    own = backend is None
    mock = backend or MockBackend(default_status=200, default_headers=MOCK_DEFAULT_HEADERS, default_body=MOCK_DEFAULT_BODY)
    if own:
        mock.start()
    if battery is not None:
        properties = dict(battery.properties)
    for value in properties.values():
        # The keys the deployed copy allows for the run: a deploy failure may echo them.
        for part in value.split(","):
            masker.learn_value(part)
    app = AppUnderTest(name, app_dir, properties)
    try:
        started = _start(runner, app, mock.url)
        if isinstance(started, VerificationResult):
            return _with(started, flags=flags, untested=battery.untested if battery else ())
        handle = started
        try:
            if not handle.running:
                return _static(
                    f"{note}the app was built but not run (the runner did not start it), so nothing was tested",
                    flags=flags,
                    untested=battery.untested if battery else (),
                )
            try:
                progress.step(Step.TESTS)
                if exchanges is not None:
                    return _replay(
                        exchanges, handle, mock, config, flags, key_notes, header_changes(bundle), runner=runner,
                        masker=masker,
                    )
                assert battery is not None
                return _run_battery(battery, handle, runner, app, mock, flags, note, masker)
            except _RuntimeStopped as exc:
                # The tests that finished before the runtime stopped keep their results; it is failed all the same.
                done = exc.results
                passed = sum(1 for r in done if r.passed)
                stopped = RUNTIME_STOPPED_MESSAGE.format(problem=exc.problem) + _restart_clause(runner)
                finished = f"; {passed} of {len(done)} tests that ran before it stopped passed" if done else ""
                return VerificationResult(
                    VerificationType.FAILED,
                    len(done),
                    passed,
                    len(done) - passed,
                    done,
                    untested=battery.untested if battery else (),
                    review_flags=flags,
                    message=f"{note}{name}: {stopped}{finished}",
                )
        finally:
            _stop(handle)
    finally:
        if own:
            mock.stop()


def _with(
    result: VerificationResult, *, flags: tuple[ReviewFlag, ...], untested: tuple[UntestedPolicy, ...]
) -> VerificationResult:
    return VerificationResult(
        result.type, result.ran, result.passed, result.failed, result.cases, untested, flags, result.message,
        result.log_excerpt,
    )


def _start(runner: Runner, app: AppUnderTest, backend_url: str) -> AppHandle | VerificationResult:
    """The started app, or the failed (or static) result when it could not be built or started."""
    try:
        progress.step(Step.BUILD)
        handle = runner.start(app, backend_url=backend_url)
        progress.step(Step.DEPLOY)  # shown once: a runner that reports its own deploy has shown it already
        return handle
    except BuildError as exc:
        return VerificationResult(
            VerificationType.FAILED, message=f"{app.name}: build failed: {exc}", log_excerpt=exc.output
        )
    except RuntimeStoppedError as exc:
        return VerificationResult(
            VerificationType.FAILED,
            message=f"{app.name}: not tested: the local Mule runtime stopped while the app was deploying ({exc})",
            log_excerpt=exc.log_excerpt,
        )
    except DeployError as exc:
        if DEPLOY_FAILED_SIGNAL in exc.log_excerpt:
            return explain_deploy_failure(app.name, exc.log_excerpt, mule_version=getattr(runner, "mule_version", None))
        message = str(exc) if app.name in str(exc) else f"{app.name}: the app did not start: {exc}"
        return VerificationResult(VerificationType.FAILED, message=message, log_excerpt=exc.log_excerpt)
    except UnsafeAppError as exc:
        return _static(f"not run, so no real backend is called: {exc}")
    except RuntimeUnavailableError as exc:
        return _static(f"not run: {exc}")
    except MuleError as exc:
        return VerificationResult(VerificationType.FAILED, message=f"{app.name}: the app could not be run: {exc}")


def _stop(handle: AppHandle) -> None:
    try:
        handle.stop()
    except MuleError as exc:
        get_logger().warning("could not stop the app under test cleanly: %s", exc)


# ---------------------------------------------------------------- battery


def _run_battery(
    battery: Battery,
    handle: AppHandle,
    runner: Runner,
    app: AppUnderTest,
    mock: MockBackend,
    flags: tuple[ReviewFlag, ...],
    note: str,
    masker: Masker | None = None,
) -> VerificationResult:
    results: list[CaseResult] = []
    pacer = _Pacer(battery.pace_seconds)
    current = handle
    restart_error: VerificationResult | None = None
    for index, case in enumerate(battery.cases):
        if battery.isolate and index > 0:
            # Counters and caches must not carry over: every case gets a freshly started app.
            _stop(current)
            started = _start(runner, app, mock.url)
            if isinstance(started, VerificationResult):
                restart_error = started
                break
            current = started
            pacer.reset()
        try:
            results.append(_run_case(case, current, mock, pacer, runner, masker))
        except _RuntimeStopped as exc:
            if current is not handle:
                _stop(current)
            raise _RuntimeStopped(exc.problem, results) from exc
    if current is not handle:
        _stop(current)
    ran = len(results)
    passed = sum(1 for r in results if r.passed)
    failed = ran - passed
    summary = f"{passed} of {ran} battery tests passed"
    if battery.notes:
        summary += "; " + "; ".join(battery.notes)
    untested = battery.untested
    if untested:
        summary += f"; not tested: {', '.join(sorted({u.name for u in untested}))}"
    if restart_error is not None:
        return VerificationResult(
            VerificationType.FAILED, ran, passed, failed, tuple(results), untested, flags,
            f"{note}{summary}; then {restart_error.message}", restart_error.log_excerpt,
        )
    kind = VerificationType.BATTERY if ran > 0 and failed == 0 else VerificationType.FAILED
    return VerificationResult(kind, ran, passed, failed, tuple(results), untested, flags, note + summary)


class _Pacer:
    """Waits between calls so a SpikeArrest's pace allows each call that is meant to pass."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.last: float | None = None

    def reset(self) -> None:
        self.last = None

    def wait(self, immediate: bool) -> None:
        if not immediate and self.seconds and self.last is not None:
            delay = self.last + self.seconds - time.monotonic()
            if delay > 0:
                time.sleep(delay)

    def done(self) -> None:
        self.last = time.monotonic()


def _run_case(
    case: BatteryCase,
    handle: AppHandle,
    mock: MockBackend,
    pacer: _Pacer,
    runner: Runner | None = None,
    masker: Masker | None = None,
) -> CaseResult:
    masker = masker or Masker()
    lines: list[str] = []
    before = len(mock.calls())
    for number, call in enumerate(case.calls, 1):
        masker.learn_request(call.request)
        pacer.wait(call.immediate)
        try:
            got = handle.send(call.request)
        except SEND_ERRORS as exc:
            problem = _runtime_problem(runner) if runner is not None else None
            if problem is not None:
                raise _RuntimeStopped(problem) from exc
            lines.append(f"call {number}: no valid response from the app: {type(exc).__name__}: {exc}")
            pacer.done()
            continue
        pacer.done()
        masker.learn_headers(got.headers)
        prefix = f"call {number} " if len(case.calls) > 1 else ""
        if got.status != call.expected_status:
            lines.append(f"{prefix}status: expected {call.expected_status}, actual {got.status}")
        if call.expected_body is not None:
            lines += [f"{prefix}{line}" for line in compare.body_diffs(call.expected_body, got.body)]
    received = mock.calls()[before:]
    for recorded in received:
        masker.learn_headers(recorded.headers)
        masker.learn_query(recorded.query)
    if len(received) != case.expected_backend_calls:
        lines.append(f"backend calls: expected {case.expected_backend_calls}, got {len(received)}")
    for number, recorded in enumerate(received, 1):
        lines += compare.header_diffs(
            case.expected_backend_headers, recorded.headers, where=f"backend call {number} header"
        )
    return CaseResult(
        name=case.name,
        situation=case.situation,
        passed=not lines,
        diff=masker.mask("\n".join(lines)),
        backend_calls=len(received),
        policy=case.policy,
        policy_type=case.policy_type,
    )


# ---------------------------------------------------------------- golden


def _replay(
    exchanges: Sequence[Exchange],
    handle: AppHandle,
    mock: MockBackend,
    config: VerifyConfig,
    flags: tuple[ReviewFlag, ...],
    notes: Sequence[str] = (),
    changes: HeaderChanges | None = None,
    *,
    runner: Runner | None = None,
    masker: Masker | None = None,
) -> VerificationResult:
    changes = changes if changes is not None else HeaderChanges()
    results: list[CaseResult] = []
    for exchange in exchanges:
        try:
            results.append(_replay_one(exchange, handle, mock, config, changes, runner=runner, masker=masker))
        except _RuntimeStopped as exc:
            raise _RuntimeStopped(exc.problem, results) from exc
    ran = len(results)
    passed = sum(1 for r in results if r.passed)
    failed = ran - passed
    kind = VerificationType.GOLDEN if ran > 0 and failed == 0 else VerificationType.FAILED
    message = "; ".join([f"{passed} of {ran} golden recordings matched", *notes])
    return VerificationResult(kind, ran, passed, failed, tuple(results), (), flags, message)


def _replay_one(
    exchange: Exchange,
    handle: AppHandle,
    mock: MockBackend,
    config: VerifyConfig,
    changes: HeaderChanges | None = None,
    *,
    runner: Runner | None = None,
    masker: Masker | None = None,
) -> CaseResult:
    changes = changes if changes is not None else HeaderChanges()
    masker = masker or Masker()
    _learn_exchange(masker, exchange)
    mock.clear_queue()
    for recorded in exchange.backend_calls:
        response = recorded.response
        mock.enqueue(
            recorded.method, recorded.path.split("?", 1)[0], status=response.status, headers=dict(response.headers),
            body=response.body,
        )
    lines: list[str] = []
    before = len(mock.calls())
    # How many backend calls had arrived when each client call got its answer: a backend call belongs to the
    # client call during which it arrived (the app calls the backend while it handles that call).
    marks: list[int] = []
    last = time.monotonic()
    try:
        for number, call in enumerate(exchange.calls, 1):
            delay = last + call.after_ms / 1000 - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            try:
                got = handle.send(call.request)
            except SEND_ERRORS as exc:
                problem = _runtime_problem(runner) if runner is not None else None
                if problem is not None:
                    raise _RuntimeStopped(problem) from exc
                lines.append(f"call {number}: no valid response from the app: {type(exc).__name__}: {exc}")
                last = time.monotonic()
                marks.append(len(mock.calls()))
                continue
            last = time.monotonic()
            marks.append(len(mock.calls()))
            masker.learn_headers(got.headers)
            result = compare.compare_response(
                call.response, got, ignore_headers=config.ignore_headers, exact_headers=changes.response_any
            )
            prefix = f"call {number} " if len(exchange.calls) > 1 else ""
            lines += [f"{prefix}{line}" for line in result.diff.splitlines()]
            if not changes.response_any:
                lines += [f"{prefix}{line}" for line in _changed_response_headers(call.response, got, config, changes)]
    finally:
        mock.clear_queue()
    received = mock.calls()[before:]
    for arrived in received:
        masker.learn_headers(arrived.headers)
        masker.learn_query(arrived.query)
    origins = [_origin(before + index, marks) for index in range(len(received))]
    lines += _backend_diffs(exchange, received, config, changes, origins)
    return CaseResult(exchange.name, None, not lines, masker.mask("\n".join(lines)), len(received))


def _learn_exchange(masker: Masker, exchange: Exchange) -> None:
    """Teach ``masker`` the credentials a recorded exchange shows (client calls, answers, backend calls)."""
    for call in exchange.calls:
        masker.learn_request(call.request)
        masker.learn_headers(call.response.headers)
    for want in exchange.backend_calls:
        masker.learn_headers(want.headers)
        masker.learn_target(want.path)
        masker.learn_headers(want.response.headers)


def _origin(position: int, marks: Sequence[int]) -> int:
    """The index of the client call during which the backend call at ``position`` (in mock.calls()) arrived;
    one that arrived after the last answer counts for the last call."""
    for index, mark in enumerate(marks):
        if position < mark:
            return index
    return max(len(marks) - 1, 0)


def _changed_response_headers(
    expected: HttpResponse, actual: HttpResponse, config: VerifyConfig, changes: HeaderChanges
) -> list[str]:
    """Lines for each response header a response step of the proxy changes that the app answered with but the
    recording does not have: Apigee removed it (or never set it), so the app must not send it either."""
    recorded = {fold(str(name)) for name in expected.headers}
    ignored = {fold(name) for name in config.ignore_headers}
    live = compare.lower_headers(actual.headers)
    return [
        f"header {name}: expected absent, actual {compare.shown(live[name])}"
        for name in sorted(changes.response)
        if name in live and name not in recorded and name not in ignored
    ]


def _backend_diffs(
    exchange: Exchange,
    received: Sequence[RecordedCall],
    config: VerifyConfig,
    changes: HeaderChanges | None = None,
    origins: Sequence[int] | None = None,
) -> list[str]:
    """Diff lines for the calls the backend received against the recorded ones (see the module docstring).

    ``origins`` holds, for each received call, the index of the client call it was made for (see
    :func:`_allowed_extra_headers`); without it each backend call is matched to the client call with the same
    number when the counts agree, else to none (only headers every client call sent with the same value).
    """
    changes = changes if changes is not None else HeaderChanges()
    lines: list[str] = []
    expected = exchange.backend_calls
    if len(received) != len(expected):
        lines.append(f"backend calls: expected {len(expected)}, got {len(received)}")
    for number, (want, got) in enumerate(zip(expected, received, strict=False), 1):
        where = f"backend call {number}"
        allowed = _allowed_extra_headers(exchange, changes, _call_for(exchange, received, origins, number - 1))
        lines += compare.target_diffs(want.method, want.path, got.method, got.path, got.query, where=where)
        lines += compare.header_diffs(
            want.headers,
            got.headers,
            config.backend_ignore_headers,
            where=f"{where} header",
            exact=True,
            tolerated=allowed,
        )
        lines += compare.request_body_diffs(want.body, got.body, where=f"{where} body")
    return lines


def _call_for(
    exchange: Exchange, received: Sequence[RecordedCall], origins: Sequence[int] | None, index: int
) -> int | None:
    """The client call the backend call ``index`` was made for, or None when that is not known."""
    if origins is not None and index < len(origins):
        return origins[index]
    if len(exchange.calls) == 1:
        return 0
    if len(received) == len(exchange.calls) == len(exchange.backend_calls):
        return index
    return None


def _allowed_extra_headers(
    exchange: Exchange, changes: HeaderChanges, call: int | None = None
) -> dict[str, frozenset[str]]:
    """Headers (lower case) a backend call may carry, with this value, without the recording listing them.

    ``call`` is the index of the client call the backend call was made for; None when that is not known.

    * A header that client call sent, with the value it sent: Apigee forwards every request header to the
      target unless a step changes it, so a recording need not repeat it. Only that call's own value: a value
      another call of the exchange sent (an earlier call's token or session id) is a mismatch, since the app
      must not carry it over. With ``call`` None, only a header every client call sent with the same value.
      Not when a request step of the proxy may change that header (or every header): then the recording says
      what the backend got, and a header it does not list (an API key or Authorization Apigee removed) must
      not reach the backend.
    * The defaults the local runtime's HTTP client and the harness's own client add to a request without
      that header (compare.LOCAL_CLIENT_DEFAULTS).
    """
    headers = [compare.lower_headers(c.request.headers) for c in exchange.calls]
    if call is not None and 0 <= call < len(headers):
        sent = headers[call]
    else:
        common = set(headers[0].items()) if headers else set()
        for other in headers[1:]:
            common &= set(other.items())
        sent = dict(common)
    named = {name for h in headers for name in h} if call is None else set(sent)
    allowed: dict[str, frozenset[str]] = {
        name: frozenset({value}) for name, value in compare.LOCAL_CLIENT_DEFAULTS.items() if name not in named
    }
    if not changes.request_any:
        for name, value in sent.items():
            if name not in changes.request:
                allowed[name] = frozenset({value})
    return allowed


# ---------------------------------------------------------------- the engine stage


class StageContext(Protocol):
    """What the engine passes each stage (see a2m.engine.ProxyContext)."""

    @property
    def name(self) -> str: ...

    @property
    def bundle_dir(self) -> Path: ...

    @property
    def out_dir(self) -> Path: ...

    @property
    def options(self) -> StageSettings: ...


class StageSettings(Protocol):
    @property
    def golden(self) -> Path | None: ...

    @property
    def mock_backends(self) -> bool: ...

    @property
    def no_runtime(self) -> bool: ...

    @property
    def golden_ignore_headers(self) -> tuple[str, ...]: ...


class VerifyStage:
    """The per-proxy verification stage; one per batch. :meth:`close` stops the Mule runtime it started."""

    __name__ = STAGE_NAME

    def __init__(self, runner: Runner | None = None, config: VerifyConfig | None = None) -> None:
        self._given = runner
        self._config = config or VerifyConfig()
        self._real: MuleAppRunner | None = None
        self._results_root: Path | None = None
        self._skip: str | None = None
        self._detected = False
        self._notices: list[str] = []

    @property
    def notices(self) -> tuple[str, ...]:
        """One line per reason the batch's apps could not be run at all (missing tools, a runtime that won't start)."""
        return tuple(self._notices)

    def _notice(self, text: str) -> None:
        if text not in self._notices:
            self._notices.append(text)

    def __call__(self, context: StageContext) -> None:
        bundle = read_bundle(context.bundle_dir, label=context.name)
        masker = Masker.for_bundle(bundle)
        with masker.logging():
            self._verify_and_report(context, bundle, masker)

    def _verify_and_report(self, context: StageContext, bundle: Bundle, masker: Masker) -> None:
        """Verify one proxy, write its verification.json and log its lines, all masked by ``masker``."""
        log = get_logger()
        name = context.name
        attempts: tuple[FixAttempt, ...] = ()
        # Absolute, so the Mule runtime's MULE_BASE never depends on the working folder.
        results_root = context.out_dir.absolute().parent
        app_dir = layout.mule_app_dir(context.out_dir.absolute())
        options = context.options
        if options.no_runtime:
            result = _static("runtime verification disabled by --no-runtime", flags=time_window_flags(bundle))
        elif requires_enterprise(app_dir):
            result = _static(f"not run: {ENTERPRISE_MESSAGE}", flags=time_window_flags(bundle))
        elif not (options.mock_backends or options.golden is not None):
            result = _static(
                "not run: pass --mock-backends (or --golden) to run the app against a mock backend; a2m never "
                "calls the proxy's real backends",
                flags=time_window_flags(bundle),
            )
        else:
            runner = self._runner(results_root)
            if runner is None:
                assert self._skip is not None
                self._notice(masker.mask(f"{self._skip}; verification type: static"))
                result = _static(self._skip, flags=time_window_flags(bundle))
            else:
                config = self._config.ignoring(tuple(getattr(options, "golden_ignore_headers", ())))
                generated = _saved_generated_steps(results_root, name)
                fixes = _FixSettings(getattr(options, "provider", None), getattr(options, "max_fix_attempts", 0))
                result, attempts = self._verify_copy(
                    bundle, app_dir, runner, results_root, name, options.golden, config, generated, masker, fixes
                )
                unavailable = self._real.unavailable_reason if self._real is not None else None
                if unavailable is not None:
                    self._notice(masker.mask(f"apps not run: {unavailable}; verification type: static"))
        # The one write point: verification.json and the lines below show only masked text.
        result = masker.mask_result(result)
        attempts = mask_attempts(attempts, masker)
        self._report(context, result, attempts)
        (log.info if result.type in (VerificationType.GOLDEN, VerificationType.BATTERY) else log.warning)(
            "%s: %s; verification type: %s (%d tests ran, %d passed, %d failed)",
            name,
            result.message,
            result.type.value,
            result.ran,
            result.passed,
            result.failed,
        )
        for case in result.cases:
            if not case.passed:
                log.warning("%s: test %s failed: %s", name, case.name, case.diff.replace("\n", "; "))
        for flag in result.review_flags:
            log.warning("%s: needs review: %s", name, flag.reason)

    def _runner(self, results_root: Path) -> Runner | None:
        if self._given is not None:
            return self._given
        if self._real is not None:
            return self._real
        if not self._detected:
            self._detected = True
            status = detect_tools()
            if status.missing or status.mule_home is None:
                self._skip = missing_message(status)
                return None
            self._results_root = results_root
            self._real = MuleAppRunner(status.mule_home, layout.mule_base_dir(results_root))
            return self._real
        return None

    def _verify_copy(
        self,
        bundle: Bundle,
        app_dir: Path,
        runner: Runner,
        results_root: Path,
        name: str,
        golden: Path | None,
        config: VerifyConfig,
        generated: GeneratedSteps | None = None,
        masker: Masker | None = None,
        fixes: _FixSettings | None = None,
    ) -> tuple[VerificationResult, tuple[FixAttempt, ...]]:
        """Verify a working copy of the project, so the build output never lands in the results.

        With an AI provider and fix attempts allowed (``fixes``), the AI fix loop runs on the working copy
        (:mod:`a2m.verify.fix_loop`), and the Mule configuration files of the version it kept are copied back
        into ``app_dir``, so the project in the results is the one the result describes.
        """
        work = layout.verify_work_dir(results_root, name)
        try:
            safefs.remove(results_root, work)
            safefs.make_dirs(results_root, work.parent)
            _copy_tree(app_dir, work)
            if fixes is None or fixes.provider is None or fixes.max_attempts <= 0:
                result = verify_proxy(
                    bundle, work, runner=runner, golden=golden, config=config, generated=generated, masker=masker
                )
                return result, ()
            loop = run_with_fixes(
                bundle, work, runner=runner, provider=fixes.provider, max_fix_attempts=fixes.max_attempts,
                golden=golden, config=config, generated=generated, masker=masker,
            )
            if any(attempt.helped for attempt in loop.attempts):
                _keep_fixed_files(results_root, work, app_dir, name)
            return loop.result, loop.attempts
        finally:
            safefs.remove(results_root, work)
            safefs.remove_empty_dir(results_root, work.parent)
            safefs.remove_empty_dir(results_root, work.parent.parent)

    def _report(self, context: StageContext, result: VerificationResult, attempts: Sequence[FixAttempt] = ()) -> None:
        out = context.out_dir
        data = result.to_json_data()
        # Every AI fix attempt, for the report (empty when the AI was never asked for a fix).
        data["attempts"] = [attempt.to_json_data() for attempt in attempts]
        text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        safefs.write_text_atomic(out.parent, out / VERIFICATION_FILE, text)

    def close(self) -> None:
        """Stop the Mule runtime this stage started (if any) and remove its private MULE_BASE."""
        real, self._real = self._real, None
        if real is None:
            return
        try:
            real.close()
        finally:
            if self._results_root is not None:
                safefs.remove(self._results_root, real.mule_base)


def _saved_generated_steps(results_root: Path, name: str) -> GeneratedSteps | None:
    """What the generate stage saved about each step of proxy ``name`` (see a2m.verify.generated), or None."""
    path = layout.generated_steps_path(results_root, name)
    try:
        if path.is_symlink() or not path.is_file():
            return None
        return GeneratedSteps.from_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        get_logger().warning("%s: the saved generated steps can't be read (%s); a2m's own decisions are used", name, exc)
        return None


@dataclass(frozen=True, slots=True)
class _FixSettings:
    """What the fix loop of one proxy may do: the AI provider --llm picked (None: no loop) and --max-fix-attempts."""

    provider: Provider | None
    max_attempts: int


def _keep_fixed_files(results_root: Path, work: Path, app_dir: Path, name: str) -> None:
    """Copy each Mule configuration file the fix loop changed in the working copy ``work`` back into ``app_dir``.

    Only the files an AI fix may change (``src/main/mule/*.xml``, plain files on both sides) are compared and
    copied, byte for byte, through :mod:`a2m.safefs`."""
    log = get_logger()
    source_dir = work.joinpath(*FIXABLE_DIR)
    if safefs.is_link(source_dir) or not source_dir.is_dir():
        return
    for source in sorted(source_dir.glob("*.xml")):
        target = app_dir.joinpath(*FIXABLE_DIR, source.name)
        if not (safefs.is_regular_file(results_root, source) and safefs.is_regular_file(results_root, target)):
            continue
        data = _read_bytes(results_root, source)
        if data == _read_bytes(results_root, target):
            continue
        safefs.write_bytes_atomic(results_root, target, data)
        log.info("%s: kept the AI fix of %s in %s/", name, "/".join((*FIXABLE_DIR, source.name)), layout.MULE_APP_DIR_NAME)


def _read_bytes(root: Path, path: Path) -> bytes:
    fd = safefs.open_plain_file(root, path, os.O_RDONLY)
    with os.fdopen(fd, "rb") as handle:
        return handle.read()


def _copy_tree(source: Path, dest: Path) -> None:
    """Copy the generated project (plain files and folders only; a link is refused)."""

    def refuse_links(folder: str, names: list[str]) -> list[str]:
        for entry in names:
            if os.path.islink(os.path.join(folder, entry)):
                raise MuleError(f"the generated project has a link at {os.path.join(folder, entry)}")
        return []

    shutil.copytree(source, dest, symlinks=True, ignore=refuse_links)


def make_verify_stage(runner: Runner | None = None, config: VerifyConfig | None = None) -> VerifyStage:
    """The engine's verification stage; with ``runner`` None it finds Java, Maven and Mule and uses the real one."""
    return VerifyStage(runner, config)


__all__ = [
    "ENTERPRISE_MESSAGE",
    "StageContext",
    "VerifyConfig",
    "VerifyStage",
    "explain_deploy_failure",
    "make_verify_stage",
    "requires_enterprise",
    "verify_proxy",
]
