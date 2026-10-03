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

Nothing is dropped silently. Whatever this step does not generate (policies
without a template, settings a template cannot carry over, conditional flows,
fault rules, target settings, targets without a fixed URL, ...) is listed in
:attr:`GenerateResult.unsupported` with a reason, and every conditional
RouteRule or step is kept in :attr:`GenerateResult.pending` with its original
condition for the condition translator. Until it is translated, a conditional
route's branch or step never runs (its ``when`` is ``#[false]``) and the
original condition is kept beside it in the flow XML.

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
from a2m.ir import (
    Bundle,
    BundleKind,
    Flow,
    FlowSteps,
    Policy,
    ProxyEndpoint,
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
    REASON_PHRASE_VAR,
    REQUEST,
    REQUEST_HEADERS_VAR,
    REQUEST_QUERY_VAR,
    RESPONSE,
    Method,
    PolicyResult,
    TemplateOutput,
    UnsupportedOption,
)

CORE = "http://www.mulesoft.org/schema/mule/core"
HTTP = "http://www.mulesoft.org/schema/mule/http"
OS = "http://www.mulesoft.org/schema/mule/os"
VALIDATION = "http://www.mulesoft.org/schema/mule/validation"
DOC = "http://www.mulesoft.org/schema/mule/documentation"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
POM = "http://maven.apache.org/POM/4.0.0"
SCHEMA_LOCATIONS = {
    CORE: "http://www.mulesoft.org/schema/mule/core/current/mule.xsd",
    HTTP: "http://www.mulesoft.org/schema/mule/http/current/mule-http.xsd",
    OS: "http://www.mulesoft.org/schema/mule/os/current/mule-os.xsd",
    VALIDATION: "http://www.mulesoft.org/schema/mule/validation/current/mule-validation.xsd",
}
# The pom dependency each module namespace beyond core and http needs, pinned to the versions proven on 4.9.0.
MODULE_DEPENDENCIES = {
    OS: ("org.mule.connectors", "mule-objectstore-connector", "1.2.2"),
    VALIDATION: ("org.mule.modules", "mule-validation-module", "2.0.9"),
}
for _prefix, _uri in (("http", HTTP), ("os", OS), ("validation", VALIDATION), ("doc", DOC), ("xsi", XSI)):
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
SHARED_FLOW_ENTRY = "default"
XML_HEAD = '<?xml version="1.0" encoding="UTF-8"?>\n'

# The request path below the matched base path (Apigee's proxy.pathsuffix). Every listener path
# ends in '/*', and Mule's maskedRequestPath is the part of the raw request path that trailing
# '*' matched, so a wildcard segment of any length in the base path ('/v1/*/search') is never
# forwarded: '/v1/acme/search/items' gives '/items'. Mule gives '/' both for the base path itself
# and for the base path with a trailing slash; Apigee's suffix is '' for the first (so the
# target gets exactly its own base path) and '/' for the second.
REQUEST_PATH = (
    "#[if (attributes.maskedRequestPath == '/' and not (attributes.rawRequestPath endsWith '/')) '' "
    "else attributes.maskedRequestPath]"
)
# Hop-by-hop and length headers the HTTP connector sets itself; everything else is passed on.
HOP_HEADERS = "['host', 'content-length', 'transfer-encoding', 'connection']"
REQUEST_HEADERS = f"#[attributes.headers -- {HOP_HEADERS}]"
REQUEST_QUERY = "#[attributes.queryParams]"
# The same, once a policy step may have changed the headers or query parameters the target gets.
CHANGED_REQUEST_HEADERS = f"#[(vars.{REQUEST_HEADERS_VAR} default attributes.headers) -- {HOP_HEADERS}]"
CHANGED_REQUEST_QUERY = f"#[vars.{REQUEST_QUERY_VAR} default attributes.queryParams]"
# Content-Type travels as the payload's media type, so it is not copied twice.
RESPONSE_HEADERS = "#[attributes.headers -- ['content-length', 'transfer-encoding', 'connection', 'content-type']]"
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


@dataclass(frozen=True, slots=True)
class UnsupportedItem:
    """Something in the bundle this step did not generate: a step, policy, target endpoint or setting."""

    name: str
    reason: str


