# bad: the run.log masking filter sits on the root logger and misses Bearer

This is the earlier `good/log-args-masked`, which was mislabelled (CP10
adversarial round 1, finding X1). Its filter renders `%s`-style args before
masking, but:

- it is attached to the root logger, and Python applies a logger's filters
  only to records written on that logger. `send_fix_request()` writes on the
  child logger `a2m.verify`, so the record propagates to the root logger's
  handlers (run.log) without ever passing the filter;
- its rule masks `key=`, `secret:`, `token=` and `password:` values only, so
  `Authorization header Bearer CANARY-...` would stay visible even where the
  filter does run.

Either way the Authorization token reaches run.log unmasked.

Where: `a2m/verify/masking.py`, `Masker.logging()`; `a2m/verify/fix_loop.py`,
`send_fix_request()`.
