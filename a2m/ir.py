"""The intermediate representation (IR) of one Apigee bundle.

Plain frozen dataclasses with no behaviour beyond the JSON round-trip:
:meth:`Bundle.to_json` writes them with :func:`dataclasses.asdict` and
:func:`json.dumps`, and :meth:`Bundle.from_json` rebuilds an equal model from
that text. Sequences are tuples so the model compares equal after a round-trip.

Nothing in a bundle is dropped on the way in. What the IR does not model as a
named field is still kept: every policy keeps its whole XML as an
:class:`XmlElement` tree (``settings``) and verbatim (``raw_xml``); endpoints
and shared flows keep their file text (``raw_xml``) and their top-level
elements a2m does not read (``other_elements``); files a2m does not parse are
listed in :attr:`Bundle.other_files`.

All paths in the IR are relative to the bundle root (``apiproxy/`` or
``sharedflowbundle/``), with forward slashes, so the saved JSON holds no
absolute path.
"""

from __future__ import annotations

import dataclasses
import json
import types
import typing
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeVar, Union

T = TypeVar("T")


class BundleKind(StrEnum):
    PROXY = "proxy"
    SHARED_FLOW = "sharedflow"


@dataclass(frozen=True, slots=True)
class XmlElement:
    """One XML element, losslessly: tag, attributes in file order, text, children and tail text.

    ``text`` and ``tail`` are kept verbatim when they hold anything besides
    whitespace, else None (indentation between elements).
    """

    tag: str
    attributes: dict[str, str]
    text: str | None
    children: tuple[XmlElement, ...]
    tail: str | None = None


@dataclass(frozen=True, slots=True)
class Step:
    """A ``<Step>``: ``name`` as written, ``policy`` the name of the policy it resolves to.

    ``shared_flow`` is the shared flow bundle a FlowCallout policy calls, else None.
    """

    name: str
    condition: str | None
    policy: str
    shared_flow: str | None


@dataclass(frozen=True, slots=True)
class FlowSteps:
    """The request and response steps of a PreFlow, PostFlow, PostClientFlow or EventFlow."""

    request: tuple[Step, ...] = ()
    response: tuple[Step, ...] = ()


@dataclass(frozen=True, slots=True)
class Flow:
    """A conditional ``<Flow>`` in an endpoint's ``<Flows>``."""

    name: str
    condition: str | None
    request: tuple[Step, ...]
    response: tuple[Step, ...]


@dataclass(frozen=True, slots=True)
class FaultRule:
    name: str
    condition: str | None
    steps: tuple[Step, ...]


@dataclass(frozen=True, slots=True)
class DefaultFaultRule:
    name: str
    always_enforce: bool
    condition: str | None
    steps: tuple[Step, ...]


@dataclass(frozen=True, slots=True)
class RouteRule:
    """A ``<RouteRule>``: ``target`` is a TargetEndpoint name, or None for a null route (or a ``url`` route)."""

    name: str
    condition: str | None
    target: str | None
    url: str | None = None


@dataclass(frozen=True, slots=True)
class ProxyEndpoint:
    name: str
    file: str
    base_path: str | None
    virtual_hosts: tuple[str, ...]
    properties: dict[str, str]
    pre_flow: FlowSteps
    post_flow: FlowSteps
    post_client_flow: FlowSteps | None
    flows: tuple[Flow, ...]
    fault_rules: tuple[FaultRule, ...]
    default_fault_rule: DefaultFaultRule | None
    route_rules: tuple[RouteRule, ...]
    # The whole <HTTPProxyConnection>, for what the named fields above do not hold.
    connection: XmlElement | None
    other_elements: tuple[XmlElement, ...]
    raw_xml: str


