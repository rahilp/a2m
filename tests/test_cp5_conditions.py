"""CP5: Apigee conditions and {variable} references become DataWeave expressions.

Public entry points used here (CP5 plan; nothing else from a2m is imported):

    a2m.conditions.translate_condition(text: str) -> result
    a2m.conditions.translate_template(text: str, prefix: str = "{", suffix: str = "}") -> result
        result.ok        bool
        result.dw        str | None   the DataWeave expression, without the #[ ] wrapper
        result.original  str          the input text, unchanged
        result.reason    str | None   non-empty when ok is False
        An empty or whitespace-only condition, and a template with no variables,
        give ok True and dw None.

    a2m.parser.read_bundle(path) -> Bundle                                    (CP2)
    a2m.generator.generate_project(bundle, dest, *, shared_flows=()) -> result  (CP3/CP4)
        result.policies    one PolicyResult per step, as in CP4
        result.pending     as in CP3/CP4; CP5 leaves no condition pending
        result.conditions  NEW in CP5: one record per non-empty condition in the
                           bundle (steps, conditional flows, fault rules, route rules):
            .name      str   the step, flow, fault rule or route rule name
            .original  str   the condition text as read from the bundle
            .ok        bool  translated (True) or can't translate (False)
            .dw        str | None   the translated expression (no #[ ] wrapper)
            .reason    str | None   why it can't be translated (names the variable)

Output vocabulary (plan): request.verb -> attributes.method, proxy.pathsuffix ->
attributes.maskedRequestPath, request.header.NAME -> the first comma-separated
value of attributes.headers['name'], trimmed, null when missing (hdr() below;
lowercased name), request.queryparam.NAME -> attributes.queryParams['NAME'], other
names -> vars['NAME']; every comparison and every and/or/not node in its own
parentheses; regex operators -> ((X default "") matches /re/); templates ->
"literal" ++ (X default ""). DataWeave cannot run here, so the DataWeave text is
the observable contract, compared after collapsing runs of whitespace.

Orchestrator decisions (checkpoint notes): AND and OR mixed without brackets
is can't translate; a bare unquoted word on the right is a string literal, a
bare number is numeric; '?' inside a Matches pattern is can't translate.

The fixture bundles conditions-api and templates-api are written under
tmp_path by this module. No network, no Java, Maven or Mule, no API keys.
"""

from __future__ import annotations

import re
import shutil
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "apigee"
CP4_ORDERS_API = FIXTURES / "cp4" / "orders-api"
CP4_ORDERS_SIMPLE = FIXTURES / "cp4" / "orders-simple"
TEST_API = FIXTURES / "azure" / "Test-API"
GET_SHARED_FLOW = FIXTURES / "azure" / "GetSharedFlow"
GOLDEN_CP3 = REPO / "tests" / "golden" / "cp3"
GOLDEN_CP4 = REPO / "tests" / "golden" / "cp4"
UPDATE_GOLDEN_ENV = "A2M_UPDATE_GOLDEN"

CORE = "http://www.mulesoft.org/schema/mule/core"
HTTP = "http://www.mulesoft.org/schema/mule/http"
DOC = "http://www.mulesoft.org/schema/mule/documentation"
DOC_NAME = f"{{{DOC}}}name"
XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'

CANT = re.compile(r"can['\u2019]?t translate|cannot translate", re.IGNORECASE)
PATH = 'attributes.maskedRequestPath default ""'
# ((LHS default "") matches /REGEX/) with the regex captured; \/ inside the literal is an escaped slash.
MATCH_DW = re.compile(r'^\(\((?P<lhs>.+?) default ""\) matches /(?P<re>(?:\\.|[^/\\])*)/\)$')



def hdr(name: str, base: str = "attributes.headers") -> str:
    """The one DataWeave read of request.header.NAME (orchestrator decision, Rahil 2026-10-03, option A).

    Apigee compares a header against its FIRST comma-separated value, trimmed. A missing header stays
    null, so `= null` keeps its meaning and missing stays distinct from "". Checked on Mule's DataWeave
    2.9 engine: "application/json, text/plain" -> "application/json", "" -> "", missing -> null.
    """
    read = f"{base}['{name}']"
    return f'(if ({read} == null) null else trim(({read} splitBy ",")[0] default ""))'


GET = 'request.verb = "GET"'
ENV = 'request.header.X-Env = "prod"'
DEBUG = 'request.queryparam.debug = "true"'
GET_DW = '(attributes.method == "GET")'
ENV_DW = f"({hdr('x-env')} == \"prod\")"
DEBUG_DW = "(attributes.queryParams['debug'] == \"true\")"


# ---------------------------------------------------------------- translator entry points


def translate_condition(text: str) -> Any:
    from a2m.conditions import translate_condition as _translate

    return _translate(text)


def translate_template(text: str, prefix: str = "{", suffix: str = "}") -> Any:
    from a2m.conditions import translate_template as _translate

    return _translate(text, prefix=prefix, suffix=suffix)


def collapse(text: str) -> str:
    return " ".join(text.split())


def assert_translates(text: str, expected: str) -> Any:
    result = translate_condition(text)
    assert result.ok is True, (text, result.reason)
    assert result.dw is not None, text
    assert collapse(result.dw) == expected, (text, result.dw)
    assert result.original == text
    return result


def assert_untranslatable(text: str, *names: str) -> Any:
    result = translate_condition(text)
    assert result.ok is False, (text, result.dw)
    assert result.dw is None, (text, result.dw)
    assert result.original == text
    assert isinstance(result.reason, str) and result.reason.strip(), (text, result.reason)
    for name in names:
        assert name in result.reason, (text, name, result.reason)
    return result


def regex_of(dw: str, lhs: str = "attributes.maskedRequestPath") -> str:
    """The regex inside ((LHS default "") matches /re/), with escaped slashes turned back into slashes."""
    match = MATCH_DW.match(collapse(dw))
    assert match is not None, f"not a regex match expression: {dw}"
    assert match.group("lhs") == lhs, dw
    return match.group("re").replace("\\/", "/")


def full(pattern: str, sample: str) -> bool:
    return re.fullmatch(pattern, sample) is not None


# ---------------------------------------------------------------- fixture bundles


def policy(root_tag: str, name: str, body: str) -> str:
    return (
        XML_HEAD + f'<{root_tag} async="false" continueOnError="false" enabled="true" name="{name}">\n'
        f"    <DisplayName>{name}</DisplayName>\n{body}</{root_tag}>\n"
    )


def assign_headers(name: str, headers: list[tuple[str, str]]) -> str:
    rows = "".join(f'            <Header name="{h}">{v}</Header>\n' for h, v in headers)
    return policy(
        "AssignMessage",
        name,
        f"    <Set>\n        <Headers>\n{rows}        </Headers>\n    </Set>\n"
        "    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n"
        '    <AssignTo createNew="false" transport="http" type="request"/>\n',
    )


def raise_fault(name: str, status: int, reason: str) -> str:
    return policy(
        "RaiseFault",
        name,
        "    <FaultResponse>\n        <Set>\n"
        f'            <Payload contentType="application/json">{{"error":"{reason}"}}</Payload>\n'
        f"            <StatusCode>{status}</StatusCode>\n            <ReasonPhrase>{reason}</ReasonPhrase>\n"
        "        </Set>\n    </FaultResponse>\n    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n",
    )


def manifest(name: str, policies: list[str]) -> str:
    items = "".join(f"        <Policy>{p}</Policy>\n" for p in policies)
    return (
        XML_HEAD + f'<APIProxy revision="1" name="{name}">\n'
        f"    <Description>a2m CP5 fixture</Description>\n    <DisplayName>{name}</DisplayName>\n"
        f"    <Policies>\n{items}    </Policies>\n"
        "    <ProxyEndpoints>\n        <ProxyEndpoint>default</ProxyEndpoint>\n    </ProxyEndpoints>\n"
        "    <TargetEndpoints>\n        <TargetEndpoint>default</TargetEndpoint>\n    </TargetEndpoints>\n"
        "</APIProxy>\n"
    )


TARGET_XML = (
    XML_HEAD + '<TargetEndpoint name="default">\n'
    '    <PreFlow name="PreFlow">\n        <Request/>\n        <Response/>\n    </PreFlow>\n'
    "    <Flows/>\n"
    '    <PostFlow name="PostFlow">\n        <Request/>\n        <Response/>\n    </PostFlow>\n'
    "    <HTTPTargetConnection>\n        <URL>https://backend.example.test/shop</URL>\n"
    "    </HTTPTargetConnection>\n</TargetEndpoint>\n"
)


def step(name: str, condition: str | None = None, *, empty_condition: bool = False) -> str:
    cond = ""
    if empty_condition:
        cond = "<Condition/>"
    elif condition is not None:
        cond = f"<Condition>{condition}</Condition>"
    return f"<Step>{cond}<Name>{name}</Name></Step>"


# Condition texts exactly as written in the conditions-api XML files (XML-escaped where noted).
COND_SA = 'request.queryparam.tier != "gold"'
COND_CLIENT = 'client.ip = "10.0.0.1"'
COND_ORDER = 'ext.orderId = "42"'
COND_GET_ORDERS = '(proxy.pathsuffix MatchesPath "/orders/*") and (request.verb = "GET")'
COND_POST_ORDERS_XML = '(proxy.pathsuffix Matches "/orders*") &amp;&amp; (request.verb = "POST")'
COND_POST_ORDERS = '(proxy.pathsuffix Matches "/orders*") && (request.verb = "POST")'
COND_AUTH = 'request.header.X-API-Key = "abc"'
COND_TAG_XML = 'request.header.X-Tag = "a&lt;b"'
COND_TAG = 'request.header.X-Tag = "a<b"'
COND_SPIKE = '(fault.name = "SpikeArrestViolation")'
COND_V2 = 'request.header.X-Version = "2"'

# Hand count of non-empty conditions in conditions-api (the empty <Condition/> on AM-Empty and the
# absent conditions of Extract-Order-Id, catch-all, RF-NotFound, RF-TooMany and RouteRule default do not count):
#   SA-Limit, AM-Client, get-orders, post-orders, AM-Auth, AM-Tag, AM-Order, FaultRule spike, RouteRule v2
EXPECTED_CONDITIONS = {
    "SA-Limit": COND_SA,
    "AM-Client": COND_CLIENT,
    "get-orders": COND_GET_ORDERS,
    "post-orders": COND_POST_ORDERS,
    "AM-Auth": COND_AUTH,
    "AM-Tag": COND_TAG,
    "AM-Order": COND_ORDER,
    "spike": COND_SPIKE,
    "v2": COND_V2,
}
CONDITION_COUNT = 9

CONDITIONS_PROXY_XML = (
    XML_HEAD + '<ProxyEndpoint name="default">\n'
    "    <FaultRules>\n"
    '        <FaultRule name="spike">\n'
    f"            {step('RF-TooMany')}\n"
    f"            <Condition>{COND_SPIKE}</Condition>\n"
    "        </FaultRule>\n"
    "    </FaultRules>\n"
    '    <PreFlow name="PreFlow">\n'
    "        <Request>\n"
    f"            {step('SA-Limit', COND_SA)}\n"
    f"            {step('Extract-Order-Id')}\n"
    f"            {step('AM-Order', COND_ORDER)}\n"
    f"            {step('AM-Empty', empty_condition=True)}\n"
    f"            {step('AM-Client', COND_CLIENT)}\n"
    "        </Request>\n"
    "        <Response/>\n"
    "    </PreFlow>\n"
    "    <Flows>\n"
    '        <Flow name="get-orders">\n'
    f"            <Request>{step('AM-Auth', COND_AUTH)}</Request>\n"
    "            <Response/>\n"
    f"            <Condition>{COND_GET_ORDERS}</Condition>\n"
    "        </Flow>\n"
    '        <Flow name="post-orders">\n'
    f"            <Request>{step('AM-Tag', COND_TAG_XML)}</Request>\n"
    "            <Response/>\n"
    f"            <Condition>{COND_POST_ORDERS_XML}</Condition>\n"
    "        </Flow>\n"
    '        <Flow name="catch-all">\n'
    f"            <Request>{step('RF-NotFound')}</Request>\n"
    "            <Response/>\n"
    "        </Flow>\n"
    "    </Flows>\n"
    '    <PostFlow name="PostFlow">\n        <Request/>\n        <Response/>\n    </PostFlow>\n'
    "    <HTTPProxyConnection>\n        <BasePath>/shop</BasePath>\n        <VirtualHost>default</VirtualHost>\n"
    "    </HTTPProxyConnection>\n"
    '    <RouteRule name="v2">\n'
    f"        <Condition>{COND_V2}</Condition>\n"
    "        <TargetEndpoint>default</TargetEndpoint>\n"
    "    </RouteRule>\n"
    '    <RouteRule name="default">\n        <TargetEndpoint>default</TargetEndpoint>\n    </RouteRule>\n'
    "</ProxyEndpoint>\n"
)

CONDITIONS_POLICIES = {
    "SA-Limit": policy("SpikeArrest", "SA-Limit", "    <Rate>10ps</Rate>\n"),
    "Extract-Order-Id": policy(
        "ExtractVariables",
        "Extract-Order-Id",
        "    <Source>request</Source>\n    <VariablePrefix>ext</VariablePrefix>\n"
        '    <JSONPayload>\n        <Variable name="orderId">\n            <JSONPath>$.order.id</JSONPath>\n'
        "        </Variable>\n    </JSONPayload>\n    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n",
    ),
    "AM-Empty": assign_headers("AM-Empty", [("X-Seen", "1")]),
    "AM-Client": assign_headers("AM-Client", [("X-Client", "1")]),
    "AM-Auth": policy(
        "AssignMessage",
        "AM-Auth",
        '    <Set>\n        <Headers>\n            <Header name="Authorization">Bearer {request.header.X-Token}</Header>\n'
        "        </Headers>\n    </Set>\n"
        '    <Add>\n        <QueryParams>\n            <QueryParam name="trace">{ext.orderId}</QueryParam>\n'
        "        </QueryParams>\n    </Add>\n"
        "    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n"
        '    <AssignTo createNew="false" transport="http" type="request"/>\n',
    ),
    "AM-Tag": assign_headers("AM-Tag", [("X-Tag", "v1")]),
    "AM-Order": assign_headers("AM-Order", [("X-Order", "1")]),
    "RF-NotFound": raise_fault("RF-NotFound", 404, "Not Found"),
    "RF-TooMany": raise_fault("RF-TooMany", 429, "Too Many Requests"),
}

TEMPLATES_PROXY_XML = (
    XML_HEAD + '<ProxyEndpoint name="default">\n'
    '    <PreFlow name="PreFlow">\n'
    "        <Request>\n"
    f"            {step('AM-Json')}\n"
    f"            {step('AM-Prefixed')}\n"
    f"            {step('AM-Unmapped')}\n"
    f"            {step('AM-Literal')}\n"
    f"            {step('RF-Echo')}\n"
    "        </Request>\n"
    "        <Response/>\n"
    "    </PreFlow>\n"
    "    <Flows/>\n"
    '    <PostFlow name="PostFlow">\n        <Request/>\n        <Response/>\n    </PostFlow>\n'
    "    <HTTPProxyConnection>\n        <BasePath>/shop</BasePath>\n        <VirtualHost>default</VirtualHost>\n"
    "    </HTTPProxyConnection>\n"
    '    <RouteRule name="default">\n        <TargetEndpoint>default</TargetEndpoint>\n    </RouteRule>\n'
    "</ProxyEndpoint>\n"
)

JSON_PAYLOAD = '{"id":"{request.queryparam.id}","verb":"{request.verb}"}'
PREFIXED_PAYLOAD = '{"id":"@request.queryparam.id#"}'
X_TIME = "at {system.timestamp}"
X_UP = "{toUpperCase(request.verb)}"
LITERALS = {"X-Plain": "no variables here", "X-Brace": "price {", "X-Empty": "empty {} braces"}


def _payload_policy(name: str, payload_attrs: str, payload: str) -> str:
    return policy(
        "AssignMessage",
        name,
        f"    <Set>\n        <Payload {payload_attrs}>{payload}</Payload>\n    </Set>\n"
        "    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n"
        '    <AssignTo createNew="false" transport="http" type="request"/>\n',
    )


TEMPLATES_POLICIES = {
    "AM-Json": _payload_policy("AM-Json", 'contentType="application/json"', JSON_PAYLOAD),
    "AM-Prefixed": _payload_policy(
        "AM-Prefixed", 'contentType="application/json" variablePrefix="@" variableSuffix="#"', PREFIXED_PAYLOAD
    ),
    "AM-Unmapped": assign_headers("AM-Unmapped", [("X-Time", X_TIME), ("X-Up", X_UP)]),
    "AM-Literal": assign_headers("AM-Literal", list(LITERALS.items())),
    "RF-Echo": policy(
        "RaiseFault",
        "RF-Echo",
        "    <FaultResponse>\n        <Set>\n"
        '            <Headers>\n                <Header name="X-Verb">{request.verb}</Header>\n            </Headers>\n'
        '            <Payload contentType="text/plain">missing {request.queryparam.id}</Payload>\n'
        "            <StatusCode>400</StatusCode>\n            <ReasonPhrase>Bad Request</ReasonPhrase>\n"
        "        </Set>\n    </FaultResponse>\n    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n",
    ),
}


