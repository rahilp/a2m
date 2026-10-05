"""Ask the AI to translate one item, and validate its answer strictly before any of it is used.

:class:`Translator` builds the prompt for a callout (:meth:`Translator.callout`)
or a condition (:meth:`Translator.expression`) from the prompt files, sends it
through the provider and checks the answer. The answer must be one JSON object
(optionally inside a single ```json fence):

* ``{"status": "translated", "confidence": "high"|"medium"|"low", "notes": str,
  "mule": str, "writes": {...}}`` for a callout (``writes`` optional, see
  below), ``"condition"`` (a structured condition, see :mod:`a2m.ai.checks`)
  instead of ``"mule"`` (and no ``writes``) for a condition; the earlier
  ``"dataweave"`` string is accepted in its place only in the small subset
  :func:`a2m.ai.checks.condition_from_dataweave` reads;
* ``{"status": "cannot_translate", "reason": str}`` (``notes`` allowed).

Anything else is an answer that could not be used: needs review, never
success. A callout's Mule code must be a well-formed XML fragment of Mule 4
core processors, parsed with the stdlib-compatible defusedxml parser and
returned as elements, so the generator writes it through ElementTree and none
of it can escape its place in the document. It may not hold flows, flow
references, global configurations or ``${...}`` property placeholders;
``doc:`` attributes are dropped (the generator labels the step). Every element,
where it sits, its children and its attributes must be as the one allowlist
:data:`STRUCTURE` allows (Mule 4.9's core schema, narrowed: ``when`` and
``otherwise`` only in a ``choice`` that has a ``when``, ``error-handler`` only
last in a ``try``, required attributes present; a ``value`` may be empty text,
as Mule allows, other required attributes may not be blank). Every expression
that decides which processors run (a ``when`` guard in a ``choice``, the
``when`` of an error handler) goes through the same checks as an AI condition
(:func:`a2m.ai.checks.guard_condition`): it must parse into the small
DataWeave subset a2m reads, every value it reads must be one a2m maps, it may
not be a constant (``#[true]``, ``#[vars.x == 'a' or true]`` and the like are
refused), and a2m writes the checked guard itself in place of the AI's.

Of the ``ee:`` namespace only Transform Message is accepted: an
``ee:transform`` at a processor's place, holding at most one ``ee:message``
(at most one ``ee:set-payload`` and one ``ee:set-attributes``) and at most one
``ee:variables`` (``ee:set-variable`` with a ``variableName``), every script
written inline and non-empty (a ``resource`` file is not shipped with the
generated app), and nothing else. Any other ``ee:`` element makes the answer
unusable. Transform Message is a Mule Enterprise component: the generator
records that such an app needs a Mule Enterprise runtime.

A callout's Mule code may not read an Apigee built-in variable as a flow
variable (``vars['verifyapikey.VA-Key.apiproduct.name']``): the generated app
never sets those, so the value would always be null.

A callout's ``writes`` declares what the ORIGINAL code writes
(``request_headers``, ``query_params``, ``verb``, ``payload``,
``response_headers``, ``variables``). It is used as the step's write model
only when the confidence is not low and the Mule code writes exactly that
(:func:`a2m.ai.checks.declared_writes`); otherwise the step may change
anything, as for a step a2m does not translate.

A condition is a tree a2m checks and writes as DataWeave itself: every value
it reads must be one a2m's own condition translator reads faithfully at that
point, and it may not be a constant (see :mod:`a2m.ai.checks`).

The AI never sees a literal value of the proxy. Each item gets a table of
placeholders of its own (:class:`a2m.ai.placeholders.Placeholders`, default
deny, as for the fix loop): every string literal, comment and regular
expression of the custom code (and of its included scripts), every value of
the policy XML (only names at the schema positions of names stay) and every
value of a condition is shown as a placeholder such as ``«v1»`` (a number
literal of the code or the condition as a number placeholder such as
``«n2»``, so the AI sees it is a number); the code's
structure, the condition's variables and operators, and the step, flow and
proxy names stay visible. a2m's refusal reason is swept with the same table.
A condition that compares with a number (one shown as a number placeholder)
is never translated, as a2m's own translator never translates one: every
answer would be refused, whatever it did with the number, so it is not sent
to the AI at all.
In the answer, each placeholder is written back before any check runs: in the
Mule code, spelled and escaped for where it stands (an attribute, element
text, a DataWeave string literal); in a condition tree's values and declared
writes, as the exact value; in a legacy DataWeave condition, spelled for its
string literal. An unknown placeholder, or one that cannot stand where it was
written, makes the answer unusable. The AI's notes and reasons are kept as it
wrote them, placeholders and all.

A provider error (any exception from ``complete``) affects only that item.
Identical prompts are asked once per translator.
"""

