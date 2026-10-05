"""CP10 adversarial round 10: the three inputs of the diff rule (context, alignment, part matching) made sound.

* CP10-X64 - a placeholder counts as unchanged only when it is read in the same context at both ends (the same
  part language and the same enclosing chain: inside ``#[...]``, a string, JSON text, plain text). A region the
  answer wraps in ``#[...]``, unwraps or turns into plain text is a change, spelled by the rules for where it now
  stands (``$19.99`` becomes ``\\$19.99`` in DataWeave, ``$(...)`` never becomes code).
* CP10-X65 - re-indenting a script (wrapping its body in ``if``) does not defeat the alignment: lines are diffed
  with blank-normalised keys first, tokens only inside unmatched hunks; above the cap the refusal says the part
  changed too much to line up, not that a placeholder was moved.
* CP10-X66 - parts are matched by aligning the sibling sequences, not by ordinal: a new element inserted before
  (or an element deleted before) an untouched one of the same tag leaves the untouched one matched; an inserted
  part is new and a paste between siblings is still refused.
* CP10-X67 - an answer that only changes an attribute's quote character keeps the original bytes (entity spelling)
  of each unchanged value, escaping only the new quote character.
"""

from __future__ import annotations

import re
import time

import pytest

from a2m.ai.placeholders import PlaceholderError, Placeholders

CORE = "http://www.mulesoft.org/schema/mule/core"
EE = "http://www.mulesoft.org/schema/mule/ee/core"
DOC = "http://www.mulesoft.org/schema/mule/documentation"
HEAD = f'<mule xmlns="{CORE}"><flow name="f">'
TAIL = "</flow></mule>"


def _show(original: str, names: tuple[str, ...] = ("f",)) -> tuple[Placeholders, str]:
    table = Placeholders(list(names))
    shown = table.mule({"a.xml": original})["a.xml"]
    assert shown is not None
    assert table.restore("a.xml", shown) == original
    return table, shown


# ------------------------------------------------------------------------------------------------ X64: context


def test_CP10_X64_a_json_payload_wrapped_in_an_expression_is_spelled_for_dataweave() -> None:
    """[CP10-X64] The reviewer's c4: a JSON text attribute wrapped in ``#[...]`` by a fix that adds a computed
    field. Its values are now read in DataWeave, so ``$19.99`` is written ``\\$19.99`` (``"$19.99"`` does not
    compile) and ``$(vars.user)`` is escaped, never turned into an interpolation."""
    original = HEAD + """<set-payload value='{"sku": "A-1", "price": "$19.99", "msg": "Hello $(vars.user)"}' """ \
        'mimeType="application/json"/>' + TAIL
    table, shown = _show(original)
    answer = shown.replace("value='{", "value='#[{").replace("}' mime", ', "id": vars.orderId}]\' mime')
    assert answer != shown
    restored = table.restore("a.xml", answer)
    assert restored == HEAD + (
        """<set-payload value='#[{"sku": "A-1", "price": "\\$19.99", "msg": "Hello \\$(vars.user)", """
        """"id": vars.orderId}]' mimeType="application/json"/>"""
    ) + TAIL, restored
    # A plain value (shown as one placeholder) put inside a string of a new #[...] is spelled for that string.
    plain = HEAD + '<set-variable variableName="price" value="$19.99 net"/>' + TAIL
    table, shown = _show(plain, ("f", "price"))
    found = re.search(r'value="(«v\d+»)"', shown)
    assert found is not None, shown
    token = found.group(1)
    answer = shown.replace(f'value="{token}"', f"value=\"#['{token}' ++ vars.suffix]\"")
    restored = table.restore("a.xml", answer)
    assert restored == HEAD + "<set-variable variableName=\"price\" value=\"#['\\$19.99 net' ++ vars.suffix]\"/>" \
        + TAIL, restored


