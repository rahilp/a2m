"""The policy registry: one template per Apigee policy type, and an explicit result for every other type.

Mirrors PolicyTransformationFactory.cs in the Azure tool, except that an
unknown type is never translated into nothing: :func:`translate` returns a
``skipped`` result with a reason, and the generator lists it as unsupported.
"""

from __future__ import annotations

from collections.abc import Callable

from a2m.conditions import ANY, NO_CHANGES, REQUEST, RequestChanges
from a2m.ir import Policy
from a2m.policies import (
    access_control,
    assign_message,
    basic_auth,
    extract_variables,
    quota,
    raise_fault,
    spike_arrest,
    verify_api_key,
)
from a2m.policies.common import DIRECTIONS, Method, PolicyResult, TemplateOutput

Template = Callable[..., TemplateOutput]

TEMPLATES: dict[str, Template] = {
    "SpikeArrest": spike_arrest.translate,
    "Quota": quota.translate,
    "VerifyAPIKey": verify_api_key.translate,
    "AssignMessage": assign_message.translate,
    "ExtractVariables": extract_variables.translate,
    "RaiseFault": raise_fault.translate,
    "BasicAuthentication": basic_auth.translate,
    "AccessControl": access_control.translate,
}


def get_template(policy_type: str) -> Template | None:
    """The template for ``policy_type``, or None when a2m has none."""
    return TEMPLATES.get(policy_type)


def translate(policy: Policy, *, direction: str, changes: RequestChanges = NO_CHANGES) -> TemplateOutput:
    """``policy`` through its template; a type without one comes back skipped, with a reason.

    ``changes`` are the request headers, query parameters and verb earlier steps on the same path may have
    changed (see :mod:`a2m.conditions.variables`); a value reading one of them is listed as can't translate.
    """
    if direction not in DIRECTIONS:
        raise ValueError(f"direction must be one of {DIRECTIONS}, not {direction!r}")
    template = get_template(policy.type)
    if template is not None:
        return template(policy, direction=direction, changes=changes)
    reason = f"{policy.type} policy {policy.name} is not translated in this version of a2m"
    return TemplateOutput((), (), {}, PolicyResult(policy.name, policy.type, Method.SKIPPED, reason))


def has_write_model(policy: Policy) -> bool:
    """True when a2m knows everything ``policy`` may write in Apigee (its documented outputs, read from its XML):
    the types with a template and OAuthV2 VerifyAccessToken. Any other policy (JavaScript, Python, JavaCallout,
    ServiceCallout, KeyValueMapOperations, the other OAuthV2 operations, an unknown type, ...) is never generated
    to run as in Apigee, so it may change anything (:meth:`RequestChanges.everything`).

    CP6 hook: an AI-translated step becomes a faithful writer only per the outputs its translation declares.
    Answer True here for it, return those declared outputs from :func:`request_changes` and
    :func:`variable_writes`, and mark the ones its processors write exactly in :attr:`TemplateOutput.written`.
    The generator reads writes only through these functions.
    """
    if policy.type == "OAuthV2":
        return _oauth_operation(policy) == "VerifyAccessToken"
    return policy.type in TEMPLATES


def request_changes(policy: Policy, *, direction: str) -> RequestChanges:
    """The request headers, query parameters and verb ``policy`` changes in Apigee when it runs on the
    ``direction`` side, by name, so a later read never sees them stale (see :mod:`a2m.conditions.variables`).

    Counted: AssignMessage and BasicAuthentication Encode, the policy types a2m
    translates that write the request; the other types with a write model
    change none. A policy without one (:func:`has_write_model`) may change
    every header, query parameter and the verb.
    """
    if not has_write_model(policy):
        every = RequestChanges.everything(policy.name)
        return RequestChanges(every.headers, every.queries, every.verb)
    if policy.type == "AssignMessage":
        return assign_message.request_changes(policy, direction=direction)
    if policy.type == "BasicAuthentication":
        return basic_auth.request_changes(policy)
    return RequestChanges()


# The proxy's own (not built-in) flow variables OAuthV2 VerifyAccessToken writes in Apigee, in lower case. Its other
# outputs (developer.*, apiproduct.*, accesstoken.*, oauthv2*) are built-in variables a2m refuses to read anyway.
# Every other OAuthV2 operation (GenerateAccessToken, RefreshAccessToken, ...) has no write model: it may write
# anything.
OAUTH_VERIFY_WRITES = frozenset(
    {
        "client_id",
        "access_token",
        "scope",
        "organization_name",
        "grant_type",
        "token_type",
        "issued_at",
        "expires_in",
        "status",
    }
)


def _oauth_operation(policy: Policy) -> str:
    operation = next((c for c in policy.settings.children if c.tag == "Operation"), None)
    return (operation.text or "").strip() if operation is not None else ""


def variable_writes(policy: Policy, *, direction: str = REQUEST) -> frozenset[str]:
    """The proxy's own flow variables and the message parts (``request.content``, ``response.content``,
    ``response.header.NAME``) ``policy`` may write in Apigee on the ``direction`` side, in lower case (``NAME.``
    for every variable under NAME), whether or not a2m carries the write over; the generator compares them with
    what the template's processors write exactly (:attr:`TemplateOutput.written`), so a later read never sees a
    write a2m dropped.

    AssignMessage, ExtractVariables and BasicAuthentication write what the policy names; VerifyAPIKey writes
    client_id and OAuthV2 VerifyAccessToken writes client_id, access_token, scope, ...; the other types with a
    template write none of the proxy's own (only their own built-in ones, such as ratelimit.*). A policy without
    a write model (:func:`has_write_model`) may write any: :data:`ANY`.
    """
    if not has_write_model(policy):
        return frozenset({ANY})
    if policy.type == "AssignMessage":
        return assign_message.variable_writes(policy, direction=direction)
    if policy.type == "ExtractVariables":
        return extract_variables.variable_writes(policy)
    if policy.type == "BasicAuthentication":
        return basic_auth.variable_writes(policy)
    if policy.type == "VerifyAPIKey":
        return verify_api_key.variable_writes(policy)
    if policy.type == "OAuthV2":
        return OAUTH_VERIFY_WRITES
    return frozenset()
