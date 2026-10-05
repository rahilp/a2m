# good: look-alike names and shapes are left visible

Contrast with `bad/overmask-lookalikes`: a credential word must be its own
dash/underscore-separated segment of the name (so `keyword` does not
match, only `x-partner-api-key` does), and there is no shape-based rule,
so a region id or an api version is never masked just because it looks
like a dash-separated token.

Example input:

```
{"x-partner-api-key": "CANARY-5a4b3c2d1e0f", "keyword": "premium-tier",
 "region": "us-east-1-production", "api-version": "2024.01.15-rc1"}
```

`mask_config()` masks only `x-partner-api-key`; `keyword`, `region` and
`api-version` stay visible.

Where: `a2m/verify/masking.py`, `_NAMED`.
