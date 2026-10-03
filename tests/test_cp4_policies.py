"""CP4: standard Apigee policies become Mule steps through fixed templates.

Public entry points used here (CP4 plan; nothing else from a2m is imported):

    a2m.parser.read_bundle(path: Path) -> Bundle                          (CP2)
    a2m.generator.generate_project(bundle, dest, *, shared_flows=()) -> result   (CP3)
        result.unsupported, result.pending   as in CP3; a step that keeps a
                            condition adds a pending item with ``name`` (the
                            step name) and ``condition`` (the original text)
        result.policies     NEW in CP4: one record per policy step, in flow
                            order (a policy used by two steps has two records):
            .name                 str, the step name
            .type                 str, the policy type (SpikeArrest, OAuthV2, ...)
            .method               "template" or "skipped"
            .reason               str, non-empty when skipped
            .unsupported_options  sequence of items with ``name`` and ``reason``
            .tags                 sequence of str
    a2m.policies.get_template(policy_type: str) -> template | None     (registry)

How a generated step is found: every generated step is one processor in the
listening flow labelled doc:name="<step name>" (anything it needs inside, or in
a sub-flow it calls, is not counted again). A skipped step leaves an XML
comment naming the step and its type at its position. The flow is walked in
execution order with flow-refs expanded.

The fixture bundles live in tests/fixtures/apigee/cp4/ (orders-api: one step of
each standard type; orders-simple: the CP3 proxy with no policies). Variants
are copies edited under tmp_path. The saved reference copy is
tests/golden/cp4/orders-api/, rewritten only when A2M_UPDATE_GOLDEN=1.

No network, no Java, Maven or Mule, no API keys.
"""

from __future__ import annotations

import inspect
import os
import re
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "apigee" / "cp4"
ORDERS_API = FIXTURES / "orders-api"
ORDERS_SIMPLE = FIXTURES / "orders-simple"
GOLDEN_CP4 = REPO / "tests" / "golden" / "cp4"
GOLDEN_CP3 = REPO / "tests" / "golden" / "cp3"
UPDATE_GOLDEN_ENV = "A2M_UPDATE_GOLDEN"

CORE = "http://www.mulesoft.org/schema/mule/core"
HTTP = "http://www.mulesoft.org/schema/mule/http"
DOC = "http://www.mulesoft.org/schema/mule/documentation"
OS_NS = "http://www.mulesoft.org/schema/mule/os"
VALIDATION = "http://www.mulesoft.org/schema/mule/validation"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
XML_NS = "http://www.w3.org/XML/1998/namespace"
POM = "http://maven.apache.org/POM/4.0.0"
MULE_NS_PREFIX = "http://www.mulesoft.org/schema/mule/"
DOC_NAME = f"{{{DOC}}}name"
REQUEST = "<http:request>"
COMMENT = "#comment"
PLACEHOLDER = re.compile(r"\$\{([^}]*)\}")
XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'

# Namespaces a generated app may use on Mule 4.9.0 CE, and the pom dependency each needs (proven versions).
CE_MODULES: dict[str, tuple[str, str, str] | None] = {
    CORE: None,
    DOC: None,
    HTTP: ("org.mule.connectors", "mule-http-connector", "1.11.3"),
    OS_NS: ("org.mule.connectors", "mule-objectstore-connector", "1.2.2"),
    VALIDATION: ("org.mule.modules", "mule-validation-module", "2.0.9"),
}

ORDER = (
    "Verify-Key",
    "Spike-Arrest",
    "Check-IP",
    "Hourly-Quota",
    "Extract-Order-Id",
    "Encode-Basic-Auth",
    "Add-Target-Headers",
    REQUEST,
    "Strip-Internal",
    "Set-Response-Header",
)
STEP_NAMES = frozenset(name for name in ORDER if name != REQUEST)
STANDARD_TYPES = (
    "SpikeArrest",
    "Quota",
    "VerifyAPIKey",
    "AssignMessage",
    "ExtractVariables",
    "RaiseFault",
    "BasicAuthentication",
    "AccessControl",
)
POLICY_TEST_FILES = {
    "SpikeArrest": "test_spike_arrest.py",
    "Quota": "test_quota.py",
    "VerifyAPIKey": "test_verify_api_key.py",
    "AssignMessage": "test_assign_message.py",
    "ExtractVariables": "test_extract_variables.py",
    "RaiseFault": "test_raise_fault.py",
    "BasicAuthentication": "test_basic_authentication.py",
    "AccessControl": "test_access_control.py",
}
SPIKE_CONDITION = 'request.verb = "POST"'


