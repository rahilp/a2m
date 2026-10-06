"""The AI fix loop: when a proxy's tests fail, ask the AI for a fix, apply it, re-test, and keep only what helps.

:func:`run_with_fixes` verifies a proxy's app as :func:`a2m.verify.verify_proxy`
does. While the result is ``failed`` with at least one test that ran and
failed, and fewer than ``max_fix_attempts`` fixes were asked for, it sends the
AI one fix request (the prompt file ``a2m/prompts/fix.md``) holding the
original Apigee policy of every generated step (from a template or by the AI;
with the source code of a custom code policy; every other policy, unsupported
or skipped, by name and type only), every Mule configuration file of the app
as it is now, and the diff of every failing test, every literal value in them
replaced by a placeholder (see below). The answer must be one JSON object
(optionally inside one ```json fence):

* ``{"status": "fixed", "files": {"src/main/mule/<file>.xml": "<full new text>"}, "notes": str}``
  (``files`` may be empty: a fix that changes nothing; ``notes`` optional);
* ``{"status": "cannot_fix", "reason": str}`` (``notes`` allowed).

``confidence`` (``high``, ``medium`` or ``low``) may be given with a fix and is
recorded. Anything else is an answer that could not be used. A fix may only rewrite
the existing Mule configuration files the AI was shown (``src/main/mule/*.xml``,
plain files reached without a link): any other path, an absolute path, a
``..`` part, a link, the build file or a new file is refused and nothing of
that fix is written, since the app runs on this machine's Mule runtime and its
build runs Maven here. A fix that points outside the project (an absolute
path, a ``..`` part, a link) also ends the loop for that proxy: the AI is not
asked again. Every rewritten file must be well-formed XML (parsed
without DTDs or entities) and is checked against the file as a2m generated it
(before any fix), element by element and by position: it must hold every
element a2m generated, once each, in the same order and unchanged. Only a step
the AI translated (labelled with its step name, ``doc:name``, and recorded with
the method ``ai``) may be changed in place, keeping its label; new elements may
be added between the generated ones. A fix that removes, moves or duplicates
an element a2m generated, or changes one that is not an AI translation, is
refused. Each element added or changed must pass the same checks the AI's
translation of a callout passes (:func:`a2m.ai.translate.parse_mule`: Mule core
processors and Transform Message only, the one allowlist of elements and
attributes, no ``${...}``, checked guards, no Apigee built-in variable read
as a flow variable), with its guards checked for the side of the flow it sits
on: before the target call (request), after it (response) or in the flow's
error handler (fault). A fix that fails any check is a failed attempt and
nothing of it is written.

The AI never sees a literal value of the proxy. Every request is built from
placeholdered material only (:mod:`a2m.verify.placeholders`, default deny):
every literal value in the policies, the custom code, the Mule files and the
diffs (element text, attribute values, every string literal of code,
expressions and conditions in any position, URL parts and query items, every
value, number and JSON field path segment of a diff) is replaced by a placeholder
such as ``«v3»`` (a number by ``«n3»``), the same value by
the same placeholder for the whole loop. Only a closed list stays visible:
element and attribute names, the names the Apigee XML declares at the schema
positions of names (never in a payload or other data), known names in a
Mule name attribute (the proxy's policy, step, endpoint and flow names from
the IR, and the names of the Mule files as a2m generated them, learned before
the first request; a string literal or other value equal to a known name is
still a placeholder), unquoted identifiers and selectors, object keys, numbers, booleans, HTTP methods, MIME types, query parameter
names and code structure (in data, numbers and booleans are placeholders too).
In a diff only a2m's own words, status codes and counts (a2m's expectations),
``null``, HTTP methods, header names, query parameter names as a URL shows
them, and JSON keys that are known names or plain words of letters stay.
Nothing a2m restored goes back to the AI: the previous attempt's reason (a refusal
that quotes the restored fix, a build or deploy error that quotes the restored
code) is taken whole, before it is cut short and before the masker changes it (a
partly masked credential, ``Bearer abcd*** (16 chars):tail``, is no spelling of
its value), and every value the table knows is
replaced by its placeholder in every spelling a2m knows of it (as it is,
XML-escaped, spelled for a DataWeave, JSON, JavaScript, Java or Python string,
repr-quoted, URL-encoded, and each spelling a2m saw in the source or wrote back
into an answer; :meth:`~a2m.ai.placeholders.Placeholders.sweep`). When the
letters and digits of a value could still be read in what is left
(:meth:`~a2m.ai.placeholders.Placeholders.holds_value`), or the masker still finds a
credential there (it would show its first characters, and the rest after the first
character a token cannot hold), the AI gets a2m's own words for what happened
instead, never the rest of that reason. Both are asked of the reason without the
names a2m shows the AI anyway (the proxy's name, which every build and deploy
error starts with, and every known name), so a value whose letters stand only
inside such a name (a base path ``/orders`` in ``orders-api``) does not hide
why the build or deploy failed. The reason is swept with its blanks as they are
and again with them collapsed, before either check.
Then every text field of the :class:`~a2m.ai.provider.AiRequest` goes through
the proxy's :class:`~a2m.verify.masking.Masker` (outside the placeholders) as
the last step before the provider gets it. In an answer, each placeholder is
written back before any check runs, spelled for where it stands (XML-escaped,
and escaped for its DataWeave or JSON string literal); an echo of a shown file
comes back byte for byte; an unknown placeholder, or one that cannot stand
where it was written, is refused. A new line that holds a
mask a2m makes (``abcd*** (12 chars)``, or a secret a2m redacts) is refused, so
a masked value is never written into the project. Asterisks a proxy writes
itself (a card number shown as ``'****-' ++ last4``) are not a mask.

The prompt names the steps the AI may change in place (the AI translations)
and, from the second request, what came of the previous attempt and why.

A fix that passes the checks is written and the app is verified again. It is
kept when every test now passes, or when strictly fewer tests fail than in the
best version so far (a test that did not run counts as failing). Otherwise
(as many or more tests fail, the project no longer builds or starts, or the
app could not be fully tested) the files are put back byte for byte, so the
next request starts from the best version so far, with that version's diff.
An answer that changes nothing (also one that only re-indents or re-quotes a
file, or re-indents or reflows a DataWeave script outside its string literals:
files are compared as XML, comments kept) is a failed attempt: nothing is
written, the app is still tested once more, but that run never counts as help,
even when it passes. A request refused for now (a rate limit, an overloaded
API) is waited out by the provider with a bounded backoff, not counted as an
attempt. The loop
stops as soon as every test passes, or after ``max_fix_attempts``
requests. A provider error or an unusable answer is a failed attempt, never a
crash; an answer cut off at the token limit or a request that timed out ends
the loop for the proxy, since the same request would end the same way. When
the fix prompt cannot be read, no fix is asked for and no attempt is recorded:
the result says the fix loop did not run, and why.

Every attempt is recorded as a :class:`FixAttempt` (what it changed, the
failing tests before and after, whether it helped and why, the steps of the
generated project its version differs in, and the AI's confidence). Each step
a kept fix changed gets a review flag on the result when the AI's confidence
is low or not given, or when no test covers that step. Every text sent
recorded (the AI's notes and reasons, the change diffs) goes through the proxy's
:class:`~a2m.verify.masking.Masker` first, as every verification output does,
and every run.log line written meanwhile is masked by it too.
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, fields, replace
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING, Any

from defusedxml import DefusedXmlException
from defusedxml import ElementTree as SafeET

from a2m import layout, progress, redaction, safefs
from a2m.ai import checks
from a2m.ai.placeholders import (
    DATAWEAVE,
    TOKEN_SPLIT,
    PlaceholderError,
    Placeholders,
    code_shape,
    expression_end,
)
from a2m.ai.prompts import FIX_PROMPT_FILE, PromptError, load_prompt, render
from a2m.ai.provider import AiRequest, Confidence, ItemKind, Provider, ProviderLimitError
from a2m.ai.sources import callout_source
from a2m.ai.translate import CORE, DOC, FENCE, PROCESSORS, parse_mule
from a2m.conditions.variables import FAULT, REQUEST, RESPONSE, SNAPSHOT_VAR
from a2m.errors import UnsafePathError
from a2m.generator.project import HTTP
from a2m.ir import Bundle, Policy, ProxyEndpoint, TargetEndpoint
from a2m.policies.common import Method
from a2m.progress import Step
from a2m.runlog import get_logger
from a2m.verify.generated import GeneratedSteps
from a2m.verify.masking import Masker
from a2m.verify.mock_backend import MockBackend
from a2m.verify.model import CaseResult, ReviewFlag, Runner, VerificationResult, VerificationType

if TYPE_CHECKING:
    from a2m.verify.harness import VerifyConfig

DEFAULT_MAX_FIX_ATTEMPTS = 3
# The only files a fix may rewrite: the existing Mule configuration files of the app.
MULE_CONFIG_PARTS = layout.MULE_CONFIG_DIR
MULE_CONFIG_SUFFIX = ".xml"
FIXED = "fixed"
DECLINED = "cannot_fix"
NOT_USABLE = "the AI answer could not be used"
NONE = "none"
# Larger answers are refused: no generated Mule configuration file comes near this.
MAX_FILE_CHARS = 1_000_000
MAX_DIFF_CHARS = 20_000
MAX_REASON_CHARS = 500
PASSED_TYPES = (VerificationType.BATTERY, VerificationType.GOLDEN)
CONFIDENCES = frozenset(c.value for c in Confidence)
# What the AI is told in place of the rest of a reason that may quote a value of the proxy a2m cannot hide.
WITHHELD = (
    "a2m does not repeat the rest of the reason here: it quotes a value of the proxy (from the fix as a2m wrote it "
    "back, or from the build or Mule) in a form a2m cannot replace with a placeholder"
)
# What every masked value shows (a2m.verify.masking.masked and a2m.redaction.MASK both hold it).
MASK_MARK = redaction.MASK
# A value a2m masked: an optional prefix, then "*** (N chars)" (a2m.verify.masking.masked).
MASKED_VALUE = re.compile(r"\*\*\* \(\d+ chars\)")
# How a step a kept fix changed is named when it is no step of the proxy (a processor added to a flow).
FLOW_OWNER = "flow {name}"
FLOW_OWNER_TYPE = "Mule flow"
FLOW_TAG = f"{{{CORE}}}flow"
SUB_FLOW_TAG = f"{{{CORE}}}sub-flow"
CHOICE_TAG = f"{{{CORE}}}choice"
FLOW_REF_TAG = f"{{{CORE}}}flow-ref"
ERROR_HANDLER_TAG = f"{{{CORE}}}error-handler"
SET_VARIABLE_TAG = f"{{{CORE}}}set-variable"
TARGET_CALL_TAG = f"{{{HTTP}}}request"
DOC_NAME = f"{{{DOC}}}name"
UTF8_NAME = re.compile(r"(?i)utf-?8")
XML_DECLARATION = re.compile(r"""\A﻿?<\?xml\s[^>]*?\bencoding\s*=\s*["']([^"']*)["']""")


@dataclass(frozen=True, slots=True)
class FixAttempt:
    """One fix request and what came of it (see the module docstring).

    ``changed_files`` are the files the fix rewrote (paths relative to the app folder, forward slashes; empty
    when nothing was written), ``diff`` a readable diff of those changes (empty when none),
    ``failing_before``/``failing_after`` the failing tests of the best version before it and of the version it
    produced (the same number when it was never tested), ``helped`` whether it was kept, ``reason`` why.
    ``changed_steps`` are the steps of the generated project the version it wrote differs in (by step name; a
    processor added outside any step is named ``flow <name>``; empty when nothing was written), so the report can
    show them as AI work; ``confidence`` is the AI's confidence in the fix (high, medium, low), or None when it gave
    none.
    """

    number: int
    changed_files: tuple[str, ...]
    diff: str
    failing_before: int
    failing_after: int
    helped: bool
    reason: str
    changed_steps: tuple[str, ...] = ()
    confidence: str | None = None

    def to_json_data(self) -> dict[str, Any]:
        """The attempt as plain JSON data (the "attempts" list of verification.json)."""
        return {
            "number": self.number,
            "changed_files": list(self.changed_files),
            "diff": self.diff,
            "failing_before": self.failing_before,
            "failing_after": self.failing_after,
            "helped": self.helped,
            "reason": self.reason,
            "changed_steps": list(self.changed_steps),
            "confidence": self.confidence,
        }

    def masked(self, masker: Masker) -> FixAttempt:
        """This attempt with its texts masked by ``masker``."""
        return replace(self, diff=masker.mask(self.diff), reason=masker.mask(self.reason))


@dataclass(frozen=True, slots=True)
class FixLoopResult:
    """``result``: the verification result of the version kept (the best one); ``attempts``: every fix asked for."""

    result: VerificationResult
    attempts: tuple[FixAttempt, ...] = ()


def run_with_fixes(
    bundle: Bundle,
    app_dir: Path,
    *,
    runner: Runner,
    provider: Provider | None,
    max_fix_attempts: int = DEFAULT_MAX_FIX_ATTEMPTS,
    backend: MockBackend | None = None,
    golden: Path | None = None,
    config: VerifyConfig | None = None,
    generated: GeneratedSteps | None = None,
    masker: Masker | None = None,
) -> FixLoopResult:
    """Verify ``bundle``'s app in ``app_dir`` and run the fix loop on it (see the module docstring).

    The arguments after ``max_fix_attempts`` are those of :func:`a2m.verify.verify_proxy`. The AI is never asked
    when ``provider`` is None, ``max_fix_attempts`` is 0 or less, or the first result is not ``failed`` with a
    failing test (it passed, or was only built: static, or the app did not build or start).
    """
    # Imported here: the harness's engine stage runs this loop, so the harness imports this module.
    from a2m.verify.harness import verify_proxy_unmasked

    masker = masker or Masker.for_bundle(bundle)

    def verify() -> VerificationResult:
        # Unmasked: what the AI is told is built from it (swept, then masked); all the loop records or returns is
        # masked, as verify_proxy's result is.
        return verify_proxy_unmasked(
            bundle, app_dir, runner=runner, backend=backend, golden=golden, config=config, generated=generated,
            masker=masker,
        )

    first = verify()
    if provider is None or max_fix_attempts <= 0 or first.type is not VerificationType.FAILED or first.failed <= 0:
        return FixLoopResult(masker.mask_result(first), ())
    steps = generated if generated is not None else GeneratedSteps.planned(bundle)
    with masker.active(), masker.logging():
        return _FixLoop(bundle, app_dir, provider, max_fix_attempts, masker, verify, steps).run(first)


@dataclass(frozen=True, slots=True)
class _Fix:
    """A usable answer: the files it rewrites (path as given: full new text), the AI's notes and its confidence."""

    files: dict[str, str]
    notes: str
    confidence: str | None = None


