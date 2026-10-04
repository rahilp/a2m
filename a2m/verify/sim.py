"""What an Apigee proxy does with a test request, as far as a2m can tell: the oracle of the test battery.

The battery (:mod:`a2m.verify.batteries`) builds requests and asks this module
what Apigee would answer, from the proxy's own IR, never from the generated
Mule app: the status, whether the backend is called, and the headers the
backend call carries that the proxy's steps set. Only what is certain is
predicted. A step whose effect a2m cannot know (a policy type with no model
here, a condition it cannot evaluate, a value it cannot resolve) makes the
prediction unknown, with the reason; the battery then lists that case as
untested rather than guessing.

Conditions are read with a2m's own condition parser (:mod:`a2m.conditions`);
they can be evaluated on a request and, for the variables a test request
controls (verb, path suffix, request headers and query parameters), a
request can be changed so a condition is true or false.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from urllib.parse import urlencode

from a2m.conditions import template_parts
from a2m.conditions.lexer import ConditionError, tokenize
from a2m.conditions.parser import Binary, Comparison, Connective, LiteralKind, Node, Not, Operator, parse
from a2m.ir import Bundle, Flow, Policy, ProxyEndpoint, Step, TargetEndpoint, XmlElement
from a2m.verify.model import fold, shout

REQUEST, RESPONSE = "request", "response"
# What the battery's mock backend answers (see a2m.verify.harness).
BACKEND_STATUS = 200
CLIENT_ADDRESS = "127.0.0.1"
OTHER_VALUE = "a2m-other"
SAMPLE_VALUE = "a2m7"
# Variable roots Apigee itself sets: a2m only knows the ones read below, every other one is unknown.
BUILTIN_ROOTS = frozenset(
    {
        "request", "response", "message", "proxy", "target", "client", "system", "environment", "organization",
        "apiproxy", "error", "fault", "ratelimit", "verifyapikey", "oauthv2", "oauthv2accesstoken", "developer",
        "apiproduct", "app", "servicecallout", "current", "is", "router", "virtualhost", "variable", "messageid",
        "flow", "route", "loadbalancing", "mint", "jwt", "publishmessage", "responsecache", "lookupcache",
        "graphql", "messagelogging", "statistics", "javascript", "python", "javacallout", "basicauthentication",
    }
)
# Policy types that never change the request, the response or a later step's input.
NO_EFFECT_TYPES = frozenset({"StatisticsCollector", "MessageLogging"})
RATE = re.compile(r"^\s*(\d+)\s*(ps|pm)\s*$")
BASIC_VALUE = re.compile(r"(?i)^Basic\s+([A-Za-z0-9+/]+=*)\s*$")
HOP_HEADERS = frozenset({"host", "content-length", "transfer-encoding", "connection"})
MIN_CACHE_SECONDS = 5


class Unknown(Exception):
    """The effect of a step (or the value of a condition) cannot be predicted; ``args[0]`` says why.

    Raised from :meth:`Model.run_call`, ``executed`` holds the steps that ran before it (their keys).
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.executed: tuple[str, ...] = ()


class FaultRuleUnknown(Unknown):
    """A step raised an error while its endpoint has FaultRules or a DefaultFaultRule, which a2m does not simulate."""


# ---------------------------------------------------------------- XML helpers


def child(element: XmlElement | None, tag: str) -> XmlElement | None:
    if element is None:
        return None
    return next((c for c in element.children if c.tag == tag), None)


def children(element: XmlElement | None, tag: str) -> list[XmlElement]:
    return [c for c in element.children if c.tag == tag] if element is not None else []


def text(element: XmlElement | None) -> str | None:
    if element is None or element.text is None:
        return None
    return element.text.strip()


def is_true(value: str | None) -> bool:
    return fold((value or "").strip()) == "true"


# ---------------------------------------------------------------- test requests


@dataclass(frozen=True, slots=True)
class Req:
    """A test request below the endpoint's base path: verb, path suffix, headers and query parameters."""

    method: str = "GET"
    suffix: str = ""
    headers: tuple[tuple[str, str], ...] = ()
    query: tuple[tuple[str, str], ...] = ()

    def header(self, name: str) -> str | None:
        key = fold(name)
        return next((v for k, v in self.headers if fold(k) == key), None)

    def with_header(self, name: str, value: str | None) -> Req:
        kept = tuple((k, v) for k, v in self.headers if fold(k) != fold(name))
        return replace(self, headers=kept if value is None else (*kept, (name, value)))

    def param(self, name: str) -> str | None:
        return next((v for k, v in self.query if k == name), None)

    def with_param(self, name: str, value: str | None) -> Req:
        kept = tuple((k, v) for k, v in self.query if k != name)
        return replace(self, query=kept if value is None else (*kept, (name, value)))

    def path(self, base_path: str) -> str:
        query = f"?{urlencode(self.query)}" if self.query else ""
        return f"{base_path}{self.suffix}{query}"


# ---------------------------------------------------------------- conditions


def parse_condition(condition: str) -> Node:
    try:
        return parse(tokenize(condition))
    except ConditionError as exc:
        raise Unknown(f"the condition {condition!r} can't be read: {exc}") from exc


