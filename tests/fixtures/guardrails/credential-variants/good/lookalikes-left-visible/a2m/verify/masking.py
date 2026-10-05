"""good: a credential word must be its own dash/underscore-separated
segment of the name (not a bare substring), and no value is masked by
shape alone - so "keyword", "region" and "api-version" stay visible while
"x-partner-api-key" is masked.
"""
from __future__ import annotations

import re

_CRED_WORD = r"(key|secret|token|password|session)"
_NAMED = re.compile(
    rf'(?i)"(?P<name>(?:[\w]+[-_])*{_CRED_WORD}(?:[-_][\w]+)*)"\s*:\s*"(?P<value>[^"]{{3,}})"'
)


class Masker:
    def mask_config(self, text: str) -> str:
        def _mask(m: re.Match[str]) -> str:
            value = m.group("value")
            return m.group(0).replace(value, f"*** ({len(value)} chars)")

        return _NAMED.sub(_mask, text)
