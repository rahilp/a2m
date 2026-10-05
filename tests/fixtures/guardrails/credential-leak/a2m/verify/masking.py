"""Minimal stand-in for a2m.verify.masking.Masker: masks literal credentials by name.

A value is masked when its header, parameter or element name has a credential word (key, secret, token, password)
as its own dash- or underscore-separated part: ``<Header name="X-Partner-Api-Key">value</Header>``,
``<Password>value</Password>``, ``x-api-key: value`` or ``client_secret=value``.
"""
from __future__ import annotations

import re

_WORD = r"(?:[\w.]+[-_])*(?:key|secret|token|password)(?:[-_][\w.]+)*"
_RULES = (
    re.compile(rf'(?i)(name="{_WORD}">)(?P<value>[^<]{{4,}})(<)'),
    re.compile(rf"(?i)(<{_WORD}>)(?P<value>[^<]{{4,}})(</)"),
    re.compile(rf"(?i)(\b{_WORD}\s*[:=]\s*'?)(?P<value>[^\s'&<]{{4,}})()"),
)


class Masker:
    def mask(self, text: str) -> str:
        for rule in _RULES:
            text = rule.sub(lambda m: f"{m.group(1)}*** ({len(m.group('value'))} chars){m.group(3)}", text)
        return text

    def mask_config(self, text: str) -> str:
        return self.mask(text)
