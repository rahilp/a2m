"""CP4: the BasicAuthentication template (contract in tests/policies/conftest.py)."""

from __future__ import annotations

import re

from .conftest import CORE, Kit, policy_xml, refs_var, tag

VALIDATION = "http://www.mulesoft.org/schema/mule/validation"
COLON = re.compile(r"""(['"]):\1""")
AUTHORIZATION_HEADER = re.compile(
    r"""headers\s*(?:\.\s*(['"]?)authorization\1(?![\w-])|\[\s*(['"])authorization\2\s*\])""", re.IGNORECASE
)


def test_CP4_T30_basic_authentication_encode_builds_the_authorization_header(kit: Kit) -> None:
    """[CP4-T30] BasicAuthentication Encode builds the Authorization header from two variables."""
    body = (
        "    <Operation>Encode</Operation>\n    <IgnoreUnresolvedVariables>false</IgnoreUnresolvedVariables>\n"
        '    <User ref="creds.user"/>\n    <Password ref="creds.pass"/>\n'
        '    <AssignTo createNew="false">request.header.Authorization</AssignTo>\n'
    )
    out = kit.translate(policy_xml("BasicAuthentication", "Encode-Basic-Auth", body))

    assert out.method == "template", out.reason
    assert out.result.type == "BasicAuthentication"
    text = out.text()
    assert refs_var(text, "creds.user"), f"flow variable creds.user is not read:\n{text}"
    assert refs_var(text, "creds.pass"), f"flow variable creds.pass is not read:\n{text}"
    assert "Basic " in text, f"the header value does not start with 'Basic ':\n{text}"
    assert re.search(r"base64", text, re.IGNORECASE), f"user:password is not base64-encoded:\n{text}"
    assert COLON.search(text), f"user and password are not joined with ':':\n{text}"
    assert re.search(r"""(['"]?)Authorization\1\s*:""", text, re.IGNORECASE), (
        f"the outgoing Authorization header is not set:\n{text}"
    )


def test_CP4_T31_basic_authentication_decode_splits_the_header_and_fails_on_a_missing_header(kit: Kit) -> None:
    """[CP4-T31] BasicAuthentication Decode splits the header into user and password, and fails on a missing header."""
    body = (
        "    <Operation>Decode</Operation>\n    <IgnoreUnresolvedVariables>false</IgnoreUnresolvedVariables>\n"
        '    <User ref="auth.user"/>\n    <Password ref="auth.pass"/>\n'
        "    <Source>request.header.Authorization</Source>\n"
    )
    out = kit.translate(policy_xml("BasicAuthentication", "Decode-Basic-Auth", body))

    assert out.method == "template", out.reason
    written = out.written_vars()
    assert {"auth.user", "auth.pass"} <= written, sorted(written)
    text = out.text()
    assert AUTHORIZATION_HEADER.search(text), f"the Authorization header is not read:\n{text}"
    assert re.search(r"base64", text, re.IGNORECASE), f"the header is not base64-decoded:\n{text}"
    assert COLON.search(text), f"the decoded value is not split at ':':\n{text}"
    failing = [
        e.tag for e in out.processor_elements() if e.tag == tag(CORE, "raise-error") or e.tag.startswith(f"{{{VALIDATION}}}")
    ]
    assert failing, "nothing raises an error when the header is missing or not Basic"


def test_CP4_T32_basic_authentication_with_an_unknown_operation_is_listed_not_guessed(kit: Kit) -> None:
    """[CP4-T32] BasicAuthentication with an unknown operation is listed, not guessed."""
    body = (
        "    <Operation>Rot13</Operation>\n"
        '    <User ref="creds.user"/>\n    <Password ref="creds.pass"/>\n'
        '    <AssignTo createNew="false">request.header.Authorization</AssignTo>\n'
    )
    out = kit.translate(policy_xml("BasicAuthentication", "Rot-Auth", body))

    assert out.method == "skipped"
    assert "Rot13" in out.reason, out.reason
    assert out.processor_elements() == [], "an Encode or Decode step was generated for an unknown operation"