@dataclass
class Scope:
    """The values a condition or template reads: the request as it is now and the flow variables set so far."""

    base_path: str
    req: Req
    variables: dict[str, str] = field(default_factory=dict)
    unknown_variables: set[str] = field(default_factory=set)
    # The step that wrote each flow variable, for tracing which steps fed a value.
    writers: dict[str, str] = field(default_factory=dict)

    def lookup(self, variable: str) -> str | None:
        """The value of ``variable`` (None: null in Apigee); raises Unknown when a2m can't know it."""
        folded = fold(variable)
        if folded == "request.verb":
            return self.req.method
        if folded == "proxy.pathsuffix":
            return self.req.suffix
        if folded == "proxy.basepath":
            return self.base_path
        if folded == "request.path":
            return self.base_path + self.req.suffix
        if folded == "request.uri":
            return self.req.path(self.base_path)
        if folded.startswith("request.header.") and folded.count(".") == 2:
            value = self.req.header(variable.split(".", 2)[2])
            return value.split(",", 1)[0].strip() if value is not None else None
        if folded.startswith("request.queryparam.") and folded.count(".") == 2:
            return self.req.param(variable.split(".", 2)[2])
        if variable in self.unknown_variables:
            raise Unknown(f"the value of {variable} is not known")
        if variable in self.variables:
            return self.variables[variable]
        if folded.split(".", 1)[0] in BUILTIN_ROOTS:
            raise Unknown(f"a2m does not know the value of {variable} in a test")
        return None


def evaluate(node: Node, scope: Scope) -> bool:
    """The value of a condition; raises Unknown when it depends on something a2m can't know."""
    if isinstance(node, Not):
        return not evaluate(node.operand, scope)
    if isinstance(node, Binary):
        left = _try(node.left, scope)
        right = _try(node.right, scope)
        if node.connective is Connective.AND:
            if left is False or right is False:
                return False
            if left is True and right is True:
                return True
        else:
            if left is True or right is True:
                return True
            if left is False and right is False:
                return False
        raise Unknown("a condition depends on a value a2m does not know")
    return _compare(node, scope.lookup(node.variable))


def _try(node: Node, scope: Scope) -> bool | None:
    try:
        return evaluate(node, scope)
    except Unknown:
        return None


def _literal(node: Comparison) -> str | None:
    value = node.value
    if value.kind is LiteralKind.NULL:
        return None
    if value.kind is LiteralKind.BOOLEAN:
        return fold(value.text)
    return value.text


def _compare(node: Comparison, actual: str | None) -> bool:
    expected = _literal(node)
    op = node.operator
    if op in (Operator.EQUALS, Operator.NOT_EQUALS):
        same = actual == expected if actual is None or expected is None else _same(actual, expected)
        return same if op is Operator.EQUALS else not same
    if actual is None or expected is None:
        if op in (Operator.EQUALS_IGNORE_CASE, Operator.STARTS_WITH, Operator.MATCHES, Operator.MATCHES_PATH):
            return False
        raise Unknown(f"a2m can't compare null with {node.spelling}")
    if op is Operator.EQUALS_IGNORE_CASE:
        return fold(actual) == fold(expected)
    if op is Operator.STARTS_WITH:
        return actual.startswith(expected)
    if op is Operator.MATCHES:
        return re.fullmatch(".*".join(re.escape(p) for p in expected.split("*")), actual, re.DOTALL) is not None
    if op is Operator.MATCHES_PATH:
        return re.fullmatch(_path_pattern(expected), actual) is not None
    if op is Operator.JAVA_REGEX:
        try:
            return re.fullmatch(expected, actual) is not None
        except re.error as exc:
            raise Unknown(f"the regular expression {expected!r} can't be read: {exc}") from exc
    try:
        left, right = float(actual), float(expected)
    except ValueError as exc:
        raise Unknown(f"{node.spelling} compares values that are not numbers") from exc
    return {
        Operator.GREATER: left > right,
        Operator.GREATER_OR_EQUAL: left >= right,
        Operator.LESS: left < right,
        Operator.LESS_OR_EQUAL: left <= right,
    }[op]


def _same(actual: str, expected: str) -> bool:
    if actual == expected:
        return True
    try:
        return float(actual) == float(expected)
    except ValueError:
        return False