from __future__ import annotations

import copy
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from defusedxml import ElementTree as SafeET

from a2m.ai import checks
from a2m.ai.checks import DeclaredWrites
from a2m.ai.placeholders import PlaceholderError, Placeholders
from a2m.ai.prompts import load_prompts, render
from a2m.ai.provider import AiRequest, Confidence, ItemKind, Provider
from a2m.ai.sources import CalloutSource
from a2m.conditions.variables import NO_CHANGES, RequestChanges
from a2m.redaction import redact

CORE = "http://www.mulesoft.org/schema/mule/core"
EE = "http://www.mulesoft.org/schema/mule/ee/core"
DOC = "http://www.mulesoft.org/schema/mule/documentation"
FRAGMENT_ROOT = "a2m-fragment"
# The namespaces an answer may be written in; of ee: only Transform Message is accepted (see _check_transform).
ALLOWED_NAMESPACES = (CORE, EE)
EE_TRANSFORM = f"{{{EE}}}transform"
EE_MESSAGE = f"{{{EE}}}message"
EE_VARIABLES = f"{{{EE}}}variables"
EE_SET_PAYLOAD = f"{{{EE}}}set-payload"
EE_SET_ATTRIBUTES = f"{{{EE}}}set-attributes"
EE_SET_VARIABLE = f"{{{EE}}}set-variable"
# The parts of an ee:transform and the scripts each part may hold.
EE_SCRIPTS = {
    EE_MESSAGE: frozenset({EE_SET_PAYLOAD, EE_SET_ATTRIBUTES}),
    EE_VARIABLES: frozenset({EE_SET_VARIABLE}),
}
# The elements whose text is their content (a DataWeave script). Text anywhere else in a callout's Mule code, an
# element's own text or the text after an element, is refused (:func:`_has_stray_text`).
TEXT_BEARING = frozenset({EE_SET_PAYLOAD, EE_SET_ATTRIBUTES, EE_SET_VARIABLE})
# Elements that are not processors at a step's place, or that refer to things the generated app does not have.
REFUSED_ELEMENTS = frozenset(
    {"mule", "flow", "sub-flow", "flow-ref", "configuration", "configuration-properties", "global-property", "import"}
)


def _core(*names: str) -> frozenset[str]:
    return frozenset(f"{{{CORE}}}{name}" for name in names)


@dataclass(frozen=True, slots=True)
class _Shape:
    """One element a callout's Mule code may hold, as Mule 4.9's core schema (mule-core-common.xsd) defines it.

    ``content`` is its children: a sequence of groups ``(allowed tags, at least, at most)`` (None for at most: any
    number), matched in order; None means the element's parts are checked by :func:`_check_transform`.
    ``attributes`` are the attributes it may have, ``required`` those it must have (not blank, except those in
    :data:`MAY_BE_EMPTY`), ``choices`` the values an attribute may take, ``guards`` its attributes that decide
    control flow (checked by :func:`_guard`). Text inside it is refused."""

    content: tuple[tuple[frozenset[str], int, int | None], ...] | None
    attributes: frozenset[str] = frozenset()
    required: frozenset[str] = frozenset()
    choices: Mapping[str, frozenset[str]] | None = None
    guards: frozenset[str] = frozenset()


