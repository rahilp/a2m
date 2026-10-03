"""Shared helpers for the CP4 per-policy tests (tests/policies/test_*.py).

Contract used here (CP4 plan; nothing else from a2m is imported):

    a2m.parser.read_bundle(path: Path) -> Bundle                          (CP2)
    a2m.policies.get_template(policy_type: str) -> template | None
        The policy registry: returns the template for SpikeArrest, Quota,
        VerifyAPIKey, AssignMessage, ExtractVariables, RaiseFault,
        BasicAuthentication and AccessControl.
    template(policy: a2m.ir.Policy, *, direction: str) -> output
        ``direction`` is "request" or "response": the side of the flow the
        step sits in.
        output.processors  sequence of xml.etree.ElementTree.Element: the Mule
                           processors placed at the step's position, in order,
                           with ElementTree "{namespace-uri}local" tags; empty
                           when the policy is skipped
        output.globals     sequence of Element: top-level elements the
                           processors need (for example an object store), may
                           be empty
        output.properties  mapping str -> str: entries the step adds to the
                           app's config.properties, referenced from the XML as
                           ${key} (or p('key') in DataWeave)
        output.result      the result record of the step:
            .name                 str, the policy name
            .type                 str, the policy type (its root element)
            .method               "template" or "skipped"
            .reason               str, why it was skipped (non-empty when skipped)
            .unsupported_options  sequence of items with ``name: str`` and
                                  ``reason: str``: settings it could not carry over
            .tags                 sequence of str (for example "time-window")
    a2m.generator.generate_project(bundle, dest, *, shared_flows=()) -> result   (CP3)
        For the file-level cases: every generated step is one processor in the
        listening flow labelled doc:name="<step name>".

Runtime behaviour (401, 429, ...) is asserted here as the concrete
configuration in the generated XML; tests/runtime/test_cp4_policies_runtime.py
proves it on a real Mule runtime.
"""

from __future__ import annotations

import itertools
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, quoteattr

import pytest

CORE = "http://www.mulesoft.org/schema/mule/core"
HTTP = "http://www.mulesoft.org/schema/mule/http"
DOC_NAME = "{http://www.mulesoft.org/schema/mule/documentation}name"
XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
PLACEHOLDER = re.compile(r"\$\{([^}]*)\}")
P_FUNCTION = re.compile(r"""(?:Mule::)?p\(\s*(['"])(.*?)\1\s*\)""")
DW_LITERAL = re.compile(r"""^#\[\s*(['"])(.*)\1\s*\]$""", re.DOTALL)


def tag(ns: str, local: str) -> str:
    return f"{{{ns}}}{local}"


def is_element(element: Any) -> bool:
    return isinstance(element.tag, str)


def dw_unescape(text: str) -> str:
    """``text`` with DataWeave/Java string escapes undone (\\" -> ", \\' -> ', \\\\ -> \\)."""
    return re.sub(r"\\(.)", r"\1", text)


def literal(value: str | None) -> str | None:
    """The plain value of an attribute: '#["x"]' and "#['x']" give x; anything else is returned as is."""
    if value is None:
        return None
    match = DW_LITERAL.match(value.strip())
    return dw_unescape(match.group(2)) if match else value


def refs_var(text: str, name: str) -> bool:
    """True when DataWeave ``text`` reads flow variable ``name`` (vars.name, vars['name'], vars."name")."""
    quoted = re.escape(name)
    forms = [rf"vars\s*\.\s*(['\"]){quoted}\1", rf"vars\s*\[\s*(['\"]){quoted}\2\s*\]"]
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        forms.append(rf"vars\s*\.\s*{quoted}\b")
    return re.search("|".join(f"(?:{f})" for f in forms), text) is not None


def resolve_refs(text: str, props: Mapping[str, str]) -> str:
    """``text`` with ${key} and p('key') replaced by their property values (unknown keys left as is)."""
    text = PLACEHOLDER.sub(lambda m: props.get(m.group(1), m.group(0)), text)
    return P_FUNCTION.sub(lambda m: props.get(m.group(2), m.group(0)), text)


def property_refs(text: str) -> set[str]:
    return set(PLACEHOLDER.findall(text)) | {m.group(2) for m in P_FUNCTION.finditer(text)}


def element_strings(elements: Iterable[ET.Element], props: Mapping[str, str]) -> list[str]:
    """Every attribute value and text of ``elements`` (comments excluded), each followed by its resolved form."""
    found: list[str] = []
    for top in elements:
        for element in top.iter():
            if not is_element(element):
                continue
            for value in [*element.attrib.values(), element.text or ""]:
                if not value.strip():
                    continue
                found.append(value)
                resolved = resolve_refs(value, props)
                if resolved != value:
                    found.append(resolved)
    return found


# ---------------------------------------------------------------- one policy through its template


