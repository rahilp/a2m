"""What every policy template shares: the result types, reading the policy XML and building Mule XML.

A template is a function ``translate(policy, *, direction, changes) -> TemplateOutput``.
``direction`` is "request" or "response": the side of the flow its step sits
in. ``changes`` are the request headers, query parameters and verb earlier
steps on the same path may have changed: a message template or a variable
reference (``ref``, ``Ref``, ``Source``; see :func:`read_variable`) reading one
of them can't be translated (see :mod:`a2m.conditions.variables`). The output
holds the Mule processors placed at the step's position (all
tags in ElementTree's ``{namespace-uri}local`` form), the top-level elements
they need, the entries they add to the app's properties file, and the result
record that later becomes the step's report row.

Conventions the generated processors follow, so the generator can wire them
up (see :data:`NEED_FAULT` and friends):

* a policy that rejects a call sets ``vars.httpStatus``, ``vars.responseHeaders``
  and the payload to an Apigee-style JSON fault, then raises
  :data:`FAULT_ERROR_TYPE`; the generator adds an ``on-error-continue`` for that
  type to every listening flow, so the listener answers with those values;
* request header and query parameter changes go to ``vars.a2mRequestHeaders``
  and ``vars.a2mRequestQuery``, which the target request then sends instead of
  the caller's own; response header changes go to ``vars.responseHeaders``.

Templates never guess: a setting they cannot carry over is listed as an
unsupported option, and a policy they cannot translate at all comes back with
method ``skipped``, a reason, and no processors.
"""

from __future__ import annotations

import json
import re
import string
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from a2m.conditions import NO_CHANGES, RequestChanges, Translation, translate_template
from a2m.conditions.lexer import ConditionError
from a2m.conditions.template import has_reference
from a2m.conditions.variables import (  # noqa: F401 (re-exported)
    RESPONSE_HEADERS_BASE,
    RESPONSE_HEADERS_VAR,
    SNAPSHOT_READ,
    accessor,
    is_custom_variable,
    stale_part,
)
from a2m.ir import Policy, XmlElement

CORE = "http://www.mulesoft.org/schema/mule/core"
OS = "http://www.mulesoft.org/schema/mule/os"
DOC = "http://www.mulesoft.org/schema/mule/documentation"

REQUEST = "request"
RESPONSE = "response"
DIRECTIONS = (REQUEST, RESPONSE)

# The error a rejecting policy raises once it has set the fault response.
FAULT_ERROR_TYPE = "A2M:POLICY_FAULT"
# The error an atomic object store insert (failIfPresent) raises when the key is already there.
OS_KEY_PRESENT = "OS:KEY_ALREADY_EXISTS"
OS_KEY_MISSING = "OS:KEY_NOT_FOUND"
# What a template's processors need from the rest of the generated flow.
NEED_FAULT = "fault"
NEED_REASON_PHRASE = "reason-phrase"
NEED_REQUEST_HEADERS = "request-headers"
NEED_REQUEST_QUERY = "request-query"
# A value reads the request on the response side: the generator saves the request snapshot before the target call.
NEED_REQUEST_SNAPSHOT = "request-snapshot"

REQUEST_HEADERS_VAR = "a2mRequestHeaders"
REQUEST_QUERY_VAR = "a2mRequestQuery"
STATUS_VAR = "httpStatus"
REASON_PHRASE_VAR = "a2mReasonPhrase"

# The headers and query parameters of the request as it will be sent: changed by earlier steps, else the caller's.
REQUEST_HEADERS_BASE = f"(vars.{REQUEST_HEADERS_VAR} default attributes.headers)"
REQUEST_QUERY_BASE = f"(vars.{REQUEST_QUERY_VAR} default attributes.queryParams)"
NOW_MILLIS = "(now() as Number {unit: 'milliseconds'})"

# Root attributes every policy may carry; continueOnError and enabled are handled separately.
COMMON_ATTRIBUTES = frozenset({"name", "async", "continueOnError", "enabled"})
UNRESOLVED_VARIABLES = "IgnoreUnresolvedVariables"
COMMON_CHILDREN = frozenset({"DisplayName", "Description", "FaultRules", "Properties"})
# HTTP header names and Apigee keywords are ASCII and compared without regard to case.
ASCII_FOLD = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)
UNSAFE_KEY_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")
class Method(StrEnum):
    TEMPLATE = "template"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class UnsupportedOption:
    """A setting of a translated policy that the template could not carry over."""

    name: str
    reason: str
    # The setting's text as written in the bundle when it is kept because it can't be translated
    # (a {variable} template a2m cannot read); the generator writes it beside the step.
    original: str | None = None


