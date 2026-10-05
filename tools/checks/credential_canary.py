"""Credential canary check: the guardrail for "secret-sent-to-ai-provider".

Usage: ``python tools/checks/credential_canary.py <target>``

``<target>`` is an a2m package directory (``a2m``), a directory holding one (a mini a2m-shaped package such as the
fixtures under ``tests/fixtures/guardrails``), or any file inside such a directory. The check imports that package,
never the project's own installed one, in a fresh isolated Python process, and drives it through the interfaces it
exposes, with distinct, obviously fake canary secrets planted in every position a credential can hold and harmless
look-alike values next to them:

* full pipeline (when ``a2m.verify.fix_loop.run_with_fixes`` exists): ``a2m migrate`` through ``a2m.cli.main`` with
  the engine's own stages, a fake failing runner and the fake AI provider wrapped to capture every request it gets
  (every kind: callout and condition translations as well as fix requests), on a golden run and a mock-backend
  battery run of three proxies (credential literals in every policy position, JavaScript and Python custom code
  with credentials as quoted object keys too, a JavaCallout, numeric credentials in the data positions of every
  policy type and of the target endpoint, a negative JSON number (its sign too), conditions (a numeric compared
  value too), numeric literals of custom code, URLs and properties; credentials in golden traffic;
  look-alikes). No canary may reach any field of
  any AI request; no traffic credential (Authorization, Basic, Cookie, Set-Cookie, VerifyAPIKey key) may reach
  run.log, any log record (captured at the root logger, after propagation from whichever logger wrote it) or
  verification.json; and every look-alike must stay visible in run.log and verification.json and never be masked
  in an AI request.
* second fix request (when ``run_with_fixes`` exists): ``a2m migrate`` with two fix attempts on a proxy whose
  AssignMessage sets headers to canaries holding ``$``, both quotes and backslashes, and whose second AssignMessage
  sets flow variables (which no battery case tests, so no failing diff quotes them) to credential-shaped canaries a
  masker changes only in part (``Bearer``, ``Basic``, a JWT and an ``Authorization: <key>`` value, with a character
  a token cannot hold inside such as ``:`` or ``$``, and a token of token characters only). The fake AI's first fix
  quotes them by placeholder in a guard a2m refuses, or in a variable a2m writes whose build or deploy then fails
  with the error Maven or Mule writes quoting the restored code (Mule's "Caused by" line with the expression's
  repr). No run of five or more letters and digits of any of these canaries, and no masked form of a credential
  canary's first characters (``Brq7***``), may reach any field of either fix request. Each scenario must be seen to
  do what it is for (attempt 1 refused for its guard; the start of the fixed app failed as planned), or the check
  reports that it could not check.
* request builders (public functions of ``a2m.verify.fix_loop`` whose parameters it can fill, such as a policy text,
  a diff, headers, a token and a masker): every planted canary is a credential, so none may be in what the function
  returns or in any log record it writes (captured at the root logger, after propagation). The verdict comes from
  the canary list alone, never from what the target's own masker can hide: a masker that hides nothing fails.
* the masker (``a2m.verify.masking.Masker``): its text masking must leave look-alike values visible.
* the run.log funnel (``Masker.logging()``): a credential passed as a ``%s`` logging argument must be masked exactly
  like the same credential written inline, and an ``Authorization: Bearer`` credential (a credential by its name
  alone) must be masked in both, as seen by a handler on the root logger.

An interface that is present but cannot be driven (an import error, a crash, a scenario that never reached the fix
loop) is a failure, never a pass. Exit 0: no canary leaked and no look-alike was masked. Exit 1: each leaked canary
and each masked look-alike is named with where it appeared. Exit 2: the target could not be checked; that output
never uses the words a detection uses (they are abbreviated, see ``neutral``), so it cannot be read as one.

The check writes only inside its own temporary folder and prints only the fake canaries, never a real secret.
"""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import importlib
import inspect
import io
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import traceback
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

PREFIX = "credential canary check"
# Exit 2 ("could not check") output never uses the words a detection report uses, so that an error can never be read
# as a detection: its prefix names no problem, and every line goes through ``neutral``.
ERROR_PREFIX = "a2m guardrail check"
DETECTION_WORDS = re.compile(r"look-?alike|credential|secret|canary|mask|leak", re.IGNORECASE)
NEUTRAL_FORMS = {
    "look-alike": "look-similar", "lookalike": "looksimilar", "credential": "cred", "secret": "scrt", "canary": "cnry",
    "mask": "msk", "leak": "lk",
}
WORKER_FLAG = "--worker"
WORKER_TIMEOUT_SECONDS = 100
FIX_REASON = "credential canary check: no fix offered"

# ---------------------------------------------------------------- canaries and look-alikes

# Literal credentials of the proxy itself (policies, code, conditions, URLs, properties) and values the golden
# recording or the backend holds under custom names: the AI provider must never see them. a2m may show them in the
# user's own local run outputs (a failing header-set diff names the expected value), so only the fix requests are
# checked for these.
PROVIDER_CANARIES: dict[str, tuple[str, str]] = {
    "am-header": ("CANARY-AMHDR-7Q2W9E4R", "AssignMessage header X-Partner-Api-Key literal"),
    "am-code": ("CANARY-AMCODE-3T6Y1U8I", "AssignMessage query parameter code (function key) literal"),
    "am-sig": ("CANARY-AMSIG-5O0P2A7S", "AssignMessage query parameter sig literal"),
    "am-appid": ("CANARY-AMAPPID-9D4F6G1H", "AssignMessage query parameter appid literal"),
    "am-variable": ("CANARY-AMVAR-8N3M7Q2W", "AssignMessage AssignVariable backend.password literal"),
    "soap-password": ("CANARY-SOAPPW-2J5K8L3Z", "CDATA SOAP payload wsse:Password literal"),
    "json-secret": ("CANARY-JSONSEC-6X1C4V9B", "JSON payload client_secret literal"),
    "ba-user": ("CANARY-BAUSER-4E9R2T6Y", "BasicAuthentication User literal"),
    "ba-password": ("CANARY-BAPW-1U7I3O5P", "BasicAuthentication Password literal"),
    "kvm-value": ("CANARY-KVMPW-0A6S8D2F", "KVM InitialEntries backend_password value"),
    "sc-userinfo": ("CANARY-SCUSER-5G1H9J4K", "ServiceCallout URL user information"),
    "sc-query": ("CANARY-SCKEY-7L3Z6X0C", "ServiceCallout URL key query value"),
    "js-source": ("CANARY-JSKEY-2V8B4N1M", "custom code (JavaScript) string literal"),
    "py-source": ("CANARY-PYKEY-7C3X9Z1L", "custom code (Python) string literal"),
    "odd-condition": ("CANARY-ODDCOND-4M8N2B6V", "Step condition literal a2m cannot translate itself (StartsWith)"),
    "step-condition": ("CANARY-STEPCOND-9Q5W3E7R", "Step condition literal compared with x-api-key"),
    "flow-condition": ("CANARY-FLOWCOND-6T2Y8U4I", "Flow condition literal compared with apikey"),
    "target-userinfo": ("CANARY-TGTUSER-3O9P1A5S", "target URL user information"),
    "target-query": ("CANARY-TGTSEC-8D4F0G6H", "target URL client_secret query value"),
    "recorded-header": ("CANARY-RECHDR-1J7K3L9Z", "golden backend call header X-Client-Token"),
    "recorded-response": ("CANARY-RECRESP-5X2C8V4B", "golden response header X-Refresh-Token"),
    "recorded-query": ("CANARY-RECQRY-0N6M2Q8W", "golden backend call access_token query value"),
    "backend-received": ("CANARY-UPSTREAM-4E1R7T3Y", "header X-Upstream-Auth the backend received"),
    "property": ("CANARY-PROPSEC-9U5I1O7P", "credential in config.properties echoed in a response body"),
    # Quoted object keys of custom code (an allowlist keyed by the credential itself).
    "js-key": ("CANARY-JSOBJKEY-8R3T6Y1U", "custom code (JavaScript) quoted object key"),
    "py-key": ("CANARY-PYOBJKEY-2W7E4R9T", "custom code (Python) quoted dictionary key"),
    "json-key": ("CANARY-JSONKEY-5Y9U3I7O", "JSON payload quoted key"),
    "dw-key": ("CANARY-DWKEY-1P6A0S4D", "DataWeave quoted object key in the app's Mule configuration"),
    # Numeric credentials in the data positions of every policy type and of the target endpoint.
    "jc-property-num": ("739218465017", "JavaCallout Property password numeric literal"),
    "js-property-num": ("604183927561", "Javascript policy Property pin numeric literal"),
    "py-property-num": ("153862094718", "Python Script policy Property pin numeric literal"),
    "am-header-num": ("518302749163", "AssignMessage header X-Partner-Pin numeric literal"),
    "am-query-num": ("927461038254", "AssignMessage query parameter pin numeric literal"),
    "am-variable-num": ("381947205639", "AssignMessage AssignVariable backend.pin numeric literal"),
    "json-num": ("265018394712", "JSON payload pin numeric literal"),
    "ba-password-num": ("846203917528", "BasicAuthentication Password numeric literal"),
    "kvm-num": ("193750284617", "KVM InitialEntries backend_pin numeric value"),
    "sc-query-num": ("472819360528", "ServiceCallout URL pin query numeric value"),
    "sc-header-num": ("658230194735", "ServiceCallout request header X-Geo-Pin numeric literal"),
    "rf-header-num": ("314928570361", "RaiseFault header X-Deny-Pin numeric literal"),
    "target-query-num": ("587106392841", "target URL pin query numeric value"),
    "target-property-num": ("920384756102", "target endpoint Property keystore.password numeric literal"),
    # Numbers a2m used to show: a condition's compared value, numeric literals of custom code, a negative JSON number
    # (its digits; its sign is checked by ``signed_number_shown``).
    "cond-num": ("482917360518", "Step condition numeric value compared with x-pin"),
    "js-num": ("648205173946", "custom code (JavaScript) numeric literal"),
    "py-num": ("375920481637", "custom code (Python) numeric literal"),
    "java-num": ("517394820613", "custom code (Java) numeric literal"),
    "json-neg-num": ("829406175302", "JSON payload negative number offset (its digits)"),
}
BASIC_PASSWORD = "CANARY-TRFBASIC-0Q6W2E8R"
BASIC_TOKEN = base64.b64encode(f"svc-backend:{BASIC_PASSWORD}".encode()).decode()
# Credentials in golden traffic under names a2m always treats as credentials: hidden everywhere.
TRAFFIC_CANARIES: dict[str, tuple[str, str]] = {
    "traffic-apikey": ("CANARY-TRFKEY-2A8S4D0F", "VerifyAPIKey header x-apikey value in golden traffic"),
    "traffic-bearer": ("CANARY-TRFBEARER-6G2H8J4K", "backend Authorization Bearer token in golden traffic"),
    "traffic-client-bearer": ("CANARY-TRFCLIENT-3R9T5Y1U", "client Authorization Bearer token in golden traffic"),
    "traffic-cookie": ("CANARY-TRFCOOKIE-1L7Z3X9C", "backend Cookie value in golden traffic"),
    "traffic-setcookie": ("CANARY-TRFSETCK-5V1B7N3M", "response Set-Cookie session value in golden traffic"),
    "traffic-basic": (BASIC_TOKEN, "backend Authorization Basic credentials (base64) in golden traffic"),
    "traffic-basic-password": (BASIC_PASSWORD, "password inside the backend Basic credentials"),
}
ALL_CANARIES = {**PROVIDER_CANARIES, **TRAFFIC_CANARIES}

