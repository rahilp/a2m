"""CP2: read Apigee proxy and shared flow bundles into a plain data model.

Public entry points used here (CP2 plan; nothing else is imported):

    a2m.parser.read_bundle(path: Path) -> Bundle
        ``path`` is a folder holding ``apiproxy/`` or ``sharedflowbundle/``
        (the shape of ``ProxyContext.bundle_dir``), or a ``.zip`` whose root
        holds one of them (the Apigee export layout).
    bundle.to_json() -> str            the saved JSON text
    type(bundle).from_json(text) -> Bundle
    a2m.errors.BundleError             raised for a bundle that cannot be read
    a2m.cli.main([...])                one batch case (CP2-T15)

Assertions are made on ``json.loads`` of the saved JSON. The JSON contract the
tests pin (keys a2m writes; extra keys are allowed):

    {"kind": "proxy" | "sharedflow", "name": str,
     "proxy_endpoints": [{"name", "base_path", "pre_flow", "post_flow", "flows",
                          "fault_rules", "default_fault_rule", "route_rules"}],
     "target_endpoints": [{"name", "url", "properties": {str: str}, "pre_flow",
                           "post_flow", "flows", "fault_rules", "default_fault_rule"}],
     "shared_flows": [{"name", "steps"}],
     "policies": [{"name", "type", "enabled": bool, "continue_on_error": bool,
                   "settings": <nested JSON holding every setting>, "raw_xml": str}],
     "resources": [{"kind": "jsc" | "py" | "java" | ..., "name", "text": str | null,
                    "binary": bool}]}
    pre_flow / post_flow: {"request": [step], "response": [step]}
    flow: {"name", "condition": str | null, "request": [step], "response": [step]}
    fault rule: {"name", "condition": str | null, "steps": [step]}
    default_fault_rule: {"name", "always_enforce": bool, "steps": [step]} or null
    route rule: {"name", "condition": str | null, "target": str | null}
    step: {"name": <as written in the Step>, "condition": str | null,
           "policy": <the "name" of the policy it resolves to>,
           "shared_flow": <shared flow a FlowCallout step calls> | null}

No network, no Java, no API keys; every write is under tmp_path.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import time
import traceback
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any
from xml.parsers.expat import ExpatError

import pytest

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "apigee"
ORDERS = FIXTURES / "orders-api"
AUDIT = FIXTURES / "audit-flow"
TEST_API = FIXTURES / "azure" / "Test-API"
GET_SHARED_FLOW = FIXTURES / "azure" / "GetSharedFlow"
BROKEN_XML = FIXTURES / "malformed" / "broken-xml"
MISSING_POLICY = FIXTURES / "malformed" / "missing-policy"
MISSING_ENDPOINT = FIXTURES / "malformed" / "missing-endpoint"
ORDERS_POLICIES = ORDERS / "apiproxy" / "policies"
ORDERS_RESOURCES = ORDERS / "apiproxy" / "resources"

FIXED_ZIP_TIME = (2026, 1, 1, 0, 0, 0)
SECRET = "TOPSECRET-123"
FLAG_ATTRIBUTES = {"name", "enabled", "continueOnError"}

ORDERS_POLICY_TYPES = {
    "VA-Key": "VerifyAPIKey",
    "SA-Limit": "SpikeArrest",
    "EV-OrderId": "ExtractVariables",
    "AM-AddHeader": "AssignMessage",
    "RF-BadType": "RaiseFault",
    "JS-Validate": "Javascript",
    "PY-Enrich": "Script",
    "RF-Unauthorized": "RaiseFault",
    "AM-ErrorBody": "AssignMessage",
    "FC-Audit": "FlowCallout",
    "OA-Token": "OAuthV2",
    "JC-Sign": "JavaCallout",
    "Q-Unused": "Quota",
}


# ---------------------------------------------------------------- helpers


def read_bundle(path: Path) -> Any:
    from a2m.parser import read_bundle as _read_bundle

    return _read_bundle(path)


def bundle_error() -> type[BaseException]:
    from a2m.errors import BundleError

    return BundleError


def save(bundle: Any, tmp_path: Path, label: str = "bundle") -> Path:
    """Save the bundle's JSON under tmp_path as a2m would, and return the file."""
    text = bundle.to_json()
    assert isinstance(text, str)
    out = tmp_path / "saved" / f"{label}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return out


