"""Mask credentials in what the verification harness writes: test diffs, run.log lines, verification.json.

A golden replay or a battery case can show a value the client sent, the
backend received or the app answered. When that value is a credential (an API
key a VerifyAPIKey reads, an Authorization or Proxy-Authorization header, a
cookie) it is shown masked: its first :data:`PREFIX_CHARS` characters (only for
values of at least :data:`PREFIX_MIN_CHARS` characters) and its length, e.g.
``'PROD*** (12 chars)'``. Whatever :mod:`a2m.redaction` masks is masked too.

The masker learns the credential values from the requests, responses and
backend calls of the run (by header and query parameter name), then replaces
every occurrence of each value in a line, wherever the line shows it (a header
line, a query string, a body). It learns the secrets inside a header, not only
the whole header: each cookie value of a Cookie or Set-Cookie header and the
credential after an Authorization scheme (for Basic also the decoded user and
password), so a session token a body echoes is masked too. Values shorter than :data:`MIN_MASKED_CHARS`
are not replaced: masking a two-letter value would garble unrelated text.

Credential-shaped values are masked even when they were never learned, at any
length: the token after an ``Authorization`` scheme (``Bearer``, ``Basic``; the
parameters of a challenge such as ``Basic realm="orders"`` are not a token), a
JSON Web Token, and the value after a credential name (Authorization,
Proxy-Authorization, Cookie, Set-Cookie, or a header or query parameter a
VerifyAPIKey reads) written as ``name: value``, ``name=value`` or
``"name": "value"`` (up to the next quote, comma, bracket, ``&`` or line end).
Text that is already masked is left as it is.

One funnel: :meth:`Masker.active` makes a masker the current one, and
:func:`mask_shown` (which :func:`a2m.verify.compare.shown` calls on the whole
value before it cuts a long value short) masks with it, so no cut-off piece of
a credential reaches a diff. :meth:`Masker.mask_result` masks every text of a
:class:`~a2m.verify.model.VerificationResult` (message, log excerpt, diffs,
untested and review reasons) and :meth:`Masker.logging` masks every run.log
line written while it is active.
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from urllib.parse import parse_qsl, quote, quote_plus

from a2m import redaction
from a2m.ir import Bundle
from a2m.runlog import get_logger
from a2m.verify.model import CaseResult, HttpRequest, ReviewFlag, UntestedPolicy, VerificationResult, fold

# Header names whose values are always credentials (lower case).
SECRET_HEADERS: tuple[str, ...] = ("authorization", "proxy-authorization", "cookie", "set-cookie")
MIN_MASKED_CHARS = 4
PREFIX_CHARS = 4
PREFIX_MIN_CHARS = 8
KEY_POLICY_TYPE = "VerifyAPIKey"
# Authorization schemes whose token is a credential, wherever the text shows "<scheme> <token>".
SECRET_SCHEMES: tuple[str, ...] = ("Bearer", "Basic")
_TOKEN = r"[A-Za-z0-9\-._~+/]"
# The token of a scheme; it must end where the token ends (not before a '*' of an already masked value).
# A challenge's parameters (WWW-Authenticate: Basic realm="orders") are not a token.
_CHALLENGE_PARAMS = r"(?:realm|charset|scope|error|error_description|error_uri|nonce|opaque|qop|stale|algorithm|domain)"
_SCHEME_RULE = re.compile(
    rf"(?i)\b(?P<head>(?:{'|'.join(SECRET_SCHEMES)})\s+)(?!{_CHALLENGE_PARAMS}\s*=)"
    rf"(?P<value>{_TOKEN}{{{MIN_MASKED_CHARS},}}=*)(?![A-Za-z0-9\-._~+/=*])"
)
_JWT_RULE = re.compile(r"(?P<value>eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*)(?![A-Za-z0-9_*-])")
# What ends the value after a credential name; a value holding '*' is already masked and is left as it is.
_VALUE_END = r"\r\n\"'<>,{}\[\]()&"
_NAMED_VALUE = rf"(?P<value>[^{_VALUE_END}*\s][^{_VALUE_END}*]{{{MIN_MASKED_CHARS - 1},}})(?=[{_VALUE_END}]|$)"
# Cookie attributes of a Set-Cookie header (lower case): their values are not credentials.
COOKIE_ATTRIBUTES: tuple[str, ...] = (
    "expires", "max-age", "domain", "path", "secure", "httponly", "samesite", "priority", "partitioned",
)
COOKIE_HEADERS: tuple[str, ...] = ("cookie", "set-cookie")
AUTH_HEADERS: tuple[str, ...] = ("authorization", "proxy-authorization")
# A cookie crumb: name=value after the start, a ';' or a ',' (Set-Cookie lines joined by ', '). A comma inside
# an Expires date is followed by a blank and a day number, never by "name=".
_CRUMB = re.compile(r"(?:^|[;,])\s*(?P<name>[^=;,\s]+)\s*=\s*(?P<value>[^;,]*)")
# Diff wording after a header name ("header cookie: expected absent, ...") is not a value.
_NOT_A_VALUE = r"(?!\s*(?:expected|actual|absent|missing)\b)"


def masked(value: str) -> str:
    """How a credential is shown: its first characters (long values only) and its length."""
    prefix = value[:PREFIX_CHARS] if len(value) >= PREFIX_MIN_CHARS else ""
    return f"{prefix}*** ({len(value)} chars)"


def _key_refs(bundle: Bundle) -> tuple[set[str], set[str]]:
    """The header and query parameter names (lower case) the bundle's VerifyAPIKey policies read the key from."""
    headers: set[str] = set()
    params: set[str] = set()
    for policy in bundle.policies:
        if policy.type != KEY_POLICY_TYPE:
            continue
        for item in policy.settings.children:
            if item.tag != "APIKey":
                continue
            ref = fold(item.attributes.get("ref", "").strip())
            if ref.startswith("request.header.") and ref.count(".") == 2:
                headers.add(ref.split(".", 2)[2])
            elif ref.startswith("request.queryparam.") and ref.count(".") == 2:
                params.add(ref.split(".", 2)[2])
    return headers, params