@dataclass(frozen=True, slots=True)
class _Escape:
    """Why a fix was refused for a path that points outside the project; the loop then stops for the proxy."""

    reason: str


@dataclass(frozen=True, slots=True)
class _Change:
    """One file a fix rewrites: its path as given, where it is, its bytes and text now, its new text, and the steps
    of the generated file the new text differs in."""

    name: str
    target: Path
    before: bytes
    old_text: str
    new_text: str
    steps: tuple[str, ...] = ()


class _FixLoop:
    def __init__(
        self,
        bundle: Bundle,
        app_dir: Path,
        provider: Provider,
        max_attempts: int,
        masker: Masker,
        verify: Callable[[], VerificationResult],
        generated: GeneratedSteps,
    ) -> None:
        # ``verify`` returns the result unmasked (a2m.verify.harness.verify_proxy_unmasked): what the AI is told is
        # built from its text before any mask changes a value there; whatever is recorded is masked.
        self.bundle = bundle
        self.app_dir = app_dir
        self.provider = provider
        self.max_attempts = max_attempts
        self.masker = masker
        self.verify = verify
        self.log = get_logger()
        # The steps the AI translated: the only generated elements a fix may change in place.
        self.ai_steps = frozenset(step.name for step in generated.steps if step.method == Method.AI.value)
        self.step_types = {step.name: step.type for step in generated.steps}
        # The steps a2m generated (from a template or by the AI): the only policies a fix can be about.
        self.generated_steps = frozenset(
            step.name for step in generated.steps if step.method in (Method.AI.value, Method.TEMPLATE.value)
        )
        # Each Mule configuration file as a2m generated it, before any fix: what every fix is checked against.
        self.generated: dict[str, str] = {}
        # The placeholders the AI is shown (one table for the loop: a value keeps its placeholder across requests).
        self.table = Placeholders()
        # Whether the table knows the proxy's names yet (learned before the first request is built).
        self._named = False
        # Each attempt's reason as the next request tells the AI (by attempt number; see :meth:`_for_ai`).
        self._ai_reasons: dict[int, str] = {}

    def run(self, first: VerificationResult) -> FixLoopResult:
        self.generated = self._generated_files()
        best = first
        total = first.ran
        attempts: list[FixAttempt] = []
        stop = False
        not_run: str | None = None
        previous: FixAttempt | None = None
        for number in range(1, self.max_attempts + 1):
            try:
                # Read for every request, so an edit during the run is used; no request without it.
                template = load_prompt(FIX_PROMPT_FILE, what=ItemKind.FIX.value)
            except PromptError as exc:
                not_run = str(exc)
                self.log.warning("%s: no AI fix was asked for: %s", self.bundle.name, not_run)
                break
            progress.step(Step.AI_FIX, number, self.max_attempts)
            before = _failing(best, total)
            attempt, kept, stop = self._attempt(number, best, total, before, template, previous)
            attempts.append(attempt)
            previous = attempt
            self.log.log(
                logging.INFO if attempt.helped else logging.WARNING,
                "%s: AI fix attempt %d of %d: %s (failing tests %d -> %d%s)",
                self.bundle.name,
                number,
                self.max_attempts,
                attempt.reason,
                attempt.failing_before,
                attempt.failing_after,
                f"; changed {', '.join(attempt.changed_files)}" if attempt.changed_files else "",
            )
            if kept is not None:
                best = kept
            if best.type in PASSED_TYPES or stop:
                break
        count = len(attempts)
        plural = "" if count == 1 else "s"
        if not attempts and not_run is not None:
            message = f"{best.message}; the AI fix loop did not run: {not_run}"
        elif best.type in PASSED_TYPES:
            message = f"{best.message}; passed after {count} AI fix attempt{plural}"
        else:
            message = f"still failing after {count} fix attempt{plural}: {best.message}"
            if not_run is not None:
                message += f"; the AI fix loop stopped: {not_run}"
        flags = self._review_flags(attempts, best)
        # Every verification result of the loop is unmasked (see run_with_fixes): masked here, as verify_proxy does.
        result = self.masker.mask_result(replace(best, message=message, review_flags=(*best.review_flags, *flags)))
        return FixLoopResult(result, tuple(attempts))

    def _attempt(
        self,
        number: int,
        best: VerificationResult,
        total: int,
        before: int,
        template: str,
        previous: FixAttempt | None = None,
    ) -> tuple[FixAttempt, VerificationResult | None, bool]:
        """One fix request: the attempt, the new best result when it is kept, and whether the loop must stop."""

        def failed(reason: str, *, stop: bool = False) -> tuple[FixAttempt, None, bool]:
            return FixAttempt(number, (), "", before, before, False, self._reason(number, reason)), None, stop

        request = self._request(number, best, template, previous)
        try:
            # The one way to the provider: every text of the request is masked as the last step.
            raw = self.provider.complete(_sealed(request, self.masker))
        except ProviderLimitError as exc:
            # The same request would be cut off (or time out) the same way again.
            return failed(f"the AI's answer could not be complete, and the fix loop stops: {_text_of(exc)}", stop=True)
        except Exception as exc:  # noqa: BLE001 (the provider boundary: any failure is this attempt's provider error)
            return failed(f"the AI provider failed: {type(exc).__name__}: {_text_of(exc)}")
        answer = _parse_answer(raw)
        if isinstance(answer, str):
            return failed(answer)
        changes = self._check(answer.files)
        if isinstance(changes, _Escape):
            # An answer that reaches outside the project is not trusted again for this proxy.
            return failed(f"the fix was refused, nothing was written, and the fix loop stops: {changes.reason}", stop=True)
        if isinstance(changes, str):
            return failed(f"the fix was refused, nothing was written: {changes}")
        # Compared as XML (comments kept): an echo that only re-indents or re-quotes a file changes nothing.
        changed = [change for change in changes if not _same_document(change.old_text, change.new_text)]
        notes = f"; AI notes: {answer.notes}" if answer.notes else ""
        confidence = answer.confidence
        if not changed:
            # Nothing to write. The app is tested once more, but that run is never credited to the AI.
            after = self.verify()
            failing = _failing(after, total)
            rerun = (
                "the tests passed on this re-run with nothing changed, which is not a fix"
                if after.type in PASSED_TYPES
                else f"failing tests on a re-run: {failing}"
            )
            reason = f"the AI changed nothing, so it did not help ({rerun}){notes}"
            unchanged = FixAttempt(number, (), "", before, failing, False, self._reason(number, reason), (), confidence)
            return unchanged, None, False
        problem = self._apply(changed)
        if problem is not None:
            return failed(f"the fix could not be written, nothing was kept: {problem}")
        names = tuple(sorted(change.name for change in changed))
        steps = tuple(sorted({step for change in changed for step in change.steps}))
        diff = self._diff(changed)
        try:
            after = self.verify()
        except BaseException:
            # Whatever stops the re-test (a crash, an interrupt) never leaves the unchecked fix in the project.
            self._restore(changed)
            raise
        failing = _failing(after, total)

        def attempt(after_count: int, helped: bool, reason: str) -> FixAttempt:
            return FixAttempt(
                number, names, diff, before, after_count, helped, self._reason(number, reason), steps, confidence
            )

        if after.type in PASSED_TYPES:
            return attempt(0, True, f"every test passed{notes}"), after, False
        comparable = after.type is VerificationType.FAILED and after.ran >= total
        if comparable and failing < before:
            reason = f"{before - failing} fewer failing tests; kept as the base for the next try{notes}"
            return attempt(failing, True, reason), after, False
        self._restore(changed)
        undone = "it was undone"
        if after.type is VerificationType.FAILED and after.ran == 0:
            reason = f"the fixed project did not build or start ({after.message}); {undone}"
        elif not comparable:
            reason = f"the fixed app could not be fully tested ({after.message}); {undone}"
        elif failing > before:
            reason = f"the fix made more tests fail ({before} to {failing}); {undone}"
        else:
            reason = f"the fix did not reduce the failing tests ({before} to {failing}); {undone}"
        return attempt(failing, False, reason + notes), None, False

    def _review_flags(self, attempts: Sequence[FixAttempt], best: VerificationResult) -> tuple[ReviewFlag, ...]:
        """A review flag for each step a kept fix changed, when the AI's confidence is low or not given, or no test
        covers the step (the report then shows the step as AI work that needs review)."""
        untested = {item.name for item in best.untested}
        flags: list[ReviewFlag] = []
        seen: set[str] = set()
        for attempt in attempts:
            if not attempt.helped:
                continue
            for step in attempt.changed_steps:
                why = []
                if attempt.confidence is None:
                    why.append("the AI gave no confidence for it")
                elif attempt.confidence == Confidence.LOW.value:
                    why.append("the AI's confidence in it is low")
                if step in untested:
                    why.append("no test covers this step")
                if not why or step in seen:
                    continue
                seen.add(step)
                reason = f"AI fix attempt {attempt.number} changed this step and {' and '.join(why)}, so it needs review"
                flags.append(ReviewFlag(step, self.step_types.get(step, FLOW_OWNER_TYPE), self._short(reason)))
        return tuple(flags)

    # ------------------------------------------------------------ the request

    def _request(
        self, number: int, best: VerificationResult, template: str, previous: FixAttempt | None = None
    ) -> AiRequest:
        """The fix request, built from placeholdered material only (see the module docstring)."""
        table = self.table
        if not self._named:
            _learn_names(table, self.bundle, self.step_types, self.generated)
            self._named = True
        failing = [case for case in best.cases if not case.passed]
        original = _originals(self.bundle, self.generated_steps, table)
        # Read, never shown: the values of the other policies and of the endpoints (the conditions a2m writes into
        # Mule comments), so the sweep of the comments and of the previous reason knows them.
        for policy in self.bundle.policies:
            if policy.name not in self.generated_steps:
                table.apigee(policy.raw_xml)
        for raw_xml in (
            *(endpoint.raw_xml for endpoint in self.bundle.proxy_endpoints),
            *(endpoint.raw_xml for endpoint in self.bundle.target_endpoints),
        ):
            table.apigee(raw_xml)
        diff = table.diff(_failures(failing))
        mule = self._mule_files(table)
        values = {
            "proxy": self.bundle.name,
            "attempt": str(number),
            "max_attempts": str(self.max_attempts),
            "original": original,
            "mule": mule,
            "diff": diff,
            "ai_steps": _ai_steps_text(self.ai_steps),
            "previous": self._previous(previous),
        }
        return AiRequest(ItemKind.FIX, self.bundle.name, original, render(template, values))

    def _mule_files(self, table: Placeholders) -> str:
        """Every Mule configuration file as the AI is shown it (placeholdered; a file that cannot be read as XML is
        named, not shown, and a fix of it is refused)."""
        texts: dict[str, str] = {}
        for path in self._config_files():
            texts[path.relative_to(self.app_dir).as_posix()] = path.read_text(encoding="utf-8", errors="replace")
        shown = table.mule(texts)
        parts = []
        for rel, text in shown.items():
            if text is None:
                parts.append(f"### {rel}\n\n(it could not be read as XML, so it is not shown)")
            else:
                parts.append(f"### {rel}\n\n```xml\n{text.rstrip()}\n```")
        return "\n\n".join(parts) or NONE

    def _config_files(self) -> list[Path]:
        folder = self.app_dir.joinpath(*MULE_CONFIG_PARTS)
        if safefs.is_link(folder) or not folder.is_dir():
            return []
        return [
            path
            for path in sorted(folder.glob(f"*{MULE_CONFIG_SUFFIX}"))
            if safefs.is_regular_file(self.app_dir, path)
        ]

    def _generated_files(self) -> dict[str, str]:
        """Each Mule configuration file's text now (before any fix), by its path relative to the app folder."""
        found: dict[str, str] = {}
        for path in self._config_files():
            try:
                found[path.relative_to(self.app_dir).as_posix()] = _read_plain(self.app_dir, path).decode("utf-8")
            except (OSError, UnsafePathError, UnicodeDecodeError):
                continue  # a fix of this file is refused: there is nothing to check it against
        return found

    # ------------------------------------------------------------ checking and applying a fix

    def _check(self, files: dict[str, str]) -> list[_Change] | _Escape | str:
        """The changes ``files`` makes, each checked; the reason when any one of them is refused (an
        :class:`_Escape` when one points outside the project)."""
        changes: list[_Change] = []
        for name in sorted(files):
            target = _target(self.app_dir, name)
            if isinstance(target, (_Escape, str)):
                return target
            try:
                before = _read_plain(self.app_dir, target)
                old_text = before.decode("utf-8")
            except (OSError, UnsafePathError, UnicodeDecodeError) as exc:
                return f"the file {name!r} cannot be read ({exc})"
            generated = self.generated.get(name)
            if generated is None:
                return f"the file {name} could not be read as a2m generated it, so a fix of it cannot be checked"
            # Every placeholder back to the exact value it stands for, before any check runs.
            try:
                restored = self.table.restore(name, files[name])
            except PlaceholderError as exc:
                return f"the file {name} in the fix {exc}"
            # Exactly what would be written (every file write masks secrets), so what is checked is what is written.
            new_text = redaction.redact(restored)
            problem = _masked_left(name, new_text, old_text, restored)
            if problem is not None:
                return problem
            checked = _check_file(name, generated, new_text, self.ai_steps)
            if isinstance(checked, str):
                return checked
            changes.append(_Change(name, target, before, old_text, new_text, checked))
        return changes

    def _apply(self, changes: Sequence[_Change]) -> str | None:
        written: list[_Change] = []
        try:
            for change in changes:
                safefs.write_text_atomic(self.app_dir, change.target, change.new_text)
                written.append(change)
        except (OSError, UnsafePathError) as exc:
            self._restore(written)
            return f"{type(exc).__name__}: {exc}"
        return None

    def _restore(self, changes: Sequence[_Change]) -> None:
        for change in changes:
            safefs.write_bytes_atomic(self.app_dir, change.target, change.before)

    def _diff(self, changes: Sequence[_Change]) -> str:
        lines: list[str] = []
        for change in changes:
            lines.extend(
                difflib.unified_diff(
                    change.old_text.splitlines(keepends=True),
                    change.new_text.splitlines(keepends=True),
                    fromfile=f"a/{change.name}",
                    tofile=f"b/{change.name}",
                )
            )
        joined = "".join(line if line.endswith("\n") else line + "\n" for line in lines)
        text = self.masker.mask(joined)
        return text if len(text) <= MAX_DIFF_CHARS else text[:MAX_DIFF_CHARS] + "\n... (diff cut short)\n"

    def _short(self, text: str) -> str:
        # Masked before it is cut short, so no part of a secret survives the cut.
        text = self.masker.mask(" ".join(text.split()))
        return text if len(text) <= MAX_REASON_CHARS else text[:MAX_REASON_CHARS] + "..."

    def _reason(self, number: int, text: str) -> str:
        """Attempt ``number``'s reason ``text`` as recorded (:meth:`_short`); what the next request tells the AI is
        kept apart (:meth:`_for_ai`), built from the whole text."""
        self._ai_reasons[number] = self._for_ai(text)
        return self._short(text)

    def _for_ai(self, text: str) -> str:
        """``text`` (a reason, which may quote the restored fix or a build or deploy error about it) as the AI may
        be told it: whole (before any cut, so no value is cut where the sweep cannot recognise it) and unmasked (the
        loop's verification results are unmasked, so a value the masker would change is still whole), every value
        the table knows replaced by its placeholder in every spelling (:meth:`Placeholders.sweep`); a2m's own opening
        words and :data:`WITHHELD` when a value could still be read in it (:meth:`Placeholders.holds_value`) or the
        masker still finds a credential in it (:func:`_masks_credential`); then masked outside the placeholders and
        cut short. The text is swept as it is, blanks included (a value with two blanks or a tab in it is found
        there), and swept again once its blanks are collapsed (a value with one blank where the text has two); both
        are checked, and only then is it shown with its blanks collapsed."""
        raw = self.table.sweep(text)
        swept = self.table.sweep(" ".join(raw.split()))
        if self._unsafe(raw) or self._unsafe(swept):
            swept = _withheld(swept, self._unsafe)
        swept = _mask_around_placeholders(swept, self.masker)
        return swept if len(swept) <= MAX_REASON_CHARS else swept[:MAX_REASON_CHARS] + "..."

    def _unsafe(self, text: str) -> bool:
        """Whether ``text`` (swept) may not be shown to the AI: a value of the table could still be read in it, or the
        masker would mask a credential in it (the masked form keeps the first characters, and a token is masked only
        up to the first character a token cannot hold). Both are asked of the text without the names a2m shows the AI
        anyway (:meth:`Placeholders.without_names`: the proxy's name, which every build and deploy error starts with,
        and every known name), so a value whose letters only stand inside such a name (the base path ``/orders`` in
        ``orders-api``) does not withhold the reason; a value anywhere else still does."""
        return self.table.holds_unshown_value(text) or _masks_credential(self.table.without_names(text), self.masker)

    def _previous(self, previous: FixAttempt | None) -> str:
        """What came of the previous attempt, as the AI is told it: its reason as :meth:`_for_ai` made it, swept
        once more with every value the table knows now; a2m's own words alone when a value could still be read."""
        if previous is None:
            return _previous_text(None)
        reason = self._ai_reasons.get(previous.number, WITHHELD)
        text = self.table.sweep(_previous_text(previous, reason))
        if self._unsafe(text):
            return _previous_text(previous, WITHHELD)
        return text


