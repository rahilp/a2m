"""CP3: generate a basic Mule 4 project for each Apigee proxy.

Public entry points used here (CP3 plan; nothing else from a2m is imported):

    a2m.parser.read_bundle(path: Path) -> Bundle          (CP2)
    a2m.generator.generate_project(bundle, dest, *, shared_flows=()) -> result
        ``bundle``        the proxy's IR (a2m.ir.Bundle, kind proxy)
        ``dest``          the mule-app folder to write (created when missing;
                          an older project already there is replaced)
        ``shared_flows``  a sequence of shared flow Bundles available to the
                          proxy's FlowCallout steps, matched by bundle name
        result.unsupported  sequence of items with ``name: str`` (the step,
                            policy, target endpoint or setting) and
                            ``reason: str``
        result.pending      sequence of items with ``name: str`` (the RouteRule
                            name) and ``condition: str`` (the original Apigee
                            condition text), for conditions CP5 translates
    a2m.cli.main(['migrate', ...])                        (CP3-T12, CP3-T21)

Generated projects are inspected on disk the way Maven and Mule read them:
every XML file is parsed with a strict parser, every ``${...}`` placeholder is
resolved against the project's one properties file, and every config-ref and
flow-ref is followed. Golden copies live in tests/golden/cp3/<proxy>/ and are
rewritten only when A2M_UPDATE_GOLDEN=1.

No network, no Java, Maven or Mule, no API keys; every write is under tmp_path
(except golden updates, which need the explicit switch).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import xml.etree.ElementTree as ET
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, quoteattr

import pytest

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "apigee"
TEST_API = FIXTURES / "azure" / "Test-API"
GET_SHARED_FLOW = FIXTURES / "azure" / "GetSharedFlow"
ORDERS_API = FIXTURES / "orders-api"
AUDIT_FLOW = FIXTURES / "audit-flow"
BROKEN_XML = FIXTURES / "malformed" / "broken-xml"
GOLDEN = REPO / "tests" / "golden" / "cp3"
UPDATE_GOLDEN_ENV = "A2M_UPDATE_GOLDEN"

CORE = "http://www.mulesoft.org/schema/mule/core"
HTTP = "http://www.mulesoft.org/schema/mule/http"
MULE_NS_PREFIX = "http://www.mulesoft.org/schema/mule/"
COMPATIBILITY = "http://www.mulesoft.org/schema/mule/compatibility"
# Studio's doc:name namespace; Mule ignores it and it has no schema location.
DOCUMENTATION = "http://www.mulesoft.org/schema/mule/documentation"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
XML_NS = "http://www.w3.org/XML/1998/namespace"
POM = "http://maven.apache.org/POM/4.0.0"
SCHEMA_LOCATIONS = {
    CORE: "http://www.mulesoft.org/schema/mule/core/current/mule.xsd",
    HTTP: "http://www.mulesoft.org/schema/mule/http/current/mule-http.xsd",
}
MULESOFT_REPOS = {
    "https://repository.mulesoft.org/releases/",
    "https://repository.mulesoft.org/nexus/content/repositories/public/",
}
PLACEHOLDER = re.compile(r"\$\{([^}]*)\}")

DESCRIPTION = 'Orders & Billing <beta> "test"'


# ---------------------------------------------------------------- hand-made bundles


@dataclass(frozen=True)
class Route:
    name: str
    condition: str | None = None
    target: str | None = None


@dataclass(frozen=True)
class Endpoint:
    name: str
    base_path: str | None
    routes: tuple[Route, ...]
    request_steps: tuple[str, ...] = ()


@dataclass(frozen=True)
class Target:
    name: str
    url: str | None
    properties: Mapping[str, str] = field(default_factory=dict)
    connection_extra: str = ""


XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
EMPTY_FLOW = "    <{tag} name={name}>\n        <Request/>\n        <Response/>\n    </{tag}>\n"


def write_proxy(
    parent: Path,
    name: str,
    *,
    endpoints: Sequence[Endpoint],
    targets: Sequence[Target] = (),
    policies: Mapping[str, str] | None = None,
    description: str = "a2m CP3 fixture",
) -> Path:
    """Write parent/<name>/apiproxy/... and return parent/<name>; ``policies`` maps policy file stem to XML."""
    policies = dict(policies or {})
    files: dict[str, str] = {}
    descriptor = [XML_HEAD, f"<APIProxy revision=\"1\" name={quoteattr(name)}>\n"]
    descriptor.append(f"    <Description>{escape(description)}</Description>\n")
    descriptor.append(f"    <DisplayName>{escape(name)}</DisplayName>\n")
    if policies:
        descriptor.append("    <Policies>\n")
        descriptor += [f"        <Policy>{escape(stem)}</Policy>\n" for stem in policies]
        descriptor.append("    </Policies>\n")
    else:
        descriptor.append("    <Policies/>\n")
    descriptor.append("    <ProxyEndpoints>\n")
    descriptor += [f"        <ProxyEndpoint>{escape(e.name)}</ProxyEndpoint>\n" for e in endpoints]
    descriptor.append("    </ProxyEndpoints>\n")
    if targets:
        descriptor.append("    <TargetEndpoints>\n")
        descriptor += [f"        <TargetEndpoint>{escape(t.name)}</TargetEndpoint>\n" for t in targets]
        descriptor.append("    </TargetEndpoints>\n")
    descriptor.append("</APIProxy>\n")
    files[f"apiproxy/{name}.xml"] = "".join(descriptor)

    for endpoint in endpoints:
        steps = "".join(
            f"            <Step>\n                <Name>{escape(step)}</Name>\n            </Step>\n"
            for step in endpoint.request_steps
        )
        request = f"        <Request>\n{steps}        </Request>\n" if steps else "        <Request/>\n"
        text = [XML_HEAD, f"<ProxyEndpoint name={quoteattr(endpoint.name)}>\n"]
        text.append(f'    <PreFlow name="PreFlow">\n{request}        <Response/>\n    </PreFlow>\n')
        text.append("    <Flows/>\n")
        text.append(EMPTY_FLOW.format(tag="PostFlow", name='"PostFlow"'))
        text.append("    <HTTPProxyConnection>\n")
        if endpoint.base_path is not None:
            text.append(f"        <BasePath>{escape(endpoint.base_path)}</BasePath>\n")
        text.append("        <VirtualHost>default</VirtualHost>\n    </HTTPProxyConnection>\n")
        for route in endpoint.routes:
            text.append(f"    <RouteRule name={quoteattr(route.name)}>\n")
            if route.condition is not None:
                text.append(f"        <Condition>{escape(route.condition)}</Condition>\n")
            if route.target is not None:
                text.append(f"        <TargetEndpoint>{escape(route.target)}</TargetEndpoint>\n")
            text.append("    </RouteRule>\n")
        text.append("</ProxyEndpoint>\n")
        files[f"apiproxy/proxies/{endpoint.name}.xml"] = "".join(text)

    for target in targets:
        text = [XML_HEAD, f"<TargetEndpoint name={quoteattr(target.name)}>\n"]
        text.append(EMPTY_FLOW.format(tag="PreFlow", name='"PreFlow"'))
        text.append("    <Flows/>\n")
        text.append(EMPTY_FLOW.format(tag="PostFlow", name='"PostFlow"'))
        text.append("    <HTTPTargetConnection>\n")
        if target.properties:
            text.append("        <Properties>\n")
            text += [
                f"            <Property name={quoteattr(key)}>{escape(value)}</Property>\n"
                for key, value in target.properties.items()
            ]
            text.append("        </Properties>\n")
        if target.connection_extra:
            text.append(target.connection_extra)
        if target.url is not None:
            text.append(f"        <URL>{escape(target.url)}</URL>\n")
        text.append("    </HTTPTargetConnection>\n</TargetEndpoint>\n")
        files[f"apiproxy/targets/{target.name}.xml"] = "".join(text)

    for stem, xml in policies.items():
        files[f"apiproxy/policies/{stem}.xml"] = xml

    root = parent / name
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def single(name: str, base_path: str | None, url: str | None, **extra: Any) -> dict[str, Any]:
    """One ProxyEndpoint 'default' with an unconditional RouteRule to one TargetEndpoint 'default'."""
    target = Target("default", url, extra.pop("target_properties", {}), extra.pop("connection_extra", ""))
    return {
        "endpoints": [Endpoint("default", base_path, (Route("default", None, "default"),), extra.pop("steps", ()))],
        "targets": [target],
        **extra,
    }


CALL_MISSING_POLICY = (
    XML_HEAD + '<FlowCallout async="false" continueOnError="false" enabled="true" name="CallMissing">\n'
    "    <DisplayName>Call Missing</DisplayName>\n"
    "    <SharedFlowBundle>DoesNotExist</SharedFlowBundle>\n"
    "</FlowCallout>\n"
)
BETA_CONDITION = 'request.header.x-beta = "true"'
CANARY_CONDITION = 'request.queryparam.canary = "1"'

HAND_MADE: dict[str, dict[str, Any]] = {
    "orders-simple": single("orders-simple", "/v1/orders", "https://api.example.com:8443/v2/orders"),
    "orders-multi": {
        "endpoints": [
            Endpoint("default", "/orders", (Route("default", None, "orders-backend"),)),
            Endpoint("admin", "/orders-admin", (Route("default", None, "admin-backend"),)),
        ],
        "targets": [
            Target("orders-backend", "http://orders.internal:8080/api"),
            Target("admin-backend", "https://admin.example.com/v1"),
        ],
    },
    "root-path": single("root-path", "/", "http://backend.example.com/api"),
    "trailing-slash": single("trailing-slash", "/v1/orders/", "http://backend.example.com/api"),
    "no-basepath": single("no-basepath", None, "http://backend.example.com/api"),
    "default-ports-https": single("default-ports-https", "/secure", "https://secure.example.com/api"),
    "default-ports-http": single("default-ports-http", "/plain", "http://backend.example.com/api"),
    "Orders_API.v2": single("Orders_API.v2", "/orders&co", "http://backend.example.com/api", description=DESCRIPTION),
    "no-target": {"endpoints": [Endpoint("default", "/no-target", (Route("noroute"),))], "targets": []},
    "target-server": single(
        "target-server",
        "/served",
        None,
        connection_extra=(
            "        <LoadBalancer>\n"
            '            <Server name="backend-1"/>\n'
            "        </LoadBalancer>\n"
            "        <Path>/api</Path>\n"
        ),
    ),
    "missing-sharedflow": single(
        "missing-sharedflow",
        "/missing",
        "http://backend.example.com/api",
        steps=("CallMissing",),
        policies={"CallMissing": CALL_MISSING_POLICY},
    ),
    "orders-routed": {
        "endpoints": [
            Endpoint(
                "default",
                "/orders",
                (
                    Route("beta", BETA_CONDITION, "beta-backend"),
                    Route("canary", CANARY_CONDITION, "canary-backend"),
                    Route("default", None, "default"),
                ),
            )
        ],
        "targets": [
            Target("beta-backend", "http://beta.internal:8081/api"),
            Target("canary-backend", "http://canary.internal:8082/api"),
            Target("default", "http://orders.internal:8080/api"),
        ],
    },
    "target-props": single(
        "target-props",
        "/props",
        "http://backend.example.com/api",
        target_properties={"io.timeout.millis": "5000", "request.streaming.enabled": "true"},
    ),
}


def make_fixture(parent: Path, name: str) -> Path:
    spec = dict(HAND_MADE[name])
    return write_proxy(parent, name, **spec)


# ---------------------------------------------------------------- a2m entry points


def read_bundle(path: Path) -> Any:
    from a2m.parser import read_bundle as _read_bundle

    return _read_bundle(path)


def generate(bundle: Any, dest: Path, shared: Sequence[Any] = ()) -> Any:
    from a2m.generator import generate_project

    return generate_project(bundle, dest, shared_flows=tuple(shared))


def gen_fixture(tmp_path: Path, name: str) -> tuple[Path, Any, Any]:
    """Write the hand-made bundle, parse it and generate tmp_path/<name>/mule-app. Returns (dir, result, bundle)."""
    bundle = read_bundle(make_fixture(tmp_path / "bundles", name))
    dest = tmp_path / name / "mule-app"
    return dest, generate(bundle, dest), bundle


def gen_test_api(tmp_path: Path, dest: Path | None = None, source: Path | None = None) -> tuple[Path, Any]:
    source = source if source is not None else FIXTURES / "azure"
    bundle = read_bundle(source / "Test-API")
    shared = read_bundle(source / "GetSharedFlow")
    dest = dest if dest is not None else tmp_path / "Test-API" / "mule-app"
    return dest, generate(bundle, dest, [shared])


def unsupported(result: Any) -> list[tuple[str, str]]:
    return [(str(item.name), str(item.reason)) for item in result.unsupported]


# ---------------------------------------------------------------- reading the project


def load_properties(path: Path) -> dict[str, str]:
    """Read a Java .properties file the way java.util.Properties does (last definition wins)."""
    logical: list[str] = []
    pending = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.lstrip()
        if not pending and (not line or line[0] in "#!"):
            continue
        trailing = len(line) - len(line.rstrip("\\"))
        if trailing % 2 == 1:
            pending += line[:-1]
            continue
        logical.append(pending + line)
        pending = ""
    if pending:
        logical.append(pending)

    props: dict[str, str] = {}
    for line in logical:
        key_chars: list[str] = []
        i = 0
        while i < len(line):
            char = line[i]
            if char == "\\" and i + 1 < len(line):
                key_chars.append(line[i : i + 2])
                i += 2
                continue
            if char in "=: \t\f":
                break
            key_chars.append(char)
            i += 1
        rest = line[i:].lstrip(" \t\f")
        if rest[:1] in ("=", ":"):
            rest = rest[1:].lstrip(" \t\f")
        props[_unescape("".join(key_chars))] = _unescape(rest)
    return props


def _unescape(text: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(text):
        char = text[i]
        if char != "\\" or i + 1 >= len(text):
            out.append(char)
            i += 1
            continue
        nxt = text[i + 1]
        if nxt == "u" and i + 6 <= len(text):
            out.append(chr(int(text[i + 2 : i + 6], 16)))
            i += 6
            continue
        out.append({"t": "\t", "n": "\n", "r": "\r", "f": "\f"}.get(nxt, nxt))
        i += 2
    return "".join(out)


def resolve(text: str, props: Mapping[str, str]) -> str:
    """Replace every ${key} with its property value; an unknown key fails the test."""

    def lookup(match: re.Match[str]) -> str:
        key = match.group(1)
        assert key in props, f"placeholder ${{{key}}} has no value in the properties file (keys: {sorted(props)})"
        return props[key]

    return PLACEHOLDER.sub(lookup, text)


def parse_xml(path: Path) -> ET.Element:
    try:
        return ET.parse(path).getroot()
    except ET.ParseError as exc:  # pragma: no cover - the assertion message is the point
        raise AssertionError(f"{path} is not well-formed XML: {exc}") from exc


def strings_with_comments(path: Path) -> list[str]:
    """Every attribute value, text, tail and comment in an XML file, read back through the parser."""
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    root = ET.parse(path, parser=parser).getroot()
    found: list[str] = []
    for element in root.iter():
        found += list(element.attrib.values())
        if element.text:
            found.append(element.text)
        if element.tail:
            found.append(element.tail)
    return found


def tag(ns: str, local: str) -> str:
    return f"{{{ns}}}{local}"


def is_element(element: ET.Element) -> bool:
    return isinstance(element.tag, str)


class Project:
    """A generated mule-app folder, read the way Mule reads it."""

    def __init__(self, root: Path) -> None:
        assert root.is_dir(), f"no project folder at {root}"
        self.root = root
        self.mule_dir = root / "src" / "main" / "mule"
        self.flow_files = sorted(self.mule_dir.glob("*.xml"))
        assert self.flow_files, f"no flow XML under {self.mule_dir}"
        props = sorted(p for p in (root / "src" / "main" / "resources").rglob("*.properties") if p.is_file())
        assert len(props) == 1, f"expected exactly one properties file, found {[str(p) for p in props]}"
        self.props_file = props[0]
        self.props = load_properties(self.props_file)
        self.docs = {path.name: parse_xml(path) for path in self.flow_files}

    def resolve(self, value: str | None) -> str | None:
        return None if value is None else resolve(value, self.props)

    def all_elements(self) -> Iterator[ET.Element]:
        for doc in self.docs.values():
            yield from (e for e in doc.iter() if is_element(e))

    def find(self, ns: str, local: str) -> list[ET.Element]:
        return [e for e in self.all_elements() if e.tag == tag(ns, local)]

    def globals(self) -> dict[str, ET.Element]:
        found: dict[str, ET.Element] = {}
        for doc in self.docs.values():
            for child in doc:
                if is_element(child) and child.get("name") is not None:
                    found[str(child.get("name"))] = child
        return found

    def callables(self) -> dict[str, ET.Element]:
        return {
            name: e for name, e in self.globals().items() if e.tag in (tag(CORE, "flow"), tag(CORE, "sub-flow"))
        }

    def walk(self, element: ET.Element, seen: tuple[str, ...] = ()) -> Iterator[ET.Element]:
        """Elements in execution order: document order, with each flow-ref expanded in place."""
        if not is_element(element):
            return
        yield element
        if element.tag == tag(CORE, "flow-ref"):
            name = element.get("name") or ""
            target = self.callables().get(name)
            if target is not None and name not in seen:
                for child in target:
                    yield from self.walk(child, (*seen, name))
        for child in element:
            yield from self.walk(child, seen)

    def listener_flows(self) -> list[ET.Element]:
        return [
            flow
            for flow in self.find(CORE, "flow")
            if any(is_element(c) and c.tag == tag(HTTP, "listener") for c in flow)
        ]

    def main_flow(self) -> ET.Element:
        flows = self.listener_flows()
        assert len(flows) == 1, f"expected one listening flow, found {len(flows)}"
        return flows[0]

    def listener(self, flow: ET.Element) -> ET.Element:
        found = [c for c in flow if is_element(c) and c.tag == tag(HTTP, "listener")]
        assert len(found) == 1
        return found[0]

    def listener_path(self, flow: ET.Element) -> str:
        path = self.resolve(self.listener(flow).get("path"))
        assert path is not None, "http:listener has no path"
        return path

    def requests(self, element: ET.Element) -> list[ET.Element]:
        return [e for e in self.walk(element) if e.tag == tag(HTTP, "request")]

    def request_target(self, request: ET.Element) -> dict[str, str | None]:
        ref = self.resolve(request.get("config-ref"))
        config = self.globals().get(ref or "")
        assert config is not None and config.tag == tag(HTTP, "request-config"), (
            f"http:request config-ref {ref!r} names no http:request-config"
        )
        connections = [c for c in config if is_element(c) and c.tag == tag(HTTP, "request-connection")]
        assert len(connections) == 1, f"request-config {ref!r} has {len(connections)} http:request-connection"
        conn = connections[0]
        base = config.get("basePath") if config.get("basePath") is not None else conn.get("basePath")
        timeout = request.get("responseTimeout")
        if timeout is None:
            timeout = config.get("responseTimeout")
        return {
            "protocol": self.resolve(conn.get("protocol")),
            "host": self.resolve(conn.get("host")),
            "port": self.resolve(conn.get("port")),
            "basePath": self.resolve(base),
            "responseTimeout": self.resolve(timeout),
            "raw_host": conn.get("host"),
            "raw_port": conn.get("port"),
            "raw_protocol": conn.get("protocol"),
            "raw_basePath": base,
        }


def address(project: Project, request: ET.Element) -> tuple[str | None, str | None, str | None, str | None]:
    target = project.request_target(request)
    host = target["host"].lower() if target["host"] is not None else None
    return target["protocol"], host, target["port"], target["basePath"]


def check_references(project: Project) -> None:
    """Every placeholder, config-ref and flow-ref in the flow files points at something defined."""
    configs = project.globals()
    callables = project.callables()
    for element in project.all_elements():
        for value in list(element.attrib.values()) + [element.text or ""]:
            for key in PLACEHOLDER.findall(value):
                assert key in project.props, f"placeholder ${{{key}}} is not defined in {project.props_file.name}"
        ref = element.get("config-ref")
        if ref is not None:
            name = project.resolve(ref)
            assert name in configs, f"config-ref {ref!r} names no global config"
            assert configs[name].tag not in (tag(CORE, "flow"), tag(CORE, "sub-flow"))
        if element.tag == tag(CORE, "flow-ref"):
            name = element.get("name")
            assert name in callables, f"flow-ref {name!r} names no flow or sub-flow in the project"


def assert_well_formed(root: Path) -> Project:
    assert (root / "pom.xml").is_file()
    parse_xml(root / "pom.xml")
    artifact = json.loads((root / "mule-artifact.json").read_text(encoding="utf-8"))
    assert isinstance(artifact, dict)
    for path in sorted(root.rglob("*.xml")):
        parse_xml(path)
    project = Project(root)
    for name, doc in project.docs.items():
        assert doc.tag == tag(CORE, "mule"), f"{name} root is {doc.tag}"
    check_references(project)
    return project


def tree_files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def canonical(path: Path) -> str:
    return ET.canonicalize(from_file=str(path), with_comments=True, strip_text=True)


def assert_matches_golden(actual_dir: Path, golden_dir: Path) -> None:
    """Compare a generated tree with its golden copy; rewrite the copy only when A2M_UPDATE_GOLDEN=1."""
    if os.environ.get(UPDATE_GOLDEN_ENV) == "1":
        if golden_dir.exists():
            shutil.rmtree(golden_dir)
        shutil.copytree(actual_dir, golden_dir)
        return
    assert golden_dir.is_dir(), (
        f"golden folder {golden_dir} is missing; generate it with {UPDATE_GOLDEN_ENV}=1 and review it"
    )
    actual, golden = tree_files(actual_dir), tree_files(golden_dir)
    assert actual == golden, (
        f"file lists differ: only generated {sorted(set(actual) - set(golden))}, "
        f"only in golden {sorted(set(golden) - set(actual))}"
    )
    differing = []
    for rel in actual:
        a, g = actual_dir / rel, golden_dir / rel
        same = canonical(a) == canonical(g) if rel.endswith(".xml") else a.read_bytes() == g.read_bytes()
        if not same:
            differing.append(rel)
    assert not differing, f"generated files differ from {golden_dir}: {differing}"


# ---------------------------------------------------------------- tests


def test_CP3_T01_each_proxy_gets_a_complete_mule_project_folder(tmp_path: Path) -> None:
    """[CP3-T01] Each proxy gets a complete Mule project folder."""
    root, _, _ = gen_fixture(tmp_path, "orders-simple")

    assert root == tmp_path / "orders-simple" / "mule-app"
    assert (root / "pom.xml").is_file()
    assert (root / "mule-artifact.json").is_file()
    flows = sorted((root / "src" / "main" / "mule").glob("*.xml"))
    assert len(flows) >= 1
    resources = root / "src" / "main" / "resources"
    props = sorted(p for p in resources.rglob("*.properties") if p.is_file())
    assert len(props) == 1, props
    rel = props[0].relative_to(resources).as_posix()

    loaded = [
        e.get("file")
        for path in flows
        for e in parse_xml(path).iter(tag(CORE, "configuration-properties"))
    ]
    assert rel in loaded, f"no <configuration-properties file={rel!r}> in the flow files (found {loaded})"


def test_CP3_T02_the_mule_flow_listens_on_the_proxys_base_path(tmp_path: Path) -> None:
    """[CP3-T02] The Mule flow listens on the proxy's base path."""
    simple, _, _ = gen_fixture(tmp_path, "orders-simple")
    test_api, _ = gen_test_api(tmp_path)

    for root, expected in ((simple, "/v1/orders/*"), (test_api, "/profile/*")):
        project = Project(root)
        flow = project.main_flow()
        assert project.listener_path(flow) == expected
        ref = project.resolve(project.listener(flow).get("config-ref"))
        config = project.globals().get(ref or "")
        assert config is not None and config.tag == tag(HTTP, "listener-config"), ref
        connection = config.find(tag(HTTP, "listener-connection"))
        assert connection is not None
        raw_port = connection.get("port")
        assert raw_port is not None and PLACEHOLDER.search(raw_port), f"listener port {raw_port!r} is not a property"
        port = project.resolve(raw_port)
        assert port is not None and port.isdigit() and 0 < int(port) < 65536, port


