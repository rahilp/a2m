"""Read one Apigee proxy or shared flow bundle into the IR (:class:`a2m.ir.Bundle`).

:func:`read_bundle` takes a folder holding ``apiproxy/`` or
``sharedflowbundle/`` (directly or inside one top folder, the same layout rule
as discovery) or a ``.zip`` export of one. A zip is unpacked first by the same
checked extraction the batch engine uses (:func:`a2m.discovery.extract_zip`).

The bundle is malformed, and :class:`a2m.errors.BundleError` is raised, when
Apigee would not deploy it: XML that is not well-formed (or declares
entities), an endpoint the descriptor lists with no file, a step naming a
policy that does not exist, a route rule to a target endpoint that does not
exist, or a ``<Step>`` somewhere a2m does not read (refused rather than
silently dropped).
"""

from __future__ import annotations

import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import TypeVar

from a2m.discovery import (
    PROXY_ROOT,
    SHARED_FLOW_ROOT,
    ZIP_SUFFIX,
    bundle_root,
    extract_zip,
    folder_subfolders,
    item_name,
)
from a2m.errors import BundleError
from a2m.ir import (
    Bundle,
    BundleKind,
    DefaultFaultRule,
    FaultRule,
    Flow,
    FlowSteps,
    ProxyEndpoint,
    RouteRule,
    SharedFlow,
    Step,
    TargetEndpoint,
)
from a2m.parser.policies import POLICIES_FOLDER, PolicyIndex, read_policies, read_resources
from a2m.parser.source import XML_BOOLEANS, BundleFiles, XmlFile, one_line, text_of, to_element

PROXIES_FOLDER = "proxies"
TARGETS_FOLDER = "targets"
SHARED_FLOWS_FOLDER = "sharedflows"

DESCRIPTOR_TAGS = {BundleKind.PROXY: "APIProxy", BundleKind.SHARED_FLOW: "SharedFlowBundle"}
ROOT_KINDS = {PROXY_ROOT: BundleKind.PROXY, SHARED_FLOW_ROOT: BundleKind.SHARED_FLOW}

# Top-level endpoint elements read into named IR fields; any other element is kept in ``other_elements``.
_ENDPOINT_FLOW_TAGS = frozenset({"PreFlow", "PostFlow", "Flows", "FaultRules", "DefaultFaultRule"})
_PROXY_TAGS = _ENDPOINT_FLOW_TAGS | {"PostClientFlow", "RouteRule"}
_TARGET_TAGS = _ENDPOINT_FLOW_TAGS | {"EventFlow"}
_TARGET_CONNECTION_TAGS = ("HTTPTargetConnection", "LocalTargetConnection")

E = TypeVar("E")


def read_bundle(path: Path, *, label: str | None = None) -> Bundle:
    """Read the bundle folder or ``.zip`` at ``path``; ``label`` names it in errors (default: its item name)."""
    shown = label if label is not None else item_name(path)
    if path.is_file() and path.suffix.lower() == ZIP_SUFFIX:
        with tempfile.TemporaryDirectory(prefix="a2m-bundle-") as unpacked:
            folder = Path(unpacked)
            _labelled(shown, lambda: extract_zip(path, folder))
            return _read_folder(folder, shown)
    if path.is_dir():
        return _read_folder(path, shown)
    raise BundleError(one_line(f"{shown}: {path.name} is not a bundle folder or a .zip file"))


def _labelled(label: str, action: Callable[[], E]) -> E:
    """Run ``action``; a BundleError from the shared discovery helpers gets the bundle's label in front."""
    try:
        return action()
    except BundleError as exc:
        raise type(exc)(one_line(f"{label}: {exc}")) from exc


def _read_folder(folder: Path, label: str) -> Bundle:
    found = _labelled(label, lambda: bundle_root(folder_subfolders(folder)))
    if found is None:
        raise BundleError(f"{label}: no {PROXY_ROOT}/ or {SHARED_FLOW_ROOT}/ folder in it")
    files = BundleFiles(folder.joinpath(*found.parts), label)
    return _BundleReader(files, ROOT_KINDS[found.root]).read()


