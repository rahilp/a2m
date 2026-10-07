"""AI translation of custom code (JavaScript, Python, Java callouts) and conditions a2m cannot translate itself.

Everything AI goes through one :class:`Provider` (``complete(request) -> str``),
picked by ``--llm`` with :func:`make_provider`. Nothing here imports the
Anthropic SDK at import time; only :class:`a2m.ai.claude.ClaudeProvider` does,
when it is created.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from a2m.ai.fake import FAKE_ANSWERS_ENV, FakeProvider
from a2m.ai.provider import AiRequest, Confidence, ItemKind, Provider, ProviderError, ProviderSetupError

CLAUDE = "claude"
FAKE = "fake"
# --llm none: no provider at all (the engine never calls make_provider for it).
NO_AI = "none"
# Why an item only the AI could translate was skipped in a --llm none run.
AI_TURNED_OFF = "AI is turned off (--llm none)"

__all__ = [
    "AI_TURNED_OFF",
    "CLAUDE",
    "FAKE",
    "NO_AI",
    "AiRequest",
    "Confidence",
    "FakeProvider",
    "ItemKind",
    "Provider",
    "ProviderError",
    "ProviderSetupError",
    "make_provider",
]


def make_provider(choice: str, environ: Mapping[str, str] | None = None) -> Provider:
    """The provider for ``--llm choice``; :class:`ProviderSetupError` when it cannot be used (checked up front, so a
    run stops before processing anything)."""
    env = os.environ if environ is None else environ
    if choice == FAKE:
        folder = env.get(FAKE_ANSWERS_ENV, "")
        return FakeProvider(Path(folder) if folder.strip() else None)
    if choice == CLAUDE:
        from a2m.ai.claude import ClaudeProvider

        return ClaudeProvider.from_environment(env)
    raise ProviderSetupError(f"unknown AI provider {choice!r}; choose {CLAUDE} or {FAKE} ({NO_AI} has no provider)")
