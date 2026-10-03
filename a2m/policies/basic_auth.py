"""BasicAuthentication: build or read an HTTP Basic Authorization value.

Encode joins the User and Password variables with ':' and writes
'Basic <base64>' to the header or variable AssignTo names. Decode reads the
Source header, and fails (500, as Apigee's InvalidBasicAuthenticationSource)
when it is missing or not a Basic value; otherwise it sets the User and
Password variables to the parts before and after the first ':'.
"""

from __future__ import annotations

from a2m.ir import Policy
from a2m.policies.common import (
    NEED_REQUEST_HEADERS,
    REQUEST,
    REQUEST_HEADERS_BASE,
    REQUEST_HEADERS_VAR,
    RESPONSE,
    RESPONSE_HEADERS_BASE,
    RESPONSE_HEADERS_VAR,
    Draft,
    TemplateOutput,
    child,
    choice,
    dw_string,
    flag,
    fold,
    is_custom_variable,
    is_true,
    read_variable,
    set_variable,
    text,
    without_keys,
)

HANDLED = {"Operation", "User", "Password", "AssignTo", "Source", "IgnoreUnresolvedVariables"}
HEADER_VAR = "a2mBasicAuth"
DECODED_VAR = "a2mBasicDecoded"
INVALID_SOURCE = "steps.basicauthentication.InvalidBasicAuthenticationSource"
BASIC_VALUE = r"/(?i)^Basic\s+[A-Za-z0-9+\/]+=*\s*$/"


def translate(policy: Policy, *, direction: str) -> TemplateOutput:
    draft = Draft(policy, direction, handled=HANDLED)
    operation = text(child(draft.settings, "Operation")) or ""
    if fold(operation) == "encode":
        return _encode(draft)
    if fold(operation) == "decode":
        return _decode(draft)
    return draft.skip(
        f"BasicAuthentication {policy.name} has the operation '{operation}'; only Encode and Decode are translated"
    )


def _ref(draft: Draft, tag: str) -> str | None:
    element = child(draft.settings, tag)
    return element.attributes.get("ref", "").strip() or None if element is not None else None


def _encode(draft: Draft) -> TemplateOutput:
    name = draft.policy.name
    user_ref, password_ref = _ref(draft, "User"), _ref(draft, "Password")
    if user_ref is None or password_ref is None:
        return draft.skip(f"BasicAuthentication {name} (Encode) needs <User ref> and <Password ref>")
    user, password = read_variable(user_ref, draft.direction), read_variable(password_ref, draft.direction)
    if user is None or password is None:
        unreadable = user_ref if user is None else password_ref
        return draft.skip(f"BasicAuthentication {name} reads {unreadable}, which a2m cannot read here")
    assign_to = child(draft.settings, "AssignTo")
    target = text(assign_to) or ""
    if not target:
        return draft.skip(f"BasicAuthentication {name} (Encode) has no <AssignTo>")
    if assign_to is not None and is_true(assign_to.attributes.get("createNew")):
        draft.option("AssignTo createNew", "createNew=true is not carried over; the value is written in place")
    if not flag(child(draft.settings, "IgnoreUnresolvedVariables")):
        missing = f"#[({user}) == null or ({password}) == null]"
        draft.add(
            choice(
                (
                    missing,
                    draft.fault(
                        500,
                        f"Unresolved variable : {user_ref} or {password_ref}",
                        "steps.basicauthentication.UnresolvedVariable",
                    ),
                )
            )
        )
    value = (
        f"'Basic ' ++ dw::core::Binaries::toBase64((((({user}) default '') as String) ++ ':' ++ "
        f"((({password}) default '') as String)) as Binary {{encoding: 'UTF-8'}})"
    )
    lowered = fold(target)
    if lowered.startswith("request.header.") and draft.direction == REQUEST:
        header = fold(target[len("request.header.") :])
        draft.need(NEED_REQUEST_HEADERS)
        kept = without_keys(REQUEST_HEADERS_BASE, [header])
        draft.add(set_variable(REQUEST_HEADERS_VAR, f"#[{kept} ++ {{{dw_string(header)}: {value}}}]"))
    elif lowered.startswith("response.header.") and draft.direction == RESPONSE:
        header = fold(target[len("response.header.") :])
        kept = without_keys(RESPONSE_HEADERS_BASE, [header])
        draft.add(set_variable(RESPONSE_HEADERS_VAR, f"#[{kept} ++ {{{dw_string(header)}: {value}}}]"))
    elif is_custom_variable(target):
        draft.add(set_variable(target, f"#[{value}]"))
    else:
        return draft.skip(
            f"BasicAuthentication {name} assigns to {target} in a {draft.direction} flow, which a2m cannot write"
        )
    return draft.done()


def _decode(draft: Draft) -> TemplateOutput:
    name = draft.policy.name
    source = text(child(draft.settings, "Source")) or ""
    reader = read_variable(source, draft.direction) if source else None
    if reader is None:
        return draft.skip(f"BasicAuthentication {name} (Decode) reads '{source}', which a2m cannot read here")
    user_ref, password_ref = _ref(draft, "User"), _ref(draft, "Password")
    if (
        user_ref is None
        or password_ref is None
        or not (is_custom_variable(user_ref) and is_custom_variable(password_ref))
    ):
        return draft.skip(
            f"BasicAuthentication {name} (Decode) needs <User ref> and <Password ref> naming flow variables"
        )
    invalid = draft.fault(500, f"Invalid basic authentication source {source}", INVALID_SOURCE)
    no_colon = draft.fault(500, f"Invalid basic authentication source {source}", INVALID_SOURCE)
    draft.add(
        set_variable(HEADER_VAR, f"#[{reader}]"),
        choice((f"#[not (((vars.{HEADER_VAR} default '') as String) matches {BASIC_VALUE})]", invalid)),
        set_variable(
            DECODED_VAR,
            f"#[dw::core::Binaries::fromBase64(trim((vars.{HEADER_VAR} as String)[6 to -1])) as String "
            "{encoding: 'UTF-8'}]",
        ),
        choice(
            (
                f"#[not ((vars.{DECODED_VAR} as String) contains ':')]",
                no_colon,
            )
        ),
        set_variable(user_ref, f"#[dw::core::Strings::substringBefore(vars.{DECODED_VAR}, ':')]"),
        set_variable(password_ref, f"#[dw::core::Strings::substringAfter(vars.{DECODED_VAR}, ':')]"),
    )
    return draft.done()
