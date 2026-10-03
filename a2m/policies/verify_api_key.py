"""VerifyAPIKey: only calls with an allowed API key get through, others get 401.

Apigee checks keys against its developer apps; a Mule app has none, so the
allowed keys come from a property of the generated properties file
(comma-separated). The property is empty by default, which rejects every key
until the user fills it in. The key is read from the query parameter or header
the policy names; nothing else is guessed.
"""

from __future__ import annotations

from a2m.ir import Policy
from a2m.policies.common import (
    REQUEST,
    Draft,
    TemplateOutput,
    child,
    choice,
    dw_string,
    fold,
    key_part,
    request_header,
    request_query,
    set_variable,
)

KEY_VAR = "a2mApiKey"
STATIC_KEYS_TAG = "allowed-keys-property"
HANDLED = {"APIKey"}


def property_key(policy: Policy) -> str:
    return f"verifyapikey.{key_part(policy.name)}.allowedKeys"


def translate(policy: Policy, *, direction: str) -> TemplateOutput:
    draft = Draft(policy, direction, handled=HANDLED)
    api_key = child(draft.settings, "APIKey")
    ref = (api_key.attributes.get("ref", "") if api_key is not None else "").strip()
    if not ref:
        return draft.skip(f"VerifyAPIKey {policy.name} names no variable in <APIKey ref>, so there is no key to read")
    lowered = fold(ref)
    reader: str | None = None
    if direction == REQUEST and lowered.startswith("request.queryparam."):
        reader = request_query(ref[len("request.queryparam.") :])
    elif direction == REQUEST and lowered.startswith("request.header."):
        reader = request_header(ref[len("request.header.") :])
    if reader is None:
        return draft.skip(
            f"VerifyAPIKey {policy.name} reads the key from {ref} in the {direction} flow; a2m translates only "
            "request.queryparam.* and request.header.* in a request flow"
        )

    key = property_key(policy)
    draft.properties[key] = ""
    draft.property_notes[key] = (
        f"API keys that VerifyAPIKey {policy.name} accepts, comma-separated. Empty rejects every key: fill it in."
    )
    draft.tags.append(STATIC_KEYS_TAG)
    allowed = f"((((p({dw_string(key)}) default '') as String) splitBy ',') map trim($)) filter ($ != '')"
    draft.add(
        set_variable(KEY_VAR, f"#[{reader}]"),
        choice(
            (
                f"#[isEmpty(vars.{KEY_VAR})]",
                draft.fault(401, f"Failed to resolve API Key variable {ref}", "steps.oauth.v2.FailedToResolveAPIKey"),
            ),
            (
                f"#[not (({allowed}) contains (vars.{KEY_VAR} as String))]",
                draft.fault(401, "Invalid ApiKey", "oauth.v2.InvalidApiKey"),
            ),
        ),
    )
    return draft.done()
