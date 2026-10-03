"""ExtractVariables: copy parts of the message into flow variables.

Translated sources: the URI path below the base path (URIPath), query
parameters, headers and simple JSONPath expressions on a JSON body (as in
Apigee's ExtractVariables reference, only when the message's Content-Type is
application/json: parameters such as charset are ignored and the media type is
compared without case, but a +json type such as application/problem+json is not
JSON there; any other body finds nothing). Each
variable is ``<VariablePrefix>.<name>`` (or just ``name``), as in Apigee. A
pattern such as ``Bearer {token}`` becomes a regular expression whose groups
fill the variables.

As in Apigee, an extraction that finds nothing (the header, query parameter or
JSON value is missing, or the pattern does not match) leaves the variable as it
was: every generated value falls back to the variable's earlier value
(``... default vars['name']``, null when it was never set). That is Apigee's
behaviour with IgnoreUnresolvedVariables=true. With the setting false or absent
(Apigee's default) Apigee may fail the call instead, and its exact behaviour is
not certain, so a2m keeps the earlier value and lists the setting as an
unsupported option for review.

Every extracted value is a string, as Apigee's flow variables are: a JSON
number or boolean becomes its text (``{"id": 42}`` gives ``"42"``), a JSON
object or array its JSON text on one line (DataWeave writes ``{"a": 1}``; the
spacing may differ from Apigee's), and JSON null or a missing value leaves
the variable as it was. So a later condition such as ``ext.orderId = "42"`` (a
string comparison in DataWeave) matches as it does in Apigee.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable

from a2m.conditions import NO_CHANGES, RequestChanges
from a2m.conditions.variables import EXACT_PATH_SUFFIX_DW, first_value, stale_part
from a2m.ir import Policy, XmlElement
from a2m.policies.common import (
    REQUEST,
    REQUEST_HEADERS_BASE,
    RESPONSE,
    UNRESOLVED_VARIABLES,
    Draft,
    TemplateOutput,
    child,
    children,
    dw_string,
    flag,
    fold,
    is_true,
    set_variable,
    text,
)

HANDLED = {"Source", "VariablePrefix", "IgnoreUnresolvedVariables", "URIPath", "QueryParam", "Header", "JSONPayload"}
PATTERN_VAR = re.compile(r"\{([^{}]+)\}")
JSON_STEP = re.compile(r"""\.([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]|\[\s*'([^'\\]*)'\s*\]|\[\s*"([^"\\]*)"\s*\]""")
# The largest array index a JSONPath step can name (Java's int).
JAVA_INT_MAX = 2**31 - 1
# Characters a Java regular expression treats specially; each is escaped with a backslash.
REGEX_SPECIAL = set("\\^$.|?*+()[]{}/")


def variable_writes(policy: Policy) -> frozenset[str]:
    """The flow variables this policy writes in Apigee, in lower case, whatever their source: every {name} of
    every <Pattern> and every <Variable name> of JSONPayload and XMLPayload, under the VariablePrefix."""
    return frozenset(_write_counts(policy))


def _write_counts(policy: Policy) -> Counter[str]:
    """How many extractions of ``policy`` write each variable of :func:`variable_writes` in Apigee."""
    prefix = text(child(policy.settings, "VariablePrefix"))
    names: Counter[str] = Counter()

    def walk(element: XmlElement) -> None:
        for item in element.children:
            if item.tag == "Pattern":
                names.update(name.strip() for name in PATTERN_VAR.findall(item.text or ""))
            elif item.tag == "Variable" and element.tag in ("JSONPayload", "XMLPayload"):
                names.update([item.attributes.get("name", "").strip()])
            walk(item)

    walk(policy.settings)
    return Counter(fold(f"{prefix}.{name}" if prefix else name) for name in names.elements() if name)


