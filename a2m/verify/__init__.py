"""Verification: build and run each generated Mule app against a mock backend, and label it honestly.

See :mod:`a2m.verify.harness` for the verification types and the engine stage,
:mod:`a2m.verify.batteries` for the per-policy tests, :mod:`a2m.verify.golden`
for recorded Apigee exchanges, :mod:`a2m.verify.mock_backend`,
:mod:`a2m.verify.runner` (the real runner on the local Mule runtime) and
:mod:`a2m.verify.tools` (finding Java, Maven and Mule).
"""

from __future__ import annotations

from a2m.verify.harness import (
    ENTERPRISE_MESSAGE,
    VerifyConfig,
    VerifyStage,
    explain_deploy_failure,
    make_verify_stage,
    requires_enterprise,
    verify_proxy,
)
from a2m.verify.model import (
    AppHandle,
    AppUnderTest,
    CaseResult,
    HttpRequest,
    HttpResponse,
    ReviewFlag,
    Runner,
    UntestedPolicy,
    VerificationResult,
    VerificationType,
)

__all__ = [
    "ENTERPRISE_MESSAGE",
    "AppHandle",
    "AppUnderTest",
    "CaseResult",
    "HttpRequest",
    "HttpResponse",
    "ReviewFlag",
    "Runner",
    "UntestedPolicy",
    "VerificationResult",
    "VerificationType",
    "VerifyConfig",
    "VerifyStage",
    "explain_deploy_failure",
    "make_verify_stage",
    "requires_enterprise",
    "verify_proxy",
]