# Values that look like credentials by name or shape but are not secrets: they must stay visible.
LOOK_ALIKES_SHOWN = (
    "api-version=2023-05-01", "region=westeurope", "'westeurope'", "'2023-05-01T10:00:00Z'", "'unauthorized'",
    "'Hemingway'", "'electronics'", "'https://sso.example.com/realms/keycloak/token'", "'us-east-1'", "'orders-api'",
    'Basic realm="orders"',
)
LOOK_ALIKE_VALUES = (
    "2023-05-01", "westeurope", "2023-05-01T10:00:00Z", "unauthorized", "Hemingway", "electronics",
    "https://sso.example.com/realms/keycloak/token", "us-east-1", "orders-api",
)

POLICIES = "canary-policies"
TRAFFIC = "canary-traffic"
LOOKS = "canary-looks"
XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
JSON_TYPE = {"Content-Type": "application/json"}


def _canary(label: str) -> str:
    return ALL_CANARIES[label][0]


# A JSON value whose sign stands outside its placeholder (``"«v1»": -«n2»``, also XML-escaped; a text placeholder is
# «vN», a number placeholder «nN»): the canary json-neg-num is planted negative, so this shows that a hidden number's
# sign reached the AI.
SIGNED_PLACEHOLDER = re.compile(r"(?:\"|&quot;)«[vn]\d+»(?:\"|&quot;)\s*:\s*[-+]\s*«[vn]\d+»")


def signed_number_shown(text: str) -> bool:
    """Whether ``text`` (a field of an AI request) shows the sign of a hidden JSON number outside its placeholder."""
    return SIGNED_PLACEHOLDER.search(text) is not None


def masked_forms(value: str) -> list[str]:
    """How a masker shows ``value`` (8 characters or more) hidden: its first characters and its length (a2m's mask)."""
    return [f"{value[:4]}*** ({len(value)} chars)"] if len(value) >= 8 else []


# ---------------------------------------------------------------- the report the worker hands back


class Report:
    def __init__(self) -> None:
        self.findings: list[str] = []
        self.errors: list[str] = []
        self.probes: list[str] = []
        self.notes: list[str] = []

    def finding(self, text: str) -> None:
        if text not in self.findings:
            self.findings.append(text)

    def error(self, text: str) -> None:
        if text not in self.errors:
            self.errors.append(text)

    def to_json(self) -> dict[str, list[str]]:
        return {"findings": self.findings, "errors": self.errors, "probes": self.probes, "notes": self.notes}


