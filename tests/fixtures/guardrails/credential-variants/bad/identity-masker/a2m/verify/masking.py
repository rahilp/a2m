"""bad: a Masker whose masking regressed to returning the text unchanged.

It has the methods the request builder calls, and it leaves every harmless
look-alike visible (it changes nothing at all), so a check that asks this
masker which credentials it hides, or that only looks for over-masking,
finds nothing wrong.
"""
from __future__ import annotations


class Masker:
    def mask(self, text: str) -> str:
        return text

    def mask_config(self, text: str) -> str:
        return text
