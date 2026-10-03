"""AssignMessage: change headers, query parameters and the payload, and set flow variables.

The message changed is the one AssignTo names (type request or response),
else the one of the flow the step is in. Request changes go to the headers
and query parameters the target request sends; response changes go to the
response the listener sends. Apigee applies Remove, then Copy, then Add, then
Set, then AssignVariable; so does the template. A value read by a later
operation sees what the earlier operations of the same policy changed: a
template or AssignVariable Ref reading a request header, query parameter or
the verb that an earlier operation (or another entry of the same operation)
changes is can't translate, exactly as a read after an earlier step is (the
generated value would read the caller's original request). The same holds for
every message the policy changes: a response header or payload (or the request
payload) an earlier operation changes in Apigee where the template does not make
that change exactly (Copy, a value that can't be translated) is can't translate
when a later AssignVariable Ref reads it, and every part of a message kept in a
named variable counts as changed.

Header, query parameter, payload and reason phrase values are Apigee message
templates: a value without {variable} references is copied literally, one with
references becomes a DataWeave expression (see :mod:`a2m.conditions`), and one
a2m cannot translate is listed as an unsupported option holding its original
text, never emitted half translated or with the reference unresolved. A
payload's variablePrefix and variableSuffix are honoured.
A new message (AssignTo createNew or a named message variable) is kept as an
object in a flow variable, since nothing in the generated flow sends it.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Sequence

from a2m.conditions import (
    ANY,
    NO_CHANGES,
    REQUEST_CONTENT_TYPE,
    RESPONSE_CONTENT_TYPE,
    RESPONSE_HEADER_PREFIX,
    RequestChanges,
)
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
    dw_object_of,
    dw_string,
    flag,
    fold,
    has_template,
    is_custom_variable,
    is_true,
    literal_value,
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
# The order Apigee applies an AssignMessage's operations in (AssignVariable comes last).
OPERATION_ORDER = ("Remove", "Copy", "Add", "Set")
# What _request_target answers for a policy that replaces the request with a new one.
NEW_REQUEST = "new"


class _Message:
    """Builds the changes to one message (the request, the response or a new message in a variable)."""

    def __init__(self, draft: Draft, kind: str, new_var: str | None) -> None:
        self.draft = draft
        self.kind = kind
        self.new_var = new_var
        self.new_parts: dict[str, list[tuple[str, str]]] = {"headers": [], "queryParams": []}
        self.new_payload: str | None = None
        # The request changes the step's earlier steps and this policy's earlier operations made, and the ones
        # of each entry of the operation being translated (see _operation_changes).
        self.prior: RequestChanges = draft.changes
        self.current: list[tuple[XmlElement, RequestChanges]] = []
        # The message parts an entry of the operation being translated writes in Apigee where the template does not
        # (a value that can't be translated): the operation does not write them exactly, whatever its other entries do.
        self.op_dropped: set[str] = set()

    def visible(self, entry: XmlElement | None) -> RequestChanges:
        """The request changes a value of ``entry`` must not read: those of the earlier steps and operations,
        and those of every other entry of the same operation (Apigee's order inside one operation is not
        relied on). An entry's own change is not one: its value is read before it is written."""
        seen = self.prior
        for element, changes in self.current:
            if element is not entry:
                seen = seen | changes
        return seen

    # ------------------------------------------------------------ entries

    def entries(self, group: XmlElement, item: str, where: str) -> list[tuple[str, str]]:
        """(name, DataWeave value) of every <item name="..."> in ``group``; a value that can't be translated is
        listed, not copied."""
        found: list[tuple[str, str]] = []
        for entry in children(group, item):
            name = entry.attributes.get("name", "").strip()
            if not name:
                self.draft.option(f"{where} {item}", f"a <{item}> without a name in <{where}> is not carried over")
                continue
            value = self.draft.value(f"{where} {item} {name}", entry.text or "", changes=self.visible(entry))
            if value is not None:
                found.append((name, value))
            elif item == "Header":
                self.op_dropped.add(RESPONSE_HEADER_PREFIX + fold(name))
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
            prefix = part.attributes.get("variablePrefix") or "{"
            suffix = part.attributes.get("variableSuffix") or "}"
            result = self.draft.template(
                "Set Payload", value, prefix=prefix, suffix=suffix, changes=self.visible(None)
            )
            if result is not None:
                self._payload(value, part.attributes.get("contentType"), result.dw)
            else:
                self.op_dropped.add(f"{self.kind}.content")
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
        if self.kind == RESPONSE:
            # The response headers being built are what a later step reads (see variable_writes).
            self.draft.wrote(*([RESPONSE_HEADER_PREFIX] if remove_all else [RESPONSE_HEADER_PREFIX + n for n in names]))
        if op == "remove" and (remove_all or "content-type" in names):
            # No Content-Type: neither Apigee nor the generated app reads the payload as JSON. Setting or adding one
            # is not exact for that read: Mule's payload keeps the media type it was parsed with.
            self.draft.wrote(REQUEST_CONTENT_TYPE if self.kind == REQUEST else RESPONSE_CONTENT_TYPE)

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

    def _payload(self, value: str, content_type: str | None, expression: str | None = None) -> None:
        """Set the payload to ``value``, or to the DataWeave ``expression`` (a translated template) when given."""
        if self.new_var is not None:
            self.new_payload = expression if expression is not None else dw_string(value)
            return
        if self.kind != self.draft.direction:
            self.draft.option(
                "Payload",
                f"the {self.kind} payload cannot be changed in a {self.draft.direction} flow of the generated app",
            )
            return
        self.draft.add(set_payload(value, content_type, expression=expression))
        self.draft.wrote(f"{self.kind}.content")
        if value and self.kind == REQUEST:
            # The target request forwards the caller's verb, which a template never knows (the step may run for
            # any verb, whatever condition guards it): say when the body is lost.
            self.draft.option(
                "Payload on GET",
                "the request payload it sets reaches the target only when the call's verb is not GET, HEAD "
                "or OPTIONS: for those verbs Mule's http:request sends no body (Apigee would send it), and "
                "the verb is not known when the app is generated",
            )
        if content_type and self.kind == REQUEST:
            pairs = [("content-type", dw_string(content_type))]
            self.draft.need(NEED_REQUEST_HEADERS)
            expression = _changed(REQUEST_HEADERS_BASE, "set", ["content-type"], pairs, False, lower=True)
            self.draft.add(set_variable(REQUEST_HEADERS_VAR, f"#[{expression}]"))
            # The header and the payload's media type are set together, as in Apigee.
            self.draft.wrote(REQUEST_CONTENT_TYPE)
        elif content_type:
            # The response being built carries the target's Content-Type header, which the listener sends: set it.
            pairs = [("content-type", dw_string(content_type))]
            expression = _changed(RESPONSE_HEADERS_BASE, "set", ["content-type"], pairs, False, lower=True)
            self.draft.add(set_variable(RESPONSE_HEADERS_VAR, f"#[{expression}]"))
            self.draft.wrote(RESPONSE_HEADER_PREFIX + "content-type", RESPONSE_CONTENT_TYPE)

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
            phrase = self.draft.value("ReasonPhrase", value, changes=self.visible(None))
            if phrase is None:
                return
            self.draft.need(NEED_REASON_PHRASE)
            self.draft.add(set_variable(REASON_PHRASE_VAR, f"#[{phrase}]"))

    def finish(self) -> None:
        """Write a new message's collected parts into its variable."""
        if self.new_var is None:
            return
        parts = [f"{key}: {dw_object_of(pairs)}" for key, pairs in self.new_parts.items()]
        if self.new_payload is not None:
            parts.append(f"payload: {self.new_payload}")
        self.draft.add(set_variable(self.new_var, "#[{" + ", ".join(parts) + "}]"))


