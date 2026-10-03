"""Which Apigee variables a2m can read in the generated app, and the DataWeave that reads them.

The vocabulary is part of the approved plan (CP5):

* ``request.verb`` -> ``attributes.method``
* ``proxy.pathsuffix`` -> ``attributes.maskedRequestPath``
* ``request.header.NAME`` -> the first comma-separated value of
  ``attributes.headers['name']``, trimmed, null when the header is missing
  (header names are case-insensitive, so NAME is lower-cased)
* ``request.queryparam.NAME`` -> ``attributes.queryParams['NAME']`` (case kept)
* ``response.header.NAME``, on the response side only -> the first
  comma-separated value of the response being built
  (``vars.responseHeaders['name']``, which holds the target's headers,
  Content-Type included, as later steps change them), trimmed, null when the
  header is missing; the framing headers Mule writes itself
  (:data:`RESPONSE_FRAMING_HEADERS`) are refused
* any name outside Apigee's built-in variable categories -> ``vars['NAME']``
  (a flow variable keeps its exact name in Mule, as the policy templates set it)

The built-in categories are an explicit allowlist (:data:`BUILTIN_PREFIXES`,
:data:`BUILTIN_TARGET_VARIABLES`). Every built-in variable without a mapping
(``client.ip``, ``system.timestamp``, ``virtualhost.name``, ``route.target``,
``loadbalancing.isfallback``, ``target.url`` ...) is refused by name, never
read as a flow variable (which would always be null in Mule). Built-in names
are matched exactly as Apigee writes them; ``Request.Verb`` is not mapped and,
since it folds to a built-in prefix, is refused rather than treated as a flow
variable.

The request variables read Mule's ``attributes``, which hold the caller's
request only until the target call: after an ``http:request`` they hold the
target's response. On the response side the generator saves the request as it
was sent (verb, path suffix, headers, query parameters) in the flow variable
:data:`SNAPSHOT_VAR` right before the target call (or where the route ends
without one), and request variables read that snapshot instead. A fault rule
can run before or after the target call, so there (direction :data:`FAULT`)
request variables are refused.

Request values read on either side are the caller's own or, on the response
side, the ones a2m carried over: a change an earlier step made in Apigee that
a2m could not carry over is not seen. So reading a request header, query
parameter or the verb that an earlier step on the same path sets, adds,
removes or copies (see :class:`RequestChanges`) is refused, never read stale.
The same holds for the proxy's own flow variables: reading one that an
earlier step on the path may write in Apigee, where the generated app does not
make that write (refused, skipped, or under a condition a2m can't translate),
is refused, never read as a missing value.
This one accessor and check serve conditions, message templates in policy
values, target URLs and the variables policies name in a ref, Ref or Source
(see :func:`a2m.policies.common.read_variable`).
"""

from __future__ import annotations

import string
from dataclasses import dataclass

from a2m.conditions.lexer import ConditionError

REQUEST = "request"
RESPONSE = "response"
# A fault rule may run before or after the target call: request variables can't be read there.
FAULT = "fault"
# The flow variable holding the request as sent to the target (see the module docstring).
SNAPSHOT_VAR = "a2mSentRequest"
SNAPSHOT_READ = f"vars.{SNAPSHOT_VAR}"

# Apigee's built-in variable categories (the allowlist), by prefix, compared in lower case: the categories of
# Apigee's flow variables reference plus the variables policies write under their own namespace. A name under
# one of them is a built-in variable; only the ones mapped in accessor() are read, every other is refused.
# Anything else is the proxy's own flow variable.
BUILTIN_PREFIXES = (
    "request.",
    "response.",
    "message.",
    "messageid",
    "client.",
    "proxy.",
    "proxyrequest.",
    "system.",
    "environment.",
    "organization.",
    "apiproxy.",
    "apiproduct.",
    "application.",
    "app.",
    "developer.",
    "error.",
    "fault.",
    "flow.",
    "current.",
    "router.",
    "route.",
    "virtualhost.",
    "loadbalancing.",
    "graphql.",
    "mint.",
    "variable.",
    "is.error",
    "apigee.",
    "ratelimit.",
    "verifyapikey.",
    "oauthv2",
    "oauthv1",
    "accesstoken.",
    "jwt.",
    "jws.",
    "saml.",
    "servicecallout.",
    "responsecache.",
    "lookupcache.",
    "populatecache.",
    "invalidatecache.",
    "messagelogging.",
    "messagevalidation.",
    "accesscontrol.",
    "basicauthentication.",
    "extractvariables.",
    "assignmessage.",
    "raisefault.",
    "spikearrest.",
    "quota.",
    "keyvaluemap.",
    "kvm.",
    "javascript.",
    "javacallout.",
    "python.",
    "xmltojson.",
    "jsontoxml.",
    "xsl.",
    "jsonthreatprotection.",
    "xmlthreatprotection.",
    "regularexpressionprotection.",
    "statisticscollector.",
    "hmac.",
    "ldap.",
    "flowcallout.",
    "sharedflow.",
    "tls.",
)

