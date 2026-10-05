# a2m report: catalog-api

Source: catalog-api
Bucket: needs-review
Verification type: static (the app was generated, and maybe built, but never run against tests)

Needs review: a person must check the items in the questions below before this project is used.

## Policies, steps and conditions

One row per step, policy file and condition in the bundle and in the shared flows it calls. Method: template (a2m's own template), ai (translated by the AI, with its confidence) or skipped (not generated, with the reason).

| Item | Kind | Type | Where | Mule result | Method | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| VK-Key | step | VerifyAPIKey | ProxyEndpoint default PreFlow request | generated | template | - |
| AM-Tag | step | AssignMessage | ProxyEndpoint default PreFlow request | generated | template | - |
| AM-Tag | policy | AssignMessage | policies/AM-Tag.xml | used by 1 step | template | - |
| VK-Key | policy | VerifyAPIKey | policies/VK-Key.xml | used by 1 step | template | - |

## Test results

No tests ran: verification type static.

Result: runtime verification disabled by --no-runtime

## Untested policies

Not known: the tests were not run.

## AI fix attempts

None: no AI fix attempts were made (nothing was run, so there was nothing to fix).

## Question for a human

1. This app was not run against tests (runtime verification disabled by --no-runtime), so nothing shows it behaves like the Apigee proxy. Run with Java, Maven and Mule to verify.

## Suggested fix

1. Install the local toolchain (Temurin 17, Maven 3.9 and Mule Kernel CE 4.9.0, see the README), then rerun a2m with --force --only catalog-api --mock-backends (or --golden), without --no-runtime.