def translate(policy: Policy, *, direction: str, changes: RequestChanges = NO_CHANGES) -> TemplateOutput:
    draft = Draft(policy, direction, handled=HANDLED, changes=changes)
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
        # Apigee's proxy.pathsuffix, as the generator forwards it: '' for the bare base path, so /{id} finds nothing.
        _patterns(draft, uri_path, "URIPath", f"({EXACT_PATH_SUFFIX_DW})", variable, segment=True)
    for query in children(settings, "QueryParam"):
        name = query.attributes.get("name", "").strip()
        if source_name != REQUEST or not name:
            draft.option(f"QueryParam {name}".strip(), "only a named query parameter of the request is carried over")
            continue
        read = draft.read(f"request.queryparam.{name}")
        if read.dw is None:
            draft.option(f"QueryParam {name}", f"query parameter {name} can't be read here: {read.reason}")
            continue
        _patterns(draft, query, f"QueryParam {name}", read.dw, variable)
    for header in children(settings, "Header"):
        name = header.attributes.get("name", "").strip()
        if not name:
            draft.option("Header", "a <Header> without a name is not carried over")
            continue
        if source_name == REQUEST:
            read = draft.read(f"request.header.{name}")
            if read.dw is None:
                draft.option(f"Header {name}", f"request header {name} can't be read here: {read.reason}")
                continue
            reader = read.dw
        else:
            read = draft.read(f"response.header.{name}")
            if read.dw is None:
                draft.option(f"Header {name}", f"response header {name} can't be read here: {read.reason}")
                continue
            reader = read.dw
        _patterns(draft, header, f"Header {name}", reader, variable)
    for json_payload in children(settings, "JSONPayload"):
        stale = stale_part(f"{source_name}.content", f"{source_name} payload", draft.changes)
        if stale is not None:
            draft.option("JSONPayload", f"the {source_name} payload can't be read here: {stale}")
            continue
        # Apigee extracts JSONPayload only from a message whose Content-Type is JSON, read as the step sees it.
        content_type, reason = _content_type(draft, source_name)
        if content_type is None:
            draft.option(
                "JSONPayload",
                f"the {source_name} Content-Type, which decides whether Apigee reads the payload as JSON, can't be "
                f"read here: {reason}",
            )
            continue
        for item in children(json_payload, "Variable"):
            _json_variable(draft, item, variable, _is_json(content_type))
        for extra in json_payload.children:
            if extra.tag != "Variable":
                draft.option(f"JSONPayload {extra.tag}", f"<{extra.tag}> in JSONPayload is not carried over")
    # A variable more than one extraction writes is not written exactly: Apigee sets it only from an extraction that
    # finds a value, so its last value may come from any of them, including one a2m does not carry over.
    draft.dropped(*(name for name, count in _write_counts(policy).items() if count > 1))
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
            found = value
        else:
            # A missing source matches nothing, even a pattern that matches the empty string.
            found = f"if ({value} == null) null else ({value} match /{flags}^{regex}$/)[{index}]"
        draft.add(set_variable(variable(name.strip()), f"#[{_or_earlier(found, variable(name.strip()))}]"))
        _unresolved(draft)
        if len(patterns) == 1:
            # With more patterns Apigee may fill the variable from a later one, which is not carried over.
            draft.wrote(variable(name.strip()))


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


def _content_type(draft: Draft, source_name: str) -> tuple[str | None, str]:
    """DataWeave reading the Content-Type of the message ``source_name`` as the step sees it, or None with the reason
    when the JSON read can't be exact: an earlier step may change the Content-Type or the payload where the generated
    app does not keep the two agreeing as Apigee does (``request.content-type``, ``response.content-type``). On the
    request side it reads a2m's request headers with every change a step carried over; on the response side
    ``response.header.Content-Type`` through the shared accessor."""
    steps = draft.changes.variable_steps(f"{source_name}.content-type")
    if steps:
        return None, (
            f"the earlier step {', '.join(steps)} on the same path may change the {source_name} Content-Type or "
            "payload in a way the generated app does not reproduce for a JSON read (the step or that setting can't "
            "be translated or never runs; a Content-Type header set alone leaves Mule's payload with the media type "
            "it was read with; a payload set without a contentType has none), so whether the payload is read as JSON "
            "could differ from Apigee"
        )
    if source_name == RESPONSE:
        read = draft.read("response.header.content-type")
        return read.dw, read.reason or ""
    return first_value(f"{REQUEST_HEADERS_BASE}['content-type']"), ""