def _changed(
    base: str, op: str, names: Sequence[str], pairs: Sequence[tuple[str, str]], remove_all: bool, *, lower: bool
) -> str:
    """DataWeave for ``base`` after removing, adding or setting ``pairs`` (name, DataWeave value)."""
    if op == "remove":
        if remove_all:
            return "{}"
        return without_keys(base, names) if lower else f"({base} -- {dw_keys(names)})"
    added = dw_object_of([(fold(n) if lower else n, v) for n, v in pairs])
    if op == "add":
        return f"({base} ++ {added})"
    kept = without_keys(base, names) if lower else f"({base} -- {dw_keys(names)})"
    return f"({kept} ++ {added})"


def translate(policy: Policy, *, direction: str, changes: RequestChanges = NO_CHANGES) -> TemplateOutput:
    draft = Draft(policy, direction, handled=HANDLED, changes=changes)
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
    target = _request_target(policy, direction)
    if target == NEW_REQUEST:
        message.prior = message.prior | _every_change(policy.name)
    # draft.writes holds the last write of every message part (request.content, response.content,
    # response.header.NAME) and flow variable this policy makes so far: exact, or stale by the operation that made it.
    changed, create_new_message = _message_of(policy, direction)
    tracked = changed in (REQUEST, RESPONSE)
    if tracked and create_new_message:
        draft.dropped(*_new_message_writes(changed), by=f"{policy.name} <AssignTo>")
    elif not tracked and is_custom_variable(changed):
        # A message kept in a named variable: a2m keeps it as one object, so every part of it read by name is stale.
        draft.dropped(changed, changed + ".", by=f"{policy.name} <AssignTo {changed}>")
    request_seen = message.prior

    operations = {
        "Remove": (REMOVE_PARTS, message.remove),
        "Add": (ADD_PARTS, message.add),
        "Set": (SET_PARTS, message.set),
    }
    for op in OPERATION_ORDER:
        message.prior = request_seen | _stale_changes(draft.stale)
        message.current = _operation_changes(policy, op, f"{policy.name} <{op}>") if target is not None else []
        message.op_dropped = set()
        blocks = children(settings, op)
        earlier_writes, draft.writes = draft.writes, {}
        if op in operations and blocks:
            allowed, apply = operations[op]
            for part in blocks[0].children:
                if part.tag not in allowed:
                    draft.option(f"{op} {part.tag}", f"<{part.tag}> in <{op}> is not carried over")
                else:
                    apply(part)
        if op in operations and len(blocks) > 1:
            draft.option(op, f"only the first <{op}> is carried over; the other <{op}> elements are not")
            message.op_dropped |= _blocks_message_writes(blocks[1:], op, changed)
        op_writes, draft.writes = draft.writes, earlier_writes
        # A part counts as written exactly by this operation only when none of its entries for it is dropped.
        exact = {name for name, by in op_writes.items() if by is None} - message.op_dropped
        for name in exact:
            # Set replaces the part (and Remove clears it): written exactly, it reads the same value as in Apigee
            # again. Add appends to it, so a value an earlier operation left stale stays stale.
            if op != "Add" or draft.writes.get(name) is None:
                draft.wrote(name)
        if tracked and not create_new_message:
            # The last write of a part this operation changes in Apigee but not exactly in the template is stale.
            draft.dropped(*(_op_message_writes(settings, op, changed) - exact), by=f"{policy.name} <{op}>")
        for _, change in message.current:
            request_seen = request_seen | change
        message.current = []
    message.prior = request_seen | _stale_changes(draft.stale)
    message.finish()
    for assignment in children(settings, "AssignVariable"):
        # A read sees the last write of every part and variable so far: a variable an earlier AssignVariable could not
        # set is a missing value, not Apigee's, until a later exact one sets it again.
        dropped = _assign_variable(draft, assignment, request_seen | _stale_changes(draft.stale))
        if dropped is not None:
            draft.dropped(dropped, by=f"{policy.name} <AssignVariable>")
    if not draft.processors and not draft.options:
        return draft.skip(f"AssignMessage {policy.name} changes nothing a2m can see")
    return draft.done()