def _path_pattern(pattern: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**", index):
            out.append(".*")
            index += 2
        elif pattern[index] == "*":
            out.append("[^/]*")
            index += 1
        else:
            out.append(re.escape(pattern[index]))
            index += 1
    return "".join(out)


def satisfy(node: Node, want: bool, req: Req) -> Req | None:
    """``req`` changed (verb, path suffix, headers, query) so ``node`` is ``want``, or None when a2m can't."""
    for candidate in _candidates(node, want, req):
        scope = Scope("", candidate)
        if _try(node, scope) is want:
            return candidate
    return None


def _candidates(node: Node, want: bool, req: Req) -> Iterator[Req]:
    if isinstance(node, Not):
        yield from _candidates(node.operand, not want, req)
        return
    if isinstance(node, Binary):
        both = (node.connective is Connective.AND) == want
        if both:
            for first in _candidates(node.left, want, req):
                yield from _candidates(node.right, want, first)
        else:
            yield from _candidates(node.left, want, req)
            yield from _candidates(node.right, want, req)
        return
    yield req
    yield from _comparison_candidates(node, want, req)


def _comparison_candidates(node: Comparison, want: bool, req: Req) -> Iterator[Req]:
    folded = fold(node.variable)
    expected = _literal(node)
    op = node.operator
    positive = want if op is not Operator.NOT_EQUALS else not want
    sample = _sample(op, expected)
    if folded == "request.verb":
        if positive and sample is not None:
            yield replace(req, method=shout(sample))
        for verb in ("GET", "POST", "PUT", "DELETE"):
            yield replace(req, method=verb)
        return
    if folded == "proxy.pathsuffix":
        if positive and sample is not None:
            yield replace(req, suffix=sample if sample.startswith("/") or not sample else "/" + sample)
        yield replace(req, suffix="/" + OTHER_VALUE)
        yield replace(req, suffix="")
        return
    for prefix, setter in (("request.header.", Req.with_header), ("request.queryparam.", Req.with_param)):
        if folded.startswith(prefix) and folded.count(".") == 2:
            name = node.variable.split(".", 2)[2]
            if positive and sample is not None:
                yield setter(req, name, sample)
            yield setter(req, name, None)
            yield setter(req, name, OTHER_VALUE)
            return


def _sample(op: Operator, expected: str | None) -> str | None:
    """A value that makes the comparison true, or None when a2m has no simple one."""
    if expected is None:
        return None
    if op in (Operator.EQUALS, Operator.NOT_EQUALS, Operator.EQUALS_IGNORE_CASE, Operator.STARTS_WITH):
        return expected
    if op is Operator.MATCHES:
        return expected.replace("*", SAMPLE_VALUE)
    if op is Operator.MATCHES_PATH:
        return expected.replace("**", SAMPLE_VALUE).replace("*", SAMPLE_VALUE)
    return None


# ---------------------------------------------------------------- templates


def resolve_template(template: str, scope: Scope, *, ignore_unresolved: bool) -> tuple[str, frozenset[str]]:
    """The value of an Apigee message template and the steps whose variables fed it; raises Unknown."""
    out: list[str] = []
    fed: set[str] = set()
    for part in template_parts(template):
        if part.problem is not None:
            raise Unknown(f"the value {template!r} can't be resolved: {part.problem}")
        if part.variable is None:
            out.append(part.text)
            continue
        value = scope.lookup(part.variable)
        if part.variable in scope.writers:
            fed.add(scope.writers[part.variable])
        if value is None:
            if part.default is not None:
                value = part.default
            elif ignore_unresolved:
                value = ""
            else:
                raise Unknown(f"{part.variable} is not set, and IgnoreUnresolvedVariables is not true")
        out.append(value)
    return "".join(out), frozenset(fed)


# ---------------------------------------------------------------- the proxy's flow


@dataclass(frozen=True, slots=True)
class Placed:
    """A step and where it sits: ``key`` is unique per step in the bundle."""

    key: str
    step: Step
    side: str
    endpoint: str
    flow: str | None
    index: int


@dataclass(frozen=True, slots=True)
class HeaderState:
    name: str
    value: str | None
    known: bool = True
    writer: str | None = None
    fed_by: frozenset[str] = frozenset()


@dataclass
class Outcome:
    """What one call does: status, body (when certain), whether the backend was called and with which headers."""

    status: int
    body: bytes | None
    reached_backend: bool
    headers: dict[str, HeaderState]
    executed: list[str]
    stopped_by: str | None = None

    def asserted_headers(self) -> dict[str, HeaderState]:
        """The backend headers some step set or removed, with a known value (what the battery checks)."""
        return {k: h for k, h in self.headers.items() if h.writer is not None and h.known and k not in HOP_HEADERS}


@dataclass
class CaseState:
    """State that lives across the calls of one test case: rate limits, quota counters, the response cache."""

    spike_passed: dict[str, int] = field(default_factory=dict)
    quota_used: dict[str, int] = field(default_factory=dict)
    cache: dict[str, tuple[int, bytes | None]] = field(default_factory=dict)


class _Stop(Exception):
    def __init__(self, status: int, body: bytes | None, step: str) -> None:
        super().__init__(status)
        self.status = status
        self.body = body
        self.step = step


@dataclass
class _Call:
    scope: Scope
    headers: dict[str, HeaderState]
    index: int
    immediate: bool
    status: int = BACKEND_STATUS
    body: bytes | None = None
    cache_keys: dict[str, str] = field(default_factory=dict)
    executed: list[str] = field(default_factory=list)
    # The headers of the backend call, once the request side is done (None: the backend was not called).
    backend_headers: dict[str, HeaderState] | None = None


class Model:
    """The proxy's flows and policies, and the facts the battery fixes (allowed API keys)."""

    def __init__(self, bundle: Bundle, valid_keys: Mapping[str, Sequence[str]]) -> None:
        self.bundle = bundle
        self.policies = {p.name: p for p in bundle.policies}
        self.targets = {t.name: t for t in bundle.target_endpoints}
        self.valid_keys = {name: tuple(keys) for name, keys in valid_keys.items()}

    # ------------------------------------------------------------ where the steps are

    def placed_steps(self) -> list[Placed]:
        """Every request and response step of every endpoint and target, in Apigee's order per endpoint."""
        found: list[Placed] = []
        for endpoint in self.bundle.proxy_endpoints:
            found += self._endpoint_steps(f"proxy:{endpoint.name}", endpoint.pre_flow.request,
                                          endpoint.flows, endpoint.post_flow.request, REQUEST)
        for target in self.bundle.target_endpoints:
            found += self._endpoint_steps(f"target:{target.name}", target.pre_flow.request,
                                          target.flows, target.post_flow.request, REQUEST)
        for target in self.bundle.target_endpoints:
            found += self._endpoint_steps(f"target:{target.name}", target.pre_flow.response,
                                          target.flows, target.post_flow.response, RESPONSE)
        for endpoint in self.bundle.proxy_endpoints:
            found += self._endpoint_steps(f"proxy:{endpoint.name}", endpoint.pre_flow.response,
                                          endpoint.flows, endpoint.post_flow.response, RESPONSE)
        return found

    @staticmethod
    def _endpoint_steps(
        where: str, pre: Sequence[Step], flows: Sequence[Flow], post: Sequence[Step], side: str
    ) -> list[Placed]:
        found: list[Placed] = []
        for index, step in enumerate(pre):
            found.append(Placed(f"{where}/PreFlow/{side}/{index}", step, side, where, None, index))
        for flow in flows:
            for index, step in enumerate(flow.request if side == REQUEST else flow.response):
                found.append(Placed(f"{where}/Flow:{flow.name}/{side}/{index}", step, side, where, flow.name, index))
        for index, step in enumerate(post):
            found.append(Placed(f"{where}/PostFlow/{side}/{index}", step, side, where, None, index))
        return found

    def endpoint_of(self, placed: Placed) -> ProxyEndpoint | None:
        """The proxy endpoint a test request for ``placed`` goes to (a target step: the first that routes to it)."""
        kind, name = placed.endpoint.split(":", 1)
        if kind == "proxy":
            return next((e for e in self.bundle.proxy_endpoints if e.name == name), None)
        return next(
            (e for e in self.bundle.proxy_endpoints if any(r.target == name for r in e.route_rules)), None
        )

    def path_constraints(self, placed: Placed, endpoint: ProxyEndpoint) -> list[tuple[str, bool]] | str:
        """The (condition, wanted value) pairs a request must meet to reach ``placed``, or why none can."""
        constraints: list[tuple[str, bool]] = []
        kind, name = placed.endpoint.split(":", 1)
        if kind == "target":
            for rule in endpoint.route_rules:
                if rule.target == name:
                    if rule.condition:
                        constraints.append((rule.condition, True))
                    break
                if not rule.condition:
                    return f"an earlier RouteRule {rule.name} without a condition always wins"
                constraints.append((rule.condition, False))
        flows = endpoint.flows if kind == "proxy" else self.targets[name].flows
        if placed.flow is not None:
            for flow in flows:
                if flow.name == placed.flow:
                    if flow.condition:
                        constraints.append((flow.condition, True))
                    break
                if not flow.condition:
                    return f"an earlier Flow {flow.name} without a condition always runs instead"
                constraints.append((flow.condition, False))
        if placed.step.condition:
            constraints.append((placed.step.condition, True))
        return constraints

    # ------------------------------------------------------------ one call

    def run_call(self, endpoint: ProxyEndpoint, req: Req, state: CaseState, index: int, immediate: bool) -> Outcome:
        """What Apigee does with ``req`` sent to ``endpoint``; raises Unknown when a2m can't tell."""
        scope = Scope(endpoint.base_path or "", req)
        headers = {fold(k): HeaderState(k, v) for k, v in req.headers}
        call = _Call(scope, headers, index, immediate)
        where = f"proxy:{endpoint.name}"
        try:
            self._steps(call, state, where, None, endpoint.pre_flow.request, REQUEST, "PreFlow")
            flow = self._choose_flow(endpoint.flows, scope)
            if flow is not None:
                self._steps(call, state, where, flow.name, flow.request, REQUEST, f"Flow:{flow.name}")
            self._steps(call, state, where, None, endpoint.post_flow.request, REQUEST, "PostFlow")
            target = self._route(endpoint, scope)
            twhere = f"target:{target.name}"
            self._steps(call, state, twhere, None, target.pre_flow.request, REQUEST, "PreFlow")
            tflow = self._choose_flow(target.flows, scope)
            if tflow is not None:
                self._steps(call, state, twhere, tflow.name, tflow.request, REQUEST, f"Flow:{tflow.name}")
            self._steps(call, state, twhere, None, target.post_flow.request, REQUEST, "PostFlow")
            call.backend_headers = dict(call.headers)
            call.status, call.body = BACKEND_STATUS, None
            self._steps(call, state, twhere, None, target.pre_flow.response, RESPONSE, "PreFlow")
            if tflow is not None:
                self._steps(call, state, twhere, tflow.name, tflow.response, RESPONSE, f"Flow:{tflow.name}")
            self._steps(call, state, twhere, None, target.post_flow.response, RESPONSE, "PostFlow")
            self._steps(call, state, where, None, endpoint.pre_flow.response, RESPONSE, "PreFlow")
            if flow is not None:
                self._steps(call, state, where, flow.name, flow.response, RESPONSE, f"Flow:{flow.name}")
            self._steps(call, state, where, None, endpoint.post_flow.response, RESPONSE, "PostFlow")
        except Unknown as exc:
            exc.executed = tuple(call.executed)
            raise
        except _Stop as stop:
            handlers = self._fault_handlers(endpoint, stop.step)
            if handlers:
                unknown = FaultRuleUnknown(
                    f"the error {stop.status} it expects could be changed by {', '.join(handlers)}, which a2m does "
                    "not simulate"
                )
                unknown.executed = tuple(call.executed)
                raise unknown from None
            sent = call.backend_headers if call.backend_headers is not None else call.headers
            return Outcome(stop.status, stop.body, call.backend_headers is not None, sent, call.executed, stop.step)
        self._populate_cache(call, state)
        return Outcome(call.status, call.body, True, call.backend_headers or {}, call.executed)

    def _fault_handlers(self, endpoint: ProxyEndpoint, step: str) -> list[str]:
        """The FaultRules and DefaultFaultRule that may handle an error raised at ``step`` (a step key)."""
        found: list[str] = []
        holders: list[ProxyEndpoint | TargetEndpoint] = [endpoint]
        if step.startswith("target:"):
            target = self.targets.get(step.split("/", 1)[0].split(":", 1)[1])
            if target is not None:
                holders.insert(0, target)
        for holder in holders:
            kind = "TargetEndpoint" if isinstance(holder, TargetEndpoint) else "ProxyEndpoint"
            found += [f"FaultRule {rule.name} of {kind} {holder.name}" for rule in holder.fault_rules]
            if holder.default_fault_rule is not None:
                found.append(f"the DefaultFaultRule of {kind} {holder.name}")
        return found

    def _choose_flow(self, flows: Sequence[Flow], scope: Scope) -> Flow | None:
        for flow in flows:
            if not flow.condition:
                return flow
            if evaluate(parse_condition(flow.condition), scope):
                return flow
        return None

    def _route(self, endpoint: ProxyEndpoint, scope: Scope) -> TargetEndpoint:
        for rule in endpoint.route_rules:
            if rule.condition and not evaluate(parse_condition(rule.condition), scope):
                continue
            if rule.target is None:
                raise Unknown(f"RouteRule {rule.name} has no target endpoint (a null or URL route)")
            target = self.targets.get(rule.target)
            if target is None:
                raise Unknown(f"RouteRule {rule.name} names the missing target endpoint {rule.target}")
            return target
        raise Unknown(f"no RouteRule of proxy endpoint {endpoint.name} matches")

    def _steps(
        self, call: _Call, state: CaseState, where: str, flow: str | None, steps: Sequence[Step], side: str,
        part: str,
    ) -> None:
        for index, step in enumerate(steps):
            key = f"{where}/{part}/{side}/{index}"
            if step.condition and not evaluate(parse_condition(step.condition), call.scope):
                continue
            policy = self.policies.get(step.policy)
            if policy is None:
                raise Unknown(f"step {step.name} names a policy that is not in the bundle")
            if not policy.enabled:
                continue
            call.executed.append(key)
            try:
                self._policy(call, state, policy, key, side)
            except _Stop:
                if policy.continue_on_error and policy.type != "RaiseFault":
                    continue
                raise

    # ------------------------------------------------------------ policy semantics

    def _policy(self, call: _Call, state: CaseState, policy: Policy, key: str, side: str) -> None:
        handler = {
            "VerifyAPIKey": self._verify_api_key,
            "SpikeArrest": self._spike_arrest,
            "Quota": self._quota,
            "AssignMessage": self._assign_message,
            "ExtractVariables": self._extract_variables,
            "RaiseFault": self._raise_fault,
            "BasicAuthentication": self._basic_auth,
            "AccessControl": self._access_control,
            "ResponseCache": self._response_cache,
        }.get(policy.type)
        if handler is None:
            if policy.type in NO_EFFECT_TYPES:
                return
            raise Unknown(f"{policy.type} {policy.name} runs on the way, and a2m cannot predict what it does")
        handler(call, state, policy, key, side)

    def _verify_api_key(self, call: _Call, state: CaseState, policy: Policy, key: str, side: str) -> None:
        if side != REQUEST:
            raise Unknown(f"VerifyAPIKey {policy.name} runs in a response flow")
        api_key = child(policy.settings, "APIKey")
        value = _read_ref(api_key.attributes.get("ref", "") if api_key is not None else "", call.scope, policy)
        if value is None or not value.strip():
            raise _Stop(401, None, key)
        if value not in self.valid_keys.get(policy.name, ()):
            raise _Stop(401, None, key)
        call.scope.variables["client_id"] = value
        call.scope.writers["client_id"] = key

    def _spike_arrest(self, call: _Call, state: CaseState, policy: Policy, key: str, side: str) -> None:
        if spike_rate(policy) is None:
            raise Unknown(f"SpikeArrest {policy.name} has no fixed rate a2m can read")
        if child(policy.settings, "MessageWeight") is not None or is_true(text(child(policy.settings, "UseEffectiveCount"))):
            raise Unknown(f"SpikeArrest {policy.name} uses MessageWeight or UseEffectiveCount")
        last = state.spike_passed.get(policy.name)
        if call.immediate and last == call.index - 1:
            raise _Stop(429, None, key)
        state.spike_passed[policy.name] = call.index

    def _quota(self, call: _Call, state: CaseState, policy: Policy, key: str, side: str) -> None:
        allowed = quota_allowance(policy)
        if allowed is None:
            raise Unknown(f"Quota {policy.name} has no fixed allowance and window a2m can read")
        used = state.quota_used.get(policy.name, 0)
        if used + 1 > allowed[0]:
            raise _Stop(429, None, key)
        state.quota_used[policy.name] = used + 1

    def _assign_message(self, call: _Call, state: CaseState, policy: Policy, key: str, side: str) -> None:
        settings = policy.settings
        assign_to = child(settings, "AssignTo")
        target = side
        if assign_to is not None:
            if is_true(assign_to.attributes.get("createNew")) or (assign_to.text or "").strip():
                self._assign_variables(call, policy, key)
                return
            target = fold((assign_to.attributes.get("type") or side).strip())
        copy = child(settings, "Copy")
        if copy is not None and copy.children:
            raise Unknown(f"AssignMessage {policy.name} copies parts of a message, which a2m does not predict")
        ignore = is_true(text(child(settings, "IgnoreUnresolvedVariables")))
        if target == REQUEST and side == REQUEST:
            remove = child(child(settings, "Remove"), "Headers")
            if remove is not None:
                names = children(remove, "Header")
                if not names:
                    raise Unknown(f"AssignMessage {policy.name} removes every request header")
                for header in names:
                    self._set_header(call, header.attributes.get("name", ""), None, key, frozenset())
            for op in ("Add", "Set"):
                for header in children(child(child(settings, op), "Headers"), "Header"):
                    name = header.attributes.get("name", "")
                    try:
                        value, fed = resolve_template(header.text or "", call.scope, ignore_unresolved=ignore)
                    except Unknown:
                        self._unknown_header(call, name, key)
                        continue
                    if op == "Add" and call.headers.get(fold(name), HeaderState(name, None)).value is not None:
                        self._unknown_header(call, name, key)
                        continue
                    self._set_header(call, name, value, key, fed)
                for param in children(child(child(settings, op), "QueryParams"), "QueryParam"):
                    name = param.attributes.get("name", "")
                    try:
                        value, _ = resolve_template(param.text or "", call.scope, ignore_unresolved=ignore)
                    except Unknown:
                        raise Unknown(f"AssignMessage {policy.name} sets query parameter {name} to an unknown value") from None
                    call.scope.req = call.scope.req.with_param(name, value)
            for tag in ("Verb", "Path"):
                if child(child(settings, "Set"), tag) is not None:
                    raise Unknown(f"AssignMessage {policy.name} changes the request {fold(tag)}")
        elif target == RESPONSE and side == RESPONSE:
            status = text(child(child(settings, "Set"), "StatusCode"))
            if status is not None:
                if not status.isdigit():
                    raise Unknown(f"AssignMessage {policy.name} sets the status from {status!r}")
                call.status = int(status)
            if child(child(settings, "Set"), "Payload") is not None:
                call.body = None
        self._assign_variables(call, policy, key)

    def _assign_variables(self, call: _Call, policy: Policy, key: str) -> None:
        ignore = is_true(text(child(policy.settings, "IgnoreUnresolvedVariables")))
        for assign in children(policy.settings, "AssignVariable"):
            name = text(child(assign, "Name"))
            if not name:
                continue
            try:
                value: str | None
                if child(assign, "Ref") is not None:
                    value = call.scope.lookup(text(child(assign, "Ref")) or "")
                    if value is None:
                        value = text(child(assign, "Value"))
                elif child(assign, "Template") is not None:
                    value, _ = resolve_template(text(child(assign, "Template")) or "", call.scope, ignore_unresolved=ignore)
                else:
                    value = text(child(assign, "Value"))
            except Unknown:
                call.scope.unknown_variables.add(name)
                continue
            if value is None:
                call.scope.variables.pop(name, None)
            else:
                call.scope.variables[name] = value
            call.scope.unknown_variables.discard(name)
            call.scope.writers[name] = key

    def _set_header(self, call: _Call, name: str, value: str | None, key: str, fed: frozenset[str]) -> None:
        if not name:
            return
        call.headers[fold(name)] = HeaderState(name, value, True, key, fed)
        call.scope.req = call.scope.req.with_header(name, value)

    def _unknown_header(self, call: _Call, name: str, key: str) -> None:
        if name:
            call.headers[fold(name)] = HeaderState(name, None, False, key)

    def _extract_variables(self, call: _Call, state: CaseState, policy: Policy, key: str, side: str) -> None:
        settings = policy.settings
        source = fold(text(child(settings, "Source")) or side)
        prefix = text(child(settings, "VariablePrefix"))
        readable = side == REQUEST and source in (REQUEST, "message")
        for item in settings.children:
            if item.tag in ("QueryParam", "Header") and readable:
                name = item.attributes.get("name", "")
                raw = call.scope.req.param(name) if item.tag == "QueryParam" else call.scope.req.header(name)
                pattern = children(item, "Pattern")
                if not pattern or not name:
                    continue
                for variable, value in _match_pattern(pattern[0], raw or ""):
                    full = f"{prefix}.{variable}" if prefix else variable
                    call.scope.unknown_variables.discard(full)
                    if value is None:
                        call.scope.variables.pop(full, None)
                    else:
                        call.scope.variables[full] = value
                    call.scope.writers[full] = key
            else:
                for variable in _named_variables(item):
                    call.scope.unknown_variables.add(f"{prefix}.{variable}" if prefix else variable)

    def _raise_fault(self, call: _Call, state: CaseState, policy: Policy, key: str, side: str) -> None:
        settings = child(child(policy.settings, "FaultResponse"), "Set")
        status_text = text(child(settings, "StatusCode")) or "500"
        if not status_text.isdigit():
            raise Unknown(f"RaiseFault {policy.name} sets the status from {status_text!r}")
        payload = child(settings, "Payload")
        body: bytes | None = None
        if payload is not None and payload.text is not None:
            parts = template_parts(payload.text)
            if all(p.variable is None and p.problem is None for p in parts):
                body = payload.text.strip().encode("utf-8")
        elif payload is None:
            body = None
        raise _Stop(int(status_text), body, key)

    def _basic_auth(self, call: _Call, state: CaseState, policy: Policy, key: str, side: str) -> None:
        settings = policy.settings
        operation = fold(text(child(settings, "Operation")) or "")
        user_ref = child(settings, "User")
        password_ref = child(settings, "Password")
        users = (user_ref.attributes.get("ref", "") if user_ref else "", password_ref.attributes.get("ref", "") if password_ref else "")
        if operation == "decode":
            source = text(child(settings, "Source")) or ""
            raw = _read_ref(source, call.scope, policy)
            if raw is None and is_true(text(child(settings, "IgnoreUnresolvedVariables"))):
                return
            match = BASIC_VALUE.match(raw or "")
            if match is None:
                raise _Stop(500, None, key)
            try:
                decoded = base64.b64decode(match.group(1), validate=True).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError):
                raise _Stop(500, None, key) from None
            if ":" not in decoded:
                raise _Stop(500, None, key)
            user, password = decoded.split(":", 1)
            for ref, value in zip(users, (user, password), strict=True):
                if ref:
                    call.scope.variables[ref] = value
                    call.scope.unknown_variables.discard(ref)
                    call.scope.writers[ref] = key
            return
        if operation == "encode":
            assign_to = text(child(settings, "AssignTo")) or ""
            if not fold(assign_to).startswith("request.header.") or side != REQUEST:
                raise Unknown(f"BasicAuthentication {policy.name} writes {assign_to!r}")
            name = assign_to.split(".", 2)[2]
            try:
                plain_user, plain_password = (call.scope.lookup(ref) for ref in users)
            except Unknown:
                self._unknown_header(call, name, key)
                return
            if plain_user is None or plain_password is None:
                raise Unknown(f"BasicAuthentication {policy.name} encodes variables that are not set")
            value = "Basic " + base64.b64encode(f"{plain_user}:{plain_password}".encode()).decode("ascii")
            self._set_header(call, name, value, key, frozenset(call.scope.writers.get(r, "") for r in users) - {""})
            return
        raise Unknown(f"BasicAuthentication {policy.name} has the operation {operation!r}")

    def _access_control(self, call: _Call, state: CaseState, policy: Policy, key: str, side: str) -> None:
        if side != REQUEST or child(policy.settings, "ValidateBasedOn") is not None:
            raise Unknown(f"AccessControl {policy.name} checks something other than the caller's address")
        decision = access_decision(policy, CLIENT_ADDRESS)
        if decision is None:
            raise Unknown(f"AccessControl {policy.name} has rules a2m cannot read")
        if decision == "DENY":
            raise _Stop(403, None, key)

    def _response_cache(self, call: _Call, state: CaseState, policy: Policy, key: str, side: str) -> None:
        settings = policy.settings
        if child(settings, "SkipCacheLookup") is not None or child(settings, "SkipCachePopulation") is not None:
            raise Unknown(f"ResponseCache {policy.name} skips the cache on conditions a2m does not predict")
        if side == REQUEST:
            cache_key = self._cache_key(policy, call.scope)
            call.cache_keys[policy.name] = cache_key
            hit = state.cache.get(f"{policy.name}\0{cache_key}")
            if hit is not None:
                raise _Stop(hit[0], hit[1], key)
            return
        if policy.name not in call.cache_keys:
            raise Unknown(f"ResponseCache {policy.name} runs in a response flow without its request step")

    def _cache_key(self, policy: Policy, scope: Scope) -> str:
        expiry = text(child(child(policy.settings, "ExpirySettings"), "TimeoutInSec"))
        if expiry is None or not expiry.isdigit() or int(expiry) < MIN_CACHE_SECONDS:
            raise Unknown(f"ResponseCache {policy.name} has no fixed expiry of at least {MIN_CACHE_SECONDS} seconds")
        fragments = children(child(policy.settings, "CacheKey"), "KeyFragment")
        if not fragments:
            return scope.req.path(scope.base_path)
        parts: list[str] = []
        for fragment in fragments:
            ref = fragment.attributes.get("ref")
            value = scope.lookup(ref) if ref else (fragment.text or "")
            parts.append(value or "")
        return "__".join(parts)

    def _populate_cache(self, call: _Call, state: CaseState) -> None:
        if 200 <= call.status < 300:
            for name, cache_key in call.cache_keys.items():
                state.cache[f"{name}\0{cache_key}"] = (call.status, call.body)


