"""good: run.log masking filter renders %-style args before masking them,
and sits on every a2m logger, so a child logger's records are masked too."""
from __future__ import annotations

import logging
import re
from contextlib import contextmanager

_CRED = re.compile(r"(?i)\b(key|secret|token|password)\s*[:=]\s*\S{4,}|\bBearer\s+\S{4,}")


def _a2m_loggers() -> list[logging.Logger]:
    # A logger's filters apply only to records written on that logger, never
    # to records propagated to it from a child, so the filter goes on "a2m"
    # and on every "a2m.*" logger (such as "a2m.verify").
    names = [name for name in logging.Logger.manager.loggerDict if name.startswith("a2m.")]
    return [logging.getLogger("a2m")] + [logging.getLogger(name) for name in sorted(names)]


class Masker:
    @contextmanager
    def logging(self):
        class _Filter(logging.Filter):
            def filter(self, record: logging.LogRecord) -> bool:
                # getMessage() substitutes record.args into record.msg
                # first, so a credential passed as a %-style arg is already
                # part of the text the regex below can see and mask.
                rendered = record.getMessage()
                record.msg = _CRED.sub("***", rendered)
                record.args = None
                return True

        handler_filter = _Filter()
        loggers = _a2m_loggers()
        for logger in loggers:
            logger.addFilter(handler_filter)
        try:
            yield
        finally:
            for logger in loggers:
                logger.removeFilter(handler_filter)