def test_CP3_T03_requests_forwarded_to_bundle_target_kept_in_properties(tmp_path: Path) -> None:
    """[CP3-T03] Requests are forwarded to the target address from the bundle, kept in the properties file."""
    simple, _, _ = gen_fixture(tmp_path, "orders-simple")
    project = Project(simple)
    (request,) = project.requests(project.main_flow())
    target = project.request_target(request)

    assert address(project, request) == ("HTTPS", "api.example.com", "8443", "/v2/orders")
    for key in ("raw_protocol", "raw_host", "raw_port", "raw_basePath"):
        assert target[key] is not None and PLACEHOLDER.search(str(target[key])), f"{key} {target[key]!r}"
    values = set(project.props.values())
    assert {"HTTPS", "api.example.com", "8443", "/v2/orders"} <= values
    for path in project.flow_files:
        assert "api.example.com" not in path.read_text(encoding="utf-8"), path.name

    test_api, _ = gen_test_api(tmp_path)
    api = Project(test_api)
    (api_request,) = api.requests(api.main_flow())
    protocol, host, port, _ = address(api, api_request)
    assert (protocol, host, port) == ("HTTPS", "setindynamicurlsharedflow", "443")


def test_CP3_T04_forwarding_keeps_method_path_query_and_headers(tmp_path: Path) -> None:
    """[CP3-T04] Forwarding keeps the caller's method, path below the base path, query string and headers."""
    root, _, _ = gen_fixture(tmp_path, "orders-simple")
    project = Project(root)
    (request,) = project.requests(project.main_flow())

    method = request.get("method") or ""
    assert method.startswith("#[") and "attributes.method" in method, method
    path = request.get("path") or ""
    assert path.startswith("#[") and "attributes." in path, f"request path {path!r} is not built from the request"
    inside = " ".join(
        [v for e in request.iter() for v in e.attrib.values()] + [e.text or "" for e in request.iter()]
    )
    assert "attributes.queryParams" in inside, "incoming query parameters are not passed on"
    assert "attributes.headers" in inside, "incoming headers are not passed on"


