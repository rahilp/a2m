# a2m migration summary

6 proxies: 0 verified, 5 needs-review, 1 unsupported. Each proxy's REPORT.md says why it is in its bucket.

## Buckets

| Bucket | Proxies |
| --- | --- |
| verified | 0 |
| needs-review | 5 |
| unsupported | 1 |

- verified: the app ran and passed every test (golden or battery), and every step, policy and condition was mapped with a template or a confident AI translation.
- needs-review: a2m produced a Mule project, but something must be checked by a person (see the proxy's REPORT.md).
- unsupported: a2m could not produce a Mule project.

## Verification types

| Verification type | Proxies |
| --- | --- |
| golden | 0 |
| battery | 0 |
| static | 5 |
| failed | 0 |

- golden: the app ran and every response matched the recorded Apigee responses.
- battery: the app ran and passed a2m's policy tests against a local mock backend.
- static: the app was generated (and maybe built) but never run against tests.
- failed: the build, the deploy or at least one test failed.

## Policy types

Policy files of the proxies that have a Mule project, shared flow policies counted for each proxy that calls them.

| Policy type | Policies |
| --- | --- |
| AssignMessage | 3 |
| BasicAuthentication | 1 |
| ExtractVariables | 1 |
| FlowCallout | 1 |
| Javascript | 1 |
| KeyValueMapOperations | 1 |
| OAuthV2 | 1 |
| RaiseFault | 1 |
| SpikeArrest | 1 |
| VerifyAPIKey | 2 |

## Proxies

| Proxy | Bucket | Verification type | Steps | Policies | Conditions | Report |
| --- | --- | --- | --- | --- | --- | --- |
| Test-API | needs-review | static | 2 | 2 | 0 | needs-review/Test-API/REPORT.md |
| broken-proxy | unsupported | - | - | - | - | unsupported/broken-proxy/REPORT.md |
| catalog-api | needs-review | static | 2 | 2 | 0 | needs-review/catalog-api/REPORT.md |
| js-transform | needs-review | static | 2 | 2 | 1 | needs-review/js-transform/REPORT.md |
| legacy-auth | needs-review | static | 2 | 2 | 1 | needs-review/legacy-auth/REPORT.md |
| weather-api | needs-review | static | 5 | 5 | 2 | needs-review/weather-api/REPORT.md |
