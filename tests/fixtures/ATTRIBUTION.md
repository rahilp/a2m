# Third-party test fixtures

## tests/fixtures/apigee/azure/

The bundles under `tests/fixtures/apigee/azure/` are verbatim copies from
Azure/Apigee_to_APIM_migration_tool (https://github.com/Azure/Apigee_to_APIM_migration_tool),
commit `ab6cb88263f4f42dd470e444ed950fd3d97baec5`:

| Fixture | Source path in that repository |
| --- | --- |
| `azure/Test-API/apiproxy/` | `ApigeeToApimMigrationTool.Test/TestBundles/Test-API/apiproxy/` |
| `azure/GetSharedFlow/sharedflowbundle/` | `ApigeeToApimMigrationTool.Test/TestBundles/GetSharedFlow/sharedflowbundle/` |

The files are unchanged (including their UTF-8 byte order marks). They are
used under the MIT licence, reproduced below.

```
MIT License

Copyright (c) Microsoft Corporation.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE
```

## tests/fixtures/e2e/input/Test-API/ and tests/fixtures/e2e/input/GetSharedFlow/

Unchanged copies of `azure/Test-API/` and `azure/GetSharedFlow/` above (same source, commit and MIT licence),
used by the CP9 end-to-end fixture run.

## Everything else

All other bundles under `tests/fixtures/apigee/` (orders-api, audit-flow,
malformed/*) are hand-authored for a2m.
