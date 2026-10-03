"""CP4: the AccessControl template (contract in tests/policies/conftest.py)."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from .conftest import Kit, Translation, policy_xml


def rules(no_match: str, *rules_xml: str) -> str:
    return f'    <IPRules noRuleMatchAction="{no_match}">\n' + "".join(rules_xml) + "    </IPRules>\n"


def match_rule(action: str, address: str, mask: str) -> str:
    return (
        f'        <MatchRule action="{action}">\n'
        f'            <SourceAddress mask="{mask}">{address}</SourceAddress>\n'
        "        </MatchRule>\n"
    )


SUBNET_THEN_HOST = (match_rule("ALLOW", "10.0.0.0", "24"), match_rule("DENY", "192.168.1.5", "32"))


def first_index(strings: list[str], needle: str) -> int:
    for index, value in enumerate(strings):
        if needle in value:
            return index
    raise AssertionError(f"{needle!r} is not in the generated XML: {strings}")


def serialised(out: Translation) -> list[bytes]:
    return [ET.tostring(e) for e in [*out.processors, *out.globals]]


def test_CP4_T33_access_control_checks_rules_in_order_with_a_default_deny(kit: Kit) -> None:
    """[CP4-T33] AccessControl allows a subnet, denies one address, and denies everything else, in rule order."""
    out = kit.translate(policy_xml("AccessControl", "Check-IP", rules("DENY", *SUBNET_THEN_HOST)))
    allow_rest = kit.translate(policy_xml("AccessControl", "Check-IP", rules("ALLOW", *SUBNET_THEN_HOST)))

    assert out.method == "template", out.reason
    assert out.result.type == "AccessControl"
    strings = out.strings()
    text = "\n".join(strings)
    assert "remoteAddress" in text, f"the client address is not checked:\n{text}"
    assert re.search(r"10\.0\.0\.0(/24\b|[^\n]*\b24\b)|\b24\b[^\n]*10\.0\.0\.0", text), f"no 10.0.0.0/24 rule:\n{text}"
    assert re.search(r"\b403\b", text), f"rejected addresses do not get 403:\n{text}"
    assert first_index(strings, "10.0.0.0") <= first_index(strings, "192.168.1.5"), "rules are not kept in order"
    # The no-match action decides what every other address gets, so DENY and ALLOW must generate different steps.
    assert allow_rest.method == "template", allow_rest.reason
    assert serialised(out) != serialised(allow_rest), "noRuleMatchAction DENY and ALLOW generate the same step"


def test_CP4_T34_access_control_settings_the_template_cannot_carry_are_listed(kit: Kit) -> None:
    """[CP4-T34] AccessControl settings the template cannot carry are listed."""
    forwarded = kit.translate(
        policy_xml(
            "AccessControl",
            "Check-Forwarded",
            rules("DENY", *SUBNET_THEN_HOST) + "    <ValidateBasedOn>X_FORWARDED_FOR_ALL_IP</ValidateBasedOn>\n",
        )
    )
    bad_mask = kit.translate(policy_xml("AccessControl", "Check-Bad-Mask", rules("ALLOW", match_rule("DENY", "10.0.0.0", "33"))))

    assert any("ValidateBasedOn" in n for n in forwarded.option_names()), forwarded.option_names()

    # Dropping the only rule would leave noRuleMatchAction ALLOW, an allow-all step: the policy is skipped instead.
    assert bad_mask.method == "skipped", bad_mask.option_text()
    assert "33" in bad_mask.reason, bad_mask.reason
    assert bad_mask.processor_elements() == [], "an allow-all or deny-all step was generated for a bad mask"
