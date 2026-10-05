"""Placeholders: every literal value a2m shows the AI, replaced by a stable stand-in it can write back.

The AI translation of custom code and conditions (:mod:`a2m.ai.translate`) shows
the AI one callout's code and policy, or one condition, through a table of its
own. A fix request (:mod:`a2m.verify.fix_loop`) shows the AI Apigee policies, custom
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
  keywords, operators, ``{variable}`` references and ``${property}``
  placeholders. Every string literal (a quoted selector such as ``vars['x']``
  included), template literal (with every ``${...}`` part), regular expression
  and comment is a placeholder, and so is everything at a point the lexer
  cannot read for sure: a ``/`` that may start a regular expression hides every
  reading unless it is provably a division, and a DataWeave ``output`` or
  ``input`` word is a directive only in a script's header (or first in a Mule
  expression with ``---``), never as a field selector or variable;
* booleans, HTTP methods, MIME types and Apigee rates (``10pm``);
* a bare number only where it is a2m's own syntax or a structural setting: in
  the text of an Apigee element of :data:`NUMBER_TEXT_SLOTS` (``StatusCode``,
  ``Interval`` ...) or an attribute of :data:`NUMBER_ATTRIBUTE_SLOTS`
  (``timeLimit``, ``Allow/@count``), in a Mule attribute a2m generates with one
  (:data:`MULE_NUMBER_ATTRIBUTES`, the value of its ``httpStatus`` variable, where
  a DataWeave status code such as ``#[403]`` stays too), and in DataWeave as the
  index of a selector (``payload.items[0]``, ``(text as String)[6 to -1]``).
  Every other number is a placeholder: in a ``Property``, a header or query
  value, any other element text or attribute, JSON text (with its sign, fraction
  and exponent), a condition, custom code (JavaScript, Python, Java) and
  everywhere else in DataWeave (arithmetic, comparisons, literals). A number
  literal of a condition (as a2m's condition translator reads one: it starts
  with a digit or a sign), of code or of JSON text gets a number placeholder,
  ``«n1»``, so the AI sees it is a number; every other placeholder, ``«v1»``,
  stands for text (an XML value that holds digits is text);
* the variables and operators of an Apigee condition (never a number in it);
* in a URL, each query parameter name (written ``name=``);
* the words of a test diff (``header X-Served-By: expected ..., actual ...``), with
  the status codes and counts a2m expects, ``null``, HTTP methods, header names,
  query parameter names as a URL shows them, and a JSON key only when it is a
  known name or a plain word of letters (no digit). Every other number of a diff
  is a number placeholder (an array index too), every other JSON key a
  placeholder, and ``true`` and ``false`` are data.

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
written back for where it stands, by what that place is, never by what the
answer happens to write there. The text of a Transform Message part
(``ee:set-payload``, ``ee:set-variable``, ``ee:set-attributes``) and every
``#[...]`` expression is DataWeave, with or without a ``%dw`` header; a value
whose whole text is a JSON object or array is JSON text (a ``${property}`` in
it included); any other attribute, element text or CDATA is plain text; code
is code of its own language. The expressions of a value are found as Mule
finds them: a value that starts with ``#[``, ends with ``]`` and holds no
other ``#[`` is one expression, whatever is in it; in any other value each
expression ends at the ``]`` the DataWeave lexer finds for sure. A restore
never falls back to reading text as plain where it cannot tell what it is:
a placeholder in or after an expression whose end is not sure, in the text
around an expression of a value that is JSON text, after a string or a line
the lexer cannot read, or in a value that starts like XML but is not XML is
refused (unless left as shown in the same context), with a reason naming the
spot. In
plain text a text placeholder is its value, XML-escaped for that place. In
DataWeave, JSON text and code a text placeholder must stand inside a string
literal: it is the value spelled for that language and quote (as the source
spelled it, when the source had it in such a string), then XML-escaped.
Written bare there it is refused, whatever its value (``"true"`` written bare
would be the boolean ``true``, a word would be code or no JSON at all).

One rule decides which placeholders of a Mule file are written back from what
a2m showed: the answer is aligned, part by part, with the exact text a2m showed
(:meth:`Placeholders.restore`). A part is an attribute value, an element text, a
CDATA section or a comment. The answer's parts are paired with the parts a2m
showed by aligning the two sequences by what each part is (its element path,
kind and attribute) and holds, so a new element inserted before others, or one
deleted, never shifts which part they stand for; a part with no pair is new,
and in a stretch of parts that differ, parts of the same shape pair in order
(by the other attributes of their start tag first and, where those are all the
same, by content: exact text first, then the most alike, refused as ambiguous
when two are equally alike), so a part whose content moved to a sibling is a
change there. A part the answer leaves exactly as shown
is its original bytes (with only a new quote character escaped when the answer
changed the attribute's quote). In a part the answer changed, its text is
diffed with the shown text with blanks ignored (by lines, then by tokens only
inside the lines that differ, bounded), so re-indenting code changes nothing;
where a part changed too much at once to be lined up, a placeholder that would
be refused there is refused with that reason. Every placeholder whose shown
stretch is unchanged at the same aligned position, and is read in the same
context (the same language and the same chain of places around it: inside
``#[...]``, a string and its quote, JSON text or plain text), is written back
from the original bytes of that stretch, whatever it is (a string
literal, a regular expression, a stretch after an unsure ``/``, a stretch a2m
could not read and showed as one placeholder, a number), and is never read
again or refused. Unchanged means the whole stretch a2m showed for it (a
literal with its quotes, a regular expression with its slashes, a comment with
its opener) and the token right before it are the same; for a placeholder
standing alone in code (a number, a stretch a2m could not read, a JSON word)
the token right after it too; for one in plain text (an attribute or element
text outside any ``#[...]``), where nothing around it is syntax, its place
alone. Only the placeholders of the changed stretches are read, by the rules
below; one copied or moved from elsewhere is a change where it now stands. A
text placeholder in a changed regular expression or ``/`` stretch is refused,
and so is the placeholder of a stretch a2m could not read when it is moved,
quoted or edited, and any placeholder written after a ``/`` a2m cannot read
for sure on its line (such a line hides only itself when everything it opens
closes on it in both readings of the ``/``, else all the code after it). In a
plain Mule attribute or text (outside every ``#[...]``) a value that would
start a Mule expression (it holds ``#[``, or makes one with what is next to
it) or a property placeholder (``${``) is refused, unless that whole ``#[``
or ``${...}`` lies inside one value left unchanged.

A number placeholder goes where a number goes:
outside any string literal of DataWeave or JSON it is the same number, as its
literal was written when it comes from that language, else in the form both
read (``5000L`` is ``5000``, ``0xFF`` is ``255``, a negative number in
DataWeave is in parentheses); in an attribute or element text it is that
number in plain digits, as Mule reads an attribute number (``5e3`` and
``5000.0`` are ``5000``, ``2.5e-3`` is ``0.0025``); it is refused inside a
string literal, as data of the answer (a condition tree's value), and when the
number cannot be written exactly. Inside a DataWeave string's ``$( )``
interpolation the AI writes code, so a placeholder there is put back as in any
code. One rule for where a placeholder may stand, whatever language its value
came from: a placeholder outside a string literal, and a number placeholder
anywhere, must stand alone; one joined to a letter, a digit, ``.``, ``_``,
``$``, a quote or another placeholder (``.«n1»``, ``«n1»«n2»``, ``«n1»e3``) is
refused, since it would join into another value. An echoed
file is byte for byte the file a2m showed. Refused, with the reason: an unknown
placeholder, a placeholder outside any value (in a tag), in a comment that is
not exactly as a2m showed it there, a text placeholder outside any string
literal of DataWeave, JSON text or code, in a string it cannot be spelled in,
in a changed regular expression or ``/`` stretch, as a new Mule expression in
plain text, or in a CDATA section or comment that cannot hold its value.
"""

from __future__ import annotations

import ast
import bisect
import json
import json.encoder
import re
import string
import struct
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from decimal import Decimal
from difflib import SequenceMatcher

from defusedxml import DefusedXmlException
from defusedxml import ElementTree as SafeET

from a2m.ai.provider import ItemKind
from a2m.conditions.lexer import ConditionError, Token, TokenKind, tokenize
from a2m.conditions.variables import fold

# A placeholder as shown to the AI: ``«vN»`` stands for text, ``«nN»`` for a number literal of a condition or of code
# (written without quotes there). Both kinds share one numbering.
TOKEN = re.compile(r"«[vn](\d{1,7})»")
TOKEN_FORMAT = "«v{number}»"
NUMBER_TOKEN_FORMAT = "«n{number}»"
NUMBER_TOKEN = re.compile(r"«n\d{1,7}»")
# Splits a text into the pieces between placeholders and the placeholders themselves (odd indexes).
TOKEN_SPLIT = re.compile(r"(«[vn]\d{1,7}»)")
# Text holding this character is never shown as it is (it could be mistaken for a placeholder).
TOKEN_MARK = "«"

# Languages of the code lexer: the custom code kinds are a2m.ai.provider.ItemKind's own values.
DATAWEAVE = "dataweave"
JAVASCRIPT = ItemKind.JAVASCRIPT.value
PYTHON = ItemKind.PYTHON.value
JAVA = ItemKind.JAVA.value
JSON_TEXT = "json"
# Where a number placeholder's literal came from when it is not code: an Apigee condition.
CONDITION = "condition"
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
# A value with at least this many letters and digits is looked for in a swept text in any spelling
# (:meth:`Placeholders.holds_value`).
MIN_HELD_CHARS = 6
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
DATA_ELEMENTS: frozenset[str] = frozenset({"Payload", "Value", "InitialEntries", "Template", "Property"})
# A bare number is shown only where it is syntax or a structural setting, never as data (default deny). In an Apigee
# policy or endpoint: the text of these elements (outside data) and these attributes (element -> attributes, ROOT: the
# root element). Every other number of a policy (a Property, a header or query value, a payload, any other element
# text or attribute) is a placeholder.
NUMBER_TEXT_SLOTS: frozenset[str] = frozenset(
    {"StatusCode", "Interval", "Rate", "ExpiresIn", "RefreshTokenExpiresIn", "TimeoutInSec", "TimeoutInSeconds"}
)
NUMBER_ATTRIBUTE_SLOTS: Mapping[str, frozenset[str]] = {
    ROOT: frozenset({"timeLimit", "revision"}),
    "Allow": frozenset({"count"}),
}
# In a Mule file: the attributes a2m generates with a structural number (local names), and a2m's own status variable
# (``<set-variable variableName="httpStatus" value="500"/>``). Every other number in a Mule attribute or element
# text is a placeholder; numbers in DataWeave code stay.
MULE_NUMBER_ATTRIBUTES: frozenset[str] = frozenset({"entryTtl", "port", "statusCode", "responseTimeout"})
MULE_STATUS_VARIABLE = "httpStatus"
# Mule elements whose text or CDATA is a DataWeave script, with or without its ``%dw`` header: the parts of a
# Transform Message, as (element, parent) local names (``ee:message/ee:set-payload``,
# ``ee:message/ee:set-attributes``, ``ee:variables/ee:set-variable``). The core ``set-payload`` and ``set-variable``
# carry their value in an attribute, never in their text.
DATAWEAVE_SCRIPT_ELEMENTS: frozenset[tuple[str, str]] = frozenset(
    {("set-payload", "message"), ("set-attributes", "message"), ("set-variable", "variables")}
)
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
# A number of code, read whole: a hexadecimal one up to its letters (and a binary exponent's sign), any other one with
# its exponent's sign (``1e-5`` is one number; ``0x1e-5`` is a subtraction).
_CODE_NUMBER = re.compile(r"0[xX](?:[pP][+-]\d|[\w.])*|\d(?:[eE][+-]\d|[\w.])*")
# A number of code that starts with its "." (JavaScript, Java and Python ``.07``, ``.5e3``, Java ``.1f``): the dot is
# part of the literal, so the digits after it are never read as an integer of their own (``07`` is octal 7).
_DOT_NUMBER = re.compile(r"\.\d(?:[eE][+-]\d|[\w.])*")
# DataWeave words after which a "." cannot select a field, so a "." with a digit after it starts a number.
_DW_OPERATOR_WORDS = frozenset({"and", "or", "not", "else", "default", "to", "case", "if", "do"})
# A number of JSON text, read whole: its sign, fraction and exponent belong to it (JSON has no operators).
_JSON_NUMBER = re.compile(r"[+-]?\.?\d(?:[eE][+-]\d|[\w.])*")
# A DataWeave number shown in a Mule attribute that holds a status code (:func:`_mule_number_slot`).
_STATUS_CODE = re.compile(r"[1-5]\d\d")
# The version of a DataWeave script's header line (``%dw 2.0``).
_DW_VERSION = re.compile(r"\s*%dw[ \t]+(\d+(?:\.\d+)?)(?![\w.])")
_HEX4 = re.compile(r"[0-9A-Fa-f]{4}")
_HEX2 = re.compile(r"[0-9A-Fa-f]{2}")
# A diff's unquoted request target (a2m.verify.compare): a path, or an address with a scheme.
_DIFF_TARGET = re.compile(r"(?:[A-Za-z][A-Za-z0-9+.\-]{0,31}://|/)[^\s,'\"]*")
# The lines a2m writes in a failing-test diff (a2m.verify.compare and a2m.verify.harness), after the "call N " a test
# with more than one call puts first. Status codes and counts are a2m's test expectations, never proxy data.
_DIFF_CALL = re.compile(r"call \d{1,9}(?=[ :])")
_DIFF_NO_RESPONSE = ": no valid response from the app: "
_DIFF_FIXED_LINE = re.compile(
    r"status: expected \d{1,9}, actual \d{1,9}|backend calls: expected \d{1,9}, got \d{1,9}"
    r"|(?:backend call \d{1,9} )?body: \d{1,9} more fields differ|\(no detail\)|none"
)
# The start of a line that names a part, then a field path, header name or query name up to ": expected " (found with
# str.find, never by backtracking: a JSON key may be long and hold anything, a line end included).
_DIFF_NAMED = re.compile(r"((?:backend call \d{1,9} )?body) field |((?:backend call \d{1,9} )?header) |(backend call \d{1,9}) query ")
_DIFF_SEPARATOR = ": expected "
_DIFF_WHOLE_BODY = re.compile(r"((?:backend call \d{1,9} )?body): (?=expected )")
_DIFF_REQUEST_TARGET = re.compile(r"(backend call \d{1,9}): expected (\S+) (\S*), got (\S+) (\S*)")
# A JSON field path of a diff: its key segments, each "." and each array index ("[3]").
_DIFF_PATH_SPLIT = re.compile(r"(\.|\[\d{1,9}\])")
_DIFF_WORD = re.compile(r"\w+")
# A JSON key of a diff shown as it is: a plain word of letters (camelCase, or words joined by "_" or "-"), no digit.
_PLAIN_KEY = re.compile(r"[A-Za-z]{1,32}(?:[_\-][A-Za-z]{1,32}){0,3}")
# An HTTP header name (RFC 9110 token), as a diff names a header.
_HEADER_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z\-]{1,256}")
# a2m's own words around the values of a diff line ("expected 'a', actual missing").
_DIFF_VALUE_WORDS: frozenset[str] = frozenset({"expected", "actual", "absent", "missing", "got"})
_ATTRIBUTE_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;", "\r": "&#13;", "\n": "&#10;", "\t": "&#09;"}
_TEXT_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}
# Backslash escapes a string literal may use, and what each stands for.
_ESCAPES = {"\\": "\\", "'": "'", '"': '"', "`": "`", "/": "/", "n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}
_JSON_ESCAPES = frozenset('\\"/bfnrt')
_JSON_WORDS = frozenset({"true", "false", "null"})
_SPELLED = {"\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t", "\b": "\\b", "\f": "\\f"}


# Where a part stands in its XML text (:func:`_lex`): each open element from the root as (tag, ordinal among its
# siblings with the same tag), then the part itself: ("@" + attribute name, 0) for an attribute value of the last
# element, or (part kind, ordinal among the parts of that kind in its parent) for a text, CDATA section or comment.
_Where = tuple[tuple[str, int], ...]

# What a placeholder stood for in a Mule part a2m showed (:class:`_Shown`): how far its stretch reaches around it.
_PLAIN = "plain"  # in plain text: nothing around it is syntax
_DELIMITED = "delimited"  # a string literal, regular expression or comment with its quotes, slashes or opener
_BARE = "bare"  # alone in code: a number, a JSON word, a stretch a2m could not read for sure

# Bounds of the diff of an answer's part with the part a2m showed (:func:`_alignment`): a part with more tokens is
# not diffed (each of its placeholders is read as changed), and a changed block of lines is diffed by tokens only up
# to this many token pairs (beyond, only its common start and end are aligned).
MAX_DIFF_TOKENS = 200_000
MAX_DIFF_LINES = 20_000
MAX_TOKEN_PAIRS = 250_000
# All the blocks of one part together are diffed by tokens only up to this many token pairs (beyond, the rest of its
# changed blocks are marked as changed too much to be lined up).
MAX_PART_TOKEN_PAIRS = 1_000_000
# Bound of the diff of an answer's sequence of value parts with a2m's (:func:`_pairing`): beyond this many parts that
# differ on either side, each is paired with the part a2m showed at the same place (its element path with sibling
# ordinals), if any.
MAX_DIFF_PARTS = 100_000
# How many placeholders of one part read in a stretch a2m cannot read for sure are checked against what came before
# them (:meth:`Placeholders._unchanged`); beyond, they are read as changed.
MAX_OPEN_PROBES = 64
# The tokens of that diff: a placeholder, a word, a line end, a run of other blanks, any other character.
_DIFF_TOKEN = re.compile(r"«[vn]\d{1,7}»|\w+|\n|[^\S\n]+|.", re.DOTALL)


@dataclass(frozen=True, slots=True)
class _Placed:
    """One placeholder of a part a2m showed: the value it stood for as the part reads (``value``), the original bytes
    of that value in the part (``raw``), and its stretch (``start``, ``end``) in the part's shown text."""

    value: str
    raw: str
    start: int
    end: int
    kind: str


@dataclass(frozen=True, slots=True)
class _Shown:
    """A part of a Mule file as a2m showed it: its kind and quote, the part as shown (``raw``) and as it was
    (``original``), its shown text as it reads (``text``) and each of its placeholders (None when a2m cannot tell the
    original bytes of each one; then only an exact echo of the whole part is written back as it was)."""

    kind: str
    quote: str
    raw: str
    original: str
    text: str
    placed: tuple[_Placed, ...] | None


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
    # For an attribute: every attribute of its start tag (name, value as written), in order.
    tag: tuple[tuple[str, str], ...] = ()
    # Where it stands in its XML text (:data:`_Where`).
    where: _Where = ()


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
    # Read where a "/" may start a regular expression or be a division (:func:`_unsure_slash`): either reading holds.
    unsure: bool = False
    # For a segment that is never closed: why ("string", "slash", "directive", "escape"; see :func:`_open_reason`).
    why: str = ""


def _local(name: str) -> str:
    return name.rpartition(":")[2]


def _lex(text: str) -> list[_Part]:
    """``text`` (XML) split into parts, each with where it stands (:data:`_Where`); raises :class:`_LexError` when it
    is not XML a2m can split (a DTD, an unclosed tag, quote, comment or CDATA section)."""
    parts: list[_Part] = []
    stack: list[str] = []
    # The open elements as (tag, ordinal), and for the document and each open element how many children of each tag
    # (or value parts of each kind) it has had so far.
    where: list[tuple[str, int]] = []
    counts: list[dict[str, int]] = [{}]
    index, size = 0, len(text)
    while index < size:
        start = text.find("<", index)
        if start < 0:
            start = size
        if start > index:
            parts.append(
                _Part(_TEXT, text[index:start], stack[-1] if stack else "", path=tuple(stack),
                      where=_value_where(where, counts, _TEXT))
            )
            index = start
            continue
        if text.startswith("<!--", index):
            end = text.find("-->", index + 4)
            if end < 0:
                raise _LexError("an XML comment is never closed")
            comment = _Part(_COMMENT, text[index + 4 : end], where=_value_where(where, counts, _COMMENT))
            parts += [_Part(_MARKUP, "<!--"), comment, _Part(_MARKUP, "-->")]
            index = end + 3
        elif text.startswith("<![CDATA[", index):
            end = text.find("]]>", index + 9)
            if end < 0:
                raise _LexError("a CDATA section is never closed")
            element = stack[-1] if stack else ""
            cdata = _Part(_CDATA, text[index + 9 : end], element, path=tuple(stack),
                          where=_value_where(where, counts, _CDATA))
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
            if where:
                where.pop()
                counts.pop()
            index = end + 1
        else:
            index = _lex_start_tag(text, index, parts, stack, where, counts)
    return parts


def _value_where(where: list[tuple[str, int]], counts: list[dict[str, int]], kind: str) -> _Where:
    """Where the next value part of ``kind`` (text, CDATA, comment) stands under the innermost open element."""
    ordinal = counts[-1].get("\x00" + kind, 0)
    counts[-1]["\x00" + kind] = ordinal + 1
    return (*where, (kind, ordinal))


def _lex_start_tag(
    text: str,
    index: int,
    parts: list[_Part],
    stack: list[str],
    where: list[tuple[str, int]],
    counts: list[dict[str, int]],
) -> int:
    size = len(text)
    end = index + 1
    while end < size and not text[end].isspace() and text[end] not in "/>":
        end += 1
    name = text[index + 1 : end]
    if not name:
        raise _LexError("a '<' does not start a tag")
    ordinal = counts[-1].get(name, 0)
    counts[-1][name] = ordinal + 1
    element = (*where, (name, ordinal))
    markup = [text[index:end]]
    position = end
    first_attribute = len(parts)
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
            _tag_attributes(parts, first_attribute)
            return position + 2
        if text[position] == ">":
            markup.append(">")
            parts.append(_Part(_MARKUP, "".join(markup)))
            _tag_attributes(parts, first_attribute)
            stack.append(_local(name))
            where.append((name, ordinal))
            counts.append({})
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
            _Part(_ATTRIBUTE, text[cursor + 1 : close], _local(name), attribute, quote, (*stack, _local(name)),
                  where=(*element, ("@" + attribute, 0)))
        )
        markup = [quote]
        position = close + 1