@dataclass(frozen=True, slots=True)
class PendingCondition:
    """A conditional RouteRule or step kept for the condition translator: ``condition`` is the original text.

    ``endpoint`` is the ProxyEndpoint of a RouteRule, or where a step sits.
    """

    name: str
    condition: str
    endpoint: str
    kind: str = "RouteRule"


@dataclass(frozen=True, slots=True)
class GenerateResult:
    files: tuple[str, ...]
    unsupported: tuple[UnsupportedItem, ...]
    pending: tuple[PendingCondition, ...]
    # One result record per policy step, in flow order (a policy used by two steps has two records).
    policies: tuple[PolicyResult, ...] = ()


class GeneratorError(ValueError):
    """The input cannot be generated at all (for example a shared flow bundle passed as the proxy)."""


def generate_project(
    bundle: Bundle,
    dest: Path,
    *,
    shared_flows: Sequence[Bundle] = (),
    results_root: Path | None = None,
) -> GenerateResult:
    """Write the Mule project for proxy ``bundle`` into ``dest``, replacing any older project there.

    ``shared_flows`` are the shared flow bundles the proxy's FlowCallout steps
    may call, matched by bundle name. Every write and delete goes through
    :mod:`a2m.safefs` and stays strictly inside ``results_root`` (default: the
    folder holding ``dest``, created when missing).
    """
    if bundle.kind is not BundleKind.PROXY:
        raise GeneratorError(f"{bundle.name} is a {bundle.kind.value} bundle, not a proxy")
    builder = _ProjectBuilder(bundle, shared_flows)
    files = builder.build()
    _write_tree(dest, files, results_root)
    return GenerateResult(
        files=tuple(sorted(files)),
        unsupported=tuple(builder.unsupported),
        pending=tuple(builder.pending),
        policies=tuple(builder.records),
    )


# ---------------------------------------------------------------- target addresses


@dataclass(frozen=True, slots=True)
class _Address:
    protocol: str
    host: str
    port: str
    base_path: str


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
        return f"its URL {shown} contains a flow variable; variables in target URLs are translated in a later step"
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
    """How one TargetEndpoint is generated: its request config, or None when it cannot be (already reported)."""

    config: str | None
    key: str | None
    timeout: bool


