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
import time
from collections.abc import Callable, Mapping
from typing import Any

from a2m.ai.provider import AiRequest, ItemKind, ProviderError, ProviderLimitError, ProviderSetupError
from a2m.redaction import MASK, redact, register_secret

KEY_ENV = "ANTHROPIC_API_KEY"
MODEL_ENV = "A2M_MODEL"
DEFAULT_MODEL = "claude-opus-5-5"
# Room for a translated step and its notes; a longer answer is cut off and refused.
MAX_TOKENS = 8192
# A fix answer holds whole Mule configuration files, so it may be long.
MAX_TOKENS_BY_KIND = {ItemKind.FIX: 16384}
REQUEST_TIMEOUT_SECONDS = 300.0
MAX_RETRIES = 2
# The client's timeout must leave room for the longest answer a request may get: 16384 tokens at a slow 30 tokens a
# second take about 550 seconds. A fix request is not retried by the client: a timeout or a cut-off answer would end
# the same way again (the fix loop stops for that proxy). A rate limit or overload is waited out here instead (see
# RATE_RETRIES_BY_KIND); any other failure is one failed fix attempt, after which the fix loop asks again itself.
TIMEOUT_BY_KIND = {ItemKind.FIX: 660.0}
RETRIES_BY_KIND = {ItemKind.FIX: 0}
# With no client retries, a fix request that is refused for now (rate limit 429, overloaded 529, another 5xx, 408,
# 409, or a connection that failed without timing out) is sent again here, up to this many times, after waiting the
# time the API's retry-after asks for (or an exponential backoff), never more than MAX_BACKOFF_SECONDS a wait.
# A timeout or a cut-off answer is never sent again.
RATE_RETRIES_BY_KIND = {ItemKind.FIX: 4}
BACKOFF_SECONDS = 2.0
MAX_BACKOFF_SECONDS = 60.0
RETRY_STATUSES = frozenset({408, 409, 429})
MAX_ERROR_CHARS = 300


class ClaudeProvider:
    """Sends each prompt as one user message and returns the text of the answer."""

    def __init__(
        self,
        client: Any,
        model: str,
        error_types: tuple[type[BaseException], ...],
        key: str,
        timeout_types: tuple[type[BaseException], ...] = (),
        connection_types: tuple[type[BaseException], ...] = (),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self.model = model
        self._error_types = error_types
        self._timeout_types = timeout_types
        self._connection_types = connection_types
        self._sleep = sleep
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
        timeout_error = getattr(sdk, "APITimeoutError", None)
        timeout_types: tuple[type[BaseException], ...] = (
            (timeout_error,) if isinstance(timeout_error, type) and issubclass(timeout_error, BaseException) else ()
        )
        connection_error = getattr(sdk, "APIConnectionError", None)
        connection_types: tuple[type[BaseException], ...] = (
            (connection_error,)
            if isinstance(connection_error, type) and issubclass(connection_error, BaseException)
            else ()
        )
        return cls(client, model, error_types, key, timeout_types, connection_types)

    def complete(self, request: AiRequest) -> str:
        limit = MAX_TOKENS_BY_KIND.get(request.kind, MAX_TOKENS)
        retries = RATE_RETRIES_BY_KIND.get(request.kind, 0)
        tried = 0
        while True:
            try:
                message = self._client_for(request.kind).messages.create(
                    model=self.model,
                    max_tokens=limit,
                    messages=[{"role": "user", "content": request.prompt}],
                )
                break
            except Exception as exc:  # noqa: BLE001 (the SDK boundary: whatever it raises, the key never shows)
                text = f"Claude API error: {type(exc).__name__}: {self._shown(exc)}"
                if self._timeout_types and isinstance(exc, self._timeout_types):
                    raise ProviderLimitError(
                        f"{text} (no answer within {self._timeout(request.kind):g} seconds)"
                    ) from None
                delay = self._retry_delay(exc, tried)
                if delay is None or tried >= retries:
                    after = f" (sent {tried + 1} times)" if delay is not None and tried else ""
                    raise ProviderError(text + after) from None
                tried += 1
                self._sleep(delay)
        if getattr(message, "stop_reason", None) == "max_tokens":
            raise ProviderLimitError(f"Claude's answer was cut off at {limit} tokens")
        parts = [
            str(getattr(block, "text", ""))
            for block in getattr(message, "content", None) or ()
            if getattr(block, "type", None) == "text"
        ]
        return "".join(parts)

    def _client_for(self, kind: ItemKind) -> Any:
        """The client for a request of ``kind``: the shared one, or a copy with that kind's timeout and retries."""
        if kind not in TIMEOUT_BY_KIND and kind not in RETRIES_BY_KIND:
            return self._client
        with_options = getattr(self._client, "with_options", None)
        if not callable(with_options):
            return self._client
        return with_options(timeout=self._timeout(kind), max_retries=RETRIES_BY_KIND.get(kind, MAX_RETRIES))

    def _retry_delay(self, exc: BaseException, tried: int) -> float | None:
        """How long to wait before sending a request that failed with ``exc`` again (``tried`` times already), or
        None when sending it again would not help (see :data:`RATE_RETRIES_BY_KIND`)."""
        status = getattr(exc, "status_code", None)
        by_status = isinstance(status, int) and (status in RETRY_STATUSES or status >= 500)
        by_connection = bool(self._connection_types) and isinstance(exc, self._connection_types)
        if not (by_status or by_connection):
            return None
        asked = _retry_after(getattr(getattr(exc, "response", None), "headers", None))
        wait = asked if asked is not None else BACKOFF_SECONDS * (2**tried)
        return min(max(wait, 0.0), MAX_BACKOFF_SECONDS)

    @staticmethod
    def _timeout(kind: ItemKind) -> float:
        return TIMEOUT_BY_KIND.get(kind, REQUEST_TIMEOUT_SECONDS)

    def _shown(self, exc: BaseException) -> str:
        """The error text, short, with the key masked should the SDK ever echo it."""
        # Masked before it is cut short, so no part of the key survives the cut either.
        text = redact(" ".join(str(exc).split()))
        if self._key:
            text = text.replace(self._key, MASK)
        return text if len(text) <= MAX_ERROR_CHARS else text[:MAX_ERROR_CHARS] + "..."


def _retry_after(headers: object) -> float | None:
    """The wait in seconds the API's ``retry-after-ms`` or ``retry-after`` header asks for, or None."""
    get = getattr(headers, "get", None)
    if not callable(get):
        return None
    for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        try:
            value = get(name)
            if value is not None:
                return float(str(value).strip()) * scale
        except (TypeError, ValueError):
            continue
    return None
