"""bad: run.log masking filter rewrites the format string, never the args."""
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
                # Masks the literal format string; record.args (a %-style
                # positional argument) is never touched, so a credential
                # passed as an arg is still substituted in unmasked when
                # the handler later renders "%s" % args.
                record.msg = _CRED.sub("***", str(record.msg))
                return True

        root = logging.getLogger()
        handler_filter = _Filter()
        root.addFilter(handler_filter)
        try:
            yield
        finally:
            root.removeFilter(handler_filter)
