"""RaiseFault: stop the flow and answer with the configured fault response.

The step sets the status (default 500 "Server Error", as Apigee), reason
phrase, headers and payload of the answer and raises the policy-fault error;
the listening flow's handler sends that answer, so no later step and no target
call runs. Header, payload and reason phrase values are Apigee message templates,
translated as in AssignMessage (see :mod:`a2m.conditions`).
"""

from __future__ import annotations

from a2m.conditions import NO_CHANGES, RequestChanges
from a2m.ir import Policy, XmlElement
from a2m.policies.common import (
    CORE,
    FAULT_ERROR_TYPE,
    NEED_FAULT,
    NEED_REASON_PHRASE,
    REASON_PHRASE_VAR,
    RESPONSE_HEADERS_VAR,
    STATUS_VAR,
    Draft,
    TemplateOutput,
    child,
    children,
    dw_object_of,
    element,
    fold,
    set_payload,
    set_variable,
    text,
)

HANDLED = {"FaultResponse", "IgnoreUnresolvedVariables"}
SET_PARTS = {"StatusCode", "ReasonPhrase", "Headers", "Payload"}
DEFAULT_STATUS = 500
DEFAULT_REASON = "Server Error"


def translate(policy: Policy, *, direction: str, changes: RequestChanges = NO_CHANGES) -> TemplateOutput:
    draft = Draft(policy, direction, handled=HANDLED, changes=changes)
    response = child(draft.settings, "FaultResponse")
    set_block = child(response, "Set") if response is not None else None
    if response is not None:
        for part in response.children:
            if part.tag != "Set":
                draft.option(part.tag, f"<{part.tag}> in FaultResponse is not carried over")
    if set_block is not None:
        for part in set_block.children:
            if part.tag not in SET_PARTS:
                draft.option(f"Set {part.tag}", f"<{part.tag}> in the fault response is not carried over")

    status_text = text(child(set_block, "StatusCode")) if set_block is not None else None
    reason = text(child(set_block, "ReasonPhrase")) if set_block is not None else None
    if status_text is not None and (not status_text.isdigit() or not 100 <= int(status_text) <= 599):
        return draft.skip(
            f"RaiseFault {policy.name} has the status code '{status_text}'; only a fixed number from 100 to 599 "
            "is translated"
        )
    status = int(status_text) if status_text is not None else DEFAULT_STATUS
    if reason is None and status_text is None:
        reason = DEFAULT_REASON
    phrase = draft.value("ReasonPhrase", reason) if reason is not None else None

    draft.add(set_variable(STATUS_VAR, f"#[{status}]"))
    if phrase is not None:
        draft.need(NEED_REASON_PHRASE)
        draft.add(set_variable(REASON_PHRASE_VAR, f"#[{phrase}]"))
    headers = _headers(draft, set_block)
    draft.add(set_variable(RESPONSE_HEADERS_VAR, f"#[{dw_object_of(headers)}]"))
    payload = child(set_block, "Payload") if set_block is not None else None
    body = payload.text or "" if payload is not None else ""
    expression: str | None = None
    if payload is not None:
        prefix = payload.attributes.get("variablePrefix") or "{"
        suffix = payload.attributes.get("variableSuffix") or "}"
        translated = draft.template("Set Payload", body, prefix=prefix, suffix=suffix)
        if translated is None:
            # The fault answer is still sent, with an empty body; the payload is listed as can't translate.
            body = ""
        else:
            expression = translated.dw
    content_type = payload.attributes.get("contentType") if payload is not None else None
    draft.add(set_payload(body, content_type, expression=expression))
    draft.need(NEED_FAULT)
    draft.add(
        element(CORE, "raise-error", {"type": FAULT_ERROR_TYPE, "description": f"RaiseFault {policy.name} ({status})"})
    )
    return draft.done()


def _headers(draft: Draft, set_block: XmlElement | None) -> list[tuple[str, str]]:
    group = child(set_block, "Headers") if set_block is not None else None
    found: list[tuple[str, str]] = []
    for header in children(group, "Header") if group is not None else []:
        name = header.attributes.get("name", "").strip()
        if not name:
            draft.option("Header", "a fault header without a name is not carried over")
            continue
        value = draft.value(f"Header {name}", header.text or "")
        if value is not None:
            found.append((fold(name), value))
    return found