def _failing(result: VerificationResult, total: int) -> int:
    """The failing tests of ``result`` out of ``total`` (the tests of the first run); a test that did not run fails."""
    return max(total, result.ran) - result.passed


def _text_of(exc: BaseException) -> str:
    try:
        return str(exc)
    except Exception:  # noqa: BLE001 (an exception whose text cannot be built still names its type)
        return "(no message)"


# ---------------------------------------------------------------- the prompt's parts


def _sealed(request: AiRequest, masker: Masker) -> AiRequest:
    """``request`` with every text field masked by ``masker`` (:meth:`Masker.mask`, outside the placeholders, which
    stand for no value of their own): the last step before any request reaches the provider."""
    changes: dict[str, Any] = {
        item.name: _mask_around_placeholders(value, masker)
        for item in fields(request)
        if isinstance(value := getattr(request, item.name), str) and not isinstance(value, ItemKind)
    }
    return replace(request, **changes)


def _mask_around_placeholders(text: str, masker: Masker) -> str:
    pieces = TOKEN_SPLIT.split(text)
    return "".join(piece if index % 2 else masker.mask(piece) for index, piece in enumerate(pieces))


def _ai_steps_text(ai_steps: frozenset[str]) -> str:
    if not ai_steps:
        return (
            "none: every step of this proxy was generated from a template, so a fix may only add new elements "
            "between the generated ones"
        )
    return "\n".join(f"- {name}" for name in sorted(ai_steps))