# The processors a callout's Mule code may hold at a step's place or inside a scope or route.
PROCESSORS = _core("set-variable", "remove-variable", "set-payload", "logger", "raise-error", "choice", "try") | {
    EE_TRANSFORM
} | _core("foreach", "until-successful")
WHEN = f"{{{CORE}}}when"
CHOICE = f"{{{CORE}}}choice"
ON_ERROR = _core("on-error-continue", "on-error-propagate")
ROOT = f"{{{CORE}}}{FRAGMENT_ROOT}"
SOME_PROCESSORS = ((PROCESSORS, 1, None),)
ON_ERROR_ATTRIBUTES = frozenset({"type", "when", "enableNotifications", "logException"})
GUARD = frozenset({"expression"})
ON_ERROR_GUARD = frozenset({"when"})
# Required attributes Mule accepts as empty text: a literal value ("" clears a variable or the payload).
MAY_BE_EMPTY = frozenset({"value"})
# The one allowlist of what a callout's Mule code may hold: every element, its children (so where it may sit) and
# its attributes. Anything not in it, or not where it allows, makes the answer unusable (:func:`_check_structure`).
STRUCTURE: dict[str, _Shape] = {
    ROOT: _Shape(SOME_PROCESSORS),
    f"{{{CORE}}}set-variable": _Shape(
        (), frozenset({"variableName", "value", "mimeType", "encoding"}), frozenset({"variableName", "value"})
    ),
    f"{{{CORE}}}remove-variable": _Shape((), frozenset({"variableName"}), frozenset({"variableName"})),
    f"{{{CORE}}}set-payload": _Shape((), frozenset({"value", "mimeType", "encoding"}), frozenset({"value"})),
    f"{{{CORE}}}logger": _Shape(
        (),
        frozenset({"message", "level", "category"}),
        choices={"level": frozenset({"ERROR", "WARN", "INFO", "DEBUG", "TRACE"})},
    ),
    f"{{{CORE}}}raise-error": _Shape((), frozenset({"type", "description"}), frozenset({"type"})),
    CHOICE: _Shape(((frozenset({WHEN}), 1, None), (_core("otherwise"), 0, 1))),
    WHEN: _Shape(SOME_PROCESSORS, GUARD, GUARD, guards=GUARD),
    f"{{{CORE}}}otherwise": _Shape(SOME_PROCESSORS),
    f"{{{CORE}}}try": _Shape(((PROCESSORS, 1, None), (_core("error-handler"), 0, 1))),
    f"{{{CORE}}}error-handler": _Shape(((ON_ERROR, 0, None),)),
    f"{{{CORE}}}on-error-continue": _Shape(((PROCESSORS, 0, None),), ON_ERROR_ATTRIBUTES, guards=ON_ERROR_GUARD),
    f"{{{CORE}}}on-error-propagate": _Shape(((PROCESSORS, 0, None),), ON_ERROR_ATTRIBUTES, guards=ON_ERROR_GUARD),
    f"{{{CORE}}}foreach": _Shape(
        SOME_PROCESSORS, frozenset({"collection", "batchSize", "rootMessageVariableName", "counterVariableName"})
    ),
    f"{{{CORE}}}until-successful": _Shape(SOME_PROCESSORS, frozenset({"maxRetries", "millisBetweenRetries"})),
    EE_TRANSFORM: _Shape(None),
}
CALLOUT_FIELD = "mule"
WRITES_FIELD = "writes"
EXPRESSION_FIELD = "condition"
# The earlier form of a condition answer: a DataWeave string, accepted only in the subset a2m can read back.
LEGACY_EXPRESSION_FIELD = "dataweave"
TRANSLATED = "translated"
DECLINED = "cannot_translate"
FENCE = re.compile(r"```(?:json)?[ \t]*\n(.*?)\n?```", re.DOTALL)
NOT_USABLE = "the AI answer could not be used"
MAX_REASON_CHARS = 500
NONE = "none"
# Shown in place of a policy configuration that cannot be read as XML (then none of it may be shown).
POLICY_NOT_SHOWN = "(not shown: a2m could not read it as XML)"


@dataclass(frozen=True, slots=True)
class Place:
    """Where an item sits: ``location`` as the generator names it (endpoint, flow, side), ``side`` request or
    response, and the step names right before and after it (``none`` at an end)."""

    proxy: str
    location: str
    side: str
    before: str = NONE
    after: str = NONE


@dataclass(frozen=True, slots=True)
class CalloutTranslated:
    """A usable callout translation. ``writes`` is its checked write model (None: it may change anything, and
    ``writes_note`` says why)."""

    confidence: Confidence
    notes: str
    processors: tuple[ET.Element, ...]
    writes: DeclaredWrites | None = None
    writes_note: str = ""