@dataclass(frozen=True, slots=True)
class PolicyResult:
    """The result record of one policy step: the source of its report row.

    ``name`` is the policy name in a template's output and the step name in
    :attr:`a2m.generator.GenerateResult.policies`; ``location`` and
    ``condition`` are filled in by the generator.
    """

    name: str
    type: str
    method: Method
    reason: str = ""
    unsupported_options: tuple[UnsupportedOption, ...] = ()
    tags: tuple[str, ...] = ()
    location: str = ""
    condition: str | None = None


@dataclass(frozen=True)
class TemplateOutput:
    processors: tuple[ET.Element, ...]
    globals: tuple[ET.Element, ...]
    properties: dict[str, str]
    result: PolicyResult
    needs: frozenset[str] = frozenset()
    # A comment written above a property in the properties file, by key.
    property_notes: dict[str, str] = field(default_factory=dict)
    # The proxy's own flow variables the processors write exactly as Apigee does (names in lower case); every
    # other variable the policy may write in Apigee is one the generated app does not (see registry.variable_writes).
    written: frozenset[str] = frozenset()


class Draft:
    """Collects one template's output; :meth:`skip` and :meth:`done` turn it into a :class:`TemplateOutput`."""

    def __init__(
        self, policy: Policy, direction: str, *, handled: Iterable[str] = (), changes: RequestChanges = NO_CHANGES
    ) -> None:
        if direction not in DIRECTIONS:
            raise ValueError(f"direction must be one of {DIRECTIONS}, not {direction!r}")
        self.policy = policy
        self.direction = direction
        self.changes = changes
        self.options: list[UnsupportedOption] = []
        self.tags: list[str] = []
        self.processors: list[ET.Element] = []
        self.globals: list[ET.Element] = []
        self.properties: dict[str, str] = {}
        self.property_notes: dict[str, str] = {}
        self.needs: set[str] = set()
        # The last write of each flow variable or message part (lower case) the policy makes, in order: None when the
        # processors make it exactly as Apigee does, else what made it in a way the template does not carry over.
        # The last write decides: a later dropped write makes an earlier exact one stale, a later exact one replaces it.
        self.writes: dict[str, str | None] = {}
        self._check_common(set(handled))

    @property
    def settings(self) -> XmlElement:
        return self.policy.settings

    def _check_common(self, handled: set[str]) -> None:
        settings = self.policy.settings
        if self.policy.continue_on_error:
            self.option(
                "continueOnError",
                "continueOnError=true is not carried over: when this step rejects or fails, the generated flow stops",
            )
        if is_true(settings.attributes.get("async")):
            self.option("async", "async=true is not carried over; the step runs in line")
        for name in settings.attributes:
            if name not in COMMON_ATTRIBUTES and name not in handled:
                self.option(name, f"attribute {name} on <{settings.tag}> is not carried over")
        for child in settings.children:
            if child.tag not in COMMON_CHILDREN and child.tag not in handled:
                self.option(child.tag, f"<{child.tag}> is not carried over")
            elif child.tag == "FaultRules" and child.children:
                self.option("FaultRules", "fault rules inside a policy are not carried over")

    def option(self, name: str, reason: str, *, original: str | None = None) -> None:
        self.options.append(UnsupportedOption(name, reason, original))

    def template(
        self,
        setting: str,
        value: str,
        *,
        prefix: str = "{",
        suffix: str = "}",
        changes: RequestChanges | None = None,
    ) -> Translation | None:
        """``value`` as an Apigee message template read on this step's side of the flow.

        None when it can't be translated (including a reference to a request
        header, query parameter or verb an earlier step may have changed): the
        setting is then listed as an unsupported option holding its original
        text, and must not be emitted. ``changes`` replaces the step's
        :attr:`changes` (a policy that changes the request itself passes what
        its earlier operations changed as well).
        """
        seen = self.changes if changes is None else changes
        result = translate_template(value, prefix, suffix, direction=self.direction, changes=seen)
        if not result.ok:
            self.option(setting, f"can't translate the value of {setting}: {result.reason}", original=value)
            return None
        if result.reads_request_snapshot:
            self.need(NEED_REQUEST_SNAPSHOT)
        unresolved = UNRESOLVED_VARIABLES
        if result.dw is not None and not flag(child(self.settings, unresolved)) and not self._noted(unresolved):
            self.option(
                unresolved,
                f"{unresolved} is not true, so Apigee fails the call when a {{variable}} in a value is missing; "
                "the generated app uses an empty string instead",
            )
        return result

    def _noted(self, name: str) -> bool:
        return any(option.name == name for option in self.options)

    def value(self, setting: str, value: str, *, changes: RequestChanges | None = None) -> str | None:
        """DataWeave for a setting's value: a quoted literal without variables, the translated template with
        them, or None when it can't be translated (listed, see :meth:`template`)."""
        result = self.template(setting, value, changes=changes)
        if result is None:
            return None
        if result.dw is None:
            return dw_string(value)
        return f"({result.dw})" if " ++ " in result.dw else result.dw

    def read(self, ref: str, *, changes: RequestChanges | None = None) -> VariableRead:
        """The Apigee variable ``ref`` read on this step's side (see :func:`read_variable`), checked against the
        step's :attr:`changes` (or ``changes``). The caller reports a refusal (``dw`` None) with its ``reason``;
        a read of the request snapshot is wired up here."""
        result = read_variable(ref, self.direction, self.changes if changes is None else changes)
        if result.snapshot:
            self.need(NEED_REQUEST_SNAPSHOT)
        return result

    def need(self, *needs: str) -> None:
        self.needs.update(needs)

    def wrote(self, *names: str) -> None:
        """The processors write the flow variables ``names`` exactly as Apigee does (see :attr:`TemplateOutput.written`)."""
        for name in names:
            self.writes[fold(name.strip())] = None

    def dropped(self, *names: str, by: str | None = None) -> None:
        """The policy writes ``names`` in Apigee where the processors do not write them exactly; ``by`` names what made
        that write (the policy by default), for a later read of the same policy to report."""
        for name in names:
            self.writes[fold(name.strip())] = by or self.policy.name

    @property
    def written(self) -> frozenset[str]:
        """The names whose last write is exact (see :attr:`TemplateOutput.written`)."""
        return frozenset(name for name, by in self.writes.items() if by is None)

    @property
    def stale(self) -> dict[str, str]:
        """The names whose last write is not carried over exactly, by what made it."""
        return {name: by for name, by in self.writes.items() if by is not None}

    def add(self, *processors: ET.Element) -> None:
        self.processors.extend(processors)

    def fault(self, status: int, faultstring: str, errorcode: str) -> list[ET.Element]:
        """Processors that answer ``status`` with an Apigee-style JSON fault and stop the flow."""
        self.need(NEED_FAULT)
        return fault_processors(status, faultstring, errorcode, f"{self.policy.type} {self.policy.name}")

    def _result(self, method: Method, reason: str) -> PolicyResult:
        return PolicyResult(
            name=self.policy.name,
            type=self.policy.type,
            method=method,
            reason=reason,
            unsupported_options=tuple(self.options),
            tags=tuple(self.tags),
        )

    def skip(self, reason: str) -> TemplateOutput:
        """Nothing is generated for this policy: method skipped with ``reason``."""
        return TemplateOutput((), (), {}, self._result(Method.SKIPPED, reason))

    def done(self) -> TemplateOutput:
        if not self.processors:
            return self.skip(f"{self.policy.type} {self.policy.name}: nothing in it could be translated")
        return TemplateOutput(
            tuple(self.processors),
            tuple(self.globals),
            dict(self.properties),
            self._result(Method.TEMPLATE, ""),
            frozenset(self.needs),
            dict(self.property_notes),
            self.written,
        )