def _previous_text(previous: FixAttempt | None, reason: str = "") -> str:
    if previous is None:
        return "none: this is the first attempt"
    outcome = "kept" if previous.helped else "not kept"
    return f"Attempt {previous.number} was {outcome}: {reason}"


# Where a2m's own opening words of a reason end: every reason starts with them, and anything quoted comes after.
_OPENING_END = re.compile(r"[:(]")


def _withheld(text: str, unsafe: Callable[[str], bool]) -> str:
    """A swept reason ``text`` that may still hold a value: its opening words (a2m's own, up to the first ":" or
    "("; left out too when ``unsafe`` says they may hold one) and :data:`WITHHELD`."""
    opening = _OPENING_END.split(text, maxsplit=1)[0].strip()
    if not opening or unsafe(opening):
        return WITHHELD
    return f"{opening}: {WITHHELD}"


def _masks_credential(text: str, masker: Masker) -> bool:
    """Whether ``masker`` masks a credential in ``text`` (outside its placeholders): masking makes a masked value
    (``abcd*** (12 chars)``) the text did not hold."""
    return len(MASKED_VALUE.findall(_mask_around_placeholders(text, masker))) > len(MASKED_VALUE.findall(text))



def _originals(bundle: Bundle, generated: frozenset[str], table: Placeholders) -> str:
    """The original Apigee policy of every generated step (from a template or by the AI), placeholdered, with the
    source code of each custom code policy; every other policy (unsupported, skipped, used by no step) by name and
    type only."""
    shown = [policy for policy in bundle.policies if policy.name in generated]
    others = [policy for policy in bundle.policies if policy.name not in generated]
    parts = [_policy_text(policy, bundle, table) for policy in shown]
    if others:
        listed = "\n".join(f"- {policy.name} ({policy.type})" for policy in others)
        parts.append(
            "The proxy's policies with no generated step (unsupported, skipped, or used by no flow step), by name "
            f"and type only:\n\n{listed}"
        )
    return "\n\n".join(parts) or NONE