def write_bundle(parent: Path, name: str, proxy_xml: str, policies: dict[str, str]) -> Path:
    root = parent / name
    files = {
        f"apiproxy/{name}.xml": manifest(name, list(policies)),
        "apiproxy/proxies/default.xml": proxy_xml,
        "apiproxy/targets/default.xml": TARGET_XML,
        **{f"apiproxy/policies/{p}.xml": xml for p, xml in policies.items()},
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def conditions_api(parent: Path) -> Path:
    return write_bundle(parent, "conditions-api", CONDITIONS_PROXY_XML, CONDITIONS_POLICIES)


def templates_api(parent: Path) -> Path:
    return write_bundle(parent, "templates-api", TEMPLATES_PROXY_XML, TEMPLATES_POLICIES)


# ---------------------------------------------------------------- generating and reading projects


def generate(bundle_dir: Path, dest: Path, shared: tuple[Path, ...] = ()) -> Any:
    from a2m.generator import generate_project
    from a2m.parser import read_bundle

    return generate_project(read_bundle(bundle_dir), dest, shared_flows=tuple(read_bundle(s) for s in shared))


def parse_strict(path: Path) -> ET.Element:
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    try:
        return ET.parse(path, parser=parser).getroot()
    except ET.ParseError as exc:  # pragma: no cover - the message is the point
        raise AssertionError(f"{path} is not well-formed XML: {exc}") from exc


def is_element(node: Any) -> bool:
    return isinstance(node.tag, str)


def tag(ns: str, local: str) -> str:
    return f"{{{ns}}}{local}"


class Project:
    def __init__(self, root: Path) -> None:
        assert root.is_dir(), f"no project at {root}"
        self.root = root
        self.files = sorted((root / "src" / "main" / "mule").glob("*.xml"))
        assert self.files, "no flow XML"
        self.docs = [parse_strict(p) for p in self.files]
        self.parents = {c: p for doc in self.docs for p in doc.iter() for c in p}
        self.callables = {
            str(e.get("name")): e
            for doc in self.docs
            for e in doc
            if is_element(e) and e.tag in (tag(CORE, "flow"), tag(CORE, "sub-flow")) and e.get("name")
        }

    def raw(self) -> str:
        return "\n".join(p.read_text(encoding="utf-8") for p in self.files)

    def nodes(self) -> Iterator[Any]:
        for doc in self.docs:
            yield from doc.iter()

    def comments(self) -> list[str]:
        return [n.text or "" for n in self.nodes() if not is_element(n)]

    def all_texts(self) -> list[str]:
        """Every attribute value, element text and comment in the flow files."""
        found: list[str] = []
        for node in self.nodes():
            if is_element(node):
                found += list(node.attrib.values())
            found.append(node.text or "")
        return found

    def listener_flow(self) -> ET.Element:
        flows = [
            e
            for doc in self.docs
            for e in doc
            if is_element(e)
            and e.tag == tag(CORE, "flow")
            and any(is_element(c) and c.tag == tag(HTTP, "listener") for c in e)
        ]
        assert len(flows) == 1, f"expected one listening flow, found {len(flows)}"
        return flows[0]

    def labelled(self, name: str) -> list[ET.Element]:
        return [n for n in self.nodes() if is_element(n) and n.get(DOC_NAME) == name]

    def step(self, name: str) -> ET.Element:
        found = self.labelled(name)
        assert len(found) == 1, f"expected one generated step labelled {name}, found {len(found)}"
        return found[0]

    def ancestors(self, node: ET.Element) -> list[ET.Element]:
        chain: list[ET.Element] = []
        while node in self.parents:
            node = self.parents[node]
            chain.append(node)
        return chain

    def contains(self, node: ET.Element, name: str, seen: tuple[str, ...] = ()) -> bool:
        """True when a step labelled ``name`` is inside ``node``, following flow-refs."""
        for child in node.iter():
            if not is_element(child):
                continue
            if child.get(DOC_NAME) == name:
                return True
            if child.tag == tag(CORE, "flow-ref"):
                ref = child.get("name") or ""
                if ref in self.callables and ref not in seen and self.contains(self.callables[ref], name, (*seen, ref)):
                    return True
        return False

    def order(self, names: set[str]) -> list[str]:
        """Labelled steps from ``names`` in the listening flow, in document order with flow-refs expanded."""
        found: list[str] = []

        def visit(node: Any, seen: tuple[str, ...]) -> None:
            if not is_element(node):
                return
            label = node.get(DOC_NAME)
            if label in names:
                found.append(str(label))
                return
            if node.tag == tag(CORE, "flow-ref"):
                ref = node.get("name") or ""
                if ref in self.callables and ref not in seen:
                    for child in self.callables[ref]:
                        visit(child, (*seen, ref))
            for child in node:
                visit(child, seen)

        for child in self.listener_flow():
            visit(child, ())
        return found

    def nearby(self, element: ET.Element) -> list[str]:
        """Text in and around ``element``: its subtree, its ancestors' attributes up to the flow, and comments
        right before or after it or any of those ancestors."""
        texts: list[str] = []
        for node in element.iter():
            if is_element(node):
                texts += list(node.attrib.values())
            texts.append(node.text or "")
        node = element
        while node in self.parents:
            parent = self.parents[node]
            if node is not element:
                texts += list(node.attrib.values())
            siblings = list(parent)
            index = next(i for i, s in enumerate(siblings) if s is node)
            for j in (index - 1, index + 1):
                if 0 <= j < len(siblings) and not is_element(siblings[j]):
                    texts.append(siblings[j].text or "")
            if parent.tag in (tag(CORE, "flow"), tag(CORE, "sub-flow")):
                break
            node = parent
        return texts


def values(element: ET.Element) -> list[str]:
    """Attribute values (doc:* excluded) and element texts inside ``element``, whitespace collapsed."""
    found: list[str] = []
    for node in element.iter():
        if not is_element(node):
            continue
        found += [collapse(v) for k, v in node.attrib.items() if not k.startswith(f"{{{DOC}}}")]
        if node.text and node.text.strip():
            found.append(collapse(node.text))
    return found


def has_setting(element: ET.Element, dw: str, key: str | None = None) -> bool:
    """True when ``dw`` is written inside ``element`` as a whole #[dw] value, or (for a header or query
    parameter that the template folds into an object) as the value of ``key`` in a DataWeave object."""
    target = collapse(dw)
    texts = values(element)
    if any(t == f"#[{target}]" for t in texts):
        return True
    if key is None:
        return False
    pattern = re.compile(r"""['"]""" + re.escape(key) + r"""['"]\s*:\s*(?:\(\s*)?""" + re.escape(target), re.IGNORECASE)
    return any(pattern.search(t) for t in texts)


def condition_records(result: Any) -> list[Any]:
    return list(result.conditions)


def condition_record(result: Any, name: str) -> Any:
    found = [c for c in condition_records(result) if str(c.name) == name]
    assert len(found) == 1, f"expected one condition record for {name}, got {[str(c.name) for c in result.conditions]}"
    return found[0]


def pending_names(result: Any) -> list[str]:
    return [str(p.name) for p in getattr(result, "pending", ())]


def policy_record(result: Any, name: str) -> Any:
    found = [r for r in result.policies if str(r.name) == name]
    assert len(found) == 1, f"expected one policy result named {name}, got {[str(r.name) for r in result.policies]}"
    return found[0]


def tree_files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def tree_bytes(root: Path) -> dict[str, bytes]:
    return {rel: (root / rel).read_bytes() for rel in tree_files(root)}


def canonical(path: Path) -> str:
    return ET.canonicalize(from_file=str(path), with_comments=True, strip_text=True)


def compare_trees(actual_dir: Path, golden_dir: Path) -> None:
    assert golden_dir.is_dir(), f"golden folder {golden_dir} is missing"
    actual, golden = tree_files(actual_dir), tree_files(golden_dir)
    assert actual == golden, (sorted(set(actual) - set(golden)), sorted(set(golden) - set(actual)))
    differing = []
    for rel in actual:
        a, g = actual_dir / rel, golden_dir / rel
        same = canonical(a) == canonical(g) if rel.endswith(".xml") else a.read_bytes() == g.read_bytes()
        if not same:
            differing.append(rel)
    assert not differing, f"generated files differ from {golden_dir}: {differing}"


@pytest.fixture(scope="module")
def cond_gen(tmp_path_factory: pytest.TempPathFactory) -> tuple[Project, Any]:
    base = tmp_path_factory.mktemp("cp5-conditions")
    dest = base / "conditions-api" / "mule-app"
    result = generate(conditions_api(base / "bundles"), dest)
    return Project(dest), result


@pytest.fixture(scope="module")
def tmpl_gen(tmp_path_factory: pytest.TempPathFactory) -> tuple[Project, Any]:
    base = tmp_path_factory.mktemp("cp5-templates")
    dest = base / "templates-api" / "mule-app"
    result = generate(templates_api(base / "bundles"), dest)
    return Project(dest), result


# ---------------------------------------------------------------- translator: variables


def test_CP5_T01_request_method_check_becomes_a_dataweave_method_check() -> None:
    """[CP5-T01] A request method check becomes a DataWeave method check."""
    assert_translates('request.verb = "GET"', GET_DW)


def test_CP5_T01_bare_unquoted_word_on_the_right_is_a_string_literal() -> None:
    """[CP5-T01] A bare unquoted word on the right is a string literal (orchestrator decision)."""
    assert_translates("request.verb = GET", GET_DW)


def test_CP5_T02_path_suffix_checks_work_with_equals_double_equals_and_not_equals() -> None:
    """[CP5-T02] Path suffix checks work with =, == and !=."""
    assert_translates('proxy.pathsuffix = "/orders"', '(attributes.maskedRequestPath == "/orders")')
    assert_translates('proxy.pathsuffix == "/orders"', '(attributes.maskedRequestPath == "/orders")')
    assert_translates('proxy.pathsuffix != "/orders"', '(attributes.maskedRequestPath != "/orders")')


@pytest.mark.parametrize(
    "text", ['request.header.X-API-Key = "abc"', 'request.header.x-api-key = "abc"', 'request.header.X-Api-KEY = "abc"']
)
def test_CP5_T03_header_names_are_case_insensitive_and_hyphens_stay_in_the_name(text: str) -> None:
    """[CP5-T03] Header names are matched without regard to case, and hyphens stay part of the name."""
    assert_translates(text, f"({hdr('x-api-key')} == \"abc\")")


def test_CP5_T04_query_parameter_names_keep_their_exact_case() -> None:
    """[CP5-T04] Query parameter names keep their exact case."""
    upper = assert_translates('request.queryparam.Id = "42"', "(attributes.queryParams['Id'] == \"42\")")
    lower = assert_translates('request.queryparam.id = "42"', "(attributes.queryParams['id'] == \"42\")")
    assert upper.dw != lower.dw


def test_CP5_T05_flow_variables_including_dotted_names_become_mule_variables() -> None:
    """[CP5-T05] Flow variables, including dotted names, become Mule variables with the same name."""
    a = assert_translates('myFlag = "on"', "(vars['myFlag'] == \"on\")")
    assert_translates('ext.orderId = "42"', "(vars['ext.orderId'] == \"42\")")
    assert_translates('my_var2 = "x"', "(vars['my_var2'] == \"x\")")
    b = assert_translates('MyFlag = "on"', "(vars['MyFlag'] == \"on\")")
    assert a.dw != b.dw


# ---------------------------------------------------------------- translator: operators


@pytest.mark.parametrize("op", ["AND", "and", "And", "&&"])
def test_CP5_T06_and_works_in_every_spelling(op: str) -> None:
    """[CP5-T06] AND works in every spelling Apigee accepts."""
    assert_translates(f"{GET} {op} {ENV}", f"({GET_DW} and {ENV_DW})")


@pytest.mark.parametrize("op", ["OR", "or", "Or", "||"])
def test_CP5_T06_or_works_in_every_spelling(op: str) -> None:
    """[CP5-T06] OR works in every spelling Apigee accepts."""
    assert_translates(f"{GET} {op} {ENV}", f"({GET_DW} or {ENV_DW})")


@pytest.mark.parametrize("text", [f"{GET} OR {ENV} AND {DEBUG}", f"{GET} AND {ENV} OR {DEBUG}"])
def test_CP5_T07_and_mixed_with_or_without_brackets_is_cant_translate(text: str) -> None:
    """[CP5-T07] AND and OR mixed without brackets is can't translate (orchestrator decision: never guess)."""
    assert_untranslatable(text)


def test_CP5_T07_chains_of_one_operator_group_left_to_right() -> None:
    """[CP5-T07] Chains of the same operator group left to right."""
    assert_translates(f"{GET} OR {ENV} OR {DEBUG}", f"(({GET_DW} or {ENV_DW}) or {DEBUG_DW})")
    assert_translates(f"{GET} AND {ENV} AND {DEBUG}", f"(({GET_DW} and {ENV_DW}) and {DEBUG_DW})")


def test_CP5_T08_brackets_override_grouping_and_extra_brackets_change_nothing() -> None:
    """[CP5-T08] Brackets override the default grouping, and extra brackets change nothing."""
    head = '(attributes.method == "HEAD")'
    assert_translates(
        f'(request.verb = "GET" OR request.verb = "HEAD") AND {ENV}', f"(({GET_DW} or {head}) and {ENV_DW})"
    )
    assert_translates('((request.verb = "GET"))', GET_DW)
    assert_translates('( request.verb = "GET" )', GET_DW)


def test_CP5_T09_not_negates_the_bracketed_condition_and_binds_tighter_than_and() -> None:
    """[CP5-T09] NOT negates the bracketed condition after it and binds tighter than AND."""
    for text in ('NOT (request.verb = "GET")', 'not (request.verb = "GET")', '!(request.verb = "GET")'):
        assert_translates(text, f"(not {GET_DW})")
    head = '(attributes.method == "HEAD")'
    assert_translates(
        'NOT (request.verb = "GET" OR request.verb = "HEAD") AND myFlag = "on"',
        f"((not ({GET_DW} or {head})) and (vars['myFlag'] == \"on\"))",
    )

    text = 'NOT request.verb = "GET"'
    result = translate_condition(text)
    assert result.original == text
    if result.ok:
        assert result.dw is not None and collapse(result.dw) == f"(not {GET_DW})", result.dw
    else:
        assert result.dw is None
    assert "not attributes.method" not in (result.dw or "")


@pytest.mark.parametrize(
    "text",
    [
        'proxy.pathsuffix Matches "/orders/*"',
        'proxy.pathsuffix matches "/orders/*"',
        'proxy.pathsuffix MATCHES "/orders/*"',
    ],
)
def test_CP5_T10_matches_uses_wildcard_patterns_on_paths(text: str) -> None:
    """[CP5-T10] Matches uses wildcard patterns, on paths and headers."""
    result = assert_translates(text, f"(({PATH}) matches /\\/orders\\/.*/)")
    pattern = regex_of(result.dw)
    assert full(pattern, "/orders/42") and full(pattern, "/orders/")
    assert not full(pattern, "/customers/1")


def test_CP5_T10_matches_works_on_headers() -> None:
    """[CP5-T10] Matches uses wildcard patterns on headers too."""
    assert_translates(
        'request.header.Accept Matches "*json*"', f"(({hdr('accept')} default \"\") matches /.*json.*/)"
    )


def test_CP5_T10_question_mark_in_matches_is_cant_translate() -> None:
    """[CP5-T10] '?' inside a Matches pattern is can't translate (orchestrator decision)."""
    assert_untranslatable('proxy.pathsuffix Matches "/orders/?"')


def test_CP5_T11_regex_characters_in_a_matches_pattern_are_taken_literally() -> None:
    """[CP5-T11] Regex characters in a Matches pattern are taken literally."""
    dotted = assert_translates('proxy.pathsuffix Matches "/v1.0/*"', f"(({PATH}) matches /\\/v1\\.0\\/.*/)")
    pattern = regex_of(dotted.dw)
    assert full(pattern, "/v1.0/x") and not full(pattern, "/v1x0/x")

    text = 'proxy.pathsuffix Matches "/a+b(1)/*"'
    result = translate_condition(text)
    assert result.ok is True, result.reason
    raw = MATCH_DW.match(collapse(result.dw))
    assert raw is not None, result.dw
    regex = raw.group("re")
    for char in "+()":
        positions = [i for i, c in enumerate(regex) if c == char]
        assert positions, (char, regex)
        assert all(i > 0 and regex[i - 1] == "\\" for i in positions), (char, regex)
    pattern = regex_of(result.dw)
    assert full(pattern, "/a+b(1)/x") and not full(pattern, "/aab1/x")


@pytest.mark.parametrize(
    "text",
    [
        'proxy.pathsuffix JavaRegex "/orders/[0-9]+"',
        'proxy.pathsuffix javaregex "/orders/[0-9]+"',
        'proxy.pathsuffix ~~ "/orders/[0-9]+"',
    ],
)
def test_CP5_T12_javaregex_and_tilde_pass_the_regex_through_and_match_the_whole_value(text: str) -> None:
    """[CP5-T12] JavaRegex and ~~ pass the regex through and match the whole value."""
    result = assert_translates(text, f"(({PATH}) matches /\\/orders\\/[0-9]+/)")
    pattern = regex_of(result.dw)
    assert full(pattern, "/orders/12") and not full(pattern, "/orders/12/items")


def test_CP5_T13_slashes_in_a_regex_are_escaped_once_never_twice() -> None:
    """[CP5-T13] Slashes in a regex are escaped once, never twice."""
    cases = [
        (r'proxy.pathsuffix ~~ "\/v1\/\d+"', rf"(({PATH}) matches /\/v1\/\d+/)"),
        (r'proxy.pathsuffix ~~ "[/]x"', rf"(({PATH}) matches /[\/]x/)"),
        (r'proxy.pathsuffix ~~ "/a/\d{2}"', rf"(({PATH}) matches /\/a\/\d{{2}}/)"),
    ]
    for text, expected in cases:
        result = assert_translates(text, expected)
        assert "\\\\/" not in result.dw, result.dw
        if "\\d" in text:
            assert "\\d" in result.dw, result.dw


def test_CP5_T14_matches_and_javaregex_treat_the_same_pattern_differently() -> None:
    """[CP5-T14] The same pattern text means different things under Matches and JavaRegex."""
    glob = assert_translates('proxy.pathsuffix Matches "/a.*"', f"(({PATH}) matches /\\/a\\..*/)")
    regex = assert_translates('proxy.pathsuffix JavaRegex "/a.*"', f"(({PATH}) matches /\\/a.*/)")
    g, r = regex_of(glob.dw), regex_of(regex.dw)
    assert full(g, "/a.json") and full(r, "/a.json")
    assert not full(g, "/ab") and full(r, "/ab")


def test_CP5_T15_compared_values_keep_their_case() -> None:
    """[CP5-T15] Compared values keep their case."""
    a = assert_translates('request.verb = "get"', '(attributes.method == "get")')
    b = assert_translates('proxy.pathsuffix Matches "/Orders*"', f"(({PATH}) matches /\\/Orders.*/)")
    assert "(?i)" not in a.dw and "(?i)" not in b.dw


def test_CP5_T16_quoted_text_is_one_value_and_is_escaped_for_dataweave() -> None:
    """[CP5-T16] Text inside quotes is kept as one value and escaped safely for DataWeave."""
    assert_translates(
        'request.header.X-Tag = "rock AND roll (live) = yes"',
        f"({hdr('x-tag')} == \"rock AND roll (live) = yes\")",
    )
    assert_translates(
        'request.header.X-Note = "price $(total)"', rf"""({hdr('x-note')} == "price \$(total)")"""
    )
    assert_translates('request.header.X-Note = "it\'s"', f"({hdr('x-note')} == \"it's\")")


def test_CP5_T17_spacing_and_line_breaks_do_not_change_the_result() -> None:
    """[CP5-T17] Spacing and line breaks do not change the result."""
    assert_translates('request.verb="GET"', GET_DW)
    assert_translates('request.verb   =   "GET"', GET_DW)
    assert_translates('\n  (request.verb = "GET")\n\tAND myFlag="on"\n', f"({GET_DW} and (vars['myFlag'] == \"on\"))")


def test_CP5_T18_text_compared_with_a_number_never_becomes_an_always_false_check() -> None:
    """[CP5-T18] Comparing a text value with a number is never turned into a check that can never pass."""
    assert_translates('request.header.X-Count = "5"', f"({hdr('x-count')} == \"5\")")
    text = "request.header.X-Count = 5"
    result = translate_condition(text)
    assert result.original == text
    if result.ok:
        assert result.dw is not None
        assert collapse(result.dw) != f"({hdr('x-count')} == 5)", result.dw
        assert "~=" in result.dw or "as Number" in result.dw, result.dw
    else:
        assert result.dw is None
        assert re.search(r"number|numeric", result.reason or "", re.IGNORECASE), result.reason


def test_CP5_T19_checks_against_null_keep_the_missing_variable_meaning() -> None:
    """[CP5-T19] Checks against null keep Apigee's missing-variable meaning."""
    assert_translates("request.header.X-Trace = null", f"({hdr('x-trace')} == null)")
    assert_translates("request.header.X-Trace != null", f"({hdr('x-trace')} != null)")
    assert_translates('request.header.X-Trace != "on"', f"({hdr('x-trace')} != \"on\")")


@pytest.mark.parametrize(
    "name",
    [
        "client.ip",
        "system.timestamp",
        "fault.name",
        "environment.name",
        "request.content",
        "request.formparam.a",
        "response.status.code",
        "target.url",
        "message.verb",
    ],
)
def test_CP5_T20_unmapped_builtin_variables_are_cant_translate_not_user_variables(name: str) -> None:
    """[CP5-T20] Built-in Apigee variables a2m has no mapping for are can't translate, not user variables."""
    assert_untranslatable(f'{name} = "x"', name)


@pytest.mark.parametrize(
    "text",
    [
        'request.verb = "GET" AND client.ip = "10.0.0.1"',
        'request.verb = "GET" OR client.ip = "10.0.0.1"',
        'NOT (client.ip = "10.0.0.1")',
        '(request.verb = "GET" AND (myFlag = "on" OR fault.name = "X"))',
    ],
)
def test_CP5_T21_one_untranslatable_part_makes_the_whole_condition_untranslatable(text: str) -> None:
    """[CP5-T21] One untranslatable part makes the whole condition untranslatable."""
    assert_untranslatable(text)


@pytest.mark.parametrize(
    "text",
    [
        '(request.verb = "GET"',
        'request.verb = "GET")',
        'request.verb = "GET',
        "request.verb =",
        'AND request.verb = "GET"',
        'request.verb "GET"',
        'request.verb =~= "GET"',
        'proxy.pathsuffix ~~ "[0-9"',
        "request.header.X-Trace",
        "myFlag",
    ],
)
def test_CP5_T22_broken_or_ambiguous_text_is_cant_translate(text: str) -> None:
    """[CP5-T22] Broken or ambiguous condition text is marked can't translate, never guessed or made always true."""
    assert_untranslatable(text)


def test_CP5_T23_startswith_is_cant_translate() -> None:
    """[CP5-T23] =| (StartsWith) is can't translate, with the original kept."""
    assert_untranslatable('request.header.User-Agent =| "curl"')


def test_CP5_T23_matchespath_is_translated_with_path_segment_meaning() -> None:
    """[CP5-T23] MatchesPath is translated: * is one path segment, ** any number of segments."""
    one = translate_condition('proxy.pathsuffix MatchesPath "/orders/*"')
    many = translate_condition('proxy.pathsuffix MatchesPath "/orders/**"')
    exact = translate_condition('proxy.pathsuffix MatchesPath "/orders"')
    for result in (one, many, exact):
        assert result.ok is True, (result.original, result.reason)
    p1, p2, p3 = regex_of(one.dw), regex_of(many.dw), regex_of(exact.dw)
    assert full(p1, "/orders/1") and not full(p1, "/orders/1/items")
    assert full(p2, "/orders/1/items") and full(p2, "/orders/1")
    assert full(p3, "/orders") and not full(p3, "/orders/1")


def test_CP5_T23_other_operators_are_exact_or_cant_translate() -> None:
    """[CP5-T23] :=, Equals and > are handled with exactly Apigee's meaning or marked can't translate."""
    eq_ci = translate_condition('request.verb := "get"')
    assert eq_ci.original == 'request.verb := "get"'
    if eq_ci.ok:
        assert collapse(eq_ci.dw) != '(attributes.method == "get")', eq_ci.dw
        assert "lower(" in eq_ci.dw, eq_ci.dw
    else:
        assert eq_ci.dw is None and (eq_ci.reason or "").strip()

    equals = translate_condition('request.verb Equals "GET"')
    if equals.ok:
        assert collapse(equals.dw) == GET_DW, equals.dw
    else:
        assert equals.dw is None and (equals.reason or "").strip()

    greater = translate_condition("request.header.X-Count > 5")
    if greater.ok:
        assert "as Number" in greater.dw and ">" in greater.dw, greater.dw
    else:
        assert greater.dw is None and (greater.reason or "").strip()


@pytest.mark.parametrize("text", ["", "  \n\t "])
def test_CP5_T24_empty_condition_string_means_always_run(text: str) -> None:
    """[CP5-T24] An empty condition means the step always runs, without being flagged."""
    result = translate_condition(text)
    assert result.ok is True
    assert result.dw is None
    assert result.original == text


def test_CP5_T24_empty_condition_step_is_generated_unguarded(cond_gen: tuple[Project, Any]) -> None:
    """[CP5-T24] A step with an empty <Condition/> is not wrapped, flagged or counted."""
    project, result = cond_gen
    element = project.step("AM-Empty")
    wrappers = [a for a in project.ancestors(element) if a.tag in (tag(CORE, "when"), tag(CORE, "choice"))]
    assert wrappers == [], "AM-Empty is wrapped in a choice/when"
    assert not any(CANT.search(t) for t in project.nearby(element))
    assert not any("pending" in t.lower() or "not translated" in t.lower() for t in project.nearby(element))
    assert "AM-Empty" not in [str(c.name) for c in condition_records(result)]
    assert "AM-Empty" not in pending_names(result)


# ---------------------------------------------------------------- templates


def test_CP5_T25_variables_in_assignmessage_header_and_query_values_are_translated(
    cond_gen: tuple[Project, Any],
) -> None:
    """[CP5-T25] Variables inside AssignMessage header and query parameter values are translated."""
    project, _ = cond_gen
    element = project.step("AM-Auth")
    assert has_setting(element, f'"Bearer " ++ ({hdr("x-token")} default "")', "authorization"), values(
        element
    )
    assert has_setting(element, "(vars['ext.orderId'] default \"\")", "trace"), values(element)
    assert not any("{request.header.X-Token}" in v for v in values(element)), values(element)


JSON_DW = (
    r""""{\"id\":\"" ++ (attributes.queryParams['id'] default "") ++ "\",\"verb\":\"" """
    r'''++ (attributes.method default "") ++ "\"}"'''
)
PREFIXED_DW = r'''"{\"id\":\"" ++ (attributes.queryParams['id'] default "") ++ "\"}"'''


def test_CP5_T26_json_payload_keeps_its_braces_and_only_real_variables_are_replaced(
    tmpl_gen: tuple[Project, Any],
) -> None:
    """[CP5-T26] A JSON payload keeps its own braces and only real variables are replaced."""
    result = translate_template(JSON_PAYLOAD)
    assert result.ok is True, result.reason
    assert result.original == JSON_PAYLOAD
    assert collapse(result.dw) == JSON_DW, result.dw

    project, _ = tmpl_gen
    element = project.step("AM-Json")
    assert has_setting(element, JSON_DW), values(element)


def test_CP5_T27_literal_text_around_template_variables_is_escaped_safely() -> None:
    """[CP5-T27] Literal text around template variables is escaped safely."""
    text = 'Cost $(5) "x" C:\\tmp {request.verb}'
    result = translate_template(text)
    assert result.ok is True, result.reason
    assert result.original == text
    assert collapse(result.dw) == r""""Cost \$(5) \"x\" C:\\tmp " ++ (attributes.method default "")""", result.dw


@pytest.mark.parametrize(
    ("text", "named"),
    [(X_TIME, "system.timestamp"), (X_UP, "toUpperCase"), ("id {request.queryparam.id} from {client.ip}", "client.ip")],
)
def test_CP5_T28_template_with_unmapped_variable_or_function_is_cant_translate(text: str, named: str) -> None:
    """[CP5-T28] A template that uses an unmapped variable or a function is marked can't translate."""
    result = translate_template(text)
    assert result.ok is False, result.dw
    assert result.dw is None
    assert result.original == text
    assert named in (result.reason or ""), result.reason


def test_CP5_T28_untranslatable_settings_are_marked_and_never_blanked_in_the_project(
    tmpl_gen: tuple[Project, Any],
) -> None:
    """[CP5-T28] Untranslatable settings carry a can't-translate marker and are not emitted half-done."""
    project, result = tmpl_gen
    texts = project.all_texts()
    for original in (X_TIME, X_UP):
        assert any(CANT.search(t) and original in t for t in texts), f"no can't-translate marker holding {original}"

    options = list(policy_record(result, "AM-Unmapped").unsupported_options)
    for header, named in (("X-Time", "system.timestamp"), ("X-Up", "toUpperCase")):
        matching = [o for o in options if header.lower() in f"{o.name} {o.reason}".lower() and named in str(o.reason)]
        assert matching, f"AM-Unmapped's record does not list {header} as untranslatable: {options}"

    emitted = [v for e in project.labelled("AM-Unmapped") for v in values(e)]
    for value in emitted:
        assert not re.search(r"""['"]x-(time|up)['"]\s*:""", value, re.IGNORECASE), value
        assert "{system.timestamp}" not in value and "{toUpperCase(" not in value, value


def test_CP5_T29_translate_template_honours_custom_markers() -> None:
    """[CP5-T29] translate_template with prefix '@' and suffix '#' uses those markers, not braces."""
    result = translate_template(PREFIXED_PAYLOAD, prefix="@", suffix="#")
    assert result.ok is True, result.reason
    assert collapse(result.dw) == PREFIXED_DW, result.dw


def test_CP5_T29_payload_with_custom_markers_uses_them_or_is_listed(tmpl_gen: tuple[Project, Any]) -> None:
    """[CP5-T29] A payload with custom variable markers uses those markers, or is listed as unsupported."""
    project, result = tmpl_gen
    options = list(policy_record(result, "AM-Prefixed").unsupported_options)
    flagged = any(re.search(r"variablePrefix|variableSuffix", f"{o.name} {o.reason}") for o in options)
    emitted = [v for e in project.labelled("AM-Prefixed") for v in values(e)]
    for value in emitted:
        assert not re.search(r"""vars\[[^\]]*(@request|\\?"id)""", value), value
    if not flagged:
        assert project.labelled("AM-Prefixed"), "AM-Prefixed is neither generated nor flagged"
        assert has_setting(project.step("AM-Prefixed"), PREFIXED_DW), emitted
        assert not any("@request.queryparam.id#" in v for v in emitted), emitted


@pytest.mark.parametrize("text", list(LITERALS.values()))
def test_CP5_T30_values_without_variables_and_stray_braces_stay_plain_text(text: str) -> None:
    """[CP5-T30] Values without variables, and stray braces, are left as plain text."""
    result = translate_template(text)
    assert result.ok is True, result.reason
    assert result.dw is None
    assert result.original == text


def test_CP5_T30_literal_header_values_are_emitted_as_plain_strings(tmpl_gen: tuple[Project, Any]) -> None:
    """[CP5-T30] Literal header values are emitted the same way CP4 emits a literal value."""
    project, result = tmpl_gen
    assert str(policy_record(result, "AM-Literal").method) == "template"
    emitted = values(project.step("AM-Literal"))
    for header, value in LITERALS.items():
        literal = re.compile(
            r"""['"]""" + re.escape(header.lower()) + r"""['"]\s*:\s*(['"])""" + re.escape(value) + r"\1"
        )
        assert any(literal.search(v) for v in emitted), (header, emitted)
        assert not any(v == f'#["{value}"]' for v in emitted), emitted
    assert not any('default ""' in v for v in emitted), emitted


def test_CP5_T31_variables_in_raisefault_responses_are_translated(tmpl_gen: tuple[Project, Any]) -> None:
    """[CP5-T31] Variables in RaiseFault responses are translated too."""
    project, _ = tmpl_gen
    element = project.step("RF-Echo")
    assert has_setting(element, '"missing " ++ (attributes.queryParams[\'id\'] default "")'), values(element)
    assert has_setting(element, '(attributes.method default "")', "x-verb"), values(element)


# ---------------------------------------------------------------- conditions in the generated project


GET_ORDERS_WHEN = re.compile(
    r'^#\[\(\(\(attributes\.maskedRequestPath default ""\) matches /(?P<re>(?:\\.|[^/\\])*)/\) '
    r'and \(attributes\.method == "GET"\)\)\]$'
)
POST_ORDERS_WHEN = (
    '#[(((attributes.maskedRequestPath default "") matches /\\/orders.*/) and (attributes.method == "POST"))]'
)


def test_CP5_T32_conditional_flows_become_a_choice_in_apigee_order(cond_gen: tuple[Project, Any]) -> None:
    """[CP5-T32] Conditional flows become a choice that tries them in Apigee's order."""
    project, _ = cond_gen
    candidates = []
    for node in project.nodes():
        if not is_element(node) or node.tag != tag(CORE, "choice"):
            continue
        branches = [c for c in node if is_element(c)]
        shape = [b.tag for b in branches] == [tag(CORE, "when"), tag(CORE, "when"), tag(CORE, "otherwise")]
        if (
            shape
            and project.contains(branches[0], "AM-Auth")
            and project.contains(branches[1], "AM-Tag")
            and project.contains(branches[2], "RF-NotFound")
        ):
            candidates.append(branches)
    assert len(candidates) == 1, f"expected one choice for the conditional flows, found {len(candidates)}"
    first, second, _ = candidates[0]
    match = GET_ORDERS_WHEN.match(collapse(first.get("expression") or ""))
    assert match is not None, first.get("expression")
    pattern = match.group("re").replace("\\/", "/")
    assert full(pattern, "/orders/1") and not full(pattern, "/orders/1/items")
    assert collapse(second.get("expression") or "") == POST_ORDERS_WHEN, second.get("expression")


@pytest.mark.parametrize(
    ("name", "expression"),
    [
        ("SA-Limit", "#[(attributes.queryParams['tier'] != \"gold\")]"),
        ("AM-Auth", f"#[({hdr('x-api-key')} == \"abc\")]"),
        ("AM-Tag", f"#[({hdr('x-tag')} == \"a<b\")]"),
    ],
)
def test_CP5_T33_step_conditions_wrap_their_step_in_place(
    cond_gen: tuple[Project, Any], name: str, expression: str
) -> None:
    """[CP5-T33] Step conditions wrap their step in place, and special characters survive the XML file."""
    project, result = cond_gen
    element = project.step(name)
    when = project.parents[element]
    assert when.tag == tag(CORE, "when"), when.tag
    assert [c for c in when if is_element(c)] == [element], f"{name} is not alone in its when"
    assert project.parents[when].tag == tag(CORE, "choice")
    assert collapse(when.get("expression") or "") == expression, when.get("expression")
    assert name not in pending_names(result)
    assert not any("not translated yet" in t for t in project.nearby(element))


def test_CP5_T33_steps_keep_their_cp4_position_and_the_file_escapes_less_than(cond_gen: tuple[Project, Any]) -> None:
    """[CP5-T33] Wrapped steps keep the position CP4 gave them; '<' is escaped in the file and decodes once."""
    project, _ = cond_gen
    names = {"SA-Limit", "Extract-Order-Id", "AM-Order", "AM-Empty", "AM-Auth", "AM-Tag", "RF-NotFound"}
    assert project.order(names) == [
        "SA-Limit",
        "Extract-Order-Id",
        "AM-Order",
        "AM-Empty",
        "AM-Auth",
        "AM-Tag",
        "RF-NotFound",
    ], project.order(names)
    raw = project.raw()
    assert "a&lt;b" in raw and "a&amp;lt;b" not in raw


def test_CP5_T34_untranslatable_step_condition_is_marked_kept_and_never_unguarded(
    cond_gen: tuple[Project, Any],
) -> None:
    """[CP5-T34] An untranslatable step condition is marked and kept, and the step does not run unguarded."""
    project, result = cond_gen
    found = project.labelled("AM-Client")
    texts: list[str] = [c for c in project.comments() if "AM-Client" in c]
    if found:
        assert len(found) == 1
        element = found[0]
        chain = project.ancestors(element)
        assert chain[0].tag not in (tag(CORE, "flow"), tag(CORE, "sub-flow")), "AM-Client is a direct child of a flow"
        assert any(a.tag == tag(CORE, "when") for a in chain), "AM-Client is not guarded by a when"
        texts += project.nearby(element)
    else:
        assert texts, "AM-Client was deleted: no step and no comment naming it"
    assert any(CANT.search(t) and COND_CLIENT in t for t in texts), texts

    for node in project.nodes():
        if is_element(node) and node.tag == tag(CORE, "when"):
            expression = (node.get("expression") or "").replace(" ", "")
            assert expression not in ("", "#[]", "#[true]", "true"), node.get("expression")

    record = condition_record(result, "AM-Client")
    assert record.ok is False
    assert record.original == COND_CLIENT
    assert "client.ip" in (record.reason or ""), record.reason


def test_CP5_T35_every_condition_gets_exactly_one_record(cond_gen: tuple[Project, Any]) -> None:
    """[CP5-T35] Every condition in the bundle gets exactly one translated or can't-translate record."""
    _, result = cond_gen
    records = condition_records(result)
    assert len(records) == CONDITION_COUNT, [str(c.name) for c in records]
    assert sorted(str(c.name) for c in records) == sorted(EXPECTED_CONDITIONS)
    for record in records:
        assert record.original == EXPECTED_CONDITIONS[str(record.name)], (record.name, record.original)
        assert record.ok in (True, False)
    assert {str(c.name) for c in records if not c.ok} == {"spike", "AM-Client"}
    assert pending_names(result) == []
    expected_dw = {
        "v2": f"({hdr('x-version')} == \"2\")",
        "SA-Limit": "(attributes.queryParams['tier'] != \"gold\")",
        "AM-Auth": f"({hdr('x-api-key')} == \"abc\")",
        "AM-Tag": f"({hdr('x-tag')} == \"a<b\")",
        "AM-Order": "(vars['ext.orderId'] == \"42\")",
        "post-orders": POST_ORDERS_WHEN[2:-1],
    }
    for name, dw in expected_dw.items():
        record = condition_record(result, name)
        assert record.ok is True and collapse(record.dw or "") == dw, (name, record.dw)
    spike = condition_record(result, "spike")
    assert spike.dw is None and "fault.name" in (spike.reason or ""), spike.reason


def test_CP5_T36_variable_set_by_extractvariables_is_read_under_the_same_name(cond_gen: tuple[Project, Any]) -> None:
    """[CP5-T36] A variable set by ExtractVariables is read under the same Mule name in a later condition."""
    project, _ = cond_gen
    order = project.step("AM-Order")
    when = project.parents[order]
    assert when.tag == tag(CORE, "when")
    assert collapse(when.get("expression") or "") == "#[(vars['ext.orderId'] == \"42\")]", when.get("expression")
    extract = project.step("Extract-Order-Id")
    set_names = [n.get("variableName") for n in extract.iter() if is_element(n) and n.tag == tag(CORE, "set-variable")]
    assert "ext.orderId" in set_names, set_names


def test_CP5_T37_output_is_repeatable_and_projects_without_conditions_are_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[CP5-T37] Output with conditions is repeatable, and projects without conditions are unchanged."""
    monkeypatch.delenv(UPDATE_GOLDEN_ENV, raising=False)
    bundles = tmp_path / "bundles"
    for make in (conditions_api, templates_api):
        source = make(bundles)
        first, second = tmp_path / "one" / source.name, tmp_path / "two" / source.name
        generate(source, first)
        generate(source, second)
        assert tree_bytes(first) == tree_bytes(second), source.name

    simple = tmp_path / "golden" / "orders-simple"
    generate(CP4_ORDERS_SIMPLE, simple)
    compare_trees(simple, GOLDEN_CP3 / "orders-simple")

    copy = tmp_path / "azure"
    shutil.copytree(TEST_API, copy / "Test-API")
    shutil.copytree(GET_SHARED_FLOW, copy / "GetSharedFlow")
    test_api = tmp_path / "golden" / "Test-API"
    generate(copy / "Test-API", test_api, (copy / "GetSharedFlow",))
    compare_trees(test_api, GOLDEN_CP3 / "Test-API")

    orders = tmp_path / "golden" / "orders-api"
    generate(CP4_ORDERS_API, orders)
    compare_trees(orders, GOLDEN_CP4 / "orders-api")


# ---------------------------------------------------------------- CP5 adversarial round 1
#
# Request changes made by earlier steps apply to every reader (conditions, message templates, target URLs),
# request variables read on the response side use the request snapshot saved before the target call, unknown
# built-in categories and ""/null comparisons a2m can't match exactly are refused, and a conditional Flow's
# condition is never affected by a sibling Flow that was tried before it.

SNAPSHOT_READ = "vars.a2mSentRequest"


def r1_proxy(
    pre_request: str = "",
    pre_response: str = "",
    flows: str = "",
    post_request: str = "",
    route_condition: str | None = None,
) -> str:
    route = ""
    if route_condition is not None:
        route = (
            f'    <RouteRule name="guarded">\n        <Condition>{route_condition}</Condition>\n'
            "        <TargetEndpoint>default</TargetEndpoint>\n    </RouteRule>\n"
        )
    return (
        XML_HEAD + '<ProxyEndpoint name="default">\n'
        f'    <PreFlow name="PreFlow">\n        <Request>{pre_request}</Request>\n'
        f"        <Response>{pre_response}</Response>\n    </PreFlow>\n"
        f"    <Flows>{flows}</Flows>\n"
        f'    <PostFlow name="PostFlow">\n        <Request>{post_request}</Request>\n        <Response/>\n'
        "    </PostFlow>\n"
        "    <HTTPProxyConnection>\n        <BasePath>/shop</BasePath>\n        <VirtualHost>default</VirtualHost>\n"
        "    </HTTPProxyConnection>\n"
        f"{route}"
        '    <RouteRule name="default">\n        <TargetEndpoint>default</TargetEndpoint>\n    </RouteRule>\n'
        "</ProxyEndpoint>\n"
    )


def r1_flow(name: str, condition: str, request: str) -> str:
    return (
        f'<Flow name="{name}"><Request>{request}</Request><Response/>'
        f"<Condition>{condition}</Condition></Flow>"
    )


def r1_target(url: str) -> str:
    return (
        XML_HEAD + '<TargetEndpoint name="default">\n'
        '    <PreFlow name="PreFlow">\n        <Request/>\n        <Response/>\n    </PreFlow>\n'
        "    <Flows/>\n"
        '    <PostFlow name="PostFlow">\n        <Request/>\n        <Response/>\n    </PostFlow>\n'
        f"    <HTTPTargetConnection>\n        <URL>{url}</URL>\n    </HTTPTargetConnection>\n</TargetEndpoint>\n"
    )


def r1_generate(
    tmp_path: Path, proxy_xml: str, policies: dict[str, str], target_url: str | None = None
) -> tuple[Project, Any]:
    root = write_bundle(tmp_path / "bundles", "r1-api", proxy_xml, policies)
    if target_url is not None:
        (root / "apiproxy" / "targets" / "default.xml").write_text(r1_target(target_url), encoding="utf-8")
    dest = tmp_path / "out" / "r1-api" / "mule-app"
    result = generate(root, dest)
    return Project(dest), result


def r1_set_verb(name: str, verb: str) -> str:
    return policy(
        "AssignMessage",
        name,
        f"    <Set>\n        <Verb>{verb}</Verb>\n    </Set>\n"
        '    <AssignTo createNew="false" transport="http" type="request"/>\n',
    )


def r1_when_of(project: Project, name: str) -> ET.Element:
    element = project.step(name)
    when = project.parents[element]
    assert when.tag == tag(CORE, "when"), (name, when.tag)
    return when


def r1_http_requests(project: Project) -> list[ET.Element]:
    return [n for n in project.nodes() if is_element(n) and n.tag == tag(HTTP, "request")]


def r1_snapshots(project: Project) -> list[ET.Element]:
    return [
        n
        for n in project.nodes()
        if is_element(n) and n.tag == tag(CORE, "set-variable") and n.get("variableName") == "a2mSentRequest"
    ]


def test_CP5_T38_conditions_reading_a_request_part_changed_by_an_earlier_step_are_cant_translate(
    tmp_path: Path,
) -> None:
    """[CP5-T38] A step, Flow or RouteRule condition reading a header or the verb an earlier step changes is refused."""
    proxy = r1_proxy(
        pre_request=step("AM-Set-Foo") + step("AM-Verb")
        + step("AM-Foo-Guarded", 'request.header.X-Foo = "abc"')
        + step("AM-Verb-Guarded", 'request.verb = "POST"')
        + step("AM-Other-Guarded", 'request.header.X-Other = "1"'),
        flows=r1_flow("foo-flow", 'request.header.x-foo = "abc"', step("AM-In-Flow")),
        route_condition='request.header.X-Foo = "abc"',
    )
    policies = {
        "AM-Set-Foo": assign_headers("AM-Set-Foo", [("X-Foo", "abc")]),
        "AM-Verb": r1_set_verb("AM-Verb", "POST"),
        "AM-Foo-Guarded": assign_headers("AM-Foo-Guarded", [("X-Seen-Foo", "1")]),
        "AM-Verb-Guarded": assign_headers("AM-Verb-Guarded", [("X-Seen-Verb", "1")]),
        "AM-Other-Guarded": assign_headers("AM-Other-Guarded", [("X-Seen-Other", "1")]),
        "AM-In-Flow": assign_headers("AM-In-Flow", [("X-In-Flow", "1")]),
    }
    project, result = r1_generate(tmp_path, proxy, policies)

    for name, changed_by in (
        ("AM-Foo-Guarded", "AM-Set-Foo"),
        ("AM-Verb-Guarded", "AM-Verb"),
        ("foo-flow", "AM-Set-Foo"),
        ("guarded", "AM-Set-Foo"),
    ):
        record = condition_record(result, name)
        assert record.ok is False and record.dw is None, (name, record.dw)
        assert changed_by in (record.reason or ""), (name, record.reason)
    for name in ("AM-Foo-Guarded", "AM-Verb-Guarded"):
        when = r1_when_of(project, name)
        assert collapse(when.get("expression") or "") == "#[false]", when.get("expression")
        assert any(CANT.search(t) for t in project.nearby(project.step(name))), name

    other = condition_record(result, "AM-Other-Guarded")
    assert other.ok is True and collapse(other.dw or "") == f"({hdr('x-other')} == \"1\")", other


def test_CP5_T38_a_new_request_marks_every_header_query_parameter_and_the_verb_changed(tmp_path: Path) -> None:
    """[CP5-T38] AssignTo createNew on the request changes every header, query parameter and the verb."""
    new_request = policy(
        "AssignMessage",
        "AM-New",
        '    <Set>\n        <Headers>\n            <Header name="X-A">1</Header>\n        </Headers>\n    </Set>\n'
        '    <AssignTo createNew="true" transport="http" type="request"/>\n',
    )
    proxy = r1_proxy(
        pre_request=step("AM-New")
        + step("AM-H", 'request.header.Anything = "x"')
        + step("AM-Q", 'request.queryparam.any = "x"')
        + step("AM-V", 'request.verb = "GET"'),
    )
    policies = {
        "AM-New": new_request,
        "AM-H": assign_headers("AM-H", [("X-H", "1")]),
        "AM-Q": assign_headers("AM-Q", [("X-Q", "1")]),
        "AM-V": assign_headers("AM-V", [("X-V", "1")]),
    }
    _, result = r1_generate(tmp_path, proxy, policies)
    for name in ("AM-H", "AM-Q", "AM-V"):
        record = condition_record(result, name)
        assert record.ok is False and "AM-New" in (record.reason or ""), (name, record.reason)


def test_CP5_T39_templates_reading_a_request_part_changed_by_an_earlier_step_are_cant_translate(
    tmp_path: Path,
) -> None:
    """[CP5-T39] AssignMessage and RaiseFault values reading a header or the verb an earlier step changes are refused."""
    use_token = assign_headers(
        "AM-Use-Token", [("Authorization", "Bearer {request.header.X-Token}"), ("X-Kept", "{request.header.X-Free}")]
    )
    echo_verb = assign_headers("AM-Echo-Verb", [("X-Verb", "{request.verb}")])
    fault = policy(
        "RaiseFault",
        "RF-Token",
        "    <FaultResponse>\n        <Set>\n"
        '            <Payload contentType="text/plain">token {request.header.X-Token}</Payload>\n'
        "            <StatusCode>400</StatusCode>\n        </Set>\n    </FaultResponse>\n"
        "    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n",
    )
    proxy = r1_proxy(
        pre_request=step("AM-Set-Token") + step("AM-Verb") + step("AM-Use-Token") + step("AM-Echo-Verb")
        + step("RF-Token", 'request.header.X-Free = "boom"'),
    )
    policies = {
        "AM-Set-Token": assign_headers("AM-Set-Token", [("X-Token", "abc")]),
        "AM-Verb": r1_set_verb("AM-Verb", "POST"),
        "AM-Use-Token": use_token,
        "AM-Echo-Verb": echo_verb,
        "RF-Token": fault,
    }
    project, result = r1_generate(tmp_path, proxy, policies)

    for name, original, changed_by in (
        ("AM-Use-Token", "Bearer {request.header.X-Token}", "AM-Set-Token"),
        ("AM-Echo-Verb", "{request.verb}", "AM-Verb"),
        ("RF-Token", "token {request.header.X-Token}", "AM-Set-Token"),
    ):
        options = list(policy_record(result, name).unsupported_options)
        matching = [o for o in options if o.original == original and changed_by in str(o.reason)]
        assert matching, (name, options)
        emitted = [v for e in project.labelled(name) for v in values(e)]
        assert not any("{request." in v for v in emitted), emitted
    use = [v for e in project.labelled("AM-Use-Token") for v in values(e)]
    assert not any(re.search(r"""['"]authorization['"]\s*:""", v) for v in use), use
    assert has_setting(project.step("AM-Use-Token"), f"({hdr('x-free')} default \"\")", "x-kept"), use