def _read_ref(ref: str, scope: Scope, policy: Policy) -> str | None:
    folded = fold(ref.strip())
    if folded.startswith(("request.header.", "request.queryparam.")) and folded.count(".") == 2:
        return scope.lookup(ref.strip())
    raise Unknown(f"{policy.type} {policy.name} reads {ref!r}, which a test request does not set")


def _match_pattern(pattern: XmlElement, value: str) -> list[tuple[str, str | None]]:
    """(variable, extracted value or None) for each {variable} of an ExtractVariables <Pattern>."""
    raw = pattern.text or ""
    names = re.findall(r"\{([A-Za-z_][\w.\-]*)\}", raw)
    pieces = re.split(r"\{[A-Za-z_][\w.\-]*\}", raw)
    regex = "(.+?)".join(re.escape(piece) for piece in pieces)
    flags = re.IGNORECASE if is_true(pattern.attributes.get("ignoreCase")) else 0
    match = re.fullmatch(regex, value, flags | re.DOTALL) if value else None
    if match is None:
        return [(name, None) for name in names]
    return list(zip(names, match.groups(), strict=True))


def _named_variables(item: XmlElement) -> list[str]:
    found: list[str] = []
    for element in [item, *_descendants(item)]:
        if element.tag == "Pattern" and element.text:
            found += re.findall(r"\{([A-Za-z_][\w.\-]*)\}", element.text)
        if element.tag == "Variable" and element.attributes.get("name"):
            found.append(element.attributes["name"])
    return found