# Apigee's target.* variables; other names under target. (target.env, say) are the proxy's own.
BUILTIN_TARGET_VARIABLES = (
    "target.url",
    "target.host",
    "target.ip",
    "target.name",
    "target.port",
    "target.scheme",
    "target.basepath",
    "target.copy",
    "target.cn",
    "target.expectedcn",
    "target.received",
    "target.sent",
    "target.ssl",
)

VERB = "request.verb"
PATH_SUFFIX = "proxy.pathsuffix"
# Apigee's proxy.pathsuffix exactly, on the request side (the request path below the matched base path). Every
# listener path ends in '/*', and Mule's maskedRequestPath is the part of the raw request path that trailing '*'
# matched, so a wildcard segment of any length in the base path ('/v1/*/search') is never forwarded:
# '/v1/acme/search/items' gives '/items'. Mule gives '/' both for the base path itself and for the base path with a
# trailing slash; Apigee's suffix is '' for the first and '/' for the second. The generator forwards this to the
# target and ExtractVariables URIPath matches against it; conditions read maskedRequestPath and refuse any check
# that tells '' from '/' apart (a2m.conditions.dataweave).
EXACT_PATH_SUFFIX_DW = (
    "if (attributes.maskedRequestPath == '/' and not (attributes.rawRequestPath endsWith '/')) '' "
    "else attributes.maskedRequestPath"
)
HEADER_PREFIX = "request.header."
QUERY_PREFIX = "request.queryparam."
ASCII_FOLD = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


ANY = "*"
# The Apigee names of the message parts a2m tracks like flow variables in RequestChanges.variables: the generated
# app's steps write them (Mule's payload, the response headers being built), so only a write it does not make
# exactly makes a later read stale.
REQUEST_CONTENT = "request.content"
RESPONSE_CONTENT = "response.content"
RESPONSE_HEADER_PREFIX = "response.header."
# The Content-Type of the request or response together with the media type its payload has in the generated app,
# tracked like the parts above: ExtractVariables JSONPayload decides on the Content-Type, as Apigee does, and reads
# the payload as Mule parsed it, so the two must still agree. Only a step that sets both together (Set Payload with a
# contentType) or removes the Content-Type writes it exactly.
REQUEST_CONTENT_TYPE = "request.content-type"
RESPONSE_CONTENT_TYPE = "response.content-type"
# The response being built: the target's headers (as the generator copies them after the call, Content-Type included)
# with every change a step made, or the headers a rejecting policy sets.
RESPONSE_HEADERS_VAR = "responseHeaders"
RESPONSE_HEADERS_BASE = f"(vars.{RESPONSE_HEADERS_VAR} default {{}})"
# The target response's framing headers: Mule's listener writes its own, so the generator does not copy the target's
# and a read of one is refused.
RESPONSE_FRAMING_HEADERS = ("content-length", "transfer-encoding", "connection")