def _learn_names(table: Placeholders, bundle: Bundle, steps: Mapping[str, str], generated: Mapping[str, str]) -> None:
    """Teach ``table`` the proxy's names before anything is shown: the policy, step, endpoint and flow names of the
    IR, the names every policy and endpoint declares, and the names of the Mule files as a2m generated them (see
    :mod:`a2m.verify.placeholders`; a known name is shown only in a Mule name attribute)."""
    endpoints: tuple[ProxyEndpoint | TargetEndpoint, ...] = (*bundle.proxy_endpoints, *bundle.target_endpoints)
    table.know(
        (
            bundle.name,
            *(policy.name for policy in bundle.policies),
            *steps,
            *(endpoint.name for endpoint in endpoints),
            *(flow.name for endpoint in endpoints for flow in endpoint.flows),
        )
    )
    # The proxy's name (the app's name too) is in the prompt and starts every build and deploy error.
    table.mention((bundle.name,))
    for raw_xml in (*(policy.raw_xml for policy in bundle.policies), *(endpoint.raw_xml for endpoint in endpoints)):
        table.learn_apigee(raw_xml)
    table.learn_mule(generated.values())


def _policy_text(policy: Policy, bundle: Bundle, table: Placeholders) -> str:
    """One policy as the AI is shown it: its XML placeholdered, and the placeholdered source code of a custom code
    policy."""
    shown = table.apigee(policy.raw_xml)
    head = f"### {policy.type} policy {policy.name} ({policy.file})"
    if shown is None:
        return f"{head}\n\n(its XML could not be read, so it is not shown)"
    text = f"{head}\n\n```xml\n{shown.strip()}\n```"
    source = callout_source(policy, bundle.resources)
    if not isinstance(source, str):
        text += f"\n\nIts source code ({source.file}):\n\n```\n{table.code(source.original, source.kind).rstrip()}\n```"
        for file, included in source.includes:
            text += f"\n\nIncluded script {file}:\n\n```\n{table.code(included, source.kind).rstrip()}\n```"
    return text