@dataclass(frozen=True, slots=True)
class TargetEndpoint:
    name: str
    file: str
    url: str | None
    properties: dict[str, str]
    pre_flow: FlowSteps
    post_flow: FlowSteps
    event_flow: FlowSteps | None
    flows: tuple[Flow, ...]
    fault_rules: tuple[FaultRule, ...]
    default_fault_rule: DefaultFaultRule | None
    # The whole <HTTPTargetConnection> (or <LocalTargetConnection>), e.g. SSLInfo, LoadBalancer, Path.
    connection: XmlElement | None
    other_elements: tuple[XmlElement, ...]
    raw_xml: str


@dataclass(frozen=True, slots=True)
class SharedFlow:
    name: str
    file: str
    steps: tuple[Step, ...]
    other_elements: tuple[XmlElement, ...]
    raw_xml: str


@dataclass(frozen=True, slots=True)
class Policy:
    """A policy file: ``type`` is its root element (SpikeArrest, OAuthV2, ...), known to a2m or not."""

    name: str
    type: str
    file: str
    enabled: bool
    continue_on_error: bool
    display_name: str | None
    settings: XmlElement
    raw_xml: str


@dataclass(frozen=True, slots=True)
class Resource:
    """A file under ``resources/<kind>/``: text when it is UTF-8, else ``binary`` with no text."""

    kind: str
    name: str
    file: str
    binary: bool
    text: str | None
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class Bundle:
    kind: BundleKind
    name: str
    descriptor_file: str
    descriptor: XmlElement
    proxy_endpoints: tuple[ProxyEndpoint, ...]
    target_endpoints: tuple[TargetEndpoint, ...]
    shared_flows: tuple[SharedFlow, ...]
    policies: tuple[Policy, ...]
    resources: tuple[Resource, ...]
    # Files in the bundle root that a2m does not parse (e.g. manifests/), sorted.
    other_files: tuple[str, ...]

    def to_json(self) -> str:
        """The saved JSON text: same model, same bytes."""
        return json.dumps(dataclasses.asdict(self), indent=2, ensure_ascii=False) + "\n"

    @classmethod
    def from_json(cls, text: str) -> Bundle:
        """Rebuild the model saved by :meth:`to_json`; raises ValueError for JSON of another shape."""
        return _decode(cls, json.loads(text))


def _decode(hint: Any, value: Any) -> Any:
    """Turn the JSON ``value`` back into an instance of the type ``hint``."""
    origin = typing.get_origin(hint)
    if origin in (Union, types.UnionType):
        options = typing.get_args(hint)
        if value is None and type(None) in options:
            return None
        (inner,) = [option for option in options if option is not type(None)]
        return _decode(inner, value)
    if origin is tuple:
        item_type = typing.get_args(hint)[0]
        return tuple(_decode(item_type, item) for item in _expect(value, list, hint))
    if origin is dict:
        value_type = typing.get_args(hint)[1]
        return {str(key): _decode(value_type, item) for key, item in _expect(value, dict, hint).items()}
    if isinstance(hint, type) and dataclasses.is_dataclass(hint):
        data = _expect(value, dict, hint)
        hints = typing.get_type_hints(hint)
        fields = dataclasses.fields(hint)
        unknown = set(data) - {f.name for f in fields}
        missing = [
            f.name
            for f in fields
            if f.name not in data and f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
        ]
        if unknown or missing:
            raise ValueError(f"{hint.__name__}: unexpected keys {sorted(unknown)}, missing keys {missing}")
        return hint(**{name: _decode(hints[name], item) for name, item in data.items()})
    if isinstance(hint, type) and issubclass(hint, StrEnum):
        return hint(_expect(value, str, hint))
    if hint in (str, bool, int):
        return _expect(value, hint, hint)
    raise ValueError(f"cannot decode type {hint!r}")


def _expect(value: Any, kind: type[T], hint: Any) -> T:
    # bool is an int subclass; an int field must not accept true/false, nor a bool field 0/1.
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise ValueError(f"expected {kind.__name__} for {hint!r}, got {type(value).__name__}")
    return value
