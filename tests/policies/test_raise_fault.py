"""CP4: the RaiseFault template (contract in tests/policies/conftest.py)."""

from __future__ import annotations

import re

from .conftest import CORE, Kit, dw_unescape, policy_xml, tag

NOT_FOUND = (
    "    <FaultResponse>\n        <Set>\n"
    '            <Headers>\n                <Header name="X-Error">missing-order</Header>\n            </Headers>\n'
    '            <Payload contentType="application/json">{"error":"not found"}</Payload>\n'
    "            <StatusCode>404</StatusCode>\n            <ReasonPhrase>Not Found</ReasonPhrase>\n"
    "        </Set>\n    </FaultResponse>\n"
)
AFTER = (
    "    <Set>\n        <Headers>\n            <Header name=\"X-After\">yes</Header>\n        </Headers>\n    </Set>\n"
)


def test_CP4_T27_raise_fault_returns_the_configured_status_body_and_header_and_stops_the_flow(kit: Kit) -> None:
    """[CP4-T27] RaiseFault returns the configured status, body and header and stops the flow."""
    project, result = kit.generate(
        {
            "Order-Not-Found": policy_xml("RaiseFault", "Order-Not-Found", NOT_FOUND),
            "After-Fault": policy_xml("AssignMessage", "After-Fault", AFTER),
        },
        request=["Order-Not-Found", "After-Fault"],
    )

    step = project.step_elements("Order-Not-Found")
    raises = [e for e in step if e.tag == tag(CORE, "raise-error")]
    assert len(raises) >= 1, "the RaiseFault step raises no Mule error, so the flow would go on"
    blockers = [e.tag for e in step if e.tag in (tag(CORE, "on-error-continue"), tag(CORE, "choice"))]
    assert blockers == [], f"the raised error is caught or made conditional inside the step: {blockers}"

    # The error's answer: whatever handles it (the step's variables, the listener's error response or an
    # error handler) carries the configured status, reason, body, content type and header.
    strings = [dw_unescape(s) for s in project.strings(project.all_elements())]
    text = "\n".join(strings)
    assert re.search(r"\b404\b", text), f"no 404 for the fault:\n{text}"
    assert "Not Found" in text, text
    assert any('{"error":"not found"}' in s for s in strings), f"the fault body is missing or altered:\n{text}"
    assert "application/json" in text, text
    assert re.search(r"""(['"]?)X-Error\1\s*:\s*(['"])missing-order\2""", text, re.IGNORECASE), text
    entry = [r for r in result.policies if str(r.name) == "Order-Not-Found"]
    assert len(entry) == 1 and str(entry[0].method) == "template", [(r.name, r.method) for r in result.policies]


def test_CP4_T28_raise_fault_without_a_status_code_defaults_to_500_server_error(kit: Kit) -> None:
    """[CP4-T28] RaiseFault without a status code defaults to 500 Server Error."""
    body = (
        "    <FaultResponse>\n        <Set>\n"
        '            <Payload contentType="application/json">{"error":"boom"}</Payload>\n'
        "        </Set>\n    </FaultResponse>\n"
    )
    out = kit.translate(policy_xml("RaiseFault", "Plain-Fault", body))

    assert out.method == "template", out.reason
    text = out.text()
    assert re.search(r"\b500\b", text), f"no default status 500:\n{text}"
    assert "Server Error" in text, f"no default reason Server Error:\n{text}"


def test_CP4_T29_raise_fault_options_the_template_cannot_carry_are_listed(kit: Kit) -> None:
    """[CP4-T29] RaiseFault options the template cannot carry are listed."""
    body = (
        "    <FaultResponse>\n"
        "        <AssignVariable>\n            <Name>fault.flag</Name>\n            <Value>yes</Value>\n"
        "        </AssignVariable>\n"
        "        <Set>\n            <StatusCode>403</StatusCode>\n            <ReasonPhrase>Forbidden</ReasonPhrase>\n"
        "        </Set>\n    </FaultResponse>\n"
        "    <ShortFaultReason>true</ShortFaultReason>\n"
    )
    out = kit.translate(policy_xml("RaiseFault", "Forbidden-Fault", body))

    names = out.option_names()
    assert any("ShortFaultReason" in n for n in names), names
    assert any("AssignVariable" in n for n in names), names
    assert out.method == "template", out.reason
    assert re.search(r"\b403\b", out.text()), out.text()
