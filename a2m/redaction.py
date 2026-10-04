"""Mask secrets in every string a2m writes out: run.log lines, files, terminal output, error text.

A secret is any value registered with :func:`register_secret` (the claude
provider registers its API key when it is created) and the value of each
environment variable in :data:`SECRET_ENV_VARS`. :func:`redact` replaces every
occurrence of each with :data:`MASK`, whatever produced the text (an SDK
exception of any type, a traceback, a model's answer). The places that write
text out call it: the run.log formatter, the terminal writer, every file write
in :mod:`a2m.safefs`, and the AI layer's error text, so the masking never
depends on how a message was built.

Values shorter than :data:`MIN_SECRET_CHARS` are never treated as secrets:
masking a one-letter "key" would garble every file a2m writes.
"""

from __future__ import annotations

import os

SECRET_ENV_VARS = ("ANTHROPIC_API_KEY",)
MIN_SECRET_CHARS = 8
MASK = "***"

_registered: set[str] = set()


def register_secret(value: str) -> None:
    """Mask ``value`` (whitespace at the ends removed) in everything a2m writes from now on."""
    secret = value.strip()
    if len(secret) >= MIN_SECRET_CHARS:
        _registered.add(secret)


def _secrets() -> list[str]:
    found = set(_registered)
    for name in SECRET_ENV_VARS:
        value = os.environ.get(name, "").strip()
        if len(value) >= MIN_SECRET_CHARS:
            found.add(value)
    # The longest first, so a secret holding another one is masked whole.
    return sorted(found, key=lambda secret: (-len(secret), secret))


def redact(text: str) -> str:
    """``text`` with every secret replaced by :data:`MASK`."""
    for secret in _secrets():
        if secret in text:
            text = text.replace(secret, MASK)
    return text
