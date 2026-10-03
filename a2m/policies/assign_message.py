"""AssignMessage: change headers, query parameters and the payload, and set flow variables.

The message changed is the one AssignTo names (type request or response),
else the one of the flow the step is in. Request changes go to the headers
and query parameters the target request sends; response changes go to the
response the listener sends. Apigee applies Remove, then Add, then Set, then
AssignVariable; so does the template.

Values are copied literally; a value holding an Apigee {variable} reference is
listed as unsupported instead of being copied with the reference unresolved.
A new message (AssignTo createNew or a named message variable) is kept as an
object in a flow variable, since nothing in the generated flow sends it.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Sequence

from a2m.ir import Policy, XmlElement
from a2m.policies.common import (
    NEED_REASON_PHRASE,
    NEED_REQUEST_HEADERS,
    NEED_REQUEST_QUERY,
    REASON_PHRASE_VAR,
    REQUEST,
    REQUEST_HEADERS_BASE,
    REQUEST_HEADERS_VAR,
    REQUEST_QUERY_BASE,
    REQUEST_QUERY_VAR,
    RESPONSE,
    RESPONSE_HEADERS_BASE,
    RESPONSE_HEADERS_VAR,
    STATUS_VAR,
    Draft,
    TemplateOutput,
    child,
    children,
    dw_keys,
    dw_object,
    dw_string,
    flag,
    fold,
    has_template,
    is_custom_variable,
    is_true,
    literal_value,
    read_variable,
    set_payload,
    set_variable,
    text,
    without_keys,
)

HANDLED = {"AssignTo", "Set", "Add", "Remove", "Copy", "AssignVariable", "IgnoreUnresolvedVariables"}
NEW_MESSAGE_VAR = "a2m.newMessage"
# What each operation can change, by element: Headers and QueryParams everywhere, the rest only under Set.
SET_PARTS = {"Headers", "QueryParams", "Payload", "StatusCode", "ReasonPhrase"}
ADD_PARTS = {"Headers", "QueryParams"}
REMOVE_PARTS = {"Headers", "QueryParams", "Payload"}


class _Message:
    """Builds the changes to one message (the request, the response or a new message in a variable)."""

    def __init__(self, draft: Draft, kind: str, new_var: str | None) -> None:
        self.draft = draft
        self.kind = kind
        self.new_var = new_var
        self.new_parts: dict[str, list[tuple[str, str]]] = {"headers": [], "queryParams": []}
        self.new_payload: str | None = None

    # ------------------------------------------------------------ entries

    def entries(self, group: XmlElement, item: str, where: str) -> list[tuple[str, str]]:
        """(name, value) of every <item name="..."> in ``group``; values with a {variable} are listed, not copied."""
        found: list[tuple[str, str]] = []
        for entry in children(group, item):
            name = entry.attributes.get("name", "").strip()
            value = entry.text or ""
            if not name:
                self.draft.option(f"{where} {item}", f"a <{item}> without a name in <{where}> is not carried over")
            elif has_template(value):
                self.draft.option(
                    f"{where} {item} {name}",
                    f"the value of {item} {name} holds a {{variable}} reference, which is not translated yet",
                )
            else:
                found.append((name, value))
        return found

    # ------------------------------------------------------------ operations

    def remove(self, part: XmlElement) -> None:
        if part.tag == "Headers":
            names = [e.attributes.get("name", "").strip() for e in children(part, "Header")]
            self._headers("remove", [(n, "") for n in names if n], remove_all=not part.children)
        elif part.tag == "QueryParams":
            names = [e.attributes.get("name", "").strip() for e in children(part, "QueryParam")]
            self._query("remove", [(n, "") for n in names if n], remove_all=not part.children)
        elif part.tag == "Payload":
            if flag(part, default=True):
                self._payload("", None)

    def add(self, part: XmlElement) -> None:
        if part.tag == "Headers":
            self._headers("add", self.entries(part, "Header", "Add"))
        elif part.tag == "QueryParams":
            self._query("add", self.entries(part, "QueryParam", "Add"))

    def set(self, part: XmlElement) -> None:
        if part.tag == "Headers":
            self._headers("set", self.entries(part, "Header", "Set"))
        elif part.tag == "QueryParams":
            self._query("set", self.entries(part, "QueryParam", "Set"))
        elif part.tag == "Payload":
            value = part.text or ""
            if has_template(value) or "variablePrefix" in part.attributes:
                self.draft.option(
                    "Set Payload", "the payload holds {variable} references, which are not translated yet"
                )
                return
            self._payload(value, part.attributes.get("contentType"))
        elif part.tag in ("StatusCode", "ReasonPhrase"):
            self._status(part)

    # ------------------------------------------------------------ the parts of a message

    def _headers(self, op: str, pairs: Sequence[tuple[str, str]], *, remove_all: bool = False) -> None:
        names = [fold(n) for n, _ in pairs]
        if self.new_var is not None:
            if op == "remove":
                self.draft.option(
                    "Remove Headers",
                    f"removing Headers from the message in flow variable {self.new_var} is not carried over",
                )
            else:
                self.new_parts["headers"] += [(fold(n), v) for n, v in pairs]
            return
        if not pairs and not remove_all:
            return
        if self.kind == REQUEST:
            var, base = REQUEST_HEADERS_VAR, REQUEST_HEADERS_BASE
            self.draft.need(NEED_REQUEST_HEADERS)
        else:
            var, base = RESPONSE_HEADERS_VAR, RESPONSE_HEADERS_BASE
        self.draft.add(set_variable(var, f"#[{_changed(base, op, names, pairs, remove_all, lower=True)}]"))

    def _query(self, op: str, pairs: Sequence[tuple[str, str]], *, remove_all: bool = False) -> None:
        if self.new_var is not None:
            if op == "remove":
                self.draft.option(
                    "Remove QueryParams",
                    f"removing QueryParams from the message in flow variable {self.new_var} is not carried over",
                )
            else:
                self.new_parts["queryParams"] += list(pairs)
            return
        if not pairs and not remove_all:
            return
        if self.kind != REQUEST:
            self.draft.option("QueryParams", "a response has no query parameters; the change is not carried over")
            return
        self.draft.need(NEED_REQUEST_QUERY)
        names = [n for n, _ in pairs]
        expression = _changed(REQUEST_QUERY_BASE, op, names, pairs, remove_all, lower=False)
        self.draft.add(set_variable(REQUEST_QUERY_VAR, f"#[{expression}]"))

    def _payload(self, value: str, content_type: str | None) -> None:
        if self.new_var is not None:
            self.new_payload = value
            return
        if self.kind != self.draft.direction:
            self.draft.option(
                "Payload",
                f"the {self.kind} payload cannot be changed in a {self.draft.direction} flow of the generated app",
            )
            return
        self.draft.add(set_payload(value, content_type))
        if value and self.kind == REQUEST:
            # The target request forwards the caller's verb, and only the PreFlow and PostFlow (which run
            # for every verb) are generated, so the verb is never known here: say when the body is lost.
            self.draft.option(
                "Payload on GET",
                "the request payload it sets reaches the target only when the call's verb is not GET, HEAD "
                "or OPTIONS: for those verbs Mule's http:request sends no body (Apigee would send it), and "
                "the verb is not known when the app is generated",
            )
        if content_type and self.kind == REQUEST:
            pairs = [("content-type", content_type)]
            self.draft.need(NEED_REQUEST_HEADERS)
            expression = _changed(REQUEST_HEADERS_BASE, "set", ["content-type"], pairs, False, lower=True)
            self.draft.add(set_variable(REQUEST_HEADERS_VAR, f"#[{expression}]"))

    def _status(self, part: XmlElement) -> None:
        value = text(part) or ""
        if self.kind != RESPONSE or self.new_var is not None or self.draft.direction != RESPONSE:
            self.draft.option(part.tag, f"<{part.tag}> is only carried over for the response in a response flow")
            return
        if part.tag == "StatusCode":
            if not value.isdigit() or not 100 <= int(value) <= 599:
                self.draft.option("StatusCode", f"the status code '{value}' is not a fixed number from 100 to 599")
                return
            self.draft.add(set_variable(STATUS_VAR, f"#[{int(value)}]"))
        else:
            if has_template(value):
                self.draft.option("ReasonPhrase", "the reason phrase holds a {variable} reference")
                return
            self.draft.need(NEED_REASON_PHRASE)
            self.draft.add(set_variable(REASON_PHRASE_VAR, f"#[{dw_string(value)}]"))

    def finish(self) -> None:
        """Write a new message's collected parts into its variable."""
        if self.new_var is None:
            return
        parts = [f"{key}: {dw_object(pairs)}" for key, pairs in self.new_parts.items()]
        if self.new_payload is not None:
            parts.append(f"payload: {dw_string(self.new_payload)}")
        self.draft.add(set_variable(self.new_var, "#[{" + ", ".join(parts) + "}]"))


