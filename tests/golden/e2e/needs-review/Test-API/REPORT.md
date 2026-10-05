# a2m report: Test-API

Source: Test-API
Bucket: needs-review
Verification type: static (the app was generated, and maybe built, but never run against tests)

Needs review: a person must check the items in the questions below before this project is used.

## Policies, steps and conditions

One row per step, policy file and condition in the bundle and in the shared flows it calls. Method: template (a2m's own template), ai (translated by the AI, with its confidence) or skipped (not generated, with the reason).

| Item | Kind | Type | Where | Mule result | Method | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| SharedFlowCallout | step | FlowCallout | TargetEndpoint default PreFlow request | generated | template | - |
| Get-Shared-Flow | step | KeyValueMapOperations | shared flow GetSharedFlow | not generated | skipped | KeyValueMapOperations policy Get-Shared-Flow is not translated in this version of a2m |
| GetTargetUrlCallout | policy | FlowCallout | policies/SharedFlowCallout.xml | used by 1 step | template | - |
| Get-Shared-Flow | policy | KeyValueMapOperations | GetSharedFlow: policies/Get-Shared-Flow.xml | used by 1 step, not generated | skipped | KeyValueMapOperations policy Get-Shared-Flow is not translated in this version of a2m |

## Test results

No tests ran: verification type static.

Result: runtime verification disabled by --no-runtime

## Untested policies

Not known: the tests were not run.

## AI fix attempts

None: no AI fix attempts were made (nothing was run, so there was nothing to fix).

## Question for a human

1. This app was not run against tests (runtime verification disabled by --no-runtime), so nothing shows it behaves like the Apigee proxy. Run with Java, Maven and Mule to verify.
2. Step Get-Shared-Flow (KeyValueMapOperations) at shared flow GetSharedFlow was not migrated: KeyValueMapOperations policy Get-Shared-Flow is not translated in this version of a2m. How should it be done in Mule?

## Suggested fix

1. Install the local toolchain (Temurin 17, Maven 3.9 and Mule Kernel CE 4.9.0, see the README), then rerun a2m with --force --only Test-API --mock-backends (or --golden), without --no-runtime.
2. Write the Mule equivalent of KeyValueMapOperations Get-Shared-Flow by hand in mule-app/src/main/mule/, or remove the step if it is not needed, then rerun a2m with --force --only Test-API.