def test_CP5_T40_response_side_request_reads_use_the_request_snapshot(tmp_path: Path) -> None:
    """[CP5-T40] A response step guarded by request.verb reads the request saved before the target call."""
    proxy = r1_proxy(
        pre_request=step("AM-Set-Token"),
        pre_response=step("AM-Resp", 'request.verb != "OPTIONS"')
        + step("AM-Resp-Token", 'request.header.X-Token = "abc"')
        + step("AM-Resp-Echo"),
    )
    echo = policy(
        "AssignMessage",
        "AM-Resp-Echo",
        '    <Set>\n        <Headers>\n            <Header name="X-Echo">{request.header.X-Caller}</Header>\n'
        "        </Headers>\n    </Set>\n    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n",
    )
    policies = {
        "AM-Set-Token": assign_headers("AM-Set-Token", [("X-Token", "abc")]),
        "AM-Resp": policy(
            "AssignMessage",
            "AM-Resp",
            '    <Set>\n        <Headers>\n            <Header name="X-Not-Options">yes</Header>\n'
            "        </Headers>\n    </Set>\n",
        ),
        "AM-Resp-Token": policy(
            "AssignMessage",
            "AM-Resp-Token",
            '    <Set>\n        <Headers>\n            <Header name="X-T">1</Header>\n        </Headers>\n    </Set>\n',
        ),
        "AM-Resp-Echo": echo,
    }
    project, result = r1_generate(tmp_path, proxy, policies)

    record = condition_record(result, "AM-Resp")
    assert record.ok is True, record.reason
    assert collapse(record.dw or "") == f'({SNAPSHOT_READ}.method != "OPTIONS")', record.dw
    when = r1_when_of(project, "AM-Resp")
    assert collapse(when.get("expression") or "") == f'#[({SNAPSHOT_READ}.method != "OPTIONS")]'

    requests = r1_http_requests(project)
    assert len(requests) == 1
    siblings = [c for c in project.parents[requests[0]] if is_element(c)]
    before = siblings[siblings.index(requests[0]) - 1]
    assert before.tag == tag(CORE, "set-variable") and before.get("variableName") == "a2mSentRequest", before.attrib
    for part in ("attributes.method", "attributes.maskedRequestPath", "attributes.headers", "attributes.queryParams"):
        assert part in (before.get("value") or ""), (part, before.get("value"))

    token = condition_record(result, "AM-Resp-Token")
    assert token.ok is False and "AM-Set-Token" in (token.reason or ""), token.reason
    assert has_setting(project.step("AM-Resp-Echo"), f"({hdr('x-caller', SNAPSHOT_READ + '.headers')} default \"\")", "x-echo")