# ---------------------------------------------------------------- bundles and variants


def copy_bundle(tmp_path: Path, source: Path, label: str) -> Path:
    dest = tmp_path / "bundles" / label / source.name
    shutil.copytree(source, dest)
    return dest


def _bundle_xml(root: Path, rel: str) -> tuple[Path, Any]:
    path = root / "apiproxy" / rel
    return path, ET.parse(path)


def _save(path: Path, tree: Any) -> None:
    ET.indent(tree, space="    ")
    path.write_text(XML_HEAD + ET.tostring(tree.getroot(), encoding="unicode") + "\n", encoding="utf-8")


def add_step(
    root: Path,
    endpoint: str,
    flow: str,
    side: str,
    name: str,
    *,
    index: int | None = None,
    condition: str | None = None,
) -> None:
    """Insert a <Step> into ``endpoint`` (e.g. 'proxies/default.xml'), ``flow`` (PreFlow/PostFlow), ``side``."""
    path, tree = _bundle_xml(root, endpoint)
    container = tree.getroot().find(f"{flow}/{side}")
    assert container is not None, (endpoint, flow, side)
    step = ET.Element("Step")
    if condition is not None:
        ET.SubElement(step, "Condition").text = condition
    ET.SubElement(step, "Name").text = name
    container.insert(len(container) if index is None else index, step)
    _save(path, tree)


def set_step_condition(root: Path, endpoint: str, name: str, condition: str) -> None:
    path, tree = _bundle_xml(root, endpoint)
    steps = [s for s in tree.getroot().iter("Step") if (s.findtext("Name") or "").strip() == name]
    assert len(steps) == 1, name
    ET.SubElement(steps[0], "Condition").text = condition
    _save(path, tree)


def write_policy(root: Path, name: str, xml: str) -> None:
    path = root / "apiproxy" / "policies" / f"{name}.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(xml, encoding="utf-8")


def remove_from_policy(root: Path, name: str, child: str) -> None:
    path, tree = _bundle_xml(root, f"policies/{name}.xml")
    element = tree.getroot().find(child)
    assert element is not None, (name, child)
    tree.getroot().remove(element)
    _save(path, tree)


def policy_xml(root_tag: str, name: str, body: str) -> str:
    return (
        XML_HEAD + f'<{root_tag} async="false" continueOnError="false" enabled="true" name="{name}">\n'
        f"    <DisplayName>{name}</DisplayName>\n{body}</{root_tag}>\n"
    )


OAUTH_VERIFY = policy_xml(
    "OAuthV2", "Verify-Token", "    <Operation>VerifyAccessToken</Operation>\n    <AccessTokenPrefix>Bearer</AccessTokenPrefix>\n"
)
ORDER_NOT_FOUND = policy_xml(
    "RaiseFault",
    "Order-Not-Found",
    "    <FaultResponse>\n        <Set>\n"
    '            <Headers>\n                <Header name="X-Error">missing-order</Header>\n            </Headers>\n'
    '            <Payload contentType="application/json">{"error":"not found"}</Payload>\n'
    "            <StatusCode>404</StatusCode>\n            <ReasonPhrase>Not Found</ReasonPhrase>\n"
    "        </Set>\n    </FaultResponse>\n",
)


def all_eight_variant(tmp_path: Path) -> Path:
    """orders-api plus a RaiseFault step at the end of the proxy PostFlow response: all eight standard types."""
    root = copy_bundle(tmp_path, ORDERS_API, "all-eight")
    write_policy(root, "Order-Not-Found", ORDER_NOT_FOUND)
    add_step(root, "proxies/default.xml", "PostFlow", "Response", "Order-Not-Found")
    return root


# ---------------------------------------------------------------- a2m entry points


def read_bundle(path: Path) -> Any:
    from a2m.parser import read_bundle as _read_bundle

    return _read_bundle(path)