class Masker:
    """Learns the credential values of one proxy's run and masks them in text (see the module docstring)."""

    def __init__(self, header_names: Iterable[str] = (), query_names: Iterable[str] = ()) -> None:
        self.headers = {fold(name) for name in (*SECRET_HEADERS, *header_names)}
        self.params = {fold(name) for name in query_names}
        self.values: set[str] = set()
        self._pattern: re.Pattern[str] | None = None
        names = sorted(self.headers | self.params, key=lambda n: (-len(n), n))
        self._named = re.compile(
            rf"(?i)(?<![A-Za-z0-9_.-])(?P<head>(?:{'|'.join(re.escape(n) for n in names)})[\"']?\s*[:=]\s*[\"']?)"
            rf"{_NOT_A_VALUE}{_NAMED_VALUE}"
        )

    @classmethod
    def for_bundle(cls, bundle: Bundle) -> Masker:
        headers, params = _key_refs(bundle)
        return cls(headers, params)

    def learn_value(self, value: str) -> None:
        """A credential value known without its name (e.g. a key the run sets in the deployed copy)."""
        self._add(value)

    def _add(self, value: str) -> None:
        value = value.strip()
        if len(value) < MIN_MASKED_CHARS:
            return
        found = {value, quote(value, safe=""), quote_plus(value, safe="")}
        scheme, _, token = value.partition(" ")
        if token.strip() and scheme:
            # "Bearer <token>", "Basic <credentials>": the credential alone may show up elsewhere.
            found.add(token.strip())
        new = {v for v in found if len(v) >= MIN_MASKED_CHARS} - self.values
        if new:
            self.values |= new
            self._pattern = None

    def learn_headers(self, headers: Mapping[str, str]) -> None:
        """The values of credential headers: the whole value and each secret in it.

        Each cookie value of a Cookie or Set-Cookie header (not the attributes such as Path or HttpOnly),
        and the credential after an Authorization scheme (for Basic also the decoded ``user:password``, the
        user and the password), so each is masked wherever it shows up alone (a body, a redirect target).
        """
        for name, value in dict(headers).items():
            key = fold(str(name))
            if key not in self.headers:
                continue
            text = str(value)
            self._add(text)
            if key in COOKIE_HEADERS:
                self._learn_cookies(text)
            elif key in AUTH_HEADERS:
                self._learn_credentials(text)

    def _learn_cookies(self, header: str) -> None:
        for crumb in _CRUMB.finditer(header):
            if fold(crumb.group("name")) in COOKIE_ATTRIBUTES:
                continue
            value = crumb.group("value").strip()
            self._add(value)
            if len(value) >= 2 and value[0] == value[-1] == '"':
                self._add(value[1:-1])

    def _learn_credentials(self, header: str) -> None:
        for item in header.split(","):
            scheme, _, token = item.strip().partition(" ")
            token = token.strip()
            if not token:
                continue
            self._add(token)
            if fold(scheme) == "basic":
                try:
                    decoded = base64.b64decode(token, validate=True).decode("utf-8")
                except (binascii.Error, ValueError):
                    continue
                user, colon, password = decoded.partition(":")
                self._add(decoded)
                if colon:
                    self._add(user)
                    self._add(password)

    def learn_query(self, query: str) -> None:
        if not self.params or not query:
            return
        for raw in query.split("&"):
            name, _, value = raw.partition("=")
            decoded = parse_qsl(raw, keep_blank_values=True)
            if decoded and fold(decoded[0][0]) in self.params:
                self._add(decoded[0][1])
                self._add(value)
            elif fold(name) in self.params:
                self._add(value)

    def learn_target(self, target: str) -> None:
        """A path with an optional ``?query``."""
        self.learn_query(target.partition("?")[2])

    def learn_request(self, request: HttpRequest) -> None:
        self.learn_headers(request.headers)
        self.learn_target(request.path)

    def mask(self, text: str) -> str:
        """``text`` with every learned and every credential-shaped value masked, then :func:`redaction.redact`."""
        if not text:
            return text
        if self.values:
            if self._pattern is None:
                ordered = sorted(self.values, key=lambda v: (-len(v), v))
                self._pattern = re.compile("|".join(re.escape(v) for v in ordered))
            text = self._pattern.sub(lambda match: masked(match.group(0)), text)
        text = _SCHEME_RULE.sub(_masked_value, text)
        text = _JWT_RULE.sub(_masked_value, text)
        text = self._named.sub(_masked_value, text)
        return redaction.redact(text)

    def mask_result(self, result: VerificationResult) -> VerificationResult:
        """``result`` with every text it holds masked: what verification.json, run.log and the console show."""
        return replace(
            result,
            message=self.mask(result.message),
            log_excerpt=self.mask(result.log_excerpt),
            cases=tuple(_masked_case(case, self) for case in result.cases),
            untested=tuple(
                UntestedPolicy(self.mask(u.name), u.type, self.mask(u.reason)) for u in result.untested
            ),
            review_flags=tuple(
                ReviewFlag(self.mask(f.policy), f.policy_type, self.mask(f.reason)) for f in result.review_flags
            ),
        )

    @contextmanager
    def active(self) -> Iterator[Masker]:
        """Make this the masker :func:`mask_shown` uses for the duration of the block."""
        token = _ACTIVE.set(self)
        try:
            yield self
        finally:
            _ACTIVE.reset(token)

    @contextmanager
    def logging(self) -> Iterator[Masker]:
        """Mask every a2m log line (run.log) written during the block, with what this masker knows by then."""
        log_filter = _MaskingFilter(self)
        logger = get_logger()
        logger.addFilter(log_filter)
        try:
            yield self
        finally:
            logger.removeFilter(log_filter)