def _failures(failing: Sequence[CaseResult]) -> str:
    parts = []
    for case in failing:
        about = f" (policy {case.policy}, {case.policy_type})" if case.policy else ""
        lines = "\n".join(f"    {line}" for line in case.diff.splitlines()) or "    (no detail)"
        parts.append(f"- test {case.name}{about}:\n{lines}")
    return "\n".join(parts) or NONE


# ---------------------------------------------------------------- reading the answer


def _parse_answer(raw: object) -> _Fix | str:
    """The usable fix in the AI's answer, or why there is none (it declined, or the answer could not be used)."""
    if not isinstance(raw, str) or not raw.strip():
        return f"{NOT_USABLE}: it is empty"
    body = raw.strip()
    fenced = FENCE.fullmatch(body)
    if fenced is not None:
        body = fenced.group(1).strip()
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):  # ValueError: JSONDecodeError, or a number too long to convert
        return f"{NOT_USABLE}: it is not one JSON object"
    if not isinstance(data, dict):
        return f"{NOT_USABLE}: it is not one JSON object"
    status = data.get("status")
    if status == DECLINED:
        extra = sorted(str(key) for key in set(data) - {"status", "reason", "notes"})
        reason = data.get("reason")
        if extra:
            return f"{NOT_USABLE}: it has fields a2m does not expect: {', '.join(extra)}"
        if not isinstance(reason, str) or not reason.strip():
            return f"{NOT_USABLE}: it declines without a reason"
        return f"the AI could not fix it: {reason}"
    if status != FIXED:
        return f"{NOT_USABLE}: its status {status!r} is not {FIXED!r} or {DECLINED!r}"
    extra = sorted(str(key) for key in set(data) - {"status", "files", "notes", "confidence"})
    if extra:
        return f"{NOT_USABLE}: it has fields a2m does not expect: {', '.join(extra)}"
    confidence = data.get("confidence")
    if confidence is not None and (not isinstance(confidence, str) or confidence not in CONFIDENCES):
        return f"{NOT_USABLE}: its confidence {confidence!r} is not high, medium or low"
    files = data.get("files")
    if not isinstance(files, dict):
        return f"{NOT_USABLE}: its files are not an object of path to file text"
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in files.items()):
        return f"{NOT_USABLE}: its files are not an object of path to file text"
    notes = data.get("notes", "")
    if not isinstance(notes, str):
        return f"{NOT_USABLE}: its notes are not text"
    return _Fix(dict(files), notes.strip(), confidence)


def _target(app_dir: Path, name: str) -> Path | _Escape | str:
    """Where the file ``name`` of a fix is, or why a fix may not write it (see the module docstring): an
    :class:`_Escape` when it points outside the project (an absolute path, a ``..`` part, a link)."""
    shown = repr(name)
    outside = _Escape(f"the file {shown} is outside the Mule project; an AI fix may only change files inside it")
    if not name or "\x00" in name or name.startswith("/") or PureWindowsPath(name).drive:
        return outside
    if "\\" in name:
        return f"the file {shown} is not written with forward slashes (src/main/mule/<file>.xml)"
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return outside
    if (
        len(parts) != len(MULE_CONFIG_PARTS) + 1
        or tuple(parts[:-1]) != MULE_CONFIG_PARTS
        or not parts[-1].endswith(MULE_CONFIG_SUFFIX)
    ):
        return (
            f"the file {shown} is not a Mule configuration file; an AI fix may only change the files under "
            f"{'/'.join(MULE_CONFIG_PARTS)}/ it was shown"
        )
    target = app_dir.joinpath(*parts)
    if any(safefs.is_link(app_dir.joinpath(*parts[:end])) for end in range(1, len(parts) + 1)):
        return _Escape(f"the file {shown} is reached through a link, which could lead outside the Mule project")
    if not safefs.is_regular_file(app_dir, target):
        return (
            f"the file {shown} is not one of the project's Mule configuration files (a new file, a link or a "
            "folder); an AI fix may only change the files it was shown"
        )
    try:
        target.resolve(strict=True).relative_to(app_dir.resolve(strict=True))
    except (OSError, ValueError):
        return outside
    return target


def _read_plain(root: Path, target: Path) -> bytes:
    fd = safefs.open_plain_file(root, target, os.O_RDONLY)
    with os.fdopen(fd, "rb") as handle:
        return handle.read()


# ---------------------------------------------------------------- what may not be written


def _masked_left(name: str, new_text: str, old_text: str, restored: str | None = None) -> str | None:
    """Why ``new_text`` may not be written because it still holds a mask a2m made, or None.

    Only what a2m's masking produces counts: a masked value (:func:`a2m.verify.masking.masked`, ``abcd*** (12
    chars)``) on a line the file does not already hold, and a secret :func:`a2m.redaction.redact` replaced in the
    answer (``restored``, the answer before that redaction). Asterisks a proxy really writes (a card number shown
    as ``'************' ++ last4``) are not a mask."""
    known = {line.strip() for line in old_text.splitlines()}
    news = new_text.splitlines()
    befores = restored.splitlines() if restored is not None else news
    for number, line in enumerate(news, start=1):
        redacted = number <= len(befores) and befores[number - 1] != line
        if line.strip() not in known and (redacted or MASKED_VALUE.search(line) is not None):
            return (
                f"the file {name} in the fix holds masked text at line {number} ('{MASK_MARK}' in place of a value "
                "a2m did not show the AI), which a2m cannot put back, so it would write the mask into the project"
            )
    return None


def _same_document(old: str, new: str) -> bool:
    """Whether ``new`` is for sure the same XML document as ``old`` (comments kept; attribute order and quoting aside,
    and every element text and every ``#[...]`` expression attribute compared by :func:`_code_shape`, so re-indenting
    or reflowing a DataWeave script between its tokens counts as no change): an answer that only re-indents a file
    changes nothing. Any difference a2m cannot prove harmless (inside a string, regular expression, template literal
    or comment, or where the lexer is unsure) is a change, which is written and re-tested, never dropped."""
    if old == new:
        return True
    try:
        return _canonical(old) == _canonical(new)
    except (ET.ParseError, ValueError, DefusedXmlException):
        return False


def _canonical(text: str) -> str:
    """``text`` (XML) canonicalized with every text and tail reduced to its :func:`_code_shape` and every attribute
    that starts with ``#[`` to :func:`_expression_shape`."""
    parser = SafeET.DefusedXMLParser(target=ET.TreeBuilder(insert_comments=True), forbid_dtd=True)
    parser.feed(text)
    root = parser.close()
    for node in root.iter():
        node.text = _code_shape(node.text) if node.text is not None else None
        node.tail = _code_shape(node.tail) if node.tail is not None else None
        for key, value in node.attrib.items():
            if value.lstrip().startswith(EXPRESSION_START):
                node.attrib[key] = _expression_shape(value)
    return ET.canonicalize(ET.tostring(root, encoding="unicode"), with_comments=True)


