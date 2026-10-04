"""Compare an expected HTTP response (recorded or predicted) with what the app answered, as a readable diff.

Expected headers are a subset by default: every expected header must be there
with the same value, extra live headers are fine. With ``exact=True`` a live
header the expectation does not name is a mismatch too ("expected absent"),
unless it is ignored or tolerated. Header names are compared without case. Headers that always change between two runs (DEFAULT_IGNORED_HEADERS,
configurable) are never compared. A Content-Type is compared by its media
type without case and by its parameters (:func:`same_content_type`): every
parameter the expectation names (a multipart boundary, say) must be there with
the same value; names are compared without case, values exactly except the
charset's, and a quoted value equals the unquoted one. A charset only one side
names must be UTF-8 or US-ASCII, the defaults that read ASCII bytes the same
(``application/json; charset=UTF-8`` matches ``application/json``, and
``charset=ISO-8859-1`` does not).
Bodies that both parse as JSON are compared as JSON (key order and spacing do
not matter) and each differing field is named; other bodies are compared as
text. Every line of the diff names the part that differs with both values.

The calls a backend received are compared in full (:func:`backend_call_diffs`):
the method, the path as sent (only an encoded unreserved character such as
``%7E`` equals the character; ``%2F`` is not ``/``) and the whole query string (a recorded path without
``?`` means no query was sent; parameters are compared as a multiset, so their
order does not matter), the request body (JSON-aware; an empty or blank body
equals an empty one) and the recorded headers minus BACKEND_IGNORED_HEADERS,
which add Host and the other hop-by-hop or forwarding headers that a2m's
redirect of every target to the mock backend changes on purpose. With
``exact=True`` (the harness's golden replay) a header the backend received
that the recording does not list (an API key Apigee removed, say) is a
mismatch, except the ones the caller tolerates: the harness tolerates the
defaults the local runtime's HTTP client and its own client add when the
request has none (LOCAL_CLIENT_DEFAULTS, with exactly those values) and the
client's own headers Apigee forwards by default (see a2m.verify.harness).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl

from a2m.verify.masking import mask_shown
from a2m.verify.model import HttpResponse, fold

# Headers whose values differ on every run or between Apigee and Mule, so a golden replay never compares them.
DEFAULT_IGNORED_HEADERS: tuple[str, ...] = (
    "Date",
    "Server",
    "Content-Length",
    "Transfer-Encoding",
    "Connection",
    "X-Request-ID",
    "X-Correlation-ID",
    "Keep-Alive",
)
# Headers of a call to the backend that differ because a2m points every target at a local mock backend
# (Host) or because they belong to the hop between proxy and backend, so a golden replay never compares them.
BACKEND_IGNORED_HEADERS: tuple[str, ...] = (
    *DEFAULT_IGNORED_HEADERS,
    "Host",
    "TE",
    "Upgrade",
    "Proxy-Connection",
    "Via",
    "Forwarded",
    "X-Forwarded-For",
    "X-Forwarded-Host",
    "X-Forwarded-Port",
    "X-Forwarded-Proto",
)
# What the local Mule runtime's HTTP requester (User-Agent, Accept) and the harness's own HTTP client
# (Accept-Encoding) add to a request that has none; on a backend call with exactly this value, such a header
# comes from the local test set-up, not from the app, so an exact comparison does not count it as extra.
LOCAL_CLIENT_DEFAULTS: dict[str, str] = {
    "user-agent": "AHC/1.0",
    "accept": "*/*",
    "accept-encoding": "identity",
}
MAX_SHOWN = 200
MAX_FIELD_DIFFS = 20
_MISSING = object()


@dataclass(frozen=True, slots=True)
class Comparison:
    matched: bool
    diff: str


def shown(value: object) -> str:
    """``value`` for a diff line: quoted text, cut short when long.

    Credentials in it are masked first, on the whole value (:func:`a2m.verify.masking.mask_shown`, with the
    masker of the run in progress), so cutting it short never leaves a piece of one unmasked.
    """
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    text = mask_shown(text)
    if len(text) > MAX_SHOWN:
        text = text[:MAX_SHOWN] + "..."
    return repr(text) if isinstance(value, str) else text


def lower_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Header names in lower case (a repeated name keeps its values joined with ', ')."""
    found: dict[str, str] = {}
    for name, value in dict(headers).items():
        key = fold(str(name))
        found[key] = f"{found[key]}, {value}" if key in found else str(value)
    return found