@dataclass(frozen=True, slots=True)
class ExpressionTranslated:
    confidence: Confidence
    notes: str
    dataweave: str


@dataclass(frozen=True, slots=True)
class NotTranslated:
    """The AI declined, its answer could not be used, or the provider failed: ``reason`` says which and why.

    ``confidence`` is LOW for an answer that could not be used, None when the
    AI declined or gave no answer. ``proposal`` is a usable condition the AI was
    not confident in (low confidence), shown to the reviewer but not used.
    ``sent`` is False when a2m refused the item without asking the AI.
    """

    reason: str
    notes: str = ""
    confidence: Confidence | None = None
    proposal: str | None = None
    sent: bool = True


class _Unusable(ValueError):
    """An answer that fails validation; the message is why."""


@dataclass(frozen=True, slots=True)
class _Failed:
    reason: str


class Translator:
    """Sends items to ``provider`` and validates the answers (see the module docstring)."""

    def __init__(self, provider: Provider, prompts: Mapping[ItemKind, str] | None = None) -> None:
        self.provider = provider
        self._prompts = dict(prompts) if prompts is not None else None
        self._answers: dict[str, str | _Failed] = {}

    def _prompt(self, kind: ItemKind, values: Mapping[str, str]) -> str:
        if self._prompts is None:
            self._prompts = load_prompts()
        return render(self._prompts[kind], values)

    def _ask(self, kind: ItemKind, name: str, original: str, values: Mapping[str, str]) -> str | _Failed:
        prompt = self._prompt(kind, values)
        known = self._answers.get(prompt)
        if known is not None:
            return known
        answer: str | _Failed
        try:
            raw = self.provider.complete(AiRequest(kind, name, original, prompt))
        except Exception as exc:  # noqa: BLE001 (the provider boundary: any failure is that item's provider error)
            answer = _Failed(_short(f"the AI provider failed: {type(exc).__name__}: {_text_of(exc)}"))
        else:
            answer = raw if isinstance(raw, str) else _Failed("the AI provider returned no text")
        self._answers[prompt] = answer
        return answer

    def callout(
        self, source: CalloutSource, step: str, policy_type: str, policy_xml: str, place: Place, changed: str = NONE
    ) -> CalloutTranslated | NotTranslated:
        """Translate the custom code of step ``step`` into Mule processors. ``changed`` lists the values earlier steps
        may have changed in Apigee in a way the generated app may not carry over."""
        # The code first, so its placeholders are numbered as a table showing the code alone numbers them.
        table = Placeholders()
        code = table.code(source.original, source.kind)
        includes = "\n\n".join(f"{file}:\n{table.code(text, source.kind)}" for file, text in source.includes) or NONE
        table.learn_apigee(policy_xml)
        shown_policy = table.apigee(policy_xml.strip())
        values = {
            **_place_values(place),
            "step": step,
            "policy_type": policy_type,
            "policy_xml": shown_policy if shown_policy is not None else POLICY_NOT_SHOWN,
            "resource": source.file,
            "original": code,
            "includes": includes,
            "changed": changed,
        }
        answer = self._ask(source.kind, step, code, values)
        if isinstance(answer, _Failed):
            return NotTranslated(answer.reason)
        try:
            data = _answer_object(answer, CALLOUT_FIELD)
            if data["status"] == DECLINED:
                return _declined(data)
            confidence, notes = _confidence(data), _notes(data)
            mule = _restore(lambda: table.restore(step, _code(data, CALLOUT_FIELD)), "its Mule code")
            processors = parse_mule(mule, place.side)
            builtins = checks.builtin_reads(processors)
            if builtins:
                raise _Unusable(
                    f"its Mule code reads the Apigee built-in variable {', '.join(builtins)} as a flow variable; the "
                    "generated app never sets it, so it would always be null"
                )
        except _Unusable as exc:
            return _unusable(str(exc))
        if confidence is Confidence.LOW:
            return CalloutTranslated(confidence, notes, processors, None, "the AI's confidence is low")
        try:
            writes = _restore(lambda: _restore_values(table, data.get(WRITES_FIELD)), "its writes")
        except _Unusable as exc:
            return _unusable(str(exc))
        model = checks.declared_writes(writes, processors)
        if isinstance(model, str):
            return CalloutTranslated(confidence, notes, processors, None, model)
        return CalloutTranslated(confidence, notes, processors, model)

    def expression(
        self,
        owner: str,
        name: str,
        original: str,
        place: Place,
        refusal: str,
        changes: RequestChanges = NO_CHANGES,
    ) -> ExpressionTranslated | NotTranslated:
        """Translate the condition ``original`` of ``owner`` (e.g. ``Flow curl-clients``; ``name`` is its name),
        which a2m's own translator refused for ``refusal``, into a DataWeave expression read after ``changes`` (what
        earlier steps on the path may have changed)."""
        table = Placeholders()
        shown = table.condition(original)
        numbers = table.numbers_in(shown)
        if numbers:
            # CP5's rule: a condition that compares with a number is not translated (an answer that drops the number,
            # or compares it as text, would not be the same condition). Every answer would be refused, so the AI is
            # not asked.
            return NotTranslated(
                f"the condition compares with a number ({', '.join(numbers)}), and comparing with a number is not "
                "translated, since Apigee converts between numbers and text by its own rules; it was not sent to the "
                "AI",
                sent=False,
            )
        values = {**_place_values(place), "owner": owner, "original": shown, "refusal": table.sweep(refusal)}
        answer = self._ask(ItemKind.EXPRESSION, name, shown, values)
        if isinstance(answer, _Failed):
            return NotTranslated(answer.reason)
        try:
            data = _answer_object(answer, EXPRESSION_FIELD)
            if data["status"] == DECLINED:
                return _declined(data)
            confidence, notes = _confidence(data), _notes(data)
            restored = dict(data)
            if EXPRESSION_FIELD in data:
                restored[EXPRESSION_FIELD] = _restore(
                    lambda: _restore_values(table, data[EXPRESSION_FIELD]), "its condition"
                )
            if isinstance(data.get(LEGACY_EXPRESSION_FIELD), str):
                restored[LEGACY_EXPRESSION_FIELD] = _restore(
                    lambda: table.restore_code(data[LEGACY_EXPRESSION_FIELD]), "its DataWeave"
                )
            dataweave = _condition(restored, place.side, changes)
        except _Unusable as exc:
            return _unusable(str(exc))
        if confidence is Confidence.LOW:
            return NotTranslated(
                "the AI's confidence in its translation is low, so it is not used (the condition stays false)",
                notes,
                Confidence.LOW,
                proposal=dataweave,
            )
        return ExpressionTranslated(confidence, notes, dataweave)


