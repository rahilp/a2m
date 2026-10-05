"""bad: logs the Authorization token as a %-style arg, which the run.log
masking filter never touches (see masking.py), so it reaches run.log
unmasked.
"""
from __future__ import annotations

from a2m.runlog import get_logger
from a2m.verify.masking import Masker

logger = get_logger("a2m.verify")


def send_fix_request(token: str) -> None:
    masker = Masker()
    with masker.logging():
        logger.info("sending fix request with Authorization header %s", token)
