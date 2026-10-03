"""RaiseFault: stop the flow and answer with the configured fault response.

The step sets the status (default 500 "Server Error", as Apigee), reason
phrase, headers and payload of the answer and raises the policy-fault error;
the listening flow's handler sends that answer, so no later step and no target
call runs.
"""

from __future__ import annotations

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
    dw_object,
    dw_string,
    element,
    fold,
    has_template,
    set_payload,
    set_variable,
    text,
)

HANDLED = {"FaultResponse", "IgnoreUnresolvedVariables"}
SET_PARTS = {"StatusCode", "ReasonPhrase", "Headers", "Payload"}
DEFAULT_STATUS = 500
DEFAULT_REASON = "Server Error"


def translate(policy: Policy, *, direction: str) -> TemplateOutput:
    draft = Draft(policy, direction, handled=HANDLED)
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
    if reason is not None and has_template(reason):
        draft.option("ReasonPhrase", "the reason phrase holds a {variable} reference, which is not translated yet")
        reason = None

    draft.add(set_variable(STATUS_VAR, f"#[{status}]"))
    if reason is not None:
        draft.need(NEED_REASON_PHRASE)
        draft.add(set_variable(REASON_PHRASE_VAR, f"#[{dw_string(reason)}]"))
    headers = _headers(draft, set_block)
    draft.add(set_variable(RESPONSE_HEADERS_VAR, f"#[{dw_object(headers)}]"))
    payload = child(set_block, "Payload") if set_block is not None else None
    body = payload.text or "" if payload is not None else ""
    if has_template(body):
        draft.option("Set Payload", "the fault payload holds {variable} references, which are not translated yet")
        body = ""
    draft.add(set_payload(body, payload.attributes.get("contentType") if payload is not None else None))
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
        value = header.text or ""
        if not name or has_template(value):
            draft.option(f"Header {name}".strip(), f"the fault header {name} holds a {{variable}} reference or no name")
            continue
        found.append((fold(name), value))
    return found