def _is_json(content_type: str) -> str:
    """DataWeave true when the Content-Type read by ``content_type`` names JSON as Apigee's JSONPayload needs it:
    the media type, parameters dropped and compared without case, is exactly application/json (Apigee's reference:
    JSON extraction is performed only when the Content-Type is application/json). A +json type and a missing
    Content-Type are not JSON."""
    media = f"lower(trim(((({content_type}) default '') splitBy ';')[0] default ''))"
    return f"({media} == 'application/json')"


def _json_variable(draft: Draft, item: XmlElement, variable: Callable[[str], str], is_json: str) -> None:
    name = item.attributes.get("name", "").strip()
    path = text(child(item, "JSONPath")) or ""
    found = _json_lookup(path, is_json)
    if not name or found is None:
        draft.option(
            f"JSONPayload {name}".strip(),
            f"the JSONPath '{path}' is not translated; only $.name, ['name'] and [index] steps are",
        )
        return
    expression = (
        f"do {{ var found = {found} --- if (found == null) null else if (found is String) found "
        "else if ((found is Object) or (found is Array)) write(found, 'application/json', {indent: false}) "
        "else (found as String) }"
    )
    draft.add(set_variable(variable(name), f"#[{_or_earlier(expression, variable(name))}]"))
    _unresolved(draft)
    draft.wrote(variable(name))


def _unresolved(draft: Draft) -> None:
    """List IgnoreUnresolvedVariables once when it is not true and the step generates an extraction (every one can
    find nothing): Apigee's default (false) may then fail the call, which is not certain, so a2m does not guess the
    fault and keeps the earlier value as with true."""
    if flag(child(draft.settings, UNRESOLVED_VARIABLES)):
        return
    if any(option.name == UNRESOLVED_VARIABLES for option in draft.options):
        return
    draft.option(
        UNRESOLVED_VARIABLES,
        f"{UNRESOLVED_VARIABLES}=false: Apigee may fail the call when nothing is extracted; a2m keeps the earlier value",
    )


def _or_earlier(found: str, name: str) -> str:
    """``found`` (an extracted value, null when the extraction finds nothing) or else flow variable ``name``'s earlier
    value, read as the condition translator reads it: Apigee sets a variable only from an extraction that finds a
    value. An earlier value the generated app may not hold is caught by the step tracking (a later read of ``name``
    stays refused)."""
    return f"({found}) default vars[{dw_string(name)}]"


def _json_lookup(path: str, is_json: str) -> str | None:
    """DataWeave finding the value of a simple JSONPath in the JSON body, null where Apigee finds nothing; None when
    the path is not translated. ``is_json`` is DataWeave true when the message's Content-Type is JSON: any other body
    finds nothing, as in Apigee, even one DataWeave parses into an object (a form, XML or CSV body).

    Only steps whose DataWeave equivalent is certain are translated: a property step (``.name``, ``['name']``) is
    applied only to an object and an index step (``[0]``) only to an array; a step applied to anything else finds
    nothing, as in Apigee (``$.id`` on a top-level array, ``$.items.id`` where items is an array, ``$[0]`` on an
    object). A DataWeave selector is never applied to an array by name, where it would collect the values of every
    element. Wildcards, filters, recursive descent, unions and slices are not translated."""
    if not path.startswith("$"):
        return None
    root = f"if (({is_json}) and ((payload is Object) or (payload is Array))) payload else null"
    rest, position, steps = path[1:], 0, []
    while position < len(rest):
        match = JSON_STEP.match(rest, position)
        if match is None:
            return None
        name, number, single, double = match.groups()
        if number is not None:
            if int(number) > JAVA_INT_MAX:
                return None
            steps.append(str(int(number)))
        else:
            steps.append(dw_string(name if name is not None else single if single is not None else double or ""))
        position = match.end()
    if not steps:
        return root
    # A number is an index step, a string a property step (['0'] is the property named 0, as in JSONPath).
    return (
        f"([{', '.join(steps)}]) reduce ((step, node = ({root})) -> if (node == null) null "
        "else if (step is Number) (if (node is Array) node[step] else null) "
        "else (if (node is Object) node[step] else null))"
    )
