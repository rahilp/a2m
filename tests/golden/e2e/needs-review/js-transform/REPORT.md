# a2m report: js-transform

Source: js-transform
Bucket: needs-review
Verification type: static (the app was generated, and maybe built, but never run against tests)

Needs review: a person must check the items in the questions below before this project is used.

## Policies, steps and conditions

One row per step, policy file and condition in the bundle and in the shared flows it calls. Method: template (a2m's own template), ai (translated by the AI, with its confidence) or skipped (not generated, with the reason).

| Item | Kind | Type | Where | Mule result | Method | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| AM-Stamp | step | AssignMessage | ProxyEndpoint default PreFlow request | generated | template | - |
| Flow reshape | condition | - | ProxyEndpoint default | translated: #[((attributes.maskedRequestPath default "") matches /\/reshape\/?/)] | template | condition: proxy.pathsuffix MatchesPath "/reshape" |
| JS-Reshape | step | Javascript | ProxyEndpoint default flow reshape request | generated from the AI's translation | ai (low) | needs review: the AI's confidence in this translation is low, so it needs review; AI notes: CP9 canned answer: reshape.js read as one DataWeave variable step, field names guessed from the script |
| AM-Stamp | policy | AssignMessage | policies/AM-Stamp.xml | used by 1 step | template | - |
| JS-Reshape | policy | Javascript | policies/JS-Reshape.xml | used by 1 step | ai (low) | needs review: the AI's confidence in this translation is low, so it needs review; AI notes: CP9 canned answer: reshape.js read as one DataWeave variable step, field names guessed from the script |

## Test results

No tests ran: verification type static.

Result: runtime verification disabled by --no-runtime

## Untested policies

Not known: the tests were not run.

## AI fix attempts

None: no AI fix attempts were made (nothing was run, so there was nothing to fix).

## Question for a human

1. This app was not run against tests (runtime verification disabled by --no-runtime), so nothing shows it behaves like the Apigee proxy. Run with Java, Maven and Mule to verify.
2. JS-Reshape (Javascript) was translated by the AI with low confidence (AI notes: CP9 canned answer: reshape.js read as one DataWeave variable step, field names guessed from the script). Does the translation behave like the original code?

## Suggested fix

1. Install the local toolchain (Temurin 17, Maven 3.9 and Mule Kernel CE 4.9.0, see the README), then rerun a2m with --force --only js-transform --mock-backends (or --golden), without --no-runtime.
2. Compare the generated step JS-Reshape in mule-app/src/main/mule/ with the original code in the bundle's resources/, correct it by hand if needed and test it, then rerun a2m with --force --only js-transform.
