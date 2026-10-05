"""bad: the fix request sends the original Apigee policy XML verbatim.

The Mule files and the failing-test diff go through the masker, but the policy XML a fix is about is put into the
request's ``original`` field and into the prompt as it was read from the bundle, so a literal backend credential an
AssignMessage sets (``<Header name="X-Partner-Api-Key">...</Header>``) reaches the AI provider unmasked.
"""
from __future__ import annotations

from a2m.ai.provider import AiRequest
from a2m.verify.masking import Masker


def build_request(policy_xml: str, mule_files: str, failing_diff: str, masker: Masker) -> AiRequest:
    mule = masker.mask_config(mule_files)
    diff = masker.mask(failing_diff)
    prompt = f"Policies:\n{policy_xml}\n\nMule files:\n{mule}\n\nFailing tests:\n{diff}\n"
    return AiRequest(kind="fix", name="orders-api", original=policy_xml, prompt=prompt)
