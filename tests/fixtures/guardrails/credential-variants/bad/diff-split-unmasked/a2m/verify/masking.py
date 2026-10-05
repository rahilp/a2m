"""Minimal stand-in for a2m.verify.masking.Masker: masks literal credentials."""
from __future__ import annotations

import re

_CRED = re.compile(r"(?i)\b(key|secret|token|password)\s*[:=]\s*(?P<value>\S{4,})")


class Masker:
    def mask(self, text: str) -> str:
        return _CRED.sub(lambda m: m.group(0).replace(m.group("value"), "***"), text)

    def mask_config(self, text: str) -> str:
        return self.mask(text)