@dataclass
class Translation:
    raw: Any

    @property
    def processors(self) -> list[ET.Element]:
        return list(self.raw.processors)

    @property
    def globals(self) -> list[ET.Element]:
        return list(self.raw.globals)

    @property
    def properties(self) -> dict[str, str]:
        return dict(self.raw.properties)

    @property
    def result(self) -> Any:
        return self.raw.result

    @property
    def method(self) -> str:
        return str(self.result.method)

    @property
    def reason(self) -> str:
        return str(self.result.reason or "")

    def option_names(self) -> list[str]:
        return [str(item.name) for item in self.result.unsupported_options]

    def option_text(self) -> str:
        return " | ".join(f"{item.name}: {item.reason}" for item in self.result.unsupported_options)

    def elements(self) -> list[ET.Element]:
        return [e for top in [*self.processors, *self.globals] for e in top.iter() if is_element(e)]

    def processor_elements(self) -> list[ET.Element]:
        return [e for top in self.processors for e in top.iter() if is_element(e)]

    def strings(self) -> list[str]:
        return element_strings([*self.processors, *self.globals], self.properties)

    def text(self) -> str:
        return "\n".join(self.strings())

    def written_vars(self) -> set[str]:
        """Flow variables the processors set: set-variable names and operation ``target`` attributes."""
        found: set[str] = set()
        for element in self.processor_elements():
            if element.tag == tag(CORE, "set-variable") and element.get("variableName"):
                found.add(str(element.get("variableName")))
            if element.get("target"):
                found.add(str(element.get("target")))
        return found

    def var_values(self, name: str) -> list[str]:
        """Values (resolved, '#["x"]' unwrapped) of every set-variable that sets ``name``."""
        values = []
        for element in self.processor_elements():
            if element.tag == tag(CORE, "set-variable") and element.get("variableName") == name:
                value = literal(resolve_refs(element.get("value") or element.text or "", self.properties))
                values.append(value or "")
        return values

    def of(self, local: str, ns: str = CORE) -> list[ET.Element]:
        return [e for e in self.elements() if e.tag == tag(ns, local)]


@dataclass
class Kit:
    root: Path
    counter: Iterator[int]

    def policy(self, xml: str) -> Any:
        """Parse ``xml`` (one Apigee policy file) through a2m's bundle reader and return its IR policy."""
        from a2m.parser import read_bundle

        root_tag = ET.fromstring(xml.encode("utf-8")).get("name")
        assert root_tag, "inline policy XML needs a name attribute"
        bundle_dir = write_policy_proxy(
            self.root / f"policy-{next(self.counter)}", "policy-test", {root_tag: xml}, request=[root_tag]
        )
        bundle = read_bundle(bundle_dir)
        found = [p for p in bundle.policies if p.name == root_tag]
        assert len(found) == 1, [p.name for p in bundle.policies]
        return found[0]

    def translate(self, xml: str, *, direction: str = "request") -> Translation:
        from a2m.policies import get_template

        policy = self.policy(xml)
        template = get_template(policy.type)
        assert template is not None, f"the registry has no template for {policy.type}"
        return Translation(template(policy, direction=direction))

    def generate(
        self,
        policies: Mapping[str, str],
        *,
        request: Sequence[str] = (),
        response: Sequence[str] = (),
        name: str = "policy-file",
    ) -> tuple[FlowProject, Any]:
        """Generate a whole mule-app for a proxy whose PreFlow request / PostFlow response hold the given steps."""
        from a2m.generator import generate_project
        from a2m.parser import read_bundle

        work = self.root / f"project-{next(self.counter)}"
        bundle = read_bundle(write_policy_proxy(work / "bundles", name, policies, request=request, response=response))
        dest = work / name / "mule-app"
        result = generate_project(bundle, dest, shared_flows=())
        return FlowProject(dest), result


@pytest.fixture
def kit(tmp_path: Path) -> Kit:
    return Kit(tmp_path / "kit", itertools.count(1))


def policy_xml(root: str, name: str, body: str, attrs: str = "") -> str:
    """A policy file: <root async="false" continueOnError="false" enabled="true" name=...>body</root>."""
    return (
        XML_HEAD
        + f'<{root} async="false" continueOnError="false" enabled="true" name={quoteattr(name)}{attrs}>\n'
        + f"    <DisplayName>{escape(name)}</DisplayName>\n{body}</{root}>\n"
    )


def _steps(names: Sequence[str]) -> str:
    if not names:
        return ""
    inner = "".join(f"            <Step>\n                <Name>{escape(n)}</Name>\n            </Step>\n" for n in names)
    return f"\n{inner}        "


