"""Templates that turn standard Apigee policies into Mule processors.

One module per policy type, each with ``translate(policy, *, direction)``;
:mod:`a2m.policies.registry` maps types to them. Types without a template
(OAuthV2, KeyValueMapOperations, ...) come back skipped with a reason.
"""

from __future__ import annotations

from a2m.policies.common import Method, PolicyResult, TemplateOutput, UnsupportedOption
from a2m.policies.registry import get_template, translate

__all__ = ["Method", "PolicyResult", "TemplateOutput", "UnsupportedOption", "get_template", "translate"]