# How a Mule attribute that holds a DataWeave expression starts.
EXPRESSION_START = "#["


def _expression_shape(value: str) -> str:
    """A Mule attribute ``value`` that starts with ``#[``: when the whole value (blanks around it aside, which stay as
    written) is one ``#[...]`` expression, ``#[shape:`` and the expression's :func:`_code_shape`; otherwise (literal
    text around it, or an end a2m cannot find for sure) ``#[exact:`` and the value as written. Both start with ``#[``,
    so neither can equal a value that is left as it is."""
    stripped = value.strip()
    if expression_end(stripped, len(EXPRESSION_START)) == len(stripped) - 1:
        lead = value[: len(value) - len(value.lstrip())]
        trail = value[len(value.rstrip()) :]
        body = stripped[len(EXPRESSION_START) : -1]
        return f"{EXPRESSION_START}shape:{len(lead)}:{lead}{len(trail)}:{trail}{_code_shape(body)}"
    return f"{EXPRESSION_START}exact:{value}"


def _code_shape(text: str, language: str = DATAWEAVE) -> str:
    """``text`` (a DataWeave script or other element text; or code in ``language``) reduced to what can change its
    meaning by the placeholders' own fail-safe lexer (:func:`a2m.verify.placeholders.code_shape`): blanks between
    tokens read for sure are reduced, and every literal (string, regular expression, template literal, comment) and
    every point the lexer is unsure of is kept byte for byte."""
    return code_shape(text, language)


# ---------------------------------------------------------------- checking a rewritten file


def _check_file(
    name: str, generated_text: str, new_text: str, ai_steps: frozenset[str] = frozenset()
) -> str | tuple[str, ...]:
    """Why the new text of Mule configuration file ``name`` may not be written, or the steps of the file as a2m
    generated it (``generated_text``) it differs in when it may (see :class:`_Structure`; ``ai_steps``: the steps
    the AI translated)."""
    if len(new_text) > MAX_FILE_CHARS:
        return f"the file {name} in the fix is longer than {MAX_FILE_CHARS} characters"
    declared = XML_DECLARATION.match(new_text)
    if declared is not None and UTF8_NAME.fullmatch(declared.group(1).strip()) is None:
        return f"the file {name} in the fix declares the encoding {declared.group(1)!r}; a2m writes UTF-8"
    try:
        # No DTD at all: Mule's own parser must never be pointed at a DTD (or entity) the AI wrote.
        new_root = SafeET.fromstring(new_text, forbid_dtd=True)
    except DefusedXmlException as exc:
        return f"the file {name} in the fix holds a DTD or entity declaration ({exc!r}), which a2m refuses"
    except (ET.ParseError, ValueError) as exc:
        return f"the file {name} in the fix is not well-formed XML ({exc}), so the project would be malformed"
    try:
        generated_root = SafeET.fromstring(generated_text)
    except (ET.ParseError, ValueError) as exc:
        return f"the project's own file {name} cannot be read as XML ({exc})"
    structure = _Structure(generated_root, ai_steps)
    problem = structure.check(new_root)
    if problem is not None:
        return f"the file {name} in the fix {problem}"
    return structure.steps


class _Structure:
    """The positional check of a rewritten Mule configuration file against the file as a2m generated it.

    The children of each generated element must come back in the new file once each, in the same order: unchanged
    (the same element with the same content), changed in place when the element is a step the AI translated (it
    keeps its ``doc:name`` label and passes :func:`_check_new` for its side), or holding the same tag, attributes and
    text with its own children checked the same way. Any other element of the new file is an added one: it may not
    sit directly in ``<mule>``, may not be a copy of a generated element or carry a generated step's label, and must
    pass :func:`_check_new` for the side of the place it is added at. A generated element with no place in the new
    file was removed, moved or replaced, and the file is refused. :attr:`steps` names the steps the new file changes
    or adds to (a processor added outside any step is named after its flow).
    """

    def __init__(self, generated: ET.Element, ai_steps: frozenset[str]) -> None:
        self.generated = generated
        self.ai_steps = ai_steps
        self.digests = _digests(generated)
        self.copies = set(self.digests.values())
        self.labels = {label for element in generated.iter() if (label := _label(element)) is not None}
        self.sides, self.gaps = _sides(generated)
        self.new_digests: dict[int, str] = {}
        self.steps: tuple[str, ...] = ()

    def check(self, new_root: ET.Element) -> str | None:
        if _shallow(new_root) != _shallow(self.generated):
            return f"changes the <{_local(new_root)}> element itself"
        self.new_digests = _digests(new_root)
        problem, steps = self._align(self.generated, new_root, None)
        self.steps = tuple(sorted(steps))
        return problem

    def _align(self, old: ET.Element, new: ET.Element, owner: str | None) -> tuple[str | None, set[str]]:
        """Why the children of ``new`` are not those of the generated ``old`` with allowed changes (see the class
        docstring), and the steps they change. The recursion follows the generated file, so its depth is bounded by
        what a2m wrote, never by the answer."""
        steps: set[str] = set()
        olds = list(old)
        news = list(new)
        index = 0
        for position, child in enumerate(news):
            if (child.tail or "").strip():
                return f"adds text after a <{_local(child)}> element", steps
            if index < len(olds):
                generated = olds[index]
                if self.new_digests[id(child)] == self.digests[id(generated)]:
                    index += 1
                    continue
                label = _label(generated)
                if label is not None and label in self.ai_steps and _label(child) == label:
                    problem = _check_new(child, self.sides[id(generated)])
                    if problem is not None:
                        return problem, steps
                    steps.add(label)
                    index += 1
                    continue
                if _shallow(child) == _shallow(generated):
                    problem, inner = self._align(generated, child, _owner(generated, owner))
                    if problem is None:
                        steps |= inner
                        index += 1
                        continue
                    # Not the generated element with allowed changes: it may still be an element added before it.
                    if self._added(child, old, index) is not None:
                        return problem, steps
                    steps.add(owner or _local(old))
                    continue
            problem = self._added(child, old, index)
            if problem is not None:
                waiting = olds[index] if index < len(olds) else None
                later = {self.new_digests[id(other)] for other in news[position + 1 :]}
                if waiting is not None and self.digests[id(waiting)] not in later and (
                    self.new_digests[id(child)] in self.copies or child.tag == waiting.tag
                ):
                    # A generated element shows up (or one of the same kind stands) where an earlier generated one
                    # is missing for good: that one was changed, removed or moved.
                    return _removed(waiting, owner), steps
                if waiting is not None and self.digests[id(waiting)] in later and self.new_digests[id(child)] in self.copies:
                    reordered = (
                        f"changes the order of the elements a2m generated: {_described(child)} now comes before "
                        f"{_described(waiting)}{_inside(owner)}; a fix must keep every element a2m generated in its place"
                    )
                    return reordered, steps
                return problem + _inside(owner), steps
            steps.add(owner or _local(old))
        if index < len(olds):
            return _removed(olds[index], owner), steps
        return None, steps

    def _added(self, child: ET.Element, parent: ET.Element, gap: int) -> str | None:
        """Why ``child`` may not be added to the generated ``parent`` before its child number ``gap``, or None."""
        if parent is self.generated:
            return f"adds a <{_local(child)}> element outside any flow"
        if self.new_digests[id(child)] in self.copies:
            return f"moves or duplicates {_described(child)} a2m generated"
        copied = next(
            (label for element in child.iter() if (label := _label(element)) is not None and label in self.labels),
            None,
        )
        if copied is not None:
            return f"adds an element labelled {copied!r}, the label of a step a2m generated (a moved or duplicated step)"
        return _check_new(child, self.gaps.get((id(parent), gap), REQUEST))