def test_CP3_T05_every_placeholder_and_reference_points_to_something_that_exists(tmp_path: Path) -> None:
    """[CP3-T05] Every placeholder and reference in the project points to something that exists."""
    simple, _, _ = gen_fixture(tmp_path, "orders-simple")
    multi, _, _ = gen_fixture(tmp_path, "orders-multi")
    test_api, _ = gen_test_api(tmp_path)

    for root in (simple, multi, test_api):
        project = Project(root)
        check_references(project)
        refs = [e for e in project.all_elements() if e.get("config-ref") is not None]
        assert refs, f"{root}: no config-ref at all"


def test_CP3_T06_generated_xml_parses_and_uses_official_mule4_namespaces(tmp_path: Path) -> None:
    """[CP3-T06] Generated XML opens cleanly and uses the official Mule 4 namespaces."""
    simple, _, _ = gen_fixture(tmp_path, "orders-simple")
    test_api, _ = gen_test_api(tmp_path)

    for root in (simple, test_api):
        xml_files = sorted(root.rglob("*.xml"))
        assert root / "pom.xml" in xml_files
        for path in xml_files:
            parse_xml(path)
        for path in sorted((root / "src" / "main" / "mule").glob("*.xml")):
            doc = parse_xml(path)
            assert doc.tag == tag(CORE, "mule"), f"{path.name}: root {doc.tag}"
            declared = {uri for _, (_, uri) in ET.iterparse(path, events=("start-ns",))}
            used = set()
            for element in doc.iter():
                for name in [element.tag, *element.attrib]:
                    if isinstance(name, str) and name.startswith("{"):
                        used.add(name[1:].split("}", 1)[0])
            pairs = (doc.get(tag(XSI, "schemaLocation")) or "").split()
            assert len(pairs) % 2 == 0, f"{path.name}: odd xsi:schemaLocation"
            locations = dict(zip(pairs[::2], pairs[1::2], strict=True))
            for uri in sorted((declared | used) - {XSI, XML_NS}):
                assert uri.startswith(MULE_NS_PREFIX), f"{path.name}: namespace {uri} is not a Mule namespace"
                assert uri != COMPATIBILITY, f"{path.name}: uses the compatibility namespace"
                if uri == DOCUMENTATION:
                    continue
                assert uri in locations, f"{path.name}: namespace {uri} has no xsi:schemaLocation"
                if uri in SCHEMA_LOCATIONS:
                    assert locations[uri] == SCHEMA_LOCATIONS[uri]
                else:
                    assert locations[uri].startswith(uri + "/current/") and locations[uri].endswith(".xsd")
            assert CORE in used and HTTP in used
            assert "mel:" not in path.read_text(encoding="utf-8"), f"{path.name} has a MEL expression"


