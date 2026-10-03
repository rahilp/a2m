"""SpikeArrest: a paced rate limit, rejected calls get 429.

Apigee smooths the rate into an even pace: 30pm allows one call every
2000 ms, 10ps one every 100 ms. A call is rejected when another call of the
same identifier (or of everyone, without an Identifier) was allowed less than
one pace interval before it.

Concurrent calls must not both pass, so the check is built only on atomic
inserts (os:store failIfPresent, see :func:`a2m.policies.common.os_store`),
never on a read followed by a write. Time is cut into buckets one interval
long, and a call in bucket b:

1. inserts key b with its time; if b is taken, a call less than one interval
   away was already allowed, so it is rejected;
2. reads key b-1: a time less than one interval before its own means it is
   too soon, so it gives key b back and is rejected; when b-1 is empty it
   inserts a 0 there, so no straggler of bucket b-1 can still be allowed after
   it (if that insert loses, it reads b-1 again and decides on what is there).

Any two allowed calls less than one interval apart sit in the same or in
neighbouring buckets, and both cases end in a failed insert or a seen time.
Under a race a call may be rejected that a strictly serial order would have
allowed; never the other way round.
"""

from __future__ import annotations

import re

from a2m.conditions import NO_CHANGES, RequestChanges
from a2m.ir import Policy
from a2m.policies.common import (
    NOW_MILLIS,
    OS_KEY_MISSING,
    OS_KEY_PRESENT,
    Draft,
    TemplateOutput,
    child,
    choice,
    flag,
    key_part,
    object_store,
    on_error_continue,
    os_remove,
    os_retrieve,
    os_store,
    set_variable,
    text,
    try_scope,
)

RATE = re.compile(r"^(\d+)\s*(pm|ps)$")
PER_UNIT_MILLIS = {"pm": 60_000, "ps": 1_000}
NOW_VAR = "a2mSpikeNow"
BUCKET_VAR = "a2mSpikeBucket"
ID_VAR = "a2mSpikeId"
VERDICT_VAR = "a2mSpikeVerdict"
PREVIOUS_VAR = "a2mSpikePrevious"
ALLOW, REJECT, RELEASE = "allow", "reject", "release"
# The value read for an empty bucket, and the value a call writes to block the bucket before its own.
EMPTY, BLOCKED = -1, 0
HANDLED = {"Rate", "Identifier", "MessageWeight", "UseEffectiveCount"}