def test_CP5_T40_the_snapshot_is_only_written_when_something_reads_it(
    cond_gen: tuple[Project, Any], tmpl_gen: tuple[Project, Any]
) -> None:
    """[CP5-T40] A project with no response-side request read writes no request snapshot."""
    for project, _ in (cond_gen, tmpl_gen):
        assert r1_snapshots(project) == []


def test_CP5_T40_a_fault_rule_condition_still_refuses_request_variables(tmp_path: Path) -> None:
    """[CP5-T40] A fault rule may run before the target call, so its request reads stay can't translate."""
    proxy = r1_proxy().replace(
        '<ProxyEndpoint name="default">\n',
        '<ProxyEndpoint name="default">\n    <FaultRules>\n        <FaultRule name="on-get">\n'
        f"            {step('RF-Fault')}\n"
        '            <Condition>request.verb = "GET"</Condition>\n        </FaultRule>\n    </FaultRules>\n',
    )
    _, result = r1_generate(tmp_path, proxy, {"RF-Fault": raise_fault("RF-Fault", 500, "x")})
    record = condition_record(result, "on-get")
    assert record.ok is False and "request.verb" in (record.reason or ""), record


@pytest.mark.parametrize(
    "name",
    [
        "virtualhost.name",
        "route.target",
        "loadbalancing.isfallback",
        "jwt.JWT-Verify.claim.sub",
        "responsecache.RC-1.cachehit",
        "lookupcache.LC-1.cachehit",
        "messagelogging.ML-1.failed",
        "apiproduct.name",
    ],
)
def test_CP5_T41_unknown_builtin_categories_are_cant_translate_never_a_null_flow_variable(name: str) -> None:
    """[CP5-T41] Apigee built-in categories a2m does not map are refused in conditions and templates."""
    assert_untranslatable(f'{name} = "secure"', name)
    result = translate_template(f"value {{{name}}}")
    assert result.ok is False and result.dw is None, result.dw
    assert name in (result.reason or ""), result.reason


@pytest.mark.parametrize(
    ("text", "named"),
    [
        ('request.header.X-Custom = ""', "request.header.X-Custom"),
        ('request.header.X-Custom != ""', "request.header.X-Custom"),
        ('request.queryparam.q != ""', "request.queryparam.q"),
        ('request.queryparam.q = ""', "request.queryparam.q"),
        ('customVar = ""', "customVar"),
        ('customVar := ""', "customVar"),
        ("proxy.pathsuffix = null", "proxy.pathsuffix"),
        ("proxy.pathsuffix != null", "proxy.pathsuffix"),
        ("request.verb = null", "request.verb"),
        ("request.verb != null", "request.verb"),
    ],
)
def test_CP5_T42_empty_text_and_null_comparisons_that_differ_from_apigee_are_cant_translate(
    text: str, named: str
) -> None:
    """[CP5-T42] "" against a variable that can be missing, and null against one that never is, are refused."""
    assert_untranslatable(text, named)


def test_CP5_T43_a_flow_condition_ignores_the_steps_of_a_sibling_flow_tried_before_it(tmp_path: Path) -> None:
    """[CP5-T43] A conditional Flow's condition is not refused because of a sibling Flow's steps."""
    proxy = r1_proxy(
        flows=r1_flow("flow-a", 'request.verb = "POST"', step("AM-Set-Flag"))
        + r1_flow("flow-b", 'request.header.X-Flag = "no"', step("AM-B")),
        post_request=step("AM-After", 'request.header.X-Flag = "yes"'),
    )
    policies = {
        "AM-Set-Flag": assign_headers("AM-Set-Flag", [("X-Flag", "yes")]),
        "AM-B": assign_headers("AM-B", [("X-B", "1")]),
        "AM-After": assign_headers("AM-After", [("X-After", "1")]),
    }
    _, result = r1_generate(tmp_path, proxy, policies)

    flow_b = condition_record(result, "flow-b")
    assert flow_b.ok is True, flow_b.reason
    assert collapse(flow_b.dw or "") == f"({hdr('x-flag')} == \"no\")", flow_b.dw
    after = condition_record(result, "AM-After")
    assert after.ok is False and "AM-Set-Flag" in (after.reason or ""), after.reason


def test_CP5_T44_a_target_url_with_a_variable_becomes_the_request_url(tmp_path: Path) -> None:
    """[CP5-T44] A target URL with a {variable} becomes the http:request url, with the request path appended."""
    proxy = r1_proxy(pre_request=step("AM-Backend"))
    backend = policy(
        "AssignMessage",
        "AM-Backend",
        "    <AssignVariable>\n        <Name>a2mtest.backend</Name>\n        <Value>backend.example.test</Value>\n"
        "    </AssignVariable>\n",
    )
    project, result = r1_generate(
        tmp_path, proxy, {"AM-Backend": backend}, target_url="http://{a2mtest.backend}/shop"
    )
    requests = r1_http_requests(project)
    assert len(requests) == 1
    url = collapse(requests[0].get("url") or "")
    assert requests[0].get("path") is None, requests[0].attrib
    assert url.startswith("#[") and "vars['a2mtest.backend']" in url and "attributes.maskedRequestPath" in url, url
    assert '"http://"' in url and '"/shop"' in url, url
    assert not [u for u in result.unsupported if "URL" in u.name], result.unsupported


def test_CP5_T44_a_target_url_reading_a_header_changed_earlier_answers_with_the_fault(tmp_path: Path) -> None:
    """[CP5-T44] A target URL reading a header an earlier step changes is refused; the route answers the fault."""
    proxy = r1_proxy(pre_request=step("AM-Set-Backend"))
    project, result = r1_generate(
        tmp_path,
        proxy,
        {"AM-Set-Backend": assign_headers("AM-Set-Backend", [("X-Backend", "elsewhere.test")])},
        target_url="https://{request.header.X-Backend}/api",
    )
    assert r1_http_requests(project) == []
    payloads = [n.get("value") or "" for n in project.nodes() if is_element(n) and n.tag == tag(CORE, "set-payload")]
    assert any("target of this route was not migrated" in p for p in payloads), payloads
    reasons = [u.reason for u in result.unsupported if "URL" in u.name]
    assert any("AM-Set-Backend" in r for r in reasons), result.unsupported


# ---------------------------------------------------------------- CP5 adversarial round 2
# One reading path: a policy's variable reference (Quota/SpikeArrest Identifier ref, VerifyAPIKey APIKey ref,
# BasicAuthentication User/Password ref and Source, AssignVariable Ref, ExtractVariables Header/QueryParam)
# reads a request part exactly as a condition does (first comma-separated header value) and is refused, never
# read stale, when an earlier step may have changed it. Inside one AssignMessage, a value read by a later
# operation sees what the earlier operations changed. A Flows section with only a catch-all Flow deploys.

R2_QUOTA = '    <Allow count="5"/>\n    <Interval>1</Interval>\n    <TimeUnit>minute</TimeUnit>\n'


def r2_quota(name: str, ref: str) -> str:
    return policy("Quota", name, R2_QUOTA + f'    <Identifier ref="{ref}"/>\n')


def r2_policies(prefix: str = "") -> dict[str, str]:
    """Policies that read request parts through a ref, named ``prefix`` + their usual name."""
    return {
        f"{prefix}Q-Limit": r2_quota(f"{prefix}Q-Limit", "request.header.X-Foo"),
        f"{prefix}SA-Limit": policy(
            "SpikeArrest", f"{prefix}SA-Limit", '    <Identifier ref="request.queryparam.who"/>\n    <Rate>30pm</Rate>\n'
        ),
        f"{prefix}VK": policy("VerifyAPIKey", f"{prefix}VK", '    <APIKey ref="request.header.X-Key"/>\n'),
        f"{prefix}BA-Encode": policy(
            "BasicAuthentication",
            f"{prefix}BA-Encode",
            "    <Operation>Encode</Operation>\n    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n"
            '    <User ref="request.header.X-User"/>\n    <Password ref="creds.pass"/>\n'
            '    <AssignTo createNew="false">request.header.X-Basic-Out</AssignTo>\n',
        ),
        f"{prefix}BA-Decode": policy(
            "BasicAuthentication",
            f"{prefix}BA-Decode",
            "    <Operation>Decode</Operation>\n    <IgnoreUnresolvedVariables>false</IgnoreUnresolvedVariables>\n"
            '    <User ref="auth.user"/>\n    <Password ref="auth.pass"/>\n'
            "    <Source>request.header.X-Basic</Source>\n",
        ),
        f"{prefix}AM-Ref": policy(
            "AssignMessage",
            f"{prefix}AM-Ref",
            "    <AssignVariable>\n        <Name>copied.ref</Name>\n        <Ref>request.header.X-Ref</Ref>\n"
            "    </AssignVariable>\n",
        ),
        f"{prefix}EV-Hdr": policy(
            "ExtractVariables",
            f"{prefix}EV-Hdr",
            "    <Source>request</Source>\n    <VariablePrefix>ev</VariablePrefix>\n"
            '    <Header name="X-Ext">\n        <Pattern>{tok}</Pattern>\n    </Header>\n'
            '    <QueryParam name="who">\n        <Pattern>{w}</Pattern>\n    </QueryParam>\n'
            "    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n",
        ),
    }


R2_CHANGED = ("X-Foo", "X-Key", "X-User", "X-Basic", "X-Ref", "X-Ext")


def r2_changer(name: str, value: str) -> str:
    """An AssignMessage that sets every header the r2 policies read, and the query parameter who, to ``value``."""
    rows = "".join(f'            <Header name="{h}">{value}</Header>\n' for h in R2_CHANGED)
    return policy(
        "AssignMessage",
        name,
        f"    <Set>\n        <Headers>\n{rows}        </Headers>\n"
        f'        <QueryParams>\n            <QueryParam name="who">{value}</QueryParam>\n        </QueryParams>\n'
        "    </Set>\n    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n"
        '    <AssignTo createNew="false" transport="http" type="request"/>\n',
    )


def r2_refusals(result: Any, name: str) -> list[str]:
    """Why policy step ``name`` did not read a request part: its skip reason or its unsupported options."""
    record = policy_record(result, name)
    found = [str(o.reason) for o in record.unsupported_options]
    if str(record.method) == "skipped":
        found.append(str(record.reason))
    return found


@pytest.mark.parametrize("value", ["{client.ip}", "fixed"], ids=["untranslatable-change", "translated-change"])
def test_CP5_T49_policy_refs_reading_a_request_part_changed_earlier_are_refused(tmp_path: Path, value: str) -> None:
    """[CP5-T49] Every policy ref reading a header or query parameter an earlier step changes is refused."""
    readers = r2_policies()
    proxy = r1_proxy(pre_request=step("AM-Set") + "".join(step(n) for n in readers))
    project, result = r1_generate(tmp_path, proxy, {"AM-Set": r2_changer("AM-Set", value), **readers})

    if value == "{client.ip}":
        # The finding's case: the change itself is dropped, so the generated app never sets X-Foo.
        assert str(policy_record(result, "AM-Set").method) == "skipped", policy_record(result, "AM-Set")
    for name in ("Q-Limit", "SA-Limit", "VK", "BA-Encode", "BA-Decode"):
        record = policy_record(result, name)
        assert str(record.method) == "skipped", (name, record)
        assert "AM-Set" in str(record.reason), (name, record.reason)
        assert project.labelled(name) == [], f"{name} was generated with a stale read"
    for name, setting in (("AM-Ref", "X-Ref"), ("EV-Hdr", "X-Ext"), ("EV-Hdr", "who")):
        reasons = r2_refusals(result, name)
        assert any("AM-Set" in r and setting in r for r in reasons), (name, reasons)
    for name in ("AM-Ref", "EV-Hdr"):
        written = [v for e in project.labelled(name) for v in values(e)]
        assert not any("x-ref" in v.lower() or "x-ext" in v.lower() or "'who'" in v for v in written), written


def test_CP5_T49_a_ref_to_a_header_no_earlier_step_changes_still_translates(tmp_path: Path) -> None:
    """[CP5-T49] A Quota counting per a header no earlier step touches still counts per that header."""
    proxy = r1_proxy(pre_request=step("AM-Set") + step("Q-Other"))
    policies = {
        "AM-Set": r2_changer("AM-Set", "{client.ip}"),
        "Q-Other": r2_quota("Q-Other", "request.header.X-Other"),
    }
    project, result = r1_generate(tmp_path, proxy, policies)
    record = policy_record(result, "Q-Other")
    assert str(record.method) == "template", record
    assert any(hdr("x-other") in v for v in values(project.step("Q-Other"))), values(project.step("Q-Other"))


def test_CP5_T50_policy_refs_read_a_header_the_way_conditions_do(tmp_path: Path) -> None:
    """[CP5-T50] A policy ref reads request.header.NAME as conditions do: the first comma-separated value."""
    readers = r2_policies()
    proxy = r1_proxy(
        pre_request="".join(step(n) for n in readers) + step("AM-Guarded", 'request.header.X-Key = "k"')
    )
    project, result = r1_generate(
        tmp_path, proxy, {**readers, "AM-Guarded": assign_headers("AM-Guarded", [("X-G", "1")])}
    )
    condition = condition_record(result, "AM-Guarded")
    assert condition.ok is True and hdr("x-key") in (condition.dw or ""), condition
    for name, header in (
        ("Q-Limit", "x-foo"),
        ("VK", "x-key"),
        ("BA-Encode", "x-user"),
        ("BA-Decode", "x-basic"),
        ("AM-Ref", "x-ref"),
        ("EV-Hdr", "x-ext"),
    ):
        assert str(policy_record(result, name).method) == "template", (name, policy_record(result, name))
        written = [v for e in project.labelled(name) for v in values(e)]
        assert any(hdr(header) in v for v in written), (name, written)
        assert not any("a2mRequestHeaders ==" in v for v in written), (name, written)


def r2_assign(name: str, body: str) -> str:
    return policy(
        "AssignMessage",
        name,
        body + "    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n"
        '    <AssignTo createNew="false" transport="http" type="request"/>\n',
    )


def r2_option(result: Any, name: str, original: str) -> list[str]:
    return [
        str(o.reason) for o in policy_record(result, name).unsupported_options if o.original == original
    ]


def test_CP5_T51_a_value_read_after_an_earlier_operation_of_the_same_policy_is_refused(tmp_path: Path) -> None:
    """[CP5-T51] Inside one AssignMessage, Remove then Set {request.header.X} (and similar) is refused."""
    remove_then_set = r2_assign(
        "AM-Remove-Set",
        '    <Remove>\n        <Headers>\n            <Header name="X-Token"/>\n        </Headers>\n    </Remove>\n'
        '    <Set>\n        <Headers>\n            <Header name="X-Echo">{request.header.X-Token}</Header>\n'
        "        </Headers>\n    </Set>\n",
    )
    same_block = r2_assign(
        "AM-Same-Set",
        '    <Set>\n        <Headers>\n            <Header name="X-B">copy {request.header.X-A}</Header>\n'
        '            <Header name="X-A">new</Header>\n'
        '            <Header name="X-Fwd">{request.header.X-Fwd}, more</Header>\n'
        "        </Headers>\n        <Verb>POST</Verb>\n    </Set>\n",
    )
    verb_then_header = r2_assign(
        "AM-Verb-Add",
        '    <Set>\n        <Headers>\n            <Header name="X-Verb">{request.verb}</Header>\n'
        "        </Headers>\n        <Verb>PUT</Verb>\n    </Set>\n",
    )
    add_then_set_query = r2_assign(
        "AM-Query",
        '    <Add>\n        <QueryParams>\n            <QueryParam name="page">2</QueryParam>\n'
        "        </QueryParams>\n    </Add>\n"
        '    <Set>\n        <Headers>\n            <Header name="X-Page">{request.queryparam.page}</Header>\n'
        "        </Headers>\n    </Set>\n",
    )
    copy_then_set = r2_assign(
        "AM-Copy",
        '    <Copy source="request">\n        <Headers>\n            <Header name="X-Copied"/>\n'
        "        </Headers>\n    </Copy>\n"
        '    <Set>\n        <Headers>\n            <Header name="X-From-Copy">{request.header.X-Copied}</Header>\n'
        "        </Headers>\n    </Set>\n",
    )
    assign_after_remove = r2_assign(
        "AM-Var",
        '    <Remove>\n        <Headers>\n            <Header name="X-Token"/>\n        </Headers>\n    </Remove>\n'
        "    <AssignVariable>\n        <Name>seen.token</Name>\n        <Ref>request.header.X-Token</Ref>\n"
        "    </AssignVariable>\n"
        "    <AssignVariable>\n        <Name>seen.other</Name>\n        <Ref>request.header.X-Untouched</Ref>\n"
        "    </AssignVariable>\n",
    )
    names = ("AM-Remove-Set", "AM-Same-Set", "AM-Verb-Add", "AM-Query", "AM-Copy", "AM-Var")
    proxy = r1_proxy(pre_request="".join(step(n) for n in names))
    policies = dict(
        zip(
            names,
            (remove_then_set, same_block, verb_then_header, add_then_set_query, copy_then_set, assign_after_remove),
            strict=True,
        )
    )
    project, result = r1_generate(tmp_path, proxy, policies)

    for name, original, header in (
        ("AM-Remove-Set", "{request.header.X-Token}", "x-echo"),
        ("AM-Same-Set", "copy {request.header.X-A}", "x-b"),
        ("AM-Verb-Add", "{request.verb}", "x-verb"),
        ("AM-Query", "{request.queryparam.page}", "x-page"),
        ("AM-Copy", "{request.header.X-Copied}", "x-from-copy"),
    ):
        reasons = r2_option(result, name, original)
        assert any(name in r for r in reasons), (name, policy_record(result, name).unsupported_options)
        written = [v for e in project.labelled(name) for v in values(e)]
        assert not any(re.search(r"""['"]""" + header + r"""['"]\s*:""", v) for v in written), (name, written)

    # An entry reading its own header reads it before it is written: still translated.
    assert has_setting(project.step("AM-Same-Set"), f"({hdr('x-fwd')} default \"\") ++ \", more\"", "x-fwd")
    var_reasons = [str(o.reason) for o in policy_record(result, "AM-Var").unsupported_options]
    assert any("seen.token" in r and "AM-Var" in r for r in var_reasons), var_reasons
    written = [v for e in project.labelled("AM-Var") for v in values(e)]
    assert any(hdr("x-untouched") in v for v in written), written
    assert not any(hdr("x-token") in v for v in written), written