def write_policy_proxy(
    parent: Path,
    name: str,
    policies: Mapping[str, str],
    *,
    request: Sequence[str] = (),
    response: Sequence[str] = (),
) -> Path:
    """A proxy 'name' (base path /<name>, target http://127.0.0.1:9/backend) with steps in PreFlow request
    and PostFlow response; ``policies`` maps policy name to its XML."""
    files = {
        f"apiproxy/{name}.xml": (
            XML_HEAD + f"<APIProxy revision=\"1\" name={quoteattr(name)}>\n"
            f"    <DisplayName>{escape(name)}</DisplayName>\n    <Policies>\n"
            + "".join(f"        <Policy>{escape(p)}</Policy>\n" for p in policies)
            + "    </Policies>\n"
            "    <ProxyEndpoints>\n        <ProxyEndpoint>default</ProxyEndpoint>\n    </ProxyEndpoints>\n"
            "    <TargetEndpoints>\n        <TargetEndpoint>default</TargetEndpoint>\n    </TargetEndpoints>\n"
            "</APIProxy>\n"
        ),
        "apiproxy/proxies/default.xml": (
            XML_HEAD + '<ProxyEndpoint name="default">\n'
            f'    <PreFlow name="PreFlow">\n        <Request>{_steps(request)}</Request>\n        <Response/>\n'
            "    </PreFlow>\n    <Flows/>\n"
            f'    <PostFlow name="PostFlow">\n        <Request/>\n        <Response>{_steps(response)}</Response>\n'
            "    </PostFlow>\n"
            f"    <HTTPProxyConnection>\n        <BasePath>/{escape(name)}</BasePath>\n"
            "        <VirtualHost>default</VirtualHost>\n    </HTTPProxyConnection>\n"
            '    <RouteRule name="default">\n        <TargetEndpoint>default</TargetEndpoint>\n    </RouteRule>\n'
            "</ProxyEndpoint>\n"
        ),
        "apiproxy/targets/default.xml": (
            XML_HEAD + '<TargetEndpoint name="default">\n'
            '    <PreFlow name="PreFlow">\n        <Request/>\n        <Response/>\n    </PreFlow>\n    <Flows/>\n'
            '    <PostFlow name="PostFlow">\n        <Request/>\n        <Response/>\n    </PostFlow>\n'
            "    <HTTPTargetConnection>\n        <URL>http://127.0.0.1:9/backend</URL>\n"
            "    </HTTPTargetConnection>\n</TargetEndpoint>\n"
        ),
    }
    for policy_name, xml in policies.items():
        files[f"apiproxy/policies/{policy_name}.xml"] = xml
    root = parent / name
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


# ---------------------------------------------------------------- a generated project, read the way Mule reads it


def read_properties(path: Path) -> dict[str, str]:
    """A java.util.Properties reader (escapes, continuations, last definition wins)."""
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


class FlowProject:
    """A generated mule-app: flow XML parsed with comments kept, properties read."""

    def __init__(self, root: Path) -> None:
        assert root.is_dir(), f"no project at {root}"
        self.root = root
        self.flow_files = sorted((root / "src" / "main" / "mule").glob("*.xml"))
        assert self.flow_files, "no flow XML"
        self.docs: dict[str, ET.Element] = {}
        for path in self.flow_files:
            parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
            try:
                self.docs[path.name] = ET.parse(path, parser=parser).getroot()
            except ET.ParseError as exc:  # pragma: no cover - the message is the point
                raise AssertionError(f"{path} is not well-formed XML: {exc}") from exc
        props = sorted((root / "src" / "main" / "resources").rglob("*.properties"))
        assert len(props) == 1, props
        self.props = read_properties(props[0])

    def callables(self) -> dict[str, ET.Element]:
        return {
            str(e.get("name")): e
            for doc in self.docs.values()
            for e in doc
            if is_element(e) and e.tag in (tag(CORE, "flow"), tag(CORE, "sub-flow"))
        }

    def listener_flows(self) -> list[ET.Element]:
        return [
            e
            for doc in self.docs.values()
            for e in doc
            if is_element(e)
            and e.tag == tag(CORE, "flow")
            and any(is_element(c) and c.tag == tag(HTTP, "listener") for c in e)
        ]

    def expanded(self, element: ET.Element, seen: tuple[str, ...] = ()) -> Iterator[ET.Element]:
        """``element`` and everything under it in execution order, each flow-ref replaced by what it calls."""
        yield element
        if not is_element(element):
            return
        if element.tag == tag(CORE, "flow-ref"):
            name = element.get("name") or ""
            target = self.callables().get(name)
            if target is not None and name not in seen:
                for child in target:
                    yield from self.expanded(child, (*seen, name))
        for child in element:
            yield from self.expanded(child, seen)

    def step(self, name: str) -> ET.Element:
        """The one processor labelled doc:name=<name> in the listening flows."""
        found = [e for flow in self.listener_flows() for e in flow.iter() if is_element(e) and e.get(DOC_NAME) == name]
        assert len(found) == 1, f"expected one processor labelled {name!r}, found {len(found)}"
        return found[0]

    def step_elements(self, name: str) -> list[ET.Element]:
        return [e for e in self.expanded(self.step(name)) if is_element(e)]

    def strings(self, elements: Iterable[ET.Element]) -> list[str]:
        return element_strings(elements, self.props)

    def all_elements(self) -> list[ET.Element]:
        return [e for doc in self.docs.values() for e in doc.iter() if is_element(e)]