def _text(element: ET.Element, path: str) -> str:
    found = element.find(path, {"m": POM})
    assert found is not None and found.text and found.text.strip(), f"pom has no {path}"
    return found.text.strip()


def test_CP3_T07_pom_is_a_mule_application_with_proven_versions(tmp_path: Path) -> None:
    """[CP3-T07] pom.xml is a Mule application with the versions proven on this machine."""
    root, _, _ = gen_fixture(tmp_path, "orders-simple")
    pom = parse_xml(root / "pom.xml")
    ns = {"m": POM}

    assert pom.tag == tag(POM, "project")
    assert _text(pom, "m:packaging") == "mule-application"
    _text(pom, "m:groupId")
    _text(pom, "m:version")
    assert _text(pom, "m:artifactId") == "orders-simple"

    plugins = {
        (_text(p, "m:groupId"), _text(p, "m:artifactId")): p for p in pom.findall("m:build/m:plugins/m:plugin", ns)
    }
    plugin = plugins.get(("org.mule.tools.maven", "mule-maven-plugin"))
    assert plugin is not None, f"no mule-maven-plugin in {sorted(plugins)}"
    assert _text(plugin, "m:version") == "4.10.1"
    assert _text(plugin, "m:extensions") == "true"

    deps = {(_text(d, "m:groupId"), _text(d, "m:artifactId")): d for d in pom.findall("m:dependencies/m:dependency", ns)}
    http = deps.get(("org.mule.connectors", "mule-http-connector"))
    assert http is not None, f"no mule-http-connector in {sorted(deps)}"
    assert _text(http, "m:version") == "1.11.3"
    assert _text(http, "m:classifier") == "mule-plugin"
    pinned = {
        ("org.mule.connectors", "mule-objectstore-connector"): "1.2.2",
        ("org.mule.modules", "mule-validation-module"): "2.0.9",
    }
    for coords, version in pinned.items():
        if coords in deps:
            assert _text(deps[coords], "m:version") == version, coords

    repos = {(r.text or "").strip() for r in pom.findall("m:repositories/m:repository/m:url", ns)}
    plugin_repos = {(r.text or "").strip() for r in pom.findall("m:pluginRepositories/m:pluginRepository/m:url", ns)}
    assert MULESOFT_REPOS <= repos, repos
    assert MULESOFT_REPOS <= plugin_repos, plugin_repos

    for item in list(deps.values()) + list(plugins.values()):
        version = _text(item, "m:version")
        assert "SNAPSHOT" not in version and version not in ("LATEST", "RELEASE"), version
        assert not re.search(r"[\[\](),]", version) and "${" not in version, version


def test_CP3_T08_mule_artifact_json_targets_490_java17_and_lists_every_flow(tmp_path: Path) -> None:
    """[CP3-T08] mule-artifact.json is valid, targets Mule 4.9.0 on Java 17, and lists every flow file."""
    simple, _, _ = gen_fixture(tmp_path, "orders-simple")
    test_api, _ = gen_test_api(tmp_path)

    for root in (simple, test_api):
        artifact = json.loads((root / "mule-artifact.json").read_text(encoding="utf-8"))
        assert isinstance(artifact, dict)
        assert artifact["minMuleVersion"] == "4.9.0"
        assert artifact["requiredProduct"] == "MULE"
        assert artifact["javaSpecificationVersions"] == ["17"]
        flows = sorted(p.name for p in (root / "src" / "main" / "mule").glob("*.xml"))
        assert artifact["configs"] == flows


def test_CP3_T09_generating_the_same_proxy_twice_gives_identical_files(tmp_path: Path) -> None:
    """[CP3-T09] Generating the same proxy twice gives identical files."""
    source = tmp_path / "input"
    shutil.copytree(TEST_API, source / "Test-API")
    shutil.copytree(GET_SHARED_FLOW, source / "GetSharedFlow")

    a, _ = gen_test_api(tmp_path, tmp_path / "a" / "mule-app", source)
    b, _ = gen_test_api(tmp_path, tmp_path / "b" / "mule-app", source)

    files = tree_files(a)
    assert files and files == tree_files(b)
    today = dt.datetime.now().astimezone().date()
    forbidden = [str(tmp_path), str(source), str(FIXTURES), today.isoformat(), today.strftime("%Y%m%d")]
    for rel in files:
        data = (a / rel).read_bytes()
        assert data == (b / rel).read_bytes(), rel
        text = data.decode("utf-8", errors="replace")
        for needle in forbidden:
            assert needle not in text, f"{rel} contains {needle!r}"


