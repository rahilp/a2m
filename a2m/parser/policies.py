"""Read the policy files and resources of a bundle into the IR.

Every file in ``policies/`` becomes a :class:`a2m.ir.Policy` whatever its type:
a type a2m has no template for (OAuthV2, say) is kept with its settings and
raw XML, never dropped. Steps find their policy by the policy's ``name``
attribute first and by its file name second (an export may name the file
differently from the policy).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from a2m.ir import Policy, Resource
from a2m.parser.source import XML_BOOLEANS, BundleFiles, text_of, to_element

POLICIES_FOLDER = "policies"
RESOURCES_FOLDER = "resources"
FLOW_CALLOUT = "FlowCallout"


@dataclass(slots=True)
class PolicyIndex:
    """The bundle's policies in file order, and how a step name resolves to one of them."""

    policies: list[Policy] = field(default_factory=list)
    by_name: dict[str, Policy] = field(default_factory=dict)
    by_file_name: dict[str, Policy] = field(default_factory=dict)
    # The shared flow bundle each FlowCallout policy calls, by policy name.
    shared_flows: dict[str, str | None] = field(default_factory=dict)

    def resolve(self, step_name: str) -> Policy | None:
        return self.by_name.get(step_name) or self.by_file_name.get(step_name)


def read_policies(files: BundleFiles) -> PolicyIndex:
    index = PolicyIndex()
    for rel in files.in_folder(POLICIES_FOLDER, ".xml"):
        doc = files.load_xml(rel)
        root = doc.root
        stem = PurePosixPath(rel).stem
        name = (root.get("name") or "").strip() or stem
        if name in index.by_name:
            raise files.error(
                f"two policies are named {name}: {files.shown(index.by_name[name].file)} and {files.shown(rel)}"
            )
        policy = Policy(
            name=name,
            type=root.tag,
            file=rel,
            enabled=_flag(files, rel, root.get("enabled"), "enabled", default=True),
            continue_on_error=_flag(files, rel, root.get("continueOnError"), "continueOnError", default=False),
            display_name=text_of(root.find("DisplayName")),
            settings=to_element(root),
            raw_xml=doc.text,
        )
        index.policies.append(policy)
        index.by_name[name] = policy
        index.by_file_name[stem] = policy
        if root.tag == FLOW_CALLOUT:
            index.shared_flows[name] = text_of(root.find("SharedFlowBundle"))
    return index


def _flag(files: BundleFiles, rel: str, value: str | None, attribute: str, *, default: bool) -> bool:
    if value is None:
        return default
    flag = XML_BOOLEANS.get(value.strip())
    if flag is None:
        raise files.error(f"{files.shown(rel)} has {attribute}={value!r}, which is not true or false")
    return flag


def read_resources(files: BundleFiles) -> list[Resource]:
    """Every file under ``resources/<kind>/``, its text when it is UTF-8, else marked binary."""
    resources = []
    for rel in files.files:
        if not is_resource(rel):
            continue
        parts = rel.split("/")
        data = files.read_bytes(rel)
        text = _utf8_text(data)
        resources.append(
            Resource(
                kind=parts[1],
                name="/".join(parts[2:]),
                file=rel,
                binary=text is None,
                text=text,
                size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
            )
        )
    return resources


def is_resource(rel: str) -> bool:
    parts = rel.split("/")
    return len(parts) >= 3 and parts[0] == RESOURCES_FOLDER


def _utf8_text(data: bytes) -> str | None:
    """``data`` as text when it is UTF-8 without NUL bytes (a JAR or image is not), else None."""
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None
