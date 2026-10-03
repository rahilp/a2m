"""The policy registry: one template per Apigee policy type, and an explicit result for every other type.

Mirrors PolicyTransformationFactory.cs in the Azure tool, except that an
unknown type is never translated into nothing: :func:`translate` returns a
``skipped`` result with a reason, and the generator lists it as unsupported.
"""

from __future__ import annotations

from collections.abc import Callable

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


def translate(policy: Policy, *, direction: str) -> TemplateOutput:
    """``policy`` through its template; a type without one comes back skipped, with a reason."""
    if direction not in DIRECTIONS:
        raise ValueError(f"direction must be one of {DIRECTIONS}, not {direction!r}")
    template = get_template(policy.type)
    if template is not None:
        return template(policy, direction=direction)
    reason = f"{policy.type} policy {policy.name} is not translated in this version of a2m"
    return TemplateOutput((), (), {}, PolicyResult(policy.name, policy.type, Method.SKIPPED, reason))
