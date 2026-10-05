# a2m report: legacy-auth

Source: legacy-auth
Bucket: needs-review
Verification type: static (the app was generated, and maybe built, but never run against tests)

Needs review: a person must check the items in the questions below before this project is used.

## Policies, steps and conditions

One row per step, policy file and condition in the bundle and in the shared flows it calls. Method: template (a2m's own template), ai (translated by the AI, with its confidence) or skipped (not generated, with the reason).

| Item | Kind | Type | Where | Mule result | Method | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| BA-Decode | step | BasicAuthentication | ProxyEndpoint default PreFlow request | generated | template | - |
| OA-Verify | step | OAuthV2 | ProxyEndpoint default PreFlow request | not generated | skipped | OAuthV2 policy OA-Verify is not translated in this version of a2m |
| Step OA-Verify | condition | - | ProxyEndpoint default PreFlow request | not used | skipped | condition: request.verb = "DELETE"; step OA-Verify is not generated, so its condition is not used |
| BA-Decode | policy | BasicAuthentication | policies/BA-Decode.xml | used by 1 step | template | - |
| OA-Verify | policy | OAuthV2 | policies/OA-Verify.xml | used by 1 step, not generated | skipped | OAuthV2 policy OA-Verify is not translated in this version of a2m |

## Test results

No tests ran: verification type static.

Result: runtime verification disabled by --no-runtime

## Untested policies

Not known: the tests were not run.

## AI fix attempts

None: no AI fix attempts were made (nothing was run, so there was nothing to fix).

## Question for a human

1. This app was not run against tests (runtime verification disabled by --no-runtime), so nothing shows it behaves like the Apigee proxy. Run with Java, Maven and Mule to verify.
2. Step OA-Verify (OAuthV2) at ProxyEndpoint default PreFlow request was not migrated: OAuthV2 policy OA-Verify is not translated in this version of a2m. How should it be done in Mule?

## Suggested fix

1. Install the local toolchain (Temurin 17, Maven 3.9 and Mule Kernel CE 4.9.0, see the README), then rerun a2m with --force --only legacy-auth --mock-backends (or --golden), without --no-runtime.
2. a2m does not migrate OAuthV2: protect the API with a Mule OAuth 2.0 provider or an API Manager OAuth policy, or remove the step if it is not needed, then rerun a2m with --force --only legacy-auth.
