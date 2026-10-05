# bad: request masked before this run's own credential headers are learned

`Masker.learn_named()` records the literal value of any header or query
parameter whose name looks like a credential (key, secret, token, ...), so
later text can be masked even where the value was never seen before. In
`a2m/verify/fix_loop.py::build_prompt`, the prompt is masked *first*, then
`learn_named()` is called on the same headers afterwards. On the very first
request of a run, the header's value (e.g. `X-Partner-Api-Key:
CANARY-5a4b3c2d1e0f`) has not been learned yet when `mask()` runs, so it is
sent to the AI provider unmasked. Only a later request in the same run
would be protected, because by then `learn_named()` has already seen it.

Where: `a2m/verify/fix_loop.py`, `build_prompt()`, the order of the
`masker.mask(prompt)` and `masker.learn_named(headers)` lines.
