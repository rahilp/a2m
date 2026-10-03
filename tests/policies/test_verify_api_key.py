"""CP4: the VerifyAPIKey template (contract in tests/policies/conftest.py)."""

from __future__ import annotations

import re

from .conftest import Kit, policy_xml, property_refs

QUERY_APIKEY = re.compile(r"""queryParams\s*(?:\.\s*(['"]?)apikey\1(?![\w-])|\[\s*(['"])apikey\2\s*\])""")
HEADER_X_API_KEY = re.compile(
    r"""headers\s*(?:\.\s*(['"])x-api-key\1|\[\s*(['"])x-api-key\2\s*\])""", re.IGNORECASE
)


def verify(name: str, ref: str) -> str:
    return policy_xml("VerifyAPIKey", name, f'    <APIKey ref="{ref}"/>\n')


def test_CP4_T16_verify_api_key_reads_the_query_parameter_and_rejects_missing_or_unknown_keys(kit: Kit) -> None:
    """[CP4-T16] VerifyAPIKey reads the key from the query parameter and rejects missing or unknown keys."""
    out = kit.translate(verify("Verify-Key", "request.queryparam.apikey"))

    assert out.method == "template", out.reason
    assert out.result.type == "VerifyAPIKey"
    assert out.processors, "no Mule processors generated"
    raw = out.text()
    assert QUERY_APIKEY.search(raw), f"the step does not read query parameter apikey:\n{raw}"
    assert re.search(r"\b401\b", out.text()), f"a missing or unknown key is not rejected with 401:\n{raw}"
    # The allowed keys come from a property of the generated properties file, empty by default (fail closed).
    referenced = property_refs(raw) & set(out.properties)
    assert referenced, f"no property of the step is referenced from its XML: {sorted(property_refs(raw))}"
    assert any(out.properties[key] == "" for key in referenced), {k: out.properties[k] for k in referenced}


def test_CP4_T17_verify_api_key_can_read_the_key_from_a_header(kit: Kit) -> None:
    """[CP4-T17] VerifyAPIKey can read the key from a header."""
    out = kit.translate(verify("Verify-Header-Key", "request.header.x-api-key"))

    assert out.method == "template", out.reason
    text = out.text()
    assert HEADER_X_API_KEY.search(text), f"the step does not read header x-api-key:\n{text}"
    assert not QUERY_APIKEY.search(text) and "queryParams" not in text, f"the step reads a query parameter:\n{text}"
    assert re.search(r"\b401\b", text), f"a missing key is not rejected with 401:\n{text}"


def test_CP4_T18_verify_api_key_with_an_unknown_key_source_is_listed(kit: Kit) -> None:
    """[CP4-T18] VerifyAPIKey with a key source the template does not know is listed."""
    out = kit.translate(verify("Verify-Form-Key", "request.formparam.key"))

    assert out.method == "skipped"
    assert "request.formparam.key" in out.reason, out.reason
    assert out.processor_elements() == [], "a step reading some other key source was generated in its place"