def _restore(put_back: Callable[[], Any], what: str) -> Any:
    """What ``put_back`` returns (part of the answer with its placeholders written back); :class:`_Unusable`, naming
    ``what``, when a placeholder is unknown or cannot stand where the AI wrote it."""
    try:
        return put_back()
    except PlaceholderError as exc:
        raise _Unusable(f"{what} {exc}") from None
    except RecursionError:
        raise _Unusable(f"{what} is nested too deeply to put its placeholders back") from None


def _restore_values(table: Placeholders, value: Any) -> Any:
    """``value`` (JSON data of the answer: a condition tree, declared writes) with every placeholder in its strings
    replaced by the exact value it stands for; keys are left as they are."""
    if isinstance(value, str):
        return table.restore_plain(value)
    if isinstance(value, list):
        return [_restore_values(table, item) for item in value]
    if isinstance(value, dict):
        return {key: _restore_values(table, item) for key, item in value.items()}
    return value


def _text_of(exc: BaseException) -> str:
    try:
        return str(exc)
    except Exception:  # noqa: BLE001 (an exception whose text cannot be built still names its type)
        return "(no message)"


def _short(text: str) -> str:
    # Secrets are masked before the text is cut short, so no part of one survives the cut.
    text = redact(" ".join(text.split()))
    return text if len(text) <= MAX_REASON_CHARS else text[:MAX_REASON_CHARS] + "..."


def _place_values(place: Place) -> dict[str, str]:
    return {
        "proxy": place.proxy,
        "location": place.location,
        "side": place.side,
        "before": place.before,
        "after": place.after,
    }