def _stale_changes(stale: dict[str, str]) -> RequestChanges:
    """``stale`` (message part -> the operation that changed it) as :attr:`RequestChanges.variables`."""
    return RequestChanges(variables=frozenset(stale.items()))


def _every_change(step: str) -> RequestChanges:
    every = frozenset({(ANY, step)})
    return RequestChanges(every, every, frozenset({step}))


def _request_target(policy: Policy, direction: str) -> str | None:
    """REQUEST when ``policy`` (on the ``direction`` side) changes the request in Apigee, :data:`NEW_REQUEST`
    when it replaces it with a new request (AssignTo createNew without a variable name), else None (it changes
    the response or a message kept in a variable other than ``request``)."""
    assign_to = child(policy.settings, "AssignTo")
    kind = direction
    if assign_to is not None:
        kind = fold(assign_to.attributes.get("type", direction).strip()) or direction
        named = text(assign_to)
        if named and fold(named) != REQUEST:
            return None
        if not named and is_true(assign_to.attributes.get("createNew")) and kind == REQUEST:
            return NEW_REQUEST
    return REQUEST if kind == REQUEST else None


def _operation_changes(policy: Policy, op: str, step: str) -> list[tuple[XmlElement, RequestChanges]]:
    """The request changes of each entry of ``policy``'s ``op`` blocks (Remove, Copy, Add or Set), by the
    element that makes it, attributed to ``step``: a named Header or QueryParam; a Headers or QueryParams list
    that Remove or Copy empties or copies whole (every one of them); Set Payload with a contentType (the
    Content-Type header); Set or Copy Verb."""
    found: list[tuple[XmlElement, RequestChanges]] = []
    for block in children(policy.settings, op):
        for part in block.children:
            if part.tag in ("Headers", "QueryParams"):
                item = "Header" if part.tag == "Headers" else "QueryParam"
                entries = [(part, ANY)] if op in ("Remove", "Copy") and not part.children else [
                    (e, fold(e.attributes.get("name", "").strip())) for e in children(part, item)
                ]
                for element, name in entries:
                    if not name:
                        continue
                    pair = frozenset({(name, step)})
                    if part.tag == "Headers":
                        found.append((element, RequestChanges(headers=pair)))
                    else:
                        found.append((element, RequestChanges(queries=pair)))
            elif op == "Set" and part.tag == "Payload" and part.attributes.get("contentType"):
                found.append((part, RequestChanges(headers=frozenset({("content-type", step)}))))
            elif part.tag == "Verb" and (op == "Set" or (op == "Copy" and flag(part, default=True))):
                found.append((part, RequestChanges(verb=frozenset({step}))))
    return found