@dataclass(frozen=True, slots=True)
class RequestChanges:
    """What earlier steps on a path may have done that the generated app does not see, and which steps.

    Each header or query entry is (name, step): the header name in lower case
    (query parameter names too, so a near-miss in case is refused rather than
    read stale), or :data:`ANY` when the step may change every one of them.
    ``verb`` holds the steps that may change the request verb (AssignMessage
    Set or Copy Verb, a new request). The generated app always reads the
    caller's request, so every change counts, carried over or not.

    ``variables`` holds the proxy's own flow variables an earlier step may
    write in Apigee where the generated app does not make that write exactly
    (the step or that setting is refused or skipped, or the step sits under a
    condition a2m can't translate): (name, step), the name in lower case,
    ``NAME.`` for every variable under NAME, or :data:`ANY` when the step may
    write any variable (a policy type a2m does not translate). A write the
    generated app makes exactly is not one: the app reads the same value.
    The message parts a2m's steps write are tracked the same way, by their
    Apigee names: :data:`REQUEST_CONTENT`, :data:`RESPONSE_CONTENT`,
    :data:`REQUEST_CONTENT_TYPE`, :data:`RESPONSE_CONTENT_TYPE` and ``response.header.NAME``
    (``response.header.`` for every one of them).

    :meth:`everything` is a step a2m does not generate to run exactly as in
    Apigee and whose writes are not documented: it may change every request
    header, query parameter, the verb, both payloads and every variable.
    """

    headers: frozenset[tuple[str, str]] = frozenset()
    queries: frozenset[tuple[str, str]] = frozenset()
    verb: frozenset[str] = frozenset()
    variables: frozenset[tuple[str, str]] = frozenset()

    def __or__(self, other: RequestChanges) -> RequestChanges:
        return RequestChanges(
            self.headers | other.headers,
            self.queries | other.queries,
            self.verb | other.verb,
            self.variables | other.variables,
        )

    def __bool__(self) -> bool:
        return bool(self.headers or self.queries or self.verb or self.variables)

    @staticmethod
    def everything(step: str) -> RequestChanges:
        """``step`` may change anything: every request header, query parameter, the verb, every flow variable and
        message part."""
        every = frozenset({(ANY, step)})
        return RequestChanges(every, every, frozenset({step}), every)

    def verb_steps(self) -> list[str]:
        """The steps that may change the request verb, sorted."""
        return sorted(self.verb)

    def header_steps(self, name: str) -> list[str]:
        """The steps that may change request header ``name``, sorted."""
        return _steps_for(self.headers, name)

    def query_steps(self, name: str) -> list[str]:
        """The steps that may change query parameter ``name``, sorted."""
        return _steps_for(self.queries, name)

    def variable_steps(self, name: str) -> list[str]:
        """The earlier steps that may write flow variable ``name`` where the generated app does not, sorted."""
        key = fold(name.strip())
        return sorted(
            {
                step
                for written, step in self.variables
                if written in (key, ANY) or (written.endswith(".") and key.startswith(written))
            }
        )


NO_CHANGES = RequestChanges()


def _steps_for(entries: frozenset[tuple[str, str]], name: str) -> list[str]:
    key = fold(name)
    return sorted({step for changed, step in entries if changed in (key, ANY)})


@dataclass(frozen=True, slots=True)
class Accessor:
    """DataWeave reading one Apigee variable; ``nullable`` is False when the value is always there."""

    dw: str
    nullable: bool


def fold(value: str) -> str:
    """``value`` with A-Z as a-z (HTTP header names and Apigee keywords are ASCII, compared without case)."""
    return value.translate(ASCII_FOLD)


def is_custom_variable(name: str) -> bool:
    """True for a flow variable the proxy sets itself (not one of Apigee's built-in variables)."""
    lowered = fold(name.strip())
    if not lowered:
        return False
    if any(lowered == p.rstrip(".") or lowered.startswith(p) for p in BUILTIN_PREFIXES):
        return False
    return lowered != "target" and not any(
        lowered == t or lowered.startswith(t + ".") for t in BUILTIN_TARGET_VARIABLES
    )