def _short_error(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {text[:300]}" if text else type(exc).__name__


# ---------------------------------------------------------------- isolation: import the target, not the project


def _isolate(root: Path) -> None:
    """Make ``import a2m`` load the target's package: drop editable-install finders and path hooks (they map a2m to
    the project's own source) and put the target's directory first on sys.path."""
    sys.meta_path[:] = [f for f in sys.meta_path if "editable" not in str(getattr(f, "__module__", "")).lower()]
    sys.path_hooks[:] = [h for h in sys.path_hooks if "editable" not in str(getattr(h, "__module__", "")).lower()]
    sys.path[:] = [p for p in sys.path if "__editable__" not in p]
    for name in [n for n in sys.modules if n == "a2m" or n.startswith("a2m.")]:
        del sys.modules[name]
    sys.path.insert(0, str(root))
    sys.path_importer_cache.clear()
    importlib.invalidate_caches()


def _foreign_modules(root: Path) -> list[str]:
    """a2m modules loaded from anywhere but the target (they would make the result say nothing about it)."""
    base = root.resolve()
    found = []
    for name, module in sorted(sys.modules.items()):
        if not (name == "a2m" or name.startswith("a2m.")) or module is None:
            continue
        file = getattr(module, "__file__", None)
        if file and base not in Path(file).resolve().parents:
            found.append(f"{name} ({file})")
    return found


def _has_module(root: Path, dotted: str) -> bool:
    rel = Path(*dotted.split("."))
    return (root / rel.with_suffix(".py")).is_file() or (root / rel / "__init__.py").is_file()


def _import(root: Path, dotted: str) -> Any | None:
    """The target's module ``dotted``, or None when the target has no such file."""
    if not _has_module(root, dotted):
        return None
    return importlib.import_module(dotted)


# ---------------------------------------------------------------- text helpers


def _texts_of(value: Any, where: str = "") -> Iterator[tuple[str, str]]:
    """Every string ``value`` holds (a string, a dataclass, a mapping or a sequence), with where it sits."""
    if isinstance(value, str):
        yield where or "value", value
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        for item in dataclasses.fields(value):
            yield from _texts_of(getattr(value, item.name, None), f"{where}.{item.name}" if where else item.name)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _texts_of(item, f"{where}[{key!r}]")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _texts_of(item, f"{where}[{index}]")


def _json_strings(text: str) -> str:
    """Every string (keys and values) of a JSON document, one per line, unescaped; the text itself if it is not JSON."""
    try:
        data = json.loads(text)
    except ValueError:
        return text
    out: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            out.append(node)
        elif isinstance(node, dict):
            for key, item in node.items():
                out.append(str(key))
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return "\n".join(out)


# ---------------------------------------------------------------- probe 1: the full pipeline


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _proxy(parent: Path, name: str, policies: dict[str, str], proxy_endpoint: str, target_endpoint: str,
           resources: dict[str, str] | None = None) -> None:
    root = parent / name / "apiproxy"
    for policy, text in policies.items():
        _write(root / "policies" / f"{policy}.xml", XML_HEAD + text + "\n")
    for rel, text in (resources or {}).items():
        _write(root / "resources" / rel, text)
    listed = "".join(f"<Policy>{policy}</Policy>" for policy in policies)
    target_name = re.search(r'<TargetEndpoint name="([^"]+)"', target_endpoint)
    target = target_name.group(1) if target_name else "default"
    _write(
        root / f"{name}.xml",
        XML_HEAD + f'<APIProxy revision="1" name="{name}"><Policies>{listed}</Policies>'
        "<ProxyEndpoints><ProxyEndpoint>default</ProxyEndpoint></ProxyEndpoints>"
        f"<TargetEndpoints><TargetEndpoint>{target}</TargetEndpoint></TargetEndpoints></APIProxy>\n",
    )
    _write(root / "proxies" / "default.xml", XML_HEAD + proxy_endpoint + "\n")
    _write(root / "targets" / f"{target}.xml", XML_HEAD + target_endpoint + "\n")


def _target_endpoint(name: str, url: str, connection: str = "") -> str:
    return (
        f'<TargetEndpoint name="{name}"><PreFlow name="PreFlow"><Request/><Response/></PreFlow><Flows/>'
        '<PostFlow name="PostFlow"><Request/><Response/></PostFlow>'
        f"<HTTPTargetConnection>{connection}<URL>{url}</URL></HTTPTargetConnection></TargetEndpoint>"
    )


def _proxy_endpoint(base_path: str, pre: str = "", flows: str = "<Flows/>", post_request: str = "",
                    post_response: str = "") -> str:
    return (
        f'<ProxyEndpoint name="default"><PreFlow name="PreFlow"><Request>{pre}</Request><Response/></PreFlow>'
        f'{flows}<PostFlow name="PostFlow"><Request>{post_request}</Request><Response>{post_response}</Response>'
        f"</PostFlow><HTTPProxyConnection><BasePath>{base_path}</BasePath><VirtualHost>default</VirtualHost>"
        '</HTTPProxyConnection><RouteRule name="default"><TargetEndpoint>default</TargetEndpoint></RouteRule>'
        "</ProxyEndpoint>"
    )


def write_policies_proxy(parent: Path, *, basic_auth_step: bool) -> None:
    """A canary in every literal credential position of every policy type (a numeric one in each data position:
    Properties, header, query and variable values, payload, KVM entry, target endpoint), the custom code (JavaScript
    and Python, a credential as a quoted object key too, a numeric literal in each; a JavaCallout with its Java source,
    a numeric literal too), four conditions (two a2m cannot translate itself, so they go to the AI: one compares a
    numeric value) and the target URL, next to a harmless
    keyword. Without ``basic_auth_step`` the BasicAuthentication policy is in no step
    (its unset variables would stop a battery run)."""
    c = _canary
    soap = (
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">'
        "<soapenv:Header><wsse:Security><wsse:UsernameToken><wsse:Username>svc-orders</wsse:Username>"
        f'<wsse:Password>{c("soap-password")}</wsse:Password></wsse:UsernameToken></wsse:Security></soapenv:Header>'
        "<soapenv:Body><GetOrder/></soapenv:Body></soapenv:Envelope>"
    )
    policies = {
        "AM-Creds": (
            '<AssignMessage name="AM-Creds"><Set><Headers>'
            f'<Header name="X-Partner-Api-Key">{c("am-header")}</Header>'
            f'<Header name="X-Partner-Pin">{c("am-header-num")}</Header></Headers><QueryParams>'
            f'<QueryParam name="code">{c("am-code")}</QueryParam><QueryParam name="sig">{c("am-sig")}</QueryParam>'
            f'<QueryParam name="appid">{c("am-appid")}</QueryParam>'
            f'<QueryParam name="pin">{c("am-query-num")}</QueryParam></QueryParams>'
            f'<Payload contentType="text/xml"><![CDATA[{soap}]]></Payload></Set>'
            f'<AssignVariable><Name>backend.password</Name><Value>{c("am-variable")}</Value></AssignVariable>'
            f'<AssignVariable><Name>backend.pin</Name><Value>{c("am-variable-num")}</Value></AssignVariable>'
            '<AssignTo createNew="false" transport="http" type="request"/></AssignMessage>'
        ),
        "AM-Json": (
            '<AssignMessage name="AM-Json"><Set><Payload contentType="application/json">'
            f'{{"client_secret": "{c("json-secret")}", "pin": {c("json-num")}, "{c("json-key")}": "partner", '
            f'"offset": -{c("json-neg-num")}, "keyword": "electronics"}}'
            "</Payload></Set>"
            '<AssignTo createNew="false" transport="http" type="request"/></AssignMessage>'
        ),
        "BA-Encode": (
            '<BasicAuthentication name="BA-Encode"><Operation>Encode</Operation>'
            f'<IgnoreUnresolvedVariables>false</IgnoreUnresolvedVariables><User>{c("ba-user")}</User>'
            f'<Password>{c("ba-password")}</Password>'
            '<AssignTo createNew="false">request.header.Authorization</AssignTo></BasicAuthentication>'
        ),
        "BA-Pin": (
            '<BasicAuthentication name="BA-Pin"><Operation>Encode</Operation>'
            '<IgnoreUnresolvedVariables>false</IgnoreUnresolvedVariables><User>svc-pin</User>'
            f'<Password>{c("ba-password-num")}</Password>'
            '<AssignTo createNew="false">request.header.X-Pin-Auth</AssignTo></BasicAuthentication>'
        ),
        "KVM-Init": (
            '<KeyValueMapOperations name="KVM-Init" mapIdentifier="backend"><InitialEntries><Entry><Key>'
            f'<Parameter>backend_password</Parameter></Key><Value>{c("kvm-value")}</Value></Entry>'
            f'<Entry><Key><Parameter>backend_pin</Parameter></Key><Value>{c("kvm-num")}</Value></Entry>'
            "</InitialEntries>"
            '<Get assignTo="private.backend_password"><Key><Parameter>backend_password</Parameter></Key></Get>'
            "<Scope>environment</Scope></KeyValueMapOperations>"
        ),
        "SC-Geo": (
            '<ServiceCallout name="SC-Geo"><Request variable="geoRequest"><Set><Headers>'
            f'<Header name="X-Geo-Pin">{c("sc-header-num")}</Header></Headers></Set></Request>'
            "<Response>geoResponse</Response>"
            f'<HTTPTargetConnection><URL>https://geo-user:{c("sc-userinfo")}@maps.example.com/geocode/json'
            f'?address=x&amp;key={c("sc-query")}&amp;pin={c("sc-query-num")}</URL></HTTPTargetConnection>'
            "</ServiceCallout>"
        ),
        "RF-Deny": (
            '<RaiseFault name="RF-Deny"><FaultResponse><Set><Headers>'
            f'<Header name="X-Deny-Pin">{c("rf-header-num")}</Header></Headers><StatusCode>403</StatusCode></Set>'
            "</FaultResponse></RaiseFault>"
        ),
        "JS-Sign": (
            '<Javascript name="JS-Sign" timeLimit="200"><Properties>'
            f'<Property name="pin">{c("js-property-num")}</Property></Properties>'
            "<ResourceURL>jsc://sign.js</ResourceURL></Javascript>"
        ),
        "PY-Hash": (
            '<Script name="PY-Hash"><Properties>'
            f'<Property name="pin">{c("py-property-num")}</Property></Properties>'
            "<ResourceURL>py://hash.py</ResourceURL></Script>"
        ),
        "JC-Auth": (
            '<JavaCallout name="JC-Auth"><Properties>'
            f'<Property name="password">{c("jc-property-num")}</Property></Properties>'
            "<ClassName>com.example.Auth</ClassName><ResourceURL>java://auth.jar</ResourceURL></JavaCallout>"
        ),
        "RF-Odd": (
            '<RaiseFault name="RF-Odd"><FaultResponse><Set><StatusCode>401</StatusCode></Set></FaultResponse>'
            "</RaiseFault>"
        ),
        "RF-Pin": (
            '<RaiseFault name="RF-Pin"><FaultResponse><Set><StatusCode>401</StatusCode></Set></FaultResponse>'
            "</RaiseFault>"
        ),
    }
    pre = (
        "<Step><Name>AM-Creds</Name></Step>"
        + ("<Step><Name>BA-Encode</Name></Step><Step><Name>BA-Pin</Name></Step>" if basic_auth_step else "")
        + f'<Step><Name>RF-Deny</Name><Condition>request.header.x-api-key != "{c("step-condition")}"</Condition>'
        "</Step>"
        + f'<Step><Name>RF-Odd</Name><Condition>request.header.x-client-id =| "{c("odd-condition")}"</Condition>'
        "</Step>"
        + f'<Step><Name>RF-Pin</Name><Condition>request.header.x-pin = {c("cond-num")}</Condition></Step>'
    )
    flows = (
        f'<Flows><Flow name="Keyed"><Condition>request.queryparam.apikey = "{c("flow-condition")}"</Condition>'
        "<Request><Step><Name>AM-Json</Name></Step></Request><Response/></Flow></Flows>"
    )
    _proxy(
        parent,
        POLICIES,
        policies,
        _proxy_endpoint(
            "/canary", pre, flows, "<Step><Name>KVM-Init</Name></Step><Step><Name>SC-Geo</Name></Step>",
            "<Step><Name>JS-Sign</Name></Step><Step><Name>PY-Hash</Name></Step><Step><Name>JC-Auth</Name></Step>",
        ),
        _target_endpoint(
            "default",
            f"https://kc-admin:{c('target-userinfo')}@sso.example.com/realms/acme?client_secret={c('target-query')}"
            f"&amp;pin={c('target-query-num')}",
            f'<Properties><Property name="keystore.password">{c("target-property-num")}</Property></Properties>',
        ),
        {
            "jsc/sign.js": (
                f'var apiKey = "{c("js-source")}";\ncontext.setVariable("request.header.X-Sig", apiKey);\n'
                f"var clients = {{'{c('js-key')}': 'partner'}};\n"
                'context.setVariable("partner.known", String(Object.keys(clients).length));\n'
                f'var pin = {c("js-num")};\ncontext.setVariable("partner.pin.ok", String(pin === {c("js-num")}));\n'
            ),
            "py/hash.py": (
                f'secret = "{c("py-source")}"\nflow.setVariable("request.header.X-Hash", secret)\n'
                f'clients = {{"{c("py-key")}": "partner"}}\nflow.setVariable("partner.known", str(len(clients)))\n'
                f'pin = {c("py-num")}\nflow.setVariable("partner.pin.ok", str(pin == {c("py-num")}))\n'
            ),
            "java/com/example/Auth.java": (
                "package com.example;\n\n"
                "public class Auth implements Execution {\n"
                "  public ExecutionResult execute(MessageContext context, ExecutionContext execution) {\n"
                '    context.setVariable("request.header.X-Auth-Checked", "yes");\n'
                f'    long pin = {c("java-num")}L;\n'
                '    context.setVariable("auth.pin.ok", String.valueOf(pin > 0));\n'
                "    return ExecutionResult.SUCCESS;\n  }\n}\n"
            ),
        },
    )


def write_traffic_proxy(parent: Path) -> None:
    """A VerifyAPIKey proxy; its golden traffic holds the credentials (see write_golden)."""
    _proxy(
        parent,
        TRAFFIC,
        {"VK-Key": '<VerifyAPIKey name="VK-Key"><APIKey ref="request.header.x-apikey"/></VerifyAPIKey>'},
        _proxy_endpoint("/traffic", "<Step><Name>VK-Key</Name></Step>"),
        _target_endpoint("default", "https://backend.example.test/traffic"),
    )


def write_looks_proxy(parent: Path) -> None:
    """Look-alikes of credentials: a target URL with api-version and region query values, a config KVM, variables
    named oauth.tokenEndpoint and session.region, a VerifyJWT audience and code setting auth.status."""
    _proxy(
        parent,
        LOOKS,
        {
            "AM-Config": (
                '<AssignMessage name="AM-Config"><AssignVariable><Name>oauth.tokenEndpoint</Name>'
                "<Value>https://sso.example.com/realms/keycloak/token</Value></AssignVariable>"
                "<AssignVariable><Name>session.region</Name><Value>us-east-1</Value></AssignVariable>"
                '<AssignTo createNew="false" transport="http" type="request"/></AssignMessage>'
            ),
            "KVM-Config": (
                '<KeyValueMapOperations name="KVM-Config" mapIdentifier="config"><InitialEntries><Entry><Key>'
                "<Parameter>region</Parameter></Key><Value>us-east-1</Value></Entry><Entry><Key><Parameter>"
                "tokenEndpoint</Parameter></Key><Value>https://sso.example.com/realms/keycloak/token</Value></Entry>"
                "</InitialEntries><Scope>environment</Scope></KeyValueMapOperations>"
            ),
            "JWT-Verify": (
                '<VerifyJWT name="JWT-Verify"><Algorithm>RS256</Algorithm><Source>request.header.jwt</Source>'
                "<Issuer>https://sso.example.com/realms/keycloak</Issuer><Audience>orders-api</Audience></VerifyJWT>"
            ),
            "JS-Status": (
                '<Javascript name="JS-Status" timeLimit="200"><ResourceURL>jsc://status.js</ResourceURL></Javascript>'
            ),
        },
        _proxy_endpoint("/looks", "<Step><Name>AM-Config</Name></Step>", post_response="<Step><Name>JS-Status</Name></Step>"),
        _target_endpoint("default", "https://backend.example.test/orders?api-version=2023-05-01&amp;region=westeurope"),
        {"jsc/status.js": 'context.setVariable("auth.status", "authorized");\n'},
    )


def _exchange(name: str, request: dict[str, Any], response: dict[str, Any], backend: dict[str, Any]) -> str:
    backend = {"body": "", "response": {"status": 200, "headers": dict(JSON_TYPE), "body": '{"ok":true}'}, **backend}
    return json.dumps({
        "name": name,
        "calls": [{"after_ms": 0, "request": {"body": "", **request}, "response": response}],
        "backend_calls": [backend],
    })


def write_golden(golden: Path) -> None:
    """One recorded exchange per proxy that the fake app does not reproduce, so each proxy fails its golden test."""
    c = _canary
    _write(golden / POLICIES / "canary-call.json", _exchange(
        "canary-call",
        {"method": "GET", "path": "/canary", "headers": {}},
        # A JSON number and a JSON key the fake app does not answer: the failing diff's body field lines carry them.
        {"status": 200, "headers": {**JSON_TYPE, "X-Refresh-Token": c("recorded-response")},
         "body": json.dumps({"ok": True, "pin": int(c("json-num")), c("json-key"): "partner"}, separators=(",", ":"))},
        {"method": "GET", "path": f"/realms/acme?access_token={c('recorded-query')}",
         "headers": {"X-Client-Token": c("recorded-header")}},
    ))
    _write(golden / TRAFFIC / "traffic-call.json", _exchange(
        "traffic-call",
        {"method": "GET", "path": "/traffic", "headers": {
            "x-apikey": c("traffic-apikey"), "Authorization": f"Bearer {c('traffic-client-bearer')}"}},
        {"status": 200, "headers": {**JSON_TYPE, "Set-Cookie": f"sid={c('traffic-setcookie')}; Path=/; HttpOnly"},
         "body": '{"ok":true}'},
        {"method": "GET", "path": "/traffic", "headers": {
            "x-apikey": c("traffic-apikey"),
            "Authorization": f"Bearer {c('traffic-bearer')}",
            "Proxy-Authorization": f"Basic {BASIC_TOKEN}",
            "Cookie": f"backend_session={c('traffic-cookie')}; theme=dark",
        }},
    ))
    looks_body = {
        "status": "unauthorized", "author": "Hemingway", "keyword": "electronics", "region": "us-east-1",
        "createdAt": "2023-05-01T10:00:00Z", "tokenEndpoint": "https://sso.example.com/realms/keycloak/token",
        "audience": "orders-api",
    }
    _write(golden / LOOKS / "looks-call.json", _exchange(
        "looks-call",
        {"method": "GET", "path": "/looks/42", "headers": {}},
        {"status": 200, "headers": {**JSON_TYPE, "WWW-Authenticate": 'Basic realm="orders"'},
         "body": json.dumps(looks_body)},
        {"method": "GET", "path": "/orders/42?api-version=2023-05-01&region=westeurope", "headers": {}},
    ))


# Proof the scenario really ran each leak path: names the run's outputs must show (lower case). A run that never
# shows them proves nothing about the canaries, so it is a failure, never a pass.
REACH_VERIFICATION = {
    ("golden run", TRAFFIC): ("set-cookie", "authorization", "cookie", "x-apikey"),
    ("golden run", POLICIES): ("x-client-token", "x-refresh-token", "x-upstream-auth"),
}
# Each entry: what the fix request must show, a name or a tuple of forms of which one will do. A header name the proxy
# does not declare (X-Client-Token, X-Refresh-Token) and a JSON key may be placeholders in a fix request's diff, so
# their lines are found by name or by shape: a backend call header line, a response header line, and body field lines
# with a key placeholder and a number placeholder (or the key and the number themselves, which is a leak of its own).
REACH_PROMPT: dict[tuple[str, str], tuple[str | tuple[str, ...], ...]] = {
    ("golden run", TRAFFIC): ("set-cookie", "authorization"),
    ("golden run", POLICIES): (
        "x-partner-api-key", "partnerallow", ("x-client-token", "backend call 1 header «v"),
        ("x-refresh-token", "\n    header «v"), ("body field «v", "body field pin"),
        ("expected «n", "expected " + _canary("json-num")),
    ),
    ("battery run", POLICIES): ("x-partner-api-key", "partnerallow"),
}


class CanaryRunner:
    """A fake Runner: each proxy's app calls the mock backend differently from the recording and answers a fixed
    response, so every proxy fails its tests (``status`` 500 for a battery run)."""

    def __init__(self, response_type: Any, status: int = 200) -> None:
        self.response_type = response_type
        self.status = status
        self.started: list[str] = []

    def start(self, app: Any, *, backend_url: str) -> Any:
        import http.client
        from urllib.parse import urlsplit

        name = str(getattr(app, "name", ""))
        self.started.append(name)
        target = urlsplit(backend_url)
        response_type = self.response_type
        status = self.status
        if name == POLICIES:
            path, headers = "/realms/acme", {"X-Upstream-Auth": _canary("backend-received")}
            body: dict[str, Any] = {"ok": True, "echo": _canary("property")}
        elif name == LOOKS:
            path, headers, body = "/orders/42?api-version=2023-05-01", {}, {"status": "ok"}
        else:
            path, headers, body = "/traffic", {}, {"ok": True}

        class _Handle:
            running = True
            base_url = "http://credential-canary.invalid/app"

            def send(self, request: Any) -> Any:
                conn = http.client.HTTPConnection(target.hostname or "127.0.0.1", target.port or 80, timeout=10)
                try:
                    conn.request("GET", path, headers=headers)
                    conn.getresponse().read()
                finally:
                    conn.close()
                return response_type(status, dict(JSON_TYPE), json.dumps(body).encode())

            def stop(self) -> None:
                self.running = False

        return _Handle()


def _request_fields(request: Any) -> dict[str, str]:
    if dataclasses.is_dataclass(request) and not isinstance(request, type):
        names = [item.name for item in dataclasses.fields(request)]
    else:
        names = sorted(vars(request)) if hasattr(request, "__dict__") else []
    return {name: value for name in names if isinstance(value := getattr(request, name, None), str)}


def _kind_of(request: Any) -> str:
    kind = getattr(request, "kind", "")
    return str(getattr(kind, "value", kind))


@contextlib.contextmanager
def _patched(owner: Any, name: str, value: Any) -> Iterator[None]:
    original = getattr(owner, name)
    setattr(owner, name, value)
    try:
        yield
    finally:
        setattr(owner, name, original)


class FullPipelineProbe:
    """``a2m migrate`` through the target's CLI and engine stages, golden and battery runs (see the module docstring)."""

    name = "full pipeline (a2m migrate, golden and battery runs, fix loop)"

    def __init__(self, root: Path, work: Path, report: Report) -> None:
        self.root = root
        self.work = work
        self.report = report

    def applies(self) -> bool:
        fix_loop = _import(self.root, "a2m.verify.fix_loop")
        return fix_loop is not None and callable(getattr(fix_loop, "run_with_fixes", None))

    def run(self) -> None:
        cli = _import(self.root, "a2m.cli")
        engine = _import(self.root, "a2m.engine")
        verify = _import(self.root, "a2m.verify")
        fake = _import(self.root, "a2m.ai.fake")
        missing = [
            what
            for what, ok in (
                ("a2m.cli.main(argv, stages=...)", cli is not None and "stages" in _parameters(getattr(cli, "main", None))),
                ("a2m.engine.parse and generate", engine is not None and all(
                    callable(getattr(engine, n, None)) for n in ("parse", "generate"))),
                ("a2m.verify.make_verify_stage and HttpResponse", verify is not None and all(
                    hasattr(verify, n) for n in ("make_verify_stage", "HttpResponse"))),
                ("a2m.ai.fake.FakeProvider", fake is not None and hasattr(fake, "FakeProvider")),
            )
            if not ok
        ]
        if missing:
            self.report.error(
                "the target has a2m.verify.fix_loop.run_with_fixes but not the interfaces the check drives it "
                f"through ({', '.join(missing)}), so its fix requests could not be checked"
            )
            return
        assert cli is not None and engine is not None and verify is not None and fake is not None
        # Every request the AI provider gets, whatever its kind: (run, kind, name, text fields).
        captured: list[tuple[str, str, str, dict[str, str]]] = []
        run_label = {"value": ""}
        provider_type = fake.FakeProvider
        original_complete = provider_type.complete

        def recording(provider: Any, request: Any) -> str:
            kind = _kind_of(request)
            captured.append((run_label["value"], kind, str(getattr(request, "name", "?")), _request_fields(request)))
            if kind == "fix":
                return json.dumps({"status": "cannot_fix", "reason": FIX_REASON})
            return original_complete(provider, request)

        with _patched(provider_type, "complete", recording):
            golden_run = self._migrate(cli, engine, verify, run_label, "golden run", captured, battery=False)
            battery_run = self._migrate(cli, engine, verify, run_label, "battery run", captured, battery=True)
        for run_name, outputs in (("golden run", golden_run), ("battery run", battery_run)):
            if outputs is not None:
                self._judge_outputs(run_name, *outputs)
        self._judge_requests(captured)

    def _migrate(self, cli: Any, engine: Any, verify: Any, run_label: dict[str, str], label: str,
                 captured: list[tuple[str, str, str, dict[str, str]]], *, battery: bool,
                 ) -> tuple[str, dict[str, str], list[tuple[str, str]]] | None:
        base = self.work / ("battery" if battery else "golden")
        exports = base / "exports"
        out = base / "out"
        write_policies_proxy(exports, basic_auth_step=not battery)
        expected = [POLICIES]
        argv = ["migrate", str(exports), "--out", str(out), "--llm", "fake", "--max-fix-attempts", "1"]
        if battery:
            argv.append("--mock-backends")
            runner = CanaryRunner(verify.HttpResponse, status=500)
        else:
            write_traffic_proxy(exports)
            write_looks_proxy(exports)
            expected += [TRAFFIC, LOOKS]
            write_golden(base / "golden")
            argv += ["--golden", str(base / "golden")]
            runner = CanaryRunner(verify.HttpResponse)

        def add_property(context: Any) -> None:
            """Between generate and verify: a credential in the app's properties, which its fake app echoes, and a
            credential as a quoted DataWeave object key in its main flow (a partner allowlist)."""
            if str(getattr(context, "name", "")) != POLICIES:
                return
            app_dir = Path(context.out_dir) / "mule-app" / "src" / "main"
            props = app_dir / "resources" / "config.properties"
            if props.is_file() and not props.is_symlink():
                with props.open("a", encoding="utf-8") as handle:
                    handle.write(f"\npartner.api.secret={_canary('property')}\n")
            flow = app_dir / "mule" / "proxy.xml"
            if flow.is_file() and not flow.is_symlink():
                text = flow.read_text(encoding="utf-8")
                start = re.search(r"<flow\b[^>]*>", text)
                if start is not None:
                    allow = (
                        '<set-variable variableName="partnerAllow" doc:name="partner-allowlist" '
                        f"value=\"#[{{'{_canary('dw-key')}': 'partner'}}]\"/>"
                    )
                    flow.write_text(text[: start.end()] + allow + text[start.end() :], encoding="utf-8")

        add_property.__name__ = "credential-canary-property"
        stages = [engine.parse, engine.generate, add_property, verify.make_verify_stage(runner=runner)]
        run_label["value"] = label
        before = len(captured)
        sink = io.StringIO()
        try:
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink), _root_capture(
                lower_level=False
            ) as records:
                cli.main(argv, stages=stages)
        except SystemExit as exc:
            self.report.error(f"{label}: a2m migrate exited early ({exc.code!r}), so its outputs could not be checked")
            return None
        finally:
            close = getattr(stages[-1], "close", None)
            if callable(close):
                close()
        run_log_path = out / "run.log"
        if not run_log_path.is_file():
            self.report.error(f"{label}: a2m migrate wrote no run.log, so the run could not be checked")
            return None
        verifications = {
            path.parent.name: path.read_text(encoding="utf-8", errors="replace")
            for path in sorted(out.rglob("verification.json"))
            if "mule-app" not in path.parts and not any(part.startswith(".") for part in path.relative_to(out).parts)
        }
        asked = {name for run, kind, name, _ in captured[before:] if run == label and kind == "fix"}
        for proxy in expected:
            if proxy not in verifications:
                self.report.error(
                    f"{label}: proxy {proxy} wrote no verification.json, so the scenario did not run "
                    "(see its run.log); a scenario that cannot run is never a pass"
                )
            elif proxy not in asked:
                self.report.error(
                    f"{label}: proxy {proxy} never reached the AI fix loop (no fix request was sent), so the "
                    "canaries could not be checked; a scenario that cannot run is never a pass"
                )
        return run_log_path.read_text(encoding="utf-8", errors="replace"), verifications, records.lines

    def _judge_outputs(self, label: str, run_log: str, verifications: dict[str, str],
                       records: list[tuple[str, str]]) -> None:
        for (run, proxy), names in REACH_VERIFICATION.items():
            text = _json_strings(verifications.get(proxy, "")).lower() if run == label else ""
            missing = [name for name in names if name not in text]
            if run == label and proxy in verifications and missing:
                self.report.error(
                    f"{label}: verification.json of {proxy} does not show the failing test lines for "
                    f"{', '.join(missing)}, so those credentials never went through the run; a scenario that cannot "
                    "run is never a pass"
                )
        places = {"run.log": run_log, **{f"verification.json of {p}": _json_strings(t) for p, t in verifications.items()}}
        for logger_name, line in records:
            where = f"a log record of logger {logger_name or 'root'!r} (as a handler on the root logger gets it)"
            places[where] = places.get(where, "") + line + "\n"
        for where, text in places.items():
            for key, (value, what) in TRAFFIC_CANARIES.items():
                if value in text:
                    self.report.finding(f"leak: canary {key} ({what}) is unmasked in {where} ({label})")
        if label != "golden run" or LOOKS not in verifications:
            return  # a looks proxy that never ran is reported as such, not as masked look-alikes
        looks = {"run.log": run_log, f"verification.json of {LOOKS}": _json_strings(verifications[LOOKS])}
        for where, text in looks.items():
            for shown in LOOK_ALIKES_SHOWN:
                if shown not in text:
                    self.report.finding(
                        f"over-masked look-alike: the harmless value {shown} is not shown in {where} ({label}); "
                        "it was masked or dropped although it is no credential"
                    )

    def _judge_requests(self, captured: list[tuple[str, str, str, dict[str, str]]]) -> None:
        for (run, proxy), names in REACH_PROMPT.items():
            prompts = [f.get("prompt", "").lower() for r, k, p, f in captured if r == run and k == "fix" and p == proxy]
            forms = [(name,) if isinstance(name, str) else name for name in names]
            missing = [
                " or ".join(form) for form in forms if prompts and not any(f in text for f in form for text in prompts)
            ]
            if missing:
                self.report.error(
                    f"{run}: the fix request of {proxy} does not name {', '.join(missing)}, so the failing test "
                    "lines that carry those credentials never reached it; a scenario that cannot run is never a pass"
                )
        for run, kind, name, fields in captured:
            for field, text in fields.items():
                item = f"proxy {name}" if kind == "fix" else f"item {name}"
                where = f"the AI provider ({kind or 'unknown'} request field '{field}', {item}, {run})"
                for key, (value, what) in ALL_CANARIES.items():
                    if value in text:
                        self.report.finding(f"leak: canary {key} ({what}) reached {where} unmasked")
                if signed_number_shown(text):
                    self.report.finding(
                        f"leak: the sign of canary json-neg-num (a negative JSON number) reached {where} outside "
                        "its placeholder"
                    )
                for value in LOOK_ALIKE_VALUES:
                    if any(form in text for form in masked_forms(value)):
                        self.report.finding(
                            f"over-masked look-alike: the harmless value '{value}' was sent masked to {where}"
                        )
        kinds = sorted({kind for _, kind, _, _ in captured})
        counts = ", ".join(f"{sum(1 for c in captured if c[1] == kind)} {kind or 'unknown'}" for kind in kinds)
        self.report.notes.append(f"{len(captured)} AI requests captured ({counts or 'none'})")