class _BundleReader:
    """Reads one bundle root; keeps the policy index and the file being read for error messages."""

    def __init__(self, files: BundleFiles, kind: BundleKind) -> None:
        self.files = files
        self.kind = kind
        self.policies = PolicyIndex()
        self.consumed: set[str] = set()

    def read(self) -> Bundle:
        files = self.files
        descriptor = self._descriptor()
        self.policies = read_policies(files)
        self.consumed.update(policy.file for policy in self.policies.policies)

        proxies: list[ProxyEndpoint] = []
        targets: list[TargetEndpoint] = []
        shared: list[SharedFlow] = []
        if self.kind is BundleKind.PROXY:
            proxies = [self._proxy_endpoint(doc) for doc in self._listed(descriptor, PROXIES_FOLDER, "ProxyEndpoint")]
            targets = [self._target_endpoint(doc) for doc in self._listed(descriptor, TARGETS_FOLDER, "TargetEndpoint")]
            self._check_routes(proxies, {target.name for target in targets})
        else:
            shared = [self._shared_flow(doc) for doc in self._listed(descriptor, SHARED_FLOWS_FOLDER, "SharedFlow")]

        resources = read_resources(files)
        self.consumed.update(resource.file for resource in resources)
        return Bundle(
            kind=self.kind,
            name=(descriptor.root.get("name") or "").strip() or PurePosixPath(descriptor.rel).stem,
            descriptor_file=descriptor.rel,
            descriptor=to_element(descriptor.root),
            proxy_endpoints=tuple(proxies),
            target_endpoints=tuple(targets),
            shared_flows=tuple(shared),
            policies=tuple(self.policies.policies),
            resources=tuple(resources),
            other_files=tuple(rel for rel in files.files if rel not in self.consumed),
        )

    # ------------------------------------------------------------ files

    def _descriptor(self) -> XmlFile:
        """The bundle descriptor: the XML file at the bundle root whose root element is APIProxy (or SharedFlowBundle)."""
        files = self.files
        tag = DESCRIPTOR_TAGS[self.kind]
        candidates = [doc for rel in files.in_folder("", ".xml") if (doc := files.load_xml(rel)).root.tag == tag]
        if not candidates:
            raise files.error(f"no bundle descriptor: {files.root_name}/ has no XML file with a <{tag}> root element")
        if len(candidates) > 1:
            shown = ", ".join(files.shown(doc.rel) for doc in candidates)
            raise files.error(f"several bundle descriptors with a <{tag}> root element: {shown}")
        self.consumed.add(candidates[0].rel)
        return candidates[0]

    def _listed(self, descriptor: XmlFile, folder: str, tag: str) -> list[XmlFile]:
        """Every ``<tag>`` file in ``folder``; each one the descriptor lists must be among them."""
        files = self.files
        docs: dict[str, XmlFile] = {}
        for rel in files.in_folder(folder, ".xml"):
            doc = files.load_xml(rel)
            if doc.root.tag != tag:
                raise files.error(f"{files.shown(rel)} is not a {tag} (its root element is <{doc.root.tag}>)")
            name = (doc.root.get("name") or "").strip() or PurePosixPath(rel).stem
            if name in docs:
                raise files.error(f"two {tag}s are named {name}: {files.shown(docs[name].rel)} and {files.shown(rel)}")
            docs[name] = doc
            self.consumed.add(rel)
        stems = {PurePosixPath(doc.rel).stem for doc in docs.values()}
        group = descriptor.root.find(f"{tag}s")
        for entry in group.findall(tag) if group is not None else []:
            listed = text_of(entry)
            if listed is not None and listed not in docs and listed not in stems:
                raise files.error(
                    f"{files.shown(f'{folder}/{listed}.xml')} is missing: the descriptor "
                    f"{files.shown(descriptor.rel)} lists {tag} {listed}"
                )
        return list(docs.values())

    # ------------------------------------------------------------ endpoints

    def _proxy_endpoint(self, doc: XmlFile) -> ProxyEndpoint:
        root = doc.root
        connection = root.find("HTTPProxyConnection")
        flows = _EndpointFlows(self, doc, _PROXY_TAGS, connection)
        endpoint = ProxyEndpoint(
            name=_name(doc),
            file=doc.rel,
            base_path=text_of(connection.find("BasePath")) if connection is not None else None,
            virtual_hosts=tuple(
                host for e in (connection.findall("VirtualHost") if connection is not None else []) if (host := text_of(e))
            ),
            properties=_properties(connection),
            pre_flow=flows.pre_flow,
            post_flow=flows.post_flow,
            post_client_flow=flows.optional("PostClientFlow"),
            flows=flows.flows,
            fault_rules=flows.fault_rules,
            default_fault_rule=flows.default_fault_rule,
            route_rules=tuple(
                RouteRule(
                    name=rule.get("name", ""),
                    condition=text_of(rule.find("Condition")),
                    target=text_of(rule.find("TargetEndpoint")),
                    url=text_of(rule.find("URL")),
                )
                for rule in root.findall("RouteRule")
            ),
            connection=to_element(connection) if connection is not None else None,
            other_elements=flows.other_elements,
            raw_xml=doc.text,
        )
        flows.check_all_steps_read()
        return endpoint

    def _target_endpoint(self, doc: XmlFile) -> TargetEndpoint:
        root = doc.root
        connection = next((c for tag in _TARGET_CONNECTION_TAGS if (c := root.find(tag)) is not None), None)
        flows = _EndpointFlows(self, doc, _TARGET_TAGS, connection)
        is_http = connection is not None and connection.tag == "HTTPTargetConnection"
        endpoint = TargetEndpoint(
            name=_name(doc),
            file=doc.rel,
            url=text_of(connection.find("URL")) if is_http and connection is not None else None,
            properties=_properties(connection),
            pre_flow=flows.pre_flow,
            post_flow=flows.post_flow,
            event_flow=flows.optional("EventFlow"),
            flows=flows.flows,
            fault_rules=flows.fault_rules,
            default_fault_rule=flows.default_fault_rule,
            connection=to_element(connection) if connection is not None else None,
            other_elements=flows.other_elements,
            raw_xml=doc.text,
        )
        flows.check_all_steps_read()
        return endpoint

    def _shared_flow(self, doc: XmlFile) -> SharedFlow:
        root = doc.root
        steps = self.steps(doc, root)
        flow = SharedFlow(
            name=_name(doc),
            file=doc.rel,
            steps=steps,
            other_elements=tuple(to_element(child) for child in root if child.tag != "Step"),
            raw_xml=doc.text,
        )
        _check_step_count(self.files, doc, len(steps))
        return flow

    def _check_routes(self, proxies: list[ProxyEndpoint], targets: set[str]) -> None:
        for endpoint in proxies:
            for rule in endpoint.route_rules:
                if rule.target is not None and rule.target not in targets:
                    raise self.files.error(
                        f"{self.files.shown(endpoint.file)}: RouteRule {rule.name} points at TargetEndpoint "
                        f"{rule.target}, which this bundle does not have"
                    )

    # ------------------------------------------------------------ steps

    def steps(self, doc: XmlFile, parent: ET.Element | None) -> tuple[Step, ...]:
        """The ``<Step>`` children of ``parent`` in order, each resolved to its policy."""
        if parent is None:
            return ()
        return tuple(self._step(doc, element) for element in parent.findall("Step"))

    def _step(self, doc: XmlFile, element: ET.Element) -> Step:
        files = self.files
        name = text_of(element.find("Name"))
        if name is None:
            raise files.error(f"{files.shown(doc.rel)} has a Step with no Name")
        policy = self.policies.resolve(name)
        if policy is None:
            raise files.error(
                f"{files.shown(doc.rel)} has a step {name} that names no policy in this bundle "
                f"(no {files.shown(f'{POLICIES_FOLDER}/{name}.xml')} and no policy named {name})"
            )
        return Step(
            name=name,
            condition=text_of(element.find("Condition")),
            policy=policy.name,
            shared_flow=self.policies.shared_flows.get(policy.name),
        )