def test_CP10_X64_an_expression_unwrapped_to_json_is_spelled_for_json() -> None:
    """[CP10-X64] The reverse (c1 case 3): ``#[{"msg": "a\\$b"}]`` unwrapped to JSON text gives ``"a$b"``, never
    the invalid JSON escape ``\\$``."""
    original = HEAD + """<set-payload value='#[{"msg": "a\\$b"}]' mimeType="application/json"/>""" + TAIL
    table, shown = _show(original)
    answer = shown.replace("value='#[{", "value='{").replace("}]' mime", ', "id": 1}\' mime')
    assert answer != shown
    restored = table.restore("a.xml", answer)
    assert restored == HEAD + """<set-payload value='{"msg": "a$b", "id": 1}' mimeType="application/json"/>""" \
        + TAIL, restored


def test_CP10_X64_json_text_turned_into_plain_text_gets_the_plain_values() -> None:
    """[CP10-X64] c1 case 2: JSON text followed by a word is plain text, so each value is its plain value
    (``C:\\temp/x``), never its JSON spelling (``C:\\\\temp\\/x``). An edit that keeps the JSON text keeps the
    source's spellings."""
    original = HEAD + """<set-payload value='{"path": "C:\\\\temp\\/x"}' mimeType="application/json"/>""" + TAIL
    table, shown = _show(original)
    kept = shown.replace("}' mime", ', "id": 1}\' mime')
    assert table.restore("a.xml", kept) == original.replace("}' mime", ', "id": 1}\' mime')
    plain = shown.replace("}' mime", "} trailing' mime")
    restored = table.restore("a.xml", plain)
    assert restored == HEAD + """<set-payload value='{"path": "C:\\temp/x"} trailing' mimeType="application/json"/>""" \
        + TAIL, restored


# ------------------------------------------------------------------------------------------------ X65: alignment


def _script_file(body: str) -> str:
    return (
        f'<mule xmlns="{CORE}" xmlns:ee="{EE}"><flow name="f">\n'
        f'<ee:transform doc:name="JS-Clean" xmlns:doc="{DOC}"><ee:message><ee:set-payload><![CDATA[%dw 2.0\n'
        f"output application/json\n---\n{body}]]></ee:set-payload></ee:message></ee:transform>\n</flow></mule>"
    )


def _script_body(lines: int) -> str:
    rows = ["{"]
    rows += [f"  field{index}: payload.items[{index % 3}].name default ''," for index in range(lines)]
    rows += ["  phone: (payload.phone default '') replace /[^0-9]/ with '',", "  status: 'ok'", "}"]
    return "\n".join(rows)


def _status_tokens(table: Placeholders) -> tuple[str, str]:
    diff = table.diff("body $.status: expected 'done', actual 'ok'")
    found = re.search(r"expected '(«v\d+»)', actual '(«v\d+»)'", diff)
    assert found is not None, diff
    return found.group(1), found.group(2)


def _rewrap(text: str, wrap: bool) -> str:
    """The script after ``---`` in ``text`` re-indented by two spaces, and wrapped in ``if`` when ``wrap``."""
    head, rest = text.split("---\n", 1)
    script, tail = rest.split("]]>", 1)
    body = "\n".join("  " + line for line in script.split("\n"))
    if wrap:
        body = "if (payload != null)\n" + body + "\nelse {}"
    return head + "---\n" + body + "]]>" + tail


@pytest.mark.parametrize("wrap", [True, False])
@pytest.mark.parametrize("lines", [13, 50, 200])
def test_CP10_X65_a_reindented_script_restores_its_untouched_regex(lines: int, wrap: bool) -> None:
    """[CP10-X65] The reviewer's c2: the status fix with the script body wrapped in ``if`` (or only re-indented).
    Every line is the same once its blanks are ignored, so the untouched ``/[^0-9]/`` comes back byte for byte
    (from 10 lines on it was refused, saying the placeholder was moved). The restore stays fast."""
    original = _script_file(_script_body(lines))
    table = Placeholders(["f", "JS-Clean"])
    shown = table.mule({"a.xml": original})["a.xml"]
    assert shown is not None
    expected, actual = _status_tokens(table)
    answer = _rewrap(shown.replace(f"status: '{actual}'", f"status: '{expected}'"), wrap)
    started = time.perf_counter()
    restored = table.restore("a.xml", answer)
    elapsed = time.perf_counter() - started
    assert restored == _rewrap(original.replace("status: 'ok'", "status: 'done'"), wrap)
    assert elapsed < 5.0, elapsed