def _changed(
    base: str, op: str, names: Sequence[str], pairs: Sequence[tuple[str, str]], remove_all: bool, *, lower: bool
) -> str:
    """DataWeave for ``base`` after removing, adding or setting ``pairs``."""
    added = dw_object([(fold(n) if lower else n, v) for n, v in pairs])
    if op == "remove":
        if remove_all:
            return "{}"
        return without_keys(base, names) if lower else f"({base} -- {dw_keys(names)})"
    if op == "add":
        return f"({base} ++ {added})"
    kept = without_keys(base, names) if lower else f"({base} -- {dw_keys(names)})"
    return f"({kept} ++ {added})"


def translate(policy: Policy, *, direction: str) -> TemplateOutput:
    draft = Draft(policy, direction, handled=HANDLED)
    settings = draft.settings
    assign_to = child(settings, "AssignTo")
    kind = direction
    new_var: str | None = None
    if assign_to is not None:
        kind = fold(assign_to.attributes.get("type", direction).strip()) or direction
        if kind not in (REQUEST, RESPONSE):
            return draft.skip(f"AssignMessage {policy.name} assigns to a message of type '{kind}'")
        named = text(assign_to)
        create_new = is_true(assign_to.attributes.get("createNew"))
        if create_new or named:
            new_var = named or NEW_MESSAGE_VAR
            draft.option(
                "AssignTo createNew" if create_new else f"AssignTo {named}",
                f"{'createNew=true: ' if create_new else ''}the {kind} message it assigns to is kept as an object "
                f"in flow variable {new_var}; nothing in the generated flow sends it",
            )
    if child(settings, "Copy") is not None:
        draft.option("Copy", "copying from another message is not carried over")
    message = _Message(draft, kind, new_var)

    operations = (
        ("Remove", REMOVE_PARTS, message.remove),
        ("Add", ADD_PARTS, message.add),
        ("Set", SET_PARTS, message.set),
    )
    for op, allowed, apply in operations:
        block = child(settings, op)
        if block is None:
            continue
        for part in block.children:
            if part.tag not in allowed:
                draft.option(f"{op} {part.tag}", f"<{part.tag}> in <{op}> is not carried over")
            else:
                apply(part)
    message.finish()
    for assignment in children(settings, "AssignVariable"):
        _assign_variable(draft, assignment)
    if not draft.processors and not draft.options:
        return draft.skip(f"AssignMessage {policy.name} changes nothing a2m can see")
    return draft.done()