def request_changes(policy: Policy, *, direction: str) -> RequestChanges:
    """The request headers, query parameters and verb this step changes in Apigee, whether or not a2m carries
    the change over (a setting a2m drops still changes the request in Apigee).

    Remove, Add, Set and Copy of Headers and QueryParams count, by name, or as
    every one of them for an empty Remove or Copy list and for a new request
    (AssignTo createNew without a variable name, which also replaces the verb).
    Set Verb and Copy Verb change the verb. Set Payload with a contentType
    changes the Content-Type header. A message kept in a named variable other
    than ``request`` is not the request.
    """
    target = _request_target(policy, direction)
    if target is None:
        return RequestChanges()
    if target == NEW_REQUEST:
        return _every_change(policy.name)
    changes = RequestChanges()
    for op in OPERATION_ORDER:
        for _, change in _operation_changes(policy, op, policy.name):
            changes = changes | change
    return changes


def variable_writes(policy: Policy, *, direction: str = REQUEST) -> frozenset[str]:
    """The proxy's own flow variables and message parts this policy writes in Apigee on the ``direction`` side, in
    lower case: every AssignVariable Name; a message kept in a named variable (that name and every variable under
    it, ``NAME.``); the payload of the request or response it changes (``request.content``, ``response.content``),
    the Content-Type with its payload's media type (``request.content-type``, ``response.content-type``) when it
    changes either, and the response headers it changes
    (``response.header.NAME``, or ``response.header.`` for all of them)."""
    found = {
        fold(name)
        for assignment in children(policy.settings, "AssignVariable")
        if (name := text(child(assignment, "Name")) or "") and is_custom_variable(name)
    }
    named = text(child(policy.settings, "AssignTo"))
    if named and is_custom_variable(named):
        found |= {fold(named), fold(named) + "."}
    return frozenset(found | _message_writes(policy, direction))


def _message_writes(policy: Policy, direction: str) -> set[str]:
    """The payload and response headers of the request or response ``policy`` changes in Apigee (see
    :func:`variable_writes`); a fault rule's message is the response."""
    message, create_new = _message_of(policy, direction)
    if message not in (REQUEST, RESPONSE):
        return set()
    if create_new:
        return _new_message_writes(message)
    found: set[str] = set()
    for op in OPERATION_ORDER:
        found |= _op_message_writes(policy.settings, op, message)
    return found