# ---------------------------------------------------------------- probe 1b: a second fix request after a restore

# Literal credentials holding the characters a string literal escapes ($, quotes, backslashes): a2m writes them back
# spelled for DataWeave, so a reason that quotes the restored fix (or a build or deploy error that quotes the restored
# code) spells them escaped. Each run of five or more letters and digits is distinctive, so a piece of one in any
# spelling is found.
ESCAPE_CANARIES: dict[str, tuple[str, str]] = {
    "esc-dollar": ("CNRYDL$Q7wK2p$$Xv9mBz", "AssignMessage header literal with $ (a DataWeave escape)"),
    "esc-quote": ("CNRYQT'Hz4nJq\"Lm8sTx", "AssignMessage header literal with both quotes"),
    "esc-backslash": ("CNRYBS\\Zr8sLk\\nPq3Wv", "AssignMessage header literal with backslashes"),
    "esc-mixed": ("CNRYMX$'\"\\Gt5vRe", "AssignMessage header literal with $, quotes and a backslash"),
}
# Credential-shaped literals a masker masks only in part: the scheme or name kept, the first characters of the token
# kept, and a token masked only up to the first character a token cannot hold (':' and '$' here), so a masked form
# keeps the rest. They are set as flow variables by AssignVariable, which no battery case tests, so no failing diff
# quotes them (a masked form in a diff would become a value of its own, and the sweep would then hide it anyway).
# Each: the value, what it is, and the first characters a masked form shows.
CREDENTIAL_CANARIES: dict[str, tuple[str, str, str]] = {
    "cred-bearer": ("Bearer Brq7CNRYkQ2mX9pL4:CNRYTAIL8vW3zQ6", "Bearer token with ':' and a tail", "Brq7"),
    "cred-basic": ("Basic Q05SWUJBNXNtcDRr$CNRYTL2nV7xK5", "Basic credentials with '$' and a tail", "Q05S"),
    "cred-token": ("Bearer Tkn9CNRYwH4qZ2mR6vB9x", "Bearer token of token characters only", "Tkn9"),
    "cred-jwt": ("eyJDTlJZand0ZQ.eyJDTlJZcGF5bG9h.CNRYsig7Qk2x$CNRYJWTTAIL4", "JWT API token with '$' and a tail",
                 "eyJD"),
    "cred-apikey": ("Authorization: Ak9xCNRYk3yQ2mR7$CNRYKEYTAIL5", "API key after a credential name, with '$'",
                    "Ak9x"),
}
CREDENTIAL_VARIABLES = {"cred-bearer": "credBearer", "cred-basic": "credBasic", "cred-token": "credToken",
                        "cred-jwt": "credJwt", "cred-apikey": "credApiKey"}
