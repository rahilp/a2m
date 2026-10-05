"""bad: every field of the request goes through the masker, but the masker
hides nothing (see masking.py), so the policy XML, the Mule files and the
failing-test diff reach the AI provider with their literal credentials.
"""
from __future__ import annotations

from a2m.ai.provider import AiRequest
from a2m.verify.masking import Masker


def build_request(policy_xml: str, mule_files: str, failing_diff: str, masker: Masker) -> AiRequest:
    return AiRequest(
        prompt="fix this",
        policies=masker.mask_config(policy_xml),
        mule_files=masker.mask_config(mule_files),
        diff=masker.mask(failing_diff),
    )
