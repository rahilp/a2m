"""The one interface a2m's AI translation talks to, and the vocabulary its results use.

A provider is any object with ``complete(request) -> str``: it gets an
:class:`AiRequest` (the item's kind and name, its original code or condition
text, and the whole prompt) and returns the model's raw answer text. a2m never
trusts that text: :mod:`a2m.ai.translate` validates every answer before any of
it reaches the generated project. Any exception ``complete`` raises is a
provider error for that one item; the other items carry on.

The implementations are :class:`a2m.ai.fake.FakeProvider` (canned answers, no
network) and :class:`a2m.ai.claude.ClaudeProvider` (the Anthropic API, which
imports the SDK lazily). :func:`a2m.ai.make_provider` picks one by ``--llm``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from a2m.errors import A2mError


class Confidence(StrEnum):
    """How sure the AI is that its translation behaves like the original."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ItemKind(StrEnum):
    """What is sent to the AI: a callout's code (by language), a condition, or a fix for a proxy whose tests failed
    (:mod:`a2m.verify.fix_loop`)."""

    JAVASCRIPT = "javascript"
    PYTHON = "python"
    JAVA = "java"
    EXPRESSION = "expression"
    FIX = "fix"


@dataclass(frozen=True, slots=True)
class AiRequest:
    """One item for the AI: ``name`` is the step (policy) name, the Flow, step or RouteRule holding a condition, or
    the proxy's name for a fix; ``original`` is the callout's source code, the condition text, or the Apigee policies
    a fix is about, verbatim; ``prompt`` is the whole text sent."""

    kind: ItemKind
    name: str
    original: str
    prompt: str


class Provider(Protocol):
    """Anything that answers an :class:`AiRequest` with the model's raw text."""

    def complete(self, request: AiRequest) -> str: ...


class ProviderError(A2mError):
    """An AI call failed (network, API or answer error); only that item is affected."""


class ProviderLimitError(ProviderError):
    """The answer could not be complete: it was cut off at the token limit or the request timed out. Sending the same
    request again would end the same way, so a caller that would ask again (the fix loop) stops instead."""


class ProviderSetupError(A2mError):
    """The chosen provider cannot be used at all (no API key, SDK not installed); nothing is processed."""