def test_CP10_X65_a_part_too_changed_to_line_up_is_refused_with_that_reason() -> None:
    """[CP10-X65] Above the cap (one long line whose every token around the regex changes at both ends), the
    regex cannot be lined up; the refusal says the part changed too much to line up with what a2m showed, not that
    the placeholder was moved, and it comes back fast."""
    fields = " ".join(f"f{index}: vars.a{index}," for index in range(1500))
    body = "{ " + fields + " phone: (payload.phone default '') replace /[^0-9]/ with '', " + fields + " }"
    original = _script_file(body)
    table = Placeholders(["f", "JS-Clean"])
    shown = table.mule({"a.xml": original})["a.xml"]
    assert shown is not None
    answer = re.sub(r"vars\.a(\d+)", r"vars.b\1", shown)
    started = time.perf_counter()
    with pytest.raises(PlaceholderError) as caught:
        table.restore("a.xml", answer)
    elapsed = time.perf_counter() - started
    reason = str(caught.value)
    assert "line up" in reason, reason
    assert "did not show it" not in reason, reason
    assert elapsed < 10.0, elapsed


# ------------------------------------------------------------------------------------------------ X66: parts


ISV1 = '<set-variable variableName="isV1" value="#[attributes.requestPath matches /^\\/v1\\/.*/]"/>'
CHOICE = (
    '<choice><when expression="#[((attributes.maskedRequestPath default &quot;&quot;) matches /\\/orders\\/.*/)]">'
    '<logger level="INFO" message="#[vars.a]"/></when></choice>'
)
NAMES = ("f", "isV1", "tenant", "first", "INFO")


def test_CP10_X66_a_new_set_variable_before_an_untouched_one_keeps_its_regex() -> None:
    """[CP10-X66] The reviewer's c3 case 1: a new ``<set-variable>`` inserted before an untouched one with a regex.
    The untouched one is still matched with what a2m showed, so it restores byte for byte (it was refused)."""
    original = HEAD + ISV1 + '<logger level="INFO" message="#[vars.isV1]"/>' + TAIL
    table, shown = _show(original, NAMES)
    new = '<set-variable variableName="tenant" value="#[attributes.headers.tenant]"/>'
    answer = shown.replace("<set-variable", new + "<set-variable", 1)
    assert table.restore("a.xml", answer) == original.replace("<set-variable", new + "<set-variable", 1)


def test_CP10_X66_a_new_choice_before_a_generated_choice_keeps_its_regex() -> None:
    """[CP10-X66] c3 case 2: a2m's own condition output, with a new ``<choice>`` inserted before it."""
    original = HEAD + CHOICE + TAIL
    table, shown = _show(original, NAMES)
    new = (
        "<choice><when expression=\"#[attributes.method == 'OPTIONS']\"><set-payload value=\"#['']\"/></when>"
        "</choice>"
    )
    answer = shown.replace("<choice>", new + "<choice>", 1)
    assert table.restore("a.xml", answer) == original.replace("<choice>", new + "<choice>", 1)


def test_CP10_X66_deleting_an_earlier_sibling_keeps_the_later_ones_regex() -> None:
    """[CP10-X66] An earlier ``<set-variable>`` with a regex of its own deleted: the later untouched one restores."""
    first = '<set-variable variableName="first" value="#[vars.x matches /^a+$/]"/>'
    original = HEAD + first + ISV1 + TAIL
    table, shown = _show(original, NAMES)
    shown_first = shown[shown.index("<set-variable") : shown.index("/>") + 2]
    answer = shown.replace(shown_first, "", 1)
    assert table.restore("a.xml", answer) == original.replace(first, "", 1)


