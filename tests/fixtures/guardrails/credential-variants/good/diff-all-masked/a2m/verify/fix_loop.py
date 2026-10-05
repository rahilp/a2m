"""good: every field of the request, including the failing-test diff, is
passed through masker.mask()/mask_config() before the AiRequest is built,
so no field can reach the provider with a literal credential still in it.
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
