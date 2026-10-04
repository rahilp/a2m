"""The plain data types of the verification harness: HTTP exchanges, the app under test and the result.

Kept apart from the harness so every module of :mod:`a2m.verify` can use them
without importing each other in a circle.
"""

from __future__ import annotations

import string
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

_TO_UPPER = str.maketrans(string.ascii_lowercase, string.ascii_uppercase)
_TO_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def fold(text: str) -> str:
    """``text`` with A-Z as a-z: HTTP header names, methods and Apigee keywords are ASCII and compared without case.

    Not a name-collision check; those go through :func:`a2m.layout.collision_key`.
    """
    return text.translate(_TO_LOWER)


def shout(text: str) -> str:
    """``text`` with a-z as A-Z (an HTTP method, an Apigee action such as ALLOW)."""
    return text.translate(_TO_UPPER)


class VerificationType(StrEnum):
    """How a proxy's Mule app was checked; never more than what actually happened.

    ``golden``: it ran and every response matched the recorded Apigee responses;
    ``battery``: it ran and passed every generated policy test against the mock backend;
    ``static``: it was only generated (or built), never run against tests;
    ``failed``: the build, deploy or start failed, or tests ran and at least one failed.
    """

    GOLDEN = "golden"
    BATTERY = "battery"
    STATIC = "static"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class HttpRequest:
    """One HTTP request; ``path`` includes the query string (``/orders?id=7``)."""

    method: str
    path: str
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""


@dataclass(frozen=True, slots=True)
class AppUnderTest:
    """The app a runner builds and starts: ``name`` (the proxy's name) and its generated project folder.

    ``properties`` are settings the tests need on top of the generated properties file (for example an
    API key the battery uses when the generated list of allowed keys is empty); a runner that deploys
    the app sets them in the deployed copy only, never in ``app_dir``.
    """

    name: str
    app_dir: Path
    properties: Mapping[str, str] = field(default_factory=dict)


class AppHandle(Protocol):
    """A started (or only built) app. ``running`` is False when the app was built but never started."""

    @property
    def running(self) -> bool: ...

    @property
    def base_url(self) -> str | None: ...

    def send(self, request: HttpRequest) -> HttpResponse: ...

    def stop(self) -> None: ...


class Runner(Protocol):
    """Builds and starts an app pointed at ``backend_url``; raises a2m.verify.mule.MuleError when it cannot."""

    def start(self, app: AppUnderTest, *, backend_url: str) -> AppHandle: ...


@dataclass(frozen=True, slots=True)
class CaseResult:
    """One battery case or golden exchange: ``situation`` is the battery situation (None for golden)."""

    name: str
    situation: str | None
    passed: bool
    diff: str
    backend_calls: int
    policy: str | None = None
    policy_type: str | None = None


@dataclass(frozen=True, slots=True)
class UntestedPolicy:
    """A policy (or one situation of it) the battery has no runnable case for, and why."""

    name: str
    type: str
    reason: str


@dataclass(frozen=True, slots=True)
class ReviewFlag:
    """Something a person must check even when every test passed (e.g. a time window a short test can't prove)."""

    policy: str
    policy_type: str
    reason: str


@dataclass(frozen=True, slots=True)
class VerificationResult:
    type: VerificationType
    ran: int = 0
    passed: int = 0
    failed: int = 0
    cases: tuple[CaseResult, ...] = ()
    untested: tuple[UntestedPolicy, ...] = ()
    review_flags: tuple[ReviewFlag, ...] = ()
    message: str = ""
    log_excerpt: str = ""

    def to_json_data(self) -> dict[str, Any]:
        """The result as plain JSON data (verification.json)."""
        return {
            "type": self.type.value,
            "ran": self.ran,
            "passed": self.passed,
            "failed": self.failed,
            "message": self.message,
            "log_excerpt": self.log_excerpt,
            "cases": [
                {
                    "name": case.name,
                    "situation": case.situation,
                    "policy": case.policy,
                    "policy_type": case.policy_type,
                    "passed": case.passed,
                    "backend_calls": case.backend_calls,
                    "diff": case.diff,
                }
                for case in self.cases
            ],
            "untested": [{"name": u.name, "policy_type": u.type, "reason": u.reason} for u in self.untested],
            "review_flags": [
                {"policy": f.policy, "policy_type": f.policy_type, "reason": f.reason} for f in self.review_flags
            ],
        }
