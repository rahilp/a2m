"""bad: two over-eager masking rules that catch non-secret look-alikes.

(1) a name is "credential-like" whenever it merely contains one of the
credential words as a substring, so "keyword" (contains "key") is treated
like an API key name.
(2) any value that merely looks token-shaped (letters, digits and dashes
or dots, 3+ segments) is masked as if credential-shaped, so a region id
("us-east-1-production") or an api version ("2024.01.15-rc1") gets masked
even though neither is a secret.
"""
from __future__ import annotations

import re

_CRED_WORD = r"(key|secret|token|password|session)"
_NAMED = re.compile(rf'(?i)"(?P<name>[\w.-]*{_CRED_WORD}[\w.-]*)"\s*:\s*"(?P<value>[^"]{{3,}})"')
_SHAPED = re.compile(r"\b[A-Za-z0-9]+(?:[-.][A-Za-z0-9]+){2,}\b")


class Masker:
    def mask_config(self, text: str) -> str:
        def _mask_named(m: re.Match[str]) -> str:
            value = m.group("value")
            return m.group(0).replace(value, f"*** ({len(value)} chars)")

        text = _NAMED.sub(_mask_named, text)
        return _SHAPED.sub(lambda m: f"*** ({len(m.group(0))} chars)", text)
