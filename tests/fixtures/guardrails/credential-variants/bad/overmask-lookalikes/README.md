# bad: over-eager masking catches non-secret look-alikes

`mask_config()` has two rules that are too broad:

1. a name is "credential-like" whenever it merely *contains* one of the
   credential words as a substring, so `keyword` (it contains `key`) is
   treated like a credential name.
2. any value that merely *looks* token-shaped (letters, digits and dashes
   or dots, three or more segments) is masked as if it were a
   credential-shaped value (the real masker's "credential-shaped values
   are masked even when never learned" rule, applied too broadly here), so
   a region id or an api version gets masked too.

Example input:

```
{"x-partner-api-key": "CANARY-5a4b3c2d1e0f", "keyword": "premium-tier",
 "region": "us-east-1-production", "api-version": "2024.01.15-rc1"}
```

`mask_config()` masks all four values; only `x-partner-api-key` is a real
credential. `keyword`, `region` and `api-version` are harmless and should
stay visible.

Where: `a2m/verify/masking.py`, `_NAMED` (substring match on the name) and
`_SHAPED` (shape-based match applied regardless of name).