def _declined(data: dict[str, Any]) -> NotTranslated:
    return NotTranslated(f"the AI could not translate it: {_short(data['reason'])}", data.get("notes", ""))


def _unusable(detail: str) -> NotTranslated:
    note = f"{NOT_USABLE}: {_short(detail)}"
    return NotTranslated(f"{note}; review it by hand", note, Confidence.LOW)


def _answer_object(text: str, field: str) -> dict[str, Any]:
    """The answer as a dict with a valid status and exactly the keys that status allows."""
    body = text.strip()
    fenced = FENCE.fullmatch(body)
    if fenced is not None:
        body = fenced.group(1).strip()
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):  # ValueError: JSONDecodeError, or a number too long to convert
        raise _Unusable("it is not one JSON object") from None
    if not isinstance(data, dict):
        raise _Unusable("it is not one JSON object")
    status = data.get("status")
    if status == DECLINED:
        allowed = {"status", "reason", "notes"}
        reason = data.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise _Unusable("it declines without a reason")
        if "notes" in data and not isinstance(data["notes"], str):
            raise _Unusable("its notes are not text")
    elif status == TRANSLATED:
        allowed = {"status", "confidence", "notes", field}
        if field == CALLOUT_FIELD:
            allowed.add(WRITES_FIELD)
        elif field == EXPRESSION_FIELD:
            allowed.add(LEGACY_EXPRESSION_FIELD)
    else:
        raise _Unusable(f"its status {status!r} is not {TRANSLATED!r} or {DECLINED!r}")
    extra = sorted(str(key) for key in set(data) - allowed)
    if extra:
        raise _Unusable(f"it has fields a2m does not expect: {', '.join(extra)}")
    return data


def _confidence(data: dict[str, Any]) -> Confidence:
    if "confidence" not in data:
        raise _Unusable("it has no confidence (high, medium or low)")
    value = data["confidence"]
    if not isinstance(value, str) or value not in {c.value for c in Confidence}:
        raise _Unusable(f"its confidence {value!r} is not high, medium or low")
    return Confidence(value)


def _notes(data: dict[str, Any]) -> str:
    notes = data.get("notes")
    if not isinstance(notes, str):
        raise _Unusable("it has no notes")
    return notes.strip()


def _code(data: dict[str, Any], field: str) -> str:
    if field not in data:
        raise _Unusable(f"it has no {field!r} code")
    code = data[field]
    if not isinstance(code, str):
        raise _Unusable(f"its {field!r} code is not text")
    if not code.strip():
        raise _Unusable(f"its {field!r} code is empty")
    return code


def parse_mule(text: str, side: str = "request") -> tuple[ET.Element, ...]:
    """The processors in ``text`` (a Mule 4 XML fragment) of a step on ``side``, checked as in the module docstring;
    every guard is replaced by the one a2m checked and wrote."""
    if "${" in text:
        raise _Unusable("its Mule code uses a ${...} property placeholder the generated app does not define")
    wrapped = f'<{FRAGMENT_ROOT} xmlns="{CORE}" xmlns:ee="{EE}" xmlns:doc="{DOC}">{text}</{FRAGMENT_ROOT}>'
    try:
        root = SafeET.fromstring(wrapped)
    except (ET.ParseError, ValueError) as exc:  # defusedxml raises ValueError subclasses for DTDs and entities
        raise _Unusable(f"its Mule code is not well-formed XML ({exc})") from None
    if _has_stray_text(root):
        raise _Unusable("its Mule code has text outside any element")
    if len(root) == 0:
        raise _Unusable("its Mule code holds no Mule processor")
    for element in root.iter():
        if element is root:
            continue
        namespace, _, local = element.tag[1:].partition("}") if element.tag.startswith("{") else ("", "", element.tag)
        if namespace not in ALLOWED_NAMESPACES:
            raise _Unusable(f"its Mule code uses the element {element.tag}, outside Mule core and ee:")
        if local in REFUSED_ELEMENTS or local.endswith("-config"):
            raise _Unusable(f"its Mule code holds a <{local}> element, which cannot sit at a step's place")
        for key in list(element.attrib):
            if key.startswith(f"{{{DOC}}}"):
                del element.attrib[key]
            elif key.startswith("{"):
                raise _Unusable(f"its Mule code uses the attribute {key}, outside Mule core")
    for parent in root.iter():
        for child in parent:
            if child.tag == EE_TRANSFORM:
                if parent.tag.startswith(f"{{{EE}}}"):
                    raise _Unusable("its Mule code holds an <ee:transform> inside another ee: element")
                _check_transform(child)
            elif child.tag.startswith(f"{{{EE}}}") and not parent.tag.startswith(f"{{{EE}}}"):
                raise _Unusable(
                    f"its Mule code uses <ee:{_local_name(child)}> outside an <ee:transform>; of the ee: "
                    "elements a2m accepts only Transform Message"
                )
    if any(child.tag == f"{{{CORE}}}error-handler" for child in root):
        raise _Unusable("its Mule code starts with an element that is not a processor")
    _check_structure(root, side)
    processors = []
    for child in root:
        copied = copy.deepcopy(child)
        copied.tail = None
        processors.append(copied)
    return tuple(processors)


