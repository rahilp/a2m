"""AccessControl: allow or deny calls by the client's IPv4 address, 403 when denied.

Rules are checked in their order; the first rule whose address range holds
the client address decides, and noRuleMatchAction decides for every other
address (Apigee's default is ALLOW). The client address is the TCP peer the
Mule listener sees. A rule a2m cannot read (an IPv6 address, a mask outside
0-32, an address from a variable) makes the whole policy unsupported, since
dropping it would change which callers get through.
"""

from __future__ import annotations

import ipaddress

from a2m.ir import Policy
from a2m.policies.common import (
    REQUEST,
    Draft,
    TemplateOutput,
    child,
    children,
    choice,
    dw_string,
    fold,
    text,
)

HANDLED = {"IPRules", "ValidateBasedOn"}
ACTIONS = {"allow": "ALLOW", "deny": "DENY"}
# The client's IPv4 address as a number, or -1. Mule gives remoteAddress as '/a.b.c.d:port'.
CLIENT_NUMBER = (
    "var address = (attributes.remoteAddress default '') replace /^\\/|:\\d+$/ with '' "
    "var parts = address splitBy '.' "
    "var ip = if (sizeOf(parts) == 4 and sizeOf(parts filter ((part) -> part matches /\\d{1,3}/)) == 4) "
    "(parts reduce ((part, total = 0) -> total * 256 + (part as Number))) else -1"
)


def translate(policy: Policy, *, direction: str) -> TemplateOutput:
    draft = Draft(policy, direction, handled=HANDLED)
    if direction != REQUEST:
        return draft.skip(
            f"AccessControl {policy.name} is in a response flow, where the generated app no longer has the "
            "client address"
        )
    validate = text(child(draft.settings, "ValidateBasedOn"))
    if validate is not None:
        draft.option(
            "ValidateBasedOn",
            f"ValidateBasedOn {validate} is not carried over; the client address the Mule listener sees is checked",
        )
    rules_element = child(draft.settings, "IPRules")
    if rules_element is None:
        return draft.skip(f"AccessControl {policy.name} has no <IPRules>, so there is nothing to check")
    default = ACTIONS.get(fold(rules_element.attributes.get("noRuleMatchAction", "ALLOW").strip()), "")
    if not default:
        return draft.skip(
            f"AccessControl {policy.name} has noRuleMatchAction "
            f"'{rules_element.attributes.get('noRuleMatchAction')}'; only ALLOW and DENY are translated"
        )

    rules: list[str] = []
    for rule in children(rules_element, "MatchRule"):
        action = ACTIONS.get(fold(rule.attributes.get("action", "").strip()), "")
        if not action:
            return draft.skip(
                f"AccessControl {policy.name} has a MatchRule with action '{rule.attributes.get('action', '')}'"
            )
        for source in children(rule, "SourceAddress"):
            address = (source.text or "").strip()
            mask = source.attributes.get("mask", "32").strip()
            if source.attributes.get("ref") or not address:
                return draft.skip(f"AccessControl {policy.name} takes an address from a variable; not translated")
            network = _network(address, mask)
            if network is None:
                return draft.skip(
                    f"AccessControl {policy.name} has the rule {action} {address} mask {mask}, which is not an "
                    "IPv4 address with a mask from 0 to 32; dropping it would change who gets through"
                )
            first, last = int(network.network_address), int(network.broadcast_address)
            rules.append(
                f"{{action: {dw_string(action)}, rule: {dw_string(f'{address}/{mask}')}, first: {first}, last: {last}}}"
            )
    decision = (
        f"(do {{ {CLIENT_NUMBER} var rules = [{', '.join(rules)}] --- "
        f"((rules filter ((rule) -> ip >= rule.first and ip <= rule.last))[0].action) default {dw_string(default)} }})"
    )
    denied = draft.fault(403, "Access Denied for client ip", "accesscontrol.IPDeniedAccess")
    draft.add(choice((f"#[{decision} == 'DENY']", denied)))
    return draft.done()


def _network(address: str, mask: str) -> ipaddress.IPv4Network | None:
    if not mask.isdigit() or not 0 <= int(mask) <= 32:
        return None
    try:
        return ipaddress.IPv4Network(f"{address}/{mask}", strict=False)
    except ValueError:
        return None