# ---------------------------------------------------------------- reading the policy XML


def child(element: XmlElement, tag: str) -> XmlElement | None:
    return next((c for c in element.children if c.tag == tag), None)


def children(element: XmlElement, tag: str) -> list[XmlElement]:
    return [c for c in element.children if c.tag == tag]


def text(element: XmlElement | None) -> str | None:
    """The stripped text of ``element``, or None when it has none."""
    if element is None or element.text is None:
        return None
    value = element.text.strip()
    return value or None


def fold(value: str) -> str:
    """``value`` with A-Z as a-z, for HTTP header names and Apigee keywords (true, request, Encode, ...).

    This is not a name-collision check; those go through :func:`a2m.layout.collision_key`.
    """
    return value.translate(ASCII_FOLD)


def is_true(value: str | None) -> bool:
    """True when an attribute value is 'true' (any case)."""
    return value is not None and fold(value.strip()) == "true"


def flag(element: XmlElement | None, default: bool = False) -> bool:
    value = text(element)
    if value is None:
        return default
    return fold(value) == "true"


def key_part(name: str) -> str:
    """``name`` as part of a Mule name or property key: letters, digits, '_', '.' and '-' only."""
    return UNSAFE_KEY_CHARS.sub("-", name).strip("-.") or "unnamed"


