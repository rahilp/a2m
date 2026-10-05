"""Which bucket a proxy lands in: verified, needs-review or unsupported.

* ``unsupported``: a2m could not produce a Mule project (the bundle was refused, or a2m failed on it);
* ``verified``: the app ran and passed (verification type golden or battery, no failing test), every step,
  policy and condition is mapped (no skipped row), no AI row has low or no confidence or is flagged for review,
  nothing else was left out of the project, and the harness raised no review flag (such as a time-window
  policy whose timing a short test cannot prove). Policies the battery has no test for are listed in the
  report but do not by themselves block verified;
* ``needs-review``: everything else that has a project. A ``static`` result (built only, or not run) and a
  ``failed`` one (the build, deploy or a test failed) are always needs-review.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from a2m import layout
from a2m.inventory import OtherItem, Row
from a2m.policies.common import Method
from a2m.verify.model import VerificationResult, VerificationType

PASSED_TYPES = frozenset({VerificationType.GOLDEN, VerificationType.BATTERY})


class Bucket(StrEnum):
    VERIFIED = layout.VERIFIED_DIR_NAME
    NEEDS_REVIEW = layout.NEEDS_REVIEW_DIR_NAME
    UNSUPPORTED = layout.UNSUPPORTED_DIR_NAME


def tests_passed(result: VerificationResult) -> bool:
    """The app ran and every test passed (golden or battery, at least one test, none failing)."""
    return result.type in PASSED_TYPES and result.failed == 0 and result.ran > 0 and result.passed == result.ran


def decide(rows: Sequence[Row], others: Sequence[OtherItem], result: VerificationResult) -> Bucket:
    """The bucket of a proxy that has a Mule project (see the module docstring)."""
    if not tests_passed(result):
        return Bucket.NEEDS_REVIEW
    if any(row.method is Method.SKIPPED or row.ai_unsure for row in rows):
        return Bucket.NEEDS_REVIEW
    if others or result.review_flags:
        return Bucket.NEEDS_REVIEW
    return Bucket.VERIFIED


__all__ = ["PASSED_TYPES", "Bucket", "decide", "tests_passed"]