def dw_key(value: str) -> str:
    """``value`` as a single-quoted DataWeave string for a selector key (``vars['ext.orderId']``).

    '$' becomes a unicode escape so neither DataWeave interpolation nor Mule's
    ${...} property placeholders see it.
    """
    escaped = (
        value.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("$", "\\u0024")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f"'{escaped}'"


def stale_part(name: str, what: str, changes: RequestChanges) -> str | None:
    """Why the message part ``name`` (``request.content``, ``response.content``, ``response.header.NAME``; ``what``
    describes it) can't be read after ``changes``, or None when it can: an earlier step may change it in Apigee
    and the generated app does not make that change exactly, so the app would read the old value."""
    steps = changes.variable_steps(name)
    if not steps:
        return None
    return (
        f"the {what} ({name}) may be changed by the earlier step {', '.join(steps)} on the same path, and the "
        "generated app does not make that change (the step or that setting can't be translated, or it never runs "
        "in the generated app); it would read the unchanged value, so the result could differ from Apigee's"
    )


def accessor(name: str, direction: str = REQUEST, changes: RequestChanges = NO_CHANGES) -> Accessor:
    """DataWeave reading Apigee variable ``name`` on the ``direction`` side; :class:`ConditionError` when unmapped.

    On the request side request variables read Mule's attributes; on the
    response side they read the request snapshot (:data:`SNAPSHOT_VAR`); for
    :data:`FAULT` they are refused. ``changes`` are the request headers, query
    parameters and verb earlier steps may have changed: reading one of those is
    refused, since the generated app may not see the changed value.
    """
    request_part = name in (VERB, PATH_SUFFIX) or name.startswith((HEADER_PREFIX, QUERY_PREFIX))
    if request_part and direction not in (REQUEST, RESPONSE):
        raise ConditionError(
            f"the Apigee variable {name} is read in a fault rule, which can run before or after the target call, "
            "so the generated app may no longer have the caller's request there"
        )
    if direction == RESPONSE:
        method, suffix = f"{SNAPSHOT_READ}.method", f"{SNAPSHOT_READ}.pathSuffix"
        headers, queries = f"{SNAPSHOT_READ}.headers", f"{SNAPSHOT_READ}.queryParams"
    else:
        method, suffix = "attributes.method", "attributes.maskedRequestPath"
        headers, queries = "attributes.headers", "attributes.queryParams"
    if name == VERB:
        steps = changes.verb_steps()
        if steps:
            raise ConditionError(
                f"the request verb ({name}) may be changed by the earlier step {', '.join(steps)} on the same path; "
                "the generated app would read the caller's original verb, so the result could differ from Apigee's"
            )
        return Accessor(method, nullable=False)
    if name == PATH_SUFFIX:
        return Accessor(suffix, nullable=False)
    for prefix, base, key_of, changed_by, part in (
        (HEADER_PREFIX, headers, fold, changes.header_steps, "request header"),
        (QUERY_PREFIX, queries, str, changes.query_steps, "query parameter"),
    ):
        if name.startswith(prefix):
            key = name[len(prefix) :]
            if not key or "." in key:
                # request.header.Accept.values.count, request.queryparam.id.2: Apigee's multi-value forms.
                raise ConditionError(f"the Apigee variable {name} uses a form a2m does not map (only {prefix}NAME)")
            steps = changed_by(key)
            if steps:
                raise ConditionError(
                    f"the {part} {key} ({name}) may be changed by the earlier step {', '.join(steps)} on the same "
                    f"path; the generated app would read the caller's original {part}, not the changed value, so "
                    "the result could differ from Apigee's"
                )
            read = f"{base}[{dw_key(key_of(key))}]"
            if prefix == HEADER_PREFIX:
                read = first_value(read)
            return Accessor(read, nullable=True)
    if name.startswith(RESPONSE_HEADER_PREFIX):
        return _response_header(name, direction, changes)
    if is_custom_variable(name):
        steps = changes.variable_steps(name)
        if steps:
            raise ConditionError(
                f"the flow variable {name} may be written by the earlier step {', '.join(steps)} on the same path, "
                "and the generated app does not make that write (the step or that setting can't be translated, "
                "or the step never runs in the generated app because its condition, or its Flow's or RouteRule's, "
                "can't be); it would read a missing value, so the result could differ from Apigee's"
            )
        return Accessor(f"vars[{dw_key(name)}]", nullable=True)
    raise ConditionError(f"the Apigee variable {name} has no mapping in a2m")


def first_value(read: str) -> str:
    """The header ``read`` as Apigee reads ``request.header.NAME`` and ``response.header.NAME``: its first
    comma-separated value, trimmed. A missing header stays null, so `= null` keeps its meaning and missing stays
    distinct from ""."""
    return f'(if ({read} == null) null else trim(({read} splitBy ",")[0] default ""))'


def _response_header(name: str, direction: str, changes: RequestChanges) -> Accessor:
    """``response.header.NAME`` read from the response being built (:data:`RESPONSE_HEADERS_BASE`), with the same
    meaning as a request header; refused outside the response side, for a framing header, and when an earlier step
    may change the header where the generated app does not."""
    key = fold(name[len(RESPONSE_HEADER_PREFIX) :])
    if direction != RESPONSE:
        if direction == FAULT:
            where = "in a fault rule, which can run before or after the target call"
        else:
            where = "on the request side, before there is a response"
        raise ConditionError(f"the Apigee variable {name} is read {where}, so the generated app has no response there")
    if not key or "." in key:
        # response.header.Set-Cookie.values.count and the like: Apigee's multi-value forms.
        raise ConditionError(
            f"the Apigee variable {name} uses a form a2m does not map (only {RESPONSE_HEADER_PREFIX}NAME)"
        )
    if key in RESPONSE_FRAMING_HEADERS:
        raise ConditionError(
            f"the Apigee variable {name} reads the framing header {key}, which Mule's listener writes itself when "
            "it sends the response; the generated app does not keep the target's value, so it can't be read exactly"
        )
    stale = stale_part(RESPONSE_HEADER_PREFIX + key, f"response header {key}", changes)
    if stale is not None:
        raise ConditionError(stale)
    return Accessor(first_value(f"{RESPONSE_HEADERS_BASE}[{dw_key(key)}]"), nullable=True)
