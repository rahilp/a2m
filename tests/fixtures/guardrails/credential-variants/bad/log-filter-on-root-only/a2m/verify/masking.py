"""bad: the run.log masking filter renders %-style args before masking, but
it is attached to the root logger and its rule misses the Bearer shape.
"""
from __future__ import annotations

import logging
import re
from contextlib import contextmanager

_CRED = re.compile(r"(?i)\b(key|secret|token|password)\s*[:=]\s*\S{4,}")


class Masker:
    @contextmanager
    def logging(self):
        class _Filter(logging.Filter):
            def filter(self, record: logging.LogRecord) -> bool:
                # getMessage() substitutes record.args into record.msg
                # first, so a credential passed as a %-style arg is part of
                # the text the regex sees; but this filter never runs for a
                # record written on a child logger (see below), and the
                # regex has no rule for "Authorization header Bearer ...".
                rendered = record.getMessage()
                record.msg = _CRED.sub("***", rendered)
                record.args = None
                return True

        # A logger's filters apply only to records written on that logger, not
        # to records propagated to it from a child such as "a2m.verify".
        root = logging.getLogger()
        handler_filter = _Filter()
        root.addFilter(handler_filter)
        try:
            yield
        finally:
            root.removeFilter(handler_filter)
