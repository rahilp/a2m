"""Parse Apigee condition tokens into a small syntax tree.

Grammar (keywords and word operators are case-insensitive)::

    condition  := term (connective term)*      one connective kind per bracket level
    term       := NOT "(" condition ")" | "(" condition ")" | comparison
    comparison := VARIABLE operator value
    value      := "quoted text" | bare-word | number | null | true | false

Decisions that keep a2m from guessing (each raises :class:`ConditionError`):

* AND and OR mixed at one bracket level without brackets: Apigee does not
  document which binds first. A chain of one connective groups left to right.
* NOT before anything but a bracketed condition: its reach is not documented.
* A variable with no comparison (``myFlag``), a missing value or operator,
  an unknown operator, unbalanced brackets, a literal on the left.

A bare unquoted word on the right is a string literal; a bare number stays a
number (and is refused later, see :mod:`a2m.conditions.dataweave`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from a2m.conditions.lexer import ConditionError, Token, TokenKind
from a2m.conditions.variables import fold


class Operator(StrEnum):
    EQUALS = "equals"
    NOT_EQUALS = "not-equals"
    EQUALS_IGNORE_CASE = "equals-ignore-case"
    MATCHES = "matches"
    MATCHES_PATH = "matches-path"
    JAVA_REGEX = "java-regex"
    STARTS_WITH = "starts-with"
    GREATER = "greater"
    GREATER_OR_EQUAL = "greater-or-equal"
    LESS = "less"
    LESS_OR_EQUAL = "less-or-equal"


class Connective(StrEnum):
    AND = "and"
    OR = "or"


class LiteralKind(StrEnum):
    STRING = "string"
    NUMBER = "number"
    NULL = "null"
    BOOLEAN = "boolean"


# Every spelling Apigee accepts for an operator, lower case.
OPERATORS: dict[str, Operator] = {
    "=": Operator.EQUALS,
    "==": Operator.EQUALS,
    "equals": Operator.EQUALS,
    "!=": Operator.NOT_EQUALS,
    "notequals": Operator.NOT_EQUALS,
    ":=": Operator.EQUALS_IGNORE_CASE,
    "equalscaseinsensitive": Operator.EQUALS_IGNORE_CASE,
    "~": Operator.MATCHES,
    "like": Operator.MATCHES,
    "matches": Operator.MATCHES,
    "~/": Operator.MATCHES_PATH,
    "likepath": Operator.MATCHES_PATH,
    "matchespath": Operator.MATCHES_PATH,
    "~~": Operator.JAVA_REGEX,
    "javaregex": Operator.JAVA_REGEX,
    "=|": Operator.STARTS_WITH,
    "startswith": Operator.STARTS_WITH,
    ">": Operator.GREATER,
    "greaterthan": Operator.GREATER,
    ">=": Operator.GREATER_OR_EQUAL,
    "greaterthanorequals": Operator.GREATER_OR_EQUAL,
    "<": Operator.LESS,
    "lesserthan": Operator.LESS,
    "<=": Operator.LESS_OR_EQUAL,
    "lesserthanorequals": Operator.LESS_OR_EQUAL,
}
CONNECTIVES: dict[str, Connective] = {
    "and": Connective.AND,
    "&&": Connective.AND,
    "or": Connective.OR,
    "||": Connective.OR,
}
NOT_WORDS = frozenset({"not", "!"})
KEYWORD_LITERALS: dict[str, LiteralKind] = {
    "null": LiteralKind.NULL,
    "true": LiteralKind.BOOLEAN,
    "false": LiteralKind.BOOLEAN,
}
VARIABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]*")
NUMBER = re.compile(r"[+-]?\d+(?:\.\d+)?")


@dataclass(frozen=True, slots=True)
class Literal:
    kind: LiteralKind
    text: str


@dataclass(frozen=True, slots=True)
class Comparison:
    variable: str
    operator: Operator
    # The operator as written, for reasons shown to the user.
    spelling: str
    value: Literal


@dataclass(frozen=True, slots=True)
class Not:
    operand: Node


@dataclass(frozen=True, slots=True)
class Binary:
    connective: Connective
    left: Node
    right: Node


Node = Comparison | Not | Binary


def parse(tokens: list[Token]) -> Node:
    """The syntax tree of a non-empty token list; raises :class:`ConditionError` with the reason otherwise."""
    if not tokens:
        raise ConditionError("the condition is empty")
    return _Parser(tokens).parse()


def _lower(token: Token | None) -> str:
    return fold(token.text) if token is not None and token.kind in (TokenKind.WORD, TokenKind.SYMBOL) else ""


class _Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self.tokens = tokens
        self.index = 0

    def peek(self) -> Token | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def advance(self) -> Token:
        token = self.tokens[self.index]
        self.index += 1
        return token

    def parse(self) -> Node:
        node = self.condition()
        extra = self.peek()
        if extra is not None:
            if extra.kind is TokenKind.RPAREN:
                raise ConditionError(f"the closing bracket at position {extra.position + 1} has no opening bracket")
            raise ConditionError(f"unexpected {_shown(extra)} at position {extra.position + 1}")
        return node

    def condition(self) -> Node:
        left = self.term()
        joined: Connective | None = None
        while (connective := CONNECTIVES.get(_lower(self.peek()))) is not None:
            if joined is not None and connective is not joined:
                raise ConditionError(
                    "it mixes AND and OR without brackets, and Apigee does not document which one applies first"
                )
            joined = connective
            self.advance()
            left = Binary(connective, left, self.term())
        return left

    def term(self) -> Node:
        token = self.peek()
        if token is None:
            raise ConditionError("the condition ends where a comparison was expected")
        word = _lower(token)
        if word in NOT_WORDS:
            self.advance()
            following = self.peek()
            if following is None or following.kind is not TokenKind.LPAREN:
                raise ConditionError(
                    "NOT is followed by something other than a bracketed condition; Apigee does not document "
                    "how far an unbracketed NOT reaches"
                )
            return Not(self.group())
        if token.kind is TokenKind.LPAREN:
            return self.group()
        if word in CONNECTIVES:
            raise ConditionError(f"{token.text} at position {token.position + 1} has no comparison before it")
        return self.comparison()

    def group(self) -> Node:
        opening = self.advance()
        node = self.condition()
        closing = self.peek()
        if closing is None or closing.kind is not TokenKind.RPAREN:
            raise ConditionError(f"the bracket opened at position {opening.position + 1} is never closed")
        self.advance()
        return node

    def comparison(self) -> Node:
        token = self.advance()
        if token.kind is not TokenKind.WORD or not VARIABLE_NAME.fullmatch(token.text):
            raise ConditionError(
                f"{_shown(token)} at position {token.position + 1} is where a variable name was expected"
            )
        variable = token.text
        op_token = self.peek()
        if op_token is None or op_token.kind is TokenKind.RPAREN or _lower(op_token) in CONNECTIVES:
            raise ConditionError(f"the variable {variable} is not compared with anything")
        operator = OPERATORS.get(_lower(op_token))
        if operator is None:
            if op_token.kind is TokenKind.SYMBOL:
                raise ConditionError(f"{op_token.text} after {variable} is not an operator a2m knows")
            raise ConditionError(f"there is no comparison operator between {variable} and {_shown(op_token)}")
        self.advance()
        value = self.peek()
        if value is None or value.kind in (TokenKind.LPAREN, TokenKind.RPAREN, TokenKind.SYMBOL):
            raise ConditionError(f"{variable} {op_token.text} has no value to compare with")
        if value.kind is TokenKind.WORD and (_lower(value) in CONNECTIVES or _lower(value) in NOT_WORDS):
            raise ConditionError(f"{variable} {op_token.text} has no value to compare with")
        self.advance()
        return Comparison(variable, operator, op_token.text, _literal(value))


def _literal(token: Token) -> Literal:
    if token.kind is TokenKind.STRING:
        return Literal(LiteralKind.STRING, token.text)
    keyword = KEYWORD_LITERALS.get(fold(token.text))
    if keyword is not None:
        return Literal(keyword, token.text)
    if token.text[0].isdigit() or token.text[0] in "+-":
        # A bare number, or a word that starts like one (2a, -x): never read as text.
        return Literal(LiteralKind.NUMBER, token.text)
    return Literal(LiteralKind.STRING, token.text)


def _shown(token: Token) -> str:
    return f'"{token.text}"' if token.kind is TokenKind.STRING else f"'{token.text}'"
