"""Translate Apigee conditions and {variable} templates into DataWeave.

:func:`translate_condition` and :func:`translate_template` return a
:class:`Translation` (``ok``, ``dw``, ``original``, ``reason``). The pipeline
is a real lexer (:mod:`.lexer`), parser (:mod:`.parser`) and emitter
(:mod:`.dataweave`); the variables a2m can read are in :mod:`.variables`.
Anything that cannot be translated exactly comes back ok False with the
reason and the original text, never as a guess or an always-true expression.
"""

from __future__ import annotations

from a2m.conditions.dataweave import Translation, dw_string, translate_condition, translate_template
from a2m.conditions.lexer import ConditionError
from a2m.conditions.template import TemplatePart, has_reference, template_parts
from a2m.conditions.variables import (
    ANY,
    FAULT,
    NO_CHANGES,
    REQUEST,
    REQUEST_CONTENT,
    REQUEST_CONTENT_TYPE,
    RESPONSE,
    RESPONSE_CONTENT,
    RESPONSE_CONTENT_TYPE,
    RESPONSE_HEADER_PREFIX,
    SNAPSHOT_VAR,
    RequestChanges,
)

__all__ = [
    "ANY",
    "FAULT",
    "NO_CHANGES",
    "REQUEST",
    "REQUEST_CONTENT",
    "REQUEST_CONTENT_TYPE",
    "RESPONSE",
    "RESPONSE_CONTENT",
    "RESPONSE_CONTENT_TYPE",
    "RESPONSE_HEADER_PREFIX",
    "SNAPSHOT_VAR",
    "ConditionError",
    "RequestChanges",
    "TemplatePart",
    "Translation",
    "dw_string",
    "has_reference",
    "template_parts",
    "translate_condition",
    "translate_template",
]
