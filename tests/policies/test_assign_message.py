"""CP4: the AssignMessage template (contract in tests/policies/conftest.py)."""

from __future__ import annotations

import re

from .conftest import CORE, HTTP, Kit, dw_unescape, is_element, literal, policy_xml, refs_var, tag


def header_pair(name: str, value: str) -> re.Pattern[str]:
    """A DataWeave object entry name: value, either side quoted."""
    return re.compile(rf"""(['"]?){re.escape(name)}\1\s*:\s*(['"]){re.escape(value)}\2""", re.IGNORECASE)


def removes(name: str, text: str) -> bool:
    """True when ``text`` is an expression that removes key ``name`` (-- / - operator, filter or != test)."""
    if not re.search(rf"""['"]{re.escape(name)}['"]""", text, re.IGNORECASE):
        return False
    return re.search(r"--|\s-\s|!=|filterObject|\bremove", text) is not None


def payloads(elements: list) -> list[tuple[str | None, str | None]]:
    return [
        (literal(e.get("value") if e.get("value") is not None else e.text), e.get("mimeType"))
        for e in elements
        if is_element(e) and e.tag == tag(CORE, "set-payload")
    ]


REQUEST_CHANGES = (
    "    <Set>\n        <Headers>\n            <Header name=\"X-Env\">prod</Header>\n        </Headers>\n    </Set>\n"
    "    <Add>\n        <QueryParams>\n            <QueryParam name=\"version\">2</QueryParam>\n"
    "        </QueryParams>\n    </Add>\n"
    "    <Remove>\n        <Headers>\n            <Header name=\"X-Internal\"/>\n        </Headers>\n    </Remove>\n"
    "    <AssignVariable>\n        <Name>target.env</Name>\n        <Value>prod</Value>\n    </AssignVariable>\n"
    '    <AssignTo createNew="false" transport="http" type="request"/>\n'
)


def test_CP4_T19_assign_message_sets_adds_removes_and_assigns_on_the_request(kit: Kit) -> None:
    """[CP4-T19] AssignMessage sets a header, adds a query parameter, removes a header and sets a variable."""
    out = kit.translate(policy_xml("AssignMessage", "Add-Target-Headers", REQUEST_CHANGES))

    assert out.method == "template", out.reason
    assert out.option_names() == [], out.option_text()
    strings = out.strings()
    text = "\n".join(strings)
    assert header_pair("X-Env", "prod").search(text), f"header X-Env: prod is not set:\n{text}"
    assert header_pair("version", "2").search(text), f"query parameter version=2 is not added:\n{text}"
    assert any(removes("X-Internal", s) for s in strings), f"header X-Internal is not removed:\n{text}"
    assert out.var_values("target.env") == ["prod"], (out.written_vars(), out.var_values("target.env"))


def test_CP4_T20_assign_message_sets_a_json_payload_with_its_content_type(kit: Kit) -> None:
    """[CP4-T20] AssignMessage sets a JSON payload with its content type."""
    body = '    <Set>\n        <Payload contentType="application/json">{"status":"ok"}</Payload>\n    </Set>\n'
    out = kit.translate(policy_xml("AssignMessage", "Set-Json", body))

    assert out.method == "template", out.reason
    found = payloads(out.processor_elements())
    assert len(found) == 1, found
    value, mime = found[0]
    assert value == '{"status":"ok"}', value
    assert (mime or "").split(";")[0].strip() == "application/json", mime


def test_CP4_T21_special_characters_in_assign_message_values_survive_intact(kit: Kit) -> None:
    """[CP4-T21] Special characters in AssignMessage values survive intact."""
    body = (
        "    <Set>\n        <Headers>\n"
        '            <Header name="X-Note">a&lt;b &amp; "c"</Header>\n'
        "        </Headers>\n"
        '        <Payload contentType="text/plain">Tom &amp; Jerry &lt;3</Payload>\n'
        "    </Set>\n"
    )
    project, _ = kit.generate({"Add-Note": policy_xml("AssignMessage", "Add-Note", body)}, request=["Add-Note"])

    elements = project.step_elements("Add-Note")
    strings = project.strings(elements)
    assert any('a<b & "c"' in dw_unescape(s) for s in strings), f"header value mangled: {strings}"
    found = payloads(elements)
    assert len(found) == 1, found
    value, mime = found[0]
    assert value == "Tom & Jerry <3", value
    assert (mime or "").split(";")[0].strip() == "text/plain", mime
    for entity in ("&amp;", "&lt;", "&quot;", "&#"):
        assert not any(entity in s for s in strings), f"double-escaped {entity} in {strings}"


