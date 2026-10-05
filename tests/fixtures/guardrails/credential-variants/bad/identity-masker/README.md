# bad: the request builder masks every field with a masker that hides nothing

`build_request()` passes the policy XML, the Mule files and the failing-test
diff through `masker.mask_config()`/`masker.mask()`, exactly like
`good/diff-all-masked`, but `Masker` returns its text unchanged. Every
literal credential in the policy (`X-Partner-Api-Key` header, `access_token`
query value, JSON `client_secret`, `password:`), the Mule files and the diff
(`x-api-key`, `password=`, `Authorization: Bearer`) reaches the AI provider
unmasked.

A check that judges a leak by asking the target's own masker whether it can
hide the value (and skips the value when it cannot) passes this variant: the
masker hides nothing, so every leak is skipped, and it masks no look-alike
either.

Where: `a2m/verify/masking.py`, `Masker.mask()` and `Masker.mask_config()`.