def test_CP10_X66_an_inserted_part_or_a_sibling_paste_is_still_a_change() -> None:
    """[CP10-X66] The inserted element is new: its regex placeholder copied after other code is refused. With a
    new element inserted, pasting the first sibling's regex into the second is still refused, and an edit of the
    untouched sibling after an insertion still restores."""
    first = '<set-variable variableName="first" value="#[vars.x matches /^a+$/]"/>'
    original = HEAD + first + ISV1 + TAIL
    table, shown = _show(original, NAMES)
    regexes = re.findall(r"matches /(«v\d+»)/", shown)
    assert len(regexes) == 2, shown
    copy = f'<set-variable variableName="tenant" value="#[vars.t matches /{regexes[1]}/]"/>'
    with pytest.raises(PlaceholderError, match="regular expression"):
        table.restore("a.xml", shown.replace("<set-variable", copy + "<set-variable", 1))
    new = '<set-variable variableName="tenant" value="#[attributes.headers.tenant]"/>'
    inserted = shown.replace("<set-variable", new + "<set-variable", 1)
    pasted = inserted.replace(
        f"attributes.requestPath matches /{regexes[1]}/", f"attributes.requestPath matches /{regexes[0]}/"
    )
    assert pasted != inserted
    with pytest.raises(PlaceholderError, match="regular expression"):
        table.restore("a.xml", pasted)
    edited = inserted.replace("attributes.requestPath matches", "attributes.maskedRequestPath matches")
    assert table.restore("a.xml", edited) == original.replace("<set-variable", new + "<set-variable", 1).replace(
        "attributes.requestPath matches", "attributes.maskedRequestPath matches"
    )


# ------------------------------------------------------------------------------------------------ X67: quote


def test_CP10_X67_a_changed_attribute_quote_keeps_each_unchanged_values_entity_spelling() -> None:
    """[CP10-X67] Only the quote character of the attribute changed: the unchanged value keeps its original bytes
    (``caf&#233; &amp; co``), and a value holding the new quote character escapes only that character."""
    original = HEAD + "<logger level=\"INFO\" message='caf&#233; &amp; co'/>" + TAIL
    table, shown = _show(original, NAMES)
    answer = shown.replace("message='", 'message="').replace("'/>", '"/>')
    assert answer != shown
    assert table.restore("a.xml", answer) == HEAD + '<logger level="INFO" message="caf&#233; &amp; co"/>' + TAIL
    quoted = HEAD + "<logger level=\"INFO\" message='say \"hi\" &#233;'/>" + TAIL
    table, shown = _show(quoted, NAMES)
    answer = shown.replace("message='", 'message="').replace("'/>", '"/>')
    assert table.restore("a.xml", answer) == HEAD + '<logger level="INFO" message="say &quot;hi&quot; &#233;"/>' + TAIL


# ------------------------------------------------------------------------------------------------ X64: open stretch


def test_CP10_X64_a_string_moved_into_another_string_where_a2m_cannot_read_is_never_code() -> None:
    """[CP10-X64] Code added later on the line of an unsure "/" makes the lexer give up on the rest of that line.
    A single-quoted string left as shown but now wrapped in a new double-quoted string there reads in another
    context, so it is not put back from its original bytes (which would end the new string and make
    ``vars.secret`` code); it is refused. With no change before it, the same addition still restores."""
    body = "{ avg: sum(payload.x) / sizeOf(payload.x) / 100, status: 'a\" ++ vars.secret ++ \"b' }"
    original = _script_file(body)
    table = Placeholders(["f", "JS-Clean"])
    shown = table.mule({"a.xml": original})["a.xml"]
    assert shown is not None
    found = re.search(r"status: '(«v\d+»)' }", shown)
    assert found is not None, shown
    token = found.group(1)
    wrapped = shown.replace(f"status: '{token}' }}", f"s: \"q {{ status: '{token}' }}\", half: vars.n / 2 }}")
    with pytest.raises(PlaceholderError) as caught:
        table.restore("a.xml", wrapped)
    assert token in str(caught.value)
    same = shown.replace(f"status: '{token}' }}", f"status: '{token}', half: vars.n / 2 }}")
    assert table.restore("a.xml", same) == original.replace("\"b' }", "\"b', half: vars.n / 2 }")