def generate(bundle_dir: Path, dest: Path) -> Any:
    from a2m.generator import generate_project

    return generate_project(read_bundle(bundle_dir), dest, shared_flows=())


def records(result: Any, name: str) -> list[Any]:
    return [r for r in result.policies if str(r.name) == name]


def record(result: Any, name: str) -> Any:
    found = records(result, name)
    assert len(found) == 1, f"expected one policy result named {name}, got {[str(r.name) for r in result.policies]}"
    return found[0]


def summary(result: Any) -> list[tuple[str, str, str, str]]:
    return [(str(r.name), str(r.type), str(r.method), str(r.reason or "")) for r in result.policies]


# ---------------------------------------------------------------- reading the generated project


def tag(ns: str, local: str) -> str:
    return f"{{{ns}}}{local}"


def is_element(node: Any) -> bool:
    return isinstance(node.tag, str)


def read_properties(path: Path) -> dict[str, str]:
    props: dict[str, str] = {}
    logical: list[str] = []
    pending = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.lstrip()
        if not pending and (not line or line[0] in "#!"):
            continue
        if (len(line) - len(line.rstrip("\\"))) % 2 == 1:
            pending += line[:-1]
            continue
        logical.append(pending + line)
        pending = ""
    if pending:
        logical.append(pending)
    for line in logical:
        match = re.match(r"((?:\\.|[^=:\s\\])*)\s*[=:]?\s*(.*)$", line)
        assert match is not None
        props[_unescape(match.group(1))] = _unescape(match.group(2))
    return props


def _unescape(text: str) -> str:
    def one(match: re.Match[str]) -> str:
        token = match.group(1)
        if token.startswith("u"):
            return chr(int(token[1:], 16))
        return {"t": "\t", "n": "\n", "r": "\r", "f": "\f"}.get(token, token)

    return re.sub(r"\\(u[0-9a-fA-F]{4}|.)", one, text)


def parse_with_comments(path: Path) -> ET.Element:
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    try:
        return ET.parse(path, parser=parser).getroot()
    except ET.ParseError as exc:  # pragma: no cover - the message is the point
        raise AssertionError(f"{path} is not well-formed XML: {exc}") from exc


class Project:
    def __init__(self, root: Path) -> None:
        assert root.is_dir(), f"no project at {root}"
        self.root = root
        files = sorted((root / "src" / "main" / "mule").glob("*.xml"))
        assert files, "no flow XML"
        self.docs = {path.name: parse_with_comments(path) for path in files}
        props = sorted((root / "src" / "main" / "resources").rglob("*.properties"))
        assert len(props) == 1, props
        self.props = read_properties(props[0])

    def resolve(self, value: str | None) -> str | None:
        if value is None:
            return None
        return PLACEHOLDER.sub(lambda m: self.props.get(m.group(1), m.group(0)), value)

    def globals(self) -> dict[str, ET.Element]:
        return {str(e.get("name")): e for doc in self.docs.values() for e in doc if is_element(e) and e.get("name")}

    def listener_flow(self) -> ET.Element:
        flows = [
            e
            for doc in self.docs.values()
            for e in doc
            if is_element(e)
            and e.tag == tag(CORE, "flow")
            and any(is_element(c) and c.tag == tag(HTTP, "listener") for c in e)
        ]
        assert len(flows) == 1, f"expected one listening flow, found {len(flows)}"
        return flows[0]

    def sequence(self, names: frozenset[str] | set[str] = STEP_NAMES) -> list[tuple[str, Any]]:
        """Labelled steps, the http:request and comments of the listening flow in execution order."""
        found: list[tuple[str, Any]] = []
        callables = {
            name: e for name, e in self.globals().items() if e.tag in (tag(CORE, "flow"), tag(CORE, "sub-flow"))
        }

        def visit(node: Any, seen: tuple[str, ...]) -> None:
            if not is_element(node):
                found.append((COMMENT, node))
                return
            label = node.get(DOC_NAME)
            if label in names:
                found.append((str(label), node))
                return
            if node.tag == tag(HTTP, "request"):
                found.append((REQUEST, node))
                return
            if node.tag == tag(CORE, "flow-ref"):
                ref = node.get("name") or ""
                if ref in callables and ref not in seen:
                    for child in callables[ref]:
                        visit(child, (*seen, ref))
            for child in node:
                visit(child, seen)

        for child in self.listener_flow():
            visit(child, ())
        return found

    def order(self, names: frozenset[str] | set[str] = STEP_NAMES) -> list[str]:
        return [label for label, _ in self.sequence(names) if label != COMMENT]

    def request_address(self, request: ET.Element) -> tuple[str | None, ...]:
        config = self.globals().get(self.resolve(request.get("config-ref")) or "")
        assert config is not None and config.tag == tag(HTTP, "request-config"), request.get("config-ref")
        conn = config.find(tag(HTTP, "request-connection"))
        assert conn is not None
        base = config.get("basePath") if config.get("basePath") is not None else conn.get("basePath")
        return (
            self.resolve(conn.get("protocol")),
            self.resolve(conn.get("host")),
            self.resolve(conn.get("port")),
            self.resolve(base),
        )

    def nearby_text(self, element: ET.Element) -> list[str]:
        """Text in, around and above ``element``: its subtree (comments too), its ancestors' attributes below
        the flow, and comments right before or after it or any of those ancestors."""
        flow = self.listener_flow()
        parents = {child: parent for doc in self.docs.values() for parent in doc.iter() for child in parent}
        chain = [element]
        while chain[-1] is not flow and chain[-1] in parents:
            chain.append(parents[chain[-1]])
        texts: list[str] = []
        for node in element.iter():
            if is_element(node):
                texts += list(node.attrib.values())
            texts.append(node.text or "")
        for node in chain:
            if node is flow:
                break
            if node is not element:
                texts += list(node.attrib.values())
            siblings = list(parents[node])
            index = next(i for i, s in enumerate(siblings) if s is node)
            for j in (index - 1, index + 1):
                if 0 <= j < len(siblings) and not is_element(siblings[j]):
                    texts.append(siblings[j].text or "")
        return texts