def _descendants(element: XmlElement) -> Iterator[XmlElement]:
    for sub in element.children:
        yield sub
        yield from _descendants(sub)


def sample_pattern_value(pattern: XmlElement) -> str:
    """A value that matches an ExtractVariables <Pattern>, with SAMPLE_VALUE for each {variable}."""
    raw = (pattern.text or "").strip()
    return re.sub(r"\{[A-Za-z_][\w.\-]*\}", SAMPLE_VALUE, raw)


# ---------------------------------------------------------------- policy settings the battery reads


def spike_rate(policy: Policy) -> tuple[int, str] | None:
    """(count, "ps" or "pm") of a SpikeArrest's fixed <Rate>, or None."""
    rate = child(policy.settings, "Rate")
    if rate is None or rate.attributes.get("ref"):
        return None
    match = RATE.match(rate.text or "")
    if match is None or int(match.group(1)) <= 0:
        return None
    return int(match.group(1)), match.group(2)


def quota_allowance(policy: Policy) -> tuple[int, int, str] | None:
    """(allowed calls, interval, time unit) of a Quota with a fixed allowance and window, or None."""
    allow = child(policy.settings, "Allow")
    count = (allow.attributes.get("count", "") if allow is not None else "").strip()
    if allow is None or allow.attributes.get("countRef") or not count.isdigit():
        return None
    interval_element = child(policy.settings, "Interval")
    interval = text(interval_element) or ""
    unit = fold(text(child(policy.settings, "TimeUnit")) or "")
    if (interval_element is not None and interval_element.attributes.get("ref")) or not interval.isdigit():
        return None
    if unit not in ("minute", "hour", "day", "week", "month"):
        return None
    return int(count), int(interval), unit


def access_decision(policy: Policy, address: str) -> str | None:
    """"ALLOW" or "DENY" for a caller at ``address``, or None when a rule can't be read."""
    rules = child(policy.settings, "IPRules")
    if rules is None:
        return None
    default = shout(rules.attributes.get("noRuleMatchAction", "ALLOW").strip())
    if default not in ("ALLOW", "DENY"):
        return None
    client = ipaddress.IPv4Address(address)
    for rule in children(rules, "MatchRule"):
        action = shout(rule.attributes.get("action", "").strip())
        if action not in ("ALLOW", "DENY"):
            return None
        for source in children(rule, "SourceAddress"):
            mask = source.attributes.get("mask", "32").strip()
            if source.attributes.get("ref") or not mask.isdigit() or int(mask) > 32:
                return None
            try:
                network = ipaddress.IPv4Network(f"{(source.text or '').strip()}/{mask}", strict=False)
            except ValueError:
                return None
            if client in network:
                return action
    return default