def _message_of(policy: Policy, direction: str) -> tuple[str, bool]:
    """The message ``policy`` changes in Apigee on the ``direction`` side (``request``, ``response`` or the
    lower-case name of the variable AssignTo names) and whether AssignTo creates it new."""
    assign_to = child(policy.settings, "AssignTo")
    message = direction if direction in (REQUEST, RESPONSE) else RESPONSE
    create_new = False
    if assign_to is not None:
        message = fold(assign_to.attributes.get("type", message).strip()) or message
        create_new = is_true(assign_to.attributes.get("createNew"))
        named = text(assign_to)
        if named:
            message = fold(named)
    return message, create_new


def _new_message_writes(message: str) -> set[str]:
    """A new request or response replaces every part of it."""
    return {f"{message}.content", f"{message}.content-type", *([RESPONSE_HEADER_PREFIX] if message == RESPONSE else [])}


def _op_message_writes(settings: XmlElement, op: str, message: str) -> set[str]:
    """The payload and response headers of ``message`` (request or response) the ``op`` blocks change in Apigee."""
    return _blocks_message_writes(children(settings, op), op, message)


def _blocks_message_writes(blocks: Sequence[XmlElement], op: str, message: str) -> set[str]:
    """The payload and response headers of ``message`` the ``op`` elements ``blocks`` change in Apigee."""
    found: set[str] = set()
    for block in blocks:
        for part in block.children:
            if part.tag == "Payload" and (op == "Set" or (op in ("Remove", "Copy") and flag(part, default=True))):
                # The payload and its media type, as a JSON read sees them (REQUEST_CONTENT_TYPE).
                found |= {f"{message}.content", f"{message}.content-type"}
                if op == "Set" and message == RESPONSE and part.attributes.get("contentType"):
                    found.add(RESPONSE_HEADER_PREFIX + "content-type")
            elif part.tag == "Headers":
                names = {fold(entry.attributes.get("name", "").strip()) for entry in children(part, "Header")}
                if (op in ("Remove", "Copy") and not part.children) or "content-type" in names:
                    found.add(f"{message}.content-type")
            if part.tag == "Headers" and message == RESPONSE:
                if op in ("Remove", "Copy") and not part.children:
                    found.add(RESPONSE_HEADER_PREFIX)
                found |= {
                    RESPONSE_HEADER_PREFIX + fold(name)
                    for entry in children(part, "Header")
                    if (name := entry.attributes.get("name", "").strip())
                }
    return found


def _assign_variable(draft: Draft, assignment: XmlElement, changes: RequestChanges) -> str | None:
    """One AssignVariable; ``changes`` are the request changes made before it, by earlier steps and by this
    policy's own operations (and the variables its earlier AssignVariables could not set). Returns the name of
    the proxy's own flow variable it could not set, else None."""
    name = text(child(assignment, "Name")) or ""
    if not name or not is_custom_variable(name):
        draft.option(
            f"AssignVariable {name}".strip(),
            f"AssignVariable '{name}' is not carried over: only the proxy's own flow variables are set",
        )
        return None
    for tag in ("Template", "PropertySetRef", "ResourceURL"):
        if child(assignment, tag) is not None:
            draft.option(f"AssignVariable {name} {tag}", f"<{tag}> in AssignVariable {name} is not carried over")
            return name
    value_element = child(assignment, "Value")
    value = value_element.text if value_element is not None and value_element.text is not None else None
    ref = text(child(assignment, "Ref"))
    read = draft.read(ref, changes=changes) if ref else None
    reader = read.dw if read is not None else None
    if read is not None and reader is None:
        draft.option(
            f"AssignVariable {name} Ref",
            f"the variable {ref} that AssignVariable {name} reads can't be read here: {read.reason}",
        )
        return name
    if value is not None and has_template(value):
        draft.option(f"AssignVariable {name} Value", f"the value of {name} holds a {{variable}} reference")
        return name
    if reader is not None:
        fallback = dw_string(value) if value is not None else "null"
        processor: ET.Element = set_variable(name, f"#[{reader} default {fallback}]")
    elif value is not None:
        processor = set_variable(name, literal_value(value))
    else:
        processor = set_variable(name, "#[null]")
    draft.add(processor)
    draft.wrote(name)
    return None
