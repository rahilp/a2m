"""What the generator made of each step of a proxy: which steps the battery may test.

The battery tests a step only when a2m generated it and it can run in the
app. That is decided from the generator's own records (:class:`GenerateResult`
``policies`` and ``conditions``), never re-decided from the policy type:

* a step whose record is ``skipped`` was not generated (no template, the
  template refused it with the request changes made before it, a disabled
  policy, ...);
* a step whose condition can't be translated is generated inside a
  ``when`` that is ``#[false]``: it never runs in the app;
* a step in a conditional Flow whose condition can't be translated, or in a
  target reached through a RouteRule whose condition can't be translated,
  never runs either (the flow or route is a ``#[false]`` branch).

The engine's generate stage saves these records in the proxy's work folder
(:func:`a2m.layout.generated_steps_path`, file name
:data:`a2m.layout.GENERATED_STEPS_NAME`); the verification
stage reads them back. A caller without them (a project generated elsewhere)
gets the records :func:`a2m.generator.project.plan_project` gives for the
bundle, which are the generator's decisions without an AI provider.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from a2m.generator.project import CANT_TRANSLATE_TAG, GenerateResult, plan_project
from a2m.ir import Bundle
from a2m.policies.common import Method

FORMAT = 1
STEP_KIND = "Step"
FLOW_KIND = "Flow"
ROUTE_KIND = "RouteRule"
PROXY_KIND, TARGET_KIND = "proxy", "target"
NOTHING_TO_TEST = "so there is nothing to test"


@dataclass(frozen=True, slots=True)
class StepRecord:
    """One policy step as the generator recorded it (``location`` as in PolicyResult, e.g. "ProxyEndpoint
    default flow items request")."""

    name: str
    type: str
    method: str
    location: str
    reason: str = ""
    cant_translate: bool = False


@dataclass(frozen=True, slots=True)
class ConditionState:
    """One condition the generator met: translated (``ok``, its ``when`` can be true) or not (``#[false]``)."""

    name: str
    kind: str
    location: str
    ok: bool


@dataclass(frozen=True, slots=True)
class GeneratedSteps:
    steps: tuple[StepRecord, ...] = ()
    conditions: tuple[ConditionState, ...] = ()

    @classmethod
    def from_result(cls, result: GenerateResult) -> GeneratedSteps:
        steps = tuple(
            StepRecord(
                p.name, p.type, str(p.method.value), p.location, p.reason or "", CANT_TRANSLATE_TAG in p.tags
            )
            for p in result.policies
        )
        conditions = tuple(ConditionState(c.name, c.kind, c.location, bool(c.ok)) for c in result.conditions)
        return cls(steps, conditions)

    @classmethod
    def planned(cls, bundle: Bundle, shared_flows: Sequence[Bundle] = ()) -> GeneratedSteps:
        """The generator's records for ``bundle`` without an AI provider (see the module docstring)."""
        return cls.from_result(plan_project(bundle, shared_flows=shared_flows))

    def to_json(self) -> str:
        data = {
            "format": FORMAT,
            "steps": [
                {"name": s.name, "type": s.type, "method": s.method, "location": s.location, "reason": s.reason,
                 "cant_translate": s.cant_translate}
                for s in self.steps
            ],
            "conditions": [
                {"name": c.name, "kind": c.kind, "location": c.location, "ok": c.ok} for c in self.conditions
            ],
        }
        return json.dumps(data, indent=1, ensure_ascii=False) + "\n"

    @classmethod
    def from_json(cls, text: str) -> GeneratedSteps:
        """Raises ValueError when ``text`` is not what :meth:`to_json` writes."""
        try:
            data: Any = json.loads(text)
            if not isinstance(data, dict) or data.get("format") != FORMAT:
                raise ValueError("not a2m's generated-steps format")
            steps = tuple(
                StepRecord(
                    str(s["name"]), str(s["type"]), str(s["method"]), str(s["location"]), str(s["reason"]),
                    bool(s["cant_translate"]),
                )
                for s in data["steps"]
            )
            conditions = tuple(
                ConditionState(str(c["name"]), str(c["kind"]), str(c["location"]), bool(c["ok"]))
                for c in data["conditions"]
            )
        except (KeyError, TypeError) as exc:
            raise ValueError(f"not a2m's generated-steps format: {exc}") from exc
        return cls(steps, conditions)

    # ------------------------------------------------------------ decisions

    def step_problem(
        self, step: str, endpoint: str, flow: str | None, part: str, side: str, route: str | None = None
    ) -> str | None:
        """Why the step ``step`` at this place does not run in the app, or None when a2m generated it there.

        ``endpoint`` is "proxy:<name>" or "target:<name>"; ``flow`` the conditional Flow it is in (None for
        PreFlow and PostFlow, then ``part`` says which); ``side`` "request" or "response"; ``route`` the
        RouteRule (of the proxy endpoint ``route`` belongs to, as "<endpoint>/<rule>") a target step is
        reached through.
        """
        kind, name = endpoint.split(":", 1)
        owner = f"{'ProxyEndpoint' if kind == PROXY_KIND else 'TargetEndpoint'} {name}"
        where = f"{owner} {f'flow {flow}' if flow is not None else part} {side}"
        records = [r for r in self.steps if r.name == step and r.location == where]
        if not records:
            return f"not generated by a2m (the generated app has no step {step} at {where}), {NOTHING_TO_TEST}"
        skipped = next((r for r in records if r.method == Method.SKIPPED.value), None)
        if skipped is not None:
            return f"not generated by a2m ({skipped.reason or 'its template generated nothing'}), {NOTHING_TO_TEST}"
        if any(r.cant_translate for r in records):
            return (
                f"not generated by a2m (its condition can't be translated, so the step never runs in the app), "
                f"{NOTHING_TO_TEST}"
            )
        if flow is not None and self._refused(FLOW_KIND, flow, owner):
            return (
                f"not generated by a2m (the condition of its Flow {flow} can't be translated, so the flow never "
                f"runs in the app), {NOTHING_TO_TEST}"
            )
        if kind == TARGET_KIND and route is not None:
            proxy, _, rule = route.partition("/")
            if self._refused(ROUTE_KIND, rule, f"ProxyEndpoint {proxy}"):
                return (
                    f"not generated by a2m (the condition of RouteRule {rule} can't be translated, so the route "
                    f"to its target is never taken in the app), {NOTHING_TO_TEST}"
                )
        return None

    def _refused(self, kind: str, name: str, location: str) -> bool:
        """Whether a generated ``when`` of that Flow or RouteRule is ``#[false]`` (its condition can't be translated)."""
        return any(c.kind == kind and c.name == name and c.location == location and not c.ok for c in self.conditions)


__all__ = ["ConditionState", "GeneratedSteps", "StepRecord"]
