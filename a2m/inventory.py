"""Every step, policy and condition of a proxy as one report row: nothing in the input is left out.

The rows come from walking the bundle's IR (and the shared flow bundles its
FlowCallout policies call, each once), so their number per kind is the number
of ``<Step>`` elements, policy files and non-empty ``<Condition>`` elements in
the input. Each row is then joined with what the generator recorded about that
item (:class:`GenerateRecords`, saved by the engine's generate stage in the
proxy's work folder): the method (template, ai or skipped), the AI's
confidence and notes, and why an item was not generated. An item the
generator left no record of is still a row, ``skipped`` with that reason.

The records never hold a callout's source code (``PolicyResult.original``).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from a2m.ai.provider import Confidence
from a2m.generator import GenerateResult
from a2m.generator.project import CANT_TRANSLATE_TAG
from a2m.ir import Bundle, DefaultFaultRule, FaultRule, FlowSteps, Policy, ProxyEndpoint, Step, TargetEndpoint
from a2m.policies.common import Method

FORMAT = 1
FLOW_CALLOUT_TYPE = "FlowCallout"
SHARED_FLOW_BUNDLE_TAG = "SharedFlowBundle"
SHARED_FLOW_ENTRY = "default"


class RowKind(StrEnum):
    STEP = "step"
    POLICY = "policy"
    CONDITION = "condition"


# ---------------------------------------------------------------- the generator's records, persisted


@dataclass(frozen=True, slots=True)
class StepRecord:
    """What the generator recorded about one policy step (a PolicyResult without the callout's source code)."""

    name: str
    type: str
    method: str
    location: str
    reason: str = ""
    options: tuple[str, ...] = ()
    cant_translate: bool = False
    confidence: str | None = None
    notes: str = ""
    needs_review: bool = False


@dataclass(frozen=True, slots=True)
class ConditionRecord:
    """What the generator recorded about one condition (see a2m.generator.project.ConditionRecord)."""

    name: str
    kind: str
    location: str
    original: str
    ok: bool
    dw: str | None
    reason: str | None
    method: str
    confidence: str | None = None
    notes: str = ""
    needs_review: bool = False


@dataclass(frozen=True, slots=True)
class OtherItem:
    """Something the generator did not generate that is not a step, policy or condition (a fault rule, a target)."""

    name: str
    reason: str


@dataclass(frozen=True, slots=True)
class GenerateRecords:
    steps: tuple[StepRecord, ...] = ()
    conditions: tuple[ConditionRecord, ...] = ()
    unsupported: tuple[OtherItem, ...] = ()
    enterprise_components: tuple[str, ...] = ()

    @classmethod
    def from_result(cls, result: GenerateResult) -> GenerateRecords:
        steps = tuple(
            StepRecord(
                name=p.name,
                type=p.type,
                method=p.method.value,
                location=p.location,
                reason=p.reason or "",
                options=tuple(f"{o.name}: {o.reason}" for o in p.unsupported_options),
                cant_translate=CANT_TRANSLATE_TAG in p.tags,
                confidence=p.confidence.value if p.confidence is not None else None,
                notes=p.notes or "",
                needs_review=p.needs_review,
            )
            for p in result.policies
        )
        conditions = tuple(
            ConditionRecord(
                name=c.name,
                kind=c.kind,
                location=c.location,
                original=c.original,
                ok=bool(c.ok),
                dw=c.dw,
                reason=c.reason,
                method=c.method.value,
                confidence=c.confidence.value if c.confidence is not None else None,
                notes=c.notes or "",
                needs_review=c.needs_review,
            )
            for c in result.conditions
        )
        others = tuple(OtherItem(u.name, u.reason) for u in result.unsupported)
        return cls(steps, conditions, others, tuple(result.enterprise_components))

    def to_json(self) -> str:
        data = {
            "format": FORMAT,
            "steps": [_fields(s) for s in self.steps],
            "conditions": [_fields(c) for c in self.conditions],
            "unsupported": [_fields(u) for u in self.unsupported],
            "enterprise_components": list(self.enterprise_components),
        }
        return json.dumps(data, indent=1, ensure_ascii=False) + "\n"

    @classmethod
    def from_json(cls, text: str) -> GenerateRecords:
        """Raises ValueError when ``text`` is not what :meth:`to_json` writes."""
        try:
            data: Any = json.loads(text)
            if not isinstance(data, dict) or data.get("format") != FORMAT:
                raise ValueError("not a2m's generate-records format")
            steps = tuple(
                StepRecord(**{**s, "options": tuple(str(o) for o in s["options"])}) for s in data["steps"]
            )
            conditions = tuple(ConditionRecord(**c) for c in data["conditions"])
            others = tuple(OtherItem(**u) for u in data["unsupported"])
            enterprise = tuple(str(e) for e in data["enterprise_components"])
        except (KeyError, TypeError) as exc:
            raise ValueError(f"not a2m's generate-records format: {exc}") from exc
        return cls(steps, conditions, others, enterprise)


def _fields(record: object) -> dict[str, Any]:
    names = getattr(type(record), "__slots__", ())
    return {name: (list(value) if isinstance(value := getattr(record, name), tuple) else value) for name in names}


# ---------------------------------------------------------------- report rows


@dataclass(frozen=True, slots=True)
class Row:
    """One row of a proxy's report table.

    ``item`` is the step or policy name, or for a condition what holds it ("Flow forecast", "Step AM-Verb");
    ``type`` the Apigee policy type (empty for a condition); ``where`` where it sits in the bundle; ``result``
    what the Mule project has for it; ``reason`` why it was skipped or needs review; ``notes`` the AI's notes or
    settings not carried over; ``original`` a condition's text as written in the bundle. ``derived`` is True when
    the row is skipped only because what it belongs to is not generated (a skipped step's condition or policy, a
    fault rule's condition), so a reviewer's question about it is the one about that step or fault rule.
    """

    item: str
    kind: RowKind
    type: str
    where: str
    result: str
    method: Method
    confidence: Confidence | None = None
    needs_review: bool = False
    reason: str = ""
    notes: str = ""
    original: str = ""
    derived: bool = False

    @property
    def ai_unsure(self) -> bool:
        """An AI row a person must check: low or no confidence, or flagged for review."""
        return self.method is Method.AI and (
            self.needs_review or self.confidence is None or self.confidence is Confidence.LOW
        )


@dataclass(frozen=True, slots=True)
class Inventory:
    rows: tuple[Row, ...]
    # Items the generator did not generate that no row already shows as skipped (e.g. a fault rule as a whole).
    others: tuple[OtherItem, ...]
    # Policy type of every policy row (shared flow policies included), for the batch summary.
    policy_types: tuple[str, ...]

    def counts(self) -> dict[str, int]:
        return {kind.value: sum(1 for row in self.rows if row.kind is kind) for kind in RowKind}


@dataclass(frozen=True, slots=True)
class _Placed:
    step: Step
    where: str
    owner: str  # the bundle the step belongs to (its policies)
    holder_skipped_reason: str | None = None


class _Pool:
    """Records handed out once each: first one at the preferred location, else any with the same key."""

    def __init__(self, keyed: Iterable[tuple[tuple[str, ...], str, Any]]) -> None:
        self._items = list(keyed)
        self._used = [False] * len(self._items)

    def take(self, key: tuple[str, ...], location: str, *, exact_only: bool) -> Any | None:
        for index, (k, loc, record) in enumerate(self._items):
            if not self._used[index] and k == key and loc == location:
                self._used[index] = True
                return record
        if exact_only:
            return None
        for index, (k, _loc, record) in enumerate(self._items):
            if not self._used[index] and k == key:
                self._used[index] = True
                return record
        return None


def called_shared_flows(bundle: Bundle, shared_flows: Sequence[Bundle]) -> tuple[Bundle, ...]:
    """The shared flow bundles ``bundle``'s FlowCallout policy files name, and those they call, each once, in the
    order first named; a name with no bundle in the input (or more than one) is left out."""
    by_name: dict[str, list[Bundle]] = {}
    for shared in shared_flows:
        by_name.setdefault(shared.name, []).append(shared)
    found: list[Bundle] = []
    seen: set[str] = set()
    queue = list(_callout_targets(bundle))
    while queue:
        name = queue.pop(0)
        if name in seen:
            continue
        seen.add(name)
        candidates = by_name.get(name, [])
        if len(candidates) != 1:
            continue
        found.append(candidates[0])
        queue.extend(_callout_targets(candidates[0]))
    return tuple(found)


def _callout_targets(bundle: Bundle) -> list[str]:
    names: list[str] = []
    for policy in bundle.policies:
        if policy.type != FLOW_CALLOUT_TYPE:
            continue
        for child in policy.settings.children:
            if child.tag == SHARED_FLOW_BUNDLE_TAG and (child.text or "").strip():
                names.append((child.text or "").strip())
    return names


def build_inventory(bundle: Bundle, shared_flows: Sequence[Bundle], records: GenerateRecords) -> Inventory:
    """The report rows of proxy ``bundle`` (see the module docstring)."""
    called = called_shared_flows(bundle, shared_flows)
    placed, conditions = _walk(bundle, called)
    step_pool = _Pool(((r.name,), r.location, r) for r in records.steps)
    found: list[StepRecord | None] = [step_pool.take((p.step.name,), p.where, exact_only=True) for p in placed]
    found = [
        record if record is not None else step_pool.take((p.step.name,), p.where, exact_only=False)
        for p, record in zip(placed, found, strict=True)
    ]
    policies_of = {b.name: {policy.name: policy for policy in b.policies} for b in (bundle, *called)}
    step_rows: dict[int, Row] = {}
    for index, (p, record) in enumerate(zip(placed, found, strict=True)):
        step_rows[index] = _step_row(p, record, policies_of.get(p.owner, {}))

    cond_pool = _Pool(((c.kind, c.name), c.location, c) for c in records.conditions)
    cond_found = [cond_pool.take((c.holder_kind, c.holder), c.where, exact_only=True) for c in conditions]
    cond_found = [
        record if record is not None else cond_pool.take((c.holder_kind, c.holder), c.where, exact_only=False)
        for c, record in zip(conditions, cond_found, strict=True)
    ]

    rows: list[Row] = []
    # Conditions sit right before what they guard: a step's condition after the step, a flow's before its steps.
    for c, record in zip(conditions, cond_found, strict=True):
        c.record = record
    emitted_conditions: set[int] = set()
    for index, p in enumerate(placed):
        for cindex, c in enumerate(conditions):
            if cindex in emitted_conditions or c.before_step != index:
                continue
            emitted_conditions.add(cindex)
            rows.append(_condition_row(c, None))
        rows.append(step_rows[index])
        for cindex, c in enumerate(conditions):
            if cindex in emitted_conditions or c.step_index != index:
                continue
            emitted_conditions.add(cindex)
            rows.append(_condition_row(c, step_rows[index]))
    for cindex, c in enumerate(conditions):
        if cindex not in emitted_conditions:
            rows.append(_condition_row(c, None))

    policy_types: list[str] = []
    for owner in (bundle, *called):
        for policy in owner.policies:
            users = [step_rows[i] for i, p in enumerate(placed) if p.owner == owner.name and p.step.policy == policy.name]
            rows.append(_policy_row(owner, policy, users, owner is not bundle))
            policy_types.append(policy.type)

    shown = {row.item for row in rows if row.method is Method.SKIPPED and row.kind is not RowKind.CONDITION}
    others = tuple(item for item in records.unsupported if item.name not in shown)
    return Inventory(tuple(rows), others, tuple(policy_types))


# ---------------------------------------------------------------- walking the IR


class _Condition:
    """One non-empty condition met in the walk; ``step_index``/``before_step`` place its row."""

    __slots__ = ("before_step", "holder", "holder_kind", "original", "record", "skipped_reason", "step_index", "where")

    def __init__(
        self,
        holder_kind: str,
        holder: str,
        where: str,
        original: str,
        *,
        step_index: int | None = None,
        before_step: int | None = None,
        skipped_reason: str | None = None,
    ) -> None:
        self.holder_kind = holder_kind
        self.holder = holder
        self.where = where
        self.original = original
        self.step_index = step_index
        self.before_step = before_step
        self.skipped_reason = skipped_reason
        self.record: ConditionRecord | None = None


def _walk(bundle: Bundle, called: Sequence[Bundle]) -> tuple[list[_Placed], list[_Condition]]:
    placed: list[_Placed] = []
    conditions: list[_Condition] = []

    def steps(items: Sequence[Step], where: str, owner: str, holder_skipped: str | None = None) -> None:
        for step in items:
            placed.append(_Placed(step, where, owner, holder_skipped))
            if step.condition and step.condition.strip():
                conditions.append(
                    _Condition("Step", step.name, where, step.condition, step_index=len(placed) - 1)
                )

    def flow_steps(flow: FlowSteps | None, where: str, part: str, owner: str) -> None:
        if flow is None:
            return
        steps(flow.request, f"{where} {part} request", owner)
        steps(flow.response, f"{where} {part} response", owner)

    def fault_rule(rule: FaultRule | DefaultFaultRule, where: str, label: str, kind: str, owner: str) -> None:
        not_generated = f"{label} {rule.name} is not generated by a2m (a2m does not generate fault rules)"
        if rule.condition and rule.condition.strip():
            conditions.append(
                _Condition(kind, rule.name, where, rule.condition, before_step=len(placed), skipped_reason=not_generated)
            )
        steps(rule.steps, f"{where} {label} {rule.name}", owner, not_generated)

    def endpoint(ep: ProxyEndpoint | TargetEndpoint, label: str) -> None:
        where = f"{label} {ep.name}"
        flow_steps(ep.pre_flow, where, "PreFlow", bundle.name)
        for flow in ep.flows:
            if flow.condition and flow.condition.strip():
                conditions.append(_Condition("Flow", flow.name, where, flow.condition, before_step=len(placed)))
            steps(flow.request, f"{where} flow {flow.name} request", bundle.name)
            steps(flow.response, f"{where} flow {flow.name} response", bundle.name)
        flow_steps(ep.post_flow, where, "PostFlow", bundle.name)
        if isinstance(ep, ProxyEndpoint):
            flow_steps(ep.post_client_flow, where, "PostClientFlow", bundle.name)
        else:
            flow_steps(ep.event_flow, where, "EventFlow", bundle.name)
        for rule in ep.fault_rules:
            fault_rule(rule, where, "fault rule", "FaultRule", bundle.name)
        if ep.default_fault_rule is not None:
            fault_rule(ep.default_fault_rule, where, "default fault rule", "DefaultFaultRule", bundle.name)
        if isinstance(ep, ProxyEndpoint):
            for route in ep.route_rules:
                if route.condition and route.condition.strip():
                    conditions.append(_Condition("RouteRule", route.name, where, route.condition))

    for proxy in bundle.proxy_endpoints:
        endpoint(proxy, "ProxyEndpoint")
    for target in bundle.target_endpoints:
        endpoint(target, "TargetEndpoint")
    for shared in called:
        flows = list(shared.shared_flows)
        entry = next((f for f in flows if f.name == SHARED_FLOW_ENTRY), flows[0] if flows else None)
        for flow in flows:
            where = f"shared flow {shared.name}" if flow is entry else f"shared flow {shared.name}/{flow.name}"
            steps(flow.steps, where, shared.name)
    return placed, conditions


# ---------------------------------------------------------------- one row each


def _confidence(value: str | None) -> Confidence | None:
    try:
        return Confidence(value) if value is not None else None
    except ValueError:
        return None


def _method(value: str) -> Method:
    try:
        return Method(value)
    except ValueError:
        return Method.SKIPPED


def _step_row(p: _Placed, record: StepRecord | None, policies: dict[str, Policy]) -> Row:
    policy = policies.get(p.step.policy)
    kind = policy.type if policy is not None else (record.type if record is not None else "unknown")
    if record is None:
        reason = p.holder_skipped_reason or "a2m made no record of generating this step"
        return Row(
            p.step.name, RowKind.STEP, kind, p.where, "not generated", Method.SKIPPED, reason=reason,
            derived=p.holder_skipped_reason is not None,
        )
    method = _method(record.method)
    if p.holder_skipped_reason is not None and method is not Method.SKIPPED:
        method = Method.SKIPPED
    confidence = _confidence(record.confidence)
    notes = "; ".join(f"not carried over: {option}" for option in record.options)
    if method is Method.SKIPPED:
        reason = p.holder_skipped_reason or record.reason or "not generated"
        return Row(
            p.step.name, RowKind.STEP, kind, p.where, "not generated", method, reason=reason, notes=notes,
            derived=p.holder_skipped_reason is not None,
        )
    if method is Method.AI:
        result = "generated from the AI's translation" if confidence is not None else "no usable AI answer"
        notes = "; ".join(part for part in (f"AI notes: {record.notes}" if record.notes else "", notes) if part)
    else:
        result = "generated"
    if record.cant_translate:
        result += ", but it never runs: its condition can't be translated"
    elif record.options:
        result += ", some settings not carried over"
    return Row(
        p.step.name,
        RowKind.STEP,
        kind,
        p.where,
        result,
        method,
        confidence=confidence,
        needs_review=record.needs_review,
        reason=record.reason if (method is Method.AI and record.needs_review) else "",
        notes=notes,
    )


def _condition_row(c: _Condition, step_row: Row | None) -> Row:
    item = f"{c.holder_kind} {c.holder}"
    record = c.record
    if c.skipped_reason is not None:
        return Row(
            item, RowKind.CONDITION, "", c.where, "not generated", Method.SKIPPED,
            reason=f"{c.skipped_reason}, so its condition is not used", original=c.original, derived=True,
        )
    if record is None:
        return Row(
            item, RowKind.CONDITION, "", c.where, "not generated", Method.SKIPPED,
            reason="a2m made no record of translating this condition", original=c.original,
        )
    method = _method(record.method)
    confidence = _confidence(record.confidence)
    if step_row is not None and step_row.method is Method.SKIPPED:
        return Row(
            item, RowKind.CONDITION, "", c.where, "not used", Method.SKIPPED,
            reason=f"step {c.holder} is not generated, so its condition is not used", original=c.original,
            derived=True,
        )
    if record.ok:
        result = f"translated: #[{record.dw}]" if record.dw else "translated"
        reason = (record.reason or "") if record.needs_review else ""
    else:
        result = f"not translated, so the {_HOLDER_WORDS.get(c.holder_kind, 'item')} it guards never runs in the app"
        reason = record.reason or "it can't be translated"
        if method is Method.TEMPLATE:
            method = Method.SKIPPED
    notes = f"AI notes: {record.notes}" if method is Method.AI and record.notes else ""
    return Row(
        item,
        RowKind.CONDITION,
        "",
        c.where,
        result,
        method,
        confidence=confidence,
        needs_review=record.needs_review or (method is Method.AI and not record.ok),
        reason=reason,
        notes=notes,
        original=c.original,
    )


_ORDER = {Method.TEMPLATE: 0, Method.AI: 1, Method.SKIPPED: 2}
_HOLDER_WORDS = {
    "Step": "step",
    "Flow": "flow",
    "RouteRule": "route",
    "FaultRule": "fault rule",
    "DefaultFaultRule": "default fault rule",
}


def _policy_row(owner: Bundle, policy: Policy, users: Sequence[Row], shared: bool) -> Row:
    where = f"{owner.name}: {policy.file}" if shared else policy.file
    if not users:
        return Row(
            policy.name, RowKind.POLICY, policy.type, where, "not used by any step", Method.SKIPPED,
            reason="not used by any step, so nothing is generated for it",
        )
    worst = max(users, key=lambda row: _ORDER[row.method])
    count = len(users)
    result = f"used by {count} step{'s' if count != 1 else ''}"
    if worst.method is Method.SKIPPED:
        result += ", not generated" if all(u.method is Method.SKIPPED for u in users) else ", partly not generated"
    unsure = next((u for u in users if u.ai_unsure), None)
    pick = unsure if worst.method is Method.AI and unsure is not None else worst
    return Row(
        policy.name,
        RowKind.POLICY,
        policy.type,
        where,
        result,
        worst.method,
        confidence=pick.confidence,
        needs_review=pick.needs_review,
        reason=pick.reason,
        notes=pick.notes,
        derived=True,
    )


__all__ = [
    "ConditionRecord",
    "GenerateRecords",
    "Inventory",
    "OtherItem",
    "Row",
    "RowKind",
    "StepRecord",
    "build_inventory",
    "called_shared_flows",
]
