"""Placeholders: every literal value the AI fix loop shows the AI, replaced by a stable stand-in it can write back.

A fix request (:mod:`a2m.verify.fix_loop`) shows the AI Apigee policies, custom
code, the app's Mule configuration files and the diffs of the failing tests.
None of that material is searched for secrets. Instead, one
:class:`Placeholders` table per request replaces **every** literal value in it
with a placeholder such as ``«v1»``, ``«v2»``: the same value gets the same
placeholder everywhere in the request (policies, Mule files, code, diffs),
different values get different ones. A value is hidden unless it is on this
closed list of what is shown (default deny):

* element and attribute names, namespace declarations and schema locations;
* the names an Apigee policy or endpoint declares at the schema positions of
  names, and only there (:data:`NAME_TEXT_SLOTS`, :data:`NAME_ATTRIBUTE_SLOTS`:
  the root's ``name``, ``Step/Name``, ``AssignVariable/Name``,
  ``Header/@name``, ``QueryParam/@name``, ``FormParam/@name``,
  ``Variable/@name`` ...): policy, step, flow, endpoint, variable, header, query
  and form parameter names. An element or attribute called ``Name``, ``name``,
  ``type`` or ``ref`` anywhere else is not a name. Data (a ``<Payload>``,
  ``<Value>``, ``<Template>``, ``<InitialEntries>``, a ``<Source>`` holding
  code, whatever is nested in them, and every document embedded in a value) has
  no names: every value in it is a placeholder, even a number or a boolean;
* in a Mule file, a name attribute (``name``, ``config-ref``, ``doc:name``,
  ``variableName`` ...), a name position itself, only when its value is one of
  the **known names**: the names given with :meth:`Placeholders.know` (the
  proxy's policies, steps and endpoints, from the IR), the names the Apigee XML
  declares at its schema positions of names
  (:meth:`Placeholders.learn_apigee`) and the names of the Mule files as a2m
  generated them (:meth:`Placeholders.learn_mule`). A known name is shown only
  at such a position: a string literal, an element text or any other value is
  never shown because its value equals a known name;
* the code of DataWeave, JavaScript, Python, Java and JSON around their string
  literals, as far as the lexer classifies it for sure: unquoted identifiers,
  field selectors (``vars.x``, ``attributes.headers.x``), function names,
  keywords, operators, numbers, ``{variable}`` references and ``${property}``
  placeholders. Every string literal (a quoted selector such as ``vars['x']``
  included), template literal (with every ``${...}`` part), regular expression
  and comment is a placeholder, and so is everything at a point the lexer
  cannot read for sure: a ``/`` that may start a regular expression hides every
  reading unless it is provably a division, and a DataWeave ``output`` or
  ``input`` word is a directive only in a script's header (or first in a Mule
  expression with ``---``), never as a field selector or variable;
* an object key (a quoted string right after ``{`` or ``,`` inside braces and
  before ``:``; never in Java, never after ``case``);
* numbers, booleans, HTTP methods, MIME types and Apigee rates (``10pm``);
* the variables and operators of an Apigee condition;
* in a URL, each query parameter name (written ``name=``);
* the words of a test diff (``header X-Served-By: expected ..., actual ...``).

Everything else is a placeholder: every other quoted string literal of code,
expressions and JSON (in any position: a list, a selector, a call argument),
XML element text and CDATA (embedded XML, JSON and form text are walked the
same way), attribute values, comments in code, URL parts (the address, each
query value, each query item with no ``=``, the fragment) and every quoted
value of a diff. Text that holds the placeholder mark ``«`` is never shown as
it is. A comment of a Mule file is shown as it is, but every value the table
knows by then (from the policies, the endpoints, the code, the diffs and the
Mule files) is replaced in it, because a2m writes conditions and original
settings into such comments.

A placeholder stands for a value as it reads, not as it is spelled: the string
literal ``'O\\'Reilly'`` and the diff value ``"O'Reilly"`` get the same
placeholder. A literal whose value a2m cannot read for sure (a DataWeave string
with ``$(...)``, a Python ``r''`` string, a JavaScript template with ``${}``)
gets a placeholder of its own, which can only be written back into a string of
the same language and quotes.

An answer comes back through :meth:`Placeholders.restore`: each placeholder is
written back for where it stands. In an attribute, element text or CDATA it is
the value, XML-escaped for that place; in a string literal of a DataWeave
expression or script, or of JSON text, it is the value spelled for that
language and quote (as the source spelled it, when the source had it in such a
string), then XML-escaped. A part the AI echoed exactly as it was shown gets
back its original bytes, so an echoed file is byte for byte the file a2m
showed. Refused, with the reason: an unknown placeholder, a placeholder outside
any value (in a tag), in a comment the AI wrote itself, outside any string
literal of an expression, in a string it cannot be spelled in, or in a CDATA
section, comment or regular expression that cannot hold its value.
"""

from __future__ import annotations

import ast
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from defusedxml import DefusedXmlException
from defusedxml import ElementTree as SafeET

from a2m.ai.provider import ItemKind
from a2m.conditions.lexer import ConditionError, Token, TokenKind, tokenize
from a2m.verify.model import fold

# A placeholder as shown to the AI.
TOKEN = re.compile(r"«v(\d{1,7})»")
TOKEN_FORMAT = "«v{number}»"
# Splits a text into the pieces between placeholders and the placeholders themselves (odd indexes).
TOKEN_SPLIT = re.compile(r"(«v\d{1,7}»)")
# Text holding this character is never shown as it is (it could be mistaken for a placeholder).
TOKEN_MARK = "«"

# Languages of the code lexer: the custom code kinds are a2m.ai.provider.ItemKind's own values.
DATAWEAVE = "dataweave"
JAVASCRIPT = ItemKind.JAVASCRIPT.value
PYTHON = ItemKind.PYTHON.value
JAVA = ItemKind.JAVA.value
JSON_TEXT = "json"
CODE_KINDS: frozenset[ItemKind] = frozenset({ItemKind.JAVASCRIPT, ItemKind.PYTHON, ItemKind.JAVA})

# XML parts.
_MARKUP = "markup"
_ATTRIBUTE = "attribute"
_TEXT = "text"
_CDATA = "cdata"
_COMMENT = "comment"

# Code segments (see _segments).
_S_TEXT = "text"
_S_WORD = "word"
_S_STRING = "string"
_S_OPEN = "open"
_S_LINE = "line"
_S_BLOCK = "block"
_S_REGEX = "regex"
_S_REF = "ref"
_S_TOKEN = "token"
_S_MARK = "mark"

# How deep embedded documents (XML in a Payload holding JSON ...) are walked; deeper text is one placeholder.
MAX_DEPTH = 6
# The comment sweep replaces only values this long at least (a shorter one cannot carry a secret).
MIN_SWEPT_CHARS = 3
MAX_NAME_CHARS = 128

