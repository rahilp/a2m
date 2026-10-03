"""Quota: a call counter per time window, calls over the allowance get 429.

The template counts calls in fixed windows of Interval x TimeUnit (aligned to
the Unix epoch), per identifier or shared, in a non-persistent object store.
Apigee's windows start differently (per quota type), so every Quota is tagged
``time-window`` for review.

Concurrent calls must not be counted as one, so the counter is built only on
atomic inserts (os:store failIfPresent, see :func:`a2m.policies.common.os_store`),
never on a read followed by a write. The allowance is a row of numbered slots
per window; a call is allowed when it inserts one free slot, and rejected when
every slot is taken, so at most Allow calls pass however many arrive at once.
A hint (the highest slot a call has taken) lets the next call start right
above the taken slots instead of at slot 1; every slot up to any hint written
is taken, so starting there never skips a free one. The cost is one small
object store entry per allowed call until its window ends.
"""

from __future__ import annotations

from a2m.ir import Policy
from a2m.policies.common import (
    NOW_MILLIS,
    OS_KEY_PRESENT,
    Draft,
    TemplateOutput,
    child,
    choice,
    flag,
    fold,
    foreach,
    key_part,
    object_store,
    on_error_continue,
    os_retrieve,
    os_store,
    raise_error,
    read_variable,
    set_variable,
    text,
    try_scope,
)

TIME_WINDOW_TAG = "time-window"
UNIT_MILLIS = {"minute": 60_000, "hour": 3_600_000, "day": 86_400_000, "week": 604_800_000}
WINDOW_VAR = "a2mQuotaWindow"
ID_VAR = "a2mQuotaId"
HINT_VAR = "a2mQuotaHint"
SLOT_VAR = "a2mQuotaSlot"
# Raised inside the slot loop once a slot is taken, to leave the loop; handled right outside it.
SLOT_TAKEN_ERROR = "A2M:QUOTA_SLOT_TAKEN"
HANDLED = {
    "type",
    "Allow",
    "Interval",
    "TimeUnit",
    "Identifier",
    "Distributed",
    "Synchronous",
}


def translate(policy: Policy, *, direction: str) -> TemplateOutput:
    draft = Draft(policy, direction, handled=HANDLED)
    draft.tags.append(TIME_WINDOW_TAG)
    settings = draft.settings
    allow = child(settings, "Allow")
    count_text = (allow.attributes.get("count", "") if allow is not None else "").strip()
    if not count_text.isdigit():
        return draft.skip(f"Quota {policy.name} has no fixed <Allow count>, so there is no allowance to translate")
    if allow is not None and allow.attributes.get("countRef"):
        draft.option("Allow countRef", f"the allowance from variable {allow.attributes['countRef']} is not read")
    if allow is not None and allow.children:
        draft.option("Allow Class", "per-class allowances are not carried over; the plain count is used")
    interval_text = text(child(settings, "Interval")) or ""
    unit = fold(text(child(settings, "TimeUnit")) or "")
    if not interval_text.isdigit() or int(interval_text) == 0:
        return draft.skip(f"Quota {policy.name} has no fixed positive <Interval>, so there is no window to translate")
    if unit not in UNIT_MILLIS:
        return draft.skip(
            f"Quota {policy.name} has the <TimeUnit> '{unit}'; only minute, hour, day and week are translated"
        )
    kind = settings.attributes.get("type")
    if kind:
        draft.option("type", f"quota type {kind} is not carried over; windows are fixed and aligned to the clock")
    if flag(child(settings, "Distributed")):
        draft.option("Distributed", "Distributed=true is not carried over; each Mule node counts on its own")

    window = int(interval_text) * UNIT_MILLIS[unit]
    identity = "''"
    identifier = child(settings, "Identifier")
    if identifier is not None:
        ref = identifier.attributes.get("ref", "").strip()
        reader = read_variable(ref, direction) if ref else None
        if reader is None:
            return draft.skip(
                f"Quota {policy.name} counts per identifier '{ref or identifier.text or ''}', "
                "which a2m cannot read here; a shared counter would reject other callers"
            )
        identity = f"(({reader} default '') as String)"

    allowed = int(count_text)
    store = f"a2m-quota-{key_part(policy.name)}"
    # Entries are written inside their window, so they last at least until it ends.
    draft.globals.append(object_store(store, window))
    # The window and slot numbers first, the identity last: no identity can make two keys collide.
    hint_key = f"#['quota-hint:' ++ vars.{WINDOW_VAR} ++ ':' ++ vars.{ID_VAR}]"
    slot_key = f"#['quota-slot:' ++ vars.{WINDOW_VAR} ++ ':' ++ (payload as String) ++ ':' ++ vars.{ID_VAR}]"
    hint = f"(vars.{HINT_VAR} as Number)"
    free_slots = f"#[if ({hint} >= {allowed}) [] else (({hint} + 1) to {allowed})]"
    take_slot = foreach(
        free_slots,
        [
            try_scope(
                [
                    os_store(store, slot_key, "#[true]", fail_if_present=True),
                    set_variable(SLOT_VAR, "#[payload]"),
                    raise_error(SLOT_TAKEN_ERROR, "a quota slot was taken; leave the slot loop"),
                ],
                on_error_continue(OS_KEY_PRESENT),
            )
        ],
        counter="a2mQuotaSlotCounter",
        root="a2mQuotaSlotRoot",
    )
    draft.add(
        set_variable(WINDOW_VAR, f"#[floor({NOW_MILLIS} / {window}) as String]"),
        set_variable(ID_VAR, f"#[{identity}]"),
        os_retrieve(store, hint_key, HINT_VAR, "#[0]"),
        set_variable(SLOT_VAR, "#[0]"),
        # The outer one-pass loop restores the caller's message, which leaving the slot loop by an error
        # would otherwise replace with a slot number.
        foreach(
            "#[[1]]",
            [try_scope([take_slot], on_error_continue(SLOT_TAKEN_ERROR))],
            counter="a2mQuotaPassCounter",
            root="a2mQuotaPassRoot",
        ),
        choice(
            (
                f"#[(vars.{SLOT_VAR} as Number) == 0]",
                draft.fault(
                    429,
                    f"Rate limit quota violation. Quota limit {allowed} per {interval_text} {unit} exceeded",
                    "policies.ratelimit.QuotaViolation",
                ),
            ),
            otherwise=[os_store(store, hint_key, f"#[vars.{SLOT_VAR}]")],
        ),
    )
    return draft.done()
