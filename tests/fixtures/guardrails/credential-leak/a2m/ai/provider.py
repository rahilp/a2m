"""Minimal stand-in for a2m.ai.provider: just enough shape for the fixture."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AiRequest:
    kind: str
    name: str
    original: str
    prompt: str