def _masked_value(match: re.Match[str]) -> str:
    """A rule's match with its value masked (the name or scheme before it and any blanks after it kept)."""
    value = match.group("value")
    core = value.rstrip()
    return f"{match.groupdict().get('head') or ''}{masked(core)}{value[len(core):]}"


def _masked_case(case: CaseResult, masker: Masker) -> CaseResult:
    return replace(case, name=masker.mask(case.name), diff=masker.mask(case.diff))


_MASKED_MARK = "a2m_masked_by"


class _MaskingFilter(logging.Filter):
    """Masks a log record's message (once per masker, when nested blocks use the same one)."""

    def __init__(self, masker: Masker) -> None:
        super().__init__()
        self.masker = masker

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, _MASKED_MARK, None) is not self.masker:
            record.msg = self.masker.mask(record.getMessage())
            record.args = None
            setattr(record, _MASKED_MARK, self.masker)
        return True


# The masker of the run in progress (see Masker.active); outside a run, one that knows no value and masks
# credential-shaped values only.
_ACTIVE: ContextVar[Masker | None] = ContextVar("a2m_verify_masker", default=None)
_SHAPE_ONLY = Masker()


def current() -> Masker:
    """The active masker (:meth:`Masker.active`), or one that masks credential-shaped values only."""
    return _ACTIVE.get() or _SHAPE_ONLY


def mask_shown(text: str) -> str:
    """``text`` (a whole value, before any cut) masked by the active masker."""
    return current().mask(text)
