"""Minimal stand-in for a2m.runlog.get_logger."""
from __future__ import annotations

import logging


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
