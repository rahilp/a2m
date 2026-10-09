# Demo exports

Apigee proxy exports for showing a2m end to end. Run them with:

```sh
a2m migrate demo/apigee-exports --out ~/a2m-demo/results --llm none --mock-backends
```

or pick `demo/apigee-exports` as the exports folder in `a2m tui`. With AI off and mock backends the run takes
about 3 minutes on a laptop and ends with:

| Bucket | Proxies |
| --- | --- |
| verified (7) | catalog-api, customer-profile-api, hr-directory-api, inventory-api, partner-gateway-api, payments-api, store-locator-api |
| needs review (4) | Test-API, js-transform, legacy-auth, weather-api |
| unsupported (1) | broken-proxy |

What each one shows:

| Proxy | Policies | Why it lands where it does |
| --- | --- | --- |
| catalog-api | VerifyAPIKey, AssignMessage | every policy translated and tested |
| customer-profile-api | VerifyAPIKey, AssignMessage, conditional flows | per-resource flows translated and tested |
| hr-directory-api | BasicAuthentication, AssignMessage | Basic credentials decoded and checked |
| inventory-api | VerifyAPIKey, ExtractVariables, AssignMessage | a query parameter reaches the backend as a header |
| partner-gateway-api | AccessControl, VerifyAPIKey (query parameter) | IP allow-list and key both tested |
| payments-api | VerifyAPIKey, AssignMessage, RaiseFault | DELETE refused with 405 at the edge |
| store-locator-api | AssignMessage (remove and set headers) | internal header stripped, version header added |
| Test-API | FlowCallout to GetSharedFlow, KeyValueMapOperations | KVM is not translated yet |
| js-transform | JavaScript | custom code needs the AI (`--llm claude`) |
| legacy-auth | OAuthV2 | OAuth is not translated yet |
| weather-api.zip | SpikeArrest, VerifyAPIKey, fault rules | rate limits and fault rules need a person to check |
| broken-proxy | (malformed XML) | the bundle cannot be read |

GetSharedFlow is the shared flow Test-API calls.