def _tag_attributes(parts: list[_Part], first: int) -> None:
    """Give each attribute part of the start tag lexed from ``parts[first]`` on the list of all its attributes."""
    tag = tuple((part.attribute, part.raw) for part in parts[first:] if part.kind == _ATTRIBUTE)
    for index in range(first, len(parts)):
        if parts[index].kind == _ATTRIBUTE:
            parts[index] = replace(parts[index], tag=tag)


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


def _plain_shown(text: str, *, data: bool, numbers: bool) -> bool:
    """Whether a plainly visible value (:func:`plain_visible`) is shown where it stands: never in data, and a bare
    number only where ``numbers`` says it is syntax or a structural setting."""
    if data or not plain_visible(text):
        return False
    return numbers or _NUMBER.fullmatch(text.strip()) is None


def _number_slot(part: _Part) -> bool:
    """Whether the Apigee attribute or text ``part`` (outside data, in the policy or endpoint itself) holds a
    structural number (:data:`NUMBER_TEXT_SLOTS`, :data:`NUMBER_ATTRIBUTE_SLOTS`)."""
    if part.kind == _ATTRIBUTE:
        path = part.path
        return part.attribute in NUMBER_ATTRIBUTE_SLOTS.get(ROOT if len(path) == 1 else path[-1], frozenset())
    return part.kind == _TEXT and part.element in NUMBER_TEXT_SLOTS


def _dataweave_script(part: _Part) -> bool:
    """Whether the Mule text or CDATA ``part`` is a DataWeave script (:data:`DATAWEAVE_SCRIPT_ELEMENTS`)."""
    return part.kind in (_TEXT, _CDATA) and len(part.path) >= 2 and part.path[-2:][::-1] in DATAWEAVE_SCRIPT_ELEMENTS