def test_CP5_T51_changes_inside_one_policy_still_count_for_later_steps(tmp_path: Path) -> None:
    """[CP5-T51] Every change an AssignMessage makes still refuses a later step's read of that request part."""
    changer = r2_assign(
        "AM-All",
        '    <Remove>\n        <Headers>\n            <Header name="X-R"/>\n        </Headers>\n    </Remove>\n'
        '    <Add>\n        <QueryParams>\n            <QueryParam name="q"/>\n        </QueryParams>\n    </Add>\n'
        "    <Set>\n        <Verb>POST</Verb>\n    </Set>\n",
    )
    proxy = r1_proxy(
        pre_request=step("AM-All")
        + step("AM-R", 'request.header.X-R = "1"')
        + step("AM-Q", 'request.queryparam.q = "1"')
        + step("AM-V", 'request.verb = "GET"')
    )
    policies = {"AM-All": changer, **{n: assign_headers(n, [("X-" + n, "1")]) for n in ("AM-R", "AM-Q", "AM-V")}}
    _, result = r1_generate(tmp_path, proxy, policies)
    for name in ("AM-R", "AM-Q", "AM-V"):
        record = condition_record(result, name)
        assert record.ok is False and "AM-All" in (record.reason or ""), (name, record.reason)


def r2_catch_all_target() -> str:
    return TARGET_XML.replace(
        "    <Flows/>\n",
        '    <Flows><Flow name="target-all"><Request>' + step("AM-T") + "</Request><Response/></Flow></Flows>\n",
    )


def test_CP5_T52_an_endpoint_whose_flows_are_only_a_catch_all_runs_them_without_an_empty_choice(
    tmp_path: Path,
) -> None:
    """[CP5-T52] Flows holding only a catch-all Flow give its steps in line, never a choice without a when."""
    proxy = r1_proxy(flows='<Flow name="all"><Request>' + step("AM-P") + "</Request><Response/></Flow>")
    root = write_bundle(
        tmp_path / "bundles",
        "r2-catch-all",
        proxy,
        {n: assign_headers(n, [("X-" + n, "1")]) for n in ("AM-P", "AM-T")},
    )
    (root / "apiproxy" / "targets" / "default.xml").write_text(r2_catch_all_target(), encoding="utf-8")
    dest = tmp_path / "out" / "r2-catch-all" / "mule-app"
    generate(root, dest)
    project = Project(dest)

    choices = [n for n in project.nodes() if is_element(n) and n.tag == tag(CORE, "choice")]
    for node in choices:
        assert any(is_element(c) and c.tag == tag(CORE, "when") for c in node), ET.tostring(node, encoding="unicode")
    for name in ("AM-P", "AM-T"):
        element = project.step(name)
        assert not any(a.tag == tag(CORE, "choice") for a in project.ancestors(element)), name
    assert project.order({"AM-P", "AM-T"}) == ["AM-P", "AM-T"]


# ---------------------------------------------------------------- CP5 adversarial round 3
# Message templates with a default ({var:default}) translate exactly, and any other {...} that starts like a
# reference but is not one a2m understands is can't translate, never literal text. Matches and MatchesPath honour
# Apigee's '%' escape (%* a literal '*', %% a literal '%') and refuse any other '%'. A read of the proxy's own flow
# variable is refused when an earlier step on the path may write it in Apigee and the generated app does not.


def test_CP5_T55_a_template_default_is_translated_exactly() -> None:
    """[CP5-T55] {var:default} reads the variable, and the default when it is missing or null."""
    for text, expected in (
        ("{request.header.X-Correlation-ID:unknown}", f'({hdr("x-correlation-id")} default "unknown")'),
        ("id={ext.id:none}!", '"id=" ++ (vars[\'ext.id\'] default "none") ++ "!"'),
        ("{request.queryparam.page:1}", "(attributes.queryParams['page'] default \"1\")"),
        ("{ext.id}", "(vars['ext.id'] default \"\")"),
    ):
        result = translate_template(text)
        assert result.ok is True, (text, result.reason)
        assert collapse(result.dw or "") == collapse(expected), (text, result.dw)
        assert result.original == text


@pytest.mark.parametrize(
    "text",
    [
        "{a: b}",
        "{a:b:c}",
        "{ request.verb }",
        "{request.header.X[0]}",
        "{a:{b}}",
        "x {a:b }",
        "{toUpperCase(request.verb)}",
    ],
)
def test_CP5_T55_a_brace_form_that_is_not_understood_is_cant_translate_not_literal(text: str) -> None:
    """[CP5-T55] A {...} that starts like a reference but does not parse fully is refused, never copied."""
    result = translate_template(text)
    assert result.ok is False, (text, result.dw)
    assert result.dw is None and result.original == text
    assert isinstance(result.reason, str) and result.reason.strip(), text


@pytest.mark.parametrize("text", ['{"id": 1}', '{ "a": {"b": 2} }', "{}", "price {", "[{1}]"])
def test_CP5_T55_json_braces_stay_literal_text(text: str) -> None:
    """[CP5-T55] Braces that do not start like a variable reference (JSON, a lone brace) stay literal."""
    result = translate_template(text)
    assert result.ok is True and result.dw is None, (text, result.dw, result.reason)


def test_CP5_T55_a_default_in_a_policy_value_is_generated_and_an_unknown_form_is_listed(tmp_path: Path) -> None:
    """[CP5-T55] AssignMessage writes {var:default} as DataWeave with the default; an unknown form is listed."""
    am = assign_headers(
        "AM-Corr",
        [("X-Corr", "{request.header.X-Correlation-ID:unknown}"), ("X-Odd", "{ request.verb }")],
    )
    project, result = r1_generate(tmp_path, r1_proxy(pre_request=step("AM-Corr")), {"AM-Corr": am})
    element = project.step("AM-Corr")
    assert has_setting(element, f'{hdr("x-correlation-id")} default "unknown"', "x-corr"), values(element)
    assert r2_option(result, "AM-Corr", "{ request.verb }"), policy_record(result, "AM-Corr").unsupported_options
    written = [v for e in project.labelled("AM-Corr") for v in values(e)]
    assert not any("x-odd" in v.lower() for v in written), written
    assert not any("{request.header.X-Correlation-ID:unknown}" in v for v in written), written


def r3_match(pattern: str, variable: str = "request.header.X-Code", operator: str = "Matches") -> Any:
    return translate_condition(f'{variable} {operator} "{pattern}"')


def test_CP5_T56_matches_honours_the_percent_escape() -> None:
    """[CP5-T56] Matches "abc%*" matches the literal value abc*, and %% is a literal percent sign."""
    for pattern, matching, other in (
        ("abc%*", ["abc*"], ["abcd", "abc%d", "abc", "abc%*"]),
        ("50%%*", ["50%", "50%off"], ["50", "5%", "50off"]),
        ("%**", ["*", "*x"], ["x", "x*"]),
    ):
        result = r3_match(pattern)
        assert result.ok is True, (pattern, result.reason)
        regex = regex_of(result.dw or "", hdr("x-code"))
        for sample in matching:
            assert full(regex, sample), (pattern, regex, sample)
        for sample in other:
            assert not full(regex, sample), (pattern, regex, sample)


def test_CP5_T56_matchespath_honours_the_percent_escape() -> None:
    """[CP5-T56] MatchesPath "/files/%*/**" matches a segment that is a literal '*' only."""
    result = r3_match("/files/%*/**", "proxy.pathsuffix", "MatchesPath")
    assert result.ok is True, result.reason
    regex = regex_of(result.dw or "")
    assert full(regex, "/files/*/a") and full(regex, "/files/*/a/b"), regex
    assert not full(regex, "/files/x/a") and not full(regex, "/files/ab/c"), regex


@pytest.mark.parametrize(
    ("pattern", "operator", "variable"),
    [
        ("a%b", "Matches", "request.header.X-Code"),
        ("abc%", "Matches", "request.header.X-Code"),
        ("%?", "~", "request.header.X-Code"),
        ("/a%/b", "MatchesPath", "proxy.pathsuffix"),
        ("/a/%", "~/", "proxy.pathsuffix"),
    ],
)
def test_CP5_T56_a_percent_escape_that_is_not_certain_is_cant_translate(
    pattern: str, operator: str, variable: str
) -> None:
    """[CP5-T56] Any '%' other than %* and %% in a Matches or MatchesPath pattern is refused."""
    assert_untranslatable(f'{variable} {operator} "{pattern}"')


def r3_assign_variable(name: str, variable: str, inner: str) -> str:
    return policy(
        "AssignMessage",
        name,
        f"    <AssignVariable>\n        <Name>{variable}</Name>\n        {inner}\n    </AssignVariable>\n"
        "    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n",
    )


def r3_readers(variable: str) -> dict[str, str]:
    """Steps that read the flow variable ``variable``: a condition, a template, a ref, a Ref and a User ref."""
    return {
        "RF-Guard": raise_fault("RF-Guard", 403, "Forbidden"),
        "AM-Echo": assign_headers("AM-Echo", [("X-Echo", "{" + variable + "}")]),
        "Q-By": r2_quota("Q-By", variable),
        "AM-Copy-Var": r3_assign_variable("AM-Copy-Var", "copy.of", f"<Ref>{variable}</Ref>"),
        "BA-Enc": policy(
            "BasicAuthentication",
            "BA-Enc",
            "    <Operation>Encode</Operation>\n    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n"
            f'    <User ref="{variable}"/>\n    <Password ref="creds.pass"/>\n'
            '    <AssignTo createNew="false">request.header.X-Basic-Out</AssignTo>\n',
        ),
    }


def r3_reader_steps(variable: str) -> str:
    return (
        step("RF-Guard", f"{variable} != null")
        + step("AM-Echo")
        + step("Q-By")
        + step("AM-Copy-Var")
        + step("BA-Enc")
    )


def r3_assert_refused(project: Project, result: Any, variable: str, writer: str) -> None:
    condition = condition_record(result, "RF-Guard")
    assert condition.ok is False, condition
    assert writer in (condition.reason or "") and variable in (condition.reason or ""), condition.reason
    when = r1_when_of(project, "RF-Guard")
    assert collapse(when.get("expression") or "") == "#[false]", when.attrib
    for name in ("Q-By", "BA-Enc"):
        record = policy_record(result, name)
        assert str(record.method) == "skipped" and writer in str(record.reason), (name, record)
        assert project.labelled(name) == [], f"{name} was generated with a read of a dropped write"
    for name, original in (("AM-Echo", "{" + variable + "}"), ("AM-Copy-Var", None)):
        reasons = r2_refusals(result, name)
        assert any(writer in r and variable in r for r in reasons), (name, reasons)
        written = [v for e in project.labelled(name) for v in values(e)]
        assert not any(f"vars['{variable}']" in v for v in written), (name, written, original)


def test_CP5_T57_a_read_after_a_refused_assignvariable_is_cant_translate(tmp_path: Path) -> None:
    """[CP5-T57] The reviewer's case: AM-Set's AssignVariable risk.score is refused, so `risk.score != null` (and
    every other read of risk.score) is can't translate, naming AM-Set, never read as a missing value."""
    writer = r3_assign_variable("AM-Set", "risk.score", "<Value>{system.timestamp}</Value>")
    proxy = r1_proxy(pre_request=step("AM-Set") + r3_reader_steps("risk.score"))
    project, result = r1_generate(tmp_path, proxy, {"AM-Set": writer, **r3_readers("risk.score")})
    assert str(policy_record(result, "AM-Set").method) == "skipped", policy_record(result, "AM-Set")
    r3_assert_refused(project, result, "risk.score", "AM-Set")


def test_CP5_T57_a_read_after_a_faithful_write_still_translates(tmp_path: Path) -> None:
    """[CP5-T57] A variable the generated app writes exactly (and one nothing writes) is still read."""
    writer = r3_assign_variable("AM-Set", "risk.score", "<Value>high</Value>")
    proxy = r1_proxy(pre_request=step("AM-Set") + r3_reader_steps("risk.score") + step("AM-Other", "other.v = \"1\""))
    policies = {"AM-Set": writer, **r3_readers("risk.score"), "AM-Other": assign_headers("AM-Other", [("X-O", "1")])}
    project, result = r1_generate(tmp_path, proxy, policies)
    condition = condition_record(result, "RF-Guard")
    assert condition.ok is True and collapse(condition.dw or "") == "(vars['risk.score'] != null)", condition
    assert condition_record(result, "AM-Other").ok is True
    for name in ("Q-By", "BA-Enc", "AM-Echo", "AM-Copy-Var"):
        assert str(policy_record(result, name).method) == "template", (name, policy_record(result, name))
        written = [v for e in project.labelled(name) for v in values(e)]
        assert any("vars['risk.score']" in v for v in written), (name, written)


@pytest.mark.parametrize(
    ("writer_step", "writer_xml", "variable"),
    [
        (
            step("EV-Var"),
            policy(
                "ExtractVariables",
                "EV-Var",
                "    <Source>myMessage</Source>\n    <VariablePrefix>ev</VariablePrefix>\n"
                '    <Header name="X-Tok">\n        <Pattern>{tok}</Pattern>\n    </Header>\n',
            ),
            "ev.tok",
        ),
        (
            step("EV-Two"),
            policy(
                "ExtractVariables",
                "EV-Two",
                "    <Source>request</Source>\n"
                '    <Header name="X-Tok">\n        <Pattern>Bearer {tok}</Pattern>\n'
                "        <Pattern>Token {tok}</Pattern>\n    </Header>\n",
            ),
            "tok",
        ),
        (
            step("BA-Dec"),
            policy(
                "BasicAuthentication",
                "BA-Dec",
                "    <Operation>Decode</Operation>\n    <IgnoreUnresolvedVariables>false</IgnoreUnresolvedVariables>\n"
                '    <User ref="auth.user"/>\n    <Password ref="auth.pass"/>\n    <Source>client.ip</Source>\n',
            ),
            "auth.user",
        ),
        (
            step("AM-Cond", 'client.ip = "10.0.0.1"'),
            r3_assign_variable("AM-Cond", "flag.on", "<Value>yes</Value>"),
            "flag.on",
        ),
        (
            step("JS-Any"),
            policy("Javascript", "JS-Any", "    <ResourceURL>jsc://any.js</ResourceURL>\n"),
            "anything.at.all",
        ),
        (
            step("AM-Msg"),
            policy(
                "AssignMessage",
                "AM-Msg",
                '    <AssignTo createNew="true" type="request">sideMsg</AssignTo>\n'
                '    <Set>\n        <Headers>\n            <Header name="X-Side">1</Header>\n'
                "        </Headers>\n    </Set>\n    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n",
            ),
            "sideMsg.header.X-Side",
        ),
    ],
    ids=["extract-refused", "extract-second-pattern", "decode-skipped", "conditional", "javascript", "named-message"],
)
def test_CP5_T57_every_kind_of_dropped_writer_refuses_a_later_read(
    tmp_path: Path, writer_step: str, writer_xml: str, variable: str
) -> None:
    """[CP5-T57] A refused ExtractVariables, one with a second pattern, a skipped Decode, a writer under a condition
    a2m can't translate, a policy type a2m does not translate and a message kept in a variable all refuse a later
    read of what they write, naming the writer."""
    writer = re.search(r"<Name>([^<]+)</Name>", writer_step)
    assert writer is not None
    proxy = r1_proxy(pre_request=writer_step + r3_reader_steps(variable))
    root = write_bundle(tmp_path / "bundles", "r3-api", proxy, {writer.group(1): writer_xml, **r3_readers(variable)})
    dest = tmp_path / "out" / "r3-api" / "mule-app"
    result = generate(root, dest)
    r3_assert_refused(Project(dest), result, variable, writer.group(1))


def test_CP5_T57_a_later_assignvariable_of_the_same_policy_does_not_read_a_dropped_one(tmp_path: Path) -> None:
    """[CP5-T57] Inside one AssignMessage, AssignVariable b Ref a, after AssignVariable a was refused, is listed."""
    am = policy(
        "AssignMessage",
        "AM-Two",
        "    <AssignVariable>\n        <Name>first.v</Name>\n        <Value>{system.timestamp}</Value>\n"
        "    </AssignVariable>\n"
        "    <AssignVariable>\n        <Name>second.v</Name>\n        <Ref>first.v</Ref>\n    </AssignVariable>\n"
        "    <AssignVariable>\n        <Name>third.v</Name>\n        <Value>ok</Value>\n    </AssignVariable>\n"
        "    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n",
    )
    project, result = r1_generate(tmp_path, r1_proxy(pre_request=step("AM-Two")), {"AM-Two": am})
    reasons = r2_refusals(result, "AM-Two")
    assert any("second.v" in r and "first.v" in r for r in reasons), reasons
    written = [v for e in project.labelled("AM-Two") for v in values(e)]
    assert not any("vars['first.v']" in v for v in written), written
    assert any(e.get("variableName") == "third.v" for e in project.step("AM-Two").iter()), written


