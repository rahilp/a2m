"""CP10 adversarial round 8: a regular expression or "/" stretch a2m showed, left as shown, is written back as it was.

Round 7 refused every text placeholder in a regular expression or an unsure "/" stretch, which also refused the
placeholder a2m itself showed there: any fix-loop edit to an expression or script that holds a regular expression (every
``MatchesPath``, ``Matches`` and ``JavaRegex`` condition becomes ``matches /.../``) or a division chain after a function
call was refused. Now a stretch a2m showed, that the answer left in the same place (the same Mule part, language, kind
of stretch and code segment right before it) with the same content, is written back from its original bytes; any other
placeholder there (moved in, newly written or edited) is still refused, with a reason that says to keep the stretch as
shown. A value written back into a plain Mule attribute or text never becomes a Mule expression.

Case IDs:

* CP10-X54 - a regular expression a2m showed, unchanged, with an unrelated edit elsewhere in the same attribute or
  script, restores byte for byte (a MatchesPath, Matches and JavaRegex when-expression from a2m's condition
  translator, a sure ``replace``, a ``splitBy`` when a key is added).
* CP10-X55 - an unsure "/" stretch a2m showed (a division chain after ``sum(...)``, an unsure ``replace``),
  unchanged, with an unrelated edit restores byte for byte.
* CP10-X56 - a placeholder moved into a regular expression or stretch, written after another word, into another part,
  or edited, is refused, while the same answer with the stretch kept restores.
* CP10-X57 - the refusal reason says to keep the stretch as shown and never suggests a string literal.
* CP10-X58 - a value holding "#[", or one that makes "#[" with what is next to it, is refused in a plain Mule
  attribute or text, unless a2m showed that same value in that same place.
"""

from __future__ import annotations

import re
from xml.sax.saxutils import quoteattr

import pytest

from a2m.ai.placeholders import PlaceholderError, Placeholders
from a2m.conditions.dataweave import translate_condition

CORE = "http://www.mulesoft.org/schema/mule/core"
EE = "http://www.mulesoft.org/schema/mule/ee/core"
DOC = "http://www.mulesoft.org/schema/mule/documentation"
WRAP = f'<mule xmlns="{CORE}" xmlns:ee="{EE}" xmlns:doc="{DOC}"><flow name="f">{{}}</flow></mule>'
TOKEN_RE = re.compile(r"«[vn]\d+»")


def _script(body: str) -> str:
    """A Transform Message whose payload script is ``body`` (DataWeave, with its header)."""
    return WRAP.format(
        '<ee:transform doc:name="JS-Clean"><ee:message><ee:set-payload><![CDATA[%dw 2.0\noutput application/json\n'
        f"---\n{body}]]></ee:set-payload></ee:message></ee:transform>"
    )


def _status_fix(body: str) -> tuple[Placeholders, str, str]:
    """The fix loop's path for a script ``body`` whose ``status: 'ok'`` a diff says should be 'done': the table, the
    answer that changes only that value (as the diff's placeholder), and the file the answer should restore to."""
    original = _script(body)
    table = Placeholders(["f", "JS-Clean"])
    shown = table.mule({"a.xml": original})["a.xml"]
    assert shown is not None
    diff = table.diff("body $.status: expected 'done', actual 'ok'")
    found = re.search(r"expected '(«v\d+»)', actual '(«v\d+»)'", diff)
    assert found is not None, diff
    expected, actual = found.groups()
    assert f"status: '{actual}'" in shown, shown
    answer = shown.replace(f"status: '{actual}'", f"status: '{expected}'")
    return table, answer, original.replace("status: 'ok'", "status: 'done'")


# ------------------------------------------------------------------------------------------------ X54: sure regex