def header_diffs(
    expected: Mapping[str, str | None],
    actual: Mapping[str, str],
    ignore: Iterable[str] = (),
    where: str = "header",
    *,
    exact: bool = False,
    tolerated: Mapping[str, Collection[str]] | None = None,
) -> list[str]:
    """Diff lines for each expected header that is missing or different (None: must be absent).

    With ``exact``, each live header that ``expected`` does not name is a line too ("expected absent"),
    unless it is ignored or ``tolerated`` holds its name (lower case) with exactly its value among the values.
    """
    ignored = {fold(name) for name in ignore}
    live = lower_headers(actual)
    lines: list[str] = []
    if exact:
        named = {fold(str(name)) for name in dict(expected)}
        allowed = dict(tolerated or {})
        for key in sorted(live):
            if key in named or key in ignored or live[key] in allowed.get(key, ()):
                continue
            lines.append(f"{where} {key}: expected absent, actual {shown(live[key])}")
    for name, value in dict(expected).items():
        key = fold(str(name))
        if key in ignored:
            continue
        got = live.get(key)
        if value is None:
            if got is not None:
                lines.append(f"{where} {name}: expected absent, actual {shown(got)}")
            continue
        if got is None:
            lines.append(f"{where} {name}: expected {shown(value)}, actual missing")
        elif not _same_header(key, str(value), got):
            lines.append(f"{where} {name}: expected {shown(str(value))}, actual {shown(got)}")
    return lines


def _same_header(key: str, expected: str, actual: str) -> bool:
    if key == "content-type":
        return same_content_type(expected, actual)
    return expected == actual


# A charset that a Content-Type without one may stand for: both read ASCII bytes the same (RFC 8259 makes
# UTF-8 JSON's only encoding, RFC 2046 makes US-ASCII text's default).
_DEFAULT_CHARSETS = frozenset({"utf-8", "us-ascii"})
_PARAMETER = re.compile(r';\s*([^=;\s]+)\s*=\s*("(?:[^"\\]|\\.)*"|[^;]*)')


def same_content_type(expected: str, actual: str) -> bool:
    """Whether the live Content-Type ``actual`` says what ``expected`` says.

    The media types match without case. Every parameter of ``expected`` is in ``actual`` with the same
    value (names without case; a quoted value equals the unquoted one; a charset's value without case),
    because a parameter such as a multipart boundary or a charset changes how a client reads the body.
    A charset only one side names must be UTF-8 or US-ASCII. Other parameters only ``actual`` has are fine.
    """
    want_type, want = _content_type(expected)
    got_type, got = _content_type(actual)
    if want_type != got_type:
        return False
    for name, value in want.items():
        if name == "charset" and name not in got:
            if value not in _DEFAULT_CHARSETS:
                return False
        elif got.get(name) != value:
            return False
    return "charset" in want or got.get("charset", "utf-8") in _DEFAULT_CHARSETS


def _content_type(value: str) -> tuple[str, dict[str, str]]:
    """The media type in lower case and the parameters (lower-case names, unquoted values; charset folded)."""
    media, _, rest = value.partition(";")
    parameters: dict[str, str] = {}
    for match in _PARAMETER.finditer(";" + rest):
        name, raw = fold(match.group(1)), match.group(2).strip()
        if len(raw) >= 2 and raw[0] == raw[-1] == '"':
            raw = re.sub(r"\\(.)", r"\1", raw[1:-1])
        parameters.setdefault(name, fold(raw) if name == "charset" else raw)
    return fold(media.strip()), parameters


def body_diffs(expected: bytes, actual: bytes, where: str = "body") -> list[str]:
    """Diff lines for two bodies: per JSON field when both are JSON, else the two texts."""
    if expected == actual:
        return []
    left, right = _json(expected), _json(actual)
    if left is _MISSING or right is _MISSING:
        return [f"{where}: expected {shown(_text(expected))}, actual {shown(_text(actual))}"]
    found: list[tuple[str, str]] = []
    _json_diffs(left, right, "", found)
    lines = [f"{where} field {path}: {pair}" if path else f"{where}: {pair}" for path, pair in found]
    if len(lines) > MAX_FIELD_DIFFS:
        lines = [*lines[:MAX_FIELD_DIFFS], f"{where}: {len(lines) - MAX_FIELD_DIFFS} more fields differ"]
    return lines


