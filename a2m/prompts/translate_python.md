# Translate an Apigee Python script policy into Mule 4

You are migrating an Apigee API proxy to a Mule 4 application. The Python
(Jython) script below runs as a Script policy. Rewrite what it does as Mule 4
processors placed where the script ran. Use DataWeave (in the expressions of
set-payload, set-variable and choice) for the logic.

## Where the script runs

- Proxy: {{proxy}}
- Place in the flow: {{location}}
- Side of the flow: {{side}}
- Step: {{step}} (Apigee policy type {{policy_type}})
- Step right before it: {{before}}
- Step right after it: {{after}}

## The policy configuration

```xml
{{policy_xml}}
```

## The Python source ({{resource}})

```python
{{original}}
```

Included scripts, if any:

```python
{{includes}}
```

## Values shown as placeholders

Every literal value of the code and of the policy configuration above is shown
as a placeholder. A text placeholder such as `«v1»` stands for text: each
string literal, comment and regular expression of the code, and each element
text and attribute value of the policy (the names it declares stay). A number
placeholder such as `«n2»` stands for a number: each number of the code. The
code's structure and identifiers, and the names of the proxy's steps and flows
are shown as they are. The same placeholder always stands for the same value.

- To use a text value in the Mule code, write its placeholder where the value
  goes: as a plain attribute value or element text (`variableName="«v2»"`),
  or inside a string literal of DataWeave or JSON text, between its quotes
  (`#['«v3»']`, `value='{"id": "«v3»"}'`). DataWeave is every `#[...]`
  expression and the script of every Transform Message part, with or without
  `%dw 2.0`; JSON text is a value that is a JSON object or array as a whole.
  a2m puts the exact value back, escaped for that place, before it checks
  your answer. In DataWeave and JSON text, a text placeholder outside any
  string literal is refused (written bare in JSON it would become another
  type, as `true` or a word that is no JSON), and so is one inside a regular
  expression or after a "/" that could start one.
- Write a number placeholder where a number goes, without quotes
  (`#[vars.count + «n4»]`, `responseTimeout="«n4»"`). In an expression a2m
  puts back the same number in a form DataWeave reads (`5000L` becomes
  `5000`, `0xFF` becomes `255`, a negative number is put in parentheses); in
  an attribute value or element text it writes the number in plain digits
  (`5e3` and `5000.0` become `5000`, `2.5e-3` becomes `0.0025`). a2m refuses
  the answer when it cannot write that number exactly. Never put a number
  placeholder inside quotes: to make text of it, write `(«n4» as String)`.
  A number placeholder stands for the whole literal, its dot and exponent
  included (`.07` is one placeholder).
- Write every placeholder outside quotes, and every number placeholder,
  alone: one joined to a letter, a digit, a ".", "_", "$", a quote or another
  placeholder (`0.«n4»`, `.«n4»`, `«n4»0`, `«n4»e3`, `«n4»«n5»`) is refused.
- Inside the `$( )` of a DataWeave string you write code: put a text
  placeholder there in quotes of its own (`#["Bearer $('«v3»')"]`); one
  written bare there is refused.
- A placeholder a2m did not show is refused.
- In `writes`, give a name shown as a placeholder as its placeholder
  (`"variables": ["«v2»"]`).

## How Apigee's Python variables map to the generated Mule app

- `flow.getVariable('request.header.NAME')` on the request side reads
  `attributes.headers['name']` (header names in lower case), or the changed
  headers in `vars.a2mRequestHeaders` when an earlier step changed them:
  `(vars.a2mRequestHeaders default attributes.headers)['name']`.
- `flow.setVariable('request.header.NAME', value)` changes the request the
  target gets: set the whole map in `vars.a2mRequestHeaders`, for example
  `(vars.a2mRequestHeaders default attributes.headers) ++ {'name': value}`.
- Query parameters work the same way with `vars.a2mRequestQuery` and
  `attributes.queryParams`; the request verb is `attributes.method` and the
  path below the base path is `attributes.maskedRequestPath`.
- The proxy's own flow variables: `flow.getVariable('NAME')` reads
  `vars['NAME']`, `flow.setVariable('NAME', value)` is
  `<set-variable variableName="NAME" .../>`, same name, dots included.
- `request.content` / `response.content` are `payload` (the request body before
  the target call, the response body after it).
- On the response side: the status code is `vars.httpStatus`, the response
  headers are the map `vars.responseHeaders`, and the request as it was sent is
  `vars.a2mSentRequest` (keys `method`, `pathSuffix`, `headers`,
  `queryParams`). Mule's `attributes` then hold the target's response.
- JSON parsing and building (`json.loads`, `json.dumps`) is plain DataWeave on
  `payload`; `print` becomes a `logger`.
- To reject the call (a raised exception in the script), set
  `vars.httpStatus`, `vars.responseHeaders` and the payload to the error
  response, then raise the error type `A2M:POLICY_FAULT`.

- Apigee built-in variables not listed above (for example
  `verifyapikey.*`, `developer.*`, `apiproduct.*`, `client.ip`) do not exist
  in the generated app. Never read one as `vars['...']`, which would always
  be null; when the script needs one (`flow.getVariable('NAME')`), answer
  `cannot_translate`.

## Values earlier steps may have changed

Earlier steps on this path may have changed these values in Apigee in a way
the generated app may not carry over, so the Mule app can still hold the
caller's original value:

{{changed}}

If the script reads one of them, say so in the notes; a2m flags this step for
review either way.

## Mule 4 examples

Changing the JSON response body with Transform Message:

```xml
<ee:transform>
  <ee:message>
    <ee:set-payload><![CDATA[%dw 2.0
output application/json
---
payload update { case total at .total -> total as Number }]]></ee:set-payload>
  </ee:message>
</ee:transform>
```

A flow variable read from the body, and a response header:

```xml
<ee:transform>
  <ee:variables>
    <ee:set-variable variableName="customerId"><![CDATA[payload.customer.id default '']]></ee:set-variable>
  </ee:variables>
</ee:transform>
<set-variable variableName="responseHeaders" value="#[output application/java --- (vars.responseHeaders default {}) ++ {'x-customer': vars.customerId}]"/>
```

## Rules for the Mule code

- Transform Message (`ee:transform`) for payload and variable changes, and
  Mule 4 core processors (set-variable, set-payload, remove-variable, choice,
  logger, raise-error, try, foreach, ...) with DataWeave expressions. No
  connectors, global configurations, flows, sub-flows or flow-ref, and no
  `ee:` element other than Transform Message.
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
- Keep to what this script does; do not add features.

## Your answer

Reply with one JSON object only, no other text:

```json
{"status": "translated", "confidence": "medium", "notes": "assumptions and differences", "mule": "<ee:transform><ee:message><ee:set-payload>...</ee:set-payload></ee:message></ee:transform>", "writes": {"request_headers": [], "query_params": [], "verb": false, "payload": true, "response_headers": [], "variables": []}}
```

`writes` lists what the ORIGINAL script writes in Apigee: the request headers
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

Use `high` confidence when the Mule code behaves like the script for every
request, `medium` when it does for the requests the script was written for
(state the assumptions in the notes), `low` when you are not sure. If the
script cannot be expressed faithfully in Mule 4, reply:

```json
{"status": "cannot_translate", "reason": "what has no Mule equivalent"}
```