# Attributes of a Mule file whose value is a name (local names; ``doc:name`` is ``name``): shown only when the value
# is a known name.
NAME_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "name", "ref", "config-ref", "variableName", "target", "type", "level", "protocol", "objectStore",
        "counterVariableName", "rootMessageVariableName", "entryTtlUnit", "file", "mapIdentifier", "assignTo",
        "variable", "transport", "action", "noRuleMatchAction", "doc:name",
    }
)
# Apigee names are positional: a value is a name only where the policy or endpoint schema puts one, never because an
# element or attribute anywhere is called Name, name, type or ref. ROOT stands for the policy's or endpoint's root
# element (as an element's parent: a child of the root).
ROOT = ""
# Schema positions whose text is a name or a fixed word: element -> the parents it is a name under.
NAME_TEXT_SLOTS: Mapping[str, frozenset[str]] = {
    "Name": frozenset({"Step", "AssignVariable"}),
    "Ref": frozenset({"AssignVariable"}),
    "AssignTo": frozenset({ROOT}),
    "Source": frozenset({ROOT}),
    "VariablePrefix": frozenset({ROOT}),
    "OutputVariable": frozenset({ROOT}),
    "Response": frozenset({ROOT}),
    "Operation": frozenset({ROOT}),
    "TimeUnit": frozenset({ROOT}),
    "Scope": frozenset({ROOT}),
    "TargetEndpoint": frozenset({"RouteRule", "TargetEndpoints"}),
    "ProxyEndpoint": frozenset({"ProxyEndpoints", "LocalTargetConnection"}),
    "Policy": frozenset({"Policies"}),
}
# Schema positions whose attribute is a name or a fixed word: element (ROOT: the root element) -> those attributes.
NAME_ATTRIBUTE_SLOTS: Mapping[str, frozenset[str]] = {
    ROOT: frozenset({"name", "mapIdentifier"}),
    "Header": frozenset({"name"}),
    "QueryParam": frozenset({"name"}),
    "FormParam": frozenset({"name"}),
    "Variable": frozenset({"name", "type"}),
    "Property": frozenset({"name"}),
    "Flow": frozenset({"name"}),
    "PreFlow": frozenset({"name"}),
    "PostFlow": frozenset({"name"}),
    "PostClientFlow": frozenset({"name"}),
    "FaultRule": frozenset({"name"}),
    "DefaultFaultRule": frozenset({"name"}),
    "RouteRule": frozenset({"name"}),
    "Server": frozenset({"name"}),
    "AssignTo": frozenset({"type", "transport"}),
    "Copy": frozenset({"source"}),
    "Request": frozenset({"variable"}),
    "Get": frozenset({"assignTo"}),
    "Parameter": frozenset({"ref"}),
    "IPRules": frozenset({"noRuleMatchAction"}),
    "MatchRule": frozenset({"action"}),
    "Allow": frozenset({"countRef"}),
    "Interval": frozenset({"ref"}),
    "TimeUnit": frozenset({"ref"}),
    "Identifier": frozenset({"ref"}),
    "MessageWeight": frozenset({"ref"}),
    "Rate": frozenset({"ref"}),
    "APIKey": frozenset({"ref"}),
    "User": frozenset({"ref"}),
    "Password": frozenset({"ref"}),
}
# Policies whose <Source> names the message they read (elsewhere, as in a JavaScript policy, it is inline code).
SOURCE_ROOTS: frozenset[str] = frozenset(
    {
        "ExtractVariables", "BasicAuthentication", "XMLToJSON", "JSONToXML", "MessageValidation", "XSL",
        "JSONThreatProtection", "XMLThreatProtection", "RegularExpressionProtection", "OASValidation",
        "SOAPMessageValidation",
    }
)
# Apigee elements whose content is data (a payload, a literal value, a template, entries, inline code): every value
# under them, at any depth and in any document embedded there, is a placeholder and is never a name.
DATA_ELEMENTS: frozenset[str] = frozenset({"Payload", "Value", "InitialEntries", "Template"})
HTTP_METHODS: frozenset[str] = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE"})
BOOLEANS: frozenset[str] = frozenset({"true", "false"})
# JavaScript reserved and contextual words: a "/" after one of them (or after the ")" of a "(" that follows one, as
# in ``if (x) /re/``) may start a regular expression. After any other identifier a "/" is a division.
_JS_KEYWORDS: frozenset[str] = frozenset(
    {
        "await", "break", "case", "catch", "class", "const", "continue", "debugger", "default", "delete", "do",
        "else", "enum", "export", "extends", "false", "finally", "for", "function", "if", "implements", "import",
        "in", "instanceof", "interface", "let", "new", "null", "of", "package", "private", "protected", "public",
        "return", "static", "super", "switch", "this", "throw", "true", "try", "typeof", "var", "void", "while",
        "with", "yield", "async", "get", "set", "from", "as",
    }
)
# JavaScript keywords after which a "/" can only start a regular expression (they cannot end an operand).
_JS_OPERATOR_WORDS: frozenset[str] = frozenset(
    {"return", "typeof", "case", "do", "else", "in", "instanceof", "new", "delete", "void", "throw", "extends"}
)
# A DataWeave output or input directive, read only where it is one for sure (see :func:`_dw_directive`): the
# directive word, an input's name, a MIME type ``type/subtype`` (group "mime", its "/" is not a regular expression),
# then only ``; name=value`` parameters and ``name=value`` writer options, to the end of the line or ``---``. A
# statement in a directive position that is not in this grammar is hidden from its word on (see :func:`_segments`).
_DW_DIRECTIVE_WORDS: frozenset[str] = frozenset({"output", "input"})
_DW_DIRECTIVE = (
    r"(?:(?P<word>output)|(?P<iword>input)(?P<igap>[ \t]+)(?P<name>[A-Za-z_]\w*))(?P<gap>[ \t]+)"
    r"(?P<mime>[A-Za-z][\w.+\-]*/[A-Za-z0-9][\w.+\-]*|[A-Za-z]\w*)(?![\w.+\-/$])"
    r"(?:[ \t]*;[ \t]*[A-Za-z][\w\-]*[ \t]*=[ \t]*[\w.+\-]+)*"
    r"(?:(?:[ \t]*,[ \t]*|[ \t]+)[A-Za-z_]\w*[ \t]*=[ \t]*"
    r"(?:\"(?:[^\"\\$\r\n]|\\[^\r\n])*\"|'(?:[^'\\$\r\n]|\\[^\r\n])*'|[\w.+\-]+))*"
)
# What follows a directive: the end of its line, "---" or a comment (in a script); "---" (in a Mule expression).
_DW_SCRIPT_DIRECTIVE = re.compile(_DW_DIRECTIVE + r"(?=[ \t]*(?:\r?\n|---|//|/\*|\Z))")
_DW_EXPRESSION_DIRECTIVE = re.compile(_DW_DIRECTIVE + r"(?=\s*---)")
# Mule bindings of a DataWeave expression: always an operand, never an infix function.
_DW_BINDINGS: frozenset[str] = frozenset({"payload", "vars", "attributes", "message", "error", "correlationId"})
# Lexer states for the token before a "/" (they cannot be confused with a word).
_LAST_NUMBER = "\x00num"
_LAST_VALUE = "\x00value"
_LAST_UNSURE = "\x00unsure"  # a placeholder or a stray placeholder mark: what it stands for is not known
# How deep string interpolations (``${`a${`b`}`}``) are followed; deeper ones are read as never closed.
_MAX_NESTING = 16
# Apigee condition words that compare (lower case): a bare word after one is a value.
_COMPARE_WORDS: frozenset[str] = frozenset(
    {
        "equals", "notequals", "isnot", "is", "greaterthan", "greaterthanorequals", "lesserthan",
        "lesserthanorequals", "matches", "like", "javaregex", "matchespath", "startswith", "equalscaseinsensitive",
    }
)
_LOGIC = frozenset({"and", "or", "not", "&&", "||", "!"})