def _check_transform(transform: ET.Element) -> None:
    """Check one ``ee:transform`` (Transform Message) as in the module docstring; :class:`_Unusable` when not."""
    what = "an <ee:transform> a2m cannot use"
    if transform.attrib:
        raise _Unusable(f"its Mule code holds {what} (it has attributes)")
    for node in transform.iter():
        if _has_stray_text(node):
            raise _Unusable(f"its Mule code holds {what} (it has text in <{_local_name(node)}> outside its scripts)")
    seen: set[str] = set()
    scripts = 0
    for section in transform:
        name = _local_name(section)
        if section.tag not in EE_SCRIPTS or section.attrib:
            raise _Unusable(f"its Mule code holds {what} (it has a {name} part)")
        if section.tag in seen:
            raise _Unusable(f"its Mule code holds {what} (it has two {name} parts)")
        seen.add(section.tag)
        allowed_scripts = EE_SCRIPTS[section.tag]
        script_names: set[str] = set()
        for script in section:
            script_name = _local_name(script)
            if script.tag not in allowed_scripts:
                raise _Unusable(f"its Mule code holds {what} (a {script_name} part inside {name})")
            if "resource" in script.attrib:
                raise _Unusable(
                    f"its Mule code holds {what} (its {script_name} reads its script from a file, "
                    "which the generated app does not have)"
                )
            wanted = {"variableName"} if script.tag == EE_SET_VARIABLE else set()
            if set(script.attrib) != wanted or len(script):
                raise _Unusable(f"its Mule code holds {what} (its {script_name} part)")
            if script.tag != EE_SET_VARIABLE:
                if script.tag in script_names:
                    raise _Unusable(f"its Mule code holds {what} (it has two {script_name} parts)")
                script_names.add(script.tag)
            elif not script.attrib["variableName"].strip():
                raise _Unusable(f"its Mule code holds {what} (a set-variable with no variable name)")
            if not (script.text or "").strip():
                raise _Unusable(f"its Mule code holds {what} (an empty script)")
            scripts += 1
    if not scripts:
        raise _Unusable(f"its Mule code holds {what} (it has no script)")