def test_CP4_T22_assign_message_in_a_response_flow_changes_the_response_not_the_request(kit: Kit) -> None:
    """[CP4-T22] AssignMessage in a response flow changes the response, not the request."""
    body = (
        "    <Set>\n        <Headers>\n            <Header name=\"X-Served-By\">a2m</Header>\n"
        "        </Headers>\n    </Set>\n"
        '    <AssignTo createNew="false" transport="http" type="response"/>\n'
    )
    project, _ = kit.generate(
        {"Set-Response-Header": policy_xml("AssignMessage", "Set-Response-Header", body)},
        response=["Set-Response-Header"],
    )

    elements = project.step_elements("Set-Response-Header")
    text = "\n".join(project.strings(elements))
    assert header_pair("X-Served-By", "a2m").search(text), f"X-Served-By: a2m is not set:\n{text}"
    written = {
        str(e.get("variableName")) for e in elements if e.tag == tag(CORE, "set-variable") and e.get("variableName")
    } | {str(e.get("target")) for e in elements if e.get("target")}
    assert written, "the step sets no variable, so it cannot change the response the listener sends"

    (flow,) = project.listener_flows()
    listener = next(c for c in flow if is_element(c) and c.tag == tag(HTTP, "listener"))
    response = listener.find(tag(HTTP, "response"))
    assert response is not None, "the listener has no http:response"
    sent = "\n".join(project.strings([response]))
    assert any(refs_var(sent, name) for name in written), (
        f"the listener's response does not use any variable the step sets ({sorted(written)}):\n{sent}"
    )
    requests = [e for e in project.all_elements() if e.tag == tag(HTTP, "request")]
    assert len(requests) == 1, len(requests)
    outgoing = "\n".join(project.strings([requests[0]]))
    touched = [name for name in written if refs_var(outgoing, name)]
    assert touched == [], f"the outgoing request reads variables the response step sets: {touched}"


def test_CP4_T23_assign_message_options_the_template_cannot_carry_are_listed(kit: Kit) -> None:
    """[CP4-T23] AssignMessage options the template cannot carry are listed."""
    body = (
        '    <Copy source="request">\n        <Headers/>\n    </Copy>\n'
        "    <Set>\n        <Headers>\n            <Header name=\"X-Kept\">yes</Header>\n        </Headers>\n    </Set>\n"
        '    <AssignTo createNew="true" transport="http" type="request">newRequest</AssignTo>\n'
    )
    out = kit.translate(policy_xml("AssignMessage", "Copy-And-Create", body))

    assert "createNew" in out.option_text(), out.option_text()
    assert any("Copy" in n for n in out.option_names()), out.option_names()
    text = out.text()
    assert header_pair("X-Kept", "yes").search(text), f"the supported Set part was not generated:\n{text}"


def test_CP4_T43_a_request_payload_mule_drops_on_get_is_listed_not_silently_lost(kit: Kit) -> None:
    """[CP4-T43] A request payload Mule sends no body for on GET is listed as unsupported, not silently lost."""
    request_body = '    <Set>\n        <Payload contentType="text/plain">hello</Payload>\n    </Set>\n'
    response_body = (
        '    <Set>\n        <Payload contentType="text/plain">bye</Payload>\n    </Set>\n'
        '    <AssignTo createNew="false" transport="http" type="response"/>\n'
    )
    project, result = kit.generate(
        {
            "Set-Request-Body": policy_xml("AssignMessage", "Set-Request-Body", request_body),
            "Set-Response-Body": policy_xml("AssignMessage", "Set-Response-Body", response_body),
        },
        request=["Set-Request-Body"],
        response=["Set-Response-Body"],
    )

    assert payloads(project.step_elements("Set-Request-Body")) == [("hello", "text/plain")]
    listed = [item for item in result.unsupported if "Payload on GET" in str(item.name)]
    assert listed, [f"{item.name}: {item.reason}" for item in result.unsupported]
    reasons = " | ".join(str(item.reason) for item in listed)
    assert "Set-Request-Body" in reasons and "GET" in reasons, reasons
    assert "Set-Response-Body" not in reasons, reasons
    (record,) = [r for r in result.policies if r.name == "Set-Request-Body"]
    assert "Payload on GET" in [str(o.name) for o in record.unsupported_options], record.unsupported_options
    (other,) = [r for r in result.policies if r.name == "Set-Response-Body"]
    assert "Payload on GET" not in [str(o.name) for o in other.unsupported_options], other.unsupported_options