_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?")
_MIME = re.compile(r"(?i)[a-z]+/[a-z0-9][a-z0-9.+\-]*(?:\s*;\s*charset\s*=\s*[a-z0-9_\-]+)?")
_RATE = re.compile(r"\d+(?:ps|pm)")
_IDENTIFIER = re.compile(r"[\w$][\w.:$\-]*(?: [\w.:$\-]+)*")
_KEY_NAME = re.compile(r"[A-Za-z0-9_$@][\w.$@:\-]*")
_ENTITY = re.compile(r"&(#x[0-9A-Fa-f]{1,6}|#[0-9]{1,7}|amp|lt|gt|quot|apos);")
_NAMED_ENTITIES = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'"}
# Apigee message template references: {request.header.x}, {escapeJSON(x)}.
_APIGEE_REF = re.compile(r"\{[A-Za-z_][\w.\-:]{0,255}(?:\([^{}()\"'\r\n]{0,256}\))?\}")
# Mule property placeholders.
_PROPERTY = re.compile(r"\$\{[^{}\s«]{1,256}\}")
_FORM = re.compile(r"[\w.\-\[\]]{1,128}=[^&\s]*(?:&[\w.\-\[\]]{1,128}=[^&\s]*)+|[\w.\-\[\]]{1,128}=[^&=\s]+")
_URL_START = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]{0,31}://")
_WORD = re.compile(r"[\w$]+")
_CODE_NUMBER = re.compile(r"\d[\w.]*")
_HEX4 = re.compile(r"[0-9A-Fa-f]{4}")
_HEX2 = re.compile(r"[0-9A-Fa-f]{2}")
# A diff's unquoted request target (a2m.verify.compare): a path, or an address with a scheme.
_DIFF_TARGET = re.compile(r"(?:[A-Za-z][A-Za-z0-9+.\-]{0,31}://|/)[^\s,'\"]*")
_ATTRIBUTE_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;", "\r": "&#13;", "\n": "&#10;", "\t": "&#09;"}
_TEXT_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}
# Backslash escapes a string literal may use, and what each stands for.
_ESCAPES = {"\\": "\\", "'": "'", '"': '"', "`": "`", "/": "/", "n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}
_JSON_ESCAPES = frozenset('\\"/bfnrt')
_JSON_WORDS = frozenset({"true", "false", "null"})
_SPELLED = {"\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t", "\b": "\\b", "\f": "\\f"}


class PlaceholderError(ValueError):
    """An answer cannot be restored; the message says why (it follows "the file <name> in the fix")."""


class _LexError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _Part:
    """A piece of an XML text: markup kept as written, or a value (attribute value, text, CDATA, comment) as written,
    with the element it belongs to, the local names of that element and its ancestors from the root (``path``) and,
    for an attribute, its name and quote."""

    kind: str
    raw: str
    element: str = ""
    attribute: str = ""
    quote: str = ""
    path: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Seg:
    """A piece of code: a string literal (its quote and content, whether it is an object key and whether letters
    stand right before it, as in Python's ``r''``), a comment, a regular expression, a word, a placeholder, or other
    text kept as written."""

    kind: str
    raw: str
    quote: str = ""
    content: str = ""
    key: bool = False
    prefixed: bool = False
    closed: bool = True


def _local(name: str) -> str:
    return name.rpartition(":")[2]


def _lex(text: str) -> list[_Part]:
    """``text`` (XML) split into parts; raises :class:`_LexError` when it is not XML a2m can split (a DTD, an
    unclosed tag, quote, comment or CDATA section)."""
    parts: list[_Part] = []
    stack: list[str] = []
    index, size = 0, len(text)
    while index < size:
        start = text.find("<", index)
        if start < 0:
            start = size
        if start > index:
            parts.append(_Part(_TEXT, text[index:start], stack[-1] if stack else "", path=tuple(stack)))
            index = start
            continue
        if text.startswith("<!--", index):
            end = text.find("-->", index + 4)
            if end < 0:
                raise _LexError("an XML comment is never closed")
            parts += [_Part(_MARKUP, "<!--"), _Part(_COMMENT, text[index + 4 : end]), _Part(_MARKUP, "-->")]
            index = end + 3
        elif text.startswith("<![CDATA[", index):
            end = text.find("]]>", index + 9)
            if end < 0:
                raise _LexError("a CDATA section is never closed")
            element = stack[-1] if stack else ""
            cdata = _Part(_CDATA, text[index + 9 : end], element, path=tuple(stack))
            parts += [_Part(_MARKUP, "<![CDATA["), cdata, _Part(_MARKUP, "]]>")]
            index = end + 3
        elif text.startswith("<?", index):
            end = text.find("?>", index + 2)
            if end < 0:
                raise _LexError("a processing instruction is never closed")
            parts.append(_Part(_MARKUP, text[index : end + 2]))
            index = end + 2
        elif text.startswith("<!", index):
            raise _LexError("it holds a DTD or another declaration")
        elif text.startswith("</", index):
            end = text.find(">", index)
            if end < 0:
                raise _LexError("an end tag is never closed")
            parts.append(_Part(_MARKUP, text[index : end + 1]))
            if stack:
                stack.pop()
            index = end + 1
        else:
            index = _lex_start_tag(text, index, parts, stack)
    return parts


def _lex_start_tag(text: str, index: int, parts: list[_Part], stack: list[str]) -> int:
    size = len(text)
    end = index + 1
    while end < size and not text[end].isspace() and text[end] not in "/>":
        end += 1
    name = text[index + 1 : end]
    if not name:
        raise _LexError("a '<' does not start a tag")
    markup = [text[index:end]]
    position = end
    while True:
        blank = position
        while position < size and text[position].isspace():
            position += 1
        markup.append(text[blank:position])
        if position >= size:
            raise _LexError(f"the tag <{name}> is never closed")
        if text.startswith("/>", position):
            markup.append("/>")
            parts.append(_Part(_MARKUP, "".join(markup)))
            return position + 2
        if text[position] == ">":
            markup.append(">")
            parts.append(_Part(_MARKUP, "".join(markup)))
            stack.append(_local(name))
            return position + 1
        attribute_end = position
        while attribute_end < size and not text[attribute_end].isspace() and text[attribute_end] not in "=/>":
            attribute_end += 1
        attribute = text[position:attribute_end]
        if not attribute:
            raise _LexError(f"the tag <{name}> has a character a2m cannot read")
        cursor = attribute_end
        while cursor < size and text[cursor].isspace():
            cursor += 1
        if cursor >= size or text[cursor] != "=":
            raise _LexError(f"the attribute {attribute} of <{name}> has no value")
        cursor += 1
        while cursor < size and text[cursor].isspace():
            cursor += 1
        if cursor >= size or text[cursor] not in "\"'":
            raise _LexError(f"the value of the attribute {attribute} of <{name}> is not quoted")
        quote = text[cursor]
        close = text.find(quote, cursor + 1)
        if close < 0:
            raise _LexError(f"the value of the attribute {attribute} of <{name}> is never closed")
        markup.append(text[position : cursor + 1])
        parts.append(_Part(_MARKUP, "".join(markup)))
        parts.append(
            _Part(_ATTRIBUTE, text[cursor + 1 : close], _local(name), attribute, quote, (*stack, _local(name)))
        )
        markup = [quote]
        position = close + 1


def _unescape(raw: str) -> str:
    def entity(match: re.Match[str]) -> str:
        name = match.group(1)
        if name.startswith("#x"):
            return chr(int("0x" + name[2:], 0))
        if name.startswith("#"):
            return chr(int(name[1:]))
        return _NAMED_ENTITIES[name]

    return _ENTITY.sub(entity, raw)


def _escape_attribute(value: str, quote: str) -> str:
    out = "".join(_ATTRIBUTE_ESCAPES.get(char, char) for char in value)
    return out.replace(quote, "&quot;" if quote == '"' else "&apos;")


def _escape_text(value: str) -> str:
    return "".join(_TEXT_ESCAPES.get(char, char) for char in value)


def _readable_xml(text: str) -> bool:
    try:
        SafeET.fromstring(text, forbid_dtd=True)
    except (DefusedXmlException, ET.ParseError, ValueError):
        return False
    return True


def plain_visible(text: str) -> bool:
    """Whether ``text`` (one value) is shown as it is: a number, a boolean, an HTTP method (or a comma list of
    them), a MIME type or an Apigee rate."""
    core = text.strip()
    if not core or TOKEN_MARK in core:
        return not core
    if _NUMBER.fullmatch(core) or fold(core) in BOOLEANS or _RATE.fullmatch(core) or _MIME.fullmatch(core):
        return True
    return all(method.strip() in HTTP_METHODS for method in core.split(","))


def _name_like(text: str) -> bool:
    return len(text) <= MAX_NAME_CHARS and _IDENTIFIER.fullmatch(text) is not None and TOKEN_MARK not in text


def _in_data(path: Sequence[str]) -> bool:
    """Whether an element of an Apigee policy or endpoint with these ancestors (``path``, from the root) stands in
    data: one of them is a data element (:data:`DATA_ELEMENTS`), or a ``<Source>`` that does not name a message."""
    for index, element in enumerate(path):
        if element in DATA_ELEMENTS:
            return True
        if element == "Source" and not (index == 1 and path[0] in SOURCE_ROOTS):
            return True
    return False


def _attribute_slot(part: _Part) -> bool:
    """Whether the attribute ``part`` of an Apigee policy or endpoint stands where its schema puts a name."""
    path = part.path
    if not path or _in_data(path[:-1]):
        return False
    return part.attribute in NAME_ATTRIBUTE_SLOTS.get(ROOT if len(path) == 1 else path[-1], frozenset())


def _text_slot(path: Sequence[str]) -> bool:
    """Whether the text of the Apigee element at ``path`` (from the root) stands where its schema puts a name."""
    if len(path) < 2 or _in_data(path):
        return False
    return (ROOT if len(path) == 2 else path[-2]) in NAME_TEXT_SLOTS.get(path[-1], frozenset())


class Placeholders:
    """The placeholders of one fix request (see the module docstring)."""

    def __init__(self, names: Iterable[str] = ()) -> None:
        self._tokens: dict[str, str] = {}
        self._values: dict[str, str] = {}
        # Placeholders of string literals whose value a2m could not read: (language, quote) they were written in.
        self._opaque: dict[str, tuple[str, str]] = {}
        # (placeholder, language, quote) -> how the source spelled it in such a string literal, None when it spelled
        # it more than one way.
        self._spellings: dict[tuple[str, str, str], str | None] = {}
        # For each Mule file shown: (kind, quote, part as shown) -> the part as it was, None when ambiguous.
        self._echoes: dict[str, dict[tuple[str, str, str], str | None]] = {}
        self._names: set[str] = set()
        self._sweep: re.Pattern[str] | None = None
        self._sweep_forms: dict[str, str] = {}
        self._sweep_size = -1
        self.know(names)

    @property
    def values(self) -> Mapping[str, str]:
        """Each placeholder and the value it stands for."""
        return self._values

    def token(self, value: str) -> str:
        """The placeholder of ``value`` (a new one the first time it is seen)."""
        return self._token(value, value)

    def _token(self, key: str, value: str) -> str:
        found = self._tokens.get(key)
        if found is None:
            found = TOKEN_FORMAT.format(number=len(self._tokens) + 1)
            self._tokens[key] = found
            self._values[found] = value
        return found

    def _hide(self, text: str) -> str:
        """``text`` as one placeholder (its blanks at the ends kept); blank text stays."""
        core = text.strip()
        if not core:
            return text
        start = text.index(core)
        return text[:start] + self.token(core) + text[start + len(core) :]

    # ------------------------------------------------------------ known names

    def know(self, names: Iterable[str]) -> None:
        """Add ``names`` (identifiers a2m took from the IR: policy, step, endpoint and flow names) to the known
        names; one that is not written as a name is ignored."""
        for name in names:
            self._add_name(name)

    def _add_name(self, name: str) -> None:
        if _name_like(name):
            self._names.add(name)

    def learn_apigee(self, xml_text: str) -> None:
        """Learn the names an Apigee policy or endpoint declares at the schema positions of names (see
        :data:`NAME_TEXT_SLOTS` and :data:`NAME_ATTRIBUTE_SLOTS`). Nothing in data (a payload, a value, a template,
        entries) is learned. Nothing is shown; a known name is shown only in a Mule name attribute."""
        if not _readable_xml(xml_text):
            return
        try:
            parts = _lex(xml_text)
        except _LexError:
            return
        for part in parts:
            if part.kind == _ATTRIBUTE and _attribute_slot(part):
                self._add_name(_unescape(part.raw))
            elif part.kind == _TEXT and _text_slot(part.path):
                self._add_name(_unescape(part.raw).strip())

    def learn_mule(self, texts: Iterable[str]) -> None:
        """Learn the names of Mule files as a2m generated them (before any fix): their name attributes and their
        ``${property}`` names. Nothing is shown."""
        for text in texts:
            try:
                parts = _lex(text)
            except _LexError:
                continue
            for part in parts:
                if part.kind not in (_ATTRIBUTE, _TEXT, _CDATA):
                    continue
                value = part.raw if part.kind == _CDATA else _unescape(part.raw)
                if part.kind == _ATTRIBUTE and (
                    part.attribute in NAME_ATTRIBUTES or _local(part.attribute) in NAME_ATTRIBUTES
                ):
                    self._add_name(value)
                for match in _PROPERTY.finditer(value):
                    self._add_name(match.group(0)[2:-1])

    # ------------------------------------------------------------ documents

    def apigee(self, xml_text: str) -> str | None:
        """An Apigee policy's or endpoint's XML as the AI is shown it; None when it cannot be read as XML without
        DTDs or entities (then none of it may be shown)."""
        if not _readable_xml(xml_text):
            return None
        try:
            pieces = self._render(xml_text, apigee=True, depth=0, echoes=None)
        except _LexError:
            return None
        return "".join(piece for piece in pieces if isinstance(piece, str))

    def mule(self, files: Mapping[str, str]) -> dict[str, str | None]:
        """Each Mule configuration file (path: text) as the AI is shown it (None when it cannot be read as XML).
        Their comments are swept last, with every value the table knows by then."""
        rendered: dict[str, list[str | _Part] | None] = {}
        for name, text in files.items():
            echoes: dict[tuple[str, str, str], str | None] = {}
            if not _readable_xml(text):
                rendered[name] = None
                continue
            try:
                rendered[name] = self._render(text, apigee=False, depth=0, echoes=echoes)
            except _LexError:
                rendered[name] = None
                continue
            self._echoes[name] = echoes
        shown: dict[str, str | None] = {}
        for name, pieces in rendered.items():
            if pieces is None:
                shown[name] = None
                continue
            out: list[str] = []
            for piece in pieces:
                if isinstance(piece, str):
                    out.append(piece)
                    continue
                comment = self._hide(piece.raw) if TOKEN_MARK in piece.raw else self.sweep(piece.raw)
                if comment != piece.raw:
                    self._remember(self._echoes[name], (_COMMENT, "", comment), piece.raw)
                out.append(comment)
            shown[name] = "".join(out)
        return shown

    def code(self, text: str, kind: ItemKind) -> str:
        """Custom code (``kind``: :attr:`ItemKind.JAVASCRIPT`, :attr:`ItemKind.PYTHON` or :attr:`ItemKind.JAVA`)
        as the AI is shown it."""
        return self._code(text, kind.value if kind in CODE_KINDS else JAVASCRIPT)

    def condition(self, text: str) -> str:
        """An Apigee condition as the AI is shown it: variables and operators kept, every value a placeholder."""
        try:
            tokens = tokenize(text)
        except ConditionError:
            return self._hide(text)
        out: list[str] = []
        position = 0
        for index, token in enumerate(tokens):
            if token.kind is TokenKind.STRING:
                end = token.position + len(token.text) + 2
                out.append(text[position : token.position] + '"' + self._whole(token.text) + '"')
                position = end
            elif token.kind is TokenKind.WORD and (
                TOKEN_MARK in token.text or (_condition_value(tokens, index) and not plain_visible(token.text))
            ):
                out.append(text[position : token.position] + self.token(token.text))
                position = token.position + len(token.text)
        out.append(text[position:])
        return "".join(out)

    def diff(self, text: str) -> str:
        """Failing-test diff text as the AI is shown it: every quoted value and every request target a
        placeholder (a JSON key stays), a2m's words, header names, numbers and punctuation kept."""
        out: list[str] = []
        index, size = 0, len(text)
        last = ""
        while index < size:
            char = text[index]
            if char in "'\"" and (index == 0 or not text[index - 1].isalnum()):
                end = _string_end(text, index + 1, char)
                line_end = text.find("\n", index)
                if end < 0 or (0 <= line_end < end):
                    # Not closed on its line: the rest of the line is one placeholder.
                    stop = size if line_end < 0 else line_end
                    out.append(self._hide(text[index:stop]))
                    index = stop
                    continue
                literal = text[index : end + 1]
                after = end + 1
                content = text[index + 1 : end]
                if last in ("{", ",") and _next_char(text, after) == ":" and _KEY_NAME.fullmatch(content):
                    out.append(literal)
                else:
                    out.append(char + self._whole(_decoded(literal, content)) + char)
                index = after
                last = "str"
                continue
            target = _DIFF_TARGET.match(text, index) if char == "/" or char.isalpha() else None
            if target is not None and (char == "/" or "://" in target.group(0)) and (index == 0 or not text[index - 1].isalnum()):
                out.append(self._url(target.group(0)))
                index = target.end()
                last = "str"
                continue
            out.append(self.token(char) if char == TOKEN_MARK else char)
            if not char.isspace():
                last = char
            index += 1
        return "".join(out)

    def sweep(self, text: str) -> str:
        """``text`` (free text a2m wrote: a comment, a refusal reason) with every value the table knows (at least
        :data:`MIN_SWEPT_CHARS` long, also XML-escaped) replaced by its placeholder."""
        if not text:
            return text
        if self._sweep is None or self._sweep_size != len(self._values):
            variants: dict[str, str] = {}
            for token, value in self._values.items():
                if len(value.strip()) < MIN_SWEPT_CHARS:
                    continue
                for form in (value, _escape_text(value), _escape_attribute(value, '"')):
                    variants.setdefault(form, token)
            self._sweep_forms = variants
            if variants:
                ordered = sorted(variants, key=lambda v: (-len(v), v))
                alternatives = [
                    ("(?<![A-Za-z0-9])" if form[0].isalnum() else "")
                    + re.escape(form)
                    + ("(?![A-Za-z0-9])" if form[-1].isalnum() else "")
                    for form in ordered
                ]
                self._sweep = re.compile("|".join(alternatives))
            else:
                self._sweep = re.compile(r"(?!)")
            self._sweep_size = len(self._values)
        forms = self._sweep_forms
        return self._sweep.sub(lambda match: forms[match.group(0)], text)

    # ------------------------------------------------------------ restoring an answer

    def restore(self, name: str, answer: str) -> str:
        """``answer`` (the AI's text of the Mule file ``name``) with every placeholder written back for where it
        stands; raises :class:`PlaceholderError` (see the module docstring)."""
        if TOKEN.search(answer) is None:
            return answer
        unknown = sorted({match.group(0) for match in TOKEN.finditer(answer) if match.group(0) not in self._values})
        if unknown:
            raise PlaceholderError(
                f"uses the placeholder {', '.join(unknown[:5])}, which stands for no value a2m showed"
            )
        try:
            parts = _lex(answer)
        except _LexError as exc:
            raise PlaceholderError(f"is not well-formed XML ({exc}), so its placeholders cannot be put back") from None
        echoes = self._echoes.get(name, {})
        out: list[str] = []
        for part in parts:
            if TOKEN.search(part.raw) is None:
                out.append(part.raw)
                continue
            if part.kind == _MARKUP:
                raise PlaceholderError("has a placeholder outside any value (in a tag), where no value can stand")
            original = echoes.get((part.kind, part.quote, part.raw))
            if original is not None:
                out.append(original)
                continue
            if part.kind == _COMMENT:
                raise PlaceholderError("has a placeholder in a comment it did not copy from what a2m showed")
            out.append(_joined(part.raw, self._part_values(part, apigee=False, depth=0)))
        return "".join(out)

    def _part_values(self, part: _Part, *, apigee: bool, depth: int) -> list[str]:
        """What each placeholder in ``part`` (an attribute value, a text or a CDATA section with placeholders) is
        written back as, XML-escaped for the part, in order."""
        pieces = TOKEN_SPLIT.split(part.raw)
        if part.kind == _CDATA:
            logical = part.raw
        else:
            logical = "".join(piece if index % 2 else _unescape(piece) for index, piece in enumerate(pieces))
        values = self._put_value(logical, apigee=apigee, depth=depth)
        if len(values) != len(pieces) // 2:
            raise PlaceholderError("has a placeholder a2m cannot tell the place of, so it cannot be put back")
        if part.kind == _ATTRIBUTE:
            return [_escape_attribute(value, part.quote) for value in values]
        if part.kind == _CDATA:
            for token, value in zip(pieces[1::2], values, strict=True):
                if "]]>" in value:
                    raise PlaceholderError(f"puts {token} in a CDATA section, which cannot hold its value")
            return values
        return [_escape_text(value) for value in values]

    def _put_value(self, text: str, *, apigee: bool, depth: int) -> list[str]:
        """What each placeholder in ``text`` (one value, read as :meth:`_value` reads it) is written back as."""
        core = text.strip()
        if not apigee:
            if core.startswith("%dw"):
                return self._put_code(core, DATAWEAVE)
            if "#[" in core or "${" in core:
                return self._put_template(core)
        if depth < MAX_DEPTH and core:
            if core.startswith("<"):
                nested = self._put_xml(core, depth + 1)
                if nested is not None:
                    return nested
            if core[0] in "{[" and core[-1] in "}]" and _APIGEE_REF.fullmatch(core) is None:
                return self._put_code(core, JSON_TEXT)
        return [self._plain(match.group(0)) for match in TOKEN.finditer(core)]

    def _put_template(self, text: str) -> list[str]:
        values: list[str] = []
        position = index = 0
        while index < len(text):
            if text.startswith("#[", index):
                end = _expression_end(text, index + 2)
                if end < 0:
                    break
                values += [self._plain(match.group(0)) for match in TOKEN.finditer(text, position, index)]
                values += self._put_code(text[index + 2 : end], DATAWEAVE)
                index = position = end + 1
                continue
            index += 1
        values += [self._plain(match.group(0)) for match in TOKEN.finditer(text, position)]
        return values

    def _put_xml(self, text: str, depth: int) -> list[str] | None:
        try:
            parts = _lex(text)
        except _LexError:
            return None
        values: list[str] = []
        for part in parts:
            if TOKEN.search(part.raw) is None:
                continue
            if part.kind == _MARKUP:
                raise PlaceholderError("has a placeholder in a tag of embedded XML, where no value can stand")
            if part.kind == _COMMENT:
                for match in TOKEN.finditer(part.raw):
                    value = self._plain(match.group(0))
                    if "--" in value or value.endswith("-"):
                        raise PlaceholderError(f"puts {match.group(0)} in an XML comment, which cannot hold its value")
                    values.append(value)
                continue
            values += self._part_values(part, apigee=True, depth=depth)
        return values

    def _put_code(self, text: str, language: str) -> list[str]:
        """What each placeholder in code is written back as: spelled for its string literal, as it is in a
        comment, refused outside any string."""
        values: list[str] = []
        for seg in _segments(text, language):
            if seg.kind == _S_STRING:
                values += [self._in_literal(m.group(0), language, seg.quote) for m in TOKEN.finditer(seg.content)]
            elif seg.kind in (_S_LINE, _S_BLOCK):
                for match in TOKEN.finditer(seg.content):
                    value = self._plain(match.group(0))
                    if ("\n" in value or "\r" in value) if seg.kind == _S_LINE else "*/" in value:
                        raise PlaceholderError(f"puts {match.group(0)} in a code comment, which cannot hold its value")
                    values.append(value)
            elif seg.kind == _S_REGEX:
                for match in TOKEN.finditer(seg.content):
                    spelled = self._spellings.get((match.group(0), language, "/"))
                    value = spelled if spelled is not None else self._plain(match.group(0))
                    if spelled is None and ("/" in value or "\n" in value or "\r" in value):
                        raise PlaceholderError(
                            f"puts {match.group(0)} in a regular expression, which cannot hold its value"
                        )
                    values.append(value)
            elif seg.kind == _S_OPEN and TOKEN.search(seg.raw):
                raise PlaceholderError("has a placeholder in a string literal that is never closed")
            elif seg.kind == _S_TOKEN:
                values.append(self._bare(seg.raw, language))
        return values

    def _bare(self, token: str, language: str) -> str:
        value = self._plain(token)
        if value == TOKEN_MARK or (language == JSON_TEXT and (_WORD.fullmatch(value) or _NUMBER.fullmatch(value))):
            return value
        raise PlaceholderError(
            f"puts {token} outside any string literal of an expression; write it inside quotes, as '{token}'"
        )

    def _in_literal(self, token: str, language: str, quote: str) -> str:
        spelled = self._spellings.get((token, language, quote))
        if spelled is not None:
            return spelled
        if token in self._opaque:
            raise PlaceholderError(
                f"puts {token} in a string it cannot be written into as it was (it stands for a piece of code a2m "
                "could only show as one placeholder)"
            )
        encoded = _encode(self._values[token], language, quote)
        if encoded is None:
            raise PlaceholderError(f"puts {token} in a string literal quoted with {quote}, which a2m cannot spell it in")
        return encoded

    def _plain(self, token: str) -> str:
        if token in self._opaque:
            raise PlaceholderError(
                f"puts {token} outside the kind of string literal it came from, where a2m cannot write it back"
            )
        return self._values[token]

    # ------------------------------------------------------------ inside a document

    @staticmethod
    def _remember(echoes: dict[tuple[str, str, str], str | None], key: tuple[str, str, str], raw: str) -> None:
        if key in echoes and echoes[key] != raw:
            echoes[key] = None  # two parts look the same but were written differently: put back by value
        else:
            echoes[key] = raw

    def _render(
        self,
        text: str,
        *,
        apigee: bool,
        depth: int,
        echoes: dict[tuple[str, str, str], str | None] | None,
        data: bool = False,
    ) -> list[str | _Part]:
        """The pieces of ``text`` (XML) as shown; a Mule comment is left as a :class:`_Part` for the sweep. ``data``:
        the whole text is data (an embedded document); in an Apigee policy or endpoint (``apigee`` at depth 0) a part
        is data when an element above it is a data element (:func:`_in_data`)."""
        pieces: list[str | _Part] = []
        positional = apigee and depth == 0
        for part in _lex(text):
            if part.kind == _MARKUP:
                pieces.append(part.raw)
                continue
            if part.kind == _COMMENT:
                pieces.append(self._hide(part.raw) if apigee else part)
                continue
            if part.kind == _ATTRIBUTE:
                in_data = data or (positional and _in_data(part.path[:-1]))
                value = _unescape(part.raw)
                shown = self._attribute(value, part, apigee=apigee, depth=depth, data=in_data)
                raw = part.raw if shown == value else _escape_attribute(shown, part.quote)
            elif part.kind == _TEXT:
                in_data = data or (positional and _in_data(part.path))
                value = _unescape(part.raw)
                shown = self._text(value, part, apigee=apigee, depth=depth, data=in_data)
                raw = part.raw if shown == value else _escape_text(shown)
            else:  # CDATA
                in_data = data or (positional and _in_data(part.path))
                shown = self._text(part.raw, part, apigee=apigee, depth=depth, data=in_data)
                raw = shown
            if echoes is not None and raw != part.raw:
                self._remember(echoes, (part.kind, part.quote, raw), part.raw)
            pieces.append(raw)
        return pieces

    def _attribute(self, value: str, part: _Part, *, apigee: bool, depth: int, data: bool) -> str:
        name = part.attribute
        if name == "xmlns" or name.startswith("xmlns:") or _local(name) == "schemaLocation":
            return value if TOKEN_MARK not in value else self._hide(value)
        # An Apigee policy declares names at the schema positions of names, never in data; a Mule file shows only
        # the known names.
        if depth == 0 and not data:
            if apigee and _attribute_slot(part) and _name_like(value):
                return value
            if not apigee and (name in NAME_ATTRIBUTES or _local(name) in NAME_ATTRIBUTES) and value in self._names:
                return value
        return self._value(value, apigee=apigee, depth=depth, data=data)

    def _text(self, value: str, part: _Part, *, apigee: bool, depth: int, data: bool) -> str:
        core = value.strip()
        if not core:
            return value
        start = value.index(core)
        lead, trail = value[:start], value[start + len(core) :]
        if apigee and depth == 0 and not data:
            if part.element == "Condition":
                return lead + self.condition(core) + trail
            if part.kind == _TEXT and _text_slot(part.path) and _name_like(core):
                return value
        return lead + self._value(core, apigee=apigee, depth=depth, data=data) + trail

    def _value(self, text: str, *, apigee: bool, depth: int, data: bool = False) -> str:
        """One value (an attribute value, a text) as shown (see the module docstring); in data even a number, a
        boolean or a MIME type is a placeholder."""
        core = text.strip()
        if not core:
            return text
        start = text.index(core)
        lead, trail = text[:start], text[start + len(core) :]
        if TOKEN_MARK in core:
            return lead + self.token(core) + trail  # it could be read as a placeholder: shown as one
        if plain_visible(core) and not data:
            return text
        if not apigee:
            if core.startswith("%dw"):
                return lead + self._code(core, DATAWEAVE) + trail
            if "#[" in core or "${" in core:
                return lead + self._mule_template(core) + trail
        embedded = self._embedded(core, depth, data=data)
        if embedded is not None:
            return lead + embedded + trail
        if apigee:
            return lead + self._apigee_template(core, data=data) + trail
        return lead + self._literal(core) + trail

    def _embedded(self, core: str, depth: int, *, data: bool = False) -> str | None:
        """``core`` walked as the embedded document it holds (XML, JSON, form text), or None. An embedded XML
        document is data throughout: none of its values is a name."""
        if depth >= MAX_DEPTH:
            return None
        if core.startswith("<"):
            if not _readable_xml(core):
                return None
            try:
                pieces = self._render(core, apigee=True, depth=depth + 1, echoes=None, data=True)
            except _LexError:
                return None
            return "".join(piece for piece in pieces if isinstance(piece, str))
        if core[0] in "{[" and core[-1] in "}]" and _APIGEE_REF.fullmatch(core) is None:
            return self._code(core, JSON_TEXT, data=data)
        if _FORM.fullmatch(core) is not None:
            out = []
            for item in core.split("&"):
                key, _, value = item.partition("=")
                out.append(key + "=" + (self._apigee_template(value, data=data) if value else ""))
            return "&".join(out)
        return None

    def _apigee_template(self, text: str, *, data: bool = False) -> str:
        """Apigee message template text: each ``{reference}`` kept, each literal piece shown by :meth:`_literal`."""
        out: list[str] = []
        position = 0
        for match in _APIGEE_REF.finditer(text):
            out.append(self._literal(text[position : match.start()], data=data))
            out.append(match.group(0))
            position = match.end()
        out.append(self._literal(text[position:], data=data))
        return "".join(out)

    def _mule_template(self, text: str) -> str:
        """A Mule attribute value or text: each ``#[...]`` expression walked as DataWeave, each ``${property}``
        kept, each literal piece shown by :meth:`_literal`."""
        out: list[str] = []
        position = 0
        index = 0
        while index < len(text):
            if text.startswith("#[", index):
                end = _expression_end(text, index + 2)
                if end < 0:
                    break
                out.append(self._literal(text[position:index]))
                out.append("#[" + self._code(text[index + 2 : end], DATAWEAVE) + "]")
                index = position = end + 1
                continue
            match = _PROPERTY.match(text, index) if text.startswith("${", index) else None
            if match is not None:
                out.append(self._literal(text[position:index]))
                out.append(match.group(0))
                index = position = match.end()
                continue
            index += 1
        out.append(self._literal(text[position:]))
        return "".join(out)

    def _literal(self, text: str, *, data: bool = False) -> str:
        """A literal piece: shown when it is plainly visible (never in data), by parts when it is an address with a
        query, else one placeholder."""
        if not text.strip() or (plain_visible(text) and not data):
            return text
        core = text.strip()
        if "?" in core and (_URL_START.match(core) or core.startswith("/")):
            start = text.index(core)
            return text[:start] + self._url(core, data=data) + text[start + len(core) :]
        return self._hide(text)

    def _url(self, url: str, *, data: bool = False) -> str:
        """An address: everything before the query one placeholder; each query item written ``name=value`` shows
        its name (when it is written as one) and hides its value; a query item with no ``=`` is hidden whole; the
        fragment is a placeholder. In data a plainly visible value is hidden too."""
        before, question, query = url.partition("?")
        query, hash_mark, fragment = query.partition("#")
        out = [self._hide(before) if before else ""]
        if question:
            items = []
            for item in query.split("&"):
                key, equals, value = item.partition("=")
                if not equals:
                    items.append(self._hide(item))  # a bare token (a key, a signature): a value, never a name
                    continue
                shown_key = key if _KEY_NAME.fullmatch(key) else self._hide(key)
                visible = not value or (plain_visible(value) and not data) or _APIGEE_REF.fullmatch(value)
                shown_value = value if visible else self.token(value)
                items.append(shown_key + equals + shown_value)
            out.append("?" + "&".join(items))
        if hash_mark:
            out.append("#" + self._hide(fragment))
        return "".join(out)

    def _whole(self, content: str) -> str:
        """The content of a quoted literal as one placeholder, blanks included; blank content stays."""
        return content if not content.strip() else self.token(content)

    def _literal_token(self, content: str, language: str, quote: str, *, prefixed: bool) -> str:
        """The placeholder of a string literal of code: by its value, or one of its own when its value cannot be
        read for sure; how the source spelled it is kept for writing it back."""
        value = None if prefixed else _decode(content, language, quote)
        if value is None:
            token = self._token(f"\x00{language}\x00{quote}\x00{content}", content)
            self._opaque[token] = (language, quote)
        else:
            token = self.token(value)
        key = (token, language, quote)
        if key in self._spellings and self._spellings[key] != content:
            self._spellings[key] = None
        else:
            self._spellings[key] = content
        return token

    def _code(self, text: str, language: str, *, data: bool = False) -> str:
        """Code (``language``) with every string literal, regular expression and comment a placeholder, except a
        blank literal and an object key; a literal is never shown because its value equals a known name. In JSON text
        every bare word but true, false and null is a placeholder, and ``{reference}`` stays; in JSON that is data
        (a payload) every number, true and false is a placeholder too."""
        out: list[str] = []
        for seg in _segments(text, language):
            kind = seg.kind
            if kind == _S_STRING:
                if _shown_literal(seg):
                    out.append(seg.raw)
                else:
                    token = self._literal_token(seg.content, language, seg.quote, prefixed=seg.prefixed)
                    out.append(seg.quote + token + seg.quote)
            elif kind == _S_LINE:
                out.append(seg.quote + self._hide(seg.content))
            elif kind == _S_BLOCK:
                out.append("/*" + self._hide(seg.content) + ("*/" if seg.closed else ""))
            elif kind == _S_REGEX:
                hidden = self._hide(seg.content)
                if hidden != seg.content:
                    self._spellings.setdefault((hidden.strip(), language, "/"), seg.content.strip())
                out.append("/" + hidden + "/")
            elif kind == _S_OPEN:
                out.append(self._hide(seg.raw))
            elif kind == _S_TOKEN:
                out.append(self.token(TOKEN_MARK) + seg.raw[1:])
            elif kind == _S_MARK:
                out.append(self.token(TOKEN_MARK))
            elif language == JSON_TEXT and _json_value(seg, data=data):
                out.append(self.token(seg.raw))
            else:
                out.append(seg.raw)
        return "".join(out)


def _shown_literal(seg: _Seg) -> bool:
    """Whether a string literal of code is shown as it is: blank, or an object key written as a name. Nothing else,
    whatever its value: a value equal to a known name is a placeholder like any other."""
    content = seg.content
    if not content.strip():
        return True
    return seg.key and _KEY_NAME.fullmatch(content) is not None and len(content) <= MAX_NAME_CHARS


def _json_value(seg: _Seg, *, data: bool) -> bool:
    """Whether a segment of JSON text is a bare value to hide: a word but true, false and null; in data (a payload)
    also true, false and every number."""
    if seg.kind == _S_WORD:
        return seg.raw != "null" if data else seg.raw not in _JSON_WORDS
    return data and seg.kind == _S_TEXT and seg.raw[:1].isdigit()


def _joined(raw: str, values: Sequence[str]) -> str:
    """``raw`` with its placeholders, in order, replaced by ``values``."""
    pieces = TOKEN_SPLIT.split(raw)
    pieces[1::2] = values
    return "".join(pieces)


def _segments(text: str, language: str) -> list[_Seg]:
    """``text`` (code in ``language``) split into segments: string literals, comments, regular expressions, words,
    placeholders and other text (see :class:`_Seg`).

    The lexer fails safe: a word, a number or punctuation is a visible segment only where it is classified for sure.
    A ``/`` of DataWeave or JavaScript is a division where that is provable (after a number, a literal, ``]``, a
    ``)`` that does not close ``if (...)`` or the like, a JavaScript identifier that is not a keyword, or a DataWeave
    field selector written right after its "." or "?."), and a regular expression must close on its line, so one that
    does not is a division too. Where a regular expression is certain (after an operator, a JavaScript keyword such as
    ``return``, or a DataWeave word that follows an operand for sure, as in ``payload splitBy /,/``) it is read as one.
    Anywhere else every reading is hidden (:func:`_unsure_slash`). No keyword turns string, regular expression or
    comment reading off: the only "/" read as part of a word is the MIME type of a DataWeave output or input
    directive, and only where the directive is certain (:func:`_dw_directive`); a statement there outside the
    directive grammar hides the rest of the text. A string whose interpolations (``${...}``, ``$(...)``, a Python
    f-string's ``{...}``) cannot be followed for sure, and a Java unicode escape outside a string (it can spell a
    quote), make the rest of the text one segment
    that is never closed."""
    segs: list[_Seg] = []
    index, size = 0, len(text)
    last = ""  # the token before: a word, punctuation, _LAST_NUMBER, _LAST_VALUE, _LAST_UNSURE, or "" (none yet)
    selected = False  # the word in ``last`` stands right after "." (a field selector)
    infix = False  # the DataWeave word in ``last`` follows an operand for sure (an infix function or operator word)
    closed_division = False  # ``last`` is ")" and a "/" after it is provably a division
    brackets: list[tuple[str, bool]] = []  # open brackets; for "(", whether a "/" after its ")" is a division
    case_open = False  # after "case" and before its ":": a string there is never an object key
    header = language == DATAWEAVE  # a DataWeave text before its "---" separator (where a directive may stand)
    script = header and text.lstrip().startswith("%dw")  # a full DataWeave script, not a Mule #[...] expression
    first = True  # no token yet in the text
    line_start = True  # no token yet on this line
    slashes = language in (DATAWEAVE, JAVASCRIPT, JAVA)
    while index < size:
        char = text[index]
        if char.isspace():
            end = index
            while end < size and text[end].isspace():
                end += 1
            segs.append(_Seg(_S_TEXT, text[index:end]))
            if "\n" in text[index:end]:
                line_start = True
            index = end
            continue
        at_text_start, at_line_start = first, line_start
        first = line_start = False
        if char == TOKEN_MARK:
            match = TOKEN.match(text, index)
            if match is not None:
                segs.append(_Seg(_S_TOKEN, match.group(0)))
                index = match.end()
            else:
                segs.append(_Seg(_S_MARK, char))
                index += 1
            last = _LAST_UNSURE
            continue
        if slashes and text.startswith("//", index):
            end = text.find("\n", index)
            end = size if end < 0 else end
            segs.append(_Seg(_S_LINE, text[index:end], quote="//", content=text[index + 2 : end]))
            index = end
            continue
        if slashes and text.startswith("/*", index):
            end = text.find("*/", index + 2)
            stop = size if end < 0 else end
            closed = end >= 0
            segs.append(_Seg(_S_BLOCK, text[index : stop + 2 if closed else size], content=text[index + 2 : stop],
                             closed=closed))
            index = stop + 2 if closed else size
            continue
        comment = _line_comment(text, index, language)
        if comment:
            end = text.find("\n", index)
            end = size if end < 0 else end
            segs.append(_Seg(_S_LINE, text[index:end], quote=comment, content=text[index + len(comment) : end]))
            index = end
            continue
        if language == JAVA and char == "\\":
            # A unicode escape outside a string literal can spell any character, a quote or a line end included.
            segs.append(_Seg(_S_OPEN, text[index:]))
            break
        if char in "'\"`" and (char != "`" or language in (DATAWEAVE, JAVASCRIPT)):
            quote = char * 3 if language in (PYTHON, JAVA) and text.startswith(char * 3, index) else char
            prefix = _prefix(text, index)
            end = _quoted_end(text, index + len(quote), quote, language, fstring=_fstring(prefix, language))
            if end < 0:
                segs.append(_Seg(_S_OPEN, text[index:]))
                break
            after = end + len(quote)
            key = (
                language != JAVA
                and not case_open
                and last in ("{", ",")
                and bool(brackets)
                and brackets[-1][0] == "{"
                and _next_char(text, after) == ":"
            )
            segs.append(
                _Seg(
                    _S_STRING,
                    text[index:after],
                    quote=quote,
                    content=text[index + len(quote) : end],
                    key=key,
                    prefixed=bool(prefix),
                )
            )
            index = after
            last = _LAST_VALUE
            continue
        if language == JSON_TEXT and char == "{":
            ref = _APIGEE_REF.match(text, index)
            if ref is not None:
                segs.append(_Seg(_S_REF, ref.group(0)))
                index = ref.end()
                last = _LAST_VALUE
                continue
        if (
            char == "/"
            and language in (DATAWEAVE, JAVASCRIPT)
            and not _division(language, last, selected=selected, closed_division=closed_division)
        ):
            end = _regex_end(text, index + 1)
            if end > 0 and not _regex_certain(language, last, infix=infix):
                # A division was possible here too: every reading is hidden (see :func:`_unsure_slash`).
                unsure_segs = _unsure_slash(text, index)
                segs += unsure_segs
                if unsure_segs[-1].kind == _S_OPEN:
                    break
                index += sum(len(seg.raw) for seg in unsure_segs)
                last = _LAST_UNSURE
                continue
            if end > 0:
                segs.append(_Seg(_S_REGEX, text[index : end + 1], content=text[index + 1 : end]))
                index = end + 1
                last = _LAST_VALUE
                continue
        number = _CODE_NUMBER.match(text, index) if char.isdigit() else None
        if number is not None:
            segs.append(_Seg(_S_TEXT, number.group(0)))
            index = number.end()
            last = _LAST_NUMBER
            continue
        word = _WORD.match(text, index) if char.isalnum() or char in "_$" else None
        if (
            word is not None
            and header
            and not brackets
            and word.group(0) in _DW_DIRECTIVE_WORDS
            and (at_line_start if script else at_text_start)
            and text[word.end() : word.end() + 1] in (" ", "\t")
        ):
            directive = _dw_directive(text, index, script=script)
            if directive is None:
                # A statement that starts as a directive but is not in its grammar: nothing after the word is read.
                segs.append(_Seg(_S_WORD, word.group(0)))
                segs.append(_Seg(_S_OPEN, text[word.end() :]))
                break
            if directive.group("name") is None:
                segs.append(_Seg(_S_WORD, directive.group("word")))
            else:
                segs.append(_Seg(_S_WORD, directive.group("iword")))
                segs.append(_Seg(_S_TEXT, directive.group("igap")))
                segs.append(_Seg(_S_WORD, directive.group("name")))
            segs.append(_Seg(_S_TEXT, directive.group("gap")))
            segs.append(_Seg(_S_TEXT, directive.group("mime")))
            index = directive.end("mime")
            selected = False
            last = _LAST_VALUE
            continue
        if word is not None:
            segs.append(_Seg(_S_WORD, word.group(0)))
            # A DataWeave word right after an operand that is one for sure (a field selector, a Mule binding, a number,
            # a literal, "]") is an infix function or an operator word: a "/" after it starts a regular expression.
            infix = language == DATAWEAVE and (
                last in (_LAST_NUMBER, _LAST_VALUE, "]")
                or (_WORD.fullmatch(last) is not None and (selected or last in _DW_BINDINGS))
            )
            # A field selector only when the word is written right after its "." (vars.total, payload?.total).
            selected = last == "." and text[index - 1 : index] == "."
            infix = infix and not selected
            index = word.end()
            last = word.group(0)
            if last == "case":
                case_open = True
            continue
        if char == "(":
            brackets.append((char, _paren_division(language, last, selected=selected)))
        elif char in "[{":
            brackets.append((char, False))
        closed_division = False
        if char in ")]}":
            opened, division = brackets.pop() if brackets else ("", False)
            closed_division = char == ")" and opened == "(" and division
        if char in "{:;>":
            case_open = False
        if language == DATAWEAVE and text.startswith("---", index):
            header = False
        segs.append(_Seg(_S_TEXT, char))
        last = char
        index += 1
    return segs


def code_shape(text: str, language: str = DATAWEAVE) -> str:
    """``text`` (code in ``language``) reduced to what can change its meaning, read by the same fail-safe lexer as
    the placeholders (:func:`_segments`): two texts with the same shape are the same code for sure.

    Every segment the lexer finds is kept with its kind and its bytes exactly as written: string literals, template
    literals, regular expressions and comments (whatever their kind), a placeholder mark, and every segment hidden
    because the lexer could not read that point for sure. Only a run of blanks between two such segments is reduced,
    and only where both are read for sure (never next to a hidden segment, a placeholder or a stray mark, where the
    run stays as written):

    * after a line comment, to a line end (the line end closes the comment);
    * in Python, a run holding a line end to a line end and the next line's indentation exactly as written;
    * in JavaScript, a run holding a line end to a line end (automatic semicolons);
    * to one blank between two word characters ("." counted) or two operator characters (``a - -b`` is not
      ``a--b``);
    * letters right before a Python string stay its prefix (``r 'x'`` is not ``r'x'``);
    * to nothing elsewhere.

    So re-indenting code changes nothing, and reflowing DataWeave, Java or JSON between its tokens changes nothing,
    while any change inside a literal or a comment, or anywhere the lexer is unsure, is a change."""
    segs = _segments(text, language)
    out: list[str] = []
    for position, seg in enumerate(segs):
        if seg.kind == _S_TEXT and not seg.raw.strip():
            before = segs[position - 1] if position > 0 else None
            after = segs[position + 1] if position + 1 < len(segs) else None
            kind, raw = "blank", _blank_shape(seg.raw, before, after, language)
            if not raw:
                continue
        else:
            # Letters right before a Python string are its prefix (r'', b'', f''); elsewhere they are a word.
            kind, raw = seg.kind + ("+prefixed" if seg.prefixed and language == PYTHON else ""), seg.raw
        out.append(f"{kind}{len(raw)}:{raw}")
    return "".join(out)


# Characters of operators a blank may keep apart; blank segments next to these kinds stay as written.
_JOINING = frozenset("+-*/%=<>!&|^~?:.@#\\")
_UNSURE_SEGMENTS = frozenset({_S_OPEN, _S_TOKEN, _S_MARK})


def _blank_shape(raw: str, before: _Seg | None, after: _Seg | None, language: str) -> str:
    """The shape of a run of blanks ``raw`` between segments ``before`` and ``after`` (see :func:`code_shape`)."""
    if (before is not None and before.kind in _UNSURE_SEGMENTS) or (after is not None and after.kind in _UNSURE_SEGMENTS):
        return "=" + raw
    if language == PYTHON and ("\n" in raw or before is None):
        return "\n" + raw[raw.rfind("\n") + 1 :] if after is not None else ""
    if before is None or after is None:
        return ""
    if before.kind == _S_LINE or (language == JAVASCRIPT and "\n" in raw):
        return "\n"
    last, first = before.raw[-1:], after.raw[:1]
    if _word_edge(last) and _word_edge(first):
        return " "
    # Two operator characters the lexer reads one by one; a literal next to one is its own segment either way.
    operators = before.kind == _S_TEXT and after.kind == _S_TEXT
    return " " if operators and last in _JOINING and first in _JOINING else ""


def _word_edge(char: str) -> bool:
    return char == "." or _WORD.fullmatch(char) is not None


def _dw_directive(text: str, index: int, *, script: bool) -> re.Match[str] | None:
    """The DataWeave output or input directive whose word starts at ``index``, or None unless the whole statement is
    in the directive grammar (:data:`_DW_DIRECTIVE`). The caller has already checked that the word stands where only
    a directive can: in the header (before ``---``, outside any bracket), as the first token of its line in a full
    script (``%dw ...``) or as the first token of a Mule ``#[...]`` expression. In a script the line ends after the
    directive (or ``---`` or a comment follows); in an expression ``---`` follows. Anywhere else (a field selector
    such as ``payload.output``, a variable, a script body) the word is an ordinary word."""
    return (_DW_SCRIPT_DIRECTIVE if script else _DW_EXPRESSION_DIRECTIVE).match(text, index)


def _division(language: str, last: str, *, selected: bool, closed_division: bool) -> bool:
    """Whether a "/" of DataWeave or JavaScript after ``last`` (see :func:`_segments`) is provably a division."""
    if last in (_LAST_NUMBER, _LAST_VALUE, "]"):
        return True
    if last == ")":
        return closed_division
    if _WORD.fullmatch(last):
        # A JavaScript keyword may come before an expression (return /re/); a bare DataWeave word may be an infix
        # function (payload splitBy /,/), while a field selector (vars.total / 2) is a value.
        return last not in _JS_KEYWORDS if language == JAVASCRIPT else selected
    return False


def _unsure_slash(text: str, index: int) -> list[_Seg]:
    """The segments for a "/" at ``index`` that may start a regular expression (one closes on its line) or be a
    division: whatever the reading, nothing that either reading would hide is shown. The rest of the line from the
    "/" is one regular expression up to its last "/" (so no "/" is left on the line to pair up differently), or, when
    a ``//`` comment may start in it, a regular expression up to the comment's first "/" and the comment to the end
    of the line. When a quote stands in that stretch (a string could run past it) or a ``/*`` comment may start in
    the line, the rest of the text is one segment that is never closed."""
    line_end = text.find("\n", index)
    line_end = len(text) if line_end < 0 else line_end
    rest = text[index:line_end]
    if "/*" in rest[1:]:
        return [_Seg(_S_OPEN, text[index:])]
    comment = rest.find("//", 1)
    if comment > 0:
        if any(quote in rest[1:comment] for quote in "'\"`"):
            return [_Seg(_S_OPEN, text[index:])]
        return [
            _Seg(_S_REGEX, rest[: comment + 1], content=rest[1:comment]),
            _Seg(_S_LINE, rest[comment + 1 :], quote="/", content=rest[comment + 2 :]),
        ]
    close = position = 1
    while position < len(rest):
        if rest[position] == "\\":
            position += 2
            continue
        if rest[position] == "/":
            close = position
        position += 1
    if any(quote in rest[1:close] for quote in "'\"`"):
        return [_Seg(_S_OPEN, text[index:])]
    return [_Seg(_S_REGEX, rest[: close + 1], content=rest[1:close])]


def _regex_certain(language: str, last: str, *, infix: bool = False) -> bool:
    """Whether a "/" after ``last`` can only start a regular expression (a division is impossible there): at the
    start, after punctuation that cannot end an operand, after a JavaScript keyword that cannot be one, or after a
    DataWeave word that follows an operand for sure (``infix``: payload splitBy /,/, vars.a matches /x/)."""
    if last == "" or (infix and _WORD.fullmatch(last) is not None):
        return True
    if len(last) == 1 and last in "(,=:[!&|?{;*%<>~^":
        return True
    return language == JAVASCRIPT and last in _JS_OPERATOR_WORDS


def _paren_division(language: str, last: str, *, selected: bool) -> bool:
    """Whether a "/" after the ")" closing a "(" that follows ``last`` is provably a division: not after a keyword
    (``if (x) /re/``) or, in DataWeave, a bare word."""
    if _WORD.fullmatch(last):
        return last not in _JS_KEYWORDS if language == JAVASCRIPT else selected
    return last != _LAST_UNSURE


def _line_comment(text: str, index: int, language: str) -> str:
    """The opener of a line comment at ``index`` other than ``//``: a Python ``#``, a JavaScript ``#!`` line at the
    start, or an HTML-like ``<!--`` or ``-->`` (read as a comment wherever it stands, which hides more, never less)."""
    if language == PYTHON and text.startswith("#", index):
        return "#"
    if language != JAVASCRIPT:
        return ""
    if index == 0 and text.startswith("#!"):
        return "#!"
    for opener in ("<!--", "-->"):
        if text.startswith(opener, index):
            return opener
    return ""


def _prefix(text: str, index: int) -> str:
    """The letters, digits and underscores right before ``index`` (a string literal's prefix, as in ``rb''``)."""
    start = index
    while start > 0 and (text[start - 1].isalnum() or text[start - 1] == "_"):
        start -= 1
    return text[start:index]


def _fstring(prefix: str, language: str) -> bool:
    return language == PYTHON and ("f" in prefix or "F" in prefix)


def _quoted_end(text: str, start: int, quote: str, language: str, *, fstring: bool = False, depth: int = 0) -> int:
    """Where the string literal of ``language`` whose content starts at ``start`` closes with ``quote``, following
    its interpolations (DataWeave ``$(...)``, a JavaScript template's ``${...}``, a Python f-string's ``{...}``), or
    -1 when it never does or an interpolation cannot be followed for sure."""
    if language == DATAWEAVE:
        opener, closer = "$(", ")"
    elif language == JAVASCRIPT and quote == "`":
        opener, closer = "${", "}"
    elif fstring:
        opener, closer = "{", "}"
    else:
        return _string_end(text, start, quote)
    if depth >= _MAX_NESTING:
        return -1
    index, size = start, len(text)
    while index < size:
        if text[index] == "\\":
            index += 2
            continue
        if text.startswith(quote, index):
            return index
        if fstring and text.startswith("{{", index):
            index += 2
            continue
        if text.startswith(opener, index):
            end = _interpolation_end(text, index + len(opener), language, closer, depth + 1)
            if end < 0:
                return -1
            index = end + 1
            continue
        index += 1
    return -1


def _interpolation_end(text: str, start: int, language: str, closer: str, depth: int) -> int:
    """Where the interpolation whose code starts at ``start`` closes with ``closer`` (nested brackets, strings and
    regular expressions skipped), or -1 when it never does or holds a comment."""
    pairs = {"(": ")", "[": "]", "{": "}"}
    expected: list[str] = []
    index, size = start, len(text)
    while index < size:
        char = text[index]
        if char in "'\"`" and (char != "`" or language in (DATAWEAVE, JAVASCRIPT)):
            quote = char * 3 if language == PYTHON and text.startswith(char * 3, index) else char
            fstring = _fstring(_prefix(text, index), language)
            end = _quoted_end(text, index + len(quote), quote, language, fstring=fstring, depth=depth)
            if end < 0:
                return -1
            index = end + len(quote)
            continue
        if char == "#" and language == PYTHON:
            return -1
        if char == "/" and language != PYTHON:
            if text.startswith(("//", "/*"), index):
                return -1
            end = _regex_end(text, index + 1)  # read as a regular expression when it may be one (skips more)
            index = end + 1 if end > 0 else index + 1
            continue
        if char in pairs:
            expected.append(pairs[char])
        elif char in ")]}":
            if not expected:
                return index if char == closer else -1
            if expected.pop() != char:
                return -1
        index += 1
    return -1


def _decode(content: str, language: str, quote: str) -> str | None:
    """The value of a string literal of ``language`` whose content (between the quotes) is ``content``, or None
    when a2m cannot read it for sure (an unknown escape, DataWeave interpolation, a JavaScript template with
    ``${}``, a Java text block)."""
    if len(quote) != 1 and language != PYTHON:
        return None
    if quote == "`" and (language != JAVASCRIPT or "${" in content):
        return None
    out: list[str] = []
    index, size = 0, len(content)
    while index < size:
        char = content[index]
        if char == "$" and language == DATAWEAVE:
            return None  # $(...) is interpolation
        if char != "\\":
            out.append(char)
            index += 1
            continue
        following = content[index + 1 : index + 2]
        if following in _ESCAPES and (language != JSON_TEXT or following in _JSON_ESCAPES):
            out.append(_ESCAPES[following])
            index += 2
        elif following == "$" and language == DATAWEAVE:
            out.append("$")
            index += 2
        elif following == "u" and _HEX4.fullmatch(content, index + 2, index + 6):
            code = int(content[index + 2 : index + 6], 16)
            if 0xD800 <= code <= 0xDFFF:
                return None
            out.append(chr(code))
            index += 6
        elif following == "x" and language in (JAVASCRIPT, PYTHON) and _HEX2.fullmatch(content, index + 2, index + 4):
            out.append(chr(int(content[index + 2 : index + 4], 16)))
            index += 4
        elif following == "0" and language in (JAVASCRIPT, PYTHON, JAVA) and not content[index + 2 : index + 3].isdigit():
            out.append("\0")
            index += 2
        elif following == "v" and language in (JAVASCRIPT, PYTHON):
            out.append("\v")
            index += 2
        elif following == "\n" and language in (JAVASCRIPT, PYTHON):
            index += 2
        else:
            return None
    return "".join(out)


def _encode(value: str, language: str, quote: str) -> str | None:
    """``value`` spelled as the content of a string literal of ``language`` quoted with ``quote`` (its backslashes,
    quotes, ``$`` in DataWeave and control characters escaped), or None when a2m cannot spell it there."""
    if len(quote) != 1 or quote == "`":
        return None
    out: list[str] = []
    for char in value:
        if char in _SPELLED:
            out.append(_SPELLED[char])
        elif char == quote:
            out.append("\\" + char)
        elif char == "$" and language == DATAWEAVE:
            out.append("\\$")
        elif ord(char) < 0x20 or char in "\u2028\u2029":
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    return "".join(out)


def _condition_value(tokens: Sequence[Token], index: int) -> bool:
    """Whether the bare word ``tokens[index]`` of an Apigee condition is a value (it follows a comparison)."""
    if index == 0:
        return False
    before = tokens[index - 1]
    if before.kind is TokenKind.SYMBOL:
        return before.text not in _LOGIC
    if before.kind is TokenKind.WORD:
        return fold(before.text) in _COMPARE_WORDS
    return False


def _string_end(text: str, start: int, quote: str) -> int:
    """Where the string literal that starts before ``start`` closes with ``quote`` (backslash escapes skipped), or
    -1 when it never does."""
    index, size = start, len(text)
    while index < size:
        if text[index] == "\\":
            index += 2
            continue
        if text.startswith(quote, index):
            return index
        index += 1
    return -1


def _regex_end(text: str, start: int) -> int:
    """Where the regular expression literal starting at ``start`` closes (escapes and classes skipped), or -1 when
    it does not close on its line."""
    index, size = start, len(text)
    in_class = False
    while index < size:
        char = text[index]
        if char == "\n":
            return -1
        if char == "\\":
            index += 2
            continue
        if char == "[":
            in_class = True
        elif char == "]":
            in_class = False
        elif char == "/" and not in_class:
            return index if index > start else -1
        index += 1
    return -1


def _expression_end(text: str, start: int) -> int:
    """Where the ``#[`` expression whose body starts at ``start`` closes, or -1 (also when it holds a comment). A
    "/" that may start a regular expression is read as one (its "]" does not close the expression), which skips more,
    never less."""
    depth = 1
    index, size = start, len(text)
    while index < size:
        char = text[index]
        if char in "'\"`":
            end = _quoted_end(text, index + 1, char, DATAWEAVE)
            if end < 0:
                return -1
            index = end + 1
            continue
        if char == "/":
            if text.startswith(("//", "/*"), index):
                return -1
            end = _regex_end(text, index + 1)
            index = end + 1 if end > 0 else index + 1
            continue
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return -1


def _next_char(text: str, index: int) -> str:
    while index < len(text) and text[index].isspace():
        index += 1
    return text[index] if index < len(text) else ""


def _decoded(literal: str, content: str) -> str:
    """The value of a quoted diff value (written as Python writes a text), or its content when it cannot be read."""
    try:
        value = ast.literal_eval(literal)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return content
    return value if isinstance(value, str) else content


__all__ = [
    "TOKEN",
    "TOKEN_SPLIT",
    "PlaceholderError",
    "Placeholders",
    "code_shape",
    "plain_visible",
]