def r3_shared(parent: Path, name: str, policies: dict[str, str]) -> Path:
    root = parent / name
    files = {
        f"sharedflowbundle/{name}.xml": XML_HEAD + f'<SharedFlowBundle revision="1" name="{name}">'
        f"<Policies>{''.join(f'<Policy>{p}</Policy>' for p in policies)}</Policies>"
        "<SharedFlows><SharedFlow>default</SharedFlow></SharedFlows></SharedFlowBundle>\n",
        "sharedflowbundle/sharedflows/default.xml": XML_HEAD
        + '<SharedFlow name="default">'
        + "".join(step(p) for p in policies)
        + "</SharedFlow>\n",
        **{f"sharedflowbundle/policies/{p}.xml": xml for p, xml in policies.items()},
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def r3_callout(name: str, shared: str) -> str:
    return policy("FlowCallout", name, f"    <SharedFlowBundle>{shared}</SharedFlowBundle>\n")


def test_CP5_T57_a_dropped_write_inside_a_shared_flow_refuses_a_read_after_the_callout(tmp_path: Path) -> None:
    """[CP5-T57] A shared flow whose AssignVariable is refused refuses a read after the FlowCallout, naming the
    shared flow's step; a read inside a shared flow of a variable a proxy step drops is refused too."""
    shared = r3_shared(
        tmp_path / "shared",
        "sf-set",
        {"SF-Set": r3_assign_variable("SF-Set", "risk.score", "<Value>{system.timestamp}</Value>")},
    )
    reader_shared = r3_shared(
        tmp_path / "shared",
        "sf-read",
        {"SF-Read": assign_headers("SF-Read", [("X-Lvl", "{lvl.v}")])},
    )
    proxy = r1_proxy(
        pre_request=step("FC-Set") + r3_reader_steps("risk.score") + step("AM-Lvl") + step("FC-Read")
    )
    policies = {
        "FC-Set": r3_callout("FC-Set", "sf-set"),
        **r3_readers("risk.score"),
        "AM-Lvl": r3_assign_variable("AM-Lvl", "lvl.v", "<Value>{system.timestamp}</Value>"),
        "FC-Read": r3_callout("FC-Read", "sf-read"),
    }
    root = write_bundle(tmp_path / "bundles", "r3-sf", proxy, policies)
    dest = tmp_path / "out" / "r3-sf" / "mule-app"
    result = generate(root, dest, (shared, reader_shared))
    project = Project(dest)
    r3_assert_refused(project, result, "risk.score", "SF-Set")
    reasons = r2_refusals(result, "SF-Read")
    assert any("AM-Lvl" in r and "lvl.v" in r for r in reasons), reasons


# ---------------------------------------------------------------- CP5 adversarial round 3: built-in writers


def r3_verify_key(name: str, ref: str) -> str:
    return policy("VerifyAPIKey", name, f'    <APIKey ref="{ref}"/>\n')


def r3_oauth(name: str, operation: str) -> str:
    return policy("OAuthV2", name, f"    <Operation>{operation}</Operation>\n")


def test_CP5_T61_a_translated_verifyapikey_writes_client_id_as_the_key_so_a_later_read_is_kept(
    tmp_path: Path,
) -> None:
    """[CP5-T61] Apigee's VerifyAPIKey sets client_id to the key the call presented; the generated step writes it
    from the same key, so a later Quota keyed on client_id and a condition on it are translated and read it."""
    proxy = r1_proxy(pre_request=step("VK") + r3_reader_steps("client_id"))
    policies = {"VK": r3_verify_key("VK", "request.header.x-api-key"), **r3_readers("client_id")}
    project, result = r1_generate(tmp_path, proxy, policies)
    assert str(policy_record(result, "VK").method) == "template", policy_record(result, "VK")
    all_writes = [n for n in project.nodes() if is_element(n) and n.get("variableName") == "client_id"]
    assert all_writes, "the VerifyAPIKey step does not write client_id"
    assert any("a2mApiKey" in (n.get("value") or "") for n in all_writes), [n.attrib for n in all_writes]
    condition = condition_record(result, "RF-Guard")
    assert condition.ok is True, condition
    record = policy_record(result, "Q-By")
    assert str(record.method) == "template", record
    assert any("client_id" in v for e in project.labelled("Q-By") for v in values(e)), "Q-By does not read client_id"


def test_CP5_T61_a_skipped_verifyapikey_refuses_a_later_read_of_client_id(tmp_path: Path) -> None:
    """[CP5-T61] A VerifyAPIKey a2m skips never writes client_id, so a later read of it is refused, naming it."""
    proxy = r1_proxy(pre_request=step("VK-Form") + r3_reader_steps("client_id"))
    policies = {"VK-Form": r3_verify_key("VK-Form", "request.formparam.key"), **r3_readers("client_id")}
    project, result = r1_generate(tmp_path, proxy, policies)
    assert str(policy_record(result, "VK-Form").method) == "skipped", policy_record(result, "VK-Form")
    r3_assert_refused(project, result, "client_id", "VK-Form")


@pytest.mark.parametrize("variable", ["client_id", "access_token", "scope"])
def test_CP5_T61_oauthv2_verifyaccesstoken_writes_refuse_a_later_read(tmp_path: Path, variable: str) -> None:
    """[CP5-T61] OAuthV2 VerifyAccessToken (never generated) writes client_id, access_token and scope in Apigee,
    so a later read of them is refused, naming the OAuthV2 step; a variable it does not write is still read."""
    proxy = r1_proxy(pre_request=step("OA-Verify") + r3_reader_steps(variable))
    policies = {"OA-Verify": r3_oauth("OA-Verify", "VerifyAccessToken"), **r3_readers(variable)}
    project, result = r1_generate(tmp_path, proxy, policies)
    r3_assert_refused(project, result, variable, "OA-Verify")


def test_CP5_T61_oauthv2_verifyaccesstoken_does_not_refuse_a_variable_it_does_not_write(tmp_path: Path) -> None:
    """[CP5-T61] A variable OAuthV2 VerifyAccessToken does not write is still read after it."""
    proxy = r1_proxy(pre_request=step("OA-Verify") + step("Q-By"))
    policies = {"OA-Verify": r3_oauth("OA-Verify", "VerifyAccessToken"), "Q-By": r2_quota("Q-By", "tier.name")}
    _, result = r1_generate(tmp_path, proxy, policies)
    assert str(policy_record(result, "Q-By").method) == "template", policy_record(result, "Q-By")


def test_CP5_T61_other_oauthv2_operations_may_write_any_variable(tmp_path: Path) -> None:
    """[CP5-T61] An OAuthV2 operation other than VerifyAccessToken may write any variable: a later read is refused."""
    proxy = r1_proxy(pre_request=step("OA-Gen") + r3_reader_steps("tier.name"))
    policies = {"OA-Gen": r3_oauth("OA-Gen", "GenerateAccessToken"), **r3_readers("tier.name")}
    project, result = r1_generate(tmp_path, proxy, policies)
    r3_assert_refused(project, result, "tier.name", "OA-Gen")


# ---------------------------------------------------------------- CP5 adversarial round 4: one writer rule
# Anything a2m does not generate to run exactly as in Apigee is a possible writer: a step inside a Flow or
# RouteRule whose condition can't be translated (a #[false] branch), a step under a condition a2m can't
# translate, and a policy a2m has no write model for (JavaScript, ServiceCallout, ...: anything). A later read
# of what it may write is can't translate, naming the step.

R4_DEAD = 'client.ip = "10.0.0.1"'  # client.ip has no mapping in a2m, so this condition can't be translated


def r4_set(name: str, variable: str, value: str = "high") -> str:
    return r3_assign_variable(name, variable, f"<Value>{value}</Value>")


def r4_post_response(proxy_xml: str, steps: str) -> str:
    """``proxy_xml`` (from r1_proxy) with ``steps`` as its PostFlow response steps."""
    old = "        <Response/>\n    </PostFlow>"
    assert proxy_xml.count(old) == 1
    return proxy_xml.replace(old, f"        <Response>{steps}</Response>\n    </PostFlow>")


def r4_response_readers(variable: str) -> tuple[str, dict[str, str]]:
    return step("RF-Guard", f"{variable} != null") + step("Q-By"), {
        "RF-Guard": raise_fault("RF-Guard", 403, "Forbidden"),
        "Q-By": r2_quota("Q-By", variable),
    }


def r4_assert_condition_refused(result: Any, name: str, writer: str, needle: str) -> None:
    record = condition_record(result, name)
    assert record.ok is False, record
    assert writer in (record.reason or "") and needle in (record.reason or ""), record.reason


def r4_assert_skipped_naming(result: Any, name: str, writer: str) -> None:
    record = policy_record(result, name)
    assert str(record.method) == "skipped" and writer in str(record.reason), (name, record)


def test_CP5_T62_a_write_inside_a_flow_whose_condition_cant_be_translated_refuses_a_later_read(
    tmp_path: Path,
) -> None:
    """[CP5-T62] The reviewers' case: Flow risky (condition client.ip = ..., a #[false] branch) sets risk.score
    exactly; a PostFlow condition, template, ref, Ref and User ref reading risk.score are refused, naming the
    step and the Flow, never read as a missing value."""
    proxy = r1_proxy(flows=r1_flow("risky", R4_DEAD, step("AM-Set")), post_request=r3_reader_steps("risk.score"))
    project, result = r1_generate(tmp_path, proxy, {"AM-Set": r4_set("AM-Set", "risk.score"), **r3_readers("risk.score")})
    assert condition_record(result, "risky").ok is False
    r3_assert_refused(project, result, "risk.score", "AM-Set")
    assert "Flow risky" in (condition_record(result, "RF-Guard").reason or "")


def test_CP5_T62_a_write_inside_a_flow_whose_condition_translates_is_still_read(tmp_path: Path) -> None:
    """[CP5-T62] The same Flow with a condition a2m translates runs its write exactly: the later reads are kept."""
    proxy = r1_proxy(flows=r1_flow("gets", GET, step("AM-Set")), post_request=r3_reader_steps("risk.score"))
    _, result = r1_generate(tmp_path, proxy, {"AM-Set": r4_set("AM-Set", "risk.score"), **r3_readers("risk.score")})
    assert condition_record(result, "RF-Guard").ok is True
    for name in ("Q-By", "BA-Enc", "AM-Echo", "AM-Copy-Var"):
        assert str(policy_record(result, name).method) == "template", (name, policy_record(result, name))


def test_CP5_T62_a_response_step_of_a_flow_whose_condition_cant_be_translated_is_a_dropped_write(
    tmp_path: Path,
) -> None:
    """[CP5-T62] The response steps of a #[false] Flow never run either: a PostFlow response read is refused."""
    flow = (
        f'<Flow name="risky"><Request/><Response>{step("AM-Set")}</Response>'
        f"<Condition>{R4_DEAD}</Condition></Flow>"
    )
    readers, reader_policies = r4_response_readers("risk.score")
    proxy = r4_post_response(r1_proxy(flows=flow), readers)
    _, result = r1_generate(tmp_path, proxy, {"AM-Set": r4_set("AM-Set", "risk.score"), **reader_policies})
    r4_assert_condition_refused(result, "RF-Guard", "AM-Set", "Flow risky")
    r4_assert_skipped_naming(result, "Q-By", "AM-Set")


def test_CP5_T62_a_target_step_behind_a_routerule_whose_condition_cant_be_translated_is_a_dropped_write(
    tmp_path: Path,
) -> None:
    """[CP5-T62] RouteRule guarded (condition client.ip = ..., a #[false] branch) routes to TargetEndpoint
    special, whose PreFlow sets risk.score exactly; a proxy response read of risk.score is refused, naming the
    step and the RouteRule."""
    readers, reader_policies = r4_response_readers("risk.score")
    proxy = r1_proxy(pre_response=readers, route_condition=R4_DEAD)
    route_target = "        <TargetEndpoint>default</TargetEndpoint>\n"
    proxy = proxy.replace(route_target, "        <TargetEndpoint>special</TargetEndpoint>\n", 1)
    root = write_bundle(tmp_path / "bundles", "r4-route", proxy, {"AM-Set": r4_set("AM-Set", "risk.score"), **reader_policies})
    special = TARGET_XML.replace('name="default"', 'name="special"').replace(
        "        <Request/>\n        <Response/>\n    </PreFlow>",
        f"        <Request>{step('AM-Set')}</Request>\n        <Response/>\n    </PreFlow>",
        1,
    )
    (root / "apiproxy" / "targets" / "special.xml").write_text(special, encoding="utf-8")
    manifest_path = root / "apiproxy" / "r4-route.xml"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace(
            "        <TargetEndpoint>default</TargetEndpoint>\n",
            "        <TargetEndpoint>default</TargetEndpoint>\n        <TargetEndpoint>special</TargetEndpoint>\n",
        ),
        encoding="utf-8",
    )
    result = generate(root, tmp_path / "out" / "r4-route" / "mule-app")
    assert condition_record(result, "guarded").ok is False
    r4_assert_condition_refused(result, "RF-Guard", "AM-Set", "RouteRule guarded")
    r4_assert_skipped_naming(result, "Q-By", "AM-Set")


def test_CP5_T62_a_shared_flow_called_inside_a_flow_whose_condition_cant_be_translated_is_a_dropped_write(
    tmp_path: Path,
) -> None:
    """[CP5-T62] A FlowCallout inside a #[false] Flow: the shared flow's exact write is dropped, so a later read
    is refused, naming the shared flow's step."""
    shared = r3_shared(tmp_path / "shared", "sf-set", {"SF-Set": r4_set("SF-Set", "risk.score")})
    proxy = r1_proxy(flows=r1_flow("risky", R4_DEAD, step("FC-Set")), post_request=r3_reader_steps("risk.score"))
    policies = {"FC-Set": r3_callout("FC-Set", "sf-set"), **r3_readers("risk.score")}
    root = write_bundle(tmp_path / "bundles", "r4-sf", proxy, policies)
    dest = tmp_path / "out" / "r4-sf" / "mule-app"
    result = generate(root, dest, (shared,))
    r3_assert_refused(Project(dest), result, "risk.score", "SF-Set")


def test_CP5_T62_a_shared_flow_reading_a_variable_written_only_inside_a_dead_flow_is_refused(tmp_path: Path) -> None:
    """[CP5-T62] A shared flow's sub-flow serves every call, so its reads are checked against every dropped write
    in the proxy: a variable written exactly only inside a #[false] Flow is refused there, naming the writer."""
    reader_shared = r3_shared(tmp_path / "shared", "sf-read", {"SF-Read": assign_headers("SF-Read", [("X-Lvl", "{lvl.v}")])})
    proxy = r1_proxy(flows=r1_flow("risky", R4_DEAD, step("AM-Lvl")), post_request=step("FC-Read"))
    policies = {"AM-Lvl": r4_set("AM-Lvl", "lvl.v"), "FC-Read": r3_callout("FC-Read", "sf-read")}
    root = write_bundle(tmp_path / "bundles", "r4-base", proxy, policies)
    result = generate(root, tmp_path / "out" / "r4-base" / "mule-app", (reader_shared,))
    reasons = r2_refusals(result, "SF-Read")
    assert any("AM-Lvl" in r and "lvl.v" in r for r in reasons), reasons


R4_OPAQUE = {
    "Javascript": "    <ResourceURL>jsc://tier.js</ResourceURL>\n",
    "Python": "    <ResourceURL>py://tier.py</ResourceURL>\n",
    "JavaCallout": "    <ClassName>com.example.Tier</ClassName>\n    <ResourceURL>java://tier.jar</ResourceURL>\n",
    "ServiceCallout": "    <Response>calloutResponse</Response>\n"
    "    <HTTPTargetConnection>\n        <URL>https://tier.example.test</URL>\n    </HTTPTargetConnection>\n",
    "KeyValueMapOperations": '    <Get assignTo="tier.name">\n        <Key><Parameter>t</Parameter></Key>\n    </Get>\n',
    "OAuthV2": "    <Operation>GenerateAccessToken</Operation>\n",
    "SomethingNew": "    <Anything/>\n",
}


def r4_request_readers() -> tuple[str, dict[str, str]]:
    """Steps reading a request header, a query parameter, the verb and a flow variable, in conditions, a template
    and a ref."""
    steps = (
        step("RF-H", 'request.header.X-Tier = "gold"')
        + step("RF-Q", 'request.queryparam.tier = "gold"')
        + step("RF-V", 'request.verb = "POST"')
        + step("RF-C", "tier.name != null")
        + step("AM-Echo")
        + step("Q-H")
    )
    policies = {
        "RF-H": raise_fault("RF-H", 403, "H"),
        "RF-Q": raise_fault("RF-Q", 403, "Q"),
        "RF-V": raise_fault("RF-V", 403, "V"),
        "RF-C": raise_fault("RF-C", 403, "C"),
        "AM-Echo": assign_headers("AM-Echo", [("X-Echo", "{request.header.X-Tier}")]),
        "Q-H": r2_quota("Q-H", "request.header.X-Tier"),
    }
    return steps, policies


def r4_assert_request_reads_refused(project: Project, result: Any, writer: str) -> None:
    for name, needle in (("RF-H", "X-Tier"), ("RF-Q", "tier"), ("RF-V", "verb"), ("RF-C", "tier.name")):
        r4_assert_condition_refused(result, name, writer, needle)
        assert collapse(r1_when_of(project, name).get("expression") or "") == "#[false]", name
    reasons = r2_refusals(result, "AM-Echo")
    assert any(writer in r and "X-Tier" in r for r in reasons), reasons
    written = [v for e in project.labelled("AM-Echo") for v in values(e)]
    assert not any("x-tier" in v.lower() and "attributes.headers" in v for v in written), written
    r4_assert_skipped_naming(result, "Q-H", writer)


@pytest.mark.parametrize("kind", list(R4_OPAQUE))
def test_CP5_T63_a_policy_without_a_write_model_may_change_any_request_part_and_variable(
    tmp_path: Path, kind: str
) -> None:
    """[CP5-T63] A skipped JavaScript (the reviewer's case: it sets request.header.X-Tier), Python, JavaCallout,
    ServiceCallout, KeyValueMapOperations, OAuthV2 GenerateAccessToken or unknown policy type may change any
    request header, query parameter, the verb and any variable: every later read is refused, naming it."""
    readers, reader_policies = r4_request_readers()
    proxy = r1_proxy(pre_request=step("OP-Step") + readers)
    project, result = r1_generate(tmp_path, proxy, {"OP-Step": policy(kind, "OP-Step", R4_OPAQUE[kind]), **reader_policies})
    assert str(policy_record(result, "OP-Step").method) == "skipped", policy_record(result, "OP-Step")
    r4_assert_request_reads_refused(project, result, "OP-Step")


@pytest.mark.parametrize(
    "writer",
    [
        r3_verify_key("OP-Step", "request.formparam.key"),
        r3_oauth("OP-Step", "VerifyAccessToken"),
    ],
    ids=["skipped-verifyapikey", "oauthv2-verify"],
)
def test_CP5_T63_a_policy_with_a_documented_write_set_keeps_request_reads(tmp_path: Path, writer: str) -> None:
    """[CP5-T63] A skipped VerifyAPIKey and OAuthV2 VerifyAccessToken change no request part in Apigee: a later
    request header, query parameter and verb read is still translated."""
    readers, reader_policies = r4_request_readers()
    proxy = r1_proxy(pre_request=step("OP-Step") + readers)
    _, result = r1_generate(tmp_path, proxy, {"OP-Step": writer, **reader_policies})
    for name in ("RF-H", "RF-Q", "RF-V", "RF-C"):
        assert condition_record(result, name).ok is True, condition_record(result, name)
    assert str(policy_record(result, "Q-H").method) == "template", policy_record(result, "Q-H")


def test_CP5_T63_an_unresolved_shared_flow_call_may_change_any_request_part(tmp_path: Path) -> None:
    """[CP5-T63] A FlowCallout whose shared flow bundle is not in the input may change anything: later request
    reads are refused, naming the callout."""
    readers, reader_policies = r4_request_readers()
    proxy = r1_proxy(pre_request=step("FC-Gone") + readers)
    project, result = r1_generate(tmp_path, proxy, {"FC-Gone": r3_callout("FC-Gone", "not-here"), **reader_policies})
    r4_assert_request_reads_refused(project, result, "FC-Gone")


def r4_extract(name: str, source: str, header: str = "X-R") -> str:
    return policy(
        "ExtractVariables",
        name,
        f"    <Source>{source}</Source>\n    <VariablePrefix>ev</VariablePrefix>\n"
        f'    <Header name="{header}">\n        <Pattern>{{h}}</Pattern>\n    </Header>\n'
        '    <JSONPayload>\n        <Variable name="id">\n            <JSONPath>$.id</JSONPath>\n'
        "        </Variable>\n    </JSONPayload>\n    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n",
    )


def r4_set_payload(name: str, kind: str, condition_free_value: str = '{"id":"1"}') -> str:
    return policy(
        "AssignMessage",
        name,
        f'    <Set>\n        <Payload contentType="application/json">{condition_free_value}</Payload>\n    </Set>\n'
        f'    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n    <AssignTo createNew="false" type="{kind}"/>\n',
    )


def test_CP5_T63_a_policy_without_a_write_model_may_change_the_request_payload(tmp_path: Path) -> None:
    """[CP5-T63] After a skipped JavaScript, ExtractVariables JSONPayload on the request is refused, naming it, and
    the variable it would have written is then refused too."""
    proxy = r1_proxy(pre_request=step("JS-Body") + step("EV-Body") + step("RF-Id", "ev.id != null"))
    policies = {
        "JS-Body": policy("Javascript", "JS-Body", R4_OPAQUE["Javascript"]),
        "EV-Body": r4_extract("EV-Body", "request", "X-Unused"),
        "RF-Id": raise_fault("RF-Id", 403, "Id"),
    }
    project, result = r1_generate(tmp_path, proxy, policies)
    reasons = r2_refusals(result, "EV-Body")
    assert any("JS-Body" in r and "payload" in r for r in reasons), reasons
    assert not any("$.id" in v or "payload.id" in v for e in project.labelled("EV-Body") for v in values(e))
    r4_assert_condition_refused(result, "RF-Id", "EV-Body", "ev.id")


def test_CP5_T63_a_policy_without_a_write_model_may_change_the_response(tmp_path: Path) -> None:
    """[CP5-T63] After a skipped JavaScript in the response flow, ExtractVariables of a response header and of the
    response payload are refused, naming it."""
    proxy = r1_proxy(pre_response=step("JS-Resp") + step("EV-Resp"))
    policies = {"JS-Resp": policy("Javascript", "JS-Resp", R4_OPAQUE["Javascript"]), "EV-Resp": r4_extract("EV-Resp", "response")}
    _, result = r1_generate(tmp_path, proxy, policies)
    reasons = r2_refusals(result, "EV-Resp")
    assert any("JS-Resp" in r and "X-R" in r for r in reasons), reasons
    assert any("JS-Resp" in r and "payload" in r for r in reasons), reasons


def test_CP5_T63_an_exact_payload_write_keeps_the_payload_read(tmp_path: Path) -> None:
    """[CP5-T63] An AssignMessage Set Payload a2m generates exactly does not refuse a later JSONPayload read."""
    proxy = r1_proxy(pre_request=step("AM-Body") + step("EV-Body"))
    policies = {"AM-Body": r4_set_payload("AM-Body", "request"), "EV-Body": r4_extract("EV-Body", "request", "X-Unused")}
    project, result = r1_generate(tmp_path, proxy, policies)
    assert not any("payload" in r for r in r2_refusals(result, "EV-Body")), r2_refusals(result, "EV-Body")
    assert any(e.get("variableName") == "ev.id" for e in project.step("EV-Body").iter())


def test_CP5_T63_a_dropped_response_header_write_refuses_only_that_header(tmp_path: Path) -> None:
    """[CP5-T63] An AssignMessage on the response whose X-R value can't be translated drops that write: a later
    ExtractVariables of response header X-R is refused, naming it; X-Other, written exactly, is still read."""
    writer = policy(
        "AssignMessage",
        "AM-Resp",
        '    <Set>\n        <Headers>\n            <Header name="X-R">{system.timestamp}</Header>\n'
        '            <Header name="X-Other">ok</Header>\n        </Headers>\n    </Set>\n'
        '    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n    <AssignTo createNew="false" type="response"/>\n',
    )
    proxy = r1_proxy(pre_response=step("AM-Resp") + step("EV-R") + step("EV-O"))
    policies = {"AM-Resp": writer, "EV-R": r4_extract("EV-R", "response"), "EV-O": r4_extract("EV-O", "response", "X-Other")}
    _, result = r1_generate(tmp_path, proxy, policies)
    assert any("AM-Resp" in r and "X-R" in r for r in r2_refusals(result, "EV-R")), r2_refusals(result, "EV-R")
    assert not any("AM-Resp" in r for r in r2_refusals(result, "EV-O")), r2_refusals(result, "EV-O")


def test_CP5_T64_a_step_under_a_condition_that_cant_be_translated_is_a_writer_of_everything_it_may_write(
    tmp_path: Path,
) -> None:
    """[CP5-T64] A JavaScript under a condition a2m can't translate still may change anything; an AssignMessage
    Set Payload under one never runs in the generated app, so a later JSONPayload read is refused."""
    readers, reader_policies = r4_request_readers()
    proxy = r1_proxy(pre_request=step("OP-Step", R4_DEAD) + readers)
    project, result = r1_generate(
        tmp_path, proxy, {"OP-Step": policy("Javascript", "OP-Step", R4_OPAQUE["Javascript"]), **reader_policies}
    )
    r4_assert_request_reads_refused(project, result, "OP-Step")

    proxy = r1_proxy(pre_request=step("AM-Body", R4_DEAD) + step("EV-Body"))
    policies = {"AM-Body": r4_set_payload("AM-Body", "request"), "EV-Body": r4_extract("EV-Body", "request", "X-Unused")}
    _, result = r1_generate(tmp_path / "payload", proxy, policies)
    assert any("AM-Body" in r and "payload" in r for r in r2_refusals(result, "EV-Body")), r2_refusals(result, "EV-Body")