def test_CP3_T10_generated_projects_match_the_saved_reference_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[CP3-T10] Generated projects match the saved reference copies."""
    simple, _, _ = gen_fixture(tmp_path, "orders-simple")
    test_api, _ = gen_test_api(tmp_path)

    assert_matches_golden(test_api, GOLDEN / "Test-API")
    assert_matches_golden(simple, GOLDEN / "orders-simple")
    monkeypatch.delenv(UPDATE_GOLDEN_ENV, raising=False)
    assert_matches_golden(test_api, GOLDEN / "Test-API")
    assert_matches_golden(simple, GOLDEN / "orders-simple")


def test_CP3_T11_shared_flow_becomes_a_sub_flow_called_before_the_target(tmp_path: Path) -> None:
    """[CP3-T11] A shared flow becomes a sub-flow called at the same point in the flow."""
    root, _ = gen_test_api(tmp_path)
    project = Project(root)

    subflows = [e for e in project.find(CORE, "sub-flow") if "getsharedflow" in (e.get("name") or "").lower()]
    assert len(subflows) == 1, [e.get("name") for e in project.find(CORE, "sub-flow")]
    name = subflows[0].get("name")

    order = list(project.walk(project.main_flow()))
    calls = [i for i, e in enumerate(order) if e.tag == tag(CORE, "flow-ref") and e.get("name") == name]
    requests = [i for i, e in enumerate(order) if e.tag == tag(HTTP, "request")]
    assert calls, f"the main flow never reaches a flow-ref to {name!r}"
    assert requests, "the main flow has no http:request"
    assert calls[0] < requests[0], "the shared flow is called after the target request"
    assert not (tmp_path / "GetSharedFlow").exists()
    assert sorted(p.parent.name for p in tmp_path.rglob("mule-app")) == ["Test-API"]


def _project_dirs(out: Path, proxy: str) -> list[Path]:
    return sorted(p for p in out.rglob("mule-app") if p.is_dir() and p.parent.name == proxy)


def test_CP3_T12_migrate_produces_a_mule_project_for_every_readable_proxy(tmp_path: Path, run_cli: Any) -> None:
    """[CP3-T12] Running a2m migrate produces a Mule project for every readable proxy."""
    exports = tmp_path / "exports"
    for source in (TEST_API, GET_SHARED_FLOW, ORDERS_API, AUDIT_FLOW):
        shutil.copytree(source, exports / source.name)
    make_fixture(exports, "orders-simple")
    out = tmp_path / "out"

    res = run_cli(["migrate", str(exports), "--out", str(out), "--llm", "fake", "--no-runtime"])

    assert res.code == 0, res.err
    for proxy in ("Test-API", "orders-simple", "orders-api"):
        poms = sorted(out.glob(f"**/{proxy}/mule-app/pom.xml"))
        assert len(poms) == 1, f"{proxy}: {poms}"
        for path in sorted(poms[0].parent.rglob("*.xml")):
            parse_xml(path)
    done = {p.parent.name for p in out.rglob(".done") if p.is_file()}
    assert {"Test-API", "orders-simple", "orders-api"} <= done


def test_CP3_T13_reference_copies_rewritten_only_with_the_update_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[CP3-T13] Reference copies are only rewritten when the update switch is on."""
    assert (GOLDEN / "orders-simple").is_dir(), "tests/golden/cp3/orders-simple is missing"
    copy = tmp_path / "golden-copy"
    shutil.copytree(GOLDEN / "orders-simple", copy)
    (props,) = sorted(copy.rglob("*.properties"))
    lines = props.read_text(encoding="utf-8").splitlines(keepends=True)
    index = next(i for i, line in enumerate(lines) if "=" in line and not line.lstrip().startswith(("#", "!")))
    lines[index] = lines[index].rstrip("\r\n") + "-edited\n"
    edited = "".join(lines)
    props.write_text(edited, encoding="utf-8")
    fresh, _, _ = gen_fixture(tmp_path, "orders-simple")

    monkeypatch.delenv(UPDATE_GOLDEN_ENV, raising=False)
    with pytest.raises(AssertionError) as failure:
        assert_matches_golden(fresh, copy)
    assert props.relative_to(copy).as_posix() in str(failure.value)
    assert props.read_text(encoding="utf-8") == edited

    monkeypatch.setenv(UPDATE_GOLDEN_ENV, "1")
    assert_matches_golden(fresh, copy)
    monkeypatch.delenv(UPDATE_GOLDEN_ENV)
    assert_matches_golden(fresh, copy)
    assert props.read_bytes() == (fresh / props.relative_to(copy)).read_bytes()

    missing = tmp_path / "no-golden"
    with pytest.raises(AssertionError):
        assert_matches_golden(fresh, missing)
    assert not missing.exists()


def test_CP3_T14_missing_shared_flow_is_flagged_not_left_dangling(tmp_path: Path) -> None:
    """[CP3-T14] A call to a shared flow that is not in the folder is flagged, not left dangling."""
    root, result, _ = gen_fixture(tmp_path, "missing-sharedflow")

    assert_well_formed(root)
    flagged = [(name, reason) for name, reason in unsupported(result) if name == "CallMissing"]
    assert len(flagged) == 1, unsupported(result)
    assert "DoesNotExist" in flagged[0][1]


def test_CP3_T15_two_endpoints_and_two_targets_each_forward_to_their_backend(tmp_path: Path) -> None:
    """[CP3-T15] A proxy with two endpoints and two targets forwards each one to its own backend."""
    root, _, _ = gen_fixture(tmp_path, "orders-multi")
    project = Project(root)

    flows = {project.listener_path(flow): flow for flow in project.listener_flows()}
    assert sorted(flows) == ["/orders-admin/*", "/orders/*"]
    (orders,) = project.requests(flows["/orders/*"])
    (admin,) = project.requests(flows["/orders-admin/*"])
    assert address(project, orders) == ("HTTP", "orders.internal", "8080", "/api")
    assert address(project, admin) == ("HTTPS", "admin.example.com", "443", "/v1")
    keys = []
    for request in (orders, admin):
        raw = project.request_target(request)["raw_host"] or ""
        found = PLACEHOLDER.findall(raw)
        assert len(found) == 1, f"host {raw!r} is not one property"
        keys.append(found[0])
    assert keys[0] != keys[1]


def test_CP3_T16_odd_base_paths_still_give_a_valid_listener_path(tmp_path: Path) -> None:
    """[CP3-T16] Odd base paths still give a valid listener path."""
    for name, expected in (("root-path", "/*"), ("trailing-slash", "/v1/orders/*"), ("no-basepath", "/*")):
        root, _, _ = gen_fixture(tmp_path, name)
        project = Project(root)
        path = project.listener_path(project.main_flow())
        assert path == expected, name
        assert "//" not in path


def test_CP3_T17_target_urls_without_a_port_get_the_standard_port(tmp_path: Path) -> None:
    """[CP3-T17] Target URLs without a port get the standard port."""
    for name, expected in (
        ("default-ports-https", ("HTTPS", "secure.example.com", "443", "/api")),
        ("default-ports-http", ("HTTP", "backend.example.com", "80", "/api")),
    ):
        root, _, _ = gen_fixture(tmp_path, name)
        project = Project(root)
        (request,) = project.requests(project.main_flow())
        assert address(project, request) == expected, name


def test_CP3_T18_special_characters_are_escaped_and_names_stay_valid(tmp_path: Path) -> None:
    """[CP3-T18] Special characters from the bundle are escaped and names stay valid."""
    root, _, _ = gen_fixture(tmp_path, "Orders_API.v2")

    project = assert_well_formed(root)
    assert project.listener_path(project.main_flow()) == "/orders&co/*"
    artifact_id = _text(parse_xml(root / "pom.xml"), "m:artifactId")
    assert re.fullmatch(r"[a-z0-9][a-z0-9._-]*", artifact_id), artifact_id
    for flow in project.find(CORE, "flow") + project.find(CORE, "sub-flow"):
        name = flow.get("name") or ""
        assert name and not set(name) & set("/[]{}#"), name
    for path in sorted(root.rglob("*.xml")):
        for element in parse_xml(path).iter():
            for value in [*element.attrib.values(), element.text or ""]:
                assert "&amp;" not in value and "&lt;" not in value, f"{path.name}: double-escaped {value!r}"
                if "Orders & Billing" in value:
                    assert DESCRIPTION in value, f"{path.name}: description reads back as {value!r}"


def test_CP3_T19_a_proxy_with_no_backend_gets_a_flow_that_does_not_call_one(tmp_path: Path) -> None:
    """[CP3-T19] A proxy with no backend gets a flow that does not call one."""
    root, _, _ = gen_fixture(tmp_path, "no-target")

    project = assert_well_formed(root)
    assert project.listener_path(project.main_flow()) == "/no-target/*"
    assert project.find(HTTP, "request") == []
    assert project.find(HTTP, "request-config") == []


def test_CP3_T20_a_target_with_no_fixed_url_is_flagged_not_guessed(tmp_path: Path) -> None:
    """[CP3-T20] A target with no fixed URL is flagged, not guessed."""
    root, result, _ = gen_fixture(tmp_path, "target-server")

    project = assert_well_formed(root)
    listener_keys = {
        key
        for config in project.find(HTTP, "listener-config")
        for e in config.iter()
        for value in e.attrib.values()
        for key in PLACEHOLDER.findall(value)
    }
    for key, value in project.props.items():
        if key in listener_keys:
            continue
        lowered = value.lower()
        assert "localhost" not in lowered and "127.0.0.1" not in lowered and "example" not in lowered, (key, value)
    for request in project.find(HTTP, "request"):
        host = project.request_target(request)["host"] or ""
        assert host.strip() and host.lower() not in ("localhost", "127.0.0.1"), host
    flagged = [(name, reason) for name, reason in unsupported(result) if name == "default"]
    assert len(flagged) == 1, unsupported(result)
    assert "backend-1" in flagged[0][1]


def test_CP3_T21_bad_bundles_and_shared_flows_get_no_mule_project(tmp_path: Path, run_cli: Any) -> None:
    """[CP3-T21] Bad bundles and shared flows do not get their own Mule project, and the batch goes on."""
    exports = tmp_path / "exports"
    for source in (TEST_API, GET_SHARED_FLOW, BROKEN_XML):
        shutil.copytree(source, exports / source.name)
    make_fixture(exports, "orders-simple")
    out = tmp_path / "out"

    res = run_cli(["migrate", str(exports), "--out", str(out), "--llm", "fake", "--no-runtime"])

    assert res.code == 0, res.err
    assert _project_dirs(out, "broken-xml") == []
    assert _project_dirs(out, "GetSharedFlow") == []
    log = (out / "run.log").read_text(encoding="utf-8").splitlines()
    assert any("broken-xml" in line and "error" in line.lower() for line in log), "\n".join(log)
    for proxy in ("Test-API", "orders-simple"):
        dirs = _project_dirs(out, proxy)
        assert len(dirs) == 1, f"{proxy}: {dirs}"
        assert_well_formed(dirs[0])