def translate(policy: Policy, *, direction: str, changes: RequestChanges = NO_CHANGES) -> TemplateOutput:
    draft = Draft(policy, direction, handled=HANDLED, changes=changes)
    settings = draft.settings
    rate_element = child(settings, "Rate")
    rate = text(rate_element)
    if rate is None:
        ref = rate_element.attributes.get("ref") if rate_element is not None else None
        if ref:
            return draft.skip(
                f"SpikeArrest {policy.name} takes its <Rate> from variable {ref}; only a fixed rate is translated"
            )
        return draft.skip(f"SpikeArrest {policy.name} has no <Rate>, so there is no limit to translate")
    match = RATE.match(rate)
    if match is None or int(match.group(1)) == 0:
        return draft.skip(
            f"SpikeArrest {policy.name} has the rate '{rate}'; only a positive number per minute (pm) "
            "or per second (ps) is translated"
        )
    if rate_element is not None and rate_element.attributes.get("ref"):
        draft.option("Rate ref", f"the rate from variable {rate_element.attributes['ref']} is not read; {rate} is used")
    if flag(child(settings, "UseEffectiveCount")):
        draft.option("UseEffectiveCount", "UseEffectiveCount=true (an unsmoothed count) is not carried over")
    weight = child(settings, "MessageWeight")
    if weight is not None:
        draft.option("MessageWeight", "message weights are not carried over; every call counts as one")

    count, unit = int(match.group(1)), match.group(2)
    interval = max(1, PER_UNIT_MILLIS[unit] // count)
    identity = "'shared'"
    identifier = child(settings, "Identifier")
    if identifier is not None:
        ref = identifier.attributes.get("ref", "").strip()
        read = draft.read(ref) if ref else None
        if read is None or read.dw is None:
            why = f" ({read.reason})" if read is not None and read.reason else ""
            return draft.skip(
                f"SpikeArrest {policy.name} counts per identifier '{ref or identifier.text or ''}', "
                f"which a2m cannot read here{why}; a shared limit would reject other callers"
            )
        identity = f"'client:' ++ (({read.dw} default '') as String)"

    store = f"a2m-spike-arrest-{key_part(policy.name)}"
    # An entry is read until one interval after the end of its bucket at the latest.
    draft.globals.append(object_store(store, 2 * interval))

    def bucket_key(offset: str) -> str:
        # The bucket number first, the identity last: no identity can make two buckets share a key.
        return f"#['spike:' ++ (((vars.{BUCKET_VAR} as Number){offset}) as String) ++ ':' ++ vars.{ID_VAR}]"

    own, before = bucket_key(""), bucket_key(" - 1")
    verdict_is = f"vars.{VERDICT_VAR} == '{{}}'".format
    too_soon = f"#[{verdict_is(ALLOW)} and ((vars.{NOW_VAR} as Number) - (vars.{PREVIOUS_VAR} as Number)) < {interval}]"
    draft.add(
        set_variable(NOW_VAR, f"#[{NOW_MILLIS}]"),
        set_variable(BUCKET_VAR, f"#[floor((vars.{NOW_VAR} as Number) / {interval})]"),
        set_variable(ID_VAR, f"#[{identity}]"),
        set_variable(VERDICT_VAR, f"#['{ALLOW}']"),
        # 1. Take this bucket, or be rejected because a call in it was already allowed.
        try_scope(
            [os_store(store, own, f"#[vars.{NOW_VAR}]", fail_if_present=True)],
            on_error_continue(OS_KEY_PRESENT, [set_variable(VERDICT_VAR, f"#['{REJECT}']")]),
        ),
        # 2. Look at the bucket before: block it when empty, else compare with the time allowed there.
        choice(
            (
                f"#[{verdict_is(ALLOW)}]",
                [
                    os_retrieve(store, before, PREVIOUS_VAR, f"#[{EMPTY}]"),
                    choice(
                        (
                            f"#[(vars.{PREVIOUS_VAR} as Number) == {EMPTY}]",
                            [
                                try_scope(
                                    [os_store(store, before, f"#[{BLOCKED}]", fail_if_present=True)],
                                    on_error_continue(
                                        OS_KEY_PRESENT,
                                        [
                                            os_retrieve(store, before, PREVIOUS_VAR, f"#[{EMPTY}]"),
                                            # Taken and gone again within the race: reject to be safe.
                                            choice(
                                                (
                                                    f"#[(vars.{PREVIOUS_VAR} as Number) == {EMPTY}]",
                                                    [set_variable(VERDICT_VAR, f"#['{RELEASE}']")],
                                                )
                                            ),
                                        ],
                                    ),
                                )
                            ],
                        )
                    ),
                    choice((too_soon, [set_variable(VERDICT_VAR, f"#['{RELEASE}']")])),
                ],
            )
        ),
        # A rejected call that took its bucket gives it back, so later calls in the bucket are judged afresh.
        choice(
            (
                f"#[{verdict_is(RELEASE)}]",
                [try_scope([os_remove(store, own)], on_error_continue(OS_KEY_MISSING))],
            )
        ),
        choice(
            (
                f"#[not ({verdict_is(ALLOW)})]",
                draft.fault(
                    429, f"Spike arrest violation. Allowed rate : {rate}", "policies.ratelimit.SpikeArrestViolation"
                ),
            )
        ),
    )
    return draft.done()
