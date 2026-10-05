# Credential-variant guardrail fixtures

Fixtures for the secret-sent-to-ai-provider / secret-in-log /
over-masking-non-secret mistake family.

## Layout

- `bad/<variant-name>/` - each is a minimal, self-contained mini package
  shaped like the real `a2m` package (`a2m/verify/...`, `a2m/ai/...`,
  `a2m/runlog.py`). Point a check at a variant's top-level directory the
  same way it would point at the real `a2m/` package. Each variant's
  `README.md` says exactly what leaks, or what gets over-masked, and where.
- `good/<variant-name>/` - same shape, but the masking or request-building
  logic is correct: real credentials are masked, look-alike non-secrets are
  left visible. Most `good/` variants are a direct contrast to one `bad/`
  variant of the same mechanism.

None of these fixtures hold a real credential. Every secret-shaped value is
an obviously fake canary (`CANARY-...`).

## Variants

bad/
- `diff-split-unmasked` - the failing-test diff is assigned to the AI
  request in a later, separate statement, under a renamed field, and never
  passes through the masker.
- `identity-masker` - every request field goes through a masker that
  returns its text unchanged, so every literal credential reaches the AI
  provider (a check that asks the target's own masker what to hide skips
  them all).
- `log-filter-on-root-only` - the run.log masking filter sits on the root
  logger, which Python never applies to records propagated from the child
  logger `a2m.verify`, and its rule misses `Bearer` (the earlier,
  mislabelled `good/log-args-masked`).
- `log-args-unmasked` - a credential logged as a `%s`-style positional
  argument escapes the run.log masking filter, which only rewrites the
  format string.
- `mask-before-learn` - the prompt is masked before this run's own
  credential-like headers are learned, so the first request that sees one
  leaks it.
- `overmask-lookalikes` - masking rules broad enough (bare substring match
  on the name, shape-based match on any value) to catch non-secret
  look-alikes such as `keyword`, `region` and `api-version`.

good/
- `diff-all-masked` - every request field, including the diff, is built
  from already-masked text in one expression.
- `learn-before-mask` - this run's own credential-like headers are learned
  before anything is masked.
- `log-args-masked` - the run.log filter renders `%s`-style args before
  masking, so a credential passed as an argument is masked too; it sits on
  every `a2m` logger (a child logger's records included) and masks `Bearer`.
- `lookalikes-left-visible` - credential words must be their own
  separator-bound segment of the name, and no value is masked by shape
  alone, so `keyword`, `region` and `api-version` stay visible.
