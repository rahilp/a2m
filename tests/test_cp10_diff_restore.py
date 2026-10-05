"""CP10 adversarial round 9: one diff-based rule for writing an answer's placeholders back.

The answer is aligned, part by part, with the exact text a2m showed; a part's place includes each element's ordinal
among its siblings of the same name. Each placeholder whose shown stretch is unchanged at the same aligned position
is written back from the original bytes of that stretch, and is never read again or refused; only the placeholders
of changed stretches are read, and one copied or moved from elsewhere is a change where it now stands.

* CP10-X59 - two sibling elements of the same shape never stand for each other: a regular expression or a value
  holding "#[" pasted from one sibling into the other is refused, while an honest edit restores each sibling's own
  bytes.
* CP10-X60 - a stretch a2m could not read and showed as one placeholder (the reviewer's b6): a fix elsewhere in the
  script restores it byte for byte; quoted, moved or written after, it is refused with a reason that never says to
  quote it.
* CP10-X61 - a "/" written later on the line of a stretch a2m showed after an unsure "/" (the reviewer's b1): a
  placeholder there is refused with a reason that names the "/" and the line, never an unclosed string; the same
  line edited with no placeholder in the change, or the change on a line of its own, restores byte for byte.
* CP10-X62 - a bounded generator of unrelated edits to shown Mule parts (sibling scripts with regular expressions,
  unsure "/" stretches, a stretch a2m could not read, numbers, string literals, an expression attribute, a plain
  value holding "#[" and an attribute with a character reference): every unchanged stretch comes back byte for byte.
* CP10-X63 - the end of a Mule expression is a public, documented function the fix loop imports (no private name).
"""

from __future__ import annotations

import inspect
import random
import re

import pytest

from a2m.ai import placeholders as placeholders_module
from a2m.ai.placeholders import PlaceholderError, Placeholders

CORE = "http://www.mulesoft.org/schema/mule/core"
EE = "http://www.mulesoft.org/schema/mule/ee/core"
DOC = "http://www.mulesoft.org/schema/mule/documentation"
TOKEN_RE = re.compile(r"«[vn]\d+»")


def _variables(*scripts: tuple[str, str]) -> str:
    """A Mule file whose Transform Message sets one variable per (name, DataWeave body) in sibling scripts."""
    parts = "".join(
        f'<ee:set-variable variableName="{name}"><![CDATA[%dw 2.0\noutput application/json\n---\n{body}]]>'
        "</ee:set-variable>"
        for name, body in scripts
    )
    return (
        f'<mule xmlns="{CORE}" xmlns:ee="{EE}"><flow name="f"><ee:transform><ee:variables>{parts}'
        "</ee:variables></ee:transform></flow></mule>"
    )


def _payload_script(body: str) -> str:
    return (
        f'<mule xmlns="{CORE}" xmlns:ee="{EE}" xmlns:doc="{DOC}">\n<flow name="f">\n'
        '<ee:transform doc:name="JS-Clean"><ee:message><ee:set-payload><![CDATA[%dw 2.0\noutput application/json\n'
        f"---\n{body}]]></ee:set-payload></ee:message></ee:transform>\n</flow></mule>"
    )


def _status_tokens(table: Placeholders) -> tuple[str, str]:
    """The diff's expected ('done') and actual ('ok') placeholders."""
    diff = table.diff("body $.status: expected 'done', actual 'ok'")
    found = re.search(r"expected '(«v\d+»)', actual '(«v\d+»)'", diff)
    assert found is not None, diff
    return found.group(1), found.group(2)


# ------------------------------------------------------------------------------------------------ X59: siblings


def test_CP10_X59_a_regex_pasted_between_sibling_scripts_of_the_same_shape_is_refused() -> None:
    """[CP10-X59] The reviewer's repro: two sibling ``<ee:set-variable>`` scripts differ only in ``variableName``;
    writing customerId's regular expression placeholder into orderId's script is refused (it restored silently, so
    orderId was validated against customerId's pattern). An honest edit of each script restores its own regex."""
    original = _variables(
        ("customerId", "payload.id matches /^A[0-9]+$/"), ("orderId", "payload.order matches /^B[0-9]+$/")
    )
    table = Placeholders()
    shown = table.mule({"app.xml": original})["app.xml"]
    assert shown is not None
    assert table.restore("app.xml", shown) == original
    first, second = re.findall(r"matches /(«v\d+»)/", shown)
    assert first != second

    pasted = shown.replace(f"payload.order matches /{second}/", f"payload.order matches /{first}/")
    with pytest.raises(PlaceholderError, match="regular expression"):
        table.restore("app.xml", pasted)

    honest = shown.replace("payload.id matches", "payload.customer.id matches").replace(
        "payload.order matches", "payload.order.id matches"
    )
    assert table.restore("app.xml", honest) == original.replace("payload.id matches", "payload.customer.id matches")\
        .replace("payload.order matches", "payload.order.id matches")


