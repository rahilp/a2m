"""bad: builds the AI request with the failing-test diff left unmasked.

mask_config() is applied to the policy text and the Mule files, but the
diff is assembled afterwards, straight from the raw test-failure text,
under a field renamed `patch_summary` so it doesn't read next to a "mask"
call. The credential a failing test echoes never passes through
masker.mask().
"""
from __future__ import annotations

from a2m.ai.provider import AiRequest
from a2m.verify.masking import Masker


def build_request(policy_xml: str, mule_files: str, failing_diff: str, masker: Masker) -> AiRequest:
    masked_policies = masker.mask_config(policy_xml)
    masked_mule = masker.mask_config(mule_files)
    # Assembled afterwards, straight from the raw test-failure text:
    patch_summary = failing_diff
    return AiRequest(
        prompt="fix this", policies=masked_policies, mule_files=masked_mule, patch_summary=patch_summary,
    )