CREDENTIAL_SCHEME_WORDS = frozenset({"Bearer", "Basic", "Authorization"})
ESCAPES = "canary-escapes"
# The base path is the proxy's name in part, as on most proxies (Apigee's default base path is the proxy's name), so
# every build and deploy error, which starts with that name, holds the base path's letters.
ESCAPE_BASE_PATH = "/escapes"
ESCAPE_HEADERS = {"esc-dollar": "X-Esc-Dollar", "esc-quote": "X-Esc-Quote", "esc-backslash": "X-Esc-Backslash",
                  "esc-mixed": "X-Esc-Mixed"}
# The scenarios whose build or deploy error quotes the credential canaries alone end with this.
CREDENTIALS_SUFFIX = "-credentials"
# What a2m's reason for a fix refused for its guard says (the guard scenario must be refused for its guard).
GUARD_REFUSAL = "guard"
ESCAPE_PIECE = 5
# What fix request 2 must quote of the error the fixed app's start failed with (EscapeRunner), by scenario: a2m's
# reason may hide values, never the cause itself.
START_CAUSES = {"build": "mvn package failed", "deploy": "while evaluating"}
ESCAPE_PROBE_VARIABLE = "canaryProbe"


def escape_pieces(value: str) -> set[str]:
    """Every run of ESCAPE_PIECE or more letters and digits in ``value``: a2m spells the rest escaped."""
    return {run for run in re.findall(r"[A-Za-z0-9]+", value) if len(run) >= ESCAPE_PIECE}


def credential_marks(key: str) -> set[str]:
    """What of credential canary ``key`` may never reach a request: each run of ESCAPE_PIECE or more letters and
    digits (the scheme or name word aside, which a2m's own words may use) and the masked form of its first
    characters (``Brq7***``)."""
    value, _, prefix = CREDENTIAL_CANARIES[key]
    return (escape_pieces(value) - CREDENTIAL_SCHEME_WORDS) | {prefix + "***"}


def _xml_text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace(
        "'", "&apos;"
    )


def write_escapes_proxy(parent: Path) -> None:
    """One AssignMessage setting a header to each escape canary, and one setting a flow variable to each credential
    canary, on the request PreFlow."""
    headers = "".join(
        f'<Header name="{ESCAPE_HEADERS[key]}">{_xml_text(value)}</Header>' for key, (value, _) in ESCAPE_CANARIES.items()
    )
    variables = "".join(
        f"<AssignVariable><Name>{CREDENTIAL_VARIABLES[key]}</Name><Value>{_xml_text(value)}</Value></AssignVariable>"
        for key, (value, _, _) in CREDENTIAL_CANARIES.items()
    )
    policies = {
        "AM-Escapes": f'<AssignMessage name="AM-Escapes"><Set><Headers>{headers}</Headers></Set></AssignMessage>',
        "AM-Creds": f'<AssignMessage name="AM-Creds">{variables}</AssignMessage>',
    }
    # The base path's letters stand inside the proxy's name, which every build and deploy error quotes first: a2m
    # must still tell the AI why the start failed (the scenario checks that the cause reached request 2).
    _proxy(
        parent, ESCAPES, policies,
        _proxy_endpoint(ESCAPE_BASE_PATH, pre="<Step><Name>AM-Escapes</Name></Step><Step><Name>AM-Creds</Name></Step>"),
        _target_endpoint("default", f"http://backend.example{ESCAPE_BASE_PATH}"),
    )