def test_CP10_X59_a_hash_value_pasted_between_sibling_loggers_is_refused() -> None:
    """[CP10-X59] The reviewer's second repro: a value a2m showed with "#[" in one ``<logger message>`` restores
    edited in that logger and is refused in its sibling logger (same element path and attribute)."""
    original = (
        f'<mule xmlns="{CORE}"><flow name="f"><logger message="see #[ docs"/><logger message="plain words"/>'
        "</flow></mule>"
    )
    table = Placeholders(["f"])
    shown = table.mule({"a.xml": original})["a.xml"]
    assert shown is not None
    first, second = re.findall(r'message="(«v\d+»)"', shown)
    with pytest.raises(PlaceholderError, match="Mule expression"):
        table.restore("a.xml", shown.replace(f'message="{second}"', f'message="{first}"'))
    edited = shown.replace(f'message="{first}"', f'message="{first} now"')
    assert table.restore("a.xml", edited) == original.replace("see #[ docs", "see #[ docs now")


def test_CP10_X59_identical_sibling_scripts_each_restore_their_own_bytes() -> None:
    """[CP10-X59] Two siblings shown identically but written differently in the source (``'it\\'s'`` and ``"it's"``
    hide as the same value) restore each its own spelling when the answer edits both; swapping them is a change, so
    each placeholder is read again and spelled for its new string (never the other sibling's bytes)."""
    original = _variables(("a", "{ s: 'it\\'s', k: payload.id matches /^A$/ }"),
                          ("b", "{ s: 'it\\'s', k: payload.id matches /^B$/ }"))
    table = Placeholders()
    shown = table.mule({"app.xml": original})["app.xml"]
    assert shown is not None
    first, second = re.findall(r"matches /(«v\d+»)/", shown)
    edited = shown.replace(" }]]>", ", z: 1 }]]>")
    assert table.restore("app.xml", edited) == original.replace(" }]]>", ", z: 1 }]]>")
    swapped = shown.replace(f"/{first}/", "/\x00/").replace(f"/{second}/", f"/{first}/").replace("/\x00/", f"/{second}/")
    with pytest.raises(PlaceholderError, match="regular expression"):
        table.restore("app.xml", swapped)


# ------------------------------------------------------------------------------------------------ X60: b6


B6_BODIES = (
    "{ status: 'ok',\n  avg: sum(payload.x) / sizeOf(payload.x), self: 'https://api.example.com/orders' }",
    "{ status: 'ok',\n  type: if ((payload.ct default '') contains /json/) 'application/json' else 'text/plain' }",
)


@pytest.mark.parametrize("body", B6_BODIES)
def test_CP10_X60_a_fix_elsewhere_in_a_script_restores_a_stretch_shown_whole(body: str) -> None:
    """[CP10-X60] The reviewer's b6: the rest of the line after an unsure "/" with a quote after it is shown as one
    placeholder. A fix of ``status`` on an earlier line restores the file byte for byte (it was refused, telling the
    AI to quote the placeholder)."""
    original = _payload_script(body)
    table = Placeholders(["f", "JS-Clean"])
    shown = table.mule({"a.xml": original})["a.xml"]
    assert shown is not None
    expected, actual = _status_tokens(table)
    answer = shown.replace(f"status: '{actual}'", f"status: '{expected}'")
    assert answer != shown
    assert table.restore("a.xml", answer) == original.replace("status: 'ok'", "status: 'done'")


@pytest.mark.parametrize("body", B6_BODIES)
def test_CP10_X60_the_whole_stretch_placeholder_quoted_moved_or_followed_is_refused(body: str) -> None:
    """[CP10-X60] Quoting that placeholder (what the old reason advised; it turned the code into a string that no
    longer compiles), moving it to another line, or writing code after it is refused, and the reason says to leave
    it exactly as shown, never to put it in quotes."""
    original = _payload_script(body)
    table = Placeholders(["f", "JS-Clean"])
    shown = table.mule({"a.xml": original})["a.xml"]
    assert shown is not None
    expected, actual = _status_tokens(table)
    answer = shown.replace(f"status: '{actual}'", f"status: '{expected}'")
    found = re.search(r"(«v\d+»)\]\]>", answer)
    assert found is not None, answer
    tail = found.group(1)
    quoted = answer.replace(f"{tail}]]>", f"'{tail}']]>")
    moved = answer.replace(f"{tail}]]>", "1 }]]>").replace("{ status:", f"{{ t: {tail},\n  status:")
    followed = answer.replace(f"{tail}]]>", f"{tail} ++ 'x'\n]]>")
    for refused in (quoted, moved, followed):
        assert refused != answer
        with pytest.raises(PlaceholderError) as caught:
            table.restore("a.xml", refused)
        reason = str(caught.value)
        assert tail in reason and "exactly as shown" in reason, reason
        assert "inside the quotes" not in reason, reason