class _ProjectBuilder:
    def __init__(self, bundle: Bundle, shared_flows: Sequence[Bundle]) -> None:
        self.bundle = bundle
        self.policies = {policy.name: policy for policy in bundle.policies}
        self.shared: dict[str, list[Bundle]] = {}
        for shared in shared_flows:
            self.shared.setdefault(shared.name, []).append(shared)
        self.names = _Names()
        self.keys = _Names()
        self.props: dict[str, str] = {LISTENER_HOST_KEY: LISTENER_HOST, LISTENER_PORT_KEY: LISTENER_PORT}
        self.unsupported: list[UnsupportedItem] = []
        self._seen_items: set[tuple[str, str]] = set()
        self.pending: list[PendingCondition] = []
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
        # What the generated policy steps need from the flows (see a2m.policies.common), and their globals.
        self.needs: set[str] = set()
        self.policy_globals: list[ET.Element] = []
        self.global_names: dict[tuple[str, str], str] = {}
        self.property_notes: dict[str, str] = {}
        self.requests: list[ET.Element] = []
        self.listener_responses: list[ET.Element] = []

    # ------------------------------------------------------------ results

    def skip(self, name: str, reason: str) -> None:
        if (name, reason) not in self._seen_items:
            self._seen_items.add((name, reason))
            self.unsupported.append(UnsupportedItem(name, reason))

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
        files["/".join((*MULE_DIR, PROXY_FILE))] = _mule_document(main)
        configs = sorted(path.rsplit("/", 1)[-1] for path in files)
        files["/".join((*RESOURCES_DIR, PROPERTIES_FILE))] = _properties_text(self.props, self.property_notes)
        files["mule-artifact.json"] = _artifact_json(configs)
        files["pom.xml"] = _pom_text(self.bundle, [ns for ns in MODULE_DEPENDENCIES if _uses(main, ns)])
        return files

    def _wire_policy_needs(self) -> None:
        """Connect the generated policy steps to the flows: changed request parts, fault answers, reason phrases."""
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

        flow.extend(self._steps(endpoint.pre_flow.request, f"{where} PreFlow request", REQUEST))
        self._skip_flows(endpoint.flows, where)
        flow.extend(self._steps(endpoint.post_flow.request, f"{where} PostFlow request", REQUEST))
        flow.extend(self._routing(endpoint, where))
        # Apigee's response order: the target's response steps (inside the routing above), then this
        # endpoint's PreFlow, conditional Flows (reported by _skip_flows) and PostFlow response steps.
        flow.extend(self._steps(endpoint.pre_flow.response, f"{where} PreFlow response", RESPONSE))
        flow.extend(self._steps(endpoint.post_flow.response, f"{where} PostFlow response", RESPONSE))
        if endpoint.post_client_flow is not None:
            self._skip_flow_steps(endpoint.post_client_flow, f"{where} PostClientFlow")
        self._skip_fault_rules(endpoint, where)
        return flow

    def _skip_endpoint(self, endpoint: ProxyEndpoint, reason: str) -> None:
        """Report a ProxyEndpoint whose base path has no Mule listener path, and everything in it."""
        where = f"ProxyEndpoint {endpoint.name}"
        base = endpoint.base_path or ""
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
        for flow in flows:
            condition = f" (condition {flow.condition})" if flow.condition else " (no condition)"
            self.skip(
                flow.name,
                f"{where}: flow {flow.name}{condition} is not generated yet; its steps are listed separately",
            )
            self._skip_flow_steps(FlowSteps(flow.request, flow.response), f"{where} flow {flow.name}")

    def _skip_flow_steps(self, steps: FlowSteps, where: str) -> None:
        for direction, items in (("request", steps.request), ("response", steps.response)):
            self._skip_steps(items, f"{where} {direction}")

    def _skip_steps(self, steps: Sequence[Step], where: str, policies: dict[str, Policy] | None = None) -> None:
        policies = self.policies if policies is None else policies
        for step in steps:
            reason = f"{where}: step {step.name} is in a part of the flow that is not generated yet"
            self.skip(step.name, reason)
            self._record_skipped(step, policies, where, reason)

    def _record_skipped(self, step: Step, policies: dict[str, Policy], where: str, reason: str) -> None:
        policy = policies.get(step.policy)
        kind = policy.type if policy is not None else "unknown"
        self.records.append(
            PolicyResult(step.name, kind, Method.SKIPPED, reason, location=where, condition=step.condition)
        )

    def _skip_fault_rules(self, endpoint: ProxyEndpoint | TargetEndpoint, where: str) -> None:
        for rule in endpoint.fault_rules:
            self.skip(rule.name, f"{where}: fault rule {rule.name} is not generated yet")
            self._skip_steps(rule.steps, f"{where} fault rule {rule.name}")
        default = endpoint.default_fault_rule
        if default is not None:
            self.skip(default.name, f"{where}: default fault rule {default.name} is not generated yet")
            self._skip_steps(default.steps, f"{where} default fault rule {default.name}")

    # ------------------------------------------------------------ routing

    def _routing(self, endpoint: ProxyEndpoint, where: str) -> list[ET.Element]:
        """The processors that pick a route: inline for one unconditional route, else a choice router."""
        conditional: list[RouteRule] = []
        fallback: RouteRule | None = None
        for rule in endpoint.route_rules:
            if fallback is not None:
                self.skip(
                    rule.name,
                    f"{where}: RouteRule {rule.name} comes after the unconditional RouteRule {fallback.name}, "
                    "so Apigee never reaches it",
                )
            elif rule.condition is None:
                fallback = rule
            else:
                conditional.append(rule)
        if not endpoint.route_rules:
            return self._null_route()
        if not conditional and fallback is not None:
            return self._route(fallback, where)
        choice = _element(CORE, "choice")
        for rule in conditional:
            condition = rule.condition or ""
            self.pending.append(PendingCondition(rule.name, condition, endpoint.name))
            when = _child(
                choice,
                CORE,
                "when",
                {
                    "expression": "#[false]",
                    f"{{{DOC}}}description": (
                        f"RouteRule {rule.name}: Apigee condition {condition} (not translated yet, "
                        "so this branch is never taken)"
                    ),
                },
            )
            when.extend(self._route(rule, where))
        otherwise = _child(choice, CORE, "otherwise")
        if fallback is not None:
            otherwise.extend(self._route(fallback, where))
        else:
            self.skip(
                f"{endpoint.name} no matching route",
                f"{where}: every RouteRule has a condition; when none matches the app answers 500 "
                "with a fault body (Apigee would send no target request)",
            )
            otherwise.extend(_fault(NO_ROUTE_FAULT))
        return [choice]

    def _null_route(self) -> list[ET.Element]:
        """No target: answer with an empty body instead of echoing the request back."""
        return [_element(CORE, "set-payload", {"value": ""})]

    def _route(self, rule: RouteRule, where: str) -> list[ET.Element]:
        if rule.url is not None and rule.target is None:
            self.skip(
                rule.name,
                f"{where}: RouteRule {rule.name} routes straight to the URL {shown_url(rule.url)}; not generated yet",
            )
            return _fault(NO_TARGET_FAULT)
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

    def _target_address(self, target: TargetEndpoint) -> _Address | str:
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
            self._skip_flow_steps(target.event_flow, f"{where} EventFlow")

    def _target_processors(self, target: TargetEndpoint) -> list[ET.Element]:
        where = f"TargetEndpoint {target.name}"
        plan = self._target_plan(target)
        if plan.config is None or plan.key is None:
            self._skip_flow_steps(target.pre_flow, f"{where} PreFlow")
            self._skip_flows(target.flows, where)
            self._skip_flow_steps(target.post_flow, f"{where} PostFlow")
            return _fault(NO_TARGET_FAULT)
        processors = self._steps(target.pre_flow.request, f"{where} PreFlow request", REQUEST)
        self._skip_flows(target.flows, where)
        processors += self._steps(target.post_flow.request, f"{where} PostFlow request", REQUEST)
        attrib = {
            "config-ref": plan.config,
            "method": "#[attributes.method]",
            "path": REQUEST_PATH,
            # Apigee hands a target's 3xx and its Location back to the caller; it never follows it.
            "followRedirects": "false",
        }
        if plan.timeout:
            attrib["responseTimeout"] = f"${{{plan.key}.responseTimeout}}"
        request = _element(HTTP, "request", attrib)
        _child(request, HTTP, "headers").text = REQUEST_HEADERS
        _child(request, HTTP, "query-params").text = REQUEST_QUERY
        self.requests.append(request)
        validator = _child(request, HTTP, "response-validator")
        # Apigee passes the target's 4xx and 5xx answers to the caller unchanged.
        _child(validator, HTTP, "success-status-code-validator", {"values": "0..599"})
        processors.append(request)
        processors.append(
            _element(CORE, "set-variable", {"variableName": "httpStatus", "value": "#[attributes.statusCode]"})
        )
        processors.append(
            _element(CORE, "set-variable", {"variableName": "responseHeaders", "value": RESPONSE_HEADERS})
        )
        processors += self._steps(target.pre_flow.response, f"{where} PreFlow response", RESPONSE)
        processors += self._steps(target.post_flow.response, f"{where} PostFlow response", RESPONSE)
        return processors

    # ------------------------------------------------------------ steps and shared flows

    def _steps(
        self, steps: Sequence[Step], where: str, direction: str, policies: dict[str, Policy] | None = None
    ) -> list[ET.Element]:
        """Processors for ``steps``, one labelled processor (or a comment, when skipped) per step, in order."""
        policies = self.policies if policies is None else policies
        found: list[ET.Element] = []
        for step in steps:
            policy = policies.get(step.policy)
            if policy is None:
                found.append(self._skipped_step(step, "unknown", f"policy {step.policy} is not in the bundle", where))
            elif not policy.enabled:
                reason = f"policy {policy.name} is disabled (enabled=false), so nothing is generated"
                found.append(self._skipped_step(step, policy.type, reason, where))
            elif policy.type == FLOW_CALLOUT:
                found.append(self._flow_callout(step, policy, where, direction))
            else:
                found.append(self._policy_step(step, registry.translate(policy, direction=direction), where))
        return found

    def _skipped_step(self, step: Step, kind: str, reason: str, where: str) -> ET.Element:
        """Report a step that is not generated; the comment left at its place names the step and its type."""
        self.skip(step.name, f"{where}: {reason}")
        condition = f" It has the condition {step.condition}." if step.condition is not None else ""
        self.records.append(
            PolicyResult(step.name, kind, Method.SKIPPED, reason, location=where, condition=step.condition)
        )
        comment = ET.Comment(_comment_text(f"Step {step.name} ({kind}) is not generated: {reason}.{condition}"))
        return cast(ET.Element, comment)

    def _policy_step(self, step: Step, output: TemplateOutput, where: str) -> ET.Element:
        result = output.result
        if result.method is not Method.TEMPLATE or not output.processors:
            return self._skipped_step(step, result.type, result.reason or "nothing could be generated", where)
        for option in result.unsupported_options:
            self.skip(f"{step.name} {option.name}", f"{where}: {result.type} step {step.name}: {option.reason}")
        names = self._take_globals(step.policy, output)
        processors = [_adopt(p, names) for p in output.processors]
        for key, value in output.properties.items():
            self.props.setdefault(key, value)
        for key, note in output.property_notes.items():
            self.property_notes.setdefault(key, note)
        self.needs.update(output.needs)
        return self._place(step, _labelled(step.name, processors), dataclasses.replace(result, name=step.name), where)

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

    def _place(self, step: Step, processor: ET.Element, result: PolicyResult, where: str) -> ET.Element:
        """Record the generated step; a step with a condition is kept but never runs until it is translated."""
        if step.condition is None:
            self.records.append(dataclasses.replace(result, location=where))
            return processor
        self.pending.append(PendingCondition(step.name, step.condition, where, kind="Step"))
        self.records.append(
            dataclasses.replace(
                result, location=where, condition=step.condition, tags=(*result.tags, "condition-pending")
            )
        )
        choice = _element(CORE, "choice")
        when = _child(
            choice,
            CORE,
            "when",
            {
                "expression": "#[false]",
                DOC_DESCRIPTION: (
                    f"Step {step.name}: Apigee condition {step.condition} (not translated yet, so this step never runs)"
                ),
            },
        )
        when.append(processor)
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
            return self._skipped_step(step, policy.type, reason, where)
        options: tuple[UnsupportedOption, ...] = ()
        parameters = [c for c in policy.settings.children if c.tag == "Parameters" and c.children]
        if parameters:
            text = f"the parameters of FlowCallout {policy.name} are not passed to {name}"
            self.skip(step.name, f"{where}: {text}")
            options = (UnsupportedOption("Parameters", text),)
        position = len(self.records)
        call = _element(CORE, "flow-ref", {"name": self._sub_flow(bundles[0], direction), DOC_NAME: step.name})
        result = PolicyResult(step.name, policy.type, Method.TEMPLATE, unsupported_options=options)
        placed = self._place(step, call, result, where)
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
                self._skip_steps(flow.steps, f"shared flow {shared.name}/{flow.name}", policies)
        if entry is not None:
            sub_flow.extend(self._steps(entry.steps, f"shared flow {shared.name}", direction, policies))
        if not any(_is_element(child) for child in sub_flow):
            # A sub-flow needs at least one processor; this one only marks where the shared flow runs.
            sub_flow.append(_element(CORE, "logger", {"level": "DEBUG", "message": f"{name} called"}))
        self.in_progress.discard(shared.name)
        self.sub_flows.append(sub_flow)
        return name

    # ------------------------------------------------------------ leftovers

    def _report_unused(self) -> None:
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


# ---------------------------------------------------------------- serialising


def _uses(elements: Sequence[ET.Element], ns: str) -> bool:
    prefix = f"{{{ns}}}"
    return any(
        _is_element(e) and (e.tag.startswith(prefix) or any(k.startswith(prefix) for k in e.attrib))
        for top in elements
        for e in top.iter()
    )


def _mule_document(children: Sequence[ET.Element]) -> str:
    root = _element(CORE, "mule", {"xmlns": CORE})
    locations = [CORE, SCHEMA_LOCATIONS[CORE]]
    for ns in (HTTP, OS, VALIDATION):
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


def _artifact_json(configs: list[str]) -> str:
    artifact = {
        "minMuleVersion": MIN_MULE_VERSION,
        "requiredProduct": "MULE",
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
    staging = dest.with_name(f".{dest.name}.a2m-new")
    safefs.remove(results_root, staging)
    safefs.make_dirs(results_root, staging)
    for rel in sorted(files):
        path = staging.joinpath(*rel.split("/"))
        safefs.make_dirs(results_root, path.parent)
        safefs.write_text_atomic(results_root, path, files[rel])
    safefs.remove(results_root, dest)
    safefs.move(results_root, staging, dest)
