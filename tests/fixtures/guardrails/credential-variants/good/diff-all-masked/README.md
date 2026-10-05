# good: every AiRequest field, including the diff, is masked before assembly

Contrast with `bad/diff-split-unmasked`: `build_request()` passes the
policy XML, the Mule files and the failing-test diff through
`masker.mask_config()`/`masker.mask()` in the same expression that builds
the `AiRequest`, so there is no later, unmasked assignment any field could
slip through.

Where: `a2m/verify/fix_loop.py`, `build_request()`.

Corrected after CP10 adversarial round 1 (finding X2, codex-correctness): the
earlier masker rule (`key|secret|token|password` followed by `:` or `=`)
missed most of the credentials the check plants (an XML
`<Header name="X-Partner-Api-Key">` or `<QueryParam name="access_token">`
value, a JSON `"client_secret"` value, a Mule `'x-api-key': '...'` value, a
diff's `X-Partner-Api-Key: expected '...'` and an `Authorization` Bearer
token), so this fixture really sent them to the AI provider. Its masker now
masks a value by its name at every one of those positions, and any
`Bearer` token; look-alikes such as `keyword`, `region` and `api-version`
stay visible. The request builder is unchanged.