# ------------------------------------------------------------------------------------------------ X61: b1


B1_DIVISION = "{ avg: sum(payload.x) / sizeOf(payload.x) / 100, status: 'ok' }"
B1_REPLACE = "{ phone: (payload.phone default '') replace /[^0-9]/ with '', status: 'ok' }"


@pytest.mark.parametrize(
    ("body", "added"),
    [(B1_DIVISION, ", half: vars.n / 2"), (B1_DIVISION, ", path: 'a/b'"), (B1_REPLACE, ", half: vars.n / 2")],
)
def test_CP10_X61_a_placeholder_after_a_slash_added_on_the_line_gets_an_accurate_refusal(body: str, added: str) -> None:
    """[CP10-X61] The reviewer's b1 cases 1, 2 and 4: the status fix with a "/" written later on the same line. The
    status placeholder now stands where a2m cannot tell what it would be in, so it is refused, and the reason names
    the "/" and says to write the code on a line of its own (it said a string literal was never closed)."""
    original = _payload_script(body)
    table = Placeholders(["f", "JS-Clean"])
    shown = table.mule({"a.xml": original})["a.xml"]
    assert shown is not None
    expected, actual = _status_tokens(table)
    answer = shown.replace(f"status: '{actual}' }}", f"status: '{expected}'{added} }}")
    assert answer != shown
    with pytest.raises(PlaceholderError) as caught:
        table.restore("a.xml", answer)
    reason = str(caught.value)
    assert expected in reason and '"/"' in reason and "line of its own" in reason, reason
    assert "never closed" not in reason, reason


@pytest.mark.parametrize("body", [B1_DIVISION, B1_REPLACE])
def test_CP10_X61_the_same_line_edit_restores_when_the_change_holds_no_placeholder(body: str) -> None:
    """[CP10-X61] A "/" added later on that line with no placeholder in the change leaves every stretch a2m showed
    unchanged, so the line restores byte for byte; the status fix with the addition on a line of its own does too."""
    original = _payload_script(body)
    table = Placeholders(["f", "JS-Clean"])
    shown = table.mule({"a.xml": original})["a.xml"]
    assert shown is not None
    same_line = shown.replace(" }]]>", ", half: vars.n / 2 }]]>")
    assert table.restore("a.xml", same_line) == original.replace(" }]]>", ", half: vars.n / 2 }]]>")
    expected, actual = _status_tokens(table)
    own_line = shown.replace(f"status: '{actual}' }}", f"status: '{expected}',\n  half: vars.n / 2 }}")
    assert table.restore("a.xml", own_line) == original.replace(
        "status: 'ok' }", "status: 'done',\n  half: vars.n / 2 }"
    )


# ------------------------------------------------------------------------------------------------ X62: generator


def _generated_file() -> str:
    """A Mule file, one part per line where a part is an attribute or a script line, whose hidden stretches never
    span a line: three sibling scripts of the same shape (a sure regex, an unsure division chain, an unsure replace,
    a number, a string literal and, last, a stretch a2m could not read), an expression attribute with a division
    chain, a plain value holding "#[" and an expression attribute holding a character reference."""
    scripts = []
    for index, letter in enumerate("ABC"):
        scripts.append(
            f'<ee:set-variable variableName="v{index}"><![CDATA[%dw 2.0\noutput application/json\n---\n{{\n'
            f"  id: payload.id matches /^{letter}[0-9]+$/,\n"
            f"  avg: sum(payload.x) / sizeOf(payload.x) / {100 + index},\n"
            f"  phone: (payload.phone default '') replace /[^0-{index}]/ with '',\n"
            f"  n: vars.count + {40 + index},\n"
            f"  name: 'O\\'Reilly {letter}',\n"
            f"  tail: sum(payload.x) / sizeOf(payload.x), self: 'https://api.example.com/{letter}' }}]]>"
            "</ee:set-variable>"
        )
    return "\n".join(
        [
            f'<mule xmlns="{CORE}" xmlns:ee="{EE}">',
            '<flow name="f">',
            "<ee:transform><ee:variables>",
            *scripts,
            "</ee:variables></ee:transform>",
            '<set-variable variableName="avg" value="#[sum(payload.x) / sizeOf(payload.x) / 100]"/>',
            '<logger level="INFO" message="see #[ docs"/>',
            '<set-variable variableName="lt" value="#[vars.a ++ \'x&#60;y\']"/>',
            "</flow>",
            "</mule>",
        ]
    )


