"""Golden recordings: Apigee exchanges saved as JSON, replayed against the Mule app with --golden.

The --golden folder has one sub-folder per proxy name; each ``*.json`` file in
it is one exchange, replayed in file-name order::

    {"name": "valid-key",
     "calls": [{"after_ms": 0,
                "request": {"method": "GET", "path": "/orders/7", "headers": {...}, "body": ""},
                "response": {"status": 200, "headers": {...}, "body": "{\\"id\\":7}"}}],
     "backend_calls": [{"method": "GET", "path": "/orders/7", "headers": {"X-Client": "a2m"}, "body": "",
                        "response": {"status": 200, "headers": {...}, "body": "..."}}]}

``after_ms`` is how long after the previous call the call was made; the
mock backend answers each recorded backend call with the response recorded
for it. Each backend call the app makes is compared with the recorded one in
full: method, path with its query string (a path without ``?`` means no query
was sent), the request ``body`` (empty when not given) and the headers. A
header the backend received that the recording does not list is a mismatch
too, except the ignored ones, the local client defaults, and a header the
client request sent (same value) that no request step of the proxy changes:
Apigee forwards those by default, so a recording need not repeat them. On a
response, a header the app answered with that the recording does not list is
a mismatch when a response step of the proxy changes it (or may change any
header). A file that cannot be read or does not have this shape is an error
naming the file; nothing is guessed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from a2m.verify import masking
from a2m.verify.model import HttpRequest, HttpResponse, shout

MAX_RECORDING_BYTES = 10 * 1024 * 1024
MAX_AFTER_MS = 600_000
MAX_SHOWN_PATH = 60
# An HTTP header name (RFC 9110 token); another name is not echoed in an error.
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}")


class RecordingError(ValueError):
    """A recording file cannot be used; the message names the file."""


@dataclass(frozen=True, slots=True)
class RecordedCall:
    after_ms: int
    request: HttpRequest
    response: HttpResponse


@dataclass(frozen=True, slots=True)
class RecordedBackendCall:
    """A call the backend received in the recording; ``path`` includes its query string, ``body`` is the request's."""

    method: str
    path: str
    headers: Mapping[str, str]
    response: HttpResponse
    body: bytes = b""


@dataclass(frozen=True, slots=True)
class Exchange:
    name: str
    file: str
    calls: tuple[RecordedCall, ...]
    backend_calls: tuple[RecordedBackendCall, ...]


def recordings_dir(golden: Path, proxy: str) -> Path:
    return golden / proxy


def load_exchanges(golden: Path, proxy: str) -> tuple[Exchange, ...] | None:
    """The recorded exchanges of ``proxy`` in file-name order, None when it has none; raises RecordingError."""
    folder = recordings_dir(golden, proxy)
    if not folder.is_dir():
        return None
    files = sorted(p for p in folder.iterdir() if p.suffix == ".json" and p.is_file())
    if not files:
        return None
    return tuple(_load(path) for path in files)


def _load(path: Path) -> Exchange:
    try:
        if path.stat().st_size > MAX_RECORDING_BYTES:
            raise RecordingError(f"recording {path.name} is larger than {MAX_RECORDING_BYTES} bytes")
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise RecordingError(f"recording {path.name} cannot be read: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RecordingError(f"recording {path.name} is not valid JSON: {exc}") from exc
    try:
        return _exchange(data, path.name)
    except (KeyError, TypeError, ValueError) as exc:
        raise RecordingError(f"recording {path.name} does not have a2m's golden format: {exc}") from exc


def _exchange(data: Any, file: str) -> Exchange:
    obj = _object(data, "the file")
    name = obj.get("name", Path(file).stem)
    if not isinstance(name, str):
        raise TypeError("'name' must be a string")
    if not name.strip():
        raise ValueError("'name' must not be empty")
    calls_data = obj["calls"]
    if not isinstance(calls_data, list) or not calls_data:
        raise ValueError("'calls' must be a non-empty list")
    calls = tuple(_call(item, index) for index, item in enumerate(calls_data, 1))
    backend_data = obj.get("backend_calls", [])
    if not isinstance(backend_data, list):
        raise TypeError("'backend_calls' must be a list")
    backend = tuple(_backend_call(item, index) for index, item in enumerate(backend_data, 1))
    return Exchange(name, file, calls, backend)


def _call(data: Any, index: int) -> RecordedCall:
    obj = _object(data, f"call {index}")
    after = obj.get("after_ms", 0)
    if not isinstance(after, int) or isinstance(after, bool) or not 0 <= after <= MAX_AFTER_MS:
        raise ValueError(f"call {index}: 'after_ms' must be a whole number from 0 to {MAX_AFTER_MS}")
    req = _object(obj["request"], f"call {index} request")
    request = HttpRequest(
        method=shout(_string(req["method"], "method")),
        path=_path(req["path"]),
        headers=_headers(req.get("headers", {})),
        body=_string(req.get("body", ""), "body").encode("utf-8"),
    )
    return RecordedCall(after, request, _response(obj["response"], f"call {index} response"))


def _backend_call(data: Any, index: int) -> RecordedBackendCall:
    obj = _object(data, f"backend call {index}")
    response = obj.get("response", {"status": 200})
    return RecordedBackendCall(
        method=shout(_string(obj["method"], "method")),
        path=_path(obj["path"]),
        headers=_headers(obj.get("headers", {})),
        response=_response(response, f"backend call {index} response"),
        body=_string(obj.get("body", ""), "body").encode("utf-8"),
    )


def _response(data: Any, what: str) -> HttpResponse:
    obj = _object(data, what)
    status = obj["status"]
    if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599:
        raise ValueError(f"{what}: 'status' must be an HTTP status number")
    return HttpResponse(status, _headers(obj.get("headers", {})), _string(obj.get("body", ""), "body").encode("utf-8"))


def _object(data: Any, what: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise TypeError(f"{what} must be a JSON object")
    return data


def _string(value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"'{what}' must be a string")
    return value


def _path(value: Any) -> str:
    path = _string(value, "path")
    if not path.startswith("/") or any(ch in path for ch in "\r\n "):
        raise ValueError(
            f"'path' must start with / and hold no spaces or line breaks, got {_shown_path(path)} "
            "(the query and anything after a blank are not shown: they may hold a credential)"
        )
    return path


def _shown_path(path: str) -> str:
    """How a bad recorded path is named in an error: its path part only, cut short and masked.

    The query string and anything after a blank or line break are left out (an API key may be there, and the
    masker has not learned the recording's values yet), and what is left goes through the active masker.
    """
    head = re.split(r"[?#\s]", path, maxsplit=1)[0]
    if len(head) > MAX_SHOWN_PATH:
        head = head[:MAX_SHOWN_PATH] + "..."
    return repr(masking.current().mask(head))


def _headers(value: Any) -> dict[str, str]:
    obj = _object(value, "'headers'")
    out: dict[str, str] = {}
    for name, item in obj.items():
        if not isinstance(item, str) or any(ch in name + item for ch in "\r\n"):
            shown = repr(name) if _HEADER_NAME.fullmatch(name) else "with a name that is not an HTTP header name"
            raise ValueError(f"header {shown} must have a one-line string value")
        out[str(name)] = item
    return out