# ---------------------------------------------------------------- golden copies


def tree_files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def tree_bytes(root: Path) -> dict[str, bytes]:
    return {rel: (root / rel).read_bytes() for rel in tree_files(root)}


def canonical(path: Path) -> str:
    return ET.canonicalize(from_file=str(path), with_comments=True, strip_text=True)


def compare_trees(actual_dir: Path, golden_dir: Path) -> None:
    assert golden_dir.is_dir(), f"golden folder {golden_dir} is missing; generate it with {UPDATE_GOLDEN_ENV}=1"
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


def assert_matches_golden(actual_dir: Path, golden_dir: Path) -> None:
    """Compare with the saved copy; rewrite the copy only when A2M_UPDATE_GOLDEN=1."""
    if os.environ.get(UPDATE_GOLDEN_ENV) == "1":
        if golden_dir.exists():
            shutil.rmtree(golden_dir)
        shutil.copytree(actual_dir, golden_dir)
        return
    before = tree_bytes(golden_dir) if golden_dir.is_dir() else None
    try:
        compare_trees(actual_dir, golden_dir)
    finally:
        after = tree_bytes(golden_dir) if golden_dir.is_dir() else None
        assert before == after, f"{golden_dir} changed without {UPDATE_GOLDEN_ENV}=1"


# ---------------------------------------------------------------- namespaces and pom


def declared_namespaces(path: Path) -> dict[str, set[str]]:
    """prefix -> every URI it is bound to anywhere in the file ('' is the default namespace)."""
    found: dict[str, set[str]] = {}
    for _, (prefix, uri) in ET.iterparse(path, events=("start-ns",)):
        found.setdefault(prefix, set()).add(uri)
    return found


def used_namespaces(root: ET.Element) -> set[str]:
    used: set[str] = set()
    for element in root.iter():
        if not is_element(element):
            continue
        for name in [element.tag, *element.attrib]:
            if name.startswith("{"):
                used.add(name[1:].split("}", 1)[0])
    return used


def schema_locations(root: ET.Element) -> dict[str, str]:
    parts = (root.get(f"{{{XSI}}}schemaLocation") or "").split()
    assert len(parts) % 2 == 0, f"odd xsi:schemaLocation: {parts}"
    return dict(zip(parts[0::2], parts[1::2], strict=True))