def _edits(rng: random.Random, lines: list[str], label: tuple[str, str]) -> list[tuple[int, str, str, str]]:
    """Random unrelated edits as (line, kind, shown text, original text): a new line inserted after a script line
    (code only, or a key holding the diff's placeholder), an operator changed, or text appended to an attribute."""
    body = [index for index, line in enumerate(lines) if line.startswith(("  id:", "  avg:", "  phone:", "  n:"))]
    edits: list[tuple[int, str, str, str]] = []
    for _ in range(rng.randint(1, 4)):
        choice = rng.randrange(6)
        if choice == 0:
            at = rng.choice(body)
            edits.append((at, "insert", f"  extra{at}: vars.e{at},", f"  extra{at}: vars.e{at},"))
        elif choice == 1:
            at = rng.choice(body)
            edits.append((at, "insert", f"  label{at}: '{label[0]}',", f"  label{at}: '{label[1]}',"))
        elif choice == 2:
            at = rng.choice([index for index in body if lines[index].startswith("  n:")])
            edits.append((at, "replace", "vars.count +", "vars.count -"))
        else:
            needle = ('value="#[sum', 'message="', 'value="#[vars.a')[choice - 3]
            at = next(index for index, line in enumerate(lines) if needle in line)
            suffix = (" default 0", " now", " default ''")[choice - 3]
            edits.append((at, "append", suffix, suffix))
    return edits


def _apply(lines: list[str], edits: list[tuple[int, str, str, str]], side: int) -> str:
    out = list(lines)
    inserted: dict[int, list[str]] = {}
    for at, kind, shown, original in edits:
        text = (shown, original)[side]
        if kind == "insert":
            inserted.setdefault(at, []).append(text)
        elif kind == "replace":
            out[at] = out[at].replace(shown, original)  # the same code change on both sides
        elif kind == "append" and text not in out[at]:
            head, tail = out[at].rsplit('"/>', 1)
            out[at] = head + text + '"/>' + tail
    for at in sorted(inserted, reverse=True):
        out[at + 1 : at + 1] = inserted[at]
    return "\n".join(out)


def test_CP10_X62_random_unrelated_edits_leave_every_unchanged_stretch_byte_exact() -> None:
    """[CP10-X62] A seeded, bounded generator (80 answers): each applies 1 to 4 random edits that are unrelated to
    the hidden stretches to the file a2m showed, and the same edits to the original. The restored file is the edited
    original byte for byte: every regular expression, unsure "/" stretch, stretch shown whole, number, string literal
    (``'O\\'Reilly'`` as spelled), the value holding "#[" and the attribute's ``&#60;`` come back as they were, each
    from its own sibling."""
    original = _generated_file()
    table = Placeholders(["f", "INFO", "avg", "lt", "v0", "v1", "v2"])
    shown = table.mule({"gen.xml": original})["gen.xml"]
    assert shown is not None
    assert "sizeOf" not in shown and "^A" not in shown and "Reilly" not in shown and "#60" not in shown, shown
    assert table.restore("gen.xml", shown) == original
    expected, _ = _status_tokens(table)
    shown_lines, original_lines = shown.split("\n"), original.split("\n")
    assert len(shown_lines) == len(original_lines)
    rng = random.Random(20261005)
    for trial in range(80):
        edits = _edits(rng, original_lines, (expected, "done"))
        if trial == 0:
            edits.append((original_lines.index("  n: vars.count + 40,"), "replace", "vars.count +", "vars.count -"))
        answer = _apply(shown_lines, edits, 0)
        assert answer != shown
        assert table.restore("gen.xml", answer) == _apply(original_lines, edits, 1), (trial, edits)


# ------------------------------------------------------------------------------------------------ X63: public name


def test_CP10_X63_the_end_of_a_mule_expression_is_a_public_documented_function() -> None:
    """[CP10-X63] The fix loop finds the end of a ``#[...]`` with :func:`a2m.ai.placeholders.expression_end`, a
    public, documented function, never a private name of another module."""
    from a2m.ai.placeholders import expression_end
    from a2m.verify import fix_loop

    assert "expression_end" in placeholders_module.__all__
    assert expression_end.__doc__ and "#[" in expression_end.__doc__
    text = "#[vars.a ++ ']' ++ (payload matches /]/)]"
    assert expression_end(text, 2) == len(text) - 1
    assert expression_end("#[vars.a", 2) == -1
    assert "_expression_end" not in inspect.getsource(fix_loop)
