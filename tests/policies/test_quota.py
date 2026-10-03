"""CP4: the Quota template (contract in tests/policies/conftest.py)."""

from __future__ import annotations

import re

from .conftest import Kit, Translation, policy_xml, refs_var

HOURLY = '    <Allow count="1000"/>\n    <Interval>1</Interval>\n    <TimeUnit>hour</TimeUnit>\n'


def one_hour_window(out: Translation) -> bool:
    """A 1-hour window: 3600000 ms, 3600 s or PT1H in the XML, or an entry TTL of 1 with unit HOURS."""
    if re.search(r"\b3600000\b|\b3600\b|\bPT1H\b", out.text()):
        return True
    for element in out.elements():
        values = [v.strip().upper() for v in element.attrib.values()]
        if "HOURS" in values and "1" in values:
            return True
    return False


def test_CP4_T14_quota_1000_per_hour_per_client_becomes_a_windowed_counter_tagged_for_review(kit: Kit) -> None:
    """[CP4-T14] Quota of 1000 per hour per client becomes a windowed counter tagged for review."""
    out = kit.translate(policy_xml("Quota", "Hourly-Quota", HOURLY + '    <Identifier ref="client_id"/>\n'))

    assert out.method == "template", out.reason
    assert out.result.type == "Quota"
    assert out.processors, "no Mule processors generated"
    text = out.text()
    assert re.search(r"\b1000\b", text), f"the allowance 1000 is not in the generated XML:\n{text}"
    assert re.search(r"\b429\b", text), f"calls over the quota are not rejected with 429:\n{text}"
    assert refs_var(text, "client_id"), f"the counter is not kept per flow variable client_id:\n{text}"
    assert one_hour_window(out), f"no 1-hour window in the generated XML:\n{text}"
    assert "time-window" in [str(t) for t in out.result.tags], list(out.result.tags)


def test_CP4_T15_quota_options_the_template_cannot_carry_are_listed(kit: Kit) -> None:
    """[CP4-T15] Quota options the template cannot carry are listed."""
    out = kit.translate(
        policy_xml(
            "Quota",
            "Shared-Quota",
            HOURLY + "    <CountOnly>true</CountOnly>\n    <SharedName>shared-orders</SharedName>\n",
        )
    )

    names = out.option_names()
    assert any("CountOnly" in n for n in names), names
    assert any("SharedName" in n for n in names), names
    assert out.method == "template", out.reason
    assert re.search(r"\b1000\b", out.text()), out.text()
    assert one_hour_window(out), out.text()
