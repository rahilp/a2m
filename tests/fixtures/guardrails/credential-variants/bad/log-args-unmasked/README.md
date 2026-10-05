# bad: a %-style logging argument escapes the run.log masking filter

`Masker.logging()` installs a `logging.Filter` that rewrites `record.msg`
through the credential regex before the line is written, but masking a
%-style template string never touches `record.args`: the literal value
passed as a positional logging argument is substituted into the message
only when the handler later calls `record.getMessage()` (`msg % args`),
after the filter already ran. In `fix_loop.py::send_fix_request`, the
Authorization token is logged as `logger.info("... %s", token)`; the
filter leaves it untouched and run.log ends up with the literal token,
e.g. `CANARY-7f6e5d4c3b2a1908`.

Where: `a2m/verify/masking.py`, `Masker.logging()`'s `_Filter.filter()`
(only rewrites `record.msg`, never `record.args`); `a2m/verify/fix_loop.py`,
`send_fix_request()`.