def r4_fault_rules(rules: str) -> str:
    return f"    <FaultRules>{rules}</FaultRules>\n"


def test_CP5_T65_a_fault_rule_step_is_a_writer_for_fault_rule_conditions_only(tmp_path: Path) -> None:
    """[CP5-T65] Fault rules are not generated, but Apigee runs their steps: a fault rule condition reading a
    variable another fault rule's step writes is reported can't translate, naming it. A target fault rule's write
    never reaches the generated path: a target PreFlow condition reading it is still translated."""
    rules = (
        f'<FaultRule name="fr-set">{step("AM-FSet")}</FaultRule>'
        f'<FaultRule name="fr-read">{step("RF-F")}<Condition>fr.v = "x"</Condition></FaultRule>'
    )
    proxy = r1_proxy().replace("    <HTTPProxyConnection>", r4_fault_rules(rules) + "    <HTTPProxyConnection>", 1)
    policies = {
        "AM-FSet": r4_set("AM-FSet", "fr.v", "x"),
        "RF-F": raise_fault("RF-F", 500, "F"),
        "AM-TSet": r4_set("AM-TSet", "t.v", "x"),
        "AM-TRead": assign_headers("AM-TRead", [("X-T", "1")]),
    }
    root = write_bundle(tmp_path / "bundles", "r4-fault", proxy, policies)
    target = TARGET_XML.replace(
        "    <HTTPTargetConnection>",
        r4_fault_rules(f'<FaultRule name="tfr">{step("AM-TSet")}</FaultRule>') + "    <HTTPTargetConnection>",
        1,
    ).replace(
        "        <Request/>\n        <Response/>\n    </PreFlow>",
        f"        <Request>{step('AM-TRead', 't.v = &quot;x&quot;')}</Request>\n        <Response/>\n    </PreFlow>",
        1,
    )
    (root / "apiproxy" / "targets" / "default.xml").write_text(target, encoding="utf-8")
    result = generate(root, tmp_path / "out" / "r4-fault" / "mule-app")
    r4_assert_condition_refused(result, "fr-read", "AM-FSet", "fr.v")
    assert condition_record(result, "AM-TRead").ok is True, condition_record(result, "AM-TRead")


# ---------------------------------------------------------------- CP5 adversarial round 5
# Inside one AssignMessage, the ordered tracking of what the earlier operations changed covers every message the
# policy changes, not only the request: a response header or payload an earlier operation changes in Apigee where
# the template does not (Copy, a value it can't translate) is can't translate when a later AssignVariable Ref of
# the same policy reads it, and so is every part of a message kept in a named variable.


def r5_response_assign(name: str, body: str, assign_to: str = '<AssignTo createNew="false" type="response"/>') -> str:
    return policy(
        "AssignMessage",
        name,
        body + f"    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n    {assign_to}\n",
    )


R5_TIER_REF = "    <AssignVariable>\n        <Name>tier</Name>\n        <Ref>response.header.X-Tier</Ref>\n    </AssignVariable>\n"


def r5_generate_tier(tmp_path: Path, writer: str) -> tuple[Project, Any]:
    """``writer`` (AM-Tier) in the response PreFlow, then a step whose condition is tier != "gold"."""
    proxy = r1_proxy(pre_response=step("AM-Tier") + step("AM-Gold", 'tier != "gold"'))
    policies = {"AM-Tier": writer, "AM-Gold": r5_response_assign("AM-Gold", r5_set_header("X-Not-Gold", "1"))}
    return r1_generate(tmp_path, proxy, policies)


def r5_set_header(header: str, value: str) -> str:
    return f'    <Set>\n        <Headers>\n            <Header name="{header}">{value}</Header>\n        </Headers>\n    </Set>\n'


def r5_assert_tier_refused(project: Project, result: Any, operation: str) -> None:
    reasons = r2_refusals(result, "AM-Tier")
    assert any("tier" in r and "AM-Tier" in r and operation in r and "x-tier" in r.lower() for r in reasons), reasons
    written = [v for e in project.labelled("AM-Tier") for v in values(e)]
    assert not any("x-tier" in v.lower() and "responseHeaders" in v and "tier" in v for v in written if "default" in v), written
    assert not any(e.get("variableName") == "tier" for x in project.labelled("AM-Tier") for e in x.iter()), written
    r4_assert_condition_refused(result, "AM-Gold", "AM-Tier", "tier")


def test_CP5_T67_a_response_header_copied_then_read_by_assignvariable_in_the_same_policy_is_refused(
    tmp_path: Path,
) -> None:
    """[CP5-T67] The reviewers' repro: a response-side AssignMessage Copies response header X-Tier (not carried
    over), then AssignVariable tier reads response.header.X-Tier. The Ref is refused, naming the Copy, tier is not
    set, and a later condition tier != "gold" is can't translate, naming the step."""
    writer = r5_response_assign(
        "AM-Tier",
        '    <Copy source="response">\n        <Headers>\n            <Header name="X-Tier"/>\n        </Headers>\n'
        "    </Copy>\n" + R5_TIER_REF,
    )
    project, result = r5_generate_tier(tmp_path, writer)
    r5_assert_tier_refused(project, result, "<Copy>")


def test_CP5_T67_a_response_header_set_to_a_value_that_cant_be_translated_then_read_is_refused(
    tmp_path: Path,
) -> None:
    """[CP5-T67] The reviewers' repro: a response-side AssignMessage Sets X-Tier to {client.ip} (refused), then
    AssignVariable tier reads response.header.X-Tier. The Ref is refused, naming the Set, and the later condition
    on tier is can't translate, naming the step."""
    writer = r5_response_assign("AM-Tier", r5_set_header("X-Tier", "{client.ip}") + R5_TIER_REF)
    project, result = r5_generate_tier(tmp_path, writer)
    r5_assert_tier_refused(project, result, "<Set>")


def test_CP5_T67_a_response_header_set_exactly_then_read_is_still_translated(tmp_path: Path) -> None:
    """[CP5-T67] Control: a response header the same policy Sets to a literal is written exactly, so the
    AssignVariable Ref reads the response headers being built and the later condition on tier is translated."""
    writer = r5_response_assign("AM-Tier", r5_set_header("X-Tier", "gold") + R5_TIER_REF)
    project, result = r5_generate_tier(tmp_path, writer)
    assert not any("tier" in r for r in r2_refusals(result, "AM-Tier")), r2_refusals(result, "AM-Tier")
    assert any(e.get("variableName") == "tier" for x in project.labelled("AM-Tier") for e in x.iter())
    assert condition_record(result, "AM-Gold").ok is True, condition_record(result, "AM-Gold")


def test_CP5_T67_a_copy_then_an_exact_set_of_the_same_response_header_is_read(tmp_path: Path) -> None:
    """[CP5-T67] A Copy of X-Tier (not carried over) followed by a Set of X-Tier to a literal: Set replaces the
    header in Apigee and in the generated app alike, so the AssignVariable Ref reads it."""
    writer = r5_response_assign(
        "AM-Tier",
        '    <Copy source="response">\n        <Headers>\n            <Header name="X-Tier"/>\n        </Headers>\n'
        "    </Copy>\n" + r5_set_header("X-Tier", "gold") + R5_TIER_REF,
    )
    _, result = r5_generate_tier(tmp_path, writer)
    assert not any("tier" in r and "Ref" in r for r in r2_refusals(result, "AM-Tier")), r2_refusals(result, "AM-Tier")
    assert condition_record(result, "AM-Gold").ok is True, condition_record(result, "AM-Gold")


def test_CP5_T67_a_copy_of_every_response_header_refuses_a_read_of_any_of_them(tmp_path: Path) -> None:
    """[CP5-T67] A Copy of every response header (an empty Headers list) then a Ref to response.header.X-Tier is
    refused, naming the Copy."""
    writer = r5_response_assign(
        "AM-Tier", '    <Copy source="response">\n        <Headers/>\n    </Copy>\n' + R5_TIER_REF
    )
    project, result = r5_generate_tier(tmp_path, writer)
    r5_assert_tier_refused(project, result, "<Copy>")


def test_CP5_T67_a_new_response_then_a_read_of_its_header_in_the_same_policy_is_refused(tmp_path: Path) -> None:
    """[CP5-T67] AssignTo createNew on the response replaces every part of it in Apigee (a2m keeps the new message
    in a variable): a Ref to response.header.X-Tier in the same policy is refused, naming the step."""
    writer = r5_response_assign(
        "AM-Tier", r5_set_header("X-Tier", "gold") + R5_TIER_REF, '<AssignTo createNew="true" type="response"/>'
    )
    project, result = r5_generate_tier(tmp_path, writer)
    r5_assert_tier_refused(project, result, "AssignTo")


def test_CP5_T67_a_part_of_a_named_message_read_in_the_same_policy_is_refused(tmp_path: Path) -> None:
    """[CP5-T67] A message kept in a named variable (AssignTo myMsg) has no faithful model of its parts: a Ref to
    myMsg.header.X-Tier in the same policy is refused, naming the step, and the later condition on tier too."""
    writer = r5_response_assign(
        "AM-Tier",
        r5_set_header("X-Tier", "gold")
        + "    <AssignVariable>\n        <Name>tier</Name>\n        <Ref>myMsg.header.X-Tier</Ref>\n"
        "    </AssignVariable>\n",
        '<AssignTo createNew="false" type="response">myMsg</AssignTo>',
    )
    project, result = r5_generate_tier(tmp_path, writer)
    reasons = r2_refusals(result, "AM-Tier")
    assert any("tier" in r and "AM-Tier" in r and "myMsg" in r for r in reasons), reasons
    assert not any(e.get("variableName") == "tier" for x in project.labelled("AM-Tier") for e in x.iter())
    r4_assert_condition_refused(result, "AM-Gold", "AM-Tier", "tier")


def test_CP5_T67_a_response_header_removed_exactly_then_set_to_a_dropped_value_is_stale_for_later_steps(
    tmp_path: Path,
) -> None:
    """[CP5-T67] Remove X-R (carried over) then Set X-R to {client.ip} (refused): Apigee's X-R is the Set value,
    the generated app's is removed, so a later ExtractVariables of response header X-R is refused, naming the
    step."""
    writer = r5_response_assign(
        "AM-Resp",
        '    <Remove>\n        <Headers>\n            <Header name="X-R"/>\n        </Headers>\n    </Remove>\n'
        + r5_set_header("X-R", "{client.ip}"),
    )
    proxy = r1_proxy(pre_response=step("AM-Resp") + step("EV-R"))
    _, result = r1_generate(tmp_path, proxy, {"AM-Resp": writer, "EV-R": r4_extract("EV-R", "response")})
    assert any("AM-Resp" in r and "X-R" in r for r in r2_refusals(result, "EV-R")), r2_refusals(result, "EV-R")


def test_CP5_T67_a_request_payload_removed_exactly_then_set_to_a_dropped_value_is_stale_for_later_steps(
    tmp_path: Path,
) -> None:
    """[CP5-T67] Remove Payload (carried over) then Set Payload holding {client.ip} (refused) on the request: a
    later ExtractVariables JSONPayload of the request is refused, naming the step."""
    writer = r2_assign(
        "AM-Body",
        "    <Remove>\n        <Payload>true</Payload>\n    </Remove>\n"
        '    <Set>\n        <Payload contentType="application/json">{"ip":"{client.ip}"}</Payload>\n    </Set>\n',
    )
    proxy = r1_proxy(pre_request=step("AM-Body") + step("EV-Body"))
    _, result = r1_generate(
        tmp_path, proxy, {"AM-Body": writer, "EV-Body": r4_extract("EV-Body", "request", "X-Unused")}
    )
    reasons = r2_refusals(result, "EV-Body")
    assert any("AM-Body" in r and "payload" in r for r in reasons), reasons


# ---------------------------------------------------------------- CP5 round 6: the last write of a name decides
# A flow variable or message part counts as written exactly only when the policy's last write of it is carried over
# exactly: an exact write followed by one a2m drops leaves Apigee's value from the dropped write, so later reads of
# it are refused; a dropped write followed by an exact one is exact again.


def r6_av(variable: str, inner: str) -> str:
    return f"    <AssignVariable>\n        <Name>{variable}</Name>\n        {inner}\n    </AssignVariable>\n"


def r6_tier_guard(tmp_path: Path, writers: dict[str, str]) -> tuple[Project, Any]:
    """``writers`` in the request PreFlow, in order, then RF-Gold under the condition tier = "gold"."""
    steps = "".join(step(name) for name in writers) + step("RF-Gold", 'tier = "gold"')
    policies = {**writers, "RF-Gold": raise_fault("RF-Gold", 403, "Gold")}
    return r1_generate(tmp_path, r1_proxy(pre_request=steps), policies)


@pytest.mark.parametrize(
    "dropped",
    ["<Ref>client.ip</Ref>", "<Value>{system.timestamp}</Value>"],
    ids=["unmapped-ref", "templated-value"],
)
def test_CP5_T68_an_exact_assignvariable_overwritten_by_a_dropped_one_is_stale_for_later_steps(
    tmp_path: Path, dropped: str
) -> None:
    """[CP5-T68] The reviewers' repro: AM-Tier sets tier to gold, then sets tier from client.ip (or a template),
    which a2m can't translate. Apigee's tier is the second value, so the later condition tier = "gold" is can't
    translate, naming AM-Tier, never translated against the generated app's stale literal."""
    am = r2_assign("AM-Tier", r6_av("tier", "<Value>gold</Value>") + r6_av("tier", dropped))
    _, result = r6_tier_guard(tmp_path, {"AM-Tier": am})
    r4_assert_condition_refused(result, "RF-Gold", "AM-Tier", "tier")


def test_CP5_T68_a_dropped_assignvariable_overwritten_by_an_exact_one_is_exact(tmp_path: Path) -> None:
    """[CP5-T68] The reverse order: tier from client.ip (dropped), then tier = gold (exact). The last write is
    exact in Apigee and the generated app alike, so a later AssignVariable of the same policy reads tier and the
    later condition on it is translated."""
    am = r2_assign(
        "AM-Tier",
        r6_av("tier", "<Ref>client.ip</Ref>") + r6_av("tier", "<Value>gold</Value>") + r6_av("copy.tier", "<Ref>tier</Ref>"),
    )
    project, result = r6_tier_guard(tmp_path, {"AM-Tier": am})
    assert not any("copy.tier" in r for r in r2_refusals(result, "AM-Tier")), r2_refusals(result, "AM-Tier")
    assert any(e.get("variableName") == "copy.tier" for x in project.labelled("AM-Tier") for e in x.iter())
    assert condition_record(result, "RF-Gold").ok is True, condition_record(result, "RF-Gold")


def test_CP5_T68_an_exact_write_in_one_step_then_a_dropped_write_in_a_later_step_is_stale(tmp_path: Path) -> None:
    """[CP5-T68] Across steps: AM-Gold sets tier exactly, then AM-Ip sets tier from client.ip (dropped). The later
    condition tier = "gold" is can't translate, naming AM-Ip."""
    writers = {
        "AM-Gold": r2_assign("AM-Gold", r6_av("tier", "<Value>gold</Value>")),
        "AM-Ip": r2_assign("AM-Ip", r6_av("tier", "<Ref>client.ip</Ref>")),
    }
    _, result = r6_tier_guard(tmp_path, writers)
    r4_assert_condition_refused(result, "RF-Gold", "AM-Ip", "tier")


def test_CP5_T68_an_exact_then_dropped_write_in_an_earlier_step_is_stale_after_an_unrelated_step(
    tmp_path: Path,
) -> None:
    """[CP5-T68] Across steps: AM-Tier (exact then dropped tier) and an unrelated step between it and the reader
    still leave tier stale, naming AM-Tier."""
    writers = {
        "AM-Tier": r2_assign("AM-Tier", r6_av("tier", "<Value>gold</Value>") + r6_av("tier", "<Ref>client.ip</Ref>")),
        "AM-Other": r2_assign("AM-Other", r6_av("other.v", "<Value>1</Value>")),
    }
    _, result = r6_tier_guard(tmp_path, writers)
    r4_assert_condition_refused(result, "RF-Gold", "AM-Tier", "tier")


def test_CP5_T68_a_response_header_set_twice_in_one_set_with_one_value_dropped_is_stale(tmp_path: Path) -> None:
    """[CP5-T68] One Set writes X-R to a literal and again to {client.ip} (refused): the header is not written
    exactly, so a later ExtractVariables of response header X-R is refused, naming the step."""
    writer = r5_response_assign(
        "AM-Resp",
        '    <Set>\n        <Headers>\n            <Header name="X-R">gold</Header>\n'
        '            <Header name="X-R">{client.ip}</Header>\n        </Headers>\n    </Set>\n',
    )
    proxy = r1_proxy(pre_response=step("AM-Resp") + step("EV-R"))
    _, result = r1_generate(tmp_path, proxy, {"AM-Resp": writer, "EV-R": r4_extract("EV-R", "response")})
    assert any("AM-Resp" in r and "X-R" in r for r in r2_refusals(result, "EV-R")), r2_refusals(result, "EV-R")


@pytest.mark.parametrize(
    "body",
    [
        (
            '    <Set>\n        <Payload contentType="application/json">{"id":"1"}</Payload>\n'
            '        <Payload contentType="application/json">{"ip":"{client.ip}"}</Payload>\n    </Set>\n'
        ),
        (
            '    <Set>\n        <Payload contentType="application/json">{"id":"1"}</Payload>\n    </Set>\n'
            '    <Set>\n        <Payload contentType="application/json">{"ip":"{client.ip}"}</Payload>\n    </Set>\n'
        ),
    ],
    ids=["two-payloads-one-set", "two-set-blocks"],
)
def test_CP5_T68_an_exact_payload_then_a_dropped_payload_in_one_policy_is_stale(tmp_path: Path, body: str) -> None:
    """[CP5-T68] An exact Set Payload followed, in the same policy, by a Set Payload a2m does not carry over leaves
    the request payload stale: a later ExtractVariables JSONPayload is refused, naming the step."""
    proxy = r1_proxy(pre_request=step("AM-Body") + step("EV-Body"))
    _, result = r1_generate(
        tmp_path, proxy, {"AM-Body": r2_assign("AM-Body", body), "EV-Body": r4_extract("EV-Body", "request", "X-Unused")}
    )
    reasons = r2_refusals(result, "EV-Body")
    assert any("AM-Body" in r and "payload" in r for r in reasons), reasons


def test_CP5_T68_a_second_set_block_is_listed_not_silently_dropped(tmp_path: Path) -> None:
    """[CP5-T68] Only the first <Set> of an AssignMessage is carried over; the second is listed as unsupported."""
    body = (
        '    <Set>\n        <Headers>\n            <Header name="X-A">1</Header>\n        </Headers>\n    </Set>\n'
        '    <Set>\n        <Headers>\n            <Header name="X-B">2</Header>\n        </Headers>\n    </Set>\n'
    )
    _, result = r1_generate(tmp_path, r1_proxy(pre_request=step("AM-Two")), {"AM-Two": r2_assign("AM-Two", body)})
    options = policy_record(result, "AM-Two").unsupported_options
    assert any("Set" in str(o.name) for o in options), options


@pytest.mark.parametrize(
    "dropped",
    [
        (
            '    <JSONPayload>\n        <Variable name="v">\n            <JSONPath>$..v</JSONPath>\n'
            "        </Variable>\n    </JSONPayload>\n"
        ),
        (
            '    <XMLPayload>\n        <Variable name="v">\n            <XPath>/a/v</XPath>\n'
            "        </Variable>\n    </XMLPayload>\n"
        ),
    ],
    ids=["jsonpath-not-translated", "xmlpayload"],
)
def test_CP5_T68_an_extracted_variable_also_written_by_an_extraction_a2m_drops_is_stale(
    tmp_path: Path, dropped: str
) -> None:
    """[CP5-T68] ExtractVariables writes v exactly from header X-V and again from an extraction a2m does not carry
    over: Apigee's v may come from the second, so a later condition on v is can't translate, naming the step."""
    ev = policy(
        "ExtractVariables",
        "EV-V",
        "    <Source>request</Source>\n"
        '    <Header name="X-V">\n        <Pattern>{v}</Pattern>\n    </Header>\n'
        + dropped
        + "    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n",
    )
    proxy = r1_proxy(pre_request=step("EV-V") + step("RF-V", 'v = "1"'))
    _, result = r1_generate(tmp_path, proxy, {"EV-V": ev, "RF-V": raise_fault("RF-V", 403, "V")})
    r4_assert_condition_refused(result, "RF-V", "EV-V", "v")


def test_CP5_T68_an_extracted_variable_written_once_exactly_is_still_read(tmp_path: Path) -> None:
    """[CP5-T68] Control: ExtractVariables writing v once, exactly, keeps the later condition on v translated."""
    ev = policy(
        "ExtractVariables",
        "EV-V",
        "    <Source>request</Source>\n"
        '    <Header name="X-V">\n        <Pattern>{v}</Pattern>\n    </Header>\n'
        "    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n",
    )
    proxy = r1_proxy(pre_request=step("EV-V") + step("RF-V", 'v = "1"'))
    _, result = r1_generate(tmp_path, proxy, {"EV-V": ev, "RF-V": raise_fault("RF-V", 403, "V")})
    assert condition_record(result, "RF-V").ok is True, condition_record(result, "RF-V")