def _escape_fix(prompt: str, scenario: str) -> str | None:
    """The first fix the fake AI answers for ``scenario``: a guard a2m refuses (``guard``: every escape and credential
    canary) or a variable a2m accepts (``build``, ``deploy``: every escape canary; ``build-credentials``,
    ``deploy-credentials``: every credential canary), comparing with or holding them by placeholder; None when the
    prompt does not show them."""
    escapes, credentials = [], []
    for header in ESCAPE_HEADERS.values():
        found = re.search(rf'<Header name="{header}">(«v\d+»)</Header>', prompt)
        if found is None:
            return None
        escapes.append(found.group(1))
    for variable in CREDENTIAL_VARIABLES.values():
        found = re.search(rf"<Name>{variable}</Name>\s*<Value>(«v\d+»)</Value>", prompt)
        if found is None:
            return None
        credentials.append(found.group(1))
    # The credential scenarios quote the credential canaries alone: a reason that also quoted an escape canary could
    # be withheld whole for it, and then would test nothing about the credentials.
    if scenario == "guard":
        tokens = escapes + credentials
    elif scenario.endswith(CREDENTIALS_SUFFIX):
        tokens = credentials
    else:
        tokens = escapes
    blocks = re.findall(r"### (src/main/mule/[^\n]+\.xml)\n\n```xml\n(.*?)\n```", prompt, re.DOTALL)
    for rel, block in blocks:
        listener = block.find("</http:listener>")
        opening = re.search(r"<flow\b[^>]*>", block)
        if opening is None:
            continue
        at = listener + len("</http:listener>") if listener >= 0 else opening.end()
        if scenario == "guard":
            test = " or ".join(f"upper(attributes.headers.'x-tag') == '{token}'" for token in tokens)
            added = (f'<choice><when expression="#[{test}]"><set-variable variableName="ok" value="#[true]"/>'
                     "</when></choice>")
        else:
            listed = ", ".join(f"'{token}'" for token in tokens)
            added = f'<set-variable variableName="{ESCAPE_PROBE_VARIABLE}" value="#[[{listed}]]"/>'
        return json.dumps({"status": "fixed", "files": {rel: block[:at] + added + block[at:]}, "notes": "n",
                           "confidence": "high"})
    return None


class EscapeRunner(CanaryRunner):
    """A CanaryRunner (every test fails) until the fix is written; then, for ``build`` or ``deploy``, starting the
    app fails with the error Maven or Mule writes, quoting the restored code (the XML line; Mule's "Caused by" line
    with the DataWeave expression as Python's repr spells it)."""

    def __init__(self, response_type: Any, scenario: str, mule: Any) -> None:
        super().__init__(response_type, status=500)
        self.scenario = scenario
        self.mule = mule
        self.failed = 0

    def start(self, app: Any, *, backend_url: str) -> Any:
        import xml.etree.ElementTree as ET

        name = str(getattr(app, "name", ""))
        folder = Path(str(getattr(app, "app_dir", ""))) / "src" / "main" / "mule"
        if self.scenario != "guard" and folder.is_dir():
            for path in sorted(folder.glob("*.xml")):
                text = path.read_text(encoding="utf-8", errors="replace")
                if ESCAPE_PROBE_VARIABLE not in text:
                    continue
                line = next(row for row in text.splitlines() if ESCAPE_PROBE_VARIABLE in row).strip()
                expression = next(
                    (node.get("value", "") for node in ET.fromstring(text).iter()
                     if node.get("variableName") == ESCAPE_PROBE_VARIABLE),
                    "",
                )
                self.failed += 1
                if self.scenario.startswith("build"):
                    raise self.mule.BuildError(f"mvn package failed: [ERROR] {path.name}: {line}", line)
                log = (f"ERROR Failed to deploy artifact [{name}]\nCaused by: org.mule.runtime.api.el."
                       f"ExpressionExecutionException: while evaluating {expression!r}\n")
                raise self.mule.DeployError(f"{name} failed to deploy", log)
        return super().start(app, backend_url=backend_url)


class SecondRequestProbe:
    """``a2m migrate`` with two fix attempts per proxy: the fake AI's first fix quotes escape canaries by placeholder
    in a guard a2m refuses (``guard``), or in a variable a2m writes, whose build (``build``) or deploy (``deploy``)
    then fails with an error quoting the restored code. No piece of any canary may reach any field of any request,
    the second fix request above all."""

    name = "second fix request (a refused guard, a failed build and a failed deploy quoting the restored fix)"
    SCENARIOS = ("guard", "build", "deploy", "build" + CREDENTIALS_SUFFIX, "deploy" + CREDENTIALS_SUFFIX)

    def __init__(self, root: Path, work: Path, report: Report) -> None:
        self.root = root
        self.work = work
        self.report = report

    def applies(self) -> bool:
        fix_loop = _import(self.root, "a2m.verify.fix_loop")
        return fix_loop is not None and callable(getattr(fix_loop, "run_with_fixes", None))

    def run(self) -> None:
        cli = _import(self.root, "a2m.cli")
        engine = _import(self.root, "a2m.engine")
        verify = _import(self.root, "a2m.verify")
        fake = _import(self.root, "a2m.ai.fake")
        mule = _import(self.root, "a2m.verify.mule")
        if not all(module is not None for module in (cli, engine, verify, fake, mule)) or not all(
            hasattr(mule, n) for n in ("BuildError", "DeployError")
        ):
            self.report.error(
                "the target has a2m.verify.fix_loop.run_with_fixes but not a2m.cli, a2m.engine, a2m.verify, "
                "a2m.ai.fake and a2m.verify.mule (BuildError, DeployError), so a second fix request could not be "
                "checked"
            )
            return
        for scenario in self.SCENARIOS:
            self._scenario(scenario, cli, engine, verify, fake, mule)

    def _scenario(self, scenario: str, cli: Any, engine: Any, verify: Any, fake: Any, mule: Any) -> None:
        base = self.work / f"escapes-{scenario}"
        exports, out = base / "exports", base / "out"
        write_escapes_proxy(exports)
        fixes: list[dict[str, str]] = []
        unusable = {"value": False}
        original_complete = fake.FakeProvider.complete

        def answering(provider: Any, request: Any) -> str:
            if _kind_of(request) != "fix":
                return original_complete(provider, request)
            fields = _request_fields(request)
            fixes.append(fields)
            if len(fixes) == 1:
                answer = _escape_fix(fields.get("prompt", ""), scenario)
                if answer is not None:
                    return answer
                unusable["value"] = True
            return json.dumps({"status": "cannot_fix", "reason": FIX_REASON})

        runner = EscapeRunner(verify.HttpResponse, scenario, mule)
        stages = [engine.parse, engine.generate, verify.make_verify_stage(runner=runner)]
        argv = ["migrate", str(exports), "--out", str(out), "--llm", "fake", "--max-fix-attempts", "2",
                "--mock-backends"]
        sink = io.StringIO()
        try:
            with _patched(fake.FakeProvider, "complete", answering), contextlib.redirect_stdout(sink), \
                    contextlib.redirect_stderr(sink):
                cli.main(argv, stages=stages)
        except SystemExit as exc:
            self.report.error(f"{scenario}: a2m migrate exited early ({exc.code!r}), so the scenario did not run")
            return
        finally:
            close = getattr(stages[-1], "close", None)
            if callable(close):
                close()
        label = f"second fix request, {scenario} scenario"
        for number, fields in enumerate(fixes, 1):
            for field, text in fields.items():
                for key, (value, what) in ESCAPE_CANARIES.items():
                    shown = sorted(piece for piece in escape_pieces(value) | {value} if piece in text)
                    if shown:
                        self.report.finding(
                            f"leak: canary {key} ({what}) reached the AI provider in fix request {number} field "
                            f"'{field}' ({label}): {', '.join(shown)}"
                        )
                for key, (value, what, _) in CREDENTIAL_CANARIES.items():
                    shown = sorted(mark for mark in credential_marks(key) | {value} if mark in text)
                    if shown:
                        self.report.finding(
                            f"leak: canary {key} ({what}) reached the AI provider in fix request {number} field "
                            f"'{field}' ({label}), partly masked or not: {', '.join(shown)}"
                        )
        if unusable["value"]:
            self.report.error(f"{label}: the first fix request did not show the canaries' placeholders")
        elif len(fixes) < 2:
            self.report.error(
                f"{label}: a2m sent {len(fixes)} fix request(s), not 2, so the second request could not be checked; a "
                "scenario that cannot run is never a pass"
            )
        elif scenario != "guard" and runner.failed == 0:
            self.report.error(f"{label}: the fix was never written and started, so no build or deploy error was made")
        else:
            problem = _scenario_problem(out, scenario)
            cause = START_CAUSES.get(scenario.removesuffix(CREDENTIALS_SUFFIX))
            if problem is not None:
                self.report.error(f"{label}: {problem}; a scenario that does not do what it is for is never a pass")
            elif cause is not None and cause not in fixes[1].get("prompt", ""):
                self.report.error(
                    f"{label}: fix request 2 does not say why the start failed ({cause!r}, from the error Maven or "
                    "Mule wrote, is not in it), so the AI was never told the cause; a scenario whose cause never "
                    "reached the AI is never a pass"
                )
            else:
                self.report.notes.append(f"{label}: {len(fixes)} fix requests checked")


