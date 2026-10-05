"""bad: the prompt is masked before this run's own named-header values are
learned, so a credential-like header's literal value is sent unmasked on
the first request that ever sees it; a later request would be covered.
"""
from __future__ import annotations

from a2m.verify.masking import Masker


def build_prompt(headers: dict[str, str], masker: Masker) -> str:
    prompt = f"backend headers: {headers}"
    masked_prompt = masker.mask(prompt)   # masked using only what was already learned
    masker.learn_named(headers)           # learned afterwards, too late for this prompt
    return masked_prompt