@pytest.mark.parametrize(
    "condition",
    ['proxy.pathsuffix MatchesPath "/orders"', 'proxy.pathsuffix MatchesPath "/orders/**"',
     'request.header.x-id JavaRegex "[a-z]+"', 'request.header.x-id Matches "ab*"'],
)
def test_CP10_X54_an_unchanged_condition_regex_restores_when_the_ai_edits_the_when_expression(condition: str) -> None:
    """[CP10-X54] The reviewers' repro: a when-expression a2m's condition translator wrote (``matches /.../``) is
    shown with its regular expression hidden; an answer that appends `` and true`` to the expression, leaving the
    regular expression as shown, restores byte for byte (round 7 refused it)."""
    translation = translate_condition(condition)
    assert translation.ok and translation.dw is not None and " matches /" in translation.dw, translation
    dw = translation.dw
    route = '<logger level="INFO" message="routed"/>'
    original = WRAP.format(f"<choice><when expression={quoteattr('#[' + dw + ']')}>{route}</when></choice>")
    table = Placeholders(["f"])
    shown = table.mule({"flow.xml": original})["flow.xml"]
    assert shown is not None and re.search(r"matches /«v\d+»/", shown), shown
    assert table.restore("flow.xml", shown) == original
    found = re.search(r"expression=(['\"])(#\[.*?)\]\1", shown)
    assert found is not None, shown
    answer = shown.replace(found.group(0), f"expression={found.group(1)}{found.group(2)} and true]{found.group(1)}")

    expected = original.replace(quoteattr("#[" + dw + "]"), quoteattr("#[" + dw + " and true]"))
    assert table.restore("flow.xml", answer) == expected


def test_CP10_X54_an_unchanged_sure_regex_restores_when_the_ai_fixes_another_value_of_the_script() -> None:
    """[CP10-X54] The reviewer's repro (c): ``payload.phone replace /[^0-9]/ with ''`` shows ``/«vN»/``; the fix
    changes only ``status`` to the diff's expected value, and the regular expression comes back byte for byte."""
    table, answer, expected = _status_fix("{ phone: payload.phone replace /[^0-9]/ with '', status: 'ok' }")
    assert "[^0-9]" not in answer and re.search(r"replace /«v\d+»/", answer), answer
    assert table.restore("a.xml", answer) == expected


def test_CP10_X54_an_unchanged_split_regex_restores_when_the_ai_adds_a_key() -> None:
    """[CP10-X54] ``{ ids: payload.csv splitBy /,/, a: "x" }`` in a set-payload attribute: an answer that adds a key
    after it restores the regular expression byte for byte."""
    original = WRAP.format('<set-payload value=\'#[{ ids: payload.csv splitBy /,/, a: "x" }]\'/>')
    table = Placeholders(["f"])
    shown = table.mule({"flow.xml": original})["flow.xml"]
    assert shown is not None and re.search(r"splitBy /«v\d+»/", shown), shown
    answer = re.sub(r'(a: "«v\d+»")', r"\1, b: 1", shown)
    assert answer != shown

    assert table.restore("flow.xml", answer) == original.replace('a: "x" }', 'a: "x", b: 1 }')


# ------------------------------------------------------------------------------------------------ X55: unsure stretch


def test_CP10_X55_an_unchanged_division_chain_restores_when_the_ai_fixes_another_value_of_the_script() -> None:
    """[CP10-X55] The reviewer's repro (a): ``sum(payload.x) / sizeOf(payload.x) / 100`` hides the stretch after the
    first "/" (it may start a regular expression); the fix changes only ``status``, and the line comes back byte for
    byte."""
    table, answer, expected = _status_fix("{ avg: sum(payload.x) / sizeOf(payload.x) / 100, status: 'ok' }")
    assert "sizeOf" not in answer and re.search(r"sum\(payload\.x\) / «v\d+» / «n\d+»", answer), answer
    assert table.restore("a.xml", answer) == expected


