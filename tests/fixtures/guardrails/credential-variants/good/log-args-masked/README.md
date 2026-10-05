# good: run.log masking filter renders the message before masking it

Contrast with `bad/log-args-unmasked`: the filter calls
`record.getMessage()` (which substitutes `record.args` into `record.msg`)
*before* masking, then overwrites `record.msg` with the masked text and
clears `record.args`, so a credential passed as a positional logging
argument is masked exactly like one written inline.

Contrast with `bad/log-filter-on-root-only` (this fixture's earlier,
mislabelled version, corrected after CP10 adversarial round 1, finding X1):

- the filter sits on the `a2m` logger and on every `a2m.*` logger (such as
  `a2m.verify`, which `send_fix_request()` writes on), because Python applies
  a logger's filters only to records written on that logger, never to
  records propagated to it from a child;
- its rule also masks a `Bearer <token>` value, the shape
  `send_fix_request()` actually logs (`Authorization header Bearer ...`).

Where: `a2m/verify/masking.py`, `Masker.logging()`'s `_Filter.filter()`.
