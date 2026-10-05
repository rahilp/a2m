"""Minimal stand-in for a2m.verify.masking.Masker: masks learned values."""
from __future__ import annotations


class Masker:
    def __init__(self) -> None:
        self.values: set[str] = set()

    def learn_named(self, headers: dict[str, str]) -> None:
        for name, value in headers.items():
            if any(word in name.lower() for word in ("key", "secret", "token", "password", "authorization")):
                self.values.add(value)

    def mask(self, text: str) -> str:
        for value in self.values:
            if value and value in text:
                text = text.replace(value, f"{value[:4]}*** ({len(value)} chars)")
        return text
