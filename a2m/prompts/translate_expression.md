# Translate an Apigee condition into a structured condition for Mule 4

You are migrating an Apigee API proxy to a Mule 4 application. a2m's own
condition translator could not translate the condition below exactly. Restate
the condition as a small JSON tree that gives the same true or false result
for every request. You do not write DataWeave: a2m checks the tree and writes
the Mule `choice` router's `when expression="#[...]"` from it itself.

## Where the condition is

- Proxy: {{proxy}}
- Condition of: {{owner}}
- Place in the flow: {{location}}
- Side of the flow: {{side}}

## The Apigee condition

```
{{original}}
```

Why a2m's translator refused it: {{refusal}}

## Values shown as placeholders

Every literal value of the condition above is shown as a placeholder; its
variables and operators are shown as they are. A text placeholder such as
`«v1»` stands for text: write it as the `VALUE` (`"value": "«v1»"`), and a2m
puts the exact value back before it checks the tree. A number placeholder
such as `«n2»` stands for a number: the condition compares with a number,
which a2m does not translate, so decline it (a2m refuses any translation of a
condition that holds one). A placeholder a2m did not show is refused.

## The condition tree

Each node is one JSON object of one of these shapes:

- `{"and": [NODE, NODE, ...]}`: every part holds (at least two parts).
- `{"or": [NODE, NODE, ...]}`: at least one part holds (at least two parts).
- `{"not": NODE}`: the part does not hold.
- `{"variable": "NAME", "operator": "OPERATOR", "value": VALUE}`: one Apigee
  variable compared with a literal.

`NAME` is the Apigee variable exactly as the condition names it:
`request.verb`, `proxy.pathsuffix`, `request.header.NAME`,
`request.queryparam.NAME`, `response.header.NAME` (response side only), or
one of the proxy's own flow variables. Every other Apigee built-in variable
(`response.status.code`, `client.ip`, `verifyapikey.*`, `developer.*`,
`apiproduct.*`, `system.*`, the message body, ...) does not exist in the
generated app: decline instead.

`OPERATOR` is one of:

- `equals`, `not-equals`: `=`, `==`, `Equals`; `!=`, `NotEquals`.
- `equals-ignore-case`: `:=`, `EqualsCaseInsensitive`.
- `starts-with`: `=|`, `StartsWith`.
- `matches`: `~`, `Like`, `Matches` (Apigee's `*` wildcard pattern).
- `matches-path`: `~/`, `LikePath`, `MatchesPath`.
- `java-regex`: `~~`, `JavaRegex`.

`VALUE` is the literal text as a JSON string, or `null` with `equals` and
`not-equals`. a2m does not translate numbers, `true`, `false` or the ordering
operators (`>`, `>=`, `<`, `<=`): decline a condition that needs them.

## How a2m writes it in Mule 4

a2m reads each variable the way its own translator does and writes the
DataWeave expression for the `when`. For example, on the request side,

```json
{"and": [{"variable": "request.verb", "operator": "equals", "value": "POST"},
         {"variable": "request.header.Content-Type", "operator": "starts-with", "value": "application/json"}]}
```

becomes (a header is its first comma-separated value, trimmed; `default`
keeps a missing header from failing):

```dataweave
((attributes.method == "POST") and (((if (attributes.headers['content-type'] == null) null else trim((attributes.headers['content-type'] splitBy ",")[0] default "")) default "") startsWith "application/json"))
```

A full script, as used in a Transform Message, would start with `%dw 2.0`; a
`when` holds only the expression after the `---`. On the response side,
request variables read the request as a2m sent it to the target.

## Rules

- The tree must depend on the request or flow variables exactly as the
  Apigee condition does. a2m refuses a tree whose result is the same
  whatever the values it reads (always true or always false), a variable it
  cannot read faithfully at this point, and a value an earlier step may have
  changed. When part of the condition cannot be expressed exactly, decline.
- Group `AND` and `OR` exactly as Apigee evaluates them; if that is unclear,
  decline.

## Your answer

Return a single JSON object and no other text:

```json
{"status": "translated", "confidence": "high", "notes": "how the operators were mapped", "condition": {"variable": "request.verb", "operator": "equals", "value": "GET"}}
```

`high`: the same result for every request. `medium`: the same for normal
requests, with the assumptions in the notes. `low`: unsure. If the condition
cannot be expressed exactly in this tree, return:

```json
{"status": "cannot_translate", "reason": "the part that has no exact equivalent"}
```