def _json(body: bytes) -> Any:
    if not body.strip():
        return _MISSING
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _MISSING


def _text(body: bytes) -> str:
    return body.decode("utf-8", errors="replace")


def _json_diffs(expected: Any, actual: Any, path: str, found: list[tuple[str, str]]) -> None:
    """Append (field path, "expected X, actual Y") for every JSON value that differs."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(set(expected) | set(actual)):
            sub = f"{path}.{key}" if path else str(key)
            if key not in actual:
                found.append((sub, f"expected {shown(expected[key])}, actual missing"))
            elif key not in expected:
                found.append((sub, f"expected absent, actual {shown(actual[key])}"))
            else:
                _json_diffs(expected[key], actual[key], sub, found)
        return
    if isinstance(expected, list) and isinstance(actual, list) and len(expected) == len(actual):
        for index, (left, right) in enumerate(zip(expected, actual, strict=True)):
            _json_diffs(left, right, f"{path}[{index}]", found)
        return
    if expected != actual or type(expected) is not type(actual):
        found.append((path, f"expected {shown(expected)}, actual {shown(actual)}"))


def compare_response(
    expected: HttpResponse,
    actual: HttpResponse,
    *,
    ignore_headers: Sequence[str] | None = None,
    exact_headers: bool = False,
) -> Comparison:
    """Compare status, headers (minus ``ignore_headers``) and body; an empty diff when equal.

    Only the expected headers are compared, unless ``exact_headers``: then a live header ``expected``
    does not have is a mismatch too.
    """
    ignore = DEFAULT_IGNORED_HEADERS if ignore_headers is None else tuple(ignore_headers)
    lines: list[str] = []
    if expected.status != actual.status:
        lines.append(f"status: expected {expected.status}, actual {actual.status}")
    lines += header_diffs(expected.headers, actual.headers, ignore, exact=exact_headers)
    lines += body_diffs(expected.body, actual.body)
    return Comparison(matched=not lines, diff="\n".join(lines))


def target_diffs(
    expected_method: str, expected_target: str, method: str, path: str, query: str, *, where: str
) -> list[str]:
    """Diff lines for a backend call's method, path and query (``expected_target`` is path plus any ?query)."""
    want_path, _, want_query = expected_target.partition("?")
    want_params = parse_qsl(want_query, keep_blank_values=True)
    got_params = parse_qsl(query, keep_blank_values=True)
    same = (
        expected_method == method
        and normal_path(want_path) == normal_path(path)
        and Counter(want_params) == Counter(got_params)
    )
    if same:
        return []
    got_target = path + (f"?{query}" if query else "")
    lines = [f"{where}: expected {expected_method} {expected_target}, got {method} {got_target}"]
    for name in sorted({k for k, _ in want_params} | {k for k, _ in got_params}):
        wanted = sorted(v for k, v in want_params if k == name)
        got = sorted(v for k, v in got_params if k == name)
        if wanted == got:
            continue
        lines.append(
            f"{where} query {name}: expected {shown(_one(wanted)) if wanted else 'absent'}, "
            f"actual {shown(_one(got)) if got else 'absent'}"
        )
    return lines


# RFC 3986 section 2.3: characters whose percent-encoding means the same as the character itself.
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_PERCENT = re.compile(r"%([0-9A-Fa-f]{2})")


def normal_path(path: str) -> str:
    """``path`` as sent, with only the normalisations that keep its meaning (RFC 3986 section 6.2.2).

    A percent-encoded unreserved character is decoded (``%7E`` is ``~``) and the hex digits of every other
    percent-encoding are upper-cased (``%2f`` is ``%2F``). A reserved character stays encoded: ``/orders%2F7``
    names one segment and ``/orders/7`` two, so they differ.
    """

    def one(match: re.Match[str]) -> str:
        code = int(match.group(1), 16)
        return chr(code) if chr(code) in _UNRESERVED else f"%{code:02X}"

    return _PERCENT.sub(one, path)


def _one(values: list[str]) -> str | list[str]:
    return values[0] if len(values) == 1 else values


def request_body_diffs(expected: bytes, actual: bytes, *, where: str) -> list[str]:
    """Diff lines for the body a backend received: JSON-aware, and blank equals empty."""
    if not expected.strip() and not actual.strip():
        return []
    return body_diffs(expected, actual, where=where)
