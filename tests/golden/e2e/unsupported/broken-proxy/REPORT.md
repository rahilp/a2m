# a2m report: broken-proxy

Source: broken-proxy
Bucket: unsupported

a2m could not produce a Mule project for this proxy, so nothing was built or tested.

## Why it is unsupported

a2m refused the bundle: broken-proxy: apiproxy/proxies/default.xml is not well-formed XML (unclosed token: line 20, column 4)

## Suggested fix

Fix the bundle so it reads cleanly (the cause above names the file and the problem), or export it again from Apigee, then rerun a2m with --force --only broken-proxy.
