"""ExtractVariables: copy parts of the message into flow variables.

Translated sources: the URI path below the base path (URIPath), query
parameters, headers and simple JSONPath expressions on a JSON body. Each
variable is ``<VariablePrefix>.<name>`` (or just ``name``), as in Apigee. A
pattern such as ``Bearer {token}`` becomes a regular expression whose groups
fill the variables; a value that does not match leaves the variable null.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from a2m.ir import Policy, XmlElement
from a2m.policies.common import (
    REQUEST,
    RESPONSE,
    RESPONSE_HEADERS_BASE,
    Draft,
    TemplateOutput,
    child,
    children,
    dw_string,
    fold,
    is_true,
    request_header,
    request_query,
    set_variable,
    text,
)

HANDLED = {"Source", "VariablePrefix", "IgnoreUnresolvedVariables", "URIPath", "QueryParam", "Header", "JSONPayload"}
PATTERN_VAR = re.compile(r"\{([^{}]+)\}")
JSON_STEP = re.compile(r"""\.([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]|\[\s*'([^'\\]*)'\s*\]|\[\s*"([^"\\]*)"\s*\]""")
# Characters a Java regular expression treats specially; each is escaped with a backslash.
REGEX_SPECIAL = set("\\^$.|?*+()[]{}/")


def translate(policy: Policy, *, direction: str) -> TemplateOutput:
    draft = Draft(policy, direction, handled=HANDLED)
    settings = draft.settings
    source = child(settings, "Source")
    source_name = fold(text(source) or "message")
    if source is not None and is_true(source.attributes.get("clearPayload")):
        draft.option("clearPayload", "clearPayload=true is not carried over; the payload is kept")
    if source_name == "message":
        source_name = direction
    if source_name not in (REQUEST, RESPONSE):
        return draft.skip(f"ExtractVariables {policy.name} reads from the message variable {source_name}")
    if source_name != direction:
        return draft.skip(
            f"ExtractVariables {policy.name} reads the {source_name} in a {direction} flow; "
            f"the generated app has only the {direction} at that point"
        )
    prefix = text(child(settings, "VariablePrefix"))

    def variable(name: str) -> str:
        return f"{prefix}.{name}" if prefix else name

    for uri_path in children(settings, "URIPath"):
        if source_name != REQUEST:
            draft.option("URIPath", "a response has no URI path; URIPath is not carried over")
            continue
        _patterns(draft, uri_path, "URIPath", "attributes.maskedRequestPath", variable, segment=True)
    for query in children(settings, "QueryParam"):
        name = query.attributes.get("name", "").strip()
        if source_name != REQUEST or not name:
            draft.option(f"QueryParam {name}".strip(), "only a named query parameter of the request is carried over")
            continue
        _patterns(draft, query, f"QueryParam {name}", request_query(name), variable)
    for header in children(settings, "Header"):
        name = header.attributes.get("name", "").strip()
        if not name:
            draft.option("Header", "a <Header> without a name is not carried over")
            continue
        if source_name == REQUEST:
            reader = request_header(name)
        else:
            reader = f"{RESPONSE_HEADERS_BASE}[{dw_string(fold(name))}]"
        _patterns(draft, header, f"Header {name}", reader, variable)
    for json_payload in children(settings, "JSONPayload"):
        for item in children(json_payload, "Variable"):
            _json_variable(draft, item, variable)
        for extra in json_payload.children:
            if extra.tag != "Variable":
                draft.option(f"JSONPayload {extra.tag}", f"<{extra.tag}> in JSONPayload is not carried over")
    return draft.done()


def _patterns(
    draft: Draft,
    element: XmlElement,
    where: str,
    value: str,
    variable: Callable[[str], str],
    *,
    segment: bool = False,
) -> None:
    patterns = children(element, "Pattern")
    if not patterns:
        draft.option(where, f"{where} has no <Pattern>")
        return
    if len(patterns) > 1:
        draft.option(f"{where} Pattern", f"only the first <Pattern> of {where} is carried over")
    pattern = patterns[0]
    raw = pattern.text or ""
    names = PATTERN_VAR.findall(raw)
    if not names:
        draft.option(f"{where} Pattern", f"the pattern '{raw}' of {where} names no variable")
        return
    regex = _regex(raw, segment=segment)
    flags = "(?i)" if is_true(pattern.attributes.get("ignoreCase")) else ""
    for index, name in enumerate(names, start=1):
        if raw.strip() == "{" + name + "}" and not segment:
            expression = value
        else:
            expression = f"(({value} default '') match /{flags}^{regex}$/)[{index}]"
        draft.add(set_variable(variable(name.strip()), f"#[{expression}]"))


def _regex(pattern: str, *, segment: bool) -> str:
    """``pattern`` as a regular expression: {name} becomes a group, '*' one path segment, '**' any path."""
    out: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        end = pattern.find("}", index) if char == "{" else -1
        if end != -1:
            out.append("([^\\/]*)" if segment else "(.*)")
            index = end + 1
            continue
        if segment and pattern.startswith("**", index):
            out.append(".*")
            index += 2
            continue
        if segment and char == "*":
            out.append("[^\\/]*")
        elif char in REGEX_SPECIAL:
            out.append("\\" + char)
        else:
            out.append(char)
        index += 1
    return "".join(out)


def _json_variable(draft: Draft, item: XmlElement, variable: Callable[[str], str]) -> None:
    name = item.attributes.get("name", "").strip()
    path = text(child(item, "JSONPath")) or ""
    selector = _json_selector(path)
    if not name or selector is None:
        draft.option(
            f"JSONPayload {name}".strip(),
            f"the JSONPath '{path}' is not translated; only $.name, ['name'] and [index] steps are",
        )
        return
    expression = f"if ((payload is Object) or (payload is Array)) payload{selector} else null"
    draft.add(set_variable(variable(name), f"#[{expression}]"))


def _json_selector(path: str) -> str | None:
    """The DataWeave selectors for a simple JSONPath ('$.order.id' gives '.order.id'), or None."""
    if not path.startswith("$"):
        return None
    rest, position, out = path[1:], 0, []
    while position < len(rest):
        match = JSON_STEP.match(rest, position)
        if match is None:
            return None
        name, number, single, double = match.groups()
        if name is not None:
            out.append(f"[{dw_string(name)}]")
        elif number is not None:
            out.append(f"[{number}]")
        else:
            out.append(f"[{dw_string(single if single is not None else double or '')}]")
        position = match.end()
    return "".join(out)