def has_template(value: str) -> bool:
    """True when ``value`` holds an Apigee {variable} reference (or a {...} part a2m does not understand) that a
    literal copy would not resolve; read with the one template reader (:mod:`a2m.conditions.template`)."""
    return has_reference(value)


# ---------------------------------------------------------------- Apigee variables as DataWeave


@dataclass(frozen=True, slots=True)
class VariableRead:
    """The DataWeave reading one Apigee variable named in a policy setting, or why a2m can't read it there.

    ``dw`` is None when it can't be read and ``reason`` says why;
    ``snapshot`` is True when ``dw`` reads the request snapshot (a request
    variable read on the response side).
    """

    dw: str | None
    reason: str = ""
    snapshot: bool = False


def read_variable(ref: str, direction: str, changes: RequestChanges = NO_CHANGES) -> VariableRead:
    """The Apigee variable ``ref`` (a policy's ref attribute, Ref or Source) read on the ``direction`` side.

    Every request variable (``request.header.NAME``, ``request.queryparam.NAME``,
    ``request.verb``, ``proxy.pathsuffix``), every response header
    (``response.header.NAME``, on the response side) and every flow variable goes
    through the one accessor conditions and message templates use
    (:func:`a2m.conditions.variables.accessor`): the same meaning (a header is its
    first comma-separated value, trimmed) and the same refusal of a part that
    ``changes`` says an earlier step may have changed.
    """
    name = ref.strip()
    try:
        read = accessor(name, direction, changes)
    except ConditionError as exc:
        return VariableRead(None, str(exc))
    return VariableRead(read.dw, snapshot=SNAPSHOT_READ in read.dw)


# ---------------------------------------------------------------- building Mule XML


def element(ns: str, tag: str, attrib: dict[str, str] | None = None, body: str | None = None) -> ET.Element:
    node = ET.Element(f"{{{ns}}}{tag}", attrib or {})
    node.text = body
    return node


def sub(parent: ET.Element, ns: str, tag: str, attrib: dict[str, str] | None = None) -> ET.Element:
    node = element(ns, tag, attrib)
    parent.append(node)
    return node


