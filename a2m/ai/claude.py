"""The Claude provider behind ``--llm claude`` (the default): the Anthropic Messages API.

The Anthropic SDK is an optional extra (``pip install 'a2m[claude]'``) and is
imported only here, inside :meth:`ClaudeProvider.from_environment`, so
``import a2m`` and ``--llm fake`` runs never need it. The API key comes from
``ANTHROPIC_API_KEY`` only (unset, empty or blank counts as missing) and is
never logged or written anywhere: it is registered with :mod:`a2m.redaction`,
so every run.log line, file and terminal line masks it, and any exception the
SDK raises (of whatever type) becomes a :class:`ProviderError` whose text is
masked too. The model is ``A2M_MODEL``, or :data:`DEFAULT_MODEL` when that is
unset or blank.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Mapping
from typing import Any

from a2m.ai.provider import AiRequest, ProviderError, ProviderSetupError
from a2m.redaction import MASK, redact, register_secret

KEY_ENV = "ANTHROPIC_API_KEY"
MODEL_ENV = "A2M_MODEL"
DEFAULT_MODEL = "claude-opus-5-5"
# Room for a translated step and its notes; a longer answer is cut off and refused.
MAX_TOKENS = 8192
REQUEST_TIMEOUT_SECONDS = 300.0
MAX_RETRIES = 2
MAX_ERROR_CHARS = 300


class ClaudeProvider:
    """Sends each prompt as one user message and returns the text of the answer."""

    def __init__(self, client: Any, model: str, error_types: tuple[type[BaseException], ...], key: str) -> None:
        self._client = client
        self.model = model
        self._error_types = error_types
        self._key = key
        register_secret(key)

    def __repr__(self) -> str:  # never show the client (or the key it holds)
        return f"ClaudeProvider(model={self.model!r})"

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> ClaudeProvider:
        """A provider for the key and model in ``environ`` (default: the process environment).

        Raises :class:`ProviderSetupError` when the key is missing or the SDK is
        not installed, before any client is built.
        """
        env = os.environ if environ is None else environ
        key = env.get(KEY_ENV, "").strip()
        if not key:
            raise ProviderSetupError(
                f"{KEY_ENV} is not set (or is empty); set it to use --llm claude (the default), "
                "or run with --llm fake to migrate without an AI provider"
            )
        try:
            sdk = importlib.import_module("anthropic")
        except ImportError:
            raise ProviderSetupError(
                "the Anthropic SDK (Python package anthropic) is not installed; install it with "
                "pip install 'a2m[claude]', or run with --llm fake to migrate without an AI provider"
            ) from None
        model = env.get(MODEL_ENV, "").strip() or DEFAULT_MODEL
        client = sdk.Anthropic(api_key=key, timeout=REQUEST_TIMEOUT_SECONDS, max_retries=MAX_RETRIES)
        api_error = getattr(sdk, "APIError", None)
        error_types: tuple[type[BaseException], ...] = (
            (api_error,) if isinstance(api_error, type) and issubclass(api_error, BaseException) else ()
        )
        return cls(client, model, error_types, key)

    def complete(self, request: AiRequest) -> str:
        try:
            message = self._client.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                messages=[{"role": "user", "content": request.prompt}],
            )
        except Exception as exc:  # noqa: BLE001 (the SDK boundary: whatever it raises, the key never shows)
            raise ProviderError(f"Claude API error: {type(exc).__name__}: {self._shown(exc)}") from None
        if getattr(message, "stop_reason", None) == "max_tokens":
            raise ProviderError(f"Claude's answer was cut off at {MAX_TOKENS} tokens")
        parts = [
            str(getattr(block, "text", ""))
            for block in getattr(message, "content", None) or ()
            if getattr(block, "type", None) == "text"
        ]
        return "".join(parts)

    def _shown(self, exc: BaseException) -> str:
        """The error text, short, with the key masked should the SDK ever echo it."""
        # Masked before it is cut short, so no part of the key survives the cut either.
        text = redact(" ".join(str(exc).split()))
        if self._key:
            text = text.replace(self._key, MASK)
        return text if len(text) <= MAX_ERROR_CHARS else text[:MAX_ERROR_CHARS] + "..."
