"""CP4: the SpikeArrest template (contract in tests/policies/conftest.py)."""

from __future__ import annotations

import re

from .conftest import Kit, policy_xml, refs_var


def spike(name: str, body: str) -> str:
    return policy_xml("SpikeArrest", name, body)


def test_CP4_T11_spike_arrest_30pm_per_client_becomes_a_per_client_pace_limit(kit: Kit) -> None:
    """[CP4-T11] SpikeArrest 30pm per client becomes a per-client rate limit at Apigee's pace."""
    out = kit.translate(spike("Spike-Arrest", '    <Identifier ref="client_id"/>\n    <Rate>30pm</Rate>\n'))

    assert out.method == "template", out.reason
    assert out.option_names() == [], out.option_text()
    assert out.result.name == "Spike-Arrest"
    assert out.result.type == "SpikeArrest"
    assert out.processors, "no Mule processors generated"
    text = out.text()
    # Apigee smooths 30 per minute into one call every 2000 ms.
    assert re.search(r"\b2000\b", text), f"no 2000 ms pace in the generated XML:\n{text}"
    assert refs_var(text, "client_id"), f"the limit is not keyed by flow variable client_id:\n{text}"
    assert re.search(r"\b429\b", text), f"extra calls are not rejected with 429:\n{text}"


def test_CP4_T12_spike_arrest_per_second_without_identifier_uses_one_shared_limit(kit: Kit) -> None:
    """[CP4-T12] SpikeArrest per second without an identifier uses one shared limit."""
    out = kit.translate(spike("Shared-Spike", "    <Rate>10ps</Rate>\n"))

    assert out.method == "template", out.reason
    assert out.option_names() == [], out.option_text()
    text = out.text()
    assert re.search(r"\b100\b", text), f"no 100 ms pace for 10ps:\n{text}"
    assert re.search(r"\b429\b", text), text
    # One shared key: nothing read from the caller or from a variable set elsewhere decides the key.
    own = out.written_vars()
    read = set(re.findall(r"vars\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)", text))
    read |= {m[1] for m in re.findall(r"vars\s*\[\s*(['\"])(.*?)\1\s*\]", text)}
    assert read <= own, f"the limit reads variables it does not set itself: {sorted(read - own)}"
    assert "remoteAddress" not in text, "the shared limit is keyed by the caller's address"


def test_CP4_T13_spike_arrest_options_the_template_cannot_carry_are_listed(kit: Kit) -> None:
    """[CP4-T13] SpikeArrest options the template cannot carry are listed."""
    weighted = kit.translate(
        spike(
            "Weighted-Spike",
            "    <Rate>30pm</Rate>\n    <UseEffectiveCount>true</UseEffectiveCount>\n"
            '    <MessageWeight ref="weight"/>\n',
        )
    )
    hourly = kit.translate(spike("Hourly-Spike", "    <Rate>30ph</Rate>\n"))

    assert weighted.method == "template", weighted.reason
    assert re.search(r"\b2000\b", weighted.text()), weighted.text()
    names = weighted.option_names()
    assert any("UseEffectiveCount" in n for n in names), names
    assert any("MessageWeight" in n for n in names), names

    assert hourly.method == "skipped"
    assert "30ph" in hourly.reason, hourly.reason
    assert hourly.processor_elements() == [], "a guessed limit was generated for an unsupported rate"