def _check_structure(root: ET.Element, side: str) -> None:
    """Check the fragment ``root`` against :data:`STRUCTURE`: every element is in it, with children in the order and
    numbers its parent allows, only the attributes it allows (the required ones present, and not blank unless in
    :data:`MAY_BE_EMPTY`) and no text; every guard is checked and rewritten by :func:`_guard`. :class:`_Unusable`
    when not."""
    stack = [root]
    while stack:
        element = stack.pop()
        shape = STRUCTURE[element.tag]
        name = _local_name(element)
        if element is not root:
            for key, value in element.attrib.items():
                if key not in shape.attributes:
                    raise _Unusable(f"its Mule code gives <{name}> the attribute {key}, which Mule does not accept there")
                allowed = (shape.choices or {}).get(key)
                if allowed is not None and value not in allowed:
                    raise _Unusable(f"its Mule code gives <{name}> the {key} {value!r}, which Mule does not accept")
            for key in sorted(shape.required):
                if key not in element.attrib or (key not in MAY_BE_EMPTY and not element.attrib[key].strip()):
                    raise _Unusable(f"its Mule code has a <{name}> without the {key} Mule requires")
            for key in sorted(shape.guards & set(element.attrib)):
                element.attrib[key] = _guard(element.attrib[key], name, side)
        if element is not root and _has_stray_text(element):
            raise _Unusable(f"its Mule code has text inside <{name}>")
        if shape.content is None:
            continue
        children = list(element)
        where = "at a step's place" if element is root else f"inside <{name}>"
        index = 0
        for allowed_tags, least, most in shape.content:
            count = 0
            while index < len(children) and children[index].tag in allowed_tags and (most is None or count < most):
                count += 1
                index += 1
            if count < least and index < len(children):
                break
            if count < least:
                wanted = "processor" if allowed_tags is PROCESSORS else " or ".join(
                    sorted(f"<{_local_name_of(tag)}>" for tag in allowed_tags)
                )
                raise _Unusable(f"its Mule code has no {wanted} {where}, where Mule requires one")
        if index < len(children):
            raise _Unusable(
                f"its Mule code holds <{_local_name(children[index])}> {where}, where Mule does not accept it"
            )
        stack.extend(children)


def _has_stray_text(element: ET.Element) -> bool:
    """Whether ``element`` holds non-whitespace text outside a script: its own text (unless it is one of
    :data:`TEXT_BEARING`) or the text after any of its children. The one text check for every element of a callout's
    Mule code, used by :func:`parse_mule`, :func:`_check_transform` (on every part of a transform) and
    :func:`_check_structure` (on every other element)."""
    if element.tag not in TEXT_BEARING and (element.text or "").strip():
        return True
    return any((child.tail or "").strip() for child in element)


def _guard(expression: str, name: str, side: str) -> str:
    """The guard a2m writes for ``expression`` (an attribute of ``<name>`` that decides which processors run, on
    ``side``): a ``#[...]`` expression put through :func:`a2m.ai.checks.guard_condition`, the pipeline AI conditions
    go through, and written by a2m. :class:`_Unusable` when it is not a ``#[...]`` expression, does not parse into
    the subset a2m reads, reads a value a2m cannot vouch for, or is a constant."""
    text = expression.strip()
    if not (text.startswith("#[") and text.endswith("]")):
        raise _Unusable(f"its Mule code has a guard {expression!r} on <{name}> that is not a #[...] expression")
    try:
        return f"#[{checks.guard_condition(text[2:-1].strip(), side)}]"
    except checks.CheckError as exc:
        raise _Unusable(f"its Mule code has a guard {expression!r} on <{name}> a2m cannot use: {exc}") from None
    except Exception as exc:  # noqa: BLE001 (AI input never crashes a2m: any other failure refuses the answer)
        raise _Unusable(f"a2m could not check the guard on <{name}> ({type(exc).__name__})") from None


def _local_name_of(tag: str) -> str:
    return tag.partition("}")[2] if tag.startswith("{") else tag


def _local_name(element: ET.Element) -> str:
    return element.tag.partition("}")[2] if element.tag.startswith("{") else element.tag


def _condition(data: dict[str, Any], side: str, changes: RequestChanges) -> str:
    """The DataWeave a2m writes for the condition answer ``data``, read on ``side`` after ``changes``."""
    structured, legacy = EXPRESSION_FIELD in data, LEGACY_EXPRESSION_FIELD in data
    if structured == legacy:
        raise _Unusable(
            f"it has {'both' if structured else 'neither'} a {EXPRESSION_FIELD!r} and a {LEGACY_EXPRESSION_FIELD!r}"
        )
    try:
        if structured:
            return checks.structured_condition(data[EXPRESSION_FIELD], side, changes)
        text = _code(data, LEGACY_EXPRESSION_FIELD).strip()
        if text.startswith("#[") and text.endswith("]"):
            text = text[2:-1].strip()
        if not text:
            raise _Unusable("its DataWeave is empty")
        return checks.dataweave_condition(text, side, changes)
    except checks.CheckError as exc:
        raise _Unusable(str(exc)) from None
    except Exception as exc:  # noqa: BLE001 (AI input never crashes a2m: any other failure refuses the answer)
        raise _Unusable(f"a2m could not check its condition ({type(exc).__name__})") from None
