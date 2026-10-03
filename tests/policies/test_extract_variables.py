"""CP4: the ExtractVariables template (contract in tests/policies/conftest.py)."""

from __future__ import annotations

import re

from .conftest import Kit, policy_xml

JSON_ORDER_ID = (
    "    <JSONPayload>\n        <Variable name=\"orderId\">\n"
    "            <JSONPath>$.order.id</JSONPath>\n        </Variable>\n    </JSONPayload>\n"
)


def reads_json_order_id(value: str) -> bool:
    """The value reads the body (payload) at order.id."""
    return "payload" in value and re.search(r"order\W{1,4}id\b", value) is not None


def test_CP4_T24_extract_variables_pulls_a_json_value_into_a_prefixed_variable(kit: Kit) -> None:
    """[CP4-T24] ExtractVariables pulls a value from the JSON body into a prefixed variable."""
    body = "    <Source>request</Source>\n    <VariablePrefix>ext</VariablePrefix>\n" + JSON_ORDER_ID
    out = kit.translate(policy_xml("ExtractVariables", "Extract-Order-Id", body))

    assert out.method == "template", out.reason
    assert out.result.type == "ExtractVariables"
    values = out.var_values("ext.orderId")
    assert len(values) == 1, (sorted(out.written_vars()), values)
    assert reads_json_order_id(values[0]), values[0]


def test_CP4_T25_extract_variables_pulls_values_from_path_query_and_header(kit: Kit) -> None:
    """[CP4-T25] ExtractVariables pulls values from the URI path, a query parameter and a header."""
    body = (
        "    <Source>request</Source>\n"
        "    <URIPath>\n        <Pattern ignoreCase=\"false\">/orders/{id}</Pattern>\n    </URIPath>\n"
        '    <QueryParam name="page">\n        <Pattern>{page}</Pattern>\n    </QueryParam>\n'
        '    <Header name="Authorization">\n        <Pattern>Bearer {token}</Pattern>\n    </Header>\n'
    )
    out = kit.translate(policy_xml("ExtractVariables", "Extract-Parts", body))

    assert out.method == "template", out.reason
    values = {name: out.var_values(name) for name in ("id", "page", "token")}
    assert all(len(v) == 1 for v in values.values()), (sorted(out.written_vars()), values)
    path_value, page_value, token_value = (values[name][0] for name in ("id", "page", "token"))

    assert re.search(r"requestPath|maskedRequestPath|rawRequestPath|requestUri", path_value), path_value
    assert "orders" in path_value, path_value
    assert re.search(r"""queryParams\s*(?:\.\s*(['"]?)page\1(?![\w-])|\[\s*(['"])page\2\s*\])""", page_value), page_value
    assert re.search(
        r"""headers\s*(?:\.\s*(['"]?)authorization\1(?![\w-])|\[\s*(['"])authorization\2\s*\])""", token_value, re.IGNORECASE
    ), token_value
    assert "Bearer " in token_value, token_value


def test_CP4_T26_extract_variables_options_the_template_cannot_carry_are_listed(kit: Kit) -> None:
    """[CP4-T26] ExtractVariables options the template cannot carry are listed."""
    body = '    <Source clearPayload="true">request</Source>\n    <VariablePrefix>ext</VariablePrefix>\n' + JSON_ORDER_ID
    out = kit.translate(policy_xml("ExtractVariables", "Extract-And-Clear", body))

    assert "clearPayload" in out.option_text(), out.option_text()
    values = out.var_values("ext.orderId")
    assert len(values) == 1, (sorted(out.written_vars()), values)
    assert reads_json_order_id(values[0]), values[0]