def pom_dependencies(pom: Path) -> dict[tuple[str, str], tuple[str, str]]:
    root = ET.parse(pom).getroot()
    ns = {"m": POM}
    deps: dict[tuple[str, str], tuple[str, str]] = {}
    for dep in root.findall("m:dependencies/m:dependency", ns):
        group = (dep.findtext("m:groupId", "", ns) or "").strip()
        artifact = (dep.findtext("m:artifactId", "", ns) or "").strip()
        version = (dep.findtext("m:version", "", ns) or "").strip()
        classifier = (dep.findtext("m:classifier", "", ns) or "").strip()
        deps[(group, artifact)] = (version, classifier)
    return deps


def check_namespaces_and_pom(project_dir: Path) -> set[str]:
    """Every XML parses, every namespace is an official Mule 4 one with a schema location, and the pom has a
    pinned dependency for every module used beyond core. Returns the namespaces the flows use."""
    for path in sorted(project_dir.rglob("*.xml")):
        parse_with_comments(path)
    deps = pom_dependencies(project_dir / "pom.xml")
    all_used: set[str] = set()
    for path in sorted((project_dir / "src" / "main" / "mule").glob("*.xml")):
        root = parse_with_comments(path)
        declared = declared_namespaces(path)
        declared_uris = {uri for uris in declared.values() for uri in uris}
        for prefix, uris in declared.items():
            for uri in uris:
                assert uri == XSI or uri.startswith(MULE_NS_PREFIX), f"{path.name}: prefix {prefix!r} -> {uri}"
        used = used_namespaces(root) - {XSI, XML_NS}
        all_used |= used
        locations = schema_locations(root)
        for uri in sorted(used):
            assert uri.startswith(MULE_NS_PREFIX), f"{path.name}: namespace {uri} is not a Mule 4 namespace"
            assert uri in declared_uris, f"{path.name}: namespace {uri} is used but never declared"
            assert uri in CE_MODULES, f"{path.name}: namespace {uri} is not one of the CE modules proven on 4.9.0"
            if uri == DOC:
                continue
            location = locations.get(uri)
            assert location is not None, f"{path.name}: no xsi:schemaLocation entry for {uri}"
            assert location.startswith(uri + "/") and location.endswith(".xsd"), (uri, location)
            module = CE_MODULES[uri]
            if module is not None:
                group, artifact, version = module
                assert (group, artifact) in deps, f"{path.name} uses {uri} but pom.xml has no {group}:{artifact}"
                assert deps[(group, artifact)] == (version, "mule-plugin"), (artifact, deps[(group, artifact)])
    return all_used


# ---------------------------------------------------------------- tests


def test_CP4_T01_every_standard_policy_appears_in_the_mule_flow_in_apigee_order(tmp_path: Path) -> None:
    """[CP4-T01] Every standard policy shows up in the Mule flow in the same order as in Apigee."""
    dest = tmp_path / "orders-api" / "mule-app"
    result = generate(copy_bundle(tmp_path, ORDERS_API, "plain"), dest)

    project = Project(dest)
    assert project.order() == list(ORDER), project.order()
    request = next(node for label, node in project.sequence() if label == REQUEST)
    assert project.request_address(request) == ("HTTPS", "backend.example.test", "443", "/orders")
    assert [str(r.name) for r in result.policies] == [n for n in ORDER if n != REQUEST], summary(result)
    assert all(str(r.method) == "template" for r in result.policies), summary(result)


def test_CP4_T02_a_policy_used_in_two_places_is_generated_in_both_places(tmp_path: Path) -> None:
    """[CP4-T02] A policy used in two places is generated in both places."""
    root = copy_bundle(tmp_path, ORDERS_API, "twice")
    add_step(root, "proxies/default.xml", "PreFlow", "Response", "Add-Target-Headers")
    dest = tmp_path / "twice" / "mule-app"
    result = generate(root, dest)

    order = Project(dest).order()
    places = [i for i, label in enumerate(order) if label == "Add-Target-Headers"]
    assert len(places) == 2, order
    assert places[0] < order.index(REQUEST) < places[1], order
    assert len(records(result, "Add-Target-Headers")) == 2, summary(result)