class _EndpointFlows:
    """The flows, fault rules and leftover elements of one endpoint file, with a count of the steps read."""

    def __init__(self, reader: _BundleReader, doc: XmlFile, modeled: frozenset[str], connection: ET.Element | None):
        self.reader = reader
        self.doc = doc
        root = doc.root
        self.step_count = 0
        self.pre_flow = self._flow_steps(root.find("PreFlow")) or FlowSteps()
        self.post_flow = self._flow_steps(root.find("PostFlow")) or FlowSteps()
        flows = root.find("Flows")
        self.flows = tuple(
            Flow(
                name=flow.get("name", ""),
                condition=text_of(flow.find("Condition")),
                request=self._steps(flow.find("Request")),
                response=self._steps(flow.find("Response")),
            )
            for flow in (flows.findall("Flow") if flows is not None else [])
        )
        rules = root.find("FaultRules")
        self.fault_rules = tuple(
            FaultRule(name=rule.get("name", ""), condition=text_of(rule.find("Condition")), steps=self._steps(rule))
            for rule in (rules.findall("FaultRule") if rules is not None else [])
        )
        default = root.find("DefaultFaultRule")
        self.default_fault_rule = (
            DefaultFaultRule(
                name=default.get("name", ""),
                always_enforce=self._always_enforce(default.find("AlwaysEnforce")),
                condition=text_of(default.find("Condition")),
                steps=self._steps(default),
            )
            if default is not None
            else None
        )
        # Every RouteRule is read; of each other modeled tag only the first element is, so a repeat is
        # kept here (and its steps are refused by check_all_steps_read).
        repeated = {"RouteRule"} & modeled
        read = {id(element) for tag in modeled - repeated if (element := root.find(tag)) is not None}
        self.other_elements = tuple(
            to_element(child)
            for child in root
            if child is not connection and child.tag not in repeated and id(child) not in read
        )

    def _always_enforce(self, element: ET.Element | None) -> bool:
        text = text_of(element)
        if text is None:
            return False
        flag = XML_BOOLEANS.get(text)
        if flag is None:
            files = self.reader.files
            raise files.error(f"{files.shown(self.doc.rel)} has AlwaysEnforce {text!r}, which is not true or false")
        return flag

    def optional(self, tag: str) -> FlowSteps | None:
        return self._flow_steps(self.doc.root.find(tag))

    def _flow_steps(self, element: ET.Element | None) -> FlowSteps | None:
        if element is None:
            return None
        return FlowSteps(request=self._steps(element.find("Request")), response=self._steps(element.find("Response")))

    def _steps(self, parent: ET.Element | None) -> tuple[Step, ...]:
        steps = self.reader.steps(self.doc, parent)
        self.step_count += len(steps)
        return steps

    def check_all_steps_read(self) -> None:
        _check_step_count(self.reader.files, self.doc, self.step_count)


def _check_step_count(files: BundleFiles, doc: XmlFile, read: int) -> None:
    """Refuse a file with a ``<Step>`` outside the places a2m reads, rather than drop it."""
    total = sum(1 for _ in doc.root.iter("Step"))
    if total != read:
        raise files.error(
            f"{files.shown(doc.rel)} has {total} Step elements but only {read} are where Apigee runs them "
            "(PreFlow, PostFlow, PostClientFlow, EventFlow, Flows, FaultRules, DefaultFaultRule or a shared flow); "
            "a2m does not drop steps, so this bundle is not read"
        )


def _name(doc: XmlFile) -> str:
    return (doc.root.get("name") or "").strip() or PurePosixPath(doc.rel).stem


def _properties(connection: ET.Element | None) -> dict[str, str]:
    """``<Properties><Property name="...">value</Property>`` of a connection, in file order."""
    if connection is None:
        return {}
    group = connection.find("Properties")
    if group is None:
        return {}
    return {prop.get("name", ""): (prop.text or "").strip() for prop in group.findall("Property")}
