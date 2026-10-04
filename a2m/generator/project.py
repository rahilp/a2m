"""Generate a Mule 4 project from one Apigee proxy (the IR of :mod:`a2m.ir`).

:func:`generate_project` writes a complete mule-app folder:

* ``pom.xml`` (from ``templates/pom.xml``, versions pinned to the ones proven
  on Mule 4.9.0) and ``mule-artifact.json``;
* ``src/main/mule/proxy.xml``: one listening flow per ProxyEndpoint that
  forwards the caller's method, path below the base path, query string,
  headers and body to the target, and passes the target's status, headers and
  body back unchanged, plus one sub-flow per shared flow the proxy calls,
  reached with flow-ref where the FlowCallout step was;
* ``src/main/resources/config.properties``: the listen port and every target
  address, so a runner can point the app at another port or backend.

Policy steps go through the templates of :mod:`a2m.policies`: each generated
step is one processor labelled ``doc:name="<step name>"`` at the step's place
in Apigee's execution order, and :attr:`GenerateResult.policies` holds one
result record per step (``template`` or ``skipped``). A skipped step leaves an
XML comment naming the step and its type where it would have been.

Conditions go through :mod:`a2m.conditions`. A conditional step sits alone
in a ``choice``/``when`` at its place; the conditional Flows of an endpoint
become one ``choice`` that runs the first matching flow, in Apigee's order
(a flow without a condition is the ``otherwise``); conditional RouteRules
become the ``when`` branches of the routing choice. A condition, message
template or target URL that reads a request header, query parameter or the
verb an earlier step on the same path may change (AssignMessage,
BasicAuthentication Encode, also inside a shared flow) can't be translated:
the generated app's attributes keep the caller's values. Each conditional
Flow's own condition is checked against the changes made before the flows
only, since a sibling Flow that was tried first never ran. Request variables
read on the response side read a snapshot of the request as sent, saved in
the flow variable ``a2mSentRequest`` right before the target call (or where the
route ends without one); it is only written when something reads it. A
condition that can't be
translated keeps its ``when`` at ``#[false]`` (the step, flow or route never
runs, it never runs unguarded), with "can't translate", the original text and
the reason beside it in the flow XML. :attr:`GenerateResult.conditions` holds
one record per non-empty condition in the bundle, translated or not, including
those of fault rules and other parts that are not generated.

Nothing is dropped silently. Whatever this step does not generate (policies
without a template, settings a template cannot carry over, fault rules,
target settings, targets without a fixed URL, ...) is listed in
:attr:`GenerateResult.unsupported` with a reason.

All XML is built with :mod:`xml.etree.ElementTree`, so every value from the
bundle is escaped. The output is deterministic: no timestamps, no absolute
paths, the same input gives the same bytes.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from a2m import safefs
from a2m.ai.checks import DeclaredWrites, all_steps, callout_reads, changed_steps, describe_changes
from a2m.ai.provider import Confidence, Provider
from a2m.ai.sources import CalloutSource, callout_kind, callout_source
from a2m.ai.translate import NONE, CalloutTranslated, NotTranslated, Place, Translator
from a2m.conditions import (
    ANY,
    FAULT,
    NO_CHANGES,
    REQUEST_CONTENT,
    REQUEST_CONTENT_TYPE,
    RESPONSE_CONTENT,
    RESPONSE_CONTENT_TYPE,
    RESPONSE_HEADER_PREFIX,
    SNAPSHOT_VAR,
    RequestChanges,
    Translation,
    dw_string,
    template_parts,
    translate_condition,
    translate_template,
)
from a2m.conditions.lexer import ConditionError, TokenKind, tokenize
from a2m.conditions.parser import OPERATORS
from a2m.conditions.variables import EXACT_PATH_SUFFIX_DW, RESPONSE_FRAMING_HEADERS, accessor, fold
from a2m.ir import (
    Bundle,
    BundleKind,
    Flow,
    FlowSteps,
    Policy,
    ProxyEndpoint,
    Resource,
    RouteRule,
    Step,
    TargetEndpoint,
    XmlElement,
)
from a2m.layout import collision_key
from a2m.policies import registry
from a2m.policies.common import (
    FAULT_ERROR_TYPE,
    NEED_FAULT,
    NEED_REASON_PHRASE,
    NEED_REQUEST_HEADERS,
    NEED_REQUEST_QUERY,
    NEED_REQUEST_SNAPSHOT,
    REASON_PHRASE_VAR,
    REQUEST,
    REQUEST_HEADERS_VAR,
    REQUEST_QUERY_VAR,
    RESPONSE,
    Method,
    PolicyResult,
    TemplateOutput,
    UnsupportedOption,
    is_true,
)

CORE = "http://www.mulesoft.org/schema/mule/core"
HTTP = "http://www.mulesoft.org/schema/mule/http"
OS = "http://www.mulesoft.org/schema/mule/os"
EE = "http://www.mulesoft.org/schema/mule/ee/core"
VALIDATION = "http://www.mulesoft.org/schema/mule/validation"
DOC = "http://www.mulesoft.org/schema/mule/documentation"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
POM = "http://maven.apache.org/POM/4.0.0"
SCHEMA_LOCATIONS = {
    CORE: "http://www.mulesoft.org/schema/mule/core/current/mule.xsd",
    HTTP: "http://www.mulesoft.org/schema/mule/http/current/mule-http.xsd",
    OS: "http://www.mulesoft.org/schema/mule/os/current/mule-os.xsd",
    EE: "http://www.mulesoft.org/schema/mule/ee/core/current/mule-ee.xsd",
    VALIDATION: "http://www.mulesoft.org/schema/mule/validation/current/mule-validation.xsd",
}
# The pom dependency each module namespace beyond core and http needs, pinned to the versions proven on 4.9.0.
MODULE_DEPENDENCIES = {
    OS: ("org.mule.connectors", "mule-objectstore-connector", "1.2.2"),
    VALIDATION: ("org.mule.modules", "mule-validation-module", "2.0.9"),
}
for _prefix, _uri in (
    ("http", HTTP),
    ("os", OS),
    ("ee", EE),
    ("validation", VALIDATION),
    ("doc", DOC),
    ("xsi", XSI),
):
    ET.register_namespace(_prefix, _uri)
DOC_NAME = f"{{{DOC}}}name"
DOC_DESCRIPTION = f"{{{DOC}}}description"

MULE_DIR = ("src", "main", "mule")
RESOURCES_DIR = ("src", "main", "resources")
PROXY_FILE = "proxy.xml"
PROPERTIES_FILE = "config.properties"

MIN_MULE_VERSION = "4.9.0"
JAVA_VERSION = "17"
LISTENER_CONFIG = "http-listener-config"
LISTENER_HOST_KEY = "http.listener.host"
LISTENER_PORT_KEY = "http.listener.port"
LISTENER_HOST = "0.0.0.0"
LISTENER_PORT = "8081"
TIMEOUT_PROPERTY = "io.timeout.millis"
DEFAULT_PORTS = {"http": 80, "https": 443}
PROTOCOLS = {"http": "HTTP", "https": "HTTPS"}
FLOW_CALLOUT = "FlowCallout"
# The flow variables holding the name of the conditional Flow that matched the request, so the same
# flow's response steps run after the target call (Apigee picks the flow once, on the request).
PROXY_FLOW_VAR = "a2mFlow"
TARGET_FLOW_VAR = "a2mTargetFlow"
CANT_TRANSLATE_TAG = "condition-cant-translate"
SHARED_FLOW_ENTRY = "default"
XML_HEAD = '<?xml version="1.0" encoding="UTF-8"?>\n'

# The request path below the matched base path: Apigee's proxy.pathsuffix, so the target gets exactly its own base
# path for a call to the bare base path (see EXACT_PATH_SUFFIX_DW).
REQUEST_PATH_DW = EXACT_PATH_SUFFIX_DW
REQUEST_PATH = f"#[{REQUEST_PATH_DW}]"
# A target URL's {variable} reference (as message templates write it).
URL_SCHEME = re.compile(r"(?i)http(s?)://")
# Hop-by-hop and length headers the HTTP connector sets itself; everything else is passed on.
HOP_HEADERS = "['host', 'content-length', 'transfer-encoding', 'connection']"
REQUEST_HEADERS = f"#[attributes.headers -- {HOP_HEADERS}]"
REQUEST_QUERY = "#[attributes.queryParams]"
# The same, once a policy step may have changed the headers or query parameters the target gets.
CHANGED_REQUEST_HEADERS = f"#[(vars.{REQUEST_HEADERS_VAR} default attributes.headers) -- {HOP_HEADERS}]"
CHANGED_REQUEST_QUERY = f"#[vars.{REQUEST_QUERY_VAR} default attributes.queryParams]"
# The request as sent (or as it stands where a route ends without a target call), for response-side reads of
# request variables (see a2m.conditions.variables): the keys are the ones the accessor reads.
REQUEST_SNAPSHOT = (
    "#[output application/java --- {method: attributes.method, pathSuffix: attributes.maskedRequestPath, "
    f"headers: (vars.{REQUEST_HEADERS_VAR} default attributes.headers), "
    f"queryParams: (vars.{REQUEST_QUERY_VAR} default attributes.queryParams)}}]"
)
# The target's headers as the response being built: Content-Type included (the listener sends that header rather than
# the payload's media type, and response.header.Content-Type reads it), the framing headers Mule writes itself not.
RESPONSE_HEADERS = "#[attributes.headers -- [" + ", ".join(f"'{h}'" for h in RESPONSE_FRAMING_HEADERS) + "]]"
NO_ROUTE_FAULT = '{"fault": "No route of the migrated proxy matched this request."}'
NO_TARGET_FAULT = '{"fault": "The target of this route was not migrated; see the migration report."}'
# What a caller gets when the flow fails (target down, timeout, any Mule error). Mule's error
# description can name the target's host and port or carry connector messages, so it never goes
# into a response: the caller gets a fixed Apigee-style fault picked by the error type alone, and
# the details stay in the app's log (Mule's default error handler logs every error it propagates).
# Each entry: status, Mule error identifiers, faultstring, errorcode. Anything else is a 500.
ERROR_FAULTS: tuple[tuple[int, tuple[str, ...], str, str], ...] = (
    (
        503,
        ("CONNECTIVITY", "RETRY_EXHAUSTED", "SERVICE_UNAVAILABLE"),
        "The Service is temporarily unavailable",
        "messaging.adaptors.http.flow.ServiceUnavailable",
    ),
    (504, ("TIMEOUT", "GATEWAY_TIMEOUT"), "Gateway Timeout", "messaging.adaptors.http.flow.GatewayTimeout"),
    (502, ("BAD_GATEWAY",), "Bad Gateway", "messaging.adaptors.http.flow.BadGateway"),
)
ERROR_FAULT_DEFAULT = (500, "Internal Server Error", "messaging.runtime.InternalServerError")
# Characters kept in Mule names and property keys; anything else becomes '-'.
UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")
ARTIFACT_UNSAFE = re.compile(r"[^a-z0-9._-]+")
# Words in a condition that are not variables: connectives, literals and word operators (see a2m.conditions.parser).
CONDITION_WORDS = frozenset({"and", "or", "not", "null", "true", "false", *OPERATORS})
NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


@dataclass(frozen=True, slots=True)
class UnsupportedItem:
    """Something in the bundle this step did not generate: a step, policy, target endpoint or setting."""

    name: str
    reason: str


@dataclass(frozen=True, slots=True)
class PendingCondition:
    """A condition left for a later translation step: ``condition`` is the original text.

    Every condition is translated or marked can't translate (see
    :class:`ConditionRecord`), so :attr:`GenerateResult.pending` stays empty;
    the type is kept for callers that read it.
    """

    name: str
    condition: str
    endpoint: str
    kind: str = "RouteRule"


@dataclass(frozen=True, slots=True)
class ConditionRecord:
    """One non-empty condition of the bundle: translated (``ok``, ``dw``) or can't translate (``reason``).

    ``name`` is the step, flow, fault rule or route rule; ``kind`` says which
    (Step, Flow, FaultRule, DefaultFaultRule, RouteRule); ``location`` is where
    it sits; ``original`` is the condition text as read from the bundle; ``dw``
    is the DataWeave expression without the ``#[ ]`` wrapper.

    ``method`` is ``template`` (a2m translated it), ``ai`` (sent to the AI:
    translated when ``ok``, else flagged for review) or ``skipped`` (can't
    translate, not sent to the AI). For ``ai``: ``confidence`` and ``notes``
    are the AI's (``notes`` also says why its answer was not used),
    ``needs_review`` is True unless the AI's translation is used with medium or
    high confidence, and ``reason`` keeps why a2m's own translator refused it
    when the AI's answer is not used either.
    """

    name: str
    kind: str
    location: str
    original: str
    ok: bool
    dw: str | None
    reason: str | None
    method: Method = Method.TEMPLATE
    confidence: Confidence | None = None
    notes: str = ""
    needs_review: bool = False


@dataclass(frozen=True, slots=True)
class GenerateResult:
    files: tuple[str, ...]
    unsupported: tuple[UnsupportedItem, ...]
    pending: tuple[PendingCondition, ...]
    # One result record per policy step, in flow order (a policy used by two steps has two records).
    policies: tuple[PolicyResult, ...] = ()
    # One record per non-empty condition, in the order the generator met them (a step generated twice has two).
    conditions: tuple[ConditionRecord, ...] = ()
    # The Mule Enterprise components the app uses (e.g. "ee:transform", Transform Message from an AI answer),
    # sorted, and the steps that hold them in document order; empty when the app runs on Mule Kernel (CE) too.
    enterprise_components: tuple[str, ...] = ()
    enterprise_steps: tuple[str, ...] = ()

    @property
    def requires_enterprise(self) -> bool:
        """True when the app can only run on a Mule Enterprise runtime (Mule Kernel CE cannot deploy it)."""
        return bool(self.enterprise_components)


class GeneratorError(ValueError):
    """The input cannot be generated at all (for example a shared flow bundle passed as the proxy)."""


def generate_project(
    bundle: Bundle,
    dest: Path,
    *,
    shared_flows: Sequence[Bundle] = (),
    results_root: Path | None = None,
    provider: Provider | None = None,
) -> GenerateResult:
    """Write the Mule project for proxy ``bundle`` into ``dest``, replacing any older project there.

    ``shared_flows`` are the shared flow bundles the proxy's FlowCallout steps
    may call, matched by bundle name. Every write and delete goes through
    :mod:`a2m.safefs` and stays strictly inside ``results_root`` (default: the
    folder holding ``dest``, created when missing).

    ``provider`` (see :mod:`a2m.ai`) translates what the templates cannot: the
    code of JavaScript, Python and Java callouts, and conditions a2m's own
    translator refuses, unless the condition reads a value an earlier step may
    have changed (the AI cannot see that change either) or an Apigee built-in
    variable a2m has no mapping for (it does not exist in the generated app).
    A callout whose code may read a value an earlier step changed is
    translated but flagged for review. An AI-translated step is a faithful
    writer only of what its checked ``writes`` declaration names; otherwise it
    may change anything. With None nothing is sent anywhere and those stay
    skipped or can't translate.
    """
    if bundle.kind is not BundleKind.PROXY:
        raise GeneratorError(f"{bundle.name} is a {bundle.kind.value} bundle, not a proxy")
    builder = _ProjectBuilder(bundle, shared_flows, Translator(provider) if provider is not None else None)
    files = builder.build()
    _write_tree(dest, files, results_root)
    return _result(builder, files)


def plan_project(bundle: Bundle, *, shared_flows: Sequence[Bundle] = ()) -> GenerateResult:
    """What :func:`generate_project` reports for ``bundle`` without an AI provider, without writing anything.

    For a caller that has a generated project but not its result (the records of a project generated with a
    provider may differ: the AI may have translated what this leaves skipped or can't translate).
    """
    if bundle.kind is not BundleKind.PROXY:
        raise GeneratorError(f"{bundle.name} is a {bundle.kind.value} bundle, not a proxy")
    builder = _ProjectBuilder(bundle, shared_flows, None)
    return _result(builder, builder.build())


def _result(builder: _ProjectBuilder, files: dict[str, str]) -> GenerateResult:
    return GenerateResult(
        files=tuple(sorted(files)),
        unsupported=tuple(builder.unsupported),
        pending=(),
        policies=tuple(builder.records),
        conditions=tuple(builder.conditions),
        enterprise_components=builder.enterprise_components,
        enterprise_steps=builder.enterprise_steps,
    )


# ---------------------------------------------------------------- target addresses


@dataclass(frozen=True, slots=True)
class _Address:
    protocol: str
    host: str
    port: str
    base_path: str


@dataclass(frozen=True, slots=True)
class _TemplateAddress:
    """A target URL with {variable} references: its fixed protocol and the URL as written."""

    protocol: str
    url: str


def shown_url(url: str) -> str:
    """``url`` as it may appear in a reason or log line: user name, password and query values masked.

    A target URL can carry credentials (``https://user:password@host``) or an
    API key in its query string; neither may reach run.log or a report.
    """
    text = url.strip()
    head, sep, rest = text.partition("//")
    if not sep:
        head, rest = "", text
    end = next((i for i, char in enumerate(rest) if char in "/?#"), len(rest))
    authority, tail = rest[:end], rest[end:]
    if "@" in authority:
        authority = "***@" + authority.rpartition("@")[2]
    tail, hash_sep, fragment = tail.partition("#")
    path, query_sep, query = tail.partition("?")
    if query_sep:
        # Parameter names stay readable; every value (and any bare token) is masked.
        query = "&".join(
            f"{part.partition('=')[0]}=***" if "=" in part else ("***" if part else "") for part in query.split("&")
        )
    return f"{head}{sep}{authority}{path}{query_sep}{query}{hash_sep}{fragment}"


def _parse_url(url: str) -> _Address | str:
    """The target address in ``url``, or the reason it cannot be used as a fixed address."""
    shown = shown_url(url)
    if "{" in url or "}" in url:
        return f"its URL {shown} contains a {{variable}} reference; a2m does not translate variables in target URLs"
    split = urlsplit(url.strip())
    scheme = split.scheme  # urlsplit gives it in lower case
    if scheme not in DEFAULT_PORTS:
        return f"its URL {shown} is not an http or https address"
    if split.username is not None or split.password is not None:
        return f"its URL {shown} holds credentials, which a2m does not copy into the project"
    try:
        port = split.port
    except ValueError:
        return f"its URL {shown} has a port that is not a number from 0 to 65535"
    if not split.hostname:
        return f"its URL {shown} has no host"
    path = re.sub(r"/{2,}", "/", split.path) or "/"
    # urlsplit lower-cases the host; keep it as the bundle wrote it (a bracketed IPv6 address as parsed).
    written = split.netloc.rpartition("@")[2]
    host = split.hostname if written.startswith("[") else written.partition(":")[0]
    return _Address(PROTOCOLS[scheme], host, str(port or DEFAULT_PORTS[scheme]), path)


def _template_url(url: str) -> _TemplateAddress | str:
    """The target address of a URL with {variable} references, or the reason it can't be generated.

    The scheme must be written literally (the request config is HTTP or HTTPS),
    every brace must belong to a {variable} reference a2m can read, and the URL
    may hold no query string or credentials (as for a fixed URL). With a
    placeholder for each reference, the rest must still be an http or https
    address with a host. The final check of the references (on the route's
    path) is in :meth:`_ProjectBuilder._target_processors`.
    """
    shown = shown_url(url)
    text = url.strip()
    parts = template_parts(text)
    if any(p.variable is None and ("{" in p.text or "}" in p.text) for p in parts):
        return f"its URL {shown} holds a brace that is not a {{variable}} reference"
    scheme = URL_SCHEME.match(text)
    if scheme is None:
        return f"its URL {shown} does not start with a fixed http:// or https://"
    translation = translate_template(text, direction=REQUEST)
    if not translation.ok:
        return f"its URL {shown} can't be translated: {translation.reason}"
    split = urlsplit("".join("0" if p.variable is not None else p.text for p in parts))
    if split.query or split.fragment:
        return f"its URL {shown} has a query string or fragment, which a2m does not carry over with {{variable}} references"
    if split.username is not None or split.password is not None:
        return f"its URL {shown} holds credentials, which a2m does not copy into the project"
    if not split.hostname:
        return f"its URL {shown} has no host"
    return _TemplateAddress("HTTPS" if scheme.group(1) else "HTTP", text)


def listener_path(base_path: str | None) -> str:
    """The http:listener path for an Apigee base path: '/v1/orders/' and '/v1/orders' give '/v1/orders/*'."""
    path = re.sub(r"/{2,}", "/", "/" + (base_path or "").strip()).rstrip("/")
    return f"{path}/*"


def unsupported_base_path(base_path: str | None) -> str | None:
    """Why ``base_path`` cannot be a Mule listener path, or None when it can.

    Mule matches '*' in a listener path only as a whole segment (one segment, like
    Apigee's '/v1/*/search') and reads '{name}' as a URI parameter, so a base path with
    a partial or double wildcard ('/v1/ab*', '/v1/**') or with braces would match other
    requests than Apigee does.
    """
    path = listener_path(base_path)[: -len("/*")]
    if "{" in path or "}" in path:
        return "Mule reads '{...}' in a listener path as a URI parameter, so it would match any segment"
    if any("*" in segment and segment != "*" for segment in path.split("/")):
        return "Mule matches '*' in a listener path only as one whole path segment"
    return None


def artifact_id(name: str) -> str:
    """A Maven artifactId for proxy ``name``: lower case letters, digits, '.', '_' and '-'."""
    text = ARTIFACT_UNSAFE.sub("-", collision_key(name)).strip("-")
    if not text or not text[0].isalnum():
        text = f"proxy-{text}".rstrip("-")
    return text


# ---------------------------------------------------------------- building


def _element(ns: str, tag: str, attrib: dict[str, str] | None = None, text: str | None = None) -> ET.Element:
    """A new element; ``ns`` CORE and POM are each document's default namespace, so they get a plain tag.

    ElementTree's ``default_namespace`` option refuses plain attribute names, so
    the default namespace is declared with an ``xmlns`` attribute on the root
    instead (see :func:`_mule_document` and :func:`_pom_text`).
    """
    element = ET.Element(tag if ns in (CORE, POM) else f"{{{ns}}}{tag}", attrib or {})
    element.text = text
    return element


def _child(parent: ET.Element, ns: str, tag: str, attrib: dict[str, str] | None = None) -> ET.Element:
    child = _element(ns, tag, attrib)
    parent.append(child)
    return child


@dataclass
class _Names:
    """Unique Mule names and property key parts, made from bundle names."""

    used: set[str] = field(default_factory=set)

    def take(self, wanted: str) -> str:
        base = UNSAFE_NAME_CHARS.sub("-", wanted).strip("-.") or "unnamed"
        name, number = base, 2
        while collision_key(name) in self.used:
            name, number = f"{base}-{number}", number + 1
        self.used.add(collision_key(name))
        return name


@dataclass
class _TargetPlan:
    """How one TargetEndpoint is generated: its request config, or None when it cannot be (already reported).

    ``url`` is the target URL as written when it has {variable} references; the
    request then sends to that URL, translated on each route's path.
    """

    config: str | None
    key: str | None
    timeout: bool
    url: str | None = None


class _ProjectBuilder:
    def __init__(self, bundle: Bundle, shared_flows: Sequence[Bundle], ai: Translator | None = None) -> None:
        self.bundle = bundle
        self.ai = ai
        self.policies = {policy.name: policy for policy in bundle.policies}
        self.shared: dict[str, list[Bundle]] = {}
        for shared in shared_flows:
            self.shared.setdefault(shared.name, []).append(shared)
        self.names = _Names()
        self.keys = _Names()
        self.props: dict[str, str] = {LISTENER_HOST_KEY: LISTENER_HOST, LISTENER_PORT_KEY: LISTENER_PORT}
        self.unsupported: list[UnsupportedItem] = []
        self._seen_items: set[tuple[str, str]] = set()
        self.globals: list[ET.Element] = []
        self.flows: list[ET.Element] = []
        self.sub_flows: list[ET.Element] = []
        self.sub_flow_names: dict[tuple[str, str], str] = {}
        self.in_progress: set[str] = set()
        self.targets: dict[str, TargetEndpoint] = {t.name: t for t in bundle.target_endpoints}
        self.target_plans: dict[str, _TargetPlan] = {}
        self.used_targets: set[str] = set()
        # Targets named by a RouteRule of a ProxyEndpoint that is not generated (see _skip_endpoint).
        self.dropped_route_targets: set[str] = set()
        self.names.take(LISTENER_CONFIG)
        self.records: list[PolicyResult] = []
        self.conditions: list[ConditionRecord] = []
        # What the generated policy steps need from the flows (see a2m.policies.common), and their globals.
        self.needs: set[str] = set()
        self.enterprise_components: tuple[str, ...] = ()
        self.enterprise_steps: tuple[str, ...] = ()
        self.policy_globals: list[ET.Element] = []
        self.global_names: dict[tuple[str, str], str] = {}
        self.property_notes: dict[str, str] = {}
        self.requests: list[ET.Element] = []
        self.listener_responses: list[ET.Element] = []
        # The request headers and query parameters the steps generated so far on the current path may change
        # (see RequestChanges): a request-side condition reading one of them can't be translated.
        self.changes: RequestChanges = NO_CHANGES
        self.shared_changes: dict[tuple[str, str], RequestChanges] = {}
        self.sub_flow_bases: dict[str, RequestChanges] = {}
        # While generating a branch that never runs in the generated app (a Flow or RouteRule whose condition can't
        # be translated is a #[false] branch), that Flow or RouteRule: every write of its steps counts as dropped.
        self.dead: str | None = None
        # The conditional flows generated as a #[false] branch (by id): their response steps never run either.
        self.dead_flows: set[int] = set()
        # Every request snapshot written (one per place a route ends); removed again when nothing reads them.
        self.snapshots: list[ET.Element] = []
        # The guard each generated when actually uses (a2m's translation, the AI's, or a refusal for #[false]), by
        # (id of the Step, Flow or RouteRule, side, changes it was read after), and every guard by owner and side:
        # the one source for whether a step, Flow or route runs in the generated app (see _runs_in_app).
        self.guards: dict[tuple[int, str, RequestChanges], Translation] = {}
        self.owner_guards: dict[tuple[int, str], list[Translation]] = {}
        # The checked write model of each AI-translated step (by id of the Step and side); None: it may change anything.
        self.write_models: dict[tuple[int, str], registry.WriteModel | None] = {}

    # ------------------------------------------------------------ results

    def skip(self, name: str, reason: str) -> None:
        if (name, reason) not in self._seen_items:
            self._seen_items.add((name, reason))
            self.unsupported.append(UnsupportedItem(name, reason))

    def _condition(
        self, name: str, kind: str, where: str, text: str | None, direction: str, *, ask_ai: bool = False
    ) -> Translation | None:
        """Translate and record a condition; None when there is none (absent or empty: Apigee runs always).

        ``ask_ai`` is True where the condition is generated into the app: one a2m's translator refuses then goes to
        the AI (see :meth:`_ai_may_read`), and the AI's expression is used when it is valid and not low confidence.
        """
        if text is None or not text.strip():
            return None
        translation = translate_condition(text, direction=direction, changes=self.changes)
        reason = translation.reason
        if translation.ok:
            record = ConditionRecord(name, kind, where, text, True, translation.dw, translation.reason)
        else:
            refusal = self._ai_refusal(text, direction) if ask_ai and self.ai is not None else None
            if ask_ai and self.ai is not None and refusal is None:
                return self._ai_condition(name, kind, where, text, direction, translation)
            if refusal is not None and refusal not in (reason or ""):
                reason = f"{reason}; not sent to the AI: {refusal}"
            record = ConditionRecord(name, kind, where, text, False, None, reason, method=Method.SKIPPED)
        self.conditions.append(record)
        return translation

    def _ai_refusal(self, text: str, direction: str) -> str | None:
        """Why condition ``text`` is not sent to the AI, or None when it may be: every variable it reads must be one
        a2m can read faithfully here. A built-in variable a2m has no mapping for does not exist in the generated app,
        and a value an earlier step may have changed would be read stale (the AI cannot see that change either), so
        no translation of the condition could be faithful."""
        if direction not in (REQUEST, RESPONSE):
            return "it is read in a fault rule"
        try:
            tokens = tokenize(text)
        except ConditionError as exc:
            return f"a2m cannot tell which values it reads ({exc})"
        for token in tokens:
            word = token.text
            if token.kind is not TokenKind.WORD or fold(word) in CONDITION_WORDS or NUMBER.fullmatch(word):
                continue
            try:
                accessor(word, direction, self.changes)
            except ConditionError as exc:
                return str(exc)
        return None

    def _emit_guard(self, owner: object, direction: str, changes: RequestChanges, translation: Translation) -> None:
        """Remember the guard a generated ``when`` of ``owner`` (a Step, Flow or RouteRule) uses."""
        self.guards[(id(owner), direction, changes)] = translation
        self.owner_guards.setdefault((id(owner), direction), []).append(translation)

    def _runs_in_app(self, owner: object, condition: str | None, direction: str, changes: RequestChanges) -> bool:
        """Whether ``owner`` (a Step, Flow or RouteRule with ``condition``, read after ``changes``) may run in the
        generated app: decided by the guard its generated ``when`` uses (a2m's or the AI's translation, or ``#[false]``),
        never by translating again. Read after other changes than where it was generated, it may run when any of its
        generated guards is not ``#[false]``; one not generated yet is decided by a2m's own translator."""
        if condition is None or not condition.strip():
            return True
        emitted = self.guards.get((id(owner), direction, changes))
        if emitted is not None:
            return emitted.ok
        others = self.owner_guards.get((id(owner), direction))
        if others:
            return any(guard.ok for guard in others)
        return translate_condition(condition, direction=direction, changes=changes).ok

    def _ai_condition(
        self, name: str, kind: str, where: str, text: str, direction: str, refused: Translation
    ) -> Translation:
        """Send a condition a2m's translator refused to the AI, record the result, and return what the generated
        ``when`` uses: the AI's expression, or a refusal (the ``when`` stays ``#[false]``)."""
        assert self.ai is not None
        refusal = refused.reason or "a2m's translator refused it"
        outcome = self.ai.expression(
            f"{kind} {name}", name, text, Place(self.bundle.name, where, direction), refusal, self.changes
        )
        if isinstance(outcome, NotTranslated):
            reason = f"{refusal}; {outcome.reason}"
            notes = outcome.notes
            if outcome.proposal is not None:
                notes = f"{notes} Proposed DataWeave (not used): {outcome.proposal}".strip()
            self.conditions.append(
                ConditionRecord(
                    name,
                    kind,
                    where,
                    text,
                    False,
                    None,
                    reason,
                    method=Method.AI,
                    confidence=outcome.confidence,
                    notes=notes,
                    needs_review=True,
                )
            )
            return Translation(text, ok=False, dw=None, reason=reason)
        dw = outcome.dataweave
        self.conditions.append(
            ConditionRecord(
                name,
                kind,
                where,
                text,
                True,
                dw,
                None,
                method=Method.AI,
                confidence=outcome.confidence,
                notes=outcome.notes,
            )
        )
        return Translation(
            text,
            ok=True,
            dw=dw,
            reason=f"translated by AI, confidence {outcome.confidence.value}",
            reads_request_snapshot=SNAPSHOT_VAR in dw,
        )

    def build(self) -> dict[str, str]:
        listener = _element(CORE, "configuration-properties", {"file": PROPERTIES_FILE})
        listener_config = _element(HTTP, "listener-config", {"name": LISTENER_CONFIG})
        _child(
            listener_config,
            HTTP,
            "listener-connection",
            {"host": f"${{{LISTENER_HOST_KEY}}}", "port": f"${{{LISTENER_PORT_KEY}}}"},
        )
        for endpoint in self.bundle.proxy_endpoints:
            reason = unsupported_base_path(endpoint.base_path)
            if reason is None:
                self.flows.append(self._endpoint_flow(endpoint))
            else:
                self._skip_endpoint(endpoint, reason)
        self._report_unused()
        self._wire_policy_needs()

        files: dict[str, str] = {}
        main = [listener, listener_config, *self.globals, *self.policy_globals, *self.flows, *self.sub_flows]
        _drop_undeclared_error_handlers(main)
        components, steps = _enterprise_uses(main)
        self.enterprise_components, self.enterprise_steps = tuple(sorted(components)), tuple(steps)
        files["/".join((*MULE_DIR, PROXY_FILE))] = _mule_document(main)
        configs = sorted(path.rsplit("/", 1)[-1] for path in files)
        files["/".join((*RESOURCES_DIR, PROPERTIES_FILE))] = _properties_text(self.props, self.property_notes)
        files["mule-artifact.json"] = _artifact_json(configs, enterprise=bool(components))
        files["pom.xml"] = _pom_text(self.bundle, [ns for ns in MODULE_DEPENDENCIES if _uses(main, ns)])
        return files

    def _wire_policy_needs(self) -> None:
        """Connect the generated policy steps to the flows: changed request parts, fault answers, reason phrases,
        the request snapshot."""
        if NEED_REQUEST_SNAPSHOT not in self.needs:
            snapshots = {id(snapshot) for snapshot in self.snapshots}
            for root in [*self.flows, *self.sub_flows]:
                for parent in root.iter():
                    for node in [c for c in parent if id(c) in snapshots]:
                        parent.remove(node)
        for request in self.requests:
            headers = request.find(f"{{{HTTP}}}headers")
            query = request.find(f"{{{HTTP}}}query-params")
            if NEED_REQUEST_HEADERS in self.needs and headers is not None:
                headers.text = CHANGED_REQUEST_HEADERS
            if NEED_REQUEST_QUERY in self.needs and query is not None:
                query.text = CHANGED_REQUEST_QUERY
        if NEED_REASON_PHRASE in self.needs:
            for response in self.listener_responses:
                response.set("reasonPhrase", f"#[vars.{REASON_PHRASE_VAR}]")
        if NEED_FAULT in self.needs:
            # A policy step that rejects the call has set the status, headers and body of its answer; the
            # handler ends the flow normally, so the listener sends exactly that.
            for flow in self.flows:
                handler = _child(flow, CORE, "error-handler")
                continuing = _child(
                    handler, CORE, "on-error-continue", {"type": FAULT_ERROR_TYPE, "logException": "false"}
                )
                _child(
                    continuing,
                    CORE,
                    "logger",
                    {"level": "DEBUG", "message": "A policy step answered the call with its fault response"},
                )

    # ------------------------------------------------------------ proxy endpoints

    def _endpoint_flow(self, endpoint: ProxyEndpoint) -> ET.Element:
        where = f"ProxyEndpoint {endpoint.name}"
        flow = _element(CORE, "flow", {"name": self.names.take(f"proxy-{endpoint.name}")})
        listener = _child(
            flow, HTTP, "listener", {"config-ref": LISTENER_CONFIG, "path": listener_path(endpoint.base_path)}
        )
        response = _child(listener, HTTP, "response", {"statusCode": "#[vars.httpStatus default 200]"})
        _child(response, HTTP, "headers").text = "#[vars.responseHeaders default {}]"
        self.listener_responses.append(response)
        error = _child(listener, HTTP, "error-response", {"statusCode": f"#[{_error_status()}]"})
        _child(error, HTTP, "body").text = _error_body()
        self._check_endpoint_settings(endpoint, where)

        self.changes = NO_CHANGES
        flow.extend(self._steps(endpoint.pre_flow.request, f"{where} PreFlow request", REQUEST))
        flow_choice, matched = self._conditional_flows(endpoint.flows, where, PROXY_FLOW_VAR)
        flow.extend(flow_choice)
        flow.extend(self._steps(endpoint.post_flow.request, f"{where} PostFlow request", REQUEST))
        flow.extend(self._routing(endpoint, where))
        # Apigee's response order: the target's response steps (inside the routing above), then this
        # endpoint's PreFlow, matched conditional Flow and PostFlow response steps.
        flow.extend(self._steps(endpoint.pre_flow.response, f"{where} PreFlow response", RESPONSE))
        flow.extend(self._flow_responses(matched, where, PROXY_FLOW_VAR))
        flow.extend(self._steps(endpoint.post_flow.response, f"{where} PostFlow response", RESPONSE))
        if endpoint.post_client_flow is not None:
            # It runs after the response is sent: its writes reach nothing generated.
            path = self.changes
            self._skip_flow_steps(endpoint.post_client_flow, f"{where} PostClientFlow")
            self.changes = path
        self._skip_fault_rules(endpoint, where)
        return flow

    def _skip_endpoint(self, endpoint: ProxyEndpoint, reason: str) -> None:
        """Report a ProxyEndpoint whose base path has no Mule listener path, and everything in it."""
        where = f"ProxyEndpoint {endpoint.name}"
        base = endpoint.base_path or ""
        self.changes = NO_CHANGES
        self.skip(
            f"BasePath {base}",
            f"{where}: base path {base} cannot be a Mule listener path ({reason}), "
            "so this ProxyEndpoint is not generated and none of its requests are forwarded",
        )
        self._skip_flow_steps(endpoint.pre_flow, f"{where} PreFlow")
        self._skip_flows(endpoint.flows, where)
        self._skip_flow_steps(endpoint.post_flow, f"{where} PostFlow")
        if endpoint.post_client_flow is not None:
            self._skip_flow_steps(endpoint.post_client_flow, f"{where} PostClientFlow")
        self._skip_fault_rules(endpoint, where)
        for rule in endpoint.route_rules:
            self.skip(rule.name, f"{where}: RouteRule {rule.name} is not generated, since its ProxyEndpoint is not")
            self._condition(rule.name, "RouteRule", where, rule.condition, REQUEST)
            if rule.target is not None:
                self.dropped_route_targets.add(rule.target)

    def _check_endpoint_settings(self, endpoint: ProxyEndpoint, where: str) -> None:
        for host in endpoint.virtual_hosts:
            if host != "default":
                self.skip(
                    f"VirtualHost {host}",
                    f"{where} uses virtual host {host}; the app listens on one plain HTTP port "
                    f"({LISTENER_PORT_KEY} in {PROPERTIES_FILE}) and does not copy virtual hosts",
                )
        for key, value in endpoint.properties.items():
            self.skip(key, f"{where} setting {key}={value} is not carried over")
        modeled = {"BasePath", "VirtualHost", "Properties"}
        for child in endpoint.connection.children if endpoint.connection is not None else ():
            if child.tag not in modeled:
                self.skip(child.tag, f"{where}: <{child.tag}> in HTTPProxyConnection is not carried over")
        self._skip_other_elements(endpoint.other_elements, where)

    def _skip_other_elements(self, elements: Sequence[XmlElement], where: str) -> None:
        for element in elements:
            if element.tag != "Description":
                self.skip(element.tag, f"{where}: <{element.tag}> is not carried over")

    def _skip_flows(self, flows: Sequence[Flow], where: str) -> None:
        """Report the conditional flows of an endpoint that is not generated, and everything in them."""
        for flow in flows:
            condition = f" (condition {flow.condition})" if flow.condition else " (no condition)"
            self.skip(
                flow.name,
                f"{where}: flow {flow.name}{condition} is not generated, since {where} is not; "
                "its steps are listed separately",
            )
            self._condition(flow.name, "Flow", where, flow.condition, REQUEST)
            self._skip_flow_steps(FlowSteps(flow.request, flow.response), f"{where} flow {flow.name}")

    def _skip_flow_steps(self, steps: FlowSteps, where: str) -> None:
        for direction, items in ((REQUEST, steps.request), (RESPONSE, steps.response)):
            self._skip_steps(items, f"{where} {direction}", direction=direction)

    def _skip_steps(
        self, steps: Sequence[Step], where: str, policies: dict[str, Policy] | None = None, *, direction: str
    ) -> None:
        """Report steps that are not generated. Apigee still runs them, so everything each may write counts as
        dropped for what is read after it (a caller whose skipped part is off the generated path restores
        :attr:`changes` afterwards)."""
        policies = self.policies if policies is None else policies
        for step in steps:
            reason = f"{where}: step {step.name} is in a part of the flow that is not generated yet"
            self.skip(step.name, reason)
            self._record_skipped(step, policies, where, reason, direction)
            dropped = self._dropped_writes(step, policies, direction, self.changes, set(self.in_progress), runs=False)
            self.changes = self.changes | self._step_changes(step, policies, set(), direction) | dropped

    def _record_skipped(
        self, step: Step, policies: dict[str, Policy], where: str, reason: str, direction: str
    ) -> None:
        policy = policies.get(step.policy)
        kind = policy.type if policy is not None else "unknown"
        self._condition(step.name, "Step", where, step.condition, direction)
        self.records.append(
            PolicyResult(step.name, kind, Method.SKIPPED, reason, location=where, condition=step.condition)
        )

    def _skip_fault_rules(self, endpoint: ProxyEndpoint | TargetEndpoint, where: str) -> None:
        # A fault rule may run before or after the target call, so whether the request snapshot exists is not
        # known: its conditions are read with the FAULT direction, which refuses request variables. A fault rule's
        # steps count as writers for the fault rules after it, never for the generated path.
        path = self.changes
        self._fault_rules(endpoint, where)
        self.changes = path

    def _fault_rules(self, endpoint: ProxyEndpoint | TargetEndpoint, where: str) -> None:
        for rule in endpoint.fault_rules:
            self.skip(rule.name, f"{where}: fault rule {rule.name} is not generated yet")
            self._condition(rule.name, "FaultRule", where, rule.condition, FAULT)
            self._skip_steps(rule.steps, f"{where} fault rule {rule.name}", direction=FAULT)
        default = endpoint.default_fault_rule
        if default is not None:
            self.skip(default.name, f"{where}: default fault rule {default.name} is not generated yet")
            self._condition(default.name, "DefaultFaultRule", where, default.condition, FAULT)
            self._skip_steps(default.steps, f"{where} default fault rule {default.name}", direction=FAULT)

    # ------------------------------------------------------------ conditional flows

    def _conditional_flows(
        self, flows: Sequence[Flow], where: str, var: str
    ) -> tuple[list[ET.Element], list[Flow]]:
        """The processors that run the first conditional flow whose condition matches, and the flows they can run.

        Apigee tries the flows in order on the request and runs the first that
        matches; a flow without a condition always matches, so the flows after it
        never run (reported). Each branch records the flow's name in ``var`` for
        :meth:`_flow_responses`. A flow whose condition can't be translated is a
        ``#[false]`` branch: it never runs.
        """
        if not flows:
            return [], []
        choice = _element(CORE, "choice")
        matched: list[Flow] = []
        fallback: Flow | None = None
        # Each flow's condition and steps start from the changes made before the choice (a flow tried earlier did
        # not match, so its steps never ran); after the choice, any branch may have run.
        before = after = self.changes
        outer = self.dead
        for flow in flows:
            self.changes = before
            if fallback is not None:
                self.skip(
                    flow.name,
                    f"{where}: flow {flow.name} comes after flow {fallback.name}, which has no condition, "
                    "so Apigee never runs it",
                )
                self._condition(flow.name, "Flow", where, flow.condition, REQUEST)
                self._skip_flow_steps(FlowSteps(flow.request, flow.response), f"{where} flow {flow.name}")
                continue
            translation = self._condition(flow.name, "Flow", where, flow.condition, REQUEST, ask_ai=True)
            if translation is not None:
                self._emit_guard(flow, REQUEST, before, translation)
            if translation is None:
                fallback = flow
                branch = _element(CORE, "otherwise")
            else:
                branch = _element(
                    CORE, "when", _guard(f"Flow {flow.name}", str(flow.condition), translation, "this flow never runs")
                )
            matched.append(flow)
            if translation is not None and not translation.ok:
                # A #[false] branch: in Apigee the flow may run, so every write of its steps is dropped.
                self.dead_flows.add(id(flow))
            else:
                # A target's flows are generated once per route, each time after that route's changes.
                self.dead_flows.discard(id(flow))
            self.dead = outer or (f"Flow {flow.name}" if id(flow) in self.dead_flows else None)
            branch.append(_element(CORE, "set-variable", {"variableName": var, "value": f"#[{dw_string(flow.name)}]"}))
            branch.extend(self._steps(flow.request, f"{where} flow {flow.name} request", REQUEST))
            self.dead = outer
            after = after | self.changes
            choice.append(branch)
        self.changes = after
        if choice[0].tag == "otherwise":
            # The first flow has no condition: it always runs, so no choice is needed.
            return list(choice[0]), matched
        return [choice], matched

    def _flow_responses(self, matched: Sequence[Flow], where: str, var: str) -> list[ET.Element]:
        """The response steps of the conditional flow :meth:`_conditional_flows` ran, picked by its name."""
        flows = [flow for flow in matched if flow.response]
        if not flows:
            return []
        choice = _element(CORE, "choice")
        # Only the matched flow's response steps run: each branch starts from the same changes.
        before = after = self.changes
        for flow in flows:
            when = _child(
                choice,
                CORE,
                "when",
                {"expression": f"#[vars.{var} == {dw_string(flow.name)}]", DOC_DESCRIPTION: _attribute_text(f"Flow {flow.name}")},
            )
            self.changes = before
            outer = self.dead
            self.dead = outer or (f"Flow {flow.name}" if id(flow) in self.dead_flows else None)
            when.extend(self._steps(flow.response, f"{where} flow {flow.name} response", RESPONSE))
            self.dead = outer
            after = after | self.changes
            _ensure_processor(when, f"flow {flow.name} response")
        self.changes = after
        return [choice]

    # ------------------------------------------------------------ routing

    def _routing(self, endpoint: ProxyEndpoint, where: str) -> list[ET.Element]:
        """The processors that pick a route: inline for one unconditional route, else a choice router."""
        conditional: list[tuple[RouteRule, str, Translation]] = []
        fallback: RouteRule | None = None
        for rule in endpoint.route_rules:
            translation = self._condition(
                rule.name, "RouteRule", where, rule.condition, REQUEST, ask_ai=fallback is None
            )
            if fallback is not None:
                self.skip(
                    rule.name,
                    f"{where}: RouteRule {rule.name} comes after the unconditional RouteRule {fallback.name}, "
                    "so Apigee never reaches it",
                )
            elif translation is None:
                fallback = rule
            else:
                self._emit_guard(rule, REQUEST, self.changes, translation)
                conditional.append((rule, rule.condition or "", translation))
        if not endpoint.route_rules:
            return self._null_route()
        if not conditional and fallback is not None:
            return self._route(fallback, where)
        choice = _element(CORE, "choice")
        # Each route starts from the changes made before routing; after it, any route may have run.
        before = after = self.changes
        outer = self.dead
        for rule, condition, translation in conditional:
            guard = _guard(f"RouteRule {rule.name}", condition, translation, "this branch is never taken")
            self.changes = before
            # A #[false] branch: in Apigee the route may be taken, so every write of its target's steps is dropped.
            self.dead = outer or (None if translation.ok else f"RouteRule {rule.name}")
            _child(choice, CORE, "when", guard).extend(self._route(rule, where))
            self.dead = outer
            after = after | self.changes
        otherwise = _child(choice, CORE, "otherwise")
        self.changes = before
        if fallback is not None:
            otherwise.extend(self._route(fallback, where))
        else:
            self.skip(
                f"{endpoint.name} no matching route",
                f"{where}: every RouteRule has a condition; when none matches the app answers 500 "
                "with a fault body (Apigee would send no target request)",
            )
            otherwise.extend(self._no_call(_fault(NO_ROUTE_FAULT)))
        self.changes = after | self.changes
        return [choice]

    def _null_route(self) -> list[ET.Element]:
        """No target: answer with an empty body instead of echoing the request back."""
        return self._no_call([_element(CORE, "set-payload", {"value": ""})])

    def _snapshot(self) -> ET.Element:
        """Save the request as it stands (as sent, right before a target call) for response-side reads."""
        snapshot = _element(CORE, "set-variable", {"variableName": SNAPSHOT_VAR, "value": REQUEST_SNAPSHOT})
        self.snapshots.append(snapshot)
        return snapshot

    def _no_call(self, processors: list[ET.Element]) -> list[ET.Element]:
        """``processors`` that end a route without a target call, after the request snapshot."""
        return [self._snapshot(), *processors]

    def _route(self, rule: RouteRule, where: str) -> list[ET.Element]:
        if rule.url is not None and rule.target is None:
            self.skip(
                rule.name,
                f"{where}: RouteRule {rule.name} routes straight to the URL {shown_url(rule.url)}; not generated yet",
            )
            return self._no_call(_fault(NO_TARGET_FAULT))
        if rule.target is None:
            return self._null_route()
        target = self.targets[rule.target]
        self.used_targets.add(target.name)
        return self._target_processors(target)

    # ------------------------------------------------------------ target endpoints

    def _target_plan(self, target: TargetEndpoint) -> _TargetPlan:
        """Create the request config and properties of ``target`` once; report what is not carried over."""
        plan = self.target_plans.get(target.name)
        if plan is not None:
            return plan
        where = f"TargetEndpoint {target.name}"
        address = self._target_address(target)
        if isinstance(address, str):
            self.skip(target.name, f"{where} is not generated: {address}")
            self._skip_target_extras(target, where)
            plan = _TargetPlan(None, None, timeout=False)
        elif isinstance(address, _TemplateAddress):
            key = self.keys.take(target.name.replace(".", "_"))
            prefix = f"target.{key}"
            config = _element(HTTP, "request-config", {"name": self.names.take(f"target-{target.name}-config")})
            _child(config, HTTP, "request-connection", {"protocol": address.protocol})
            self.globals.append(config)
            timeout = self._target_settings(target, where, prefix)
            plan = _TargetPlan(config.get("name"), prefix, timeout, url=address.url)
        else:
            key = self.keys.take(target.name.replace(".", "_"))  # a dot would split the key
            prefix = f"target.{key}"
            self.props[f"{prefix}.protocol"] = address.protocol
            self.props[f"{prefix}.host"] = address.host
            self.props[f"{prefix}.port"] = address.port
            self.props[f"{prefix}.basePath"] = address.base_path
            config = _element(
                HTTP,
                "request-config",
                {"name": self.names.take(f"target-{target.name}-config"), "basePath": f"${{{prefix}.basePath}}"},
            )
            _child(
                config,
                HTTP,
                "request-connection",
                {"protocol": f"${{{prefix}.protocol}}", "host": f"${{{prefix}.host}}", "port": f"${{{prefix}.port}}"},
            )
            self.globals.append(config)
            timeout = self._target_settings(target, where, prefix)
            plan = _TargetPlan(config.get("name"), prefix, timeout)
        self.target_plans[target.name] = plan
        return plan

    def _target_address(self, target: TargetEndpoint) -> _Address | _TemplateAddress | str:
        connection = target.connection
        if connection is None:
            return "it has no HTTPTargetConnection"
        if connection.tag != "HTTPTargetConnection":
            return f"it uses <{connection.tag}> (proxy chaining), which is not generated"
        if target.url is None:
            servers = sorted(
                {
                    s.attributes.get("name", "")
                    for child in connection.children
                    for s in child.children
                    if child.tag == "LoadBalancer" and s.tag == "Server"
                }
                - {""}
            )
            if servers:
                return (
                    f"it has no fixed URL; it uses a LoadBalancer with target server {', '.join(servers)}, "
                    "whose address is an Apigee environment setting a2m cannot see"
                )
            return "it has no URL"
        if "{" in target.url or "}" in target.url:
            return _template_url(target.url)
        return _parse_url(target.url)

    def _target_settings(self, target: TargetEndpoint, where: str, prefix: str) -> bool:
        """Carry io.timeout.millis over as the request's responseTimeout; report every other setting."""
        timeout = False
        for key, value in target.properties.items():
            if key == TIMEOUT_PROPERTY and value.isdigit():
                self.props[f"{prefix}.responseTimeout"] = value
                timeout = True
            else:
                self.skip(key, f"{where} setting {key}={value} is not carried over")
        modeled = {"URL", "Properties"}
        for child in target.connection.children if target.connection is not None else ():
            if child.tag not in modeled:
                self.skip(
                    f"{target.name} {child.tag}", f"{where}: <{child.tag}> in HTTPTargetConnection is not carried over"
                )
        if target.url is not None and urlsplit(target.url.strip()).query:
            self.skip(
                f"{target.name} URL query",
                f"{where}: the query string in its URL {shown_url(target.url)} is not carried over",
            )
        self._skip_target_extras(target, where)
        return timeout

    def _skip_target_extras(self, target: TargetEndpoint, where: str) -> None:
        self._skip_other_elements(target.other_elements, where)
        self._skip_fault_rules(target, where)
        if target.event_flow is not None:
            path = self.changes
            self._skip_flow_steps(target.event_flow, f"{where} EventFlow")
            self.changes = path

    def _target_processors(self, target: TargetEndpoint) -> list[ET.Element]:
        where = f"TargetEndpoint {target.name}"
        plan = self._target_plan(target)
        if plan.config is None or plan.key is None:
            self._skip_flow_steps(target.pre_flow, f"{where} PreFlow")
            self._skip_flows(target.flows, where)
            self._skip_flow_steps(target.post_flow, f"{where} PostFlow")
            return self._no_call(_fault(NO_TARGET_FAULT))
        processors = self._steps(target.pre_flow.request, f"{where} PreFlow request", REQUEST)
        flow_choice, matched = self._conditional_flows(target.flows, where, TARGET_FLOW_VAR)
        processors += flow_choice
        processors += self._steps(target.post_flow.request, f"{where} PostFlow request", REQUEST)
        attrib = {
            "config-ref": plan.config,
            "method": "#[attributes.method]",
            "path": REQUEST_PATH,
            # Apigee hands a target's 3xx and its Location back to the caller; it never follows it.
            "followRedirects": "false",
        }
        if plan.url is not None:
            url = self._target_url(target, plan.url, where)
            if url is None:
                for direction, steps in (
                    ("PreFlow response", target.pre_flow.response),
                    *((f"flow {f.name} response", f.response) for f in matched),
                    ("PostFlow response", target.post_flow.response),
                ):
                    self._skip_steps(steps, f"{where} {direction}", direction=RESPONSE)
                return processors + self._no_call(_fault(NO_TARGET_FAULT))
            del attrib["path"]
            attrib = {**attrib, "url": url}
        if plan.timeout:
            attrib["responseTimeout"] = f"${{{plan.key}.responseTimeout}}"
        request = _element(HTTP, "request", attrib)
        _child(request, HTTP, "headers").text = REQUEST_HEADERS
        _child(request, HTTP, "query-params").text = REQUEST_QUERY
        self.requests.append(request)
        validator = _child(request, HTTP, "response-validator")
        # Apigee passes the target's 4xx and 5xx answers to the caller unchanged.
        _child(validator, HTTP, "success-status-code-validator", {"values": "0..599"})
        processors.append(self._snapshot())
        processors.append(request)
        processors.append(
            _element(CORE, "set-variable", {"variableName": "httpStatus", "value": "#[attributes.statusCode]"})
        )
        processors.append(
            _element(CORE, "set-variable", {"variableName": "responseHeaders", "value": RESPONSE_HEADERS})
        )
        processors += self._steps(target.pre_flow.response, f"{where} PreFlow response", RESPONSE)
        processors += self._flow_responses(matched, where, TARGET_FLOW_VAR)
        processors += self._steps(target.post_flow.response, f"{where} PostFlow response", RESPONSE)
        return processors

    def _target_url(self, target: TargetEndpoint, url: str, where: str) -> str | None:
        """The http:request url for a target URL with {variable} references, on the current path; None (reported)
        when a reference reads a request header or query parameter an earlier step may have changed.

        As for a fixed URL, the request's path below the base path is appended
        (one slash where the URL ends in '/' and the path starts with one), and
        the query parameters are sent with http:query-params.
        """
        translation = translate_template(url, direction=REQUEST, changes=self.changes)
        if not translation.ok or translation.dw is None:
            self.skip(
                f"{target.name} URL",
                f"{where}: its URL {shown_url(url)} can't be translated on this route ({translation.reason}); "
                "the route answers 500 with a fault body instead of calling it",
            )
            return None
        return (
            f"#[do {{ var base = {translation.dw} var suffix = {REQUEST_PATH_DW} --- "
            "if ((base endsWith '/') and (suffix startsWith '/')) base[0 to -2] ++ suffix else base ++ suffix }]"
        )

    # ------------------------------------------------------------ steps and shared flows

    def _steps(
        self,
        steps: Sequence[Step],
        where: str,
        direction: str,
        policies: dict[str, Policy] | None = None,
        resources: Sequence[Resource] | None = None,
    ) -> list[ET.Element]:
        """Processors for ``steps``, one labelled processor (or a comment, when skipped) per step, in order.

        ``policies`` and ``resources`` are those of the bundle the steps belong to (default: the proxy's).
        """
        policies = self.policies if policies is None else policies
        resources = self.bundle.resources if resources is None else resources
        found: list[ET.Element] = []
        for index, step in enumerate(steps):
            before = self.changes
            policy = policies.get(step.policy)
            if policy is None:
                reason = f"policy {step.policy} is not in the bundle"
                found.append(self._skipped_step(step, "unknown", reason, where, direction))
            elif not policy.enabled:
                reason = f"policy {policy.name} is disabled (enabled=false), so nothing is generated"
                found.append(self._skipped_step(step, policy.type, reason, where, direction))
            elif policy.type == FLOW_CALLOUT:
                found.append(self._flow_callout(step, policy, where, direction))
            elif self.ai is not None and callout_kind(policy) is not None:
                neighbours = (
                    steps[index - 1].name if index > 0 else NONE,
                    steps[index + 1].name if index + 1 < len(steps) else NONE,
                )
                found.append(self._ai_step(step, policy, where, direction, resources, neighbours))
            else:
                output = registry.translate(policy, direction=direction, changes=self.changes)
                found.extend(_untranslated_settings(step.name, output.result.unsupported_options))
                found.append(self._policy_step(step, output, where, direction))
            # Counted whether or not the step is generated, and even under a condition: in Apigee it may have
            # changed the request (a response-side step too, with AssignTo type="request"), so a later read must
            # not see the caller's original value. The same for a flow variable it may write where the generated
            # step does not (recomputed from the same inputs the step was generated from).
            dropped = self._dropped_writes(
                step, policies, direction, before, set(self.in_progress), runs=self.dead is None
            )
            if self.dead is not None:
                dropped = _in_dead_branch(dropped, self.dead)
            self.changes = self.changes | self._step_changes(step, policies, set(), direction) | dropped
        return found

    def _dropped_writes(
        self,
        step: Step,
        policies: dict[str, Policy],
        direction: str,
        changes: RequestChanges,
        visiting: set[str],
        *,
        runs: bool = True,
        base: RequestChanges | None = None,
    ) -> RequestChanges:
        """The proxy's own flow variables ``step`` may write in Apigee where its generated step does not write them
        exactly, as :attr:`RequestChanges.variables`, for the step generated after ``changes``.

        A write counts unless the step is generated (not skipped), its condition
        translates (a step whose condition can't be translated never runs), and
        its template writes that variable exactly (:attr:`TemplateOutput.written`).
        ``runs`` False (the step is not generated, or sits in a branch that
        never runs) counts every write. A policy a2m has no write model for
        (:func:`registry.has_write_model`) may write anything. ``visiting`` are the shared flows being
        generated (a call into one of them is refused); ``base`` is what a shared
        flow's sub-flow is generated after (see :meth:`_sub_flow_base`).
        """
        policy = policies.get(step.policy)
        if policy is None or not policy.enabled:
            return NO_CHANGES
        if runs:
            runs = self._runs_in_app(step, step.condition, direction, changes)
        if policy.type == FLOW_CALLOUT:
            return self._callout_dropped(step, policy, direction, visiting, runs, base)
        model = self.write_models.get((id(step), direction))
        writes = registry.variable_writes(policy, direction=direction, declared=model)
        if not writes:
            return NO_CHANGES
        written: frozenset[str] = frozenset()
        if runs and model is not None:
            written = model.written
        elif runs:
            output = registry.translate(policy, direction=direction, changes=changes)
            if output.result.method is Method.TEMPLATE and output.processors:
                written = output.written
        return RequestChanges(variables=frozenset((name, step.name) for name in writes - written))

    def _callout_dropped(
        self,
        step: Step,
        policy: Policy,
        direction: str,
        visiting: set[str],
        runs: bool,
        base: RequestChanges | None,
    ) -> RequestChanges:
        """The flow variable writes a FlowCallout step (its Parameters, which are not passed, and the steps of the
        shared flow it calls) may make in Apigee that the generated app does not make (see :meth:`_dropped_writes`)."""
        parameters = {
            fold(item.attributes.get("name", "").strip())
            for group in policy.settings.children
            if group.tag == "Parameters"
            for item in group.children
            if item.tag == "Parameter" and item.attributes.get("name", "").strip()
        }
        dropped = RequestChanges(variables=frozenset((name, step.name) for name in parameters))
        name = step.shared_flow
        bundles = self.shared.get(name, []) if name is not None else []
        if name is None or len(bundles) != 1:
            # Not generated, and what the shared flow would write is not known.
            return dropped | RequestChanges(variables=frozenset({(ANY, step.name)}))
        shared = bundles[0]
        if shared.name in visiting:
            # A call from inside itself is refused; its steps are counted where the shared flow runs.
            return dropped
        if base is not None:
            context = base
        else:
            # With runs False nothing is translated, so the sub-flow base is not needed.
            context = self._sub_flow_base(direction) if runs else NO_CHANGES
        return dropped | self._shared_flow_dropped(shared, direction, visiting | {shared.name}, runs, context)

    def _shared_flow_dropped(
        self, shared: Bundle, direction: str, visiting: set[str], runs: bool, base: RequestChanges
    ) -> RequestChanges:
        """The flow variable writes the entry flow of ``shared`` may make in Apigee that its sub-flow, generated
        after ``base``, does not make; with ``runs`` False, every write."""
        flows = list(shared.shared_flows)
        entry = next((f for f in flows if f.name == SHARED_FLOW_ENTRY), flows[0] if flows else None)
        policies = {policy.name: policy for policy in shared.policies}
        context, found = base, NO_CHANGES
        for step in entry.steps if entry is not None else ():
            dropped = self._dropped_writes(step, policies, direction, context, visiting, runs=runs, base=base)
            found = found | dropped
            context = context | self._step_changes(step, policies, set(), direction) | dropped
        return found

    def _sub_flow_base(self, direction: str) -> RequestChanges:
        """What a shared flow's sub-flow on the ``direction`` side is generated after: every request change any step
        of the proxy may make (:meth:`_proxy_request_changes`) and every flow variable write any step (shared flows
        included) may make in Apigee that the generated app does not, found by repeating until nothing is added
        (a dropped write can make a later step's condition or value untranslatable, which drops its writes too)."""
        known = self.sub_flow_bases.get(direction)
        if known is not None:
            return known
        context = self._proxy_request_changes(direction)
        while True:
            found = context
            for side, step, runs in self._proxy_steps(direction, context):
                found = found | self._dropped_writes(step, self.policies, side, context, set(), runs=runs, base=context)
            if found == context:
                break
            context = found
        self.sub_flow_bases[direction] = context
        return context

    def _step_changes(
        self, step: Step, policies: dict[str, Policy], visiting: set[str], direction: str = REQUEST
    ) -> RequestChanges:
        """The request headers, query parameters and verb ``step`` (on the ``direction`` side) may change in Apigee."""
        policy = policies.get(step.policy)
        if policy is None or not policy.enabled:
            return NO_CHANGES
        if policy.type != FLOW_CALLOUT:
            model = self.write_models.get((id(step), direction))
            return registry.request_changes(policy, direction=direction, declared=model)
        name = step.shared_flow
        bundles = self.shared.get(name, []) if name is not None else []
        if name is None or len(bundles) != 1:
            # Not generated, and what the shared flow would change is not known: anything.
            every = RequestChanges.everything(step.name)
            return RequestChanges(every.headers, every.queries, every.verb)
        return self._shared_flow_changes(bundles[0], visiting, direction)

    def _shared_flow_changes(self, shared: Bundle, visiting: set[str], direction: str = REQUEST) -> RequestChanges:
        """What the entry flow of ``shared`` (and the shared flows it calls) may change in the request when it is
        called on the ``direction`` side."""
        known = self.shared_changes.get((shared.name, direction))
        if known is not None:
            return known
        if shared.name in visiting:
            return NO_CHANGES  # a call cycle: the generator refuses it, and its steps are counted once already
        top = not visiting
        visiting.add(shared.name)
        flows = list(shared.shared_flows)
        entry = next((f for f in flows if f.name == SHARED_FLOW_ENTRY), flows[0] if flows else None)
        policies = {policy.name: policy for policy in shared.policies}
        changes = NO_CHANGES
        for step in entry.steps if entry is not None else ():
            changes = changes | self._step_changes(step, policies, visiting, direction)
        visiting.discard(shared.name)
        if top:
            # A result found inside a cycle can miss the steps of the flow that was cut off; only a complete one is kept.
            self.shared_changes[(shared.name, direction)] = changes
        return changes

    def _proxy_request_changes(self, direction: str = REQUEST) -> RequestChanges:
        """Every request change any step of the proxy may make before a step on the ``direction`` side runs, in
        any order: the request-side steps, and for the response side the response-side steps as well."""
        changes = NO_CHANGES
        for side, step, _ in self._proxy_steps(direction):
            changes = changes | self._step_changes(step, self.policies, set(), side)
        return changes

    def _proxy_steps(
        self, direction: str = REQUEST, context: RequestChanges | None = None
    ) -> Iterator[tuple[str, Step, bool]]:
        """(side, step, runs) for every step of the proxy that may run before a step on the ``direction`` side: the
        request-side steps, and for the response side the response-side steps as well.

        ``runs`` is False for a step the generated app may never run where
        Apigee does: in a ProxyEndpoint or TargetEndpoint that is not generated,
        or, with ``context`` (the changes the conditions are read after), in a
        Flow or behind a RouteRule whose condition can't be translated.
        """

        def can_run(owner: Flow | RouteRule) -> bool:
            if context is None:
                return True
            return self._runs_in_app(owner, owner.condition, REQUEST, context)

        live: dict[int, bool] = {}
        for proxy in self.bundle.proxy_endpoints:
            generated = unsupported_base_path(proxy.base_path) is None
            live[id(proxy)] = generated
            for rule in proxy.route_rules:
                target = self.targets.get(rule.target) if rule.target is not None else None
                if target is not None and not (generated and can_run(rule)):
                    live[id(target)] = False
        for target in self.bundle.target_endpoints:
            if isinstance(self._target_address(target), str):
                live[id(target)] = False
        endpoints: list[ProxyEndpoint | TargetEndpoint] = [*self.bundle.proxy_endpoints, *self.bundle.target_endpoints]
        for endpoint in endpoints:
            runs = live.get(id(endpoint), True)
            groups = [(REQUEST, runs, endpoint.pre_flow.request), (REQUEST, runs, endpoint.post_flow.request)]
            flows = [(f, runs and can_run(f)) for f in endpoint.flows]
            groups += [(REQUEST, flow_runs, f.request) for f, flow_runs in flows]
            if direction == RESPONSE:
                groups += [(RESPONSE, runs, endpoint.pre_flow.response), (RESPONSE, runs, endpoint.post_flow.response)]
                groups += [(RESPONSE, flow_runs, f.response) for f, flow_runs in flows]
            for side, group_runs, steps in groups:
                for step in steps:
                    yield side, step, group_runs

    def _skipped_step(
        self,
        step: Step,
        kind: str,
        reason: str,
        where: str,
        direction: str,
        options: tuple[UnsupportedOption, ...] = (),
    ) -> ET.Element:
        """Report a step that is not generated; the comment left at its place names the step and its type.

        ``options`` are the settings the template listed on its way to skipping the step (kept in its record).
        """
        self.skip(step.name, f"{where}: {reason}")
        self._condition(step.name, "Step", where, step.condition, direction)
        condition = f" It has the condition {step.condition}." if step.condition is not None else ""
        self.records.append(
            PolicyResult(
                step.name,
                kind,
                Method.SKIPPED,
                reason,
                unsupported_options=options,
                location=where,
                condition=step.condition,
            )
        )
        comment = ET.Comment(_comment_text(f"Step {step.name} ({kind}) is not generated: {reason}.{condition}"))
        return cast(ET.Element, comment)

    def _policy_step(self, step: Step, output: TemplateOutput, where: str, direction: str) -> ET.Element:
        result = output.result
        if result.method is not Method.TEMPLATE or not output.processors:
            reason = result.reason or "nothing could be generated"
            return self._skipped_step(step, result.type, reason, where, direction, result.unsupported_options)
        for option in result.unsupported_options:
            self.skip(f"{step.name} {option.name}", f"{where}: {result.type} step {step.name}: {option.reason}")
        names = self._take_globals(step.policy, output)
        processors = [_adopt(p, names) for p in output.processors]
        for key, value in output.properties.items():
            self.props.setdefault(key, value)
        for key, note in output.property_notes.items():
            self.property_notes.setdefault(key, note)
        self.needs.update(output.needs)
        labelled = _labelled(step.name, processors)
        return self._place(step, labelled, dataclasses.replace(result, name=step.name), where, direction)

    def _ai_step(
        self,
        step: Step,
        policy: Policy,
        where: str,
        direction: str,
        resources: Sequence[Resource],
        neighbours: tuple[str, str],
    ) -> ET.Element:
        """A custom code step translated by the AI, labelled with the step name at its place; or, when its code is
        not in the bundle, the AI declines, fails or its answer cannot be used, a comment (flagged for review)."""
        assert self.ai is not None
        source = callout_source(policy, resources)
        if isinstance(source, str):
            return self._skipped_step(step, policy.type, source, where, direction)
        options = _callout_options(policy)
        place = Place(self.bundle.name, where, direction, *neighbours)
        changed = "\n".join(f"- {line}" for line in describe_changes(self.changes)) or NONE
        outcome = self.ai.callout(source, step.name, policy.type, policy.raw_xml, place, changed)
        key = (id(step), direction)
        if isinstance(outcome, CalloutTranslated):
            for option in options:
                self.skip(f"{step.name} {option.name}", f"{where}: {policy.type} step {step.name}: {option.reason}")
            processors = [_adopt(p, {}) for p in outcome.processors]
            self.needs.update(_ai_needs(processors))
            stale = self._stale_inputs(source)
            # Code that may read a stale value may write a stale value: its writes are not faithful, so it stays a
            # step that may change anything (a later read of what it writes is refused).
            self.write_models[key] = (
                _write_model(outcome.writes, step.name, direction)
                if outcome.writes is not None and stale is None
                else None
            )
            low = outcome.confidence is Confidence.LOW
            reasons = ["the AI's confidence in this translation is low, so it needs review"] if low else []
            if stale is not None:
                reasons.append(stale)
            result = PolicyResult(
                step.name,
                policy.type,
                Method.AI,
                "; ".join(reasons),
                unsupported_options=options,
                confidence=outcome.confidence,
                notes=outcome.notes,
                needs_review=bool(reasons),
                original=source.original,
            )
            return self._place(step, _labelled(step.name, processors), result, where, direction)
        self.write_models[key] = None
        self.skip(step.name, f"{where}: {outcome.reason}")
        self._condition(step.name, "Step", where, step.condition, direction)
        self.records.append(
            PolicyResult(
                step.name,
                policy.type,
                Method.AI,
                outcome.reason,
                unsupported_options=options,
                location=where,
                condition=step.condition,
                confidence=outcome.confidence,
                notes=outcome.notes,
                needs_review=True,
                original=source.original,
            )
        )
        comment = ET.Comment(_comment_text(f"Step {step.name} ({policy.type}) is not generated: {outcome.reason}."))
        return cast(ET.Element, comment)

    def _stale_inputs(self, source: CalloutSource) -> str | None:
        """Why the AI's translation of a callout may read a stale value (flagged for review), or None: the code reads a
        value an earlier step may have changed in Apigee in a way the generated app may not carry over. The values read
        are the getVariable calls with a fixed name in the main script and in every included script; code that may
        read anything else counts as reading every value."""
        reads = callout_reads([source.original, *(text for _, text in source.includes)], source.kind)
        if reads is None:
            steps = all_steps(self.changes)
            if not steps:
                return None
            return (
                "a2m cannot tell which values its code reads, and the earlier step "
                f"{', '.join(steps)} may change values in Apigee in a way the generated app may not carry over; "
                "check that the translation reads the values as Apigee would"
            )
        stale = {name: changed_steps(name, self.changes) for name in sorted(reads)}
        found = [f"{name} (by {', '.join(steps)})" for name, steps in stale.items() if steps]
        if not found:
            return None
        return (
            f"its code reads {', '.join(found)}, which the earlier step may change in Apigee in a way the generated "
            "app may not carry over; check that the translation reads the changed value"
        )

    def _take_globals(self, policy_name: str, output: TemplateOutput) -> dict[str, str]:
        """Add the template's global elements once per policy; returns old name -> unique Mule name."""
        names: dict[str, str] = {}
        for element in output.globals:
            wanted = element.get("name") or "unnamed"
            key = (policy_name, wanted)
            if key not in self.global_names:
                self.global_names[key] = self.names.take(wanted)
                names[wanted] = self.global_names[key]
                self.policy_globals.append(_adopt(element, names))
            names[wanted] = self.global_names[key]
        return names

    def _place(
        self, step: Step, processor: ET.Element, result: PolicyResult, where: str, direction: str
    ) -> ET.Element:
        """Record the generated step; a step with a condition runs inside a when guarded by its translation, or
        never (``#[false]``, marked can't translate) when the condition can't be translated."""
        translation = self._condition(step.name, "Step", where, step.condition, direction, ask_ai=True)
        if translation is None:
            self.records.append(dataclasses.replace(result, location=where))
            return processor
        self._emit_guard(step, direction, self.changes, translation)
        if translation.ok and translation.reads_request_snapshot:
            self.needs.add(NEED_REQUEST_SNAPSHOT)
        tags = result.tags if translation.ok else (*result.tags, CANT_TRANSLATE_TAG)
        self.records.append(dataclasses.replace(result, location=where, condition=step.condition, tags=tags))
        choice = _element(CORE, "choice")
        guard = _guard(f"Step {step.name}", str(step.condition), translation, "this step never runs")
        _child(choice, CORE, "when", guard).append(processor)
        return choice

    def _flow_callout(self, step: Step, policy: Policy, where: str, direction: str) -> ET.Element:
        name = step.shared_flow
        reason: str | None = None
        bundles = self.shared.get(name, []) if name is not None else []
        if name is None:
            reason = f"FlowCallout {policy.name} names no SharedFlowBundle"
        elif not bundles:
            reason = (
                f"FlowCallout {policy.name} calls shared flow bundle {name}, which is not in the input "
                "folder (or could not be read), so no sub-flow is generated"
            )
        elif len(bundles) > 1:
            reason = f"FlowCallout {policy.name} calls {name}, and {len(bundles)} shared flow bundles have that name"
        elif name in self.in_progress:
            reason = f"FlowCallout {policy.name} calls shared flow {name} from inside itself"
        if reason is not None:
            return self._skipped_step(step, policy.type, reason, where, direction)
        options: tuple[UnsupportedOption, ...] = ()
        parameters = [c for c in policy.settings.children if c.tag == "Parameters" and c.children]
        if parameters:
            text = f"the parameters of FlowCallout {policy.name} are not passed to {name}"
            self.skip(step.name, f"{where}: {text}")
            options = (UnsupportedOption("Parameters", text),)
        position = len(self.records)
        call = _element(CORE, "flow-ref", {"name": self._sub_flow(bundles[0], direction), DOC_NAME: step.name})
        result = PolicyResult(step.name, policy.type, Method.TEMPLATE, unsupported_options=options)
        placed = self._place(step, call, result, where, direction)
        # The callout runs before the shared flow's own steps, which were recorded while generating it.
        self.records.insert(position, self.records.pop())
        return placed

    def _sub_flow(self, shared: Bundle, direction: str) -> str:
        """The name of the sub-flow for ``shared`` on the ``direction`` side, generating it the first time."""
        existing = self.sub_flow_names.get((shared.name, direction))
        if existing is not None:
            return existing
        name = self.names.take(f"shared-flow-{shared.name}")
        self.sub_flow_names[(shared.name, direction)] = name
        self.in_progress.add(shared.name)
        # One sub-flow serves every call on this side, each with its own earlier changes: its conditions are
        # checked against every request change (and dropped flow variable write) the proxy may make anywhere.
        caller_changes, caller_dead = self.changes, self.dead
        # The sub-flow serves every call; a call from a branch that never runs is counted at the call (see
        # _callout_dropped), not here.
        self.dead = None
        self.changes = self._sub_flow_base(direction)
        sub_flow = _element(CORE, "sub-flow", {"name": name})
        flows = list(shared.shared_flows)
        entry = next((f for f in flows if f.name == SHARED_FLOW_ENTRY), flows[0] if flows else None)
        policies = {policy.name: policy for policy in shared.policies}
        for flow in flows:
            if flow is not entry:
                self.skip(
                    f"{shared.name}/{flow.name}",
                    f"shared flow bundle {shared.name}: flow {flow.name} is not its entry flow and is not generated",
                )
                base = self.changes
                self._skip_steps(flow.steps, f"shared flow {shared.name}/{flow.name}", policies, direction=direction)
                self.changes = base
        if entry is not None:
            sub_flow.extend(
                self._steps(entry.steps, f"shared flow {shared.name}", direction, policies, shared.resources)
            )
        if not any(_is_element(child) for child in sub_flow):
            # A sub-flow needs at least one processor; this one only marks where the shared flow runs.
            sub_flow.append(_element(CORE, "logger", {"level": "DEBUG", "message": f"{name} called"}))
        self.in_progress.discard(shared.name)
        self.changes, self.dead = caller_changes, caller_dead
        self.sub_flows.append(sub_flow)
        return name

    # ------------------------------------------------------------ leftovers

    def _report_unused(self) -> None:
        self.changes = NO_CHANGES
        for target in self.bundle.target_endpoints:
            if target.name not in self.used_targets:
                where = f"TargetEndpoint {target.name}"
                if target.name in self.dropped_route_targets:
                    reason = "is only used by RouteRules of ProxyEndpoints that are not generated"
                else:
                    reason = "is not used by any RouteRule"
                self.skip(target.name, f"{where} {reason}, so it is not generated")
                self._skip_flow_steps(target.pre_flow, f"{where} PreFlow")
                self._skip_flows(target.flows, where)
                self._skip_flow_steps(target.post_flow, f"{where} PostFlow")
                self._skip_target_extras(target, where)
        used = {step.policy for step in _all_steps(self.bundle)}
        for policy in self.bundle.policies:
            if policy.name not in used:
                self.skip(policy.name, f"policy {policy.name} is not used by any step, so nothing is generated for it")


def _all_steps(bundle: Bundle) -> Iterator[Step]:
    endpoints: list[ProxyEndpoint | TargetEndpoint] = [*bundle.proxy_endpoints, *bundle.target_endpoints]
    for endpoint in endpoints:
        groups: list[FlowSteps | None] = [endpoint.pre_flow, endpoint.post_flow]
        groups.append(endpoint.post_client_flow if isinstance(endpoint, ProxyEndpoint) else endpoint.event_flow)
        for group in groups:
            if group is not None:
                yield from group.request
                yield from group.response
        for flow in endpoint.flows:
            yield from flow.request
            yield from flow.response
        for rule in endpoint.fault_rules:
            yield from rule.steps
        if endpoint.default_fault_rule is not None:
            yield from endpoint.default_fault_rule.steps


def _in_dead_branch(changes: RequestChanges, branch: str) -> RequestChanges:
    """``changes`` (dropped flow variable writes) with each writer named as sitting in ``branch``, a Flow or
    RouteRule whose condition can't be translated, so a refused read says why the write never happens."""
    label = f" (in {branch}, whose condition can't be translated, so it never runs in the generated app)"
    return dataclasses.replace(changes, variables=frozenset((name, step + label) for name, step in changes.variables))


def _fault(body: str) -> list[ET.Element]:
    """Answer 500 with a fixed JSON fault body."""
    return [
        _element(CORE, "set-variable", {"variableName": "httpStatus", "value": "500"}),
        _element(CORE, "set-payload", {"value": body, "mimeType": "application/json"}),
    ]


def _is_element(node: ET.Element) -> bool:
    return isinstance(node.tag, str)


def _adopt(node: ET.Element, renames: dict[str, str]) -> ET.Element:
    """A copy of a template's element for this document: core tags plain, renamed globals referenced by new name."""
    copied = copy.deepcopy(node)
    for element in copied.iter():
        if not _is_element(element):
            continue
        element.tag = element.tag.removeprefix(f"{{{CORE}}}")
        for key, value in element.attrib.items():
            if value in renames:
                element.set(key, renames[value])
    return copied


def _write_model(declared: DeclaredWrites, step: str, direction: str) -> registry.WriteModel:
    """The registry's write model for an AI-translated step ``step`` on the ``direction`` side, from its checked
    declaration: the body it sets is written exactly (its Content-Type pairing is not, so a later read of that is
    refused), and so are the flow variables and response headers it declared. The registry's names are in lower case
    (a near miss in case is read as changed, never as unchanged)."""
    content, content_type = (
        (RESPONSE_CONTENT, RESPONSE_CONTENT_TYPE) if direction == RESPONSE else (REQUEST_CONTENT, REQUEST_CONTENT_TYPE)
    )
    variables = {fold(name) for name in declared.variables}
    written = variables | {RESPONSE_HEADER_PREFIX + name for name in declared.response_headers}
    if declared.payload:
        written.add(content)
    writes = written | ({content_type} if declared.payload else set())
    request = RequestChanges(
        headers=frozenset((name, step) for name in declared.request_headers),
        queries=frozenset((fold(name), step) for name in declared.query_params),
    )
    return registry.WriteModel(request, frozenset(writes), frozenset(written))


def _callout_options(policy: Policy) -> tuple[UnsupportedOption, ...]:
    """The settings of a custom code policy the AI's translation does not carry over."""
    options: list[UnsupportedOption] = []
    if policy.continue_on_error:
        options.append(
            UnsupportedOption(
                "continueOnError",
                "continueOnError=true is not carried over: when this step fails, the generated flow stops",
            )
        )
    if is_true(policy.settings.attributes.get("async")):
        options.append(UnsupportedOption("async", "async=true is not carried over; the step runs in line"))
    return tuple(options)


def _ai_needs(processors: Sequence[ET.Element]) -> set[str]:
    """What the AI's processors need from the flows (see a2m.policies.common), from the variables they use."""
    text = " ".join(
        " ".join([element.text or "", *element.attrib.values()])
        for processor in processors
        for element in processor.iter()
        if _is_element(element)
    )
    wanted = (
        (REQUEST_HEADERS_VAR, NEED_REQUEST_HEADERS),
        (REQUEST_QUERY_VAR, NEED_REQUEST_QUERY),
        (REASON_PHRASE_VAR, NEED_REASON_PHRASE),
        (SNAPSHOT_VAR, NEED_REQUEST_SNAPSHOT),
        (FAULT_ERROR_TYPE, NEED_FAULT),
    )
    return {need for marker, need in wanted if marker in text}


def _labelled(name: str, processors: Sequence[ET.Element]) -> ET.Element:
    """One processor labelled with the step name: the only processor, or a try scope holding them all."""
    if len(processors) == 1:
        processors[0].set(DOC_NAME, name)
        return processors[0]
    scope = _element(CORE, "try", {DOC_NAME: name})
    scope.extend(processors)
    # A policy fault raised inside passes on to the flow's handler without being logged as an error here.
    handler = _child(scope, CORE, "error-handler")
    _child(handler, CORE, "on-error-propagate", {"type": FAULT_ERROR_TYPE, "logException": "false"})
    return scope


def _guard(label: str, original: str, translation: Translation, effect: str) -> dict[str, str]:
    """The attributes of the ``when`` for a condition: its translation, or ``#[false]`` marked can't translate."""
    if translation.ok and translation.dw is not None:
        description = f"{label}: Apigee condition {original}"
        if translation.reason:  # how it was translated, when not by a2m's own translator (the AI)
            description += f" ({translation.reason})"
        return {"expression": f"#[{translation.dw}]", DOC_DESCRIPTION: _attribute_text(description)}
    description = f"{label}: can't translate the Apigee condition {original} ({translation.reason}); {effect}"
    return {"expression": "#[false]", DOC_DESCRIPTION: _attribute_text(description)}


def _attribute_text(text: str) -> str:
    """``text`` for an attribute Mule only displays: Mule resolves ``${...}`` property placeholders in every
    attribute value when it deploys the app (an unknown key fails the deployment), so '${' becomes '$ {'.
    The exact text stays in the condition record."""
    return text.replace("${", "$ {")


def _untranslated_settings(step: str, options: Sequence[UnsupportedOption]) -> list[ET.Element]:
    """A comment for each setting of ``step`` kept because it can't be translated, holding its original text."""
    return [
        cast(ET.Element, ET.Comment(_comment_text(f"Step {step}: {option.reason}. Original value: {option.original}")))
        for option in options
        if option.original is not None
    ]


def _ensure_processor(container: ET.Element, label: str) -> None:
    """Mule needs at least one processor in a route; one holding only comments gets a DEBUG logger."""
    if not any(_is_element(child) for child in container):
        container.append(_element(CORE, "logger", {"level": "DEBUG", "message": f"{label}: nothing to run"}))


def _comment_text(text: str) -> str:
    """``text`` as the content of an XML comment, which may not hold '--' or end in '-'."""
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", text)
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "- -")
    return f" {cleaned} "


def _error_status() -> str:
    """DataWeave for the caller's status on a flow error, from the error type only."""
    branches = "".join(
        f"if ([{', '.join(repr(i) for i in ids)}] contains id) {status} else " for status, ids, _, _ in ERROR_FAULTS
    )
    return f"do {{ var id = error.errorType.identifier default '' --- {branches}{ERROR_FAULT_DEFAULT[0]} }}"


def _error_body() -> str:
    """DataWeave for the caller's JSON fault on a flow error: fixed text per status, no error details."""

    def fault(message: str, code: str) -> str:
        return json.dumps({"fault": {"faultstring": message, "detail": {"errorcode": code}}})

    cases = "".join(f"case {status} -> {fault(message, code)} " for status, _, message, code in ERROR_FAULTS)
    default = fault(*ERROR_FAULT_DEFAULT[1:])
    return f"#[output application/json --- ({_error_status()}) match {{ {cases}else -> {default} }}]"


# ---------------------------------------------------------------- error types

# The namespace of the error types a2m's generated steps raise. Mule knows such a type only when the app declares
# it, by raising it (``raise-error``) or mapping to it (an ``error-mapping`` targetType); a handler or mapping that
# names an undeclared one fails the deployment ("Could not find error").
APP_ERROR_NAMESPACE = FAULT_ERROR_TYPE.split(":", 1)[0]
_ON_ERROR_TAGS = ("on-error-continue", "on-error-propagate")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _error_types(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _is_app_error(error_type: str) -> bool:
    return error_type.split(":", 1)[0].strip() == APP_ERROR_NAMESPACE


def declared_error_types(elements: Sequence[ET.Element]) -> set[str]:
    """The app-namespace error types ``elements`` declare: raised by a ``raise-error`` or mapped to by an
    ``error-mapping``."""
    declared: set[str] = set()
    for top in elements:
        for node in top.iter():
            if not _is_element(node):
                continue
            local = _local(node.tag)
            if local == "raise-error":
                declared.update(t for t in _error_types(node.get("type", "")) if _is_app_error(t))
            elif local == "error-mapping":
                declared.update(t for t in _error_types(node.get("targetType", "")) if _is_app_error(t))
    return declared


def _drop_undeclared_error_handlers(elements: Sequence[ET.Element]) -> None:
    """Remove every handler entry and error mapping that names an app error type nothing in ``elements`` raises.

    Such an entry can never fire (nothing raises the type), and Mule refuses to deploy an app that names an
    undeclared type. A handler entry naming several types keeps the declared ones; one left with none is removed
    (an entry with no type would handle every error). An error handler (or error-mappings list) left empty is
    removed with it, so its scope passes every error on, as it did before. Removing a mapping can undeclare its
    target type, so this repeats until nothing changes.
    """
    while _drop_undeclared_once(elements):
        pass


def _drop_undeclared_once(elements: Sequence[ET.Element]) -> bool:
    declared = declared_error_types(elements)
    changed = False
    for top in elements:
        parents = {id(child): parent for parent in top.iter() for child in parent}
        for node in [n for n in top.iter() if _is_element(n)]:
            local = _local(node.tag)
            if local in _ON_ERROR_TAGS and node.get("type") is not None:
                types = _error_types(node.get("type", ""))
                kept = [t for t in types if not _is_app_error(t) or t in declared]
                if kept == types:
                    continue
                if kept:
                    node.set("type", ", ".join(kept))
                    changed = True
                    continue
            elif local == "error-mapping":
                source = node.get("sourceType", "").strip()
                if not (source and _is_app_error(source) and source not in declared):
                    continue
            else:
                continue
            parent = parents[id(node)]
            parent.remove(node)
            changed = True
            holder = parents.get(id(parent))
            if (
                holder is not None
                and _local(parent.tag) in ("error-handler", "error-mappings")
                and not any(_is_element(c) for c in parent)
            ):
                holder.remove(parent)
    return changed


# ---------------------------------------------------------------- serialising


def _uses(elements: Sequence[ET.Element], ns: str) -> bool:
    prefix = f"{{{ns}}}"
    return any(
        _is_element(e) and (e.tag.startswith(prefix) or any(k.startswith(prefix) for k in e.attrib))
        for top in elements
        for e in top.iter()
    )


def _enterprise_uses(elements: Sequence[ET.Element]) -> tuple[set[str], list[str]]:
    """The Mule Enterprise (``ee:``) components under ``elements`` as ``ee:<name>``, and the labels (doc:name) of
    the steps holding them, each once in document order."""
    prefix = f"{{{EE}}}"
    components: set[str] = set()
    steps: list[str] = []

    def walk(node: ET.Element, label: str | None) -> None:
        label = node.get(DOC_NAME, label)
        for child in node:
            if not _is_element(child):
                continue
            if child.tag.startswith(prefix):
                components.add(f"ee:{_local(child.tag)}")
                step = child.get(DOC_NAME, label)
                if step is not None and step not in steps:
                    steps.append(step)
            else:
                walk(child, label)

    for top in elements:
        if _is_element(top) and top.tag.startswith(prefix):
            components.add(f"ee:{_local(top.tag)}")
        else:
            walk(top, None)
    return components, steps


def _mule_document(children: Sequence[ET.Element]) -> str:
    root = _element(CORE, "mule", {"xmlns": CORE})
    locations = [CORE, SCHEMA_LOCATIONS[CORE]]
    for ns in (HTTP, OS, EE, VALIDATION):
        if _uses(children, ns):
            locations += [ns, SCHEMA_LOCATIONS[ns]]
    root.set(f"{{{XSI}}}schemaLocation", " ".join(locations))
    root.extend(children)
    ET.indent(root, space="    ")
    return XML_HEAD + ET.tostring(root, encoding="unicode") + "\n"


def _pom_text(bundle: Bundle, modules: Sequence[str] = ()) -> str:
    template = resources.files("a2m.generator").joinpath("templates", "pom.xml").read_text(encoding="utf-8")
    root = ET.fromstring(template)
    for element in root.iter():
        element.tag = element.tag.removeprefix(f"{{{POM}}}")
    root.attrib = {"xmlns": POM, **root.attrib}
    for path, value in (("artifactId", artifact_id(bundle.name)), ("name", bundle.name)):
        field_element = root.find(path)
        if field_element is None:
            raise GeneratorError(f"templates/pom.xml has no <{path}> element")
        field_element.text = value
    description = next((c.text for c in bundle.descriptor.children if c.tag == "Description" and c.text), None)
    if description:
        name = root.find("name")
        index = list(root).index(name) + 1 if name is not None else len(root)
        root.insert(index, _element(POM, "description", text=description.strip()))
    dependencies = root.find("dependencies")
    if dependencies is None:
        raise GeneratorError("templates/pom.xml has no <dependencies> element")
    for ns in modules:
        group, artifact, version = MODULE_DEPENDENCIES[ns]
        dependency = _child(dependencies, POM, "dependency")
        for tag, value in (("groupId", group), ("artifactId", artifact), ("version", version), ("classifier", "mule-plugin")):
            dependency.append(_element(POM, tag, text=value))
    ET.indent(root, space="  ")
    return XML_HEAD + ET.tostring(root, encoding="unicode") + "\n"


def _artifact_json(configs: list[str], *, enterprise: bool = False) -> str:
    artifact = {
        "minMuleVersion": MIN_MULE_VERSION,
        # MULE_EE when the app uses Mule Enterprise components (Transform Message), which Mule Kernel CE lacks.
        "requiredProduct": "MULE_EE" if enterprise else "MULE",
        "javaSpecificationVersions": [JAVA_VERSION],
        "configs": configs,
        "secureProperties": [],
        "redeploymentEnabled": True,
    }
    return json.dumps(artifact, indent=2) + "\n"


def _properties_text(props: dict[str, str], notes: dict[str, str] | None = None) -> str:
    lines = ["# Settings of the generated Mule app: the port it listens on and the address of each target.\n"]
    for key, value in props.items():
        note = (notes or {}).get(key)
        if note:
            lines.append("# " + " ".join(note.split()) + "\n")
        lines.append(f"{_escape_property(key, key=True)}={_escape_property(value, key=False)}\n")
    return "".join(lines)


def _escape_property(text: str, *, key: bool) -> str:
    """Escape ``text`` for a java.util.Properties file (ASCII only, so the file encoding never matters)."""
    out: list[str] = []
    for index, char in enumerate(text):
        if char == "\\":
            out.append("\\\\")
        elif char in "\t\n\r\f":
            out.append({"\t": "\\t", "\n": "\\n", "\r": "\\r", "\f": "\\f"}[char])
        elif char in "=:#!" or (char == " " and (key or index == 0)):
            out.append("\\" + char)
        elif not 32 <= ord(char) <= 126:
            out.extend(f"\\u{unit:04x}" for unit in _utf16_units(char))
        else:
            out.append(char)
    return "".join(out)


def _utf16_units(char: str) -> list[int]:
    data = char.encode("utf-16-be")
    return [int.from_bytes(data[i : i + 2], "big") for i in range(0, len(data), 2)]


# ---------------------------------------------------------------- writing


def _write_tree(dest: Path, files: dict[str, str], results_root: Path | None) -> None:
    """Write ``files`` into a fresh staging folder beside ``dest``, then swap it in for the old ``dest``."""
    if results_root is None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        results_root = dest.parent
    else:
        # The caller's own results folder (trusted, like dest's parent above); everything below it goes through safefs.
        results_root.mkdir(parents=True, exist_ok=True)
    staging = dest.with_name(f".{dest.name}.a2m-new")
    safefs.remove(results_root, staging)
    safefs.make_dirs(results_root, staging)
    for rel in sorted(files):
        path = staging.joinpath(*rel.split("/"))
        safefs.make_dirs(results_root, path.parent)
        safefs.write_text_atomic(results_root, path, files[rel])
    safefs.remove(results_root, dest)
    safefs.move(results_root, staging, dest)