def test_CP3_T22_regenerating_over_an_older_project_leaves_no_stale_files(tmp_path: Path) -> None:
    """[CP3-T22] Regenerating over an older project leaves no stale flow files."""
    bundle = read_bundle(make_fixture(tmp_path / "bundles", "orders-simple"))
    dest = tmp_path / "orders-simple" / "mule-app"
    old_flow = dest / "src" / "main" / "mule" / "old-flow.xml"
    old_props = dest / "src" / "main" / "resources" / "old.properties"
    old_flow.parent.mkdir(parents=True)
    old_props.parent.mkdir(parents=True)
    old_flow.write_text(f'<?xml version="1.0"?>\n<mule xmlns="{CORE}"><flow name="old"/></mule>\n', encoding="utf-8")
    old_props.write_text("old.key=old\n", encoding="utf-8")

    generate(bundle, dest)
    fresh = tmp_path / "fresh" / "mule-app"
    generate(bundle, fresh)

    assert not old_flow.exists()
    assert not old_props.exists()
    assert tree_files(dest) == tree_files(fresh)


def test_CP3_T23_conditional_routes_become_a_choice_with_conditions_kept(tmp_path: Path) -> None:
    """[CP3-T23] Conditional routes become a choice in the flow, with conditions kept for later translation."""
    root, result, bundle = gen_fixture(tmp_path, "orders-routed")
    project = Project(root)
    conditions = {rule.name: rule.condition for rule in bundle.proxy_endpoints[0].route_rules}
    assert conditions["beta"] and conditions["canary"] and conditions["default"] is None

    choices = [e for e in project.walk(project.main_flow()) if e.tag == tag(CORE, "choice")]
    assert len(choices) == 1, f"expected one choice router, found {len(choices)}"
    branches = [c for c in choices[0] if is_element(c)]
    assert [b.tag for b in branches] == [tag(CORE, "when"), tag(CORE, "when"), tag(CORE, "otherwise")]

    expected = [
        ("HTTP", "beta.internal", "8081", "/api"),
        ("HTTP", "canary.internal", "8082", "/api"),
        ("HTTP", "orders.internal", "8080", "/api"),
    ]
    for branch, want in zip(branches, expected, strict=True):
        requests = project.requests(branch)
        assert len(requests) == 1, f"branch has {len(requests)} http:request"
        assert address(project, requests[0]) == want

    texts = [s for path in project.flow_files for s in strings_with_comments(path)]
    for name in ("beta", "canary"):
        condition = str(conditions[name])
        assert any(condition in s for s in texts), f"condition {condition!r} is not kept in the flow XML"
        for when in branches[:2]:
            assert condition not in (when.get("expression") or ""), "Apigee condition used as a Mule expression"

    pending = [(str(item.name), str(item.condition)) for item in result.pending]
    # Superseded by CP5: each conditional route is either untranslated (#[false] guard, listed pending
    # with the original condition) or translated (a non-constant DataWeave guard, not pending).
    # Never both, never neither.
    for name, when in zip(("beta", "canary"), branches[:2], strict=True):
        guard = re.sub(r"\s+", "", str(when.get("expression") or ""))
        route_pending = [entry for entry in pending if entry[0] == name]
        records = [c for c in result.conditions if str(c.name) == name]
        assert len(records) == 1, (
            f"expected one condition record for {name}: {[str(c.name) for c in result.conditions]}"
        )
        if guard == "#[false]":
            assert records[0].ok is False, f"route {name} has a #[false] guard but its condition is marked translated"
            assert str(records[0].original) == str(conditions[name]), records[0].original
            assert str(records[0].reason or "").strip(), f"untranslated route {name} has no reason"
        else:
            inner = guard[2:-1] if guard.startswith("#[") and guard.endswith("]") else ""
            assert inner not in ("", "true", "false"), f"guard is not a non-constant DataWeave expression: {guard}"
            assert records[0].ok is True, f"route {name} has a real guard but its condition is marked untranslated"
            assert not route_pending, f"a translated route condition is still listed as pending: {pending}"


def test_CP3_T24_target_timeout_carries_over_and_other_settings_are_flagged(tmp_path: Path) -> None:
    """[CP3-T24] The target's timeout setting carries over and other target settings are flagged."""
    test_api, _ = gen_test_api(tmp_path)
    props_root, props_result, _ = gen_fixture(tmp_path, "target-props")
    simple, _, _ = gen_fixture(tmp_path, "orders-simple")

    for root, expected in ((test_api, "180000"), (props_root, "5000"), (simple, None)):
        project = Project(root)
        (request,) = project.requests(project.main_flow())
        assert project.request_target(request)["responseTimeout"] == expected, root

    items = unsupported(props_result)
    assert any("request.streaming.enabled" in reason for _, reason in items), items
    assert not any("io.timeout.millis" in f"{name} {reason}" for name, reason in items), items


# ---------------------------------------------------------------- CP3 adversarial round 1 (CP3-T36 to CP3-T38)

import dataclasses  # noqa: E402

FLOW_COND = 'proxy.pathsuffix MatchesPath "/x"'
FAULT_COND = 'fault.name = "Boom"'


def _am_policy(name: str) -> str:
    return (
        XML_HEAD + f'<AssignMessage async="false" continueOnError="false" enabled="true" name={quoteattr(name)}>\n'
        '    <Set><Headers><Header name="X-Pos">1</Header></Headers></Set>\n'
        "</AssignMessage>\n"
    )


def _fc_policy(name: str, shared: str) -> str:
    return (
        XML_HEAD + f'<FlowCallout async="false" continueOnError="false" enabled="true" name={quoteattr(name)}>\n'
        f"    <SharedFlowBundle>{escape(shared)}</SharedFlowBundle>\n"
        "</FlowCallout>\n"
    )


def _steps_xml(names: Sequence[str]) -> str:
    return "".join(f"<Step><Name>{escape(n)}</Name></Step>" for n in names)


def _req_resp(request: Sequence[str], response: Sequence[str]) -> str:
    return f"<Request>{_steps_xml(request)}</Request><Response>{_steps_xml(response)}</Response>"