# ---------------------------------------------------------------- CP5 adversarial round 7
# H1/A1: an ExtractVariables extraction that finds nothing leaves the variable as it was (proven on the runtime in
# tests/runtime/test_cp5_conditions_runtime.py, CP5-T69). H2: response.header.NAME reads go through the one accessor,
# Content-Type included, on every reader (ExtractVariables, AssignVariable Ref, templates, conditions).


def r7_ev(name: str, source: str, header: str, pattern: str) -> str:
    return policy(
        "ExtractVariables",
        name,
        f"    <Source>{source}</Source>\n"
        f'    <Header name="{header}">\n        <Pattern>{pattern}</Pattern>\n    </Header>\n'
        "    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n",
    )


def r7_response_readers(tmp_path: Path, header: str) -> tuple[Project, Any]:
    """Every reader of response.header.``header`` on the response side: EV-H (ExtractVariables), AM-Ref
    (AssignVariable Ref), AM-Tpl (a header template) and AM-Cond (a condition)."""
    ref = f"response.header.{header}"
    readers = {
        "EV-H": r7_ev("EV-H", "response", header, "{h}"),
        "AM-Ref": r5_response_assign("AM-Ref", r6_av("h2", f"<Ref>{ref}</Ref>")),
        "AM-Tpl": r5_response_assign("AM-Tpl", r5_set_header("X-Copy", "{" + ref + "}")),
        "AM-Cond": r5_response_assign("AM-Cond", r5_set_header("X-Json", "yes")),
    }
    steps = step("EV-H") + step("AM-Ref") + step("AM-Tpl") + step("AM-Cond", f'{ref} = "application/json"')
    return r1_generate(tmp_path, r1_proxy(pre_response=steps), readers)


def test_CP5_T69_an_extraction_after_a_default_is_translated_and_the_condition_on_it_is_kept(tmp_path: Path) -> None:
    """[CP5-T69] The reviewers' repro: AssignVariable tier = free, ExtractVariables {tier} from header X-Tier, then
    RaiseFault under tier = "free". The extraction keeps the default when X-Tier is missing (runtime CP5-T69), so
    the extraction is translated with nothing listed and the condition stays translated."""
    writers = {
        "AM-Default": r2_assign("AM-Default", r6_av("tier", "<Value>free</Value>")),
        "EV-Tier": r7_ev("EV-Tier", "request", "X-Tier", "{tier}"),
    }
    _, result = r6_tier_guard(tmp_path, writers)
    assert r2_refusals(result, "EV-Tier") == [], r2_refusals(result, "EV-Tier")
    assert condition_record(result, "RF-Gold").ok is True, condition_record(result, "RF-Gold")


def test_CP5_T69_an_extraction_that_keeps_a_stale_earlier_value_leaves_the_variable_stale(tmp_path: Path) -> None:
    """[CP5-T69] AM-Ip sets tier from client.ip (not carried over), then EV-Tier extracts tier from X-Tier. When
    X-Tier is missing Apigee keeps AM-Ip's value, which the generated app does not hold, so the later condition on
    tier is can't translate, naming AM-Ip."""
    writers = {
        "AM-Ip": r2_assign("AM-Ip", r6_av("tier", "<Ref>client.ip</Ref>")),
        "EV-Tier": r7_ev("EV-Tier", "request", "X-Tier", "{tier}"),
    }
    _, result = r6_tier_guard(tmp_path, writers)
    r4_assert_condition_refused(result, "RF-Gold", "AM-Ip", "tier")


def test_CP5_T69_every_reader_of_the_response_content_type_is_translated(tmp_path: Path) -> None:
    """[CP5-T69] response.header.Content-Type after the target call is read the same way by ExtractVariables, an
    AssignVariable Ref, a header template and a condition: every one is translated, none refused."""
    _, result = r7_response_readers(tmp_path, "Content-Type")
    for name in ("EV-H", "AM-Ref", "AM-Tpl"):
        assert r2_refusals(result, name) == [], (name, r2_refusals(result, name))
    assert condition_record(result, "AM-Cond").ok is True, condition_record(result, "AM-Cond")


@pytest.mark.parametrize("header", ["Content-Length", "Transfer-Encoding", "Connection"])
def test_CP5_T69_every_reader_refuses_a_response_framing_header(tmp_path: Path, header: str) -> None:
    """[CP5-T69] Mule's listener writes the framing headers itself and the generated app does not keep the target's
    values, so every reader of response.header.<framing header> refuses it, naming the header."""
    _, result = r7_response_readers(tmp_path, header)
    for name in ("EV-H", "AM-Ref", "AM-Tpl"):
        reasons = r2_refusals(result, name)
        assert any(header.lower() in r.lower() for r in reasons), (name, reasons)
    record = condition_record(result, "AM-Cond")
    assert record.ok is False and header.lower() in (record.reason or "").lower(), record


def test_CP5_T69_a_response_header_read_on_the_request_side_is_refused(tmp_path: Path) -> None:
    """[CP5-T69] There is no response on the request side: a condition and an AssignVariable Ref reading
    response.header.X-R there are refused, never read as a missing value."""
    policies = {
        "AM-Ref": r2_assign("AM-Ref", r6_av("h2", "<Ref>response.header.X-R</Ref>")),
        "AM-Cond": assign_headers("AM-Cond", [("X-Seen", "yes")]),
    }
    proxy = r1_proxy(pre_request=step("AM-Ref") + step("AM-Cond", 'response.header.X-R = "1"'))
    _, result = r1_generate(tmp_path, proxy, policies)
    assert any("response.header.X-R" in r for r in r2_refusals(result, "AM-Ref")), r2_refusals(result, "AM-Ref")
    assert condition_record(result, "AM-Cond").ok is False, condition_record(result, "AM-Cond")


def test_CP5_T69_a_response_payload_content_type_is_read_back_by_a_later_condition(tmp_path: Path) -> None:
    """[CP5-T69] A response Set Payload with contentType text/plain changes the Content-Type header in Apigee and in
    the generated app alike, so a later condition on response.header.Content-Type is translated."""
    body = '    <Set>\n        <Payload contentType="text/plain">hi</Payload>\n    </Set>\n'
    policies = {
        "AM-Body": r5_response_assign("AM-Body", body),
        "AM-Text": r5_response_assign("AM-Text", r5_set_header("X-Text", "yes")),
    }
    proxy = r1_proxy(pre_response=step("AM-Body") + step("AM-Text", 'response.header.Content-Type = "text/plain"'))
    _, result = r1_generate(tmp_path, proxy, policies)
    assert condition_record(result, "AM-Text").ok is True, condition_record(result, "AM-Text")


# ---------------------------------------------------------------- CP5 adversarial round 8
# B1: a JSONPath is translated only when every step's DataWeave equivalent is certain (a property step applied to an
# object, an index to an array; anything else finds nothing, proven on the runtime in CP5-T70). Paths with wildcards,
# filters, recursive descent, unions or slices are can't translate, and a later read of the variable is refused.
# H1 (URIPath on the bare base path) is proven on the runtime in CP5-T70.


def r8_json_ev(path: str) -> str:
    return policy(
        "ExtractVariables",
        "EV-J",
        "    <Source>request</Source>\n"
        f'    <JSONPayload>\n        <Variable name="v">\n            <JSONPath>{path}</JSONPath>\n'
        "        </Variable>\n    </JSONPayload>\n"
        "    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n",
    )


def r8_json_guard(tmp_path: Path, path: str) -> Any:
    proxy = r1_proxy(pre_request=step("EV-J") + step("RF-V", 'v = "1"'))
    _, result = r1_generate(tmp_path, proxy, {"EV-J": r8_json_ev(path), "RF-V": raise_fault("RF-V", 403, "V")})
    return result


@pytest.mark.parametrize(
    "path",
    ["$.items[*].id", "$.*", "$..id", "$.items[?(@.id)].id", "$.items[0,1]", "$.items[0:2]", "$.items[-1]"],
    ids=["wildcard-index", "wildcard-property", "recursive-descent", "filter", "union", "slice", "negative-index"],
)
def test_CP5_T70_a_jsonpath_without_a_certain_dataweave_equivalent_is_cant_translate(tmp_path: Path, path: str) -> None:
    """[CP5-T70] A JSONPath with a wildcard, filter, recursive descent, union, slice or negative index is not
    translated: the extraction is listed naming the path, and a later condition on the variable is refused."""
    result = r8_json_guard(tmp_path, path)
    assert any(path in r for r in r2_refusals(result, "EV-J")), r2_refusals(result, "EV-J")
    assert condition_record(result, "RF-V").ok is False, condition_record(result, "RF-V")


@pytest.mark.parametrize("path", ["$.j", "$.items.id", "$[0]", "$[0].j", "$.items[1].id", "$['a b'][0]"])
def test_CP5_T70_a_jsonpath_of_property_and_index_steps_is_translated(tmp_path: Path, path: str) -> None:
    """[CP5-T70] A JSONPath of property and index steps is translated (each step finds nothing when applied to the
    wrong JSON type, runtime CP5-T70), so nothing is listed and the later condition on the variable is kept."""
    result = r8_json_guard(tmp_path, path)
    assert r2_refusals(result, "EV-J") == [], r2_refusals(result, "EV-J")
    assert condition_record(result, "RF-V").ok is True, condition_record(result, "RF-V")


# ---------------------------------------------------------------- CP5 adversarial round 9
# A custom error type is known to Mule only when the app declares it (raises it, or maps an error to it). Every
# custom type a generated app names in an error handler or an error mapping must be declared in that app, whatever
# policies the bundle holds: a step wrapped in a try scope, with no RaiseFault anywhere, used to name A2M:POLICY_FAULT
# and failed to deploy ("Could not find error 'A2M:POLICY_FAULT'"). Proven on the runtime in CP5-T71.

# Error type namespaces Mule and the modules a2m uses declare themselves; a type without a namespace is MULE's.
R9_BUILTIN_ERROR_NAMESPACES = {"MULE", "HTTP", "OS", "VALIDATION"}


def r9_error_types(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def r9_is_custom(error_type: str) -> bool:
    namespace = error_type.split(":", 1)[0].upper() if ":" in error_type else "MULE"
    return namespace not in R9_BUILTIN_ERROR_NAMESPACES


def r9_undeclared_error_types(project: Project) -> set[str]:
    """Custom error types named in an on-error handler or an error mapping that nothing in the app declares."""
    declared: set[str] = set()
    named: set[str] = set()
    for node in project.nodes():
        if not is_element(node):
            continue
        local = str(node.tag).rsplit("}", 1)[-1]
        if local == "raise-error":
            declared.update(r9_error_types(node.get("type", "")))
        elif local == "error-mapping":
            declared.update(r9_error_types(node.get("targetType", "")))
            named.update(r9_error_types(node.get("sourceType", "")))
        elif local in ("on-error-continue", "on-error-propagate"):
            named.update(r9_error_types(node.get("type", "")))
    return {t for t in named if r9_is_custom(t) and t not in declared}


def r9_two_variables(name: str) -> str:
    return policy(
        "AssignMessage",
        name,
        "    <AssignVariable>\n        <Name>a</Name>\n        <Value>1</Value>\n    </AssignVariable>\n"
        "    <AssignVariable>\n        <Name>b</Name>\n        <Value>2</Value>\n    </AssignVariable>\n",
    )


def r9_projects(tmp_path: Path) -> dict[str, Project]:
    """Every fixture bundle generated, plus apps whose steps are wrapped in a try scope and hold no RaiseFault."""
    projects: dict[str, Project] = {}
    bundles = tmp_path / "bundles"
    for make in (conditions_api, templates_api):
        source = make(bundles)
        generate(source, tmp_path / "out" / source.name)
        projects[source.name] = Project(tmp_path / "out" / source.name)
    for source in (CP4_ORDERS_API, CP4_ORDERS_SIMPLE, FIXTURES / "orders-api", FIXTURES / "policy-runtime"):
        dest = tmp_path / "out" / f"{source.parent.name}-{source.name}"
        generate(source, dest)
        projects[dest.name] = Project(dest)
    copy = tmp_path / "azure"
    shutil.copytree(TEST_API, copy / "Test-API")
    shutil.copytree(GET_SHARED_FLOW, copy / "GetSharedFlow")
    generate(copy / "Test-API", tmp_path / "out" / "Test-API", (copy / "GetSharedFlow",))
    projects["Test-API"] = Project(tmp_path / "out" / "Test-API")
    for label, pre_request, pre_response in (
        ("wrapped-request", step("AM-Two"), ""),
        ("wrapped-response", "", step("AM-Two")),
        ("wrapped-conditional", step("AM-Two", 'request.verb = "GET"'), ""),
    ):
        root = write_bundle(bundles / label, label, r1_proxy(pre_request, pre_response), {"AM-Two": r9_two_variables("AM-Two")})
        generate(root, tmp_path / "out" / label)
        projects[label] = Project(tmp_path / "out" / label)
    for golden in sorted([*GOLDEN_CP3.iterdir(), *GOLDEN_CP4.iterdir()]):
        projects[f"golden-{golden.parent.name}-{golden.name}"] = Project(golden)
    return projects


def test_CP5_T71_every_custom_error_type_an_app_names_is_declared_in_that_app(tmp_path: Path) -> None:
    """[CP5-T71] Across every generated fixture and golden project, and apps whose only step is wrapped in a try scope
    with no RaiseFault, every custom error type named by an error handler or an error mapping is raised or mapped to
    in the same app, so Mule can resolve it when it deploys the app."""
    projects = r9_projects(tmp_path)
    undeclared = {name: sorted(types) for name, project in projects.items() if (types := r9_undeclared_error_types(project))}
    assert undeclared == {}, undeclared



# ---------------------------------------------------------------- CP5 adversarial round 9 (JSONPayload Content-Type)
# Apigee extracts JSONPayload only when the message's Content-Type is JSON, and parses the payload as JSON. The
# generated app decides on the Content-Type as the step sees it and reads the payload as Mule parsed it, so an
# earlier step that may make the two disagree (a Content-Type header set alone, a payload set without a contentType,
# a change a2m does not carry over) refuses the extraction; a Set Payload with a contentType or a removed
# Content-Type keeps it.


def x72_am(name: str, kind: str, body: str) -> str:
    return policy(
        "AssignMessage",
        name,
        f'{body}    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n'
        f'    <AssignTo createNew="false" transport="http" type="{kind}"/>\n',
    )


def x72_writers(kind: str) -> dict[str, tuple[str, bool]]:
    """Each earlier step on the ``kind`` side, by id: (its policy XML as AM-Ct, whether a later JSONPayload read
    stays translated)."""
    header = '    <Set>\n        <Headers>\n            <Header name="Content-Type">{}</Header>\n        </Headers>\n    </Set>\n'
    return {
        "header-dropped": (x72_am("AM-Ct", kind, header.format("{client.ip}")), False),
        "header-alone": (x72_am("AM-Ct", kind, header.format("application/json")), False),
        "payload-no-type": (x72_am("AM-Ct", kind, '    <Set>\n        <Payload>{"id":"1"}</Payload>\n    </Set>\n'), False),
        "payload-typed": (
            x72_am("AM-Ct", kind, '    <Set>\n        <Payload contentType="application/json">{"id":"1"}</Payload>\n    </Set>\n'),
            True,
        ),
        "remove-type": (
            x72_am("AM-Ct", kind, '    <Remove>\n        <Headers>\n            <Header name="Content-Type"/>\n'
                   "        </Headers>\n    </Remove>\n"),
            True,
        ),
        "remove-all-dropped": (
            x72_am("AM-Ct", kind, "    <Remove>\n        <Payload>false</Payload>\n    </Remove>\n"
                   "    <Remove>\n        <Headers/>\n    </Remove>\n"),
            False,
        ),
        "basic-auth": (
            policy(
                "BasicAuthentication",
                "AM-Ct",
                "    <Operation>Encode</Operation>\n    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n"
                '    <User ref="creds.user"/>\n    <Password ref="creds.pass"/>\n'
                f'    <AssignTo createNew="false">{kind}.header.Content-Type</AssignTo>\n',
            ),
            False,
        ),
    }


@pytest.mark.parametrize("kind", ["request", "response"])
@pytest.mark.parametrize("writer", list(x72_writers("request")))
def test_CP5_T72_a_jsonpayload_read_after_a_content_type_or_payload_change_is_kept_only_when_they_still_agree(
    tmp_path: Path, kind: str, writer: str
) -> None:
    """[CP5-T72] After each earlier step, on the request and on the response side, a JSONPayload extraction is
    either generated (and a condition on its variable kept) or refused naming the step and the Content-Type (and
    the condition refused too), never generated with a JSON decision that may differ from Apigee's."""
    xml, kept = x72_writers(kind)[writer]
    extract = r4_extract("EV-Body", kind, "X-Unused")
    if kind == "request":
        proxy = r1_proxy(pre_request=step("AM-Ct") + step("EV-Body") + step("RF-Id", 'ev.id = "1"'))
    else:
        proxy = r1_proxy(pre_response=step("AM-Ct") + step("EV-Body") + step("RF-Id", 'ev.id = "1"'))
    policies = {"AM-Ct": xml, "EV-Body": extract, "RF-Id": raise_fault("RF-Id", 403, "Id")}
    project, result = r1_generate(tmp_path, proxy, policies)
    reasons = r2_refusals(result, "EV-Body")
    labelled = project.labelled("EV-Body")
    wrote_id = any(e.get("variableName") == "ev.id" for element in labelled for e in element.iter())
    if kept:
        assert not any("Content-Type" in r for r in reasons), reasons
        assert wrote_id
        assert condition_record(result, "RF-Id").ok is True, condition_record(result, "RF-Id")
    else:
        assert any("AM-Ct" in r and "Content-Type" in r for r in reasons), reasons
        assert not wrote_id
        r4_assert_condition_refused(result, "RF-Id", "EV-Body", "ev.id")


# ---------------------------------------------------------------- CP5 adversarial round 10: IgnoreUnresolvedVariables
# Apigee's IgnoreUnresolvedVariables defaults to false, and with false Apigee may fail the call when an extraction
# finds nothing; its exact behaviour is not certain, so a2m does not guess a fault. It keeps the earlier value, as
# with true, and lists the setting as an unsupported option so the proxy is reviewed. With true nothing is listed.

X73_SOURCES = {
    "json": '    <JSONPayload>\n        <Variable name="v">\n            <JSONPath>$.a.b</JSONPath>\n'
    "        </Variable>\n    </JSONPayload>\n",
    "header": '    <Header name="X-V">\n        <Pattern>Bearer {v}</Pattern>\n    </Header>\n',
    "query": '    <QueryParam name="v">\n        <Pattern>{v}</Pattern>\n    </QueryParam>\n',
    "uripath": "    <URIPath>\n        <Pattern>/items/{v}</Pattern>\n    </URIPath>\n",
}
X73_SETTINGS = {
    "absent": "",
    "false": "    <IgnoreUnresolvedVariables>false</IgnoreUnresolvedVariables>\n",
    "true": "    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>\n",
    "true-upper": "    <IgnoreUnresolvedVariables>TRUE</IgnoreUnresolvedVariables>\n",
}


@pytest.mark.parametrize("setting", list(X73_SETTINGS))
@pytest.mark.parametrize("source", list(X73_SOURCES))
def test_CP5_T73_ignore_unresolved_variables_not_true_is_listed_and_the_earlier_value_is_kept(
    tmp_path: Path, source: str, setting: str
) -> None:
    """[CP5-T73] An ExtractVariables step whose IgnoreUnresolvedVariables is false or absent lists the setting as an
    unsupported option (Apigee may fail the call when nothing is extracted; a2m keeps the earlier value), once,
    whatever the source; with true nothing is listed. Either way the variable is still generated (falling back to
    its earlier value) and a later condition on it is kept."""
    extract = policy(
        "ExtractVariables",
        "EV-X73",
        f"    <Source>request</Source>\n    <VariablePrefix>ev</VariablePrefix>\n{X73_SOURCES[source]}"
        f"{X73_SETTINGS[setting]}",
    )
    proxy = r1_proxy(pre_request=step("EV-X73") + step("RF-V", 'ev.v = "1"'))
    project, result = r1_generate(tmp_path, proxy, {"EV-X73": extract, "RF-V": raise_fault("RF-V", 403, "V")})
    record = policy_record(result, "EV-X73")
    assert str(record.method) == "template", record
    listed = [o for o in record.unsupported_options if str(o.name) == "IgnoreUnresolvedVariables"]
    if setting.startswith("true"):
        assert listed == [], listed
    else:
        assert len(listed) == 1, record.unsupported_options
        reason = str(listed[0].reason)
        assert "IgnoreUnresolvedVariables=false" in reason and "fail the call" in reason, reason
        assert "keeps the earlier value" in reason, reason
    labelled = project.labelled("EV-X73")
    assert any(e.get("variableName") == "ev.v" for element in labelled for e in element.iter())
    assert condition_record(result, "RF-V").ok is True, condition_record(result, "RF-V")


def test_CP5_T73_an_extract_variables_step_with_nothing_translated_does_not_list_ignore_unresolved_variables(
    tmp_path: Path,
) -> None:
    """[CP5-T73] With no extraction generated (an untranslated JSONPath), only that setting is listed, not
    IgnoreUnresolvedVariables: no generated extraction can find nothing."""
    extract = policy(
        "ExtractVariables",
        "EV-X73",
        "    <Source>request</Source>\n"
        '    <JSONPayload>\n        <Variable name="v">\n            <JSONPath>$..b</JSONPath>\n'
        "        </Variable>\n    </JSONPayload>\n",
    )
    proxy = r1_proxy(pre_request=step("EV-X73"))
    _, result = r1_generate(tmp_path, proxy, {"EV-X73": extract})
    names = [str(o.name) for o in policy_record(result, "EV-X73").unsupported_options]
    assert "IgnoreUnresolvedVariables" not in names, names
    assert any(name.startswith("JSONPayload") for name in names), names