def test_CP10_X55_an_unchanged_unsure_replace_restores_when_the_ai_fixes_another_value_of_the_script() -> None:
    """[CP10-X55] The reviewer's repro (b): ``(payload.phone default '') replace /[^0-9]/ with ''`` (a "/" after the
    ")" of a call-like group is unsure); the fix changes only ``status``, and the regular expression comes back."""
    table, answer, expected = _status_fix("{ phone: (payload.phone default '') replace /[^0-9]/ with '', status: 'ok' }")
    assert "[^0-9]" not in answer, answer
    assert table.restore("a.xml", answer) == expected


def test_CP10_X55_an_unchanged_division_chain_restores_in_a_mule_expression() -> None:
    """[CP10-X55] The same division chain in a ``#[...]`` attribute: an edit after it restores the stretch."""
    original = WRAP.format('<set-variable variableName="avg" value="#[sum(payload.x) / sizeOf(payload.x) / 100]"/>')
    table = Placeholders(["f"])
    shown = table.mule({"flow.xml": original})["flow.xml"]
    assert shown is not None and "sizeOf" not in shown, shown
    answer = shown.replace(']"/>', ' default 0]"/>')

    assert table.restore("flow.xml", answer) == original.replace('100]"/>', '100 default 0]"/>')


# ------------------------------------------------------------------------------------------------ X56: moved or edited


def test_CP10_X56_a_placeholder_moved_into_a_regular_expression_is_refused() -> None:
    """[CP10-X56] The stretch a2m showed, kept, restores; a placeholder a2m showed elsewhere (a string literal)
    written into the regular expression, the regular expression's own placeholder edited, written after another word,
    or written into another part, is refused."""
    body = "{ phone: payload.phone replace /[^0-9]/ with '', status: 'ok' }"
    table, answer, expected = _status_fix(body)
    assert table.restore("a.xml", answer) == expected  # kept as shown
    regex = re.search(r"replace /(«v\d+»)/", answer)
    status = re.search(r"status: '(«v\d+»)'", answer)
    assert regex is not None and status is not None, answer
    token, text = regex.group(1), status.group(1)

    moved = answer.replace(f"/{token}/", f"/{text}/")
    edited = answer.replace(f"/{token}/", f"/{token}x/")
    other_word = answer.replace(f"replace /{token}/ with ''", f"replace /{token}/ with '', ids: payload.a splitBy /{token}/")
    for refused in (moved, edited, other_word):
        assert refused != answer
        with pytest.raises(PlaceholderError, match="regular expression"):
            table.restore("a.xml", refused)
    other_part = answer.replace(
        "</ee:transform>", f"</ee:transform><set-variable variableName=\"p\" value=\"#[payload.phone replace /{token}/ "
        "with '']\"/>"
    )
    with pytest.raises(PlaceholderError, match="regular expression"):
        table.restore("a.xml", other_part)


def test_CP10_X56_an_unsure_stretch_placeholder_moved_into_a_regular_expression_is_refused() -> None:
    """[CP10-X56] The division chain's hidden stretch kept restores; its placeholder written into a sure regular
    expression, or the stretch edited, is refused."""
    table, answer, expected = _status_fix("{ avg: sum(payload.x) / sizeOf(payload.x) / 100, status: 'ok' }")
    assert table.restore("a.xml", answer) == expected
    found = re.search(r"sum\(payload\.x\) / («v\d+») /", answer)
    assert found is not None, answer
    token = found.group(1)

    for refused in (
        answer.replace("status:", f"m: payload matches /{token}/, status:"),
        answer.replace(f"/ {token} /", f"/ {token} + 1 /"),
    ):
        with pytest.raises(PlaceholderError, match="regular expression"):
            table.restore("a.xml", refused)


# ------------------------------------------------------------------------------------------------ X57: refusal reason


