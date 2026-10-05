# good: this run's own credential headers are learned before anything is masked

Contrast with `bad/mask-before-learn`: `build_prompt()` calls
`masker.learn_named(headers)` first, so the literal value of a
credential-like header (e.g. `X-Partner-Api-Key`) is already known before
`masker.mask(prompt)` runs. Even the very first request of a run has that
value replaced before it reaches the provider.

Where: `a2m/verify/fix_loop.py`, `build_prompt()`.

Corrected after CP10 adversarial round 1 (finding X2, codex-correctness):
`learn_named` learned a header only when its name held key, secret, token or
password, so this run's `Authorization: Bearer ...` header was never learned
and reached the AI provider unmasked. `authorization` is now one of its
credential words. The order (learn first, then mask) is unchanged.