def _scenario_problem(out: Path, scenario: str) -> str | None:
    """Why the escapes proxy's run under ``out`` did not do what ``scenario`` is for, or None: attempt 1 must be
    refused for its guard (``guard``: nothing written, the reason names the guard) or written and then undone because
    the start failed (``build``, ``deploy``), as its verification.json records it; and no failing test diff may quote
    a credential canary (its masked form would become a value of its own, and nothing would test the restored one)."""
    found = [path for path in sorted(out.rglob("verification.json")) if path.parent.name == ESCAPES]
    if not found:
        return f"proxy {ESCAPES} wrote no verification.json, so what came of attempt 1 is not known"
    try:
        data = json.loads(found[0].read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"the verification.json of {ESCAPES} cannot be read ({_short_error(exc)})"
    attempts = data.get("attempts") if isinstance(data, dict) else None
    first = attempts[0] if isinstance(attempts, list) and attempts and isinstance(attempts[0], dict) else None
    if first is None:
        return f"the verification.json of {ESCAPES} records no attempt 1"
    reason = str(first.get("reason", ""))
    written = bool(first.get("changed_files"))
    if first.get("helped") is not False:
        return f"attempt 1 is not recorded as not kept ({reason[:200]!r})"
    if scenario == "guard" and (written or GUARD_REFUSAL not in reason.lower()):
        return f"attempt 1 was not refused for its guard ({reason[:200]!r}), so the refusal path was not tested"
    if scenario != "guard" and not written:
        return f"attempt 1 was not written and started ({reason[:200]!r}), so no build or deploy error was tested"
    cases = data.get("cases") if isinstance(data, dict) else None
    diffs = " ".join(str(case.get("diff", "")) for case in cases if isinstance(case, dict)) if isinstance(cases, list) else ""
    quoted = sorted(key for key in CREDENTIAL_CANARIES if any(mark in diffs for mark in credential_marks(key)))
    if quoted:
        return f"a failing test diff quotes the credential canaries {', '.join(quoted)}"
    return None


def _parameters(func: Any) -> dict[str, inspect.Parameter]:
    try:
        return dict(inspect.signature(func).parameters)
    except (TypeError, ValueError):
        return {}


# ---------------------------------------------------------------- probe 2: request builders


B_CANARIES: dict[str, tuple[str, str]] = {
    "b-policy-header": ("CANARY-BPOLHDR-6W1E7R3T", "policy header X-Partner-Api-Key literal"),
    "b-policy-query": ("CANARY-BPOLQRY-2Y8U4I0O", "policy query access_token literal"),
    "b-policy-json": ("CANARY-BPOLJSON-9P5A1S7D", "policy JSON payload client_secret literal"),
    "b-policy-form": ("CANARY-BPOLFORM-4F0G6H2J", "policy password: literal"),
    "b-mule": ("CANARY-BMULE-8K4L0Z6X", "Mule file x-api-key literal"),
    "b-diff-header": ("CANARY-BDIFFHDR-3C9V5B1N", "failing-test diff X-Partner-Api-Key value"),
    "b-diff-key": ("CANARY-BDIFFKEY-7M3Q9W5E", "failing-test diff x-api-key header line"),
    "b-diff-form": ("CANARY-BDIFFFORM-1R7T3Y9U", "failing-test diff password= form value"),
    "b-diff-bearer": ("CANARY-BDIFFBEAR-5I1O7P3A", "failing-test diff Authorization Bearer token"),
    "b-header-key": ("CANARY-BHDRKEY-0S6D2F8G", "this run's X-Partner-Api-Key header value"),
    "b-header-bearer": ("CANARY-BHDRBEAR-4H0J6K2L", "this run's Authorization Bearer token"),
    "b-token": ("CANARY-BTOKEN-8Z4X0C6V", "a credential passed on its own"),
}


def _b(label: str) -> str:
    return B_CANARIES[label][0]


def _b_inputs() -> dict[str, Any]:
    return {
        "policy": (
            '<AssignMessage name="Set-Backend-Auth"><Set><Headers>'
            f'<Header name="X-Partner-Api-Key">{_b("b-policy-header")}</Header></Headers><QueryParams>'
            f'<QueryParam name="access_token">{_b("b-policy-query")}</QueryParam></QueryParams>'
            f'<Payload contentType="application/json">{{"client_secret": "{_b("b-policy-json")}", '
            '"keyword": "premium-tier-gold"}</Payload></Set>'
            f"<!-- password: {_b('b-policy-form')} --></AssignMessage>"
        ),
        "mule": (
            '<flow name="canary-flow"><set-variable variableName="partner" '
            f"value=\"#[{{'x-api-key': '{_b('b-mule')}'}}]\"/></flow>\n"
            f"x-api-key: {_b('b-mule')}"
        ),
        "diff": (
            "- test header-set (policy Set-Backend-Auth, AssignMessage):\n"
            f"    backend call 1 header X-Partner-Api-Key: expected '{_b('b-diff-header')}', actual missing\n"
            f"    backend call 1 request header x-api-key: {_b('b-diff-key')}\n"
            f"    backend call 1 body: expected 'user=svc&password={_b('b-diff-form')}', actual absent\n"
            f"    header Authorization: expected 'Bearer {_b('b-diff-bearer')}', actual absent"
        ),
        "headers": {
            "X-Partner-Api-Key": _b("b-header-key"),
            "Authorization": f"Bearer {_b('b-header-bearer')}",
            "api-version": "2024.01.15-rc1",
            "keyword": "premium-tier-gold",
        },
        "token": f"Bearer {_b('b-token')}",
    }


def _fill(name: str, inputs: dict[str, Any], masker: Any) -> Any:
    n = name.lower()
    if n == "masker":
        return masker
    if "diff" in n or "fail" in n:
        return inputs["diff"]
    if "polic" in n or n in ("original", "original_xml"):
        return inputs["policy"]
    if "mule" in n:
        return inputs["mule"]
    if "header" in n:
        return dict(inputs["headers"])
    if n in ("token", "secret", "credential", "api_key", "apikey", "password", "key"):
        return inputs["token"]
    if n in ("prompt", "text", "message"):
        return "\n".join((inputs["policy"], inputs["mule"], inputs["diff"]))
    return _MISSING


_MISSING = object()


class RequestBuilderProbe:
    """Public functions of the target's fix loop module whose every parameter the check can fill: no planted canary
    (each is a credential, whatever the target's own masker can hide) may be in what they return (the request for the
    AI) or in a log record they write."""

    name = "request builders (a2m.verify.fix_loop)"

    def __init__(self, root: Path, work: Path, report: Report) -> None:
        self.root = root
        self.report = report
        self.calls: list[tuple[str, Callable[..., Any], dict[str, str]]] = []

    def applies(self) -> bool:
        fix_loop = _import(self.root, "a2m.verify.fix_loop")
        masking = _import(self.root, "a2m.verify.masking")
        if fix_loop is None or masking is None or not isinstance(getattr(masking, "Masker", None), type):
            return False
        self.masker_type = masking.Masker
        try:
            self.masker_type()
        except TypeError:
            return False
        for name, func in sorted(vars(fix_loop).items()):
            if name.startswith("_") or name == "run_with_fixes" or not inspect.isfunction(func):
                continue
            if func.__module__ != fix_loop.__name__:
                continue
            params = _parameters(func)
            if not params:
                continue
            fillable = {}
            for param in params.values():
                if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
                    continue
                if _fill(param.name, _b_inputs(), None) is _MISSING:
                    if param.default is param.empty:
                        break
                    continue
                fillable[param.name] = param.name
            else:
                if fillable:
                    self.calls.append((name, func, fillable))
        return bool(self.calls)

    def run(self) -> None:
        for name, func, params in self.calls:
            inputs = _b_inputs()
            masker = self.masker_type()
            kwargs = {param: _fill(param, inputs, masker) for param in params}
            try:
                with _root_capture(lower_level=True) as records:
                    result = func(**kwargs)
            except Exception as exc:  # noqa: BLE001 (any failure of the target's function is reported, never a pass)
                self.report.error(f"a2m.verify.fix_loop.{name}() could not be driven: {_short_error(exc)}")
                continue
            texts = list(_texts_of(result))
            for key, (value, what) in B_CANARIES.items():
                fields = sorted({where for where, text in texts if value in text})
                if fields:
                    self.report.finding(
                        f"leak: canary {key} ({what}) reached the AI provider request built by "
                        f"a2m.verify.fix_loop.{name}() unmasked (field {', '.join(fields)})"
                    )
                loggers = sorted({logger or "root" for logger, line in records.lines if value in line})
                if loggers:
                    self.report.finding(
                        f"leak: canary {key} ({what}) reached run.log unmasked: a2m.verify.fix_loop.{name}() wrote it "
                        f"in a log record of logger {', '.join(repr(lg) for lg in loggers)}, as a handler on the root "
                        "logger gets it after propagation"
                    )


# ---------------------------------------------------------------- probe 3: the masker and look-alikes


C_LOOK_ALIKES = ("premium-tier-gold", "us-east-1-production", "2024.01.15-rc1", "westeurope", "electronics", "Hemingway")
C_TEXT = (
    '{"keyword": "premium-tier-gold", "region": "us-east-1-production", "api-version": "2024.01.15-rc1", '
    '"author": "Hemingway"}\n'
    "keyword: premium-tier-gold\nx-region: us-east-1-production\napi-version: 2024.01.15-rc1\n"
    "GET /search?keyword=electronics&region=westeurope&api-version=2024.01.15-rc1\n"
    "header api-version: expected '2024.01.15-rc1', actual '2023.12.01-rc9'\n"
    "query region: expected 'westeurope', actual absent\n"
    "body field author: expected 'Hemingway', actual 'E. Hemingway'\n"
)


class MaskerLookAlikeProbe:
    """The target's masker (fresh) masking text with harmless look-alikes: every look-alike must stay visible."""

    name = "masker look-alikes (a2m.verify.masking.Masker)"

    def __init__(self, root: Path, work: Path, report: Report) -> None:
        self.root = root
        self.report = report

    def applies(self) -> bool:
        masking = _import(self.root, "a2m.verify.masking")
        masker_type = getattr(masking, "Masker", None) if masking is not None else None
        if not isinstance(masker_type, type) or not any(
            callable(getattr(masker_type, m, None)) for m in ("mask", "mask_config")
        ):
            return False
        try:
            masker_type()
        except TypeError:
            return False
        self.masker_type = masker_type
        return True

    def run(self) -> None:
        for method in ("mask", "mask_config"):
            if not callable(getattr(self.masker_type, method, None)):
                continue
            try:
                shown = getattr(self.masker_type(), method)(C_TEXT)
            except Exception as exc:  # noqa: BLE001 (a crash of the target's masker is reported, never a pass)
                self.report.error(f"Masker().{method}() could not be driven: {_short_error(exc)}")
                continue
            if not isinstance(shown, str):
                self.report.error(f"Masker().{method}() returned {type(shown).__name__}, not text")
                continue
            for value in C_LOOK_ALIKES:
                if C_TEXT.count(value) != shown.count(value):
                    self.report.finding(
                        f"over-masked look-alike: Masker().{method}() masks the harmless value '{value}' (a keyword, "
                        "region, api-version or author is no credential)"
                    )


# ---------------------------------------------------------------- probe 4: the run.log funnel


D_CREDENTIALS = {
    "log-bearer": "Authorization: Bearer CANARY-LOGBEAR-4K8M2P6Q",
    "log-token": "token=CANARY-LOGTOKEN-7R3T9V1X",
    "log-api-key": "x-api-key: CANARY-LOGKEY-5B2N8C4Z",
    "log-password": "password: CANARY-LOGPASS-3H6J9L2D",
}
# Shapes that are a credential by their name alone, whatever the masker has learned: masked inline and as an argument.
D_ALWAYS = frozenset({"log-bearer"})


class _Capture(logging.Handler):
    """Keeps every record it gets as (logger name, rendered message)."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[tuple[str, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append((record.name, record.getMessage()))
        except Exception as exc:  # noqa: BLE001 (a record that cannot be rendered is kept as its error)
            self.lines.append((record.name, f"(unrenderable record: {type(exc).__name__})"))


@contextlib.contextmanager
def _root_capture(*, lower_level: bool) -> Iterator[_Capture]:
    """A handler on the root logger for the block: it gets every record any logger writes and propagates, after the
    filters of the logger that wrote it and none of its ancestors' (Python applies a logger's filters only to records
    written on that logger), so a masking filter on a parent logger does not hide a child's record from it.
    ``lower_level`` lets records of every level through for the block (the root logger's own level is restored)."""
    root = logging.getLogger()
    capture = _Capture()
    level = root.level
    root.addHandler(capture)
    if lower_level:
        root.setLevel(logging.DEBUG)
    try:
        yield capture
    finally:
        root.removeHandler(capture)
        root.setLevel(level)


def _all_loggers() -> list[logging.Logger]:
    loggers = [logging.getLogger()]
    loggers += [lg for lg in logging.Logger.manager.loggerDict.values() if isinstance(lg, logging.Logger)]
    return loggers


class LogFunnelProbe:
    """The target's run.log masking (``Masker().logging()``): on each logger it filters, a credential logged as a
    ``%s`` argument must come out masked whenever the same credential written inline does, and a credential by name
    (``Authorization: Bearer``) must come out masked both ways, as a handler on the root logger gets the records."""

    name = "run.log funnel (Masker.logging)"

    def __init__(self, root: Path, work: Path, report: Report) -> None:
        self.root = root
        self.report = report

    def applies(self) -> bool:
        masking = _import(self.root, "a2m.verify.masking")
        masker_type = getattr(masking, "Masker", None) if masking is not None else None
        if not isinstance(masker_type, type) or not callable(getattr(masker_type, "logging", None)):
            return False
        try:
            masker_type()
        except TypeError:
            return False
        self.masker_type = masker_type
        _import(self.root, "a2m.runlog")
        return True

    def run(self) -> None:
        before = {id(lg): list(lg.filters) for lg in _all_loggers()}
        masker = self.masker_type()
        with masker.logging():
            filtered = [lg for lg in _all_loggers() if [f for f in lg.filters if f not in before.get(id(lg), [])]]
            if not filtered:
                self.report.error("Masker().logging() installs no logging filter the check can find")
                return
            for logger in filtered:
                self._probe(logger)

    def _probe(self, logger: logging.Logger) -> None:
        level = logger.level
        logger.setLevel(logging.DEBUG)
        where = logger.name or "root"
        try:
            with _root_capture(lower_level=False) as capture:
                for key, credential in D_CREDENTIALS.items():
                    canary = credential.split()[-1].split("=")[-1]
                    capture.lines.clear()
                    logger.info(f"credential canary probe: {credential}")
                    logger.info("credential canary probe: %s", credential)
                    logger.info("credential canary probe: %(value)s", {"value": credential})
                    lines = [line for name, line in capture.lines if name == logger.name]
                    if len(lines) != 3:
                        self.report.error(f"logger {where!r}: the probe lines were not all written")
                        return
                    inline, *with_args = lines
                    if key in D_ALWAYS and canary in inline:
                        self.report.finding(
                            f"leak: canary {key} ({canary}) written inline reaches run.log unmasked (logger {where!r}), "
                            "although an Authorization Bearer value is a credential by its name alone"
                        )
                    if canary in inline and key not in D_ALWAYS:
                        continue  # a shape this masker hides only once learned; the full pipeline judges those
                    if any(canary in line for line in with_args):
                        self.report.finding(
                            f"leak: canary {key} ({canary}) passed as a %-style logging argument reaches run.log "
                            f"unmasked (logger {where!r})"
                            + ("" if canary in inline else ", while the same credential written inline is masked")
                        )
        finally:
            logger.setLevel(level)


# ---------------------------------------------------------------- the worker (runs in the isolated process)


PROBES = (FullPipelineProbe, SecondRequestProbe, RequestBuilderProbe, MaskerLookAlikeProbe, LogFunnelProbe)


def worker(root: Path, work: Path, result_path: Path) -> int:
    report = Report()
    try:
        _isolate(root)
        try:
            package = importlib.import_module("a2m")
        except Exception as exc:  # noqa: BLE001 (an unimportable target is reported, never a pass)
            report.error(f"the a2m package at {root} cannot be imported: {_short_error(exc)}")
            package = None
        if package is not None and _foreign_modules(root):
            report.error(f"import a2m did not load the target's package ({', '.join(_foreign_modules(root))})")
        elif package is not None:
            for probe_type in PROBES:
                probe = probe_type(root, work, report)
                try:
                    if not probe.applies():
                        continue
                    report.probes.append(probe.name)
                    probe.run()
                except Exception as exc:  # noqa: BLE001 (a probe that cannot run is reported, never a pass)
                    report.error(f"{probe.name} could not run: {_short_error(exc)}")
                    report.notes.append(traceback.format_exc(limit=8))
            foreign = _foreign_modules(root)
            if foreign:
                report.error(f"modules from outside the target were used: {', '.join(foreign)}")
            if not report.probes and not report.errors:
                report.error(
                    "the target exposes none of the interfaces the check drives (a2m.verify.fix_loop request "
                    "builders or run_with_fixes, a2m.verify.masking.Masker with mask, mask_config or logging)"
                )
    except Exception as exc:  # noqa: BLE001 (whatever happens, the parent gets a report)
        report.error(f"the check itself failed: {_short_error(exc)}")
    result_path.write_text(json.dumps(report.to_json()), encoding="utf-8")
    return 0


# ---------------------------------------------------------------- the command (parent process)


def package_root(target: Path) -> Path | None:
    """The directory holding the a2m package ``target`` is, holds, or sits in."""
    start = target if target.is_dir() else target.parent
    for folder in (start, *start.parents):
        if folder.name == "a2m" and (folder / "__init__.py").is_file():
            return folder.parent
        if (folder / "a2m" / "__init__.py").is_file():
            return folder
    return None


def _child_env(tmp: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key != "ANTHROPIC_API_KEY" and not key.startswith(("A2M_", "PYTHON", "FORCE_COLOR"))
    }
    env["TMPDIR"] = str(tmp)
    env["NO_COLOR"] = "1"
    return env


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith(("Traceback", "  "))]
    return lines[-1][:300] if lines else "(no output)"