def test_CP4_T03_a_steps_condition_is_kept_and_marked_pending_not_dropped(tmp_path: Path) -> None:
    """[CP4-T03] A step's condition is kept and marked pending, not dropped."""
    root = copy_bundle(tmp_path, ORDERS_API, "conditional")
    set_step_condition(root, "proxies/default.xml", "Spike-Arrest", SPIKE_CONDITION)
    dest = tmp_path / "conditional" / "mule-app"
    result = generate(root, dest)

    project = Project(dest)
    order = project.order()
    assert order[:3] == ["Verify-Key", "Spike-Arrest", "Check-IP"], order
    spike = next(node for label, node in project.sequence() if label == "Spike-Arrest")
    nearby = project.nearby_text(spike)
    assert any(SPIKE_CONDITION in text for text in nearby), f"condition not kept next to the step: {nearby}"
    pending = [(str(p.name), str(p.condition)) for p in result.pending]
    assert ("Spike-Arrest", SPIKE_CONDITION) in pending, pending


def test_CP4_T04_oauthv2_is_listed_as_unsupported_with_its_name_and_type(tmp_path: Path) -> None:
    """[CP4-T04] OAuthV2 is listed as unsupported with its name and type."""
    root = copy_bundle(tmp_path, ORDERS_API, "oauth")
    write_policy(root, "Verify-Token", OAUTH_VERIFY)
    add_step(root, "proxies/default.xml", "PreFlow", "Request", "Verify-Token", index=1)
    dest = tmp_path / "oauth" / "mule-app"
    result = generate(root, dest)

    entry = record(result, "Verify-Token")
    assert (str(entry.type), str(entry.method)) == ("OAuthV2", "skipped"), summary(result)
    assert str(entry.reason or "").strip(), "the unsupported entry has no reason"

    project = Project(dest)
    sequence = project.sequence(STEP_NAMES | {"Verify-Token"})
    labels = [label for label, _ in sequence]
    assert "Verify-Token" not in labels, "an unsupported OAuthV2 step was generated as a Mule step"
    key, spike = labels.index("Verify-Key"), labels.index("Spike-Arrest")
    between = [node.text or "" for label, node in sequence[key + 1 : spike] if label == COMMENT]
    assert any("Verify-Token" in text and "OAuthV2" in text for text in between), (
        f"no comment naming Verify-Token and OAuthV2 between Verify-Key and Spike-Arrest: {between}"
    )
    assert project.order() == list(ORDER), project.order()


def test_CP4_T05_any_unknown_policy_type_is_listed_and_no_policy_goes_missing(tmp_path: Path) -> None:
    """[CP4-T05] Any unknown policy type is listed, and no policy goes missing."""
    root = copy_bundle(tmp_path, ORDERS_SIMPLE, "unknown")
    write_policy(root, "Acme-Thing", policy_xml("AcmeCustomPolicy", "Acme-Thing", "    <Level>high</Level>\n"))
    write_policy(
        root,
        "Lookup-Config",
        policy_xml(
            "KeyValueMapOperations",
            "Lookup-Config",
            '    <Scope>environment</Scope>\n    <Get assignTo="cfg">\n        <Key>\n'
            "            <Parameter>MyKey</Parameter>\n        </Key>\n    </Get>\n",
        ),
    )
    write_policy(root, "Spike-Arrest", policy_xml("SpikeArrest", "Spike-Arrest", "    <Rate>30pm</Rate>\n"))
    for name in ("Acme-Thing", "Lookup-Config", "Spike-Arrest"):
        add_step(root, "proxies/default.xml", "PreFlow", "Request", name)
    bundle = read_bundle(root)
    steps = [s for e in bundle.proxy_endpoints for s in e.pre_flow.request]
    assert len(steps) == 3

    from a2m.generator import generate_project

    result = generate_project(bundle, tmp_path / "unknown" / "mule-app", shared_flows=())

    assert len(result.policies) == len(steps), summary(result)
    assert [(n, t, m) for n, t, m, _ in summary(result)] == [
        ("Acme-Thing", "AcmeCustomPolicy", "skipped"),
        ("Lookup-Config", "KeyValueMapOperations", "skipped"),
        ("Spike-Arrest", "SpikeArrest", "template"),
    ], summary(result)
    for name in ("Acme-Thing", "Lookup-Config"):
        assert str(record(result, name).reason or "").strip(), f"{name} is skipped without a reason"