def _mule_number_slot(part: _Part) -> bool:
    """Whether the Mule attribute ``part`` holds a structural number a2m generates (:data:`MULE_NUMBER_ATTRIBUTES`,
    or the value of a2m's own status variable)."""
    name = _local(part.attribute)
    if name in MULE_NUMBER_ATTRIBUTES:
        return True
    return (
        part.element == "set-variable"
        and name == "value"
        and any(_local(key) == "variableName" and raw == MULE_STATUS_VARIABLE for key, raw in part.tag)
    )


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
        # For each Mule file shown: where each part a2m changed stands (:data:`_Where`) -> that part as shown.
        self._shown: dict[str, dict[_Where, _Shown]] = {}
        # Number placeholders («nN»): the language their literal was written in, and the literal as an exact DataWeave
        # and JSON number (:func:`_number_form`), None when it cannot be written as one.
        self._number_origins: dict[str, str] = {}
        self._number_forms: dict[str, str | None] = {}
        self._names: set[str] = set()
        # Every spelling of a value a2m saw in the source (a string literal's escapes, the bytes of a Mule part) or
        # wrote back into an answer, by placeholder: the sweep replaces these too (:meth:`sweep`).
        self._spelled: dict[str, set[str]] = {}
        # The sweep's index: every spelling of every value it replaces -> the placeholder; the spellings by their first
        # :data:`MIN_SWEPT_CHARS` characters, longest first; the placeholders indexed so far.
        self._sweep_index: dict[str, str] = {}
        self._sweep_buckets: dict[str, list[str]] = {}
        self._sweep_unsorted: set[str] = set()
        self._swept: set[str] = set()
        # The letters and digits of each value at least MIN_HELD_CHARS of them long (:meth:`holds_value`).
        self._cores: dict[str, str] = {}
        # The pattern of the names a2m shows the AI anyway (:meth:`without_names`), for the set of names it was made
        # from; and each value's letters and digits with those names taken out (:meth:`holds_unshown_value`), for the
        # pattern they were made with.
        self._mentioned: set[str] = set()
        self._names_key: frozenset[str] | None = None
        self._names_regex: re.Pattern[str] | None = None
        self._unnamed_pattern: re.Pattern[str] | None = None
        self._unnamed_cores: dict[str, str] = {}
        # Placeholders of stretches of code a2m could not read for sure and showed whole (:data:`_S_OPEN`): put back
        # only unchanged where a2m showed them, never in a string, as text or anywhere else.
        self._stretches: set[str] = set()
        # While a Mule part is shown: each placeholder shown in it, in order, as [placeholder, the original bytes it
        # stands for, chars of its stretch before it, chars after it, kind of stretch] (:meth:`_record`).
        self._emitted: list[list[object]] | None = None
        self._nested_xml = False
        # While a changed part is restored: the stand-in of each placeholder left unchanged -> (its original bytes as
        # the part reads, the placeholder the answer wrote) (:meth:`_part_values`).
        self._fixed: dict[str, tuple[str, str]] = {}
        # For each Mule file shown: its value parts as shown, in order (:func:`_shown_parts`), to pair an answer's parts
        # with them (:func:`_pairing`).
        self._shown_parts: dict[str, list[_Part]] = {}
        # While the contexts of a part's placeholders are read (:meth:`_contexts`): the chain of places the reader is in
        # (the language of the code, "#[", a string and its quote, a regular expression ...) and, for each placeholder
        # read, that chain with the kind of place it stands in.
        self._chain: list[str] = []
        self._probe: list[tuple[str, ...]] | None = None
        self.know(names)

    @property
    def values(self) -> Mapping[str, str]:
        """Each placeholder and the value it stands for."""
        return self._values

    def token(self, value: str) -> str:
        """The placeholder of ``value`` (a new one the first time it is seen)."""
        return self._token(value, value)

    def _token(self, key: str, value: str, form: str = TOKEN_FORMAT, raw: str | None = None) -> str:
        """The placeholder of ``key`` (``value``: what it stands for); while a Mule part is shown, remembered with
        ``raw``, the bytes it replaces there (``value`` when None)."""
        found = self._tokens.get(key)
        if found is None:
            found = form.format(number=len(self._tokens) + 1)
            self._tokens[key] = found
            self._values[found] = value
        if raw is not None and raw != value:
            self._note(found, raw)
        if self._emitted is not None:
            self._emitted.append([found, value if raw is None else raw, 0, 0, _PLAIN])
        return found

    def _number_token(self, raw: str, language: str) -> str:
        """The number placeholder («nN») of the number literal ``raw`` written in ``language`` (code, JSON text or
        :data:`CONDITION`): the same literal of the same language always gets the same one, never a text's."""
        token = self._token(f"\x00n\x00{language}\x00{raw}", raw, NUMBER_TOKEN_FORMAT)
        if token not in self._number_origins:
            self._number_origins[token] = language
            self._number_forms[token] = _number_form(raw, language)
        return token

    def is_number(self, token: str) -> bool:
        """Whether ``token`` is a number placeholder this table showed («nN»)."""
        return token in self._number_origins

    def numbers_in(self, text: str) -> list[str]:
        """The number placeholders this table showed that ``text`` uses, in order of first use."""
        found: list[str] = []
        for match in NUMBER_TOKEN.finditer(text):
            if match.group(0) in self._number_origins and match.group(0) not in found:
                found.append(match.group(0))
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
            pieces = self._render(xml_text, apigee=True, depth=0, records=None)
        except _LexError:
            return None
        return "".join(piece for piece in pieces if isinstance(piece, str))

    def mule(self, files: Mapping[str, str]) -> dict[str, str | None]:
        """Each Mule configuration file (path: text) as the AI is shown it (None when it cannot be read as XML).
        Their comments are swept last, with every value the table knows by then."""
        rendered: dict[str, list[str | _Part] | None] = {}
        for name, text in files.items():
            records: dict[_Where, _Shown] = {}
            if not _readable_xml(text):
                rendered[name] = None
                continue
            try:
                rendered[name] = self._render(text, apigee=False, depth=0, records=records)
            except _LexError:
                rendered[name] = None
                continue
            self._shown[name] = records
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
                    self._shown[name][piece.where] = _Shown(_COMMENT, "", comment, piece.raw, comment, None)
                out.append(comment)
            text = shown[name] = "".join(out)
            try:
                self._shown_parts[name] = [part for part in _lex(text) if part.kind != _MARKUP]
            except _LexError:
                self._shown_parts[name] = []
        return shown

    def code(self, text: str, kind: ItemKind) -> str:
        """Custom code (``kind``: :attr:`ItemKind.JAVASCRIPT`, :attr:`ItemKind.PYTHON` or :attr:`ItemKind.JAVA`)
        as the AI is shown it."""
        return self._code(text, kind.value if kind in CODE_KINDS else JAVASCRIPT)

    def condition(self, text: str) -> str:
        """An Apigee condition as the AI is shown it: variables and operators kept, every value a placeholder (every
        number too, wherever it stands: a variable name never starts like one)."""
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
            elif token.kind is TokenKind.WORD and condition_number(token.text):
                # A number as a2m's condition translator reads one: a number placeholder, so the AI sees it is one.
                out.append(text[position : token.position] + self._number_token(token.text, CONDITION))
                position = token.position + len(token.text)
            elif token.kind is TokenKind.WORD and (
                TOKEN_MARK in token.text
                or _numeric_word(token.text)
                or (_condition_value(tokens, index) and not plain_visible(token.text))
            ):
                out.append(text[position : token.position] + self.token(token.text))
                position = token.position + len(token.text)
        out.append(text[position:])
        return "".join(out)

    def diff(self, text: str) -> str:
        """Failing-test diff text as the AI is shown it, default deny, line by line (the lines of
        :mod:`a2m.verify.compare` and :mod:`a2m.verify.harness`): every quoted value, every request target
        (:meth:`_url`), every number (a number placeholder, the same as the same JSON number in a policy), every
        JSON key of a field path that is not a known name or a plain word of letters (a key with a digit or any
        other character; an array index is a number) and ``true`` and ``false`` (data) a placeholder. What stays:
        a2m's own words, ``null``, a status code and a count of calls or fields (a2m's expectations), a header name
        (an HTTP token), a query parameter name as a URL shows it, and an HTTP method. A JSON key holding a line end splits its
        line; the parts are read as one line. The HTTP client's error of a call with no valid response, and a line
        of no shape a2m writes, keep their words, with every quoted value, request target and number a placeholder.
        A ``- test`` line (the test's name and policy) is shown as before: its quoted values and request targets are
        placeholders."""
        lines = text.split("\n")
        # For each line, the first line from it on that holds ": expected " (None: no such line): a JSON key that holds
        # a line end splits its diff line, and the parts are read as one line.
        separated: list[int | None] = [None] * (len(lines) + 1)
        for number in range(len(lines) - 1, -1, -1):
            separated[number] = number if _DIFF_SEPARATOR in lines[number] else separated[number + 1]
        out: list[str] = []
        number = 0
        while number < len(lines):
            body = lines[number].lstrip(" ")
            indent = lines[number][: len(lines[number]) - len(body)]
            number += 1
            if body.startswith("- test "):
                out.append(indent + self._diff_free(body, words=None, numbers=True))
                continue
            named = _DIFF_NAMED.match(body, _diff_call_end(body))
            last = separated[number]
            if named is not None and _DIFF_SEPARATOR not in body[named.end() :] and last is not None:
                body = "\n".join([body, *lines[number : last + 1]])
                number = last + 1
            out.append(indent + self._diff_line(body))
        return "\n".join(out)

    def _diff_line(self, line: str) -> str:
        """One diff line (without its indent) as the AI is shown it (see :meth:`diff`)."""
        prefix = ""
        call = _DIFF_CALL.match(line)
        if call is not None:
            prefix, line = call.group(0), line[call.end() :]
            if line.startswith(_DIFF_NO_RESPONSE):
                # The HTTP client's own error text: its words stay, every value and number is a placeholder.
                rest = line[len(_DIFF_NO_RESPONSE) :]
                return prefix + _DIFF_NO_RESPONSE + self._diff_free(rest, words=None, numbers=False)
            if not line.startswith(" "):
                return prefix + self._diff_free(line, words=None, numbers=False)
            prefix, line = prefix + " ", line[1:]
        if _DIFF_FIXED_LINE.fullmatch(line):
            return prefix + line
        named = _DIFF_NAMED.match(line)
        at = line.find(_DIFF_SEPARATOR, named.end()) if named is not None else -1
        if named is not None and at >= 0:
            name = line[named.end() : at]
            body, header, query = named.groups()
            if body is not None:
                part = f"{body} field {self._diff_path(name)}"
            elif header is not None:
                # A header name stays (an HTTP token, as a2m reads a header name; the module docstring's diff words).
                shown = name if _HEADER_TOKEN.fullmatch(name) else self._hide(name)
                part = f"{header} {shown}"
            else:
                # A query parameter name as a URL shows it (the request target line above shows the same names).
                shown = name if name in self._names or _KEY_NAME.fullmatch(name) else self._hide(name)
                part = f"{query} query {shown}"
            return f"{prefix}{part}: {self._diff_values(line[at + 2 :])}"
        whole = _DIFF_WHOLE_BODY.match(line)
        if whole is not None:
            return prefix + whole.group(0) + self._diff_values(line[whole.end() :])
        found = _DIFF_REQUEST_TARGET.fullmatch(line)
        if found is not None:
            where, want_method, want, got_method, got = found.groups()
            return (
                f"{prefix}{where}: expected {self._diff_method(want_method)} {self._diff_target(want)}, "
                f"got {self._diff_method(got_method)} {self._diff_target(got)}"
            )
        # A line of no shape a2m writes: its words stay, every quoted value, request target and number is a placeholder.
        return prefix + self._diff_free(line, words=None, numbers=False)

    def _diff_values(self, text: str) -> str:
        """The ``expected X, actual Y`` part of a diff line: a2m's words stay, every value is a placeholder."""
        return self._diff_free(text, words=_DIFF_VALUE_WORDS, numbers=False)

    def _diff_path(self, path: str) -> str:
        """A JSON field path of a diff (``items[0].pin``): each key a placeholder unless it is a known name or a
        standard header name, each array index a number placeholder, the separators kept."""
        out: list[str] = []
        for piece in _DIFF_PATH_SPLIT.split(path):
            if piece == ".":
                out.append(piece)
            elif piece.startswith("[") and piece.endswith("]") and piece[1:-1].isdigit():
                out.append("[" + self._number_token(piece[1:-1], JSON_TEXT) + "]")
            else:
                out.append(self._diff_key(piece))
        return "".join(out)

    def _diff_key(self, name: str) -> str:
        """A JSON key of a diff: shown when it is a known name or a plain word (:data:`_PLAIN_KEY`: letters only, no
        digit, as a field is named), else one placeholder (a key with a digit or any other character, such as an API
        key used as an object key, or a masked value)."""
        if not name.strip():
            return name
        if TOKEN_MARK not in name and (_PLAIN_KEY.fullmatch(name) or name in self._names):
            return name
        return self._whole(name)

    def _diff_method(self, method: str) -> str:
        return method if method in HTTP_METHODS else self._whole(method)

    def _diff_target(self, target: str) -> str:
        if not target:
            return target
        if target.startswith("/") or _URL_START.match(target):
            return self._url(target)
        return self._whole(target)

    def _diff_free(self, text: str, *, words: frozenset[str] | None, numbers: bool) -> str:
        """Diff text read character by character: every quoted value and request target a placeholder; every number
        a number placeholder unless ``numbers``; every word not in ``words`` a placeholder (``null`` stays, ``true``
        and ``false`` are data) unless ``words`` is None; punctuation kept."""
        out: list[str] = []
        index, size = 0, len(text)
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
                out.append(char + self._whole(_decoded(literal, content)) + char)
                index = after
                continue
            target = _DIFF_TARGET.match(text, index) if char == "/" or char.isalpha() else None
            if target is not None and (char == "/" or "://" in target.group(0)) and (index == 0 or not text[index - 1].isalnum()):
                out.append(self._url(target.group(0)))
                index = target.end()
                continue
            joined = index > 0 and (text[index - 1].isalnum() or text[index - 1] == "_")
            if (
                not numbers
                and not joined
                and (char.isdigit() or (char in "+-." and index + 1 < size and text[index + 1].isdigit()))
            ):
                number = _JSON_NUMBER.match(text, index)
                if number is not None:
                    out.append(self._number_token(number.group(0), JSON_TEXT))
                    index = number.end()
                    continue
            if words is not None and (char.isalnum() or char == "_"):
                word = _DIFF_WORD.match(text, index)
                if word is not None:
                    found = word.group(0)
                    shown = found in words or found == "null" or (numbers and found.isdigit())
                    out.append(found if shown else self.token(found))
                    index = word.end()
                    continue
            out.append(self.token(char) if char == TOKEN_MARK else char)
            index += 1
        return "".join(out)

    def sweep(self, text: str) -> str:
        """``text`` (free text a2m or a tool wrote: a comment, a refusal reason, a build or deploy error) with every
        value the table knows (at least :data:`MIN_SWEPT_CHARS` long) replaced by its placeholder, in every spelling
        a2m knows of it: as it is; as the content of a string literal of DataWeave, JSON, JavaScript, Java and Python
        with either quote; JSON-, repr- and URL-encoded; each spelling the source used or a2m wrote back into an
        answer (:meth:`_note`); and each of those once or twice more XML-escaped, repr-quoted (backslashes doubled,
        either quote escaped) or JSON-quoted, as a reason that quotes a restored expression with ``!r`` spells it. A
        spelling that starts or ends with a letter or digit is replaced only where no letter or digit joins it there.
        A placeholder already in ``text`` is left as it is (a value ``v10`` never rewrites ``«v10»``), so sweeping a
        swept text changes nothing.
        """
        if not text:
            return text
        self._index_values()
        if not self._sweep_buckets:
            return text
        for key in self._sweep_unsorted:
            self._sweep_buckets[key].sort(key=lambda form: (-len(form), form))
        self._sweep_unsorted.clear()
        pieces = TOKEN_SPLIT.split(text)
        return "".join(piece if number % 2 else self._sweep_plain(piece) for number, piece in enumerate(pieces))

    def _sweep_plain(self, text: str) -> str:
        """:meth:`sweep` of ``text``, which holds no placeholder (the index is ready and sorted)."""
        buckets = self._sweep_buckets
        index = self._sweep_index
        out: list[str] = []
        position = start = 0
        size = len(text)
        while position < size:
            for form in buckets.get(text[position : position + MIN_SWEPT_CHARS], ()):
                end = position + len(form)
                if (
                    text.startswith(form, position)
                    and not (form[0].isalnum() and position > 0 and text[position - 1] in _ASCII_ALNUM)
                    and not (form[-1].isalnum() and end < size and text[end] in _ASCII_ALNUM)
                ):
                    out.append(text[start:position])
                    out.append(index[form])
                    position = start = end
                    break
            else:
                position += 1
        out.append(text[start:])
        return "".join(out)

    def holds_value(self, text: str) -> bool:
        """Whether ``text`` (a swept text) may still hold a value of the table in a spelling the sweep does not know:
        the letters and digits of a value (at least :data:`MIN_HELD_CHARS` of them) stand in order, with nothing but
        other characters between them, among the letters and digits of ``text``, read after its escapes (``\\uXXXX``,
        ``\\xXX``, ``%XX``, XML references) and without its placeholders. A text for which this is true is not
        shown to the AI."""
        skeleton = _skeleton(TOKEN_SPLIT.sub(" ", text))
        if len(skeleton) < MIN_HELD_CHARS:
            return False
        for token, value in self._values.items():
            core = self._cores.get(token)
            if core is None:
                core = self._cores[token] = "".join(char for char in value if char.isalnum())
            if len(core) >= MIN_HELD_CHARS and core in skeleton:
                return True
        return False

    def mention(self, names: Iterable[str]) -> None:
        """Add ``names`` to the names a2m shows the AI anyway, whatever they are written like (the proxy's name, which
        the fix prompt names): :meth:`without_names` takes them out as it takes out the known names. Nothing is shown
        because of it, and none of them becomes a known name."""
        self._mentioned.update(name for name in names if name)

    def without_names(self, text: str, names: Iterable[str] = ()) -> str:
        """``text`` with every whole occurrence of a name a2m shows the AI anyway taken out (a blank in its place):
        each known name, each name given with :meth:`mention` and each of ``names`` with at least
        :data:`MIN_HELD_CHARS` letters and digits. Whole: no letter, digit, ``_`` or ``-`` joins it on either side
        (``orders-api:`` and ``orders-api.xml`` hold the name ``orders-api``; ``orders-apiX9`` does not)."""
        pattern = self._names_pattern(names)
        return text if pattern is None else pattern.sub(" ", text)

    def holds_unshown_value(self, text: str, names: Iterable[str] = ()) -> bool:
        """:meth:`holds_value` of ``text`` (a swept text) once the names a2m shows the AI anyway are taken out of it
        (:meth:`without_names`): a value whose letters and digits stand only inside such a name (a base path
        ``/orders`` inside the proxy name ``orders-api``) is not held, since the AI reads that name anyway. A value
        that holds such a name is also looked for with the name taken out of it the same way, so the rest of it is
        still found. Every other value is looked for exactly as :meth:`holds_value` looks for it."""
        names = tuple(names)
        pattern = self._names_pattern(names)
        if pattern is None:
            return self.holds_value(text)
        skeleton = _skeleton(TOKEN_SPLIT.sub(" ", pattern.sub(" ", text)))
        if len(skeleton) < MIN_HELD_CHARS:
            return False
        if self._unnamed_pattern is not pattern:
            self._unnamed_pattern, self._unnamed_cores = pattern, {}
        for token, value in self._values.items():
            core = self._cores.get(token)
            if core is None:
                core = self._cores[token] = "".join(char for char in value if char.isalnum())
            if len(core) >= MIN_HELD_CHARS and core in skeleton:
                return True
            unnamed = self._unnamed_cores.get(token)
            if unnamed is None:
                unnamed = self._unnamed_cores[token] = "".join(char for char in pattern.sub(" ", value) if char.isalnum())
            if unnamed != core and len(unnamed) >= MIN_HELD_CHARS and unnamed in skeleton:
                return True
        return False

    def _names_pattern(self, names: Iterable[str]) -> re.Pattern[str] | None:
        """The pattern of :meth:`without_names` for the known names and ``names`` (cached while they stay the same),
        None when there is no such name."""
        chosen = frozenset(
            name
            for name in (*self._names, *self._mentioned, *names)
            if name and TOKEN_MARK not in name and sum(char.isalnum() for char in name) >= MIN_HELD_CHARS
        )
        if self._names_key != chosen:
            self._names_key = chosen
            ordered = sorted(chosen, key=lambda name: (-len(name), name))
            self._names_regex = (
                re.compile(r"(?<![\w\-])(?:" + "|".join(re.escape(name) for name in ordered) + r")(?![\w\-])")
                if ordered
                else None
            )
        return self._names_regex

    def _note(self, token: str, *forms: str) -> None:
        """Remember ``forms`` as spellings of the value of ``token`` (seen in the source or written back), so the
        sweep replaces them too."""
        spelled = self._spelled.setdefault(token, set())
        new = [form for form in forms if form not in spelled]
        if not new:
            return
        spelled.update(new)
        if token in self._swept and len(self._values.get(token, "").strip()) >= MIN_SWEPT_CHARS:
            self._index_forms(token, new)

    def _index_values(self) -> None:
        """Index the spellings of every value not indexed yet (in the order the values were met)."""
        if len(self._swept) == len(self._values):
            return
        for token, value in self._values.items():
            if token in self._swept:
                continue
            self._swept.add(token)
            if len(value.strip()) < MIN_SWEPT_CHARS:
                continue
            self._index_forms(token, {*_spellings_of(value), *self._spelled.get(token, ())})

    def _index_forms(self, token: str, forms: Iterable[str]) -> None:
        index, buckets = self._sweep_index, self._sweep_buckets
        for form in _quoted_twice(forms):
            if len(form) < MIN_SWEPT_CHARS or form in index:
                continue
            index[form] = token
            key = form[:MIN_SWEPT_CHARS]
            buckets.setdefault(key, []).append(form)
            self._sweep_unsorted.add(key)

    # ------------------------------------------------------------ restoring an answer

    def restore(self, name: str, answer: str) -> str:
        """``answer`` (the AI's text of the Mule file ``name``) with every placeholder written back; raises
        :class:`PlaceholderError` (see the module docstring).

        Each part of the answer is paired with a part a2m showed by aligning the two sequences of parts
        (:func:`_pairing`: inserted or deleted elements do not shift the others; a part with no pair is new). A part
        left exactly as shown is its original bytes. In a changed part, each placeholder whose shown stretch is
        unchanged at the same aligned position and read in the same context (:meth:`_unchanged`) is its original
        bytes, never read again; every other placeholder is written back for where it stands, by what that place is,
        never by what the answer happens to write there."""
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
        records = self._shown.get(name, {})
        ambiguous: set[int] = set()
        pairs = _pairing(self._shown_parts.get(name, []), parts, ambiguous) if records else {}
        out: list[str] = []
        for index, part in enumerate(parts):
            if TOKEN.search(part.raw) is None:
                out.append(part.raw)
                continue
            if part.kind == _MARKUP:
                raise PlaceholderError("has a placeholder outside any value (in a tag), where no value can stand")
            where = pairs.get(index)
            record = None if where is None else records.get(where)
            if record is not None and (record.kind, record.quote, record.raw) == (part.kind, part.quote, part.raw):
                out.append(record.original)
                continue
            if part.kind == _COMMENT:
                raise PlaceholderError(
                    "has a placeholder in a comment that is not exactly as a2m showed it in that place"
                )
            fixed, crowded = self._unchanged(record, part) if record is not None else ({}, False)
            try:
                values = self._part_values(part, apigee=False, depth=0, fixed=fixed)
            except PlaceholderError as exc:
                if crowded:
                    raise PlaceholderError(_crowded_reason()) from None
                if index in ambiguous:
                    raise PlaceholderError(_ambiguous_reason(str(exc))) from None
                raise
            out.append(_joined(part.raw, values))
        return "".join(out)

    def _unchanged(self, record: _Shown, part: _Part) -> tuple[dict[int, tuple[str, str | None]], bool]:
        """The placeholders of the answer's ``part`` (by their index in it) left unchanged from ``record``, the part
        a2m showed paired with it, each with the original bytes it stands for as the part reads and as written (with
        the new quote escaped when the answer changed the attribute's quote character); and whether a placeholder of
        the part could not be lined up because the part changed too much at once (:func:`_alignment`).

        The part's text is aligned with the shown text (:func:`_alignment`). A placeholder is unchanged when it is
        aligned with one a2m showed, is read in the same context there (:meth:`_contexts`: the same language and the
        same chain of places around it, such as ``#[...]`` and a string with its quote), and its whole stretch
        (:class:`_Placed`) is aligned with the token right before it (and right after it, for one standing alone in
        code). In plain text on both sides (outside every ``#[...]`` of a value that is not code,
        :func:`_plain_flags`), where nothing around it is syntax, its alignment and context alone."""
        if record.placed is None or record.kind != part.kind:
            return {}, False
        pieces = TOKEN_SPLIT.split(part.raw)
        text = part.raw if part.kind == _CDATA else "".join(
            piece if index % 2 else _unescape(piece) for index, piece in enumerate(pieces)
        )
        shown, written = _DIFF_TOKEN.findall(record.text), _DIFF_TOKEN.findall(text)
        if len(shown) > MAX_DIFF_TOKENS or len(written) > MAX_DIFF_TOKENS:
            return {}, True
        shown_contexts, written_contexts = self._contexts(record.text, part), self._contexts(text, part)
        if shown_contexts is None or written_contexts is None:
            return {}, False
        aligned, crowded = _alignment(shown, written)
        starts: list[int] = []  # where each shown token starts in the shown text
        ordinals: dict[int, int] = {}  # shown token index -> index of that placeholder in the shown part
        offset = 0
        for index, token in enumerate(shown):
            starts.append(offset)
            if TOKEN.fullmatch(token):
                ordinals[index] = len(ordinals)
            offset += len(token)
        plain = _plain_flags(text, part)
        requote = part.kind == _ATTRIBUTE and part.quote != record.quote
        fixed: dict[int, tuple[str, str | None]] = {}
        lost = False
        probes = 0
        number = -1
        for index, token in enumerate(written):
            if not TOKEN.fullmatch(token):
                continue
            number += 1
            at = aligned[index]
            if at is None or at not in ordinals:
                lost = lost or crowded[index]
                continue
            placed = record.placed[ordinals[at]]
            end = bisect.bisect_left(starts, placed.end)  # one past the stretch's last token
            if placed.kind == _PLAIN and plain[number]:
                kept = True
            else:
                first = bisect.bisect_right(starts, placed.start) - 1
                last = end + 1 if placed.kind == _BARE or (placed.kind == _PLAIN and not plain[number]) else end
                kept = all(
                    _same(aligned, index + (other - at), other, len(shown)) for other in range(first - 1, last)
                )
            context = shown_contexts[ordinals[at]]
            if kept and context != written_contexts[number]:
                # Read in another context. When either reading is a stretch a2m cannot read for sure (code after it
                # on the line made the lexer give up, which never changes what came before), it is the same context
                # only if the answer's text up to the end of the stretch, followed by the rest of what a2m showed,
                # reads it as a2m showed it.
                kept = False
                if probes < MAX_OPEN_PROBES and (_open_place(context) or _open_place(written_contexts[number])):
                    probes += 1
                    cut = index + (end - at)
                    hybrid = self._contexts("".join(written[:cut]) + "".join(shown[end:]), part)
                    kept = hybrid is not None and hybrid[number] == context
            if kept:
                fixed[number] = (placed.value, _requoted(placed.raw, part.quote) if requote else placed.raw)
            else:
                lost = lost or any(crowded[max(0, index - 1) : index + 2])
        return fixed, lost

    def _contexts(self, text: str, part: _Part) -> list[tuple[str, ...]] | None:
        """For each placeholder of the Mule value ``text`` (``part``'s text as it reads), in order, the context it is
        read in when the answer is restored (:meth:`_put_value`): the chain of places around it (the language of the
        code, "#[", a string and its quote, a ``$( )`` interpolation, a regular expression, a comment, embedded XML)
        and the kind of place it stands in (plain text, a string literal, alone in code ...). None when a2m cannot
        read a context for each one."""
        tokens = [match.group(0) for match in TOKEN.finditer(text)]
        saved, chain = self._fixed, self._chain
        self._fixed, self._chain, self._probe = {token: ("", token) for token in tokens}, [], []
        try:
            self._put_value(text, apigee=False, depth=0, script=_dataweave_script(part))
            found: list[tuple[str, ...]] | None = self._probe
        except PlaceholderError:
            found = None
        finally:
            self._fixed, self._chain, self._probe = saved, chain, None
        return found if found is not None and len(found) == len(tokens) else None

    def _held(self, token: str, place: str) -> str:
        """The original bytes of ``token``, a placeholder left unchanged (:attr:`_fixed`), read at a ``place`` of
        the kind given; while contexts are read (:meth:`_contexts`), that place is noted with the chain around it."""
        if self._probe is not None:
            self._probe.append((*self._chain, place))
        return self._fixed[token][0]

    def _written(self, text: str) -> str:
        """``text`` (a piece of a part being restored) with each stand-in of a placeholder left unchanged
        (:attr:`_fixed`) replaced by the placeholder the answer wrote there, to quote it in a reason."""
        return TOKEN.sub(lambda match: self._fixed.get(match.group(0), ("", match.group(0)))[1], text)

    @contextmanager
    def _inside(self, place: str) -> Iterator[None]:
        """Read what follows inside ``place`` (a language, "#[", a string ...): noted in the chain of contexts."""
        self._chain.append(place)
        try:
            yield
        finally:
            self._chain.pop()

    def _refuse_unknown(self, text: str) -> None:
        unknown = sorted({match.group(0) for match in TOKEN.finditer(text) if match.group(0) not in self._values})
        if unknown:
            raise PlaceholderError(
                f"uses the placeholder {', '.join(unknown[:5])}, which stands for no value a2m showed"
            )

    def restore_code(self, text: str, language: str = DATAWEAVE) -> str:
        """``text`` (code in ``language`` the AI wrote, such as a DataWeave expression) with every placeholder written
        back spelled for the string literal it stands in; raises :class:`PlaceholderError` for an unknown placeholder
        or one outside any string literal."""
        if TOKEN.search(text) is None:
            return text
        self._refuse_unknown(text)
        values = self._put_code(text, language)
        if len(values) != len(TOKEN.findall(text)):
            raise PlaceholderError("has a placeholder a2m cannot tell the place of, so it cannot be put back")
        for match, value in zip(TOKEN.finditer(text), values, strict=True):
            self._note(match.group(0), value)
        return _joined(text, values)

    def restore_plain(self, text: str) -> str:
        """``text`` (one value the AI wrote as data, such as a literal of a condition tree) with every placeholder
        replaced by the exact value it stands for; raises :class:`PlaceholderError` for an unknown placeholder or one
        that stands for a piece of code a2m could only show whole."""
        if TOKEN.search(text) is None:
            return text
        self._refuse_unknown(text)
        numbers = self.numbers_in(text)
        if numbers:
            raise PlaceholderError(
                f"uses {numbers[0]} as text, but it stands for a number; a2m does not turn a number into text"
            )
        return TOKEN.sub(lambda match: self._plain(match.group(0)), text)

    def _part_values(
        self, part: _Part, *, apigee: bool, depth: int, fixed: Mapping[int, tuple[str, str | None]] | None = None
    ) -> list[str]:
        """What each placeholder in ``part`` (an attribute value, a text or a CDATA section with placeholders) is
        written back as, XML-escaped for the part, in order. ``fixed``: the placeholders (by index) left unchanged
        from what a2m showed there (:meth:`_unchanged`), each its original bytes: the part is read with a stand-in
        for each, which every rule takes as that value, never refused."""
        pieces = TOKEN_SPLIT.split(part.raw)
        readable = [piece if index % 2 or part.kind == _CDATA else _unescape(piece) for index, piece in enumerate(pieces)]
        fixed = fixed or {}
        stand_ins: list[str] = []
        for number, (value, _raw) in fixed.items():
            stand_in = TOKEN_FORMAT.format(number=len(self._values) + 1 + number)
            self._fixed[stand_in] = (value, pieces[2 * number + 1])
            readable[2 * number + 1] = stand_in
            stand_ins.append(stand_in)
        logical = "".join(readable)
        try:
            script = not apigee and depth == 0 and _dataweave_script(part)
            values = self._put_value(logical, apigee=apigee, depth=depth, script=script)
            if len(values) != len(pieces) // 2:
                raise PlaceholderError("has a placeholder a2m cannot tell the place of, so it cannot be put back")
            if not apigee and depth == 0 and not script and not logical.strip().startswith("%dw"):
                self._refuse_new_expression(logical, values)
        finally:
            for stand_in in stand_ins:
                del self._fixed[stand_in]
        out: list[str] = []
        for number, (token, value) in enumerate(zip(pieces[1::2], values, strict=True)):
            if number in fixed:
                value, raw = fixed[number]
                if raw is not None:
                    out.append(raw)  # the original bytes, as the part wrote them
                    continue
            if part.kind == _ATTRIBUTE:
                out.append(_escape_attribute(value, part.quote))
            elif part.kind == _CDATA:
                if "]]>" in value:
                    raise PlaceholderError(f"puts {token} in a CDATA section, which cannot hold its value")
                out.append(value)
            else:
                out.append(_escape_text(value))
        for token, value, written in zip(pieces[1::2], values, out, strict=True):
            self._note(token, value, written)
        return out

    def _refuse_new_expression(self, logical: str, values: Sequence[str]) -> None:
        """Refuse an answer whose placeholders, written back outside every ``#[...]`` of the Mule part ``logical``
        (an attribute value or text that is not a DataWeave script), would start a Mule expression there: a value
        holding "#[", or a value that makes "#[" with the text or value next to it. The one exception is a "#[" that
        lies wholly inside one value left unchanged from what a2m showed there (:attr:`_fixed`). A plain value is
        never turned into code."""
        pieces = TOKEN_SPLIT.split(logical)
        plain = set(_plain_tokens(logical))
        restored: list[str] = []
        spans: list[tuple[int, int, str]] = []  # (start, end) in the restored text of each plain value, placeholder
        size = 0
        for index, piece in enumerate(pieces):
            value = values[index // 2] if index % 2 else piece
            if index % 2 and index in plain:
                spans.append((size, size + len(value), piece))
            restored.append(value)
            size += len(value)
        text = "".join(restored)
        for start, end, token in spans:
            written = self._fixed[token][1] if token in self._fixed else token
            for match in re.finditer(r"#\[", text[max(0, start - 1) : end + 1]):
                at = max(0, start - 1) + match.start()
                if token in self._fixed and start <= at and at + 2 <= end:
                    continue
                raise PlaceholderError(_hash_reason(written))
            # A "${" the value makes, or one before it that is still open where it starts: Mule would read the
            # value as (part of) a property name. Only a whole "${...}" inside one value left unchanged stays.
            for match in re.finditer(r"\$\{", text[max(0, start - 1) : end + 1]):
                at = max(0, start - 1) + match.start()
                close = text.find("}", at + 2)
                if token in self._fixed and start <= at and 0 <= close < end:
                    continue
                raise PlaceholderError(_property_reason(written))
            opener = text.rfind("${", 0, start)
            if opener >= 0 and text.find("}", opener + 2, start) < 0:
                raise PlaceholderError(_property_reason(written))

    def _put_value(self, text: str, *, apigee: bool, depth: int, script: bool = False) -> list[str]:
        """What each placeholder in ``text`` (one value, read as :meth:`_value` reads it) is written back as: by what
        the value is, never by what the answer wrote in it. ``script``: the text of a Transform Message part, a
        DataWeave script whether or not it starts with ``%dw``."""
        core = text.strip()
        if not apigee:
            if script or core.startswith("%dw"):
                return self._put_code(core, DATAWEAVE)
            if "#[" in core or ("${" in core and not (depth < MAX_DEPTH and _json_shape(core))):
                return self._put_template(core)
        if depth < MAX_DEPTH and core:
            if core.startswith("<"):
                with self._inside("xml"):
                    nested = self._put_xml(core, depth + 1)
                if nested is not None:
                    return nested
                # It starts like XML but a2m cannot read it as XML: whether its values are XML text or plain text is
                # not known, so only a placeholder left as shown there is put back.
                for match in TOKEN.finditer(core):
                    if match.group(0) not in self._fixed:
                        raise PlaceholderError(_unreadable_xml_reason(match.group(0)))
            if _json_shape(core):
                return self._put_code(core, JSON_TEXT)
        return self._put_text(core)

    def _put_text(self, text: str, start: int = 0, end: int | None = None) -> list[str]:
        """What each placeholder in ``text[start:end]`` (plain text: an attribute value, an element text, a comment)
        is written back as: its value, a number as plain digits (:meth:`_plain`). A number placeholder must stand
        alone (:func:`_touching`), or it would join what is next to it into another number or word."""
        values: list[str] = []
        for match in TOKEN.finditer(text, start, len(text) if end is None else end):
            token = match.group(0)
            if token in self._fixed:
                values.append(self._held(token, "text"))
                continue
            if token in self._number_origins and (
                _touching(text[match.start() - 1 : match.start()]) or _touching(text[match.end() : match.end() + 1])
            ):
                raise PlaceholderError(_glued_reason(token))
            values.append(self._plain(token))
        return values

    def _put_template(self, text: str) -> list[str]:
        """What each placeholder of a Mule value with ``#[...]`` expressions or ``${...}`` properties (``text``, its
        core) is written back as: in each expression as in DataWeave code (:meth:`_put_code`), in the text around
        them as plain text (:meth:`_put_text`). The expressions are found as Mule finds them
        (:func:`_template_spans`); where a2m cannot find the end of one for sure, every placeholder after its "#["
        is refused, never read as plain text. Around the expressions of a value that is JSON text as a whole
        (:func:`_json_template`), what Mule writes is JSON text, so a text placeholder there is refused unless it is
        left as shown in the same context."""
        values: list[str] = []
        spans, unsure = _template_spans(text)
        json_text = _json_template(text, spans)
        position = 0
        for start, end in spans:
            values += self._put_literal(text, position, start, json_text=json_text)
            with self._inside("#["):
                values += self._put_code(text[start + 2 : end], DATAWEAVE)
            position = end + 1
        if unsure < 0:
            return values + self._put_literal(text, position, None, json_text=json_text)
        values += self._put_literal(text, position, unsure, json_text=json_text)
        later = TOKEN.search(text, unsure)
        if later is not None:
            raise PlaceholderError(
                _expression_reason(self._written(later.group(0)), self._written(text[unsure : unsure + 60]))
            )
        return values

    def _put_literal(self, text: str, start: int, end: int | None, *, json_text: bool) -> list[str]:
        """:meth:`_put_text` for the text of a Mule template around its expressions; ``json_text``: the whole value
        is JSON text, where a text placeholder a2m cannot spell for both Mule's template and JSON is refused."""
        if not json_text:
            return self._put_text(text, start, end)
        for match in TOKEN.finditer(text, start, len(text) if end is None else end):
            token = match.group(0)
            if token not in self._fixed and token not in self._number_origins:
                raise PlaceholderError(_json_template_reason(token))
        with self._inside("json template"):
            return self._put_text(text, start, end)

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
                for match, value in zip(TOKEN.finditer(part.raw), self._put_text(part.raw), strict=True):
                    if "--" in value or value.endswith("-"):
                        raise PlaceholderError(f"puts {match.group(0)} in an XML comment, which cannot hold its value")
                    values.append(value)
                continue
            values += self._part_values(part, apigee=True, depth=depth)
        return values

    def _put_code(self, text: str, language: str) -> list[str]:
        """What each placeholder in code is written back as: spelled for its string literal (the code of a DataWeave
        ``$( )`` interpolation in it walked as code), as it is in a comment. A placeholder left unchanged from what
        a2m showed (:attr:`_fixed`) is its original bytes wherever it stands. Any other text placeholder is refused
        outside any string literal (:meth:`_bare`), in a regular expression, where a "/" may start one
        (:meth:`_put_unsure`) and after a point the lexer cannot read for sure (:func:`_open_reason`).
        Outside a string literal and comment every placeholder must stand alone (:func:`_glued`)."""
        with self._inside(language):
            return self._put_segments(text, language)

    def _put_segments(self, text: str, language: str) -> list[str]:
        """:meth:`_put_code` for the segments of ``text``."""
        values: list[str] = []
        segs = _segments(text, language)
        for position, seg in enumerate(segs):
            if seg.unsure and seg.kind in (_S_REGEX, _S_LINE):
                with self._inside("unsure " + seg.kind):
                    values += self._put_unsure(seg.content, language)
            elif seg.kind == _S_STRING:
                with self._inside("string " + seg.quote):
                    values += self._put_string(seg.content, language, seg.quote)
            elif seg.kind in (_S_LINE, _S_BLOCK):
                with self._inside(seg.kind + " " + seg.quote):
                    comment = self._put_text(seg.content)
                for match, value in zip(TOKEN.finditer(seg.content), comment, strict=True):
                    if match.group(0) not in self._fixed and (
                        ("\n" in value or "\r" in value) if seg.kind == _S_LINE else "*/" in value
                    ):
                        raise PlaceholderError(f"puts {match.group(0)} in a code comment, which cannot hold its value")
                    values.append(value)
            elif seg.kind == _S_REGEX:
                for match in TOKEN.finditer(seg.content):
                    token = match.group(0)
                    if token in self._fixed:
                        continue
                    self._refuse_stretch(token)
                    if token not in self._number_origins:
                        raise PlaceholderError(_regex_reason(token))
                with self._inside("regex"):
                    values += self._put_text(seg.content)
            elif seg.kind == _S_OPEN:
                for match in TOKEN.finditer(seg.raw):
                    token = match.group(0)
                    if token not in self._fixed:
                        at = sum(len(other.raw) for other in segs[:position])
                        line_start = text.rfind("\n", 0, at) + 1
                        line_end = text.find("\n", at)
                        line = text[line_start : len(text) if line_end < 0 else line_end].strip()
                        earlier = "\n" in seg.raw[: match.start()]
                        raise PlaceholderError(_open_reason(token, seg.why, self._written(line), earlier=earlier))
                    values.append(self._held(token, "open " + seg.why))
            elif seg.kind == _S_TOKEN:
                if seg.raw in self._fixed:
                    values.append(self._held(seg.raw, "bare"))
                    continue
                if _glued(segs, position):
                    raise PlaceholderError(_glued_reason(seg.raw))
                values.append(self._bare(seg.raw, language))
        return values

    def _put_unsure(self, content: str, language: str) -> list[str]:
        """What each placeholder in a stretch of code read at an unsure "/" (:func:`_unsure_slash`: a regular
        expression or a comment in one reading, code in the other) is written back as: as in code, since either reading
        may be the one that runs. One left unchanged is its original bytes; a number placeholder that stands alone is
        the same number (:meth:`_bare`); any other text placeholder is refused, as one outside any string literal and
        as one in a regular expression."""
        values: list[str] = []
        for match in TOKEN.finditer(content):
            token = match.group(0)
            if token in self._fixed:
                values.append(self._held(token, "unsure"))
                continue
            self._refuse_stretch(token)
            if token not in self._number_origins:
                raise PlaceholderError(
                    f"puts {token} after a \"/\" that may start a regular expression or be a division, where a2m did "
                    "not show it: a2m does not write a value into a regular expression, and as code it is outside any "
                    "string literal. a2m writes back only a stretch it showed, left exactly as shown in the same "
                    "place; keep that stretch as a2m showed it, and never move a placeholder into it"
                )
            if _touching(content[match.start() - 1 : match.start()]) or _touching(content[match.end() : match.end() + 1]):
                raise PlaceholderError(_glued_reason(token))
            values.append(self._bare(token, language))
        return values

    def _refuse_stretch(self, token: str) -> None:
        """Refuse ``token`` when it stands for a stretch of code a2m could not read for sure (:data:`_S_OPEN`): it
        is put back only where a2m showed it, unchanged (:meth:`_unchanged`), never anywhere else."""
        if token in self._stretches:
            raise PlaceholderError(_stretch_reason(token))

    def _put_string(self, content: str, language: str, quote: str) -> list[str]:
        """What each placeholder in the content of a string literal is written back as: spelled for that string in
        its literal text; in the code of a DataWeave ``$( )`` interpolation, as code (:meth:`_put_code`: a text
        placeholder there must stand in quotes of its own, a number placeholder goes back as a number)."""
        values: list[str] = []
        position = index = 0
        while language == DATAWEAVE and index < len(content):
            if content[index] == "\\":
                index += 2
                continue
            if content.startswith("$(", index):
                end = _interpolation_end(content, index + 2, language, ")", 1)
                if end < 0:
                    # The lexer followed it to close the string, so this does not happen; never read the rest as
                    # the string's text.
                    later = TOKEN.search(content, index)
                    if later is not None:
                        raise PlaceholderError(_open_reason(self._written(later.group(0)), "string"))
                    break
                values += [self._in_literal(m.group(0), language, quote) for m in TOKEN.finditer(content, position, index)]
                with self._inside("$("):
                    values += self._put_code(content[index + 2 : end], language)
                index = position = end + 1
                continue
            index += 1
        values += [self._in_literal(m.group(0), language, quote) for m in TOKEN.finditer(content, position)]
        return values

    def _bare(self, token: str, language: str) -> str:
        """What a placeholder written outside any string literal of code or JSON text is written back as: a number
        placeholder as the same number, spelled for ``language`` (:meth:`_number`). A text placeholder is refused,
        whatever its value: written bare it would be code, or a JSON word of another type (``"true"`` written bare is
        the boolean ``true``, ``"active"`` is no JSON at all), never the text it stands for."""
        if token in self._fixed:
            return self._held(token, "bare")
        self._refuse_stretch(token)
        if token in self._number_origins:
            return self._number(token, language)
        raise PlaceholderError(
            f"puts {token} outside any string literal of an expression, JSON text or code; it stands for text, so "
            f"write it inside the quotes of a string literal, as '{token}' (\"{token}\" in JSON text)"
        )

    def _number(self, token: str, language: str) -> str:
        """The number placeholder ``token`` written back where a number goes in ``language`` (DataWeave, JSON text):
        as its literal was written when it comes from that same language, else as the exact same number in a form
        DataWeave and JSON read (a negative one in parentheses in DataWeave, so its sign cannot join an operator
        before it); refused when the literal is not a number either can write exactly."""
        if self._number_origins[token] == language:
            return self._values[token]
        form = self._number_forms[token]
        if form is None:
            raise PlaceholderError(
                f"puts {token} where a number goes, but it stands for a literal a2m cannot write as the exact same "
                "number there"
            )
        return f"({form})" if language == DATAWEAVE and form.startswith("-") else form

    def _in_literal(self, token: str, language: str, quote: str) -> str:
        if token in self._fixed:
            return self._held(token, "literal")  # as the source spelled it in this same literal
        self._refuse_stretch(token)
        if token in self._number_origins:
            raise PlaceholderError(
                f"puts {token} inside a string literal, but it stands for a number: write it where a number goes, "
                f"without quotes ({token}), or convert it ({token} as String)"
            )
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
        """The value ``token`` stands for, as plain text (an attribute value, an element text, a comment): a number
        placeholder as the same number in plain digits (:func:`_plain_number`), never DataWeave's ``5e3`` or
        ``5000.0``, which an integer attribute of Mule cannot read."""
        if token in self._fixed:
            return self._held(token, "text")
        self._refuse_stretch(token)
        if token in self._number_origins:
            form = self._number_forms[token]
            plain = None if form is None else _plain_number(form)
            if plain is None:
                raise PlaceholderError(
                    f"puts {token} as a value, but it stands for a literal a2m cannot write as the exact same number "
                    "in plain digits"
                )
            return plain
        if token in self._opaque:
            raise PlaceholderError(
                f"puts {token} outside the kind of string literal it came from, where a2m cannot write it back"
            )
        return self._values[token]

    # ------------------------------------------------------------ inside a document

    def _render(
        self,
        text: str,
        *,
        apigee: bool,
        depth: int,
        records: dict[_Where, _Shown] | None,
        data: bool = False,
    ) -> list[str | _Part]:
        """The pieces of ``text`` (XML) as shown; a Mule comment is left as a :class:`_Part` for the sweep. ``data``:
        the whole text is data (an embedded document); in an Apigee policy or endpoint (``apigee`` at depth 0) a part
        is data when an element above it is a data element (:func:`_in_data`). ``records``: for a Mule file, gets each
        part a2m changed, as shown, by where it stands (:meth:`_record`)."""
        pieces: list[str | _Part] = []
        positional = apigee and depth == 0
        for part in _lex(text):
            if part.kind == _MARKUP:
                pieces.append(part.raw)
                continue
            if part.kind == _COMMENT:
                pieces.append(self._hide(part.raw) if apigee else part)
                continue
            if records is None:
                raw, _ = self._render_part(part, apigee=apigee, depth=depth, data=data, positional=positional)
            else:
                self._emitted, self._nested_xml = [], False
                try:
                    raw, shown = self._render_part(part, apigee=apigee, depth=depth, data=data, positional=positional)
                    emitted = None if self._nested_xml else self._emitted
                finally:
                    self._emitted = None
                if raw != part.raw:
                    record = records[part.where] = _record(part, raw, shown, emitted)
                    for match, placed in zip(TOKEN.finditer(record.text), record.placed or (), strict=False):
                        self._note(match.group(0), placed.value, placed.raw)
            pieces.append(raw)
        return pieces

    def _render_part(self, part: _Part, *, apigee: bool, depth: int, data: bool, positional: bool) -> tuple[str, str]:
        """The value ``part`` (an attribute, a text or a CDATA section) as shown: as written in the XML, and as it
        reads."""
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
        return raw, shown

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
        numbers = depth == 0 and not data and (_number_slot(part) if apigee else _mule_number_slot(part))
        return self._value(value, apigee=apigee, depth=depth, data=data, numbers=numbers)

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
        numbers = apigee and depth == 0 and not data and _number_slot(part)
        script = not apigee and depth == 0 and _dataweave_script(part)
        return lead + self._value(core, apigee=apigee, depth=depth, data=data, numbers=numbers, script=script) + trail

    def _value(
        self, text: str, *, apigee: bool, depth: int, data: bool = False, numbers: bool = False, script: bool = False
    ) -> str:
        """One value (an attribute value, a text) as shown (see the module docstring); in data even a number, a
        boolean or a MIME type is a placeholder, and a bare number is shown only where ``numbers`` says it is a
        structural setting. ``script``: the text of a Transform Message part, shown as DataWeave with or without its
        ``%dw`` header (:meth:`_put_value` restores it so)."""
        core = text.strip()
        if not core:
            return text
        start = text.index(core)
        lead, trail = text[:start], text[start + len(core) :]
        if TOKEN_MARK in core:
            return lead + self.token(core) + trail  # it could be read as a placeholder: shown as one
        if _plain_shown(core, data=data, numbers=numbers):
            return text
        if not apigee:
            if script or core.startswith("%dw"):
                return lead + self._code(core, DATAWEAVE) + trail
            # A value that is JSON text as a whole stays JSON text with a ${property} in it (Mule puts the property's
            # value in and the text is still JSON), as :meth:`_put_value` reads it.
            if "#[" in core or ("${" in core and not (depth < MAX_DEPTH and _json_shape(core))):
                return lead + self._mule_template(core, numbers=numbers) + trail
        embedded = self._embedded(core, depth, data=data)
        if embedded is not None:
            return lead + embedded + trail
        if apigee:
            return lead + self._apigee_template(core, data=data, numbers=numbers) + trail
        return lead + self._literal(core, numbers=numbers) + trail

    def _embedded(self, core: str, depth: int, *, data: bool = False) -> str | None:
        """``core`` walked as the embedded document it holds (XML, JSON, form text), or None. An embedded XML
        document is data throughout: none of its values is a name."""
        if depth >= MAX_DEPTH:
            return None
        if core.startswith("<"):
            if not _readable_xml(core):
                return None
            try:
                pieces = self._render(core, apigee=True, depth=depth + 1, records=None, data=True)
            except _LexError:
                return None
            # Its values are escaped for the embedded XML, so a2m cannot tell their original bytes in the outer part.
            self._nested_xml = True
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

    def _apigee_template(self, text: str, *, data: bool = False, numbers: bool = False) -> str:
        """Apigee message template text: each ``{reference}`` kept, each literal piece shown by :meth:`_literal`."""
        out: list[str] = []
        position = 0
        for match in _APIGEE_REF.finditer(text):
            out.append(self._literal(text[position : match.start()], data=data, numbers=numbers))
            out.append(match.group(0))
            position = match.end()
        out.append(self._literal(text[position:], data=data, numbers=numbers))
        return "".join(out)

    def _mule_template(self, text: str, *, numbers: bool = False) -> str:
        """A Mule attribute value or text: each ``#[...]`` expression walked as DataWeave (``numbers``: the attribute
        holds a status code, which stays), each ``${property}`` kept, each literal piece shown by :meth:`_literal`."""
        out: list[str] = []
        position = 0
        index = 0
        ends = dict(_template_spans(text)[0])
        while index < len(text):
            if text.startswith("#[", index):
                end = ends.get(index, -1)
                if end < 0:
                    break  # not an expression a2m can find the end of for sure: the rest is one placeholder
                out.append(self._literal(text[position:index]))
                out.append("#[" + self._code(text[index + 2 : end], DATAWEAVE, numbers=numbers) + "]")
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

    def _literal(self, text: str, *, data: bool = False, numbers: bool = False) -> str:
        """A literal piece: shown when it is plainly visible (never in data; a bare number only where ``numbers``),
        by parts when it is an address with a query, else one placeholder."""
        if not text.strip() or _plain_shown(text, data=data, numbers=numbers):
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
                # A query value is data: a number in it is a placeholder.
                visible = not value or _plain_shown(value, data=data, numbers=False) or _APIGEE_REF.fullmatch(value)
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
            token = self._token(value, value, raw=content)
        key = (token, language, quote)
        if key in self._spellings and self._spellings[key] != content:
            self._spellings[key] = None
        else:
            self._spellings[key] = content
        return token

    def _code(self, text: str, language: str, *, data: bool = False, numbers: bool = False) -> str:
        """Code (``language``) with every string literal (an object key included), regular expression and comment a
        placeholder, except a blank literal; a literal is never shown because its value equals a known name. Every
        number is a placeholder (see :func:`_shown_number` for the few a2m writes itself that stay). In JSON text every
        bare word but true, false and null is a placeholder, and ``{reference}`` stays; in JSON that is data (a
        payload) true and false are placeholders too. ``numbers``: a DataWeave expression of a Mule attribute that
        holds a status code."""
        out: list[str] = []
        directives: list[tuple[int, int]] = []
        segs = _segments(text, language, directives)
        structural = _dw_structural(text, segs, directives) if language == DATAWEAVE else frozenset()
        for position, seg in enumerate(segs):
            kind = seg.kind
            emitted = len(self._emitted) if self._emitted is not None else -1
            if kind == _S_REGEX:
                out.append("/" + self._hide(seg.content) + "/")
            elif kind == _S_STRING:
                if _shown_literal(seg):
                    out.append(seg.raw)
                else:
                    token = self._literal_token(seg.content, language, seg.quote, prefixed=seg.prefixed)
                    out.append(seg.quote + token + seg.quote)
            elif kind == _S_LINE:
                out.append(seg.quote + self._hide(seg.content))
            elif kind == _S_BLOCK:
                out.append("/*" + self._hide(seg.content) + ("*/" if seg.closed else ""))
            elif kind == _S_OPEN:
                out.append(self._open(seg.raw))
            elif kind == _S_TOKEN:
                out.append(self.token(TOKEN_MARK) + seg.raw[1:])
            elif kind == _S_MARK:
                out.append(self.token(TOKEN_MARK))
            elif language == JSON_TEXT and _json_value(seg, data=data):
                hidden = self.token(seg.raw) if seg.kind == _S_WORD else self._number_token(seg.raw, JSON_TEXT)
                out.append(hidden)
            elif language != JSON_TEXT and _number_segment(seg):
                shown = language == DATAWEAVE and _shown_number(seg.raw, position in structural, numbers=numbers)
                out.append(seg.raw if shown else self._number_token(seg.raw, language))
            else:
                out.append(seg.raw)
            if self._emitted is not None and len(self._emitted) == emitted + 1:
                # Its stretch: a literal, regular expression or comment with its quotes, slashes or opener; else the
                # placeholder alone.
                entry, chunk = self._emitted[-1], out[-1]
                at = chunk.find(str(entry[0]))
                if at >= 0 and kind in (_S_REGEX, _S_STRING, _S_LINE, _S_BLOCK):
                    entry[2:] = [at, len(chunk) - at - len(str(entry[0])), _DELIMITED]
                elif at >= 0:
                    entry[2:] = [0, 0, _BARE]
        return "".join(out)

    def _open(self, raw: str) -> str:
        """A stretch of code the lexer could not read for sure (:data:`_S_OPEN`) as one placeholder of its own, its
        blanks at the ends kept: put back only unchanged where it was shown (:meth:`_refuse_stretch`)."""
        core = raw.strip()
        if not core:
            return raw
        start = raw.index(core)
        token = self._token("\x00open\x00" + core, core)
        self._stretches.add(token)
        return raw[:start] + token + raw[start + len(core) :]


def _number_segment(seg: _Seg) -> bool:
    """Whether a code segment is a number (the lexer reads one from a digit on, or from a "." with a digit after it;
    see :func:`_segments` and :func:`_dot_starts_number`)."""
    return seg.kind == _S_TEXT and (seg.raw[:1].isdigit() or (seg.raw[:1] == "." and seg.raw[1:2].isdigit()))


# What a placeholder may not touch with nothing between: it would join into a word, a number, a string or another
# placeholder (``x«n1»``, ``.«n1»``, ``«n1»e3``, ``«n1»_0``, ``«n1»«n2»``, ``«v1»"x"``).
_GLUE = frozenset("._$'\"`«»")


def _touching(char: str) -> bool:
    """Whether ``char`` (the character right before or after a placeholder, "" at an end) joins it to what is next
    to it: a letter, a digit, ".", "_", "$", a quote or a placeholder mark."""
    return bool(char) and (char.isalnum() or char in _GLUE)


def _glued(segs: Sequence[_Seg], position: int) -> bool:
    """Whether the placeholder at ``position`` of ``segs`` (code the AI wrote) does not stand alone as a whole token:
    it touches a letter, a digit, ".", "_", "$", a quote or another placeholder (:func:`_touching`). One rule for every
    placeholder outside a string literal, wherever its value came from: written back, such a placeholder would join
    what is next to it into another number, word, selector or string (``.«n1»`` for 5 is 0.5, ``«n1»«n2»`` for 5
    and 7 is 57). A placeholder of a Mule file left unchanged from what a2m showed is put back from its original
    bytes before this rule is reached (:meth:`Placeholders.restore`)."""
    before = segs[position - 1].raw[-1:] if position > 0 else ""
    after = segs[position + 1].raw[:1] if position + 1 < len(segs) else ""
    return _touching(before) or _touching(after)


# The most digits a number written in plain digits may have on either side of its point (a Java long has 19).
_PLAIN_DIGITS = 40


def _plain_number(form: str) -> str | None:
    """``form`` (an exact DataWeave and JSON number, :func:`_number_form`) in plain digits, as an attribute value or
    element text of Mule reads a number: an integer as its digits (``5e3`` and ``5000.0`` are ``5000``), any other
    number as a decimal with no exponent (``2.5e-3`` is ``0.0025``); None when that needs more than
    :data:`_PLAIN_DIGITS` digits on either side of the point."""
    value = Decimal(form.strip("()"))
    if not value.is_finite():
        return None
    if value.is_zero():
        return "0"
    if value.adjusted() >= _PLAIN_DIGITS:
        return None
    if value == value.to_integral_value():
        return str(int(value))
    if value.as_tuple().exponent < -_PLAIN_DIGITS:  # type: ignore[operator]
        return None
    return format(value, "f")


def _regex_reason(token: str) -> str:
    return (
        f"puts {token} in a regular expression where a2m did not show it; a2m does not write a value into a regular "
        f"expression (its syntax differs between languages) and writes back only a regular expression it showed, "
        f"left exactly as shown in the same place (/{token}/ with the code right before it unchanged); keep the "
        "regular expression as a2m showed it, and never move a placeholder into one"
    )


def _hash_reason(token: str) -> str:
    return (
        f"puts {token} in a plain value where it would start a Mule expression (\"#[\"), which a2m never lets a value "
        "become; leave that value where and as a2m showed it, or write the expression yourself in #[...]"
    )


def _expression_reason(token: str, start: str) -> str:
    return (
        f"puts {token} in or after a Mule expression whose end a2m cannot find for sure (it starts `{start}`): a \"/\", a "
        "comment, a bracket or a quote in it may close it in one reading and not in another, so a2m cannot tell "
        f"whether {token} stands in DataWeave or in plain text; write the whole value as one expression (starting "
        "with \"#[\" and ending with \"]\", with no other \"#[\" in it), or close that expression before "
        f"{token} without such a \"/\" or comment"
    )


def _json_template_reason(token: str) -> str:
    return (
        f"puts {token} in the text around a Mule expression (\"#[\") of a value that is JSON text as a whole: Mule "
        "writes that text as it is around the expression's result, so a2m cannot spell the value both as text and "
        f"for JSON; write the whole value as one DataWeave expression (#[{{ ... }}], with '{token}' in a string "
        "literal there), or leave the value as a2m showed it"
    )


def _unreadable_xml_reason(token: str) -> str:
    return (
        f"puts {token} in a value that starts like XML (\"<\") but is not XML a2m can read, so it cannot tell "
        "whether the value is XML text or plain text; write it as well-formed XML, or leave it as a2m showed it"
    )


def _property_reason(token: str) -> str:
    return (
        f"puts {token} in a plain value where it would make a Mule property placeholder (\"${{...}}\"), which a2m "
        "never lets a value become; leave that value where and as a2m showed it, or write the property placeholder "
        "yourself without a placeholder in it"
    )


def _stretch_reason(token: str) -> str:
    return (
        f"moves, quotes or edits {token}, which stands for a stretch of code a2m could not read for sure and showed "
        "as one placeholder; a2m puts it back only where and as it showed it, with the code right before and after "
        f"it unchanged. Leave {token} exactly as shown (never in quotes), or rewrite that code without it"
    )


_MAX_QUOTED_LINE = 120


def _open_reason(token: str, why: str, line: str = "", *, earlier: bool = False) -> str:
    """Why ``token``, written where the lexer reads the rest of the code (or of the line) as one stretch it cannot
    read for sure (:data:`_S_OPEN`, :func:`_segments`), cannot be put back: by what made that stretch. ``line``: the
    line of the code where the stretch starts, as the answer wrote it; ``earlier``: that line is above the one of
    ``token``."""
    shown = line if len(line) <= _MAX_QUOTED_LINE else line[: _MAX_QUOTED_LINE - 3] + "..."
    where = f" (the line `{shown}`)" if shown else ""
    if why == "slash" and earlier:
        return (
            f"puts {token} below a line that has a \"/\" a2m cannot read for sure{where}: that \"/\" may start a "
            "regular expression or be a division, and a quote, \"//\" or \"/*\" after it on its line may open a "
            f"string or comment that runs past the line end in one reading, so a2m cannot tell what {token} and "
            "everything after that line would be in. On that line, close every quote you open, or make the \"/\" "
            "surely a division by putting its left side in parentheses (`(ceil(x)) / 10`); keep each stretch a2m "
            "showed after a \"/\" exactly as shown"
        )
    if why in ("slash", "slash line"):
        return (
            f"puts {token} after a \"/\" that a2m cannot read for sure on its line{where} (it may start a regular "
            "expression or be a division) with a quote, another \"/\" or \"/*\" after it on that line: a2m cannot "
            f"tell where a string, regular expression or comment there would end, so it cannot tell what {token} "
            "would be in. Write the code you add on a line of its own, so that no quote or \"/\" follows such a \"/\" "
            "on its line, and leave each stretch a2m showed after a \"/\" exactly as shown"
        )
    if why == "directive":
        return (
            f"puts {token} after an output or input directive a2m cannot read for sure, so it cannot tell what "
            f"{token} would be in; write the directive on its own line as a2m showed it"
        )
    if why == "escape":
        return (
            f"puts {token} after a unicode escape written outside a string literal (it can stand for a quote or a "
            f"line end), so a2m cannot tell what {token} would be in"
        )
    return (
        f"puts {token} in or after a string literal that is never closed{where}, so a2m cannot tell what {token} "
        "would be in; close that string on its line"
    )


def _record(part: _Part, raw: str, shown: str, emitted: list[list[object]] | None) -> _Shown:
    """The record of the Mule ``part`` a2m showed as ``raw`` (``shown`` as it reads), with each placeholder shown in
    it (``emitted``, in order: placeholder, original bytes, chars of its stretch before and after it, kind of
    stretch). The placeholders are kept only when putting each one's original bytes back into the shown text gives
    the part exactly as it was; otherwise (or when ``emitted`` is None) only an exact echo of the part is restored."""
    original = part.raw if part.kind == _CDATA else _unescape(part.raw)
    pieces = TOKEN_SPLIT.split(shown)
    if emitted is None or len(emitted) != len(pieces) // 2:
        return _Shown(part.kind, part.quote, raw, part.raw, shown, None)
    offsets = None if part.kind == _CDATA else _raw_offsets(part.raw)
    rebuilt: list[str] = []
    placed: list[_Placed] = []
    shown_at = original_at = 0
    for index, piece in enumerate(pieces):
        if not index % 2:
            rebuilt.append(piece)
            shown_at += len(piece)
            original_at += len(piece)
            continue
        token, value, before, after, kind = emitted[index // 2]
        if token != piece or not isinstance(value, str) or not isinstance(before, int) or not isinstance(after, int):
            return _Shown(part.kind, part.quote, raw, part.raw, shown, None)
        end = original_at + len(value)
        last = len(original)
        written = value if offsets is None else part.raw[offsets[min(original_at, last)] : offsets[min(end, last)]]
        placed.append(_Placed(value, written, shown_at - before, shown_at + len(piece) + after, str(kind)))
        rebuilt.append(value)
        shown_at += len(piece)
        original_at = end
    if "".join(rebuilt) != original:
        return _Shown(part.kind, part.quote, raw, part.raw, shown, None)
    return _Shown(part.kind, part.quote, raw, part.raw, shown, tuple(placed))


def _open_place(context: tuple[str, ...]) -> bool:
    """Whether a placeholder read in ``context`` (:meth:`Placeholders._contexts`) stands in a stretch of code the
    lexer could not read for sure (:data:`_S_OPEN`)."""
    return bool(context) and context[-1].startswith(_S_OPEN)


def _requoted(raw: str, quote: str) -> str:
    """``raw``, the original bytes of an attribute's value, for an attribute now quoted with ``quote``: as written,
    with only that quote character escaped (entities and every other byte kept)."""
    return raw.replace(quote, "&quot;" if quote == '"' else "&apos;")


def _ambiguous_reason(reason: str) -> str:
    return (
        "adds an element next to one a2m showed (or removes one) and writes it as much like that element as the one "
        "you kept, so a2m cannot tell which of them stands for the element it showed, nor which placeholders you "
        "left as shown there; leave the element a2m showed exactly as shown when you add one like it, or make the "
        f"new one differ more from it (a2m read it as new, and it {reason})"
    )


def _crowded_reason() -> str:
    return (
        "changes so much of one part at once (an attribute, an element text or a script) that a2m cannot line up "
        "that part with the one it showed, so it cannot tell which of its placeholders were left as shown; make a "
        "smaller change there, leaving the lines and code around each placeholder as a2m showed them"
    )


def _shape(part: _Part) -> tuple[str, ...]:
    """What a value part is, without where it stands among its siblings: the tags of its elements from the root, its
    kind and, for an attribute, its name."""
    return (*(tag for tag, _ in part.where[:-1]), part.where[-1][0] if part.where else part.kind)


def _anchor(part: _Part) -> tuple[tuple[str, str], ...]:
    """The other attributes of an attribute part's start tag, as written (an element keeps them when the answer edits
    one of its values); nothing for any other part."""
    return tuple(item for item in part.tag if item[0] != part.attribute) if part.kind == _ATTRIBUTE else ()


def _pairing(
    shown: Sequence[_Part], parts: Sequence[_Part], ambiguous: set[int] | None = None
) -> dict[int, _Where]:
    """For each value part of an answer (by its index in ``parts``, the answer lexed), where the part a2m showed that
    it stands for stands (:data:`_Where`), if any. The two sequences of value parts (``shown``: a2m's, in order) are
    aligned by what each part is and holds (:func:`_shape` and the text as shown), so an element inserted or deleted
    before others never shifts which part they stand for: a part the answer left as shown is paired with it, and a
    part with no pair is new. In each block where the two differ, parts of the same shape are paired in order, by the
    other attributes of their start tag first (:func:`_anchor`) and, where those are all the same, by content
    (:func:`_pair_by_content`), so a part whose content moved to another sibling is paired with that sibling and
    read as a change there. ``ambiguous``, when given, gets each answer part a2m could not pair for sure."""
    written = [index for index, part in enumerate(parts) if part.kind != _MARKUP]
    shown_keys = [(_shape(part), part.raw) for part in shown]
    written_keys = [(_shape(parts[index]), parts[index].raw) for index in written]
    pairs: dict[int, _Where] = {}
    low = 0
    while low < len(shown_keys) and low < len(written_keys) and shown_keys[low] == written_keys[low]:
        pairs[written[low]] = shown[low].where
        low += 1
    high_shown, high_written = len(shown_keys), len(written_keys)
    while high_shown > low and high_written > low and shown_keys[high_shown - 1] == written_keys[high_written - 1]:
        high_shown -= 1
        high_written -= 1
        pairs[written[high_written]] = shown[high_shown].where
    if max(high_shown, high_written) - low > MAX_DIFF_PARTS:
        places = {part.where for part in shown[low:high_shown]}
        for index in written[low:high_written]:
            if parts[index].where in places:
                pairs[index] = parts[index].where
        return pairs
    matcher = SequenceMatcher(None, shown_keys[low:high_shown], written_keys[low:high_written])
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for step in range(i2 - i1):
                pairs[written[low + j1 + step]] = shown[low + i1 + step].where
        elif tag == "replace":
            block = [(index, parts[index]) for index in written[low + j1 : low + j2]]
            _pair_block(shown[low + i1 : low + i2], block, pairs, ambiguous)
    return pairs


def _pair_block(
    shown: Sequence[_Part],
    written: Sequence[tuple[int, _Part]],
    pairs: dict[int, _Where],
    ambiguous: set[int] | None = None,
) -> None:
    """Pair the parts of a block where the answer differs from what a2m showed: parts of the same shape, in order,
    aligned by their anchors (:func:`_anchor`) and, where those differ, one for one from the start. Where every
    anchor of the group is the same (nothing in the start tags tells the parts apart), by their content
    (:func:`_pair_by_content`); a part a2m cannot pair for sure that way goes to ``ambiguous``."""
    groups: dict[tuple[str, ...], tuple[list[_Part], list[tuple[int, _Part]]]] = {}
    for part in shown:
        groups.setdefault(_shape(part), ([], []))[0].append(part)
    for index, part in written:
        group = groups.get(_shape(part))
        if group is not None:
            group[1].append((index, part))
    for left, right in groups.values():
        anchors = {_anchor(part) for part in left} | {_anchor(part) for _, part in right}
        if len(left) * len(right) > MAX_TOKEN_PAIRS:
            for part, (index, _) in zip(left, right, strict=False):
                pairs[index] = part.where
            continue
        if len(anchors) == 1:
            _pair_by_content(left, right, pairs, ambiguous)
            continue
        matcher = SequenceMatcher(
            None, [_anchor(part) for part in left], [_anchor(part) for _, part in right], autojunk=False
        )
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag in ("equal", "replace"):
                for part, (index, _) in zip(left[i1:i2], right[j1:j2], strict=False):
                    pairs[index] = part.where


# Bound of pairing by content (:func:`_pair_alike`): the tokens of the parts a2m showed in one stretch of parts that
# differ, times those of the answer's parts there; beyond, the parts pair in order.
MAX_CONTENT_TOKEN_PAIRS = 250_000


def _pair_by_content(
    left: Sequence[_Part], right: Sequence[tuple[int, _Part]], pairs: dict[int, _Where], ambiguous: set[int] | None
) -> None:
    """Pair parts of the same shape that nothing in their start tags tells apart (``left``: a2m's, ``right``: the
    answer's, each in order) by what they hold: first the parts written exactly as a2m showed one (in order), then,
    in each stretch between those, each part with the one it is most like (:func:`_pair_alike`)."""
    matcher = SequenceMatcher(None, [part.raw for part in left], [part.raw for _, part in right], autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for part, (index, _) in zip(left[i1:i2], right[j1:j2], strict=True):
                pairs[index] = part.where
        elif tag == "replace":
            _pair_alike(left[i1:i2], right[j1:j2], pairs, ambiguous)


def _pair_alike(
    left: Sequence[_Part], right: Sequence[tuple[int, _Part]], pairs: dict[int, _Where], ambiguous: set[int] | None
) -> None:
    """:func:`_pair_by_content` for one stretch where no part is written exactly as shown: the pairing that keeps
    the order with the greatest total likeness (:func:`_likeness`), so a new element inserted before an edited one
    leaves the edited one paired with the part a2m showed. With as many parts on each side, they pair in order when
    that is as good as any pairing. A pair for which a part left out on either side is exactly as alike is not
    sure (a new part as like the shown one as the edited one): those answer parts stay unpaired (new) and go to
    ``ambiguous``. One part on each side pairs; over :data:`MAX_CONTENT_TOKEN_PAIRS`, the parts pair in order."""
    m, n = len(left), len(right)
    shown_tokens = [[token for token in _DIFF_TOKEN.findall(part.raw) if not token.isspace()] for part in left]
    written_tokens = [[token for token in _DIFF_TOKEN.findall(part.raw) if not token.isspace()] for _, part in right]
    size = sum(map(len, shown_tokens)) * sum(map(len, written_tokens))
    if m * n == 1 or max(size, m * n) > MAX_CONTENT_TOKEN_PAIRS:
        for part, (index, _) in zip(left, right, strict=False):
            pairs[index] = part.where
        return
    like = [[_likeness(a, b) for b in written_tokens] for a in shown_tokens]
    # best[i][j]: the greatest total likeness pairing left[i:] with right[j:] in order.
    best = [[0.0] * (n + 1) for _ in range(m + 1)]
    for i in range(m - 1, -1, -1):
        for j in range(n - 1, -1, -1):
            best[i][j] = max(best[i + 1][j], best[i][j + 1], best[i + 1][j + 1] + like[i][j])
    if m == n and sum(like[i][i] for i in range(m)) >= best[0][0] - 1e-9:
        for part, (index, _) in zip(left, right, strict=True):
            pairs[index] = part.where
        return
    chosen: list[tuple[int, int]] = []
    i = j = 0
    while i < m and j < n:
        if like[i][j] > 0 and abs(best[i][j] - best[i + 1][j + 1] - like[i][j]) <= 1e-9:
            chosen.append((i, j))
            i, j = i + 1, j + 1
        elif abs(best[i][j] - best[i + 1][j]) <= 1e-9:
            i += 1
        else:
            j += 1
    used_left = {a for a, _ in chosen}
    used_right = {b for _, b in chosen}
    for i, j in chosen:
        rivals = [other for other in range(n) if other not in used_right and abs(like[i][other] - like[i][j]) <= 1e-9]
        rivals_left = [other for other in range(m) if other not in used_left and abs(like[other][j] - like[i][j]) <= 1e-9]
        if rivals or rivals_left:
            if ambiguous is not None:
                ambiguous.add(right[j][0])
                ambiguous.update(right[other][0] for other in rivals)
            continue
        pairs[right[j][0]] = left[i].where


def _likeness(shown: Sequence[str], written: Sequence[str]) -> float:
    """How alike two parts are (each given by its tokens that are not blank), from 0 to 1: the share of their
    tokens that line up (:class:`SequenceMatcher` ratio)."""
    if not shown and not written:
        return 1.0
    return SequenceMatcher(None, shown, written, autojunk=False).ratio()


def _raw_offsets(raw: str) -> list[int]:
    """For each character of ``raw`` (XML text or attribute value) as it reads (:func:`_unescape`), where it starts in
    ``raw``, and the length of ``raw`` last: an entity is one character."""
    offsets: list[int] = []
    position = 0
    for match in _ENTITY.finditer(raw):
        offsets += range(position, match.start())
        offsets.append(match.start())
        position = match.end()
    offsets += range(position, len(raw))
    offsets.append(len(raw))
    return offsets


def _plain_flags(text: str, part: _Part) -> list[bool]:
    """For each placeholder of the Mule value ``text`` (``part``'s text as it reads), whether it stands in plain text,
    where nothing around it is syntax: outside every ``#[...]`` of a value that is neither a DataWeave script, JSON
    text nor embedded XML (read as :meth:`Placeholders._put_value` reads a Mule value)."""
    count = len(TOKEN.findall(text))
    core = text.strip()
    if _dataweave_script(part) or core.startswith("%dw"):
        return [False] * count
    if "#[" in core and _json_template(core, _template_spans(core)[0]):
        return [False] * count  # around its expressions what Mule writes is JSON text
    if "#[" in core or ("${" in core and not _json_shape(core)):
        plain = set(_plain_tokens(text))
        return [2 * number + 1 in plain for number in range(count)]
    if core.startswith("<") or _json_shape(core):
        return [False] * count
    return [True] * count


def _json_shape(core: str) -> bool:
    """Whether a value's core (``core``) is JSON text as a whole: it starts with "{" or "[" and ends with "}" or "]"
    (an Apigee ``{reference}`` alone is not)."""
    return core[:1] in "{[" and core[-1:] in "}]" and _APIGEE_REF.fullmatch(core) is None


def _json_template(core: str, spans: Sequence[tuple[int, int]]) -> bool:
    """Whether the Mule template ``core`` (with the expressions at ``spans``) is JSON text as a whole: it starts with
    "{" or "[" and ends with "}" or "]" in its text, not with the "]" of an expression (``[INFO] #[vars.x]`` is
    not)."""
    return _json_shape(core) and not (spans and spans[-1][1] == len(core) - 1)


# How many ``#[...]`` expressions of one Mule value a2m reads (:func:`_template_spans`), and how many characters it
# lexes for them in all; beyond, the rest is read as an expression whose end it cannot find.
MAX_TEMPLATE_EXPRESSIONS = 64
MAX_TEMPLATE_CHARS = 2_000_000


def _template_spans(text: str) -> tuple[list[tuple[int, int]], int]:
    """The expressions of the Mule value ``text`` (its core), as Mule finds them: (where its "#[" starts, where its
    closing "]" is) for each, and where the first "#[" whose end a2m cannot find for sure starts (-1 when none).

    A value that starts with "#[", ends with "]" and holds no other "#[" is one expression whose body is everything
    between, whatever it holds (a "/", a comment, a string with "/" in it). In any other value each "#[" ends at the
    "]" the DataWeave lexer finds for sure (:func:`_dw_expression_end`)."""
    if len(text) >= 3 and text.startswith("#[") and text.endswith("]") and "#[" not in text[2:]:
        return [(0, len(text) - 1)], -1
    spans: list[tuple[int, int]] = []
    budget = MAX_TEMPLATE_CHARS
    index = text.find("#[")
    while index >= 0:
        budget -= len(text) - index
        within = len(spans) < MAX_TEMPLATE_EXPRESSIONS and budget >= 0
        end = _dw_expression_end(text, index + 2) if within else -1
        if end < 0:
            return spans, index
        spans.append((index, end))
        index = text.find("#[", end + 1)
    return spans, -1


def _dw_expression_end(text: str, start: int) -> int:
    """Where the ``#[...]`` expression whose body starts at ``start`` of ``text`` closes, as the DataWeave lexer reads
    it for sure (:func:`_segments`): the index of the first "]" outside every string, comment, regular expression
    and bracket of the body; -1 when a2m cannot tell it for sure: the lexer reaches a stretch it cannot read, a
    comment holds a bracket (Mule's template may not read it as a comment), a stretch after an unsure "/" holds a
    bracket that could close the expression or change the depth when read as code, or a bracket closes that was
    never opened."""
    depth = 0
    offset = start
    for seg in _segments(text[start:], DATAWEAVE):
        raw = seg.raw
        if seg.kind == _S_OPEN or (seg.kind == _S_BLOCK and not seg.closed):
            return -1
        if seg.kind in (_S_LINE, _S_BLOCK) and any(char in raw for char in "[]"):
            return -1
        if seg.kind == _S_REGEX and seg.unsure:
            inner = depth
            for char in raw:
                inner += 1 if char in "([{" else -1 if char in ")]}" else 0
                if inner < 0:
                    return -1
            if inner != depth:
                return -1
        elif seg.kind == _S_TEXT and raw in ("(", "[", "{"):
            depth += 1
        elif seg.kind == _S_TEXT and raw in (")", "]", "}"):
            if depth == 0:
                return offset if raw == "]" else -1
            depth -= 1
        offset += len(raw)
    return -1


def _alignment(shown: Sequence[str], written: Sequence[str]) -> tuple[list[int | None], list[bool]]:
    """For each token of ``written`` (an answer's part), the index of the token of ``shown`` (the part a2m showed)
    it is aligned with as unchanged, or None; and for each, whether it lies where the part changed too much at once
    to be lined up (:data:`MAX_TOKEN_PAIRS`, :data:`MAX_DIFF_LINES`).

    Blanks never decide the alignment, so re-indenting code (wrapping it in ``if``, ``do { }``) changes nothing: the
    tokens that are not blank are aligned (:func:`_solid_alignment`: their common start and end, then a diff of the
    lines keyed by those tokens alone, then a diff of tokens only inside each block of lines that differ, when it is
    small enough), and each run of blanks between two aligned tokens that stand next to each other on both sides is
    aligned blank by blank from its ends while both are line ends or both are other blanks. Every step is linear or
    bounded, and the alignment never crosses itself."""
    aligned: list[int | None] = [None] * len(written)
    crowded = [False] * len(written)
    shown_solid, shown_lines = _solid(shown)
    written_solid, written_lines = _solid(written)
    solid, over = _solid_alignment(
        [shown[index] for index in shown_solid], [written[index] for index in written_solid], shown_lines, written_lines
    )
    for number, at in enumerate(solid):
        if at is not None:
            aligned[written_solid[number]] = shown_solid[at]
        crowded[written_solid[number]] = over[number]
    # Blank runs between aligned tokens that are next to each other (or the ends) on both sides.
    previous_written, previous_shown = -1, -1
    for written_at in [*written_solid, len(written)]:
        shown_at = len(shown) if written_at == len(written) else aligned[written_at]
        if shown_at is None:
            previous_written = previous_shown = -2  # not aligned: the blanks after it are not either
            continue
        if previous_written > -2 and all(token.isspace() for token in shown[previous_shown + 1 : shown_at]):
            _blank_run(aligned, shown, written, previous_shown + 1, shown_at, previous_written + 1, written_at)
        previous_written, previous_shown = written_at, shown_at
    for index, token in enumerate(written):
        if token.isspace() and aligned[index] is None:
            crowded[index] = (index > 0 and crowded[index - 1]) or (index + 1 < len(written) and crowded[index + 1])
    return aligned, crowded


def _solid(tokens: Sequence[str]) -> tuple[list[int], list[int]]:
    """The indexes of the tokens of ``tokens`` that are not blank, and the line each of them stands on."""
    indexes: list[int] = []
    lines: list[int] = []
    line = 0
    for index, token in enumerate(tokens):
        if token == "\n":
            line += 1
        elif not token.isspace():
            indexes.append(index)
            lines.append(line)
    return indexes, lines


def _blank_run(
    aligned: list[int | None], shown: Sequence[str], written: Sequence[str], a: int, b: int, c: int, d: int
) -> None:
    """Align the blanks ``written[c:d]`` with the blanks ``shown[a:b]`` from both ends, while both are line ends or
    both are other blanks."""
    while a < b and c < d and (shown[b - 1] == "\n") == (written[d - 1] == "\n"):
        b -= 1
        d -= 1
        aligned[d] = b
    while a < b and c < d and (shown[a] == "\n") == (written[c] == "\n"):
        aligned[c] = a
        a += 1
        c += 1


def _solid_alignment(
    shown: Sequence[str], written: Sequence[str], shown_lines: Sequence[int], written_lines: Sequence[int]
) -> tuple[list[int | None], list[bool]]:
    """:func:`_alignment` of the tokens that are not blank (``shown``, ``written``, each with the line it stands on):
    the common start and end; the rest diffed by lines, each line keyed by its tokens; each block of lines that
    differ diffed by tokens when it is small enough (:data:`MAX_TOKEN_PAIRS`, :data:`MAX_PART_TOKEN_PAIRS` for the
    part), else marked as changed too much to be lined up (the second list)."""
    aligned: list[int | None] = [None] * len(written)
    over = [False] * len(written)
    start, shown_end, written_end = _ends(shown, written, 0, len(shown), 0, len(written), aligned)
    shown_rows = _rows(shown_lines, start, shown_end)
    written_rows = _rows(written_lines, start, written_end)
    if len(shown_rows) > MAX_DIFF_LINES or len(written_rows) > MAX_DIFF_LINES:
        over[start:written_end] = [True] * (written_end - start)
        return aligned, over
    matcher = SequenceMatcher(
        None,
        ["\x00".join(shown[a:b]) for a, b in shown_rows],
        ["\x00".join(written[a:b]) for a, b in written_rows],
    )
    budget = MAX_PART_TOKEN_PAIRS
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for (a, b), (c, _) in zip(shown_rows[i1:i2], written_rows[j1:j2], strict=True):
                for step in range(b - a):
                    aligned[c + step] = a + step
        elif tag == "replace":
            a, b = shown_rows[i1][0], shown_rows[i2 - 1][1]
            c, d = written_rows[j1][0], written_rows[j2 - 1][1]
            a, b, d = _ends(shown, written, a, b, c, d, aligned)
            c = a - shown_rows[i1][0] + written_rows[j1][0]
            if (b - a) * (d - c) <= min(MAX_TOKEN_PAIRS, budget):
                budget -= (b - a) * (d - c)
                tokens = SequenceMatcher(None, shown[a:b], written[c:d], autojunk=False)
                for block in tokens.get_matching_blocks():
                    for step in range(block.size):
                        aligned[c + block.b + step] = a + block.a + step
            else:
                over[c:d] = [True] * (d - c)
    return aligned, over


def _rows(lines: Sequence[int], start: int, end: int) -> list[tuple[int, int]]:
    """The tokens ``start`` to ``end`` split into lines (start, end) by the line each stands on (``lines``)."""
    rows: list[tuple[int, int]] = []
    first = start
    for index in range(start + 1, end):
        if lines[index] != lines[index - 1]:
            rows.append((first, index))
            first = index
    if first < end:
        rows.append((first, end))
    return rows


def _ends(
    shown: Sequence[str], written: Sequence[str], a: int, b: int, c: int, d: int, aligned: list[int | None]
) -> tuple[int, int, int]:
    """Align the common start and end of ``shown[a:b]`` and ``written[c:d]``; the shown start of what is left (the
    written start is as far from ``c``), and the ends of what is left on each side."""
    size = 0
    while a + size < b and c + size < d and shown[a + size] == written[c + size]:
        aligned[c + size] = a + size
        size += 1
    a += size
    c += size
    while a < b and c < d and shown[b - 1] == written[d - 1]:
        b -= 1
        d -= 1
        aligned[d] = b
    return a, b, d


def _same(aligned: Sequence[int | None], written: int, shown: int, shown_size: int) -> bool:
    """Whether the written token at index ``written`` is aligned with the shown token at index ``shown``; before the
    first token and after the last one of either side, whether the other side is at its start or end too."""
    if shown < 0:
        return written == -1
    if shown >= shown_size:
        return written == len(aligned)
    return 0 <= written < len(aligned) and aligned[written] == shown


def _plain_tokens(text: str) -> list[int]:
    """The indexes in ``TOKEN_SPLIT.split(text)`` of the placeholders of the Mule value ``text`` that stand outside
    every ``#[...]`` expression (read as :meth:`Placeholders._put_template` reads it)."""
    pieces = TOKEN_SPLIT.split(text)
    lead = len(text) - len(text.lstrip())
    spans, unsure = _template_spans(text.strip())
    ranges = [(lead + start, lead + end + 1) for start, end in spans]
    if unsure >= 0:
        ranges.append((lead + unsure, len(text)))  # not plain text for sure (restoring refuses what stands there)
    found: list[int] = []
    offset = 0
    for number, piece in enumerate(pieces):
        if number % 2 and not any(start <= offset < end for start, end in ranges):
            found.append(number)
        offset += len(piece)
    return found


def _glued_reason(token: str) -> str:
    return (
        f"writes {token} joined to a letter, a digit, a \".\", \"_\", \"$\", a quote or another placeholder, which "
        f"would make another value of it; write the placeholder alone, with a space, an operator or a bracket next "
        f"to it ({token})"
    )


def _shown_number(raw: str, structural: bool, *, numbers: bool) -> bool:
    """Whether a number of DataWeave is shown: only a2m's own syntax (``structural``, see :func:`_dw_structural`)
    and, in a Mule attribute that holds a status code (``numbers``), a status code (``#[403]``,
    ``#[vars.httpStatus default 200]``). Every other number of DataWeave (arithmetic, a comparison, a literal) may
    have been copied from the bundle: a placeholder."""
    return structural or (numbers and _STATUS_CODE.fullmatch(raw) is not None)


def _dw_structural(text: str, segs: Sequence[_Seg], directives: Sequence[tuple[int, int]]) -> frozenset[int]:
    """The positions in ``segs`` (DataWeave ``text``, with the spans of its ``directives``) of the numbers that are
    DataWeave's own syntax: the version of a script's ``%dw`` line, a number in an output or input directive read
    for sure (``charset=UTF-8``, ``indent=2``) and the index of a selector (:func:`_dw_selectors`)."""
    found = set(_dw_selectors(segs))
    version = _DW_VERSION.match(text)
    offset = 0
    for position, seg in enumerate(segs):
        if _number_segment(seg) and (
            (version is not None and offset == version.start(1))
            or any(start <= offset < end for start, end in directives)
        ):
            found.add(position)
        offset += len(seg.raw)
    return frozenset(found)


def _dw_selectors(segs: Sequence[_Seg]) -> frozenset[int]:
    """The positions in ``segs`` (DataWeave) of the numbers that stand directly in a selector's brackets: a ``[``
    right after an operand that is one for sure (a string literal, a field selector written right after its ".", a
    Mule binding such as ``payload``, a ``]``, or the ``)`` of a grouping parenthesis that follows no word, as in
    ``(text as String)[0]``). A ``[`` anywhere else (an array literal, after ``if (...)``, after a function call or a
    keyword) is not a selector."""
    found: list[int] = []
    # Open brackets: (bracket, whether it is a selector "[" or a grouping "(").
    stack: list[tuple[str, bool]] = []
    operand = False  # the last segment that is not blank ends an operand for sure
    previous: _Seg | None = None  # the last segment that is not blank
    for position, seg in enumerate(segs):
        if seg.kind == _S_TEXT and not seg.raw.strip():
            continue
        if seg.kind == _S_OPEN:
            stack.clear()  # a stretch a2m cannot read may open or close brackets: none before it is known any more
        ends_operand = False
        if seg.kind == _S_STRING:
            ends_operand = True
        elif seg.kind == _S_WORD:
            selected = position > 0 and segs[position - 1].kind == _S_TEXT and segs[position - 1].raw == "."
            ends_operand = selected or seg.raw in _DW_BINDINGS
        elif seg.kind == _S_TEXT and seg.raw in ("[", "(", "{"):
            if seg.raw == "[":
                stack.append(("[", operand))
            else:
                stack.append((seg.raw, seg.raw == "(" and (previous is None or previous.kind != _S_WORD)))
        elif seg.kind == _S_TEXT and seg.raw in (")", "]", "}"):
            opened, flag = stack.pop() if stack else ("", False)
            ends_operand = (seg.raw == "]" and opened == "[") or (seg.raw == ")" and opened == "(" and flag)
        elif _number_segment(seg) and stack and stack[-1][0] == "[" and stack[-1][1]:
            found.append(position)
        operand = ends_operand
        previous = seg
    return frozenset(found)


def _shown_literal(seg: _Seg) -> bool:
    """Whether a string literal of code is shown as it is: only when it is blank. Every other literal is a
    placeholder whatever its value or position: a value equal to a known name, and an object key in any language
    (DataWeave, JSON text, JavaScript, Python), because code and payloads keep application data, credentials
    included, in their keys (an allowlist ``{'partner-secret-123456': 'partner'}``)."""
    return not seg.content.strip()


def _json_value(seg: _Seg, *, data: bool) -> bool:
    """Whether a segment of JSON text is a bare value to hide: every number with its sign, fraction and exponent
    (JSON text is data, so a number in it is never syntax) and every word but true, false and null; in data (a
    payload) also true and false."""
    if seg.kind == _S_WORD:
        return seg.raw != "null" if data else seg.raw not in _JSON_WORDS
    return seg.kind == _S_TEXT and (seg.raw[:1].isdigit() or (len(seg.raw) > 1 and seg.raw[0] in "+-."))


def _joined(raw: str, values: Sequence[str]) -> str:
    """``raw`` with its placeholders, in order, replaced by ``values``."""
    pieces = TOKEN_SPLIT.split(raw)
    pieces[1::2] = values
    return "".join(pieces)


def _segments(
    text: str, language: str, directives: list[tuple[int, int]] | None = None, *, bound: bool = True
) -> list[_Seg]:
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
    that is never closed. ``directives``, when given, gets the span (start, end) of each DataWeave output or input
    directive read for sure, its MIME parameters and writer options included. ``bound``: an unsure "/" whose line
    a2m cannot read may hide only the rest of that line when both readings close on it (:func:`_unsure_slash`)."""
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
            segs.append(_Seg(_S_OPEN, text[index:], why="escape"))
            break
        if char in "'\"`" and (char != "`" or language in (DATAWEAVE, JAVASCRIPT)):
            quote = char * 3 if language in (PYTHON, JAVA) and text.startswith(char * 3, index) else char
            prefix = _prefix(text, index)
            end = _quoted_end(text, index + len(quote), quote, language, fstring=_fstring(prefix, language))
            if end < 0:
                segs.append(_Seg(_S_OPEN, text[index:], why="string"))
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
                unsure_segs = _unsure_slash(text, index, language, bound=bound)
                segs += unsure_segs
                if unsure_segs[-1].kind == _S_OPEN and unsure_segs[-1].why != "slash line":
                    break
                if unsure_segs[-1].kind == _S_OPEN:
                    # The rest of the line could close a bracket in one reading and not in the other: no ")" after
                    # it is a division for sure any more.
                    brackets = [(bracket, False) for bracket, _ in brackets]
                    closed_division = False
                index += sum(len(seg.raw) for seg in unsure_segs)
                last = _LAST_UNSURE
                continue
            if end > 0:
                segs.append(_Seg(_S_REGEX, text[index : end + 1], content=text[index + 1 : end]))
                index = end + 1
                last = _LAST_VALUE
                continue
        if language == JSON_TEXT and (char.isdigit() or char in "+-."):
            number = _JSON_NUMBER.match(text, index)  # a sign, fraction or exponent is part of the number
        elif char.isdigit():
            number = _CODE_NUMBER.match(text, index)
        elif char == "." and _dot_starts_number(text, index, language, last, selected=selected):
            number = _DOT_NUMBER.match(text, index)
        else:
            number = None
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
                segs.append(_Seg(_S_OPEN, text[word.end() :], why="directive"))
                break
            if directive.group("name") is None:
                segs.append(_Seg(_S_WORD, directive.group("word")))
            else:
                segs.append(_Seg(_S_WORD, directive.group("iword")))
                segs.append(_Seg(_S_TEXT, directive.group("igap")))
                segs.append(_Seg(_S_WORD, directive.group("name")))
            segs.append(_Seg(_S_TEXT, directive.group("gap")))
            segs.append(_Seg(_S_TEXT, directive.group("mime")))
            if directives is not None:
                directives.append((directive.start(), directive.end()))
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


def _dot_starts_number(text: str, index: int, language: str, last: str, *, selected: bool) -> bool:
    """Whether the "." at ``index`` of ``text`` (code in ``language``) starts a number literal with its digits after
    it (``.07``, ``.5e3``, Java ``.1f``), so the literal is one segment, dot included.

    In JavaScript, Java and Python a "." with a digit after it is always a number: no identifier or member starts
    with a digit (JavaScript ``a?.5:1`` is ``a ? .5 : 1``). Not after another "." (JavaScript and Java ``...``,
    Python ``...``), where the number starts at the digit. In DataWeave only where the "." cannot select a field: at
    the start, after an operator or an opening bracket, or after an operator word (``else .5``); after an operand
    (``payload.5``) it stays a selector."""
    if not text[index + 1 : index + 2].isdigit() or text[index - 1 : index] == ".":
        return False
    if language in (JAVASCRIPT, JAVA, PYTHON):
        return True
    if language != DATAWEAVE:
        return False
    if last in _DW_OPERATOR_WORDS:
        return not selected
    return last == "" or (len(last) == 1 and last not in ")]}.?" and _WORD.fullmatch(last) is None)


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


def _unsure_slash(text: str, index: int, language: str = DATAWEAVE, *, bound: bool = True) -> list[_Seg]:
    """The segments for a "/" at ``index`` that may start a regular expression (one closes on its line) or be a
    division: whatever the reading, nothing that either reading would hide is shown. The rest of the line from the
    "/" is one regular expression up to its last "/" (so no "/" is left on the line to pair up differently), or, when
    a ``//`` comment may start in it, a regular expression up to the comment's first "/" and the comment to the end
    of the line. When a quote stands in that stretch (a string could run past it) or a ``/*`` comment may start in
    the line, that stretch is one segment a2m cannot read: only the rest of the line (why "slash line") when in both
    readings everything that starts on the line also ends on it (:func:`_line_closes`, checked when ``bound``), else
    the rest of the text, never closed (why "slash")."""
    line_end = text.find("\n", index)
    line_end = len(text) if line_end < 0 else line_end
    rest = text[index:line_end]

    def unreadable() -> list[_Seg]:
        if bound and line_end < len(text) and _line_closes(rest, language):
            return [_Seg(_S_OPEN, rest, why="slash line")]
        return [_Seg(_S_OPEN, text[index:], why="slash")]

    if "/*" in rest[1:]:
        return unreadable()
    comment = rest.find("//", 1)
    if comment > 0:
        if any(quote in rest[1:comment] for quote in "'\"`"):
            return unreadable()
        return [
            _Seg(_S_REGEX, rest[: comment + 1], content=rest[1:comment], unsure=True),
            _Seg(_S_LINE, rest[comment + 1 :], quote="/", content=rest[comment + 2 :], unsure=True),
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
        return unreadable()
    return [_Seg(_S_REGEX, rest[: close + 1], content=rest[1:close], unsure=True)]


def _line_closes(rest: str, language: str) -> bool:
    """Whether everything that starts in ``rest`` (the rest of a line from a "/" that may start a regular expression
    or be a division) ends on that line in both readings: read as a division, the rest is code; read as a regular
    expression, the expression closes at its first "/" (:func:`_regex_end`) and the rest is code after a value. Each
    reading is lexed alone (no further bound), and must leave no string, comment or stretch open at the line end. A
    "---" in it (a DataWeave header could end there) never closes."""
    if "---" in rest:
        return False
    readings = ["0" + rest]
    end = _regex_end(rest, 1)
    if end > 0:
        readings.append("0" + rest[end + 1 :])
    return all(
        not any(seg.kind == _S_OPEN or (seg.kind == _S_BLOCK and not seg.closed) for seg in _segments(
            reading, language, bound=False
        ))
        for reading in readings
    )


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


_ASCII_ALNUM = frozenset(string.ascii_letters + string.digits)
# The languages whose string literals a value may be spelled in, in a text a2m or a tool wrote.
_SPELLING_LANGUAGES = (DATAWEAVE, JSON_TEXT, JAVASCRIPT, JAVA, PYTHON)
# An escape a text may spell a character with: \uXXXX, \xXX, %XX, an XML character or entity reference.
_ESCAPED_CHAR = re.compile(
    r"\\u([0-9A-Fa-f]{4})|\\x([0-9A-Fa-f]{2})|%([0-9A-Fa-f]{2})|&#[xX]([0-9A-Fa-f]{1,6});|&#([0-9]{1,7});"
    r"|&(amp|lt|gt|quot|apos);"
)
_MAX_SKELETON_PASSES = 4


def _spelled_table(language: str, quote: str) -> dict[int, str]:
    """:func:`_encode` for ``language`` and ``quote`` as a translation table."""
    table = {code: f"\\u{code:04x}" for code in (*range(0x20), 0x2028, 0x2029)}
    table.update({ord(char): spelled for char, spelled in _SPELLED.items()})
    table[ord(quote)] = "\\" + quote
    if language == DATAWEAVE:
        table[ord("$")] = "\\$"
    return table


_SPELLED_TABLES = [
    (language, _spelled_table(language, quote)) for language in _SPELLING_LANGUAGES for quote in ("'", '"')
]


def _json_content(text: str, *, ascii_only: bool) -> str:
    encode = json.encoder.encode_basestring_ascii if ascii_only else json.encoder.encode_basestring
    return str(encode(text))[1:-1]


def _spellings_of(value: str) -> set[str]:
    """The spellings of ``value`` a2m or a tool may write in a text: as it is, as the content of a string literal of
    each language a2m reads (either quote, as :func:`_encode` spells it; DataWeave's ``$`` also as ``\\u0024``),
    JSON-, repr- and URL-encoded."""
    forms = {value, _json_content(value, ascii_only=True), _json_content(value, ascii_only=False), repr(value)[1:-1]}
    forms.add(ascii(value)[1:-1])
    for language, table in _SPELLED_TABLES:
        encoded = value.translate(table)
        forms.add(encoded)
        if language == DATAWEAVE:
            forms.add(encoded.replace("\\$", "\\u0024"))
    forms.update((urllib.parse.quote(value, safe=""), urllib.parse.quote(value), urllib.parse.quote_plus(value)))
    return forms


_PYTHON_CONTENT = {
    quote: str.maketrans({"\\": "\\\\", quote: "\\" + quote, "\n": "\\n", "\r": "\\r", "\t": "\\t"})
    for quote in ("'", '"')
}
_XML_TEXT = str.maketrans({"&": "&amp;", "<": "&lt;", ">": "&gt;"})
_XML_ATTRIBUTE = {
    quote: str.maketrans({**_ATTRIBUTE_ESCAPES, quote: "&quot;" if quote == '"' else "&apos;"}) for quote in ("'", '"')
}
_DOUBLED = str.maketrans({"\\": "\\\\"})


def _python_content(text: str, quote: str) -> str:
    """``text`` as the content of a Python string literal quoted with ``quote`` (what ``repr`` writes)."""
    if text.isprintable():
        return text.translate(_PYTHON_CONTENT[quote])
    return "".join(
        char.translate(_PYTHON_CONTENT[quote]) if char.isprintable() or char in "\n\r\t" else repr(char)[1:-1]
        for char in text
    )


def _quoted_once(form: str) -> set[str]:
    """``form`` as a text may quote it once more: as it is, XML-escaped, as the content of a Python literal of either
    quote (``!r``), with its backslashes doubled, or JSON-quoted."""
    return {
        form, form.translate(_XML_TEXT), form.translate(_XML_ATTRIBUTE['"']), form.translate(_XML_ATTRIBUTE["'"]),
        _python_content(form, "'"), _python_content(form, '"'), form.translate(_DOUBLED),
        _json_content(form, ascii_only=True), _json_content(form, ascii_only=False),
    }


def _quoted_twice(forms: Iterable[str]) -> set[str]:
    """Every form of ``forms`` quoted up to twice (:func:`_quoted_once`)."""
    once = {quoted for form in forms for quoted in _quoted_once(form)}
    return {quoted for form in once for quoted in _quoted_once(form)}


def _skeleton(text: str) -> str:
    """The letters and digits of ``text``, read after its escapes (:data:`_ESCAPED_CHAR`, a few times over, as a text
    quoted more than once spells them)."""
    for _ in range(_MAX_SKELETON_PASSES):
        decoded = _ESCAPED_CHAR.sub(_escaped_char, text)
        if decoded == text:
            break
        text = decoded
    return "".join(char for char in text if char.isalnum())


def _escaped_char(match: re.Match[str]) -> str:
    named = match.group(6)
    if named is not None:
        return _NAMED_ENTITIES[named]
    digits = next(group for group in match.groups()[:5] if group is not None)
    code = int(digits, 10 if match.group(5) is not None else 16)
    return chr(code) if code <= 0x10FFFF else " "


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


def condition_number(word: str) -> bool:
    """Whether ``word`` (a word token of an Apigee condition) is a number literal that :meth:`Placeholders.condition`
    shows as a number placeholder: a condition holding one is never sent to the AI (CP5 does not translate a
    comparison with a number)."""
    return TOKEN_MARK not in word and _condition_number(word)


def _condition_number(text: str) -> bool:
    """Whether a bare word of an Apigee condition is a number as a2m's condition translator reads one (it starts with
    a digit or a sign: ``42``, ``-7``, ``2a``), which it never compares as text."""
    return bool(text) and (text[0].isdigit() or text[0] in "+-")


# A decimal number as written: digits, an optional fraction and exponent (either part may be empty, checked after).
_DECIMAL = re.compile(r"(?P<int>\d*)(?:(?P<dot>\.)(?P<frac>\d*))?(?:[eE](?P<exp>[+-]?\d{1,6}))?")
_RADIX = {"x": 16, "X": 16, "o": 8, "O": 8, "b": 2, "B": 2}
_LEADING_DIGITS = re.compile(r"\d+")
_RADIX_DIGITS = {16: re.compile(r"[0-9A-Fa-f]+"), 8: re.compile(r"[0-7]+"), 2: re.compile(r"[01]+")}
# Underscores of a code number stand only between two digits or letters (1_000, 0xFF_FF).
_LOOSE_UNDERSCORE = re.compile(r"(?<![0-9A-Za-z])_|_(?![0-9A-Za-z])")
# The largest integer every IEEE double holds exactly, with all the integers below it.
_EXACT_DOUBLE = 2**53
# Number literals longer than this are not written back (no source holds such a number; it bounds the work).
MAX_NUMBER_CHARS = 400


def _number_form(raw: str, language: str) -> str | None:
    """The number literal ``raw`` (written in ``language``: JavaScript, Python, Java, DataWeave, JSON text or an
    Apigee :data:`CONDITION`) as the exact same number, spelled the way DataWeave and JSON both read a number: an
    optional ``-``, an integer without leading zeros, an optional fraction and exponent (``-0.5``, ``1.5e-3``,
    ``1000000``, ``255``). Separators (``1_000``), suffixes (Java ``L``, ``d``, JavaScript ``n``) and a leading ``+``
    go; hexadecimal, octal and binary integers are written in decimal (a Java ``int`` or ``long`` as the two's
    complement value Java gives it). None when ``raw`` is not a number of that language, or when the number cannot
    be written exactly: a Java ``float`` the decimal does not equal, a floating point integer past what a double
    holds exactly, a number that overflows to infinity, a Python imaginary number, a hexadecimal fraction."""
    text = raw.strip()
    if not text or len(text) > MAX_NUMBER_CHARS:
        return None
    sign = ""
    if text[0] in "+-":
        sign, text = ("-" if text[0] == "-" else ""), text[1:]
    code = language in (JAVASCRIPT, PYTHON, JAVA)
    if code and "_" in text:
        if _LOOSE_UNDERSCORE.search(text):
            return None
        text = text.replace("_", "")
    if not text:
        return None
    radix = len(text) > 1 and text[0] == "0" and text[1] in _RADIX
    suffix = ""
    if language == JAVASCRIPT and text.endswith("n"):
        suffix, text = "bigint", text[:-1]
    elif language == JAVA and text[-1:] in ("l", "L"):
        suffix, text = "long", text[:-1]
    elif language == JAVA and text[-1:] in ("f", "F", "d", "D") and not radix:
        suffix, text = ("float" if text[-1] in "fF" else "double"), text[:-1]
    if code and radix:
        base = _RADIX[text[1]]
        if language == JAVA and base == 8:
            return None  # Java has no 0o
        return _radix_form(sign, base, text[2:], language, suffix)
    if code and suffix not in ("float", "double") and len(text) > 1 and text[0] == "0" and text.isdigit():
        # A leading zero: a legacy octal integer in JavaScript and Java (010 is 8); Python allows only zeros.
        if language == PYTHON:
            return sign + "0" if set(text) == {"0"} else None
        if _RADIX_DIGITS[8].fullmatch(text):
            return _radix_form(sign, 8, text[1:], language, suffix)
        if language == JAVA:
            return None
    if language == JAVASCRIPT and len(text) > 1 and text[0] == "0" and text[1].isdigit():
        leading = _LEADING_DIGITS.match(text)
        if leading is not None and _RADIX_DIGITS[8].fullmatch(leading.group(0)):
            return None  # a legacy octal before "." or an exponent: 07.5 is a syntax error, 07.e3 a property of 7
    match = _DECIMAL.fullmatch(text)
    if match is None or not (match.group("int") or match.group("frac")):
        return None
    dot, exponent = match.group("dot"), match.group("exp")
    if suffix in ("bigint", "long") and (dot or exponent is not None):
        return None
    whole = match.group("int").lstrip("0") or "0"
    fraction = match.group("frac") or ("0" if dot else "")
    form = sign + whole + ("." + fraction if fraction else "") + (f"e{exponent}" if exponent is not None else "")
    floating = (language == JAVASCRIPT and suffix != "bigint") or (
        language in (PYTHON, JAVA) and (bool(dot) or exponent is not None or suffix in ("float", "double"))
    )
    if floating:
        value = float(form)
        if value in (float("inf"), float("-inf")):
            return None
        if suffix == "float" and not _exact_float32(form):
            return None
        exact = Decimal(form)
        if exact == exact.to_integral_value() and abs(exact) > _EXACT_DOUBLE and Decimal(value) != exact:
            return None  # the language reads it as the nearest double, another number
    return form


def _radix_form(sign: str, base: int, digits: str, language: str, suffix: str) -> str | None:
    """A hexadecimal, octal or binary integer of code (``digits`` after its ``0x``, ``0o``, ``0b`` or legacy ``0``)
    in decimal; None when it is not one exactly."""
    if suffix in ("float", "double") or not _RADIX_DIGITS[base].fullmatch(digits):
        return None
    value = int(digits, base)
    if language == JAVA:
        bits = 64 if suffix == "long" else 32
        if value >= 2**bits:
            return None
        if value >= 2 ** (bits - 1):
            value -= 2**bits  # Java reads such a literal as a negative two's complement int or long
    elif language == JAVASCRIPT and suffix != "bigint" and value > _EXACT_DOUBLE and int(float(value)) != value:
        return None  # JavaScript reads it as the nearest double, another number
    return str(-value if sign == "-" else value)


def _exact_float32(form: str) -> bool:
    """Whether the decimal ``form`` is exactly a value of a Java ``float`` (so ``1.5f`` is 1.5, ``1.1f`` is not 1.1)."""
    try:
        packed = struct.unpack("<f", struct.pack("<f", float(form)))[0]
    except (OverflowError, struct.error):
        return False
    return Decimal(packed) == Decimal(form)


def _numeric_word(text: str) -> bool:
    """Whether a bare word of an Apigee condition is a number (or starts like one: ``42``, ``-7``, ``1.5``, ``2a``),
    which no variable name does."""
    return text[:1].isdigit() or text[:1] in "+-."


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


def expression_end(text: str, start: int) -> int:
    """Where the Mule expression ``#[...]`` whose body starts at ``start`` of ``text`` closes: the index of its
    closing ``]``, or -1 when a2m cannot find it for sure (it never closes, or it holds a comment).

    String literals (with their DataWeave ``$( )`` interpolations) and nested brackets are skipped, and a "/" that
    may start a regular expression is read as one (a "]" in it does not close the expression), which skips more,
    never less. Used to split a Mule value into its expressions and plain text, both when values are shown and
    restored (:class:`Placeholders`) and when the fix loop compares the shape of an expression attribute."""
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


def _diff_call_end(line: str) -> int:
    """Where a diff line's part starts: after the "call N " a test with more than one call puts first."""
    call = _DIFF_CALL.match(line)
    return call.end() + 1 if call is not None and line[call.end() : call.end() + 1] == " " else 0


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
    "expression_end",
    "plain_visible",
]