def neutral(text: str) -> str:
    """``text`` with every word a detection report uses abbreviated (``credential`` -> ``cred``, ``Masker`` ->
    ``Msker``, a path such as ``credential-variants`` -> ``cred-variants``), for the exit 2 output."""

    def short(match: re.Match[str]) -> str:
        word = match.group(0)
        form = NEUTRAL_FORMS[word.lower()]
        return form.capitalize() if word[0].isupper() else form

    return DETECTION_WORDS.sub(short, text)


def _cannot_check(text: str) -> int:
    """Print the exit 2 report ``text`` (neutral, see ERROR_PREFIX) and return 2."""
    print(neutral(f"{ERROR_PREFIX}: COULD NOT RUN, {text}"))
    return 2


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == [WORKER_FLAG] and len(args) == 4:
        return worker(Path(args[1]), Path(args[2]), Path(args[3]))
    if len(args) != 1:
        print(neutral(f"{ERROR_PREFIX}: usage: {Path(__file__).name} <a2m package directory>"), file=sys.stderr)
        return 2
    target = Path(args[0])
    shown = args[0]
    if not target.exists():
        return _cannot_check(f"{shown} does not exist, so nothing was checked")
    root = package_root(target.absolute())
    if root is None:
        return _cannot_check(f"no a2m package found at or around {shown}, so nothing was checked")
    with tempfile.TemporaryDirectory(prefix="a2m-credential-canary-") as tmp_name:
        tmp = Path(tmp_name)
        work = tmp / "work"
        work.mkdir()
        result_path = tmp / "result.json"
        command = [sys.executable, "-I", "-B", str(Path(__file__).resolve()), WORKER_FLAG, str(root), str(work),
                   str(result_path)]
        try:
            proc = subprocess.run(command, cwd=work, env=_child_env(tmp), capture_output=True, text=True,
                                  timeout=WORKER_TIMEOUT_SECONDS, check=False)
        except subprocess.TimeoutExpired:
            return _cannot_check(f"driving {shown} took longer than {WORKER_TIMEOUT_SECONDS}s, so nothing was ruled out")
        try:
            data = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return _cannot_check(
                f"the check could not drive {shown} (exit {proc.returncode}: {_last_line(proc.stderr)}), so nothing "
                "was ruled out"
            )
    return _print_report(shown, data)


def _print_report(shown: str, data: dict[str, list[str]]) -> int:
    findings, errors, probes = data.get("findings", []), data.get("errors", []), data.get("probes", [])
    ran = "; ".join(probes) or "none"
    if findings:
        print(f"{PREFIX}: FAILED on {shown}: {len(findings)} credential leak or masked look-alike problem(s)")
        for line in findings:
            print(f"  - {line}")
        for line in errors:
            print(f"  - could not check: {line}")
        print(f"  probes run: {ran}")
        print("  Every credential must be masked before it reaches the AI provider, run.log or verification.json, "
              "and harmless look-alikes must stay visible.")
        return 1
    if errors:
        details = "".join(f"\n  - {line}" for line in errors)
        return _cannot_check(
            f"the check could not drive {shown}, so nothing was ruled out (exit 2: not a detection){details}\n"
            f"  probes run: {ran}"
        )
    notes = "; ".join(note for note in data.get("notes", []) if "\n" not in note)
    print(f"{PREFIX}: passed on {shown}: no canary leaked and no look-alike was masked (probes: {ran}"
          + (f"; {notes}" if notes else "") + ")")
    return 0


if __name__ == "__main__":
    sys.exit(main())
