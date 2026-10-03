"""Split Apigee condition text into tokens.

Tokens are brackets, double-quoted values, words (variable names, bare values,
keywords and word operators such as ``Matches``) and symbol operators (``=``,
``!=``, ``&&``, ``~~`` ...). A quoted value is one token whatever it holds, so
``"rock AND roll (live)"`` is never read as operators. Quoted values are taken
as written: a backslash is an ordinary character (a JavaRegex keeps its
``\\d``), except that a backslash right before the closing quote is refused,
because Apigee does not document whether it escapes the quote.

Anything the lexer does not recognise raises :class:`ConditionError` with a
reason; the caller turns that into "can't translate", never a guess.
"""

from __future__ import annotations

import string
from dataclasses import dataclass
from enum import StrEnum


class ConditionError(ValueError):
    """The condition or template cannot be translated; the message is the reason shown to the user."""


class TokenKind(StrEnum):
    LPAREN = "("
    RPAREN = ")"
    STRING = "string"
    WORD = "word"
    SYMBOL = "symbol"


@dataclass(frozen=True, slots=True)
class Token:
    kind: TokenKind
    text: str
    position: int


# Characters of a variable name or bare value: request.header.X-API-Key, my_var2, GET, 42.
WORD_CHARS = frozenset(string.ascii_letters + string.digits + "_.-")
# Characters symbol operators are made of; a run of them is one operator (=, !=, :=, =|, ~~, ~/, &&, ||, ...).
SYMBOL_CHARS = frozenset("=!~:|<>&/")
QUOTE = '"'
ESCAPE = "\\"


def tokenize(text: str) -> list[Token]:
    """The tokens of ``text``; raises :class:`ConditionError` for an unclosed quote or an unknown character."""
    tokens: list[Token] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char.isspace():
            index += 1
        elif char == "(":
            tokens.append(Token(TokenKind.LPAREN, char, index))
            index += 1
        elif char == ")":
            tokens.append(Token(TokenKind.RPAREN, char, index))
            index += 1
        elif char == QUOTE:
            end = text.find(QUOTE, index + 1)
            if end < 0:
                raise ConditionError(f"the quoted value starting at character {index + 1} is never closed")
            if text[end - 1] == ESCAPE and end - 1 > index:
                raise ConditionError(
                    f"the quoted value starting at character {index + 1} has a backslash before a quote; "
                    "Apigee does not document whether that escapes the quote"
                )
            tokens.append(Token(TokenKind.STRING, text[index + 1 : end], index))
            index = end + 1
        elif char in WORD_CHARS:
            end = index
            while end < len(text) and text[end] in WORD_CHARS:
                end += 1
            tokens.append(Token(TokenKind.WORD, text[index:end], index))
            index = end
        elif char in SYMBOL_CHARS:
            end = index
            while end < len(text) and text[end] in SYMBOL_CHARS:
                end += 1
            tokens.append(Token(TokenKind.SYMBOL, text[index:end], index))
            index = end
        else:
            raise ConditionError(
                f"the character {char!r} at position {index + 1} is not part of Apigee's condition syntax"
            )
    return tokens