def test_CP10_X57_the_refusal_says_to_keep_the_stretch_as_shown_never_to_quote_it() -> None:
    """[CP10-X57] Fed back to the next attempt, the reason must not suggest a string literal (that would turn a
    pattern match into a literal match, or code into text): it says to keep the stretch exactly as shown."""
    table, answer, _ = _status_fix("{ phone: payload.phone replace /[^0-9]/ with '', avg: sum(payload.x) / "
                                   "sizeOf(payload.x) / 100, status: 'ok' }")
    status = re.search(r"status: '(«v\d+»)'", answer)
    assert status is not None, answer
    text = status.group(1)
    regex_answer = re.sub(r"replace /«v\d+»/", f"replace /{text}/", answer)
    unsure_answer = re.sub(r"sum\(payload\.x\) / «v\d+» /", f"sum(payload.x) / {text} /", answer)
    for refused in (regex_answer, unsure_answer):
        assert refused != answer
        with pytest.raises(PlaceholderError) as caught:
            table.restore("a.xml", refused)
        reason = str(caught.value)
        assert "exactly as shown" in reason or "as a2m showed it" in reason, reason
        assert "inside the quotes" not in reason and "write the text in a string literal" not in reason, reason


# ------------------------------------------------------------------------------------------------ X58: no new "#["


def test_CP10_X58_a_value_never_becomes_a_mule_expression_in_a_plain_attribute_or_text() -> None:
    """[CP10-X58] A diff value holding "#[" written into a plain attribute or element text is refused, and so is a
    value that makes "#[" with the text or placeholder next to it; inside a DataWeave string literal of ``#[...]`` the
    same value is only text and restores."""
    original = WRAP.format('<set-variable variableName="who" value="anon"/><logger level="INFO" message="hi"/>')
    table = Placeholders(["f", "who"])
    shown = table.mule({"flow.xml": original})["flow.xml"]
    assert shown is not None
    diff = table.diff("body $.a: expected '#[payload]', actual 'x'\nbody $.b: expected '[payload]', actual 'abc#'")
    hashed, _, bracket, ends = TOKEN_RE.findall(diff)
    plain = re.search(r'value="(«v\d+»)"', shown)
    assert plain is not None, shown

    for value in (hashed, f"#{bracket}", f"{ends}[1]", f"{ends}{bracket}", f"pre {hashed}"):
        answer = shown.replace(plain.group(0), f'value="{value}"')
        with pytest.raises(PlaceholderError, match="Mule expression"):
            table.restore("flow.xml", answer)
    text_answer = shown.replace("</flow>", f"<ee:note>{hashed}</ee:note></flow>")
    with pytest.raises(PlaceholderError, match="Mule expression"):
        table.restore("flow.xml", text_answer)

    quoted = shown.replace(plain.group(0), f"value=\"#['{hashed}']\"")
    assert 'value="#[\'#[payload]\']"' in table.restore("flow.xml", quoted)
    assert table.restore("flow.xml", shown.replace(plain.group(0), f'value="{bracket}"')).count('value="[payload]"') == 1


def test_CP10_X58_a_value_a2m_showed_with_its_hash_in_that_same_place_restores_and_nowhere_else() -> None:
    """[CP10-X58] A plain value that already held "#[" where a2m showed it (an expression that never closes, shown
    as one placeholder) may stay in that same attribute when the answer edits it; written into another attribute it
    is refused."""
    original = WRAP.format('<logger level="INFO" message="see #[ docs"/><set-variable variableName="who" value="anon"/>')
    table = Placeholders(["f", "who"])
    shown = table.mule({"flow.xml": original})["flow.xml"]
    assert shown is not None
    found = re.search(r'message="(«v\d+»)"', shown)
    other = re.search(r'value="(«v\d+»)"', shown)
    assert found is not None and other is not None, shown
    token = found.group(1)
    assert table.values[token] == "see #[ docs"
    assert table.restore("flow.xml", shown) == original

    with pytest.raises(PlaceholderError, match="Mule expression"):
        table.restore("flow.xml", shown.replace(other.group(0), f'value="{token}"'))
    edited = shown.replace(found.group(0), f'message="{token} now"')
    assert table.restore("flow.xml", edited) == original.replace("see #[ docs", "see #[ docs now")
