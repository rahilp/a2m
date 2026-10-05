"""good: this run's own named-header values are learned before the prompt
is masked, so even the first request that sees a credential-like header
has its literal value replaced.
"""
from __future__ import annotations

from a2m.verify.masking import Masker


def build_prompt(headers: dict[str, str], masker: Masker) -> str:
    masker.learn_named(headers)  # learned first
    prompt = f"backend headers: {headers}"
    return masker.mask(prompt)   # masked afterwards, so this run's headers are already covered