def saved_json(path: Path, tmp_path: Path, label: str = "bundle") -> dict[str, Any]:
    """Read the bundle at ``path``, save its JSON and load it back."""
    data = json.loads(save(read_bundle(path), tmp_path, label).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def by_name(items: list[dict[str, Any]], name: str) -> dict[str, Any]:
    found = [item for item in items if item["name"] == name]
    assert len(found) == 1, f"expected exactly one {name!r}, found {len(found)} in {[i['name'] for i in items]}"
    return found[0]


def names(steps: list[dict[str, Any]]) -> list[str]:
    return [step["name"] for step in steps]


def proxy_endpoint(data: dict[str, Any], name: str = "default") -> dict[str, Any]:
    return by_name(data["proxy_endpoints"], name)


def target_endpoint(data: dict[str, Any], name: str) -> dict[str, Any]:
    return by_name(data["target_endpoints"], name)


def policy(data: dict[str, Any], name: str) -> dict[str, Any]:
    return by_name(data["policies"], name)


def endpoint_steps(endpoint: dict[str, Any]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for part in ("pre_flow", "post_flow"):
        steps += endpoint[part]["request"] + endpoint[part]["response"]
    for flow in endpoint["flows"]:
        steps += flow["request"] + flow["response"]
    for rule in endpoint["fault_rules"]:
        steps += rule["steps"]
    if endpoint["default_fault_rule"] is not None:
        steps += endpoint["default_fault_rule"]["steps"]
    return steps


def all_steps(data: dict[str, Any]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for endpoint in data["proxy_endpoints"] + data["target_endpoints"]:
        steps += endpoint_steps(endpoint)
    for shared in data["shared_flows"]:
        steps += shared["steps"]
    return steps


def leaves(value: Any) -> set[str]:
    """Every scalar in a JSON value (dict keys included), as stripped text."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            found.add(str(key).strip())
            found |= leaves(item)
    elif isinstance(value, list):
        for item in value:
            found |= leaves(item)
    elif isinstance(value, bool):
        found.add("true" if value else "false")
    elif isinstance(value, (int, float)):
        found.add(str(value))
    elif isinstance(value, str):
        found.add(value.strip())
    return found


def xml_values(path: Path) -> set[str]:
    """Every attribute value and non-empty text in a policy file, except the flag attributes on its root."""
    root = ET.parse(path).getroot()
    values: set[str] = set()
    for element in root.iter():
        for attr, val in element.attrib.items():
            if element is root and attr in FLAG_ATTRIBUTES:
                continue
            values.add(val.strip())
        if element.text and element.text.strip():
            values.add(element.text.strip())
    return values


def independent_step_count(bundle_folder: Path) -> int:
    """Count <Step> elements in the endpoint and shared flow XML files, read directly."""
    roots = [p for p in (bundle_folder / "apiproxy", bundle_folder / "sharedflowbundle") if p.is_dir()]
    assert len(roots) == 1
    count = 0
    for sub in ("proxies", "targets", "sharedflows"):
        for xml_file in sorted((roots[0] / sub).glob("*.xml")):
            count += sum(1 for _ in ET.parse(xml_file).getroot().iter("Step"))
    return count


def zip_bundle(folder: Path, dest: Path) -> Path:
    """Zip a fixture bundle folder with its bundle root at the zip root, deterministically."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                info = zipfile.ZipInfo(path.relative_to(folder).as_posix(), FIXED_ZIP_TIME)
                zf.writestr(info, path.read_bytes())
    return dest


def raw_parser_errors() -> tuple[type[BaseException], ...]:
    errors: list[type[BaseException]] = [ET.ParseError, ExpatError]
    try:
        defused = importlib.import_module("defusedxml")
    except ImportError:
        pass
    else:
        errors.append(defused.DefusedXmlException)
    return tuple(errors)


def full_error_text(exc: BaseException) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


# ---------------------------------------------------------------- CP2-T01


def test_CP2_T01_endpoints_with_base_path_and_target_address(tmp_path: Path) -> None:
    """[CP2-T01] Proxy and target endpoints are read with their base path and target address."""
    orders = saved_json(ORDERS, tmp_path, "orders")
    assert orders["kind"] == "proxy"
    assert orders["name"] == "orders-api"
    assert [e["name"] for e in orders["proxy_endpoints"]] == ["default"]
    assert proxy_endpoint(orders)["base_path"] == "/orders/v1"
    assert sorted(e["name"] for e in orders["target_endpoints"]) == ["backend", "legacy"]
    assert target_endpoint(orders, "backend")["url"] == "https://orders.example.com/api"
    assert target_endpoint(orders, "legacy")["url"] == "https://legacy.example.com/v0"

    test_api = saved_json(TEST_API, tmp_path, "test-api")
    assert test_api["kind"] == "proxy"
    assert test_api["name"] == "Test-API"
    assert [e["name"] for e in test_api["proxy_endpoints"]] == ["default"]
    assert proxy_endpoint(test_api)["base_path"] == "/profile"
    assert [e["name"] for e in test_api["target_endpoints"]] == ["default"]
    target = target_endpoint(test_api, "default")
    assert target["url"] == "https://SetInDynamicUrlSharedflow"
    assert target["properties"] == {"io.timeout.millis": "180000"}


# ---------------------------------------------------------------- CP2-T02


def test_CP2_T02_flows_keep_step_order_and_conditions(tmp_path: Path) -> None:
    """[CP2-T02] Pre-flows, post-flows and conditional flows keep their steps in the original order, with conditions."""
    data = saved_json(ORDERS, tmp_path)
    proxy = proxy_endpoint(data)

    assert names(proxy["pre_flow"]["request"]) == ["VA-Key", "SA-Limit"]
    assert proxy["pre_flow"]["response"] == []

    assert [f["name"] for f in proxy["flows"]] == ["GetOrder", "CreateOrder"]
    get_order, create_order = proxy["flows"]
    assert get_order["condition"] == '(proxy.pathsuffix MatchesPath "/{id}") and (request.verb = "GET")'
    assert names(get_order["request"]) == ["EV-OrderId"]
    assert names(get_order["response"]) == ["AM-AddHeader"]
    assert create_order["condition"] == 'request.verb = "POST" && request.header.x-tenant != null'
    assert names(create_order["request"]) == ["RF-BadType", "JS-Validate", "PY-Enrich"]
    assert create_order["response"] == []
    bad_type, js_validate, py_enrich = create_order["request"]
    assert bad_type["condition"] == 'request.header.Content-Type != "application/json"'
    assert js_validate["condition"] is None
    assert py_enrich["condition"] is None

    assert proxy["post_flow"]["request"] == []
    assert names(proxy["post_flow"]["response"]) == ["AM-AddHeader"]

    backend = target_endpoint(data, "backend")
    assert names(backend["pre_flow"]["request"]) == ["FC-Audit"]
    assert backend["pre_flow"]["response"] == []
    assert backend["post_flow"]["request"] == []
    assert names(backend["post_flow"]["response"]) == ["OA-Token", "JC-Sign"]
    assert backend["flows"] == []

    legacy = target_endpoint(data, "legacy")
    assert endpoint_steps(legacy) == []
    assert legacy["flows"] == []


# ---------------------------------------------------------------- CP2-T03


def test_CP2_T03_fault_rules_and_route_rules(tmp_path: Path) -> None:
    """[CP2-T03] Fault rules and route rules are read, including conditions and a route with no target."""
    data = saved_json(ORDERS, tmp_path, "orders")
    proxy = proxy_endpoint(data)

    assert len(proxy["fault_rules"]) == 1
    rule = proxy["fault_rules"][0]
    assert rule["name"] == "InvalidKey"
    assert rule["condition"] == 'fault.name = "InvalidApiKey"'
    assert names(rule["steps"]) == ["RF-Unauthorized"]

    default_rule = proxy["default_fault_rule"]
    assert default_rule["name"] == "all"
    assert default_rule["always_enforce"] is True
    assert names(default_rule["steps"]) == ["AM-ErrorBody"]

    routes = [(r["name"], r["condition"], r["target"]) for r in proxy["route_rules"]]
    assert routes == [
        ("mock", 'request.header.x-mock = "true"', None),
        ("legacy", 'request.header.x-legacy = "1"', "legacy"),
        ("default", None, "backend"),
    ]

    test_api = proxy_endpoint(saved_json(TEST_API, tmp_path, "test-api"))
    assert test_api["fault_rules"] == []
    assert test_api["default_fault_rule"] is None
    assert [(r["name"], r["condition"], r["target"]) for r in test_api["route_rules"]] == [
        ("default", None, "default")
    ]


# ---------------------------------------------------------------- CP2-T04


def test_CP2_T04_policies_with_type_flags_and_all_settings(tmp_path: Path) -> None:
    """[CP2-T04] Every policy appears with its type, flags and all of its settings."""
    data = saved_json(ORDERS, tmp_path)

    assert len(data["policies"]) == 13
    assert {p["name"]: p["type"] for p in data["policies"]} == ORDERS_POLICY_TYPES

    assert policy(data, "JS-Validate")["continue_on_error"] is True
    assert policy(data, "JS-Validate")["enabled"] is True
    assert policy(data, "PY-Enrich")["enabled"] is False
    assert policy(data, "PY-Enrich")["continue_on_error"] is False
    assert policy(data, "VA-Key")["enabled"] is True
    assert policy(data, "VA-Key")["continue_on_error"] is False
    create_order = by_name(proxy_endpoint(data)["flows"], "CreateOrder")
    assert "PY-Enrich" in names(create_order["request"])

    sa_settings = leaves(policy(data, "SA-Limit")["settings"])
    assert {"30pm", "request.header.client_id"} <= sa_settings
    assert {"X-Served-By", "a2m"} <= leaves(policy(data, "AM-AddHeader")["settings"])

    for name in ORDERS_POLICY_TYPES:
        expected = xml_values(ORDERS_POLICIES / f"{name}.xml")
        missing = expected - leaves(policy(data, name)["settings"])
        assert not missing, f"{name}: settings lost {sorted(missing)}"


# ---------------------------------------------------------------- CP2-T05


def test_CP2_T05_unknown_and_unreferenced_policies_are_kept(tmp_path: Path) -> None:
    """[CP2-T05] A policy type a2m does not know, and a policy no step uses, are kept rather than dropped."""
    data = saved_json(ORDERS, tmp_path)

    token = policy(data, "OA-Token")
    assert token["type"] == "OAuthV2"
    assert "VerifyAccessToken" in leaves(token["settings"])
    original = (ORDERS_POLICIES / "OA-Token.xml").read_text(encoding="utf-8")
    assert token["raw_xml"].strip() == original.strip()
    assert ET.fromstring(token["raw_xml"].encode("utf-8")).tag == "OAuthV2"

    quota = policy(data, "Q-Unused")
    assert quota["type"] == "Quota"
    assert "100" in leaves(quota["settings"])

    used = {step["policy"] for step in all_steps(data)}
    assert "Q-Unused" not in used
    assert used == set(ORDERS_POLICY_TYPES) - {"Q-Unused"}


# ---------------------------------------------------------------- CP2-T06


def test_CP2_T06_script_resources_with_exact_contents(tmp_path: Path) -> None:
    """[CP2-T06] JavaScript, Python and Java source resources are listed with their exact contents."""
    data = saved_json(ORDERS, tmp_path)
    resources = {(r["kind"], r["name"]): r for r in data["resources"]}

    for kind, name in (("jsc", "validate.js"), ("py", "enrich.py"), ("java", "Sign.java")):
        assert (kind, name) in resources, f"{kind}/{name} not listed in {sorted(resources)}"
        resource = resources[(kind, name)]
        fixture = ORDERS_RESOURCES / kind / name
        assert resource["binary"] is False
        assert resource["text"].encode("utf-8") == fixture.read_bytes()

    assert "context.getVariable" in resources[("jsc", "validate.js")]["text"]
    assert "flow.setVariable" in resources[("py", "enrich.py")]["text"]
    assert "class Sign" in resources[("java", "Sign.java")]["text"]

    assert "jsc://validate.js" in leaves(policy(data, "JS-Validate")["settings"])
    assert "py://enrich.py" in leaves(policy(data, "PY-Enrich")["settings"])


# ---------------------------------------------------------------- CP2-T07


def test_CP2_T07_jar_listed_as_binary_resource(tmp_path: Path) -> None:
    """[CP2-T07] A compiled Java JAR is listed as a binary resource without crashing."""
    saved = save(read_bundle(ORDERS), tmp_path)
    raw = saved.read_bytes()
    data = json.loads(raw.decode("utf-8"))

    jars = [r for r in data["resources"] if r["name"] == "sign.jar"]
    assert len(jars) == 1
    jar = jars[0]
    assert jar["kind"] == "java"
    assert jar["binary"] is True
    assert jar["text"] is None


# ---------------------------------------------------------------- CP2-T08


def test_CP2_T08_shared_flow_bundles_are_read(tmp_path: Path) -> None:
    """[CP2-T08] A shared flow bundle is read like a proxy."""
    azure = saved_json(GET_SHARED_FLOW, tmp_path, "get-shared-flow")
    assert azure["kind"] == "sharedflow"
    assert azure["name"] == "GetSharedFlow"
    assert azure["proxy_endpoints"] == []
    assert azure["target_endpoints"] == []
    assert [f["name"] for f in azure["shared_flows"]] == ["default"]
    assert names(by_name(azure["shared_flows"], "default")["steps"]) == ["Get-Shared-Flow"]
    kvm = policy(azure, "Get-Shared-Flow")
    assert kvm["type"] == "KeyValueMapOperations"
    assert {"TargetUrls", "MyKey"} <= leaves(kvm["settings"])

    audit = saved_json(AUDIT, tmp_path, "audit-flow")
    assert audit["kind"] == "sharedflow"
    assert audit["name"] == "audit-flow"
    assert audit["proxy_endpoints"] == []
    assert audit["target_endpoints"] == []
    steps = by_name(audit["shared_flows"], "default")["steps"]
    assert names(steps) == ["AM-AuditHeader", "KVM-Lookup"]
    assert steps[0]["condition"] == 'request.verb != "OPTIONS"'
    assert steps[1]["condition"] is None


# ---------------------------------------------------------------- CP2-T09


def test_CP2_T09_flow_callout_step_names_its_shared_flow(tmp_path: Path) -> None:
    """[CP2-T09] A FlowCallout step names the shared flow it calls, even when the policy file and policy name differ."""
    test_api = saved_json(TEST_API, tmp_path, "test-api")
    step = target_endpoint(test_api, "default")["pre_flow"]["request"]
    assert names(step) == ["SharedFlowCallout"]
    callout = step[0]
    assert callout["policy"] == "GetTargetUrlCallout"
    assert policy(test_api, callout["policy"])["type"] == "FlowCallout"
    assert callout["shared_flow"] == "GetSharedFlow"

    orders = saved_json(ORDERS, tmp_path, "orders")
    fc_audit = target_endpoint(orders, "backend")["pre_flow"]["request"][0]
    assert fc_audit["name"] == "FC-Audit"
    assert policy(orders, fc_audit["policy"])["type"] == "FlowCallout"
    assert fc_audit["shared_flow"] == "audit-flow"

    others = [s for s in all_steps(orders) if s["name"] != "FC-Audit"]
    assert len(others) == 12
    assert all(s["shared_flow"] is None for s in others), [s["name"] for s in others if s["shared_flow"]]


# ---------------------------------------------------------------- CP2-T10


@pytest.mark.parametrize(
    ("folder", "total", "per_endpoint"),
    [
        pytest.param(ORDERS, 13, {"default": 10, "backend": 3, "legacy": 0}, id="orders-api"),
        pytest.param(TEST_API, 1, {"default@proxy": 0, "default@target": 1}, id="Test-API"),
        pytest.param(GET_SHARED_FLOW, 1, {}, id="GetSharedFlow"),
        pytest.param(AUDIT, 2, {}, id="audit-flow"),
    ],
)
def test_CP2_T10_step_count_matches_hand_count(
    tmp_path: Path, folder: Path, total: int, per_endpoint: dict[str, int]
) -> None:
    """[CP2-T10] Step count in the JSON matches a hand count of the bundle."""
    data = saved_json(folder, tmp_path)
    assert len(all_steps(data)) == total
    assert len(all_steps(data)) == independent_step_count(folder)

    if folder == TEST_API:
        assert len(endpoint_steps(proxy_endpoint(data))) == per_endpoint["default@proxy"]
        assert len(endpoint_steps(target_endpoint(data, "default"))) == per_endpoint["default@target"]
    elif folder == ORDERS:
        assert len(endpoint_steps(proxy_endpoint(data))) == per_endpoint["default"]
        assert len(endpoint_steps(target_endpoint(data, "backend"))) == per_endpoint["backend"]
        assert len(endpoint_steps(target_endpoint(data, "legacy"))) == per_endpoint["legacy"]


# ---------------------------------------------------------------- CP2-T11


def test_CP2_T11_json_round_trips_and_is_deterministic_for_folder_and_zip(tmp_path: Path) -> None:
    """[CP2-T11] The saved JSON round-trips, is identical on every read, and is the same for a zipped bundle."""
    first = read_bundle(ORDERS)
    second = read_bundle(ORDERS)
    zipped = read_bundle(zip_bundle(ORDERS, tmp_path / "zips" / "orders-api.zip"))

    texts = [save(b, tmp_path, label).read_bytes() for b, label in ((first, "a"), (second, "b"), (zipped, "c"))]
    assert texts[0] == texts[1]
    assert texts[0] == texts[2]

    text = texts[0].decode("utf-8")
    loaded = type(first).from_json(text)
    assert loaded == first
    assert loaded.to_json() == text

    for forbidden in (str(tmp_path), str(REPO), str(Path.home()), "/tmp/"):
        assert forbidden not in text, f"saved JSON contains {forbidden!r}"
    for value in leaves(json.loads(text)):
        if len(value) > 1 and os.path.isabs(value):
            assert not Path(value).exists(), f"saved JSON contains the absolute path {value!r}"


# ---------------------------------------------------------------- CP2-T12


def test_CP2_T12_broken_xml_gives_clear_error_naming_bundle_and_file() -> None:
    """[CP2-T12] A bundle with broken XML gives a clear error naming the proxy and the file."""
    error = bundle_error()
    with pytest.raises(error) as caught:
        read_bundle(BROKEN_XML)
    exc = caught.value
    assert not isinstance(exc, raw_parser_errors()), f"raw parser exception leaked: {type(exc).__name__}"
    message = str(exc)
    assert "\n" not in message
    assert "broken-xml" in message
    assert "proxies/default.xml" in message


# ---------------------------------------------------------------- CP2-T13


@pytest.mark.parametrize(
    ("folder", "named"),
    [
        pytest.param(MISSING_ENDPOINT, "proxies/default.xml", id="missing-endpoint"),
        pytest.param(MISSING_POLICY, "Ghost-Policy", id="missing-policy"),
    ],
)
def test_CP2_T13_missing_endpoint_or_policy_gives_clear_error(folder: Path, named: str) -> None:
    """[CP2-T13] A bundle that points at a missing endpoint or policy file gives a clear error."""
    error = bundle_error()
    with pytest.raises(error) as caught:
        read_bundle(folder)
    message = str(caught.value)
    assert "\n" not in message
    assert named in message
    assert folder.name in message


# ---------------------------------------------------------------- CP2-T14

ENTITY_BOMB = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE SpikeArrest [
  <!ENTITY lol "lol">
  <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
  <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">
  <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
  <!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">
  <!ENTITY lol5 "&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;">
  <!ENTITY lol6 "&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;">
  <!ENTITY lol7 "&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;">
  <!ENTITY lol8 "&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;">
  <!ENTITY lol9 "&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;">
]>
<SpikeArrest async="false" continueOnError="false" enabled="true" name="SA-Limit">
    <Identifier ref="request.header.client_id"/>
    <Rate>&lol9;</Rate>
</SpikeArrest>
"""

EXTERNAL_ENTITY = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE SpikeArrest [
  <!ENTITY xxe SYSTEM "{uri}">
]>
<SpikeArrest async="false" continueOnError="false" enabled="true" name="SA-Limit">
    <Identifier ref="request.header.client_id"/>
    <Rate>&xxe;</Rate>
</SpikeArrest>
"""


@pytest.mark.parametrize("variant", ["internal-entity-expansion", "external-entity"])
def test_CP2_T14_xml_entity_tricks_are_refused(tmp_path: Path, variant: str) -> None:
    """[CP2-T14] XML entity tricks in a bundle are refused, not expanded."""
    secret = tmp_path / "secret.txt"
    secret.write_text(SECRET + "\n", encoding="utf-8")
    bundle = tmp_path / "xxe-bundle"
    shutil.copytree(ORDERS, bundle)
    text = ENTITY_BOMB if variant == "internal-entity-expansion" else EXTERNAL_ENTITY.format(uri=secret.as_uri())
    (bundle / "apiproxy" / "policies" / "SA-Limit.xml").write_text(text, encoding="utf-8")

    error = bundle_error()
    started = time.monotonic()
    with pytest.raises(error) as caught:
        read_bundle(bundle)
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, f"reading took {elapsed:.1f}s"
    assert "policies/SA-Limit.xml" in str(caught.value)
    assert SECRET not in str(caught.value)
    assert SECRET not in full_error_text(caught.value)


# ---------------------------------------------------------------- CP2-T15


def test_CP2_T15_batch_reports_malformed_bundle_and_finishes_the_rest(tmp_path: Path, run_cli) -> None:
    """[CP2-T15] In a batch run, a malformed bundle is reported and the other proxies still finish."""
    exports = tmp_path / "exports"
    for source in (ORDERS, BROKEN_XML, TEST_API):
        shutil.copytree(source, exports / source.name)
    results = tmp_path / "results"

    res = run_cli(["migrate", str(exports), "--out", str(results), "--llm", "fake", "--no-runtime"])

    assert "Traceback (most recent call last)" not in res.out
    assert "Traceback (most recent call last)" not in res.err
    assert res.code == 0, res.err

    lines = [line for line in (results / "run.log").read_text(encoding="utf-8").splitlines() if line.strip()]
    assert any(
        "broken-xml" in line and "proxies/default.xml" in line and "error" in line.lower() for line in lines
    ), "run.log has no error entry naming broken-xml and proxies/default.xml:\n" + "\n".join(lines)

    markers = {p.parent.name for p in results.rglob(".done") if p.is_file()}
    assert markers == {"orders-api", "Test-API"}