def _label(element: ET.Element) -> str | None:
    """The step name a2m labelled ``element`` with (``doc:name``), or None."""
    return element.get(DOC_NAME)


def _owner(element: ET.Element, owner: str | None) -> str | None:
    """Whom a change inside the generated ``element`` is credited to: its step, its flow, or ``owner``."""
    if element.tag in (FLOW_TAG, SUB_FLOW_TAG):
        return FLOW_OWNER.format(name=element.get("name", ""))
    return _label(element) or owner


def _inside(owner: str | None) -> str:
    if owner is None:
        return ""
    return f" (inside {owner})" if owner.startswith(FLOW_OWNER.format(name="")) else f" (inside step {owner!r})"


def _removed(element: ET.Element, owner: str | None) -> str:
    return (
        f"changes, removes or moves {_described(element)}{_inside(owner)} that a2m generated; a fix must keep every "
        "element a2m generated, once each, in the same order and unchanged, and may only change in place the steps "
        "the AI translated"
    )


def _described(element: ET.Element) -> str:
    label = _label(element)
    return f"the <{_local(element)}> element" + (f" of step {label!r}" if label is not None else "")


def _sides(root: ET.Element) -> tuple[dict[int, str], dict[tuple[int, int], str]]:
    """The side of the flow each element of the generated ``root`` starts on (by ``id``), and the side at each place
    between the children of an element (by ``id`` of the element and the index of the child after the place).

    A flow starts on the request side; the target call (``http:request``), or the saved request snapshot
    (``set-variable`` of :data:`~a2m.conditions.variables.SNAPSHOT_VAR`) where a route ends without a target call,
    turns it to the response side; after a
    ``choice`` it is the response side when any branch got there. A flow's error handler is the fault side; the
    error handler of a scope inside a flow keeps the side it is on. A sub-flow is on the side of the ``flow-ref``
    that calls it (the request side when calls from both sides or none are found)."""
    sides: dict[int, str] = {}
    gaps: dict[tuple[int, int], str] = {}
    calls: dict[str, set[str]] = {}

    def walk(element: ET.Element, side: str) -> str:
        sides[id(element)] = side
        if element.tag == TARGET_CALL_TAG or _is_snapshot(element):
            return RESPONSE
        if element.tag == FLOW_REF_TAG:
            calls.setdefault(element.get("name", ""), set()).add(side)
            return side
        current = side
        branches = side
        for index, child in enumerate(element):
            gaps[(id(element), index)] = current
            if child.tag == ERROR_HANDLER_TAG:
                walk(child, FAULT if element.tag in (FLOW_TAG, SUB_FLOW_TAG) else current)
            elif element.tag == CHOICE_TAG:
                if walk(child, side) == RESPONSE:
                    branches = RESPONSE
            else:
                after = walk(child, current)
                if _is_snapshot(child) and any(_calls_target(later) for later in list(element)[index + 1 :]):
                    after = current  # the snapshot right before the target call: the call turns the side
                current = after
        gaps[(id(element), len(element))] = current
        return branches if element.tag == CHOICE_TAG else current

    sub_flows = []
    for child in root:
        if child.tag == SUB_FLOW_TAG:
            sub_flows.append(child)
        else:
            walk(child, REQUEST)
    for sub_flow in sub_flows:
        found = calls.get(sub_flow.get("name", ""), set())
        walk(sub_flow, next(iter(found)) if len(found) == 1 else REQUEST)
    return sides, gaps


def _is_snapshot(element: ET.Element) -> bool:
    return element.tag == SET_VARIABLE_TAG and element.get("variableName") == SNAPSHOT_VAR


def _calls_target(element: ET.Element) -> bool:
    return any(node.tag == TARGET_CALL_TAG for node in element.iter())


def _check_new(element: ET.Element, side: str = REQUEST) -> str | None:
    """Why the new or changed ``element`` may not be written on ``side`` of the flow, or None: it must be a processor
    the checks of an AI translation on that side accept as it is (a guard a2m would write differently is refused,
    so what is checked is what runs)."""
    name = _local(element)
    if element.tag not in PROCESSORS:
        return (
            f"adds or changes a <{name}> element; an AI fix may only add or change Mule core processors and "
            "Transform Message, and must leave everything else a2m generated as it is"
        )
    fragment = copy.deepcopy(element)
    fragment.tail = None
    try:
        processors = parse_mule(ET.tostring(fragment, encoding="unicode"), side)
    except ValueError as exc:  # translate's refusal of an answer (and defusedxml's) are ValueErrors
        return f"adds or changes a <{name}> element a2m cannot use on the {side} side: {exc}"
    except Exception as exc:  # noqa: BLE001 (AI output never crashes a2m: any other failure refuses the fix)
        return f"adds or changes a <{name}> element a2m could not check ({type(exc).__name__})"
    builtins = checks.builtin_reads(processors)
    if builtins:
        return (
            f"reads the Apigee built-in variable {', '.join(builtins)} as a flow variable; the generated app never "
            "sets it, so it would always be null"
        )
    if len(processors) != 1 or _digest_of(_without_doc(fragment)) != _digest_of(processors[0]):
        return f"has a guard in a <{name}> element that a2m writes differently; write each guard as a simple test"
    return None


def _without_doc(element: ET.Element) -> ET.Element:
    """A copy of ``element`` without ``doc:`` attributes (a2m labels steps itself; they do not change behaviour)."""
    clean = copy.deepcopy(element)
    for node in clean.iter():
        for key in [k for k in node.attrib if k.startswith(f"{{{DOC}}}")]:
            del node.attrib[key]
    return clean


def _local(element: ET.Element) -> str:
    return element.tag.partition("}")[2] if element.tag.startswith("{") else element.tag


def _shallow(element: ET.Element) -> tuple[str, tuple[tuple[str, str], ...], str]:
    """An element without its children: tag, attributes and own text (outer blanks aside)."""
    return element.tag, tuple(sorted(element.attrib.items())), (element.text or "").strip()


def _digests(root: ET.Element) -> dict[int, str]:
    """A digest of each element of ``root`` with everything in it, by ``id``; computed without recursion."""
    found: dict[int, str] = {}
    stack: list[tuple[ET.Element, bool]] = [(root, False)]
    while stack:
        element, ready = stack.pop()
        if not ready:
            stack.append((element, True))
            stack.extend((child, False) for child in element)
            continue
        found[id(element)] = _digest(element, [found[id(child)] for child in element])
    return found


def _digest(element: ET.Element, children: Sequence[str]) -> str:
    tag, attributes, text = _shallow(element)
    data = json.dumps([tag, attributes, text, (element.tail or "").strip(), list(children)], ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _digest_of(element: ET.Element) -> str:
    probe = copy.deepcopy(element)
    probe.tail = None
    return _digests(probe)[id(probe)]


def mask_attempts(attempts: Sequence[FixAttempt], masker: Masker) -> tuple[FixAttempt, ...]:
    """``attempts`` with every text masked by ``masker`` (what verification.json and run.log show)."""
    return tuple(attempt.masked(masker) for attempt in attempts)


__all__ = [
    "DEFAULT_MAX_FIX_ATTEMPTS",
    "FixAttempt",
    "FixLoopResult",
    "mask_attempts",
    "run_with_fixes",
]