def test_CP4_T06_a_known_policy_missing_a_required_setting_is_listed_not_a_crash(tmp_path: Path) -> None:
    """[CP4-T06] A known policy missing a required setting is listed as unsupported, not a crash."""
    root = copy_bundle(tmp_path, ORDERS_API, "incomplete")
    remove_from_policy(root, "Spike-Arrest", "Rate")
    remove_from_policy(root, "Check-IP", "IPRules")
    dest = tmp_path / "incomplete" / "mule-app"
    result = generate(root, dest)

    Project(dest)
    for path in sorted(dest.rglob("*.xml")):
        parse_with_comments(path)
    spike, check = record(result, "Spike-Arrest"), record(result, "Check-IP")
    assert str(spike.method) == "skipped" and "Rate" in str(spike.reason), summary(result)
    assert str(check.method) == "skipped" and "IPRules" in str(check.reason), summary(result)
    others = [r for r in summary(result) if r[0] not in ("Spike-Arrest", "Check-IP")]
    assert len(others) == 7 and all(m == "template" for _, _, m, _ in others), others


def test_CP4_T07_generated_policy_xml_uses_official_namespaces_and_the_pom_has_what_they_need(
    tmp_path: Path,
) -> None:
    """[CP4-T07] Generated policy XML uses official Mule 4 namespaces and the pom has what they need."""
    plain = tmp_path / "orders-api" / "mule-app"
    generate(copy_bundle(tmp_path, ORDERS_API, "plain"), plain)
    eight = tmp_path / "all-eight" / "mule-app"
    result = generate(all_eight_variant(tmp_path), eight)

    assert str(record(result, "Order-Not-Found").method) == "template", summary(result)
    for project_dir in (plain, eight):
        used = check_namespaces_and_pom(project_dir)
        assert {CORE, HTTP} <= used, used


def test_CP4_T08_generating_the_policy_sample_twice_gives_identical_files_matching_the_saved_copy(
    tmp_path: Path,
) -> None:
    """[CP4-T08] Generating the policy sample twice gives identical files that match the saved copy."""
    source = copy_bundle(tmp_path, ORDERS_API, "plain")
    first, second = tmp_path / "one" / "orders-api", tmp_path / "two" / "orders-api"
    generate(source, first)
    generate(source, second)

    assert tree_bytes(first) == tree_bytes(second)
    assert Project(first).order() == list(ORDER), "the sample's policy steps are not generated"
    assert_matches_golden(first, GOLDEN_CP4 / "orders-api")


def test_CP4_T09_a_proxy_without_policies_still_generates_exactly_what_cp3_produced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[CP4-T09] A proxy without policies still generates exactly what CP3 produced."""
    monkeypatch.delenv(UPDATE_GOLDEN_ENV, raising=False)
    dest = tmp_path / "orders-simple" / "mule-app"
    result = generate(copy_bundle(tmp_path, ORDERS_SIMPLE, "simple"), dest)

    compare_trees(dest, GOLDEN_CP3 / "orders-simple")
    assert list(result.policies) == []


def _source_file(template: Any) -> Path:
    plain = inspect.isfunction(template) or inspect.ismethod(template) or inspect.isclass(template)
    target = template if plain else type(template)
    source = inspect.getsourcefile(target)
    assert source is not None, template
    return Path(source).resolve()


def test_CP4_T10_each_of_the_eight_policies_has_its_own_code_file_and_test_file() -> None:
    """[CP4-T10] Each of the eight policies has its own code file and its own test file."""
    from a2m.policies import get_template

    package = (REPO / "a2m" / "policies").resolve()
    sources: dict[str, Path] = {}
    for policy_type in STANDARD_TYPES:
        template = get_template(policy_type)
        assert template is not None, f"the registry has no template for {policy_type}"
        sources[policy_type] = _source_file(template)

    for policy_type, source in sources.items():
        assert source.parent == package, f"{policy_type} template lives in {source}, not in a2m/policies/"
    assert len(set(sources.values())) == 8, {t: s.name for t, s in sources.items()}
    tests_dir = REPO / "tests" / "policies"
    missing = [name for name in POLICY_TEST_FILES.values() if not (tests_dir / name).is_file()]
    assert missing == [], missing