def _assign_variable(draft: Draft, assignment: XmlElement) -> None:
    name = text(child(assignment, "Name")) or ""
    if not name or not is_custom_variable(name):
        draft.option(
            f"AssignVariable {name}".strip(),
            f"AssignVariable '{name}' is not carried over: only the proxy's own flow variables are set",
        )
        return
    for tag in ("Template", "PropertySetRef", "ResourceURL"):
        if child(assignment, tag) is not None:
            draft.option(f"AssignVariable {name} {tag}", f"<{tag}> in AssignVariable {name} is not carried over")
            return
    value_element = child(assignment, "Value")
    value = value_element.text if value_element is not None and value_element.text is not None else None
    ref = text(child(assignment, "Ref"))
    reader = read_variable(ref, draft.direction) if ref else None
    if ref and reader is None:
        draft.option(
            f"AssignVariable {name} Ref", f"the variable {ref} that AssignVariable {name} reads is not readable"
        )
        return
    if value is not None and has_template(value):
        draft.option(f"AssignVariable {name} Value", f"the value of {name} holds a {{variable}} reference")
        return
    if reader is not None:
        fallback = dw_string(value) if value is not None else "null"
        processor: ET.Element = set_variable(name, f"#[{reader} default {fallback}]")
    elif value is not None:
        processor = set_variable(name, literal_value(value))
    else:
        processor = set_variable(name, "#[null]")
    draft.add(processor)