def dw_string(value: str) -> str:
    """``value`` as a single-quoted DataWeave string literal.

    '$' is written as a unicode escape so neither DataWeave interpolation nor
    Mule's ${...} property placeholders ever see it.
    """
    escaped = (
        value.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("$", "\\u0024")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f"'{escaped}'"


def literal_value(value: str) -> str:
    """``value`` for an attribute Mule reads as a literal: plain when safe, else a DataWeave string expression."""
    if "#[" in value or "${" in value:
        return f"#[{dw_string(value)}]"
    return value


def dw_object(pairs: Sequence[tuple[str, str]]) -> str:
    """A DataWeave object literal of string keys and string values, in order."""
    return dw_object_of([(k, dw_string(v)) for k, v in pairs])


def dw_object_of(pairs: Sequence[tuple[str, str]]) -> str:
    """A DataWeave object literal of string keys and DataWeave value expressions, in order."""
    return "{" + ", ".join(f"{dw_string(k)}: {v}" for k, v in pairs) + "}"


def dw_keys(keys: Iterable[str]) -> str:
    return "[" + ", ".join(dw_string(k) for k in keys) + "]"


def without_keys(base: str, keys: Sequence[str]) -> str:
    """``base`` (an object) without the entries named ``keys``, compared case-insensitively."""
    return (
        f"({base} filterObject ((value, key) -> not ({dw_keys(fold(k) for k in keys)} contains lower(key as String))))"
    )


def set_variable(name: str, value: str) -> ET.Element:
    """A set-variable; an expression value gets ``output application/java`` so values read from a JSON body and
    from Java attributes can be combined (DataWeave cannot infer one output type for both)."""
    if value.startswith("#[") and value.endswith("]"):
        value = f"#[output application/java --- {value[2:-1]}]"
    return element(CORE, "set-variable", {"variableName": name, "value": value})


def set_payload(value: str, mime_type: str | None = None, *, expression: str | None = None) -> ET.Element:
    """Set the payload to ``value`` taken literally, or to the DataWeave ``expression`` when one is given."""
    attrib = {"value": f"#[{expression}]" if expression is not None else literal_value(value)}
    if mime_type:
        attrib["mimeType"] = mime_type
    return element(CORE, "set-payload", attrib)


def choice(*branches: tuple[str, Sequence[ET.Element]], otherwise: Sequence[ET.Element] = ()) -> ET.Element:
    node = element(CORE, "choice")
    for expression, body in branches:
        when = sub(node, CORE, "when", {"expression": expression})
        when.extend(body)
    if otherwise:
        sub(node, CORE, "otherwise").extend(otherwise)
    return node


def fault_body(faultstring: str, errorcode: str) -> str:
    return json.dumps({"fault": {"faultstring": faultstring, "detail": {"errorcode": errorcode}}})


def fault_processors(status: int, faultstring: str, errorcode: str, label: str) -> list[ET.Element]:
    """Set the fault response (status, no headers, JSON body) and raise :data:`FAULT_ERROR_TYPE`."""
    return [
        set_variable(STATUS_VAR, f"#[{status}]"),
        set_variable(RESPONSE_HEADERS_VAR, "#[{}]"),
        set_payload(fault_body(faultstring, errorcode), "application/json"),
        element(
            CORE, "raise-error", {"type": FAULT_ERROR_TYPE, "description": f"{label} rejected the call ({status})"}
        ),
    ]


def object_store(name: str, ttl_millis: int) -> ET.Element:
    """A non-persistent object store whose entries expire ``ttl_millis`` after they were written."""
    return element(
        OS,
        "object-store",
        {"name": name, "persistent": "false", "entryTtl": str(ttl_millis), "entryTtlUnit": "MILLISECONDS"},
    )


def os_retrieve(store: str, key: str, target: str, default: str) -> ET.Element:
    node = element(OS, "retrieve", {"key": key, "objectStore": store, "target": target})
    sub(node, OS, "default-value").text = default
    return node


def os_store(store: str, key: str, value: str, *, fail_if_present: bool = False) -> ET.Element:
    """Store ``value`` under ``key``; with ``fail_if_present`` an atomic insert that raises
    :data:`OS_KEY_PRESENT` when the key is already there.

    mule-objectstore-connector 1.2.2 runs every operation under a lock on its key, so of several
    concurrent inserts of one key exactly one succeeds: the only atomic test-and-set available on
    Mule CE, which the rate-limit templates build their counters on.
    """
    attrib = {"key": key, "objectStore": store}
    if fail_if_present:
        attrib["failIfPresent"] = "true"
    node = element(OS, "store", attrib)
    sub(node, OS, "value").text = value
    return node


def os_remove(store: str, key: str) -> ET.Element:
    return element(OS, "remove", {"key": key, "objectStore": store})


def on_error_continue(error_type: str, body: Sequence[ET.Element] = ()) -> ET.Element:
    """An error handler entry that handles ``error_type`` quietly and goes on after its scope."""
    node = element(CORE, "on-error-continue", {"type": error_type, "logException": "false"})
    node.extend(body)
    return node


def try_scope(body: Sequence[ET.Element], *handlers: ET.Element) -> ET.Element:
    node = element(CORE, "try")
    node.extend(body)
    if handlers:
        sub(node, CORE, "error-handler").extend(handlers)
    return node


def foreach(collection: str, body: Sequence[ET.Element], *, counter: str, root: str) -> ET.Element:
    """A for-each scope with its own counter and root-message variable names, so a proxy variable named
    ``counter`` or ``rootMessage`` is never touched. The caller's message is restored when the scope ends."""
    node = element(
        CORE,
        "foreach",
        {"collection": collection, "counterVariableName": counter, "rootMessageVariableName": root},
    )
    node.extend(body)
    return node


def raise_error(error_type: str, description: str) -> ET.Element:
    return element(CORE, "raise-error", {"type": error_type, "description": description})