def _write_tree_files(root: Path, files: Mapping[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _all_positions_body(prefix: str, *, proxy: bool) -> tuple[str, dict[str, str]]:
    """An endpoint body with one AssignMessage step in every place Apigee runs steps; returns (xml, policies)."""
    names: dict[str, str] = {}

    def step(position: str) -> list[str]:
        name = f"AM-{prefix}-{position}"
        names[name] = _am_policy(name)
        return [name]

    extra = "PostClientFlow" if proxy else "EventFlow"
    body = (
        f'<PreFlow name="PreFlow">{_req_resp(step("pre-req"), step("pre-resp"))}</PreFlow>'
        f'<Flows><Flow name="f1"><Condition>{escape(FLOW_COND)}</Condition>'
        f"{_req_resp(step('flow-req'), step('flow-resp'))}</Flow></Flows>"
        f'<PostFlow name="PostFlow">{_req_resp(step("post-req"), step("post-resp"))}</PostFlow>'
        f"<{extra}>{_req_resp(step('extra-req'), step('extra-resp'))}</{extra}>"
        f'<FaultRules><FaultRule name="fr1"><Condition>{escape(FAULT_COND)}</Condition>'
        f"{_steps_xml(step('fault'))}</FaultRule></FaultRules>"
        f'<DefaultFaultRule name="dfr"><AlwaysEnforce>true</AlwaysEnforce>{_steps_xml(step("default-fault"))}'
        "</DefaultFaultRule>"
    )
    return body, names


def _write_proxy_files(
    parent: Path,
    name: str,
    proxy_body: str,
    targets: Mapping[str, str],
    policies: Mapping[str, str],
) -> Path:
    """One ProxyEndpoint 'default' (``proxy_body`` holds its flows and RouteRules) and the given target files."""
    head = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    listed = "".join(f"<Policy>{escape(p)}</Policy>" for p in policies)
    target_list = "".join(f"<TargetEndpoint>{escape(t)}</TargetEndpoint>" for t in targets)
    files = {
        f"apiproxy/{name}.xml": head + f"<APIProxy revision=\"1\" name={quoteattr(name)}>"
        f"<Policies>{listed}</Policies><ProxyEndpoints><ProxyEndpoint>default</ProxyEndpoint></ProxyEndpoints>"
        f"<TargetEndpoints>{target_list}</TargetEndpoints></APIProxy>\n",
        "apiproxy/proxies/default.xml": head + f'<ProxyEndpoint name="default">{proxy_body}</ProxyEndpoint>\n',
    }
    for target, text in targets.items():
        files[f"apiproxy/targets/{target}.xml"] = head + text
    for policy, xml in policies.items():
        files[f"apiproxy/policies/{policy}.xml"] = xml
    return _write_tree_files(parent / name, files)


def _write_shared_flow(parent: Path, name: str, flows: Mapping[str, Sequence[str]]) -> Path:
    """A shared flow bundle whose flows hold AssignMessage steps (``flows`` maps flow name to step names)."""
    head = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    policies = [step for steps in flows.values() for step in steps]
    files = {
        f"sharedflowbundle/{name}.xml": head + f'<SharedFlowBundle revision="1" name={quoteattr(name)}>'
        f"<Policies>{''.join(f'<Policy>{escape(p)}</Policy>' for p in policies)}</Policies>"
        f"<SharedFlows>{''.join(f'<SharedFlow>{escape(f)}</SharedFlow>' for f in flows)}</SharedFlows>"
        "</SharedFlowBundle>\n",
    }
    for flow, steps in flows.items():
        files[f"sharedflowbundle/sharedflows/{flow}.xml"] = (
            head + f"<SharedFlow name={quoteattr(flow)}>{_steps_xml(steps)}</SharedFlow>\n"
        )
    for policy in policies:
        files[f"sharedflowbundle/policies/{policy}.xml"] = _am_policy(policy)
    return _write_tree_files(parent / name, files)


def _target_xml(name: str, url: str, body: str = "") -> str:
    return (
        f"<TargetEndpoint name={quoteattr(name)}>{body}"
        f"<HTTPTargetConnection><URL>{escape(url)}</URL></HTTPTargetConnection></TargetEndpoint>\n"
    )


def _every_step(value: Any) -> Iterator[Any]:
    """Every Step anywhere in an IR value, found by walking all dataclass fields (so new IR places count too)."""
    from a2m.ir import Step

    if isinstance(value, Step):
        yield value
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        for item in dataclasses.fields(value):
            yield from _every_step(getattr(value, item.name))
    elif isinstance(value, (tuple, list)):
        for item in value:
            yield from _every_step(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _every_step(item)


def _flow_ref_order(project: Project) -> list[str]:
    """The flow-ref targets and http:request elements of the main flow, in execution order."""
    order = []
    for element in project.walk(project.main_flow()):
        if element.tag == tag(CORE, "flow-ref"):
            order.append(str(element.get("name")))
        elif element.tag == tag(HTTP, "request"):
            order.append("<http:request>")
    return order


def test_CP3_T36_every_step_in_every_flow_position_is_generated_or_reported(tmp_path: Path) -> None:
    """[CP3-T36] Every step, wherever Apigee runs it, is either generated or reported; none is silently dropped."""
    proxy_body, policies = _all_positions_body("proxy", proxy=True)
    policies["Call-Shared"] = _fc_policy("Call-Shared", "pos-shared")
    proxy_body = proxy_body.replace("<Request>", "<Request>" + _steps_xml(["Call-Shared"]), 1)
    proxy_body += (
        "<HTTPProxyConnection><BasePath>/all</BasePath><VirtualHost>default</VirtualHost></HTTPProxyConnection>"
        '<RouteRule name="dynamic"><Condition>request.header.x-dyn = "1"</Condition>'
        "<TargetEndpoint>dynamic</TargetEndpoint></RouteRule>"
        '<RouteRule name="default"><TargetEndpoint>default</TargetEndpoint></RouteRule>'
    )
    targets: dict[str, str] = {}
    # 'default' is generated, 'dynamic' cannot be (variable URL), 'spare' is used by no RouteRule.
    for target, url in (
        ("default", "http://backend.example.com/api"),
        ("dynamic", "http://{backend.host}/api"),
        ("spare", "http://spare.example.com/api"),
    ):
        body, names = _all_positions_body(target, proxy=False)
        policies.update(names)
        targets[target] = _target_xml(target, url, body)
    bundles = tmp_path / "bundles"
    bundle = read_bundle(_write_proxy_files(bundles, "all-positions", proxy_body, targets, policies))
    shared = read_bundle(
        _write_shared_flow(bundles, "pos-shared", {"default": ["AM-shared-entry"], "other": ["AM-shared-other"]})
    )
    dest = tmp_path / "all-positions" / "mule-app"

    result = generate(bundle, dest, [shared])

    steps = [*_every_step(bundle), *_every_step(shared)]
    # 10 places per endpoint x 4 endpoints, the FlowCallout and the 2 shared flow steps.
    assert len(steps) == 43, len(steps)
    assert len({step.name for step in steps}) == len(steps)
    project = assert_well_formed(dest)
    calls = [e for e in project.walk(project.main_flow()) if e.tag == tag(CORE, "flow-ref")]
    reported = {name for name, _ in unsupported(result)}
    pending = {str(item.name) for item in result.pending}
    doc_name = tag(DOCUMENTATION, "name")
    named = {e.get(doc_name) for e in project.all_elements()}
    policy_types = {p.name: p.type for p in (*bundle.policies, *shared.policies)}
    missing = []
    both = []
    for step in steps:
        generated = step.name in named
        if policy_types[step.policy] == "FlowCallout":
            generated = generated or any("pos-shared" in (call.get("name") or "") for call in calls)
        if not generated and step.name not in reported and step.name not in pending:
            missing.append(step.name)
        if generated and step.name in reported:
            both.append(step.name)
    assert not missing, f"steps neither generated nor reported: {missing}"
    assert not both, f"steps both generated and reported as unsupported: {both}"


def test_CP3_T37_proxy_preflow_response_steps_run_in_apigee_response_order(tmp_path: Path) -> None:
    """[CP3-T37] ProxyEndpoint PreFlow response steps are generated (or reported) in Apigee's response order."""
    policies = {
        "Call-Target-Resp": _fc_policy("Call-Target-Resp", "target-resp-flow"),
        "Call-Pre-Resp": _fc_policy("Call-Pre-Resp", "pre-resp-flow"),
        "Call-Post-Resp": _fc_policy("Call-Post-Resp", "post-resp-flow"),
        "Add-CORS": _am_policy("Add-CORS"),
    }
    proxy_body = (
        f'<PreFlow name="PreFlow">{_req_resp([], ["Call-Pre-Resp", "Add-CORS"])}</PreFlow><Flows/>'
        f'<PostFlow name="PostFlow">{_req_resp([], ["Call-Post-Resp"])}</PostFlow>'
        "<HTTPProxyConnection><BasePath>/cors</BasePath><VirtualHost>default</VirtualHost></HTTPProxyConnection>"
        '<RouteRule name="default"><TargetEndpoint>default</TargetEndpoint></RouteRule>'
    )
    target_body = f'<PostFlow name="PostFlow">{_req_resp([], ["Call-Target-Resp"])}</PostFlow>'
    bundles = tmp_path / "bundles"
    bundle = read_bundle(
        _write_proxy_files(
            bundles,
            "cors",
            proxy_body,
            {"default": _target_xml("default", "http://backend.example.com/api", target_body)},
            policies,
        )
    )
    shared = [
        read_bundle(_write_shared_flow(bundles, name, {"default": [f"AM-{name}"]}))
        for name in ("target-resp-flow", "pre-resp-flow", "post-resp-flow")
    ]
    dest = tmp_path / "cors" / "mule-app"

    result = generate(bundle, dest, shared)

    project = assert_well_formed(dest)
    order = _flow_ref_order(project)

    def position(shared_name: str) -> int:
        found = [i for i, name in enumerate(order) if shared_name in name]
        assert len(found) == 1, (shared_name, order)
        return found[0]

    request = order.index("<http:request>")
    assert request < position("target-resp-flow") < position("pre-resp-flow") < position("post-resp-flow"), order
    # Add-CORS is generated in its response slot or reported as unsupported: never both, never neither.
    doc_name = tag(DOCUMENTATION, "name")
    walked = list(project.walk(project.main_flow()))
    refs = {
        s: [i for i, e in enumerate(walked) if e.tag == tag(CORE, "flow-ref") and s in str(e.get("name"))]
        for s in ("pre-resp-flow", "post-resp-flow")
    }
    assert len(refs["pre-resp-flow"]) == 1 and len(refs["post-resp-flow"]) == 1, refs
    cors_in_chain = [i for i, e in enumerate(walked) if e.get(doc_name) == "Add-CORS"]
    cors_anywhere = [e for e in project.all_elements() if e.get(doc_name) == "Add-CORS"]
    flagged = [reason for name, reason in unsupported(result) if name == "Add-CORS"]
    assert bool(cors_anywhere) != bool(flagged), (cors_anywhere, unsupported(result))
    if cors_anywhere:
        assert cors_in_chain, "Add-CORS is generated but never runs in the main flow"
        assert all(refs["pre-resp-flow"][0] < i < refs["post-resp-flow"][0] for i in cors_in_chain), (
            cors_in_chain,
            refs,
        )
    else:
        assert len(flagged) == 1, unsupported(result)
        assert "AssignMessage" in flagged[0], flagged


SECRETS = ("Pw-cred-1", "Pw-var-2", "Key-q-3", "alice", "bob")


def test_CP3_T38_credentials_in_target_urls_never_reach_reasons_or_run_log(tmp_path: Path, run_cli: Any) -> None:
    """[CP3-T38] Credentials and keys in target URLs never reach the unsupported reasons or run.log."""
    spec: dict[str, Any] = {
        "endpoints": [
            Endpoint(
                "default",
                "/sec",
                (
                    Route("cred", BETA_CONDITION, "cred"),
                    Route("var", CANARY_CONDITION, "var"),
                    Route("default", None, "default"),
                ),
            )
        ],
        "targets": [
            Target("cred", "https://alice:Pw-cred-1@api.example.com:8443/x"),
            Target("var", "https://bob:Pw-var-2@{backend.host}/x"),
            Target("default", "http://backend.example.com/api?apikey=Key-q-3"),
        ],
    }
    exports = tmp_path / "exports"
    bundle = read_bundle(write_proxy(exports, "secret-urls", **spec))

    result = generate(bundle, tmp_path / "gen" / "mule-app")

    items = unsupported(result)
    text = "\n".join(f"{name} {reason}" for name, reason in items)
    for secret in SECRETS:
        assert secret not in text, (secret, items)
    # The user can still tell which target and setting each line is about.
    for name, wanted in (("cred", "api.example.com:8443"), ("var", "{backend.host}"), ("default URL query", "apikey")):
        assert any(n == name and wanted in reason for n, reason in items), (name, items)

    out = tmp_path / "out"
    res = run_cli(["migrate", str(exports), "--out", str(out), "--llm", "fake", "--no-runtime"])

    assert res.code == 0, res.err
    log = (out / "run.log").read_text(encoding="utf-8")
    assert "api.example.com:8443" in log, log
    for secret in SECRETS:
        assert secret not in log, secret
        assert secret not in res.out + res.err, secret


# ---------------------------------------------------------------- CP3 adversarial round 4 (CP3-T46)
#
# A flow error (target down, timeout, any Mule error) must never send Mule's error details to the
# caller: error.description and friends can name the target's host and port or carry connector
# messages. Only the error *type* may decide what the caller gets.

DW_STRING = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')
# Any use of the error object other than error.errorType (description, detailedDescription,
# cause, errorMessage, childErrors, the whole object, ...).
ERROR_DETAIL = re.compile(r"\berror\b(?!\s*\.\s*errorType\b)")


def _dw_expressions(root: ET.Element) -> Iterator[tuple[str, str]]:
    """(where, expression) for every DataWeave expression (#[...]) in attributes and element text."""
    for element in root.iter():
        values = [(f"{element.tag} @{key}", value) for key, value in element.attrib.items()]
        values.append((f"{element.tag} text", element.text or ""))
        for where, value in values:
            if value.strip().startswith("#["):
                yield where, value


def test_CP3_T46_no_generated_response_carries_mule_error_details(tmp_path: Path) -> None:
    """[CP3-T46] No generated flow returns Mule error details to the caller; every listener answers errors itself."""
    projects = [gen_fixture(tmp_path, name)[0] for name in HAND_MADE]
    projects.append(gen_test_api(tmp_path)[0])
    listeners = 0
    for project in projects:
        for path in sorted((project / "src" / "main" / "mule").glob("*.xml")):
            root = parse_xml(path)
            for where, expression in _dw_expressions(root):
                code = DW_STRING.sub('""', expression)
                assert not ERROR_DETAIL.search(code), f"{path}: {where} uses Mule error details: {expression}"
            for listener in root.iter(tag(HTTP, "listener")):
                listeners += 1
                # Without its own error-response a listener falls back to Mule's default one.
                error_responses = listener.findall(tag(HTTP, "error-response"))
                assert len(error_responses) == 1, f"{path}: listener {listener.get('path')} has no error-response"
                body = error_responses[0].find(tag(HTTP, "body"))
                assert body is not None and (body.text or "").strip(), f"{path}: error-response has no fixed body"
                status = error_responses[0].get("statusCode") or ""
                assert status, f"{path}: error-response has no status"
    assert listeners >= len(projects)


# ---------------------------------------------------------------------------
# CP3 adversarial round 6: wildcard base paths (CP3-T49, CP3-T50).
#
# Apigee base paths may have '*' segments ('/v1/*/search'). Mule matches '*' in a listener
# path as one whole segment too, so those are generated; the forwarded suffix must come from
# what the listener matched (proven at runtime by CP3-T51). Forms Mule cannot express (a
# partial or double wildcard, '{...}' which Mule reads as a URI parameter) are reported, and
# the endpoint is not generated, rather than forwarded wrongly.

WILDCARD_SPEC: dict[str, Any] = {
    "endpoints": [
        Endpoint("literal", "/v1/orders", (Route("default", None, "default"),)),
        Endpoint("tenant", "/v1/*/search", (Route("default", None, "default"),)),
        Endpoint("last", "/v2/*", (Route("default", None, "default"),)),
        Endpoint("double", "/w/*/*/items/", (Route("default", None, "default"),)),
    ],
    "targets": [Target("default", "http://backend.example.com/api")],
}


def test_CP3_T49_literal_and_wildcard_base_paths_listen_and_forward_alike(tmp_path: Path) -> None:
    """[CP3-T49] Literal and whole-segment wildcard base paths each get a listener and the same forwarding."""
    bundle = read_bundle(write_proxy(tmp_path / "bundles", "wildcards", **WILDCARD_SPEC))
    result = generate(bundle, tmp_path / "wildcards" / "mule-app")

    project = assert_well_formed(tmp_path / "wildcards" / "mule-app")
    flows = {project.listener_path(flow): flow for flow in project.listener_flows()}
    assert sorted(flows) == ["/v1/*/search/*", "/v1/orders/*", "/v2/*/*", "/w/*/*/items/*"]
    reported = unsupported(result)
    assert not [item for item in reported if "BasePath" in item[0] or "base path" in item[1]], reported
    paths = set()
    for flow in flows.values():
        (request,) = project.requests(flow)
        assert address(project, request) == ("HTTP", "backend.example.com", "80", "/api")
        path = request.get("path") or ""
        assert path.startswith("#[") and "attributes." in path, path
        paths.add(path)
    # One expression for every base path: the suffix comes from the request, not from the pattern text.
    assert len(paths) == 1, paths


@pytest.mark.parametrize("base_path", ["/v1/ab*/search", "/v1/**", "/v1/{tenant}/search", "/v1/*x"])
def test_CP3_T50_base_paths_mule_cannot_listen_on_are_reported_not_forwarded(tmp_path: Path, base_path: str) -> None:
    """[CP3-T50] A base path Mule cannot express is reported with its endpoint's contents; other endpoints still work."""
    spec: dict[str, Any] = {
        "endpoints": [
            Endpoint("odd", base_path, (Route("odd-route", None, "odd-backend"),), ("CallMissing",)),
            Endpoint("plain", "/plain", (Route("default", None, "default"),)),
        ],
        "targets": [
            Target("odd-backend", "http://odd.example.com/api"),
            Target("default", "http://backend.example.com/api"),
        ],
        "policies": {"CallMissing": CALL_MISSING_POLICY},
    }
    bundle = read_bundle(write_proxy(tmp_path / "bundles", "odd-base", **spec))
    result = generate(bundle, tmp_path / "odd-base" / "mule-app")

    project = assert_well_formed(tmp_path / "odd-base" / "mule-app")
    flows = {project.listener_path(flow): flow for flow in project.listener_flows()}
    assert sorted(flows) == ["/plain/*"], "only the endpoint with an expressible base path listens"
    (request,) = project.requests(flows["/plain/*"])
    assert address(project, request) == ("HTTP", "backend.example.com", "80", "/api")
    reported = unsupported(result)
    base_items = [reason for name, reason in reported if base_path in name]
    assert len(base_items) == 1, reported
    assert "odd" in base_items[0] and base_path in base_items[0], base_items
    names = [name for name, _ in reported]
    for dropped in ("CallMissing", "odd-route", "odd-backend"):
        assert dropped in names, f"{dropped} was dropped without being reported: {reported}"
    backend_reason = next(reason for name, reason in reported if name == "odd-backend")
    assert "not used by any RouteRule" not in backend_reason, backend_reason
    assert "default" not in names, reported
    hosts = {address(project, r)[1] for r in project.find(HTTP, "request")}
    assert hosts == {"backend.example.com"}, hosts


# ---------------------------------------------------------------------------
# CP3 adversarial round 7: backend redirects pass through to the caller (CP3-T52).
#
# Apigee returns a target's 3xx with its Location to the caller and never follows it. Mule's
# HTTP requester follows redirects unless told not to, which would send a second backend request
# and hand the caller the destination's answer. Proven at runtime by CP3-T53.


def _follows_redirects(project: Project, request: ET.Element) -> bool:
    """Mule's rule: the operation's followRedirects wins, else its request-config's, else true."""
    value = project.resolve(request.get("followRedirects"))
    if value is None:
        config = project.globals().get(project.resolve(request.get("config-ref")) or "")
        value = project.resolve(config.get("followRedirects")) if config is not None else None
    return (value or "true").strip().lower() != "false"


def test_CP3_T52_no_generated_request_follows_backend_redirects(tmp_path: Path) -> None:
    """[CP3-T52] Every generated backend request returns a 3xx to the caller instead of following it."""
    roots = [gen_fixture(tmp_path, name)[0] for name in HAND_MADE]
    roots.append(gen_test_api(tmp_path)[0])
    requests = 0
    for root in roots:
        project = assert_well_formed(root)
        for request in project.find(HTTP, "request"):
            requests += 1
            assert not _follows_redirects(project, request), f"{root}: a backend request follows redirects"
    assert requests >= 1
