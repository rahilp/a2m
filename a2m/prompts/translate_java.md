# Translate an Apigee Java callout into Mule 4

You are migrating an Apigee API proxy to a Mule 4 application. The JavaCallout
policy below runs the Java class whose source follows. Work out what the
class does to the message and flow variables, and express it as Mule 4
processors at the place where the callout ran. Use DataWeave (expressions in
core processors and its standard modules such as dw::Crypto) instead of Java.

## Where the callout runs

- Proxy: {{proxy}}
- Place in the flow: {{location}}
- Side of the flow: {{side}}
- Step: {{step}} (Apigee policy type {{policy_type}})
- Step right before it: {{before}}
- Step right after it: {{after}}

## The policy configuration (ClassName, Properties)

```xml
{{policy_xml}}
```

## The Java source ({{resource}})

```java
{{original}}
```

## How Apigee's Java message context maps to the generated Mule app

- `messageContext.getVariable("NAME")` / `setVariable("NAME", value)` for the
  proxy's own flow variables: `vars['NAME']` and
  `<set-variable variableName="NAME" .../>`, with the same name.
- `getMessage().getContent()` and `setContent(...)` are `payload`.
- Request headers on the request side: read
  `(vars.a2mRequestHeaders default attributes.headers)['name']` (names in lower
  case); to change them, set the whole map in `vars.a2mRequestHeaders`. Query
  parameters likewise with `vars.a2mRequestQuery` and `attributes.queryParams`.
- On the response side: the status code is `vars.httpStatus`, the response
  headers are the map `vars.responseHeaders`, and the request as it was sent is
  `vars.a2mSentRequest` (keys `method`, `pathSuffix`, `headers`,
  `queryParams`).
- `ExecutionResult.ABORT` (rejecting the call): set `vars.httpStatus`,
  `vars.responseHeaders` and the payload to the error response, then raise the
  error type `A2M:POLICY_FAULT`.
- Java hashing, encoding and string handling map to DataWeave functions (for
  example `dw::Crypto::hashWith`, `dw::core::Binaries::toBase64`); say in the
  notes when the output format differs from the Java code's.

- Apigee built-in variables not listed above (for example
  `verifyapikey.*`, `developer.*`, `apiproduct.*`, `client.ip`) do not exist
  in the generated app. Never read one as `vars['...']`, which would always
  be null; when the class needs one (`messageContext.getVariable("NAME")`), answer
  `cannot_translate`.

## Values earlier steps may have changed

Earlier steps on this path may have changed these values in Apigee in a way
the generated app may not carry over, so the Mule app can still hold the
caller's original value:

{{changed}}

If the class reads one of them, say so in the notes; a2m flags this step for
review either way.

## Mule 4 examples

A signature computed with DataWeave into a flow variable:

```xml
<set-variable variableName="signature" value="#[%dw 2.0 import dw::Crypto output text/plain --- Crypto::hashWith(write(payload, 'application/json') as Binary, 'SHA-256')]"/>
```

A whole new body built with Transform Message:

```xml
<ee:transform>
  <ee:message>
    <ee:set-payload><![CDATA[%dw 2.0
output application/json
---
{data: payload, signedBy: vars.signer default 'unknown'}]]></ee:set-payload>
  </ee:message>
</ee:transform>
```

## Rules for the Mule code

- Transform Message (`ee:transform`) for payload and variable changes, and
  Mule 4 core processors (set-variable, set-payload, remove-variable, choice,
  logger, raise-error, try, foreach, ...) holding DataWeave. No connectors,
  Java module calls, global configurations, flows, sub-flows or flow-ref,
  and no `ee:` element other than Transform Message.
- Write each Transform Message script inline (no `resource` files).
- Do not declare namespaces. Leave out `doc:name`: a2m labels the step itself.
- No `${...}` property placeholders.
- A `choice` guard (`<when expression="#[...]">`) and an error handler's
  `when` must be a simple test a2m can check: a read of a header
  (`attributes.headers['name']`), a query parameter, the verb, a flow variable
  (`vars['NAME']`) or the payload compared with a text literal (`==`, `!=`,
  `startsWith`), `isEmpty(...)` of a header or query parameter, joined with
  `and` / `or` (one kind per bracket level) and `not (...)`. Never `true`,
  `false` or a test that always gives the same result. a2m refuses any other
  guard and the step goes to review.
- Translate only what the class does; libraries you cannot see are a reason
  for low confidence, not for guessing.

## Your answer

Give one JSON object as your entire answer:

```json
{"status": "translated", "confidence": "low", "notes": "what is uncertain and why", "mule": "<set-variable variableName=\"signature\" value=\"#[...]\"/>", "writes": {"request_headers": [], "query_params": [], "verb": false, "payload": false, "response_headers": [], "variables": ["signature"]}}
```

`writes` lists what the ORIGINAL class writes in Apigee: the request headers
and query parameters it sets or removes (`request_headers`, `query_params`,
by name), whether it changes the request verb (`verb`) or the body of the
message on this side (`payload`), the response headers it sets or removes
(`response_headers`) and the flow variables it sets or removes (`variables`,
exact names). a2m compares it with what your Mule code writes; when the two
differ, or `writes` is missing, a2m assumes the step may change anything, so
later conditions on the request may not be translatable.

a2m reads the header and query parameter keys your Mule code writes only from
writes of one literal key each: `(vars.a2mRequestHeaders default
attributes.headers) ++ {'name': value}` to set one, or `(vars.a2mRequestHeaders
default attributes.headers) - 'name'` to remove one (the same with
`vars.a2mRequestQuery` and `attributes.queryParams`, and with
`vars.responseHeaders` and `{}`). Use one such write per key. Any other write
of those maps, of the attributes or of the payload counts as changing all of
it. Header names are compared without case, query parameter and variable
names exactly.

Confidence `high` means the Mule code gives the same results as the class for
every message, `medium` that it does for expected messages (with the
assumptions in the notes), `low` that you are not sure. When the class cannot
be reproduced in Mule 4 without Java, answer:

```json
{"status": "cannot_translate", "reason": "the part with no Mule equivalent"}
```
