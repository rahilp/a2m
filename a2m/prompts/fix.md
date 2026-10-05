# Fix a generated Mule 4 app whose tests fail

You are helping migrate an Apigee API proxy to a Mule 4 application. a2m
generated the Mule app below, ran it on a local Mule runtime against a mock
backend, and some of its tests failed. Change the Mule configuration so the
app behaves like the original Apigee proxy and the failing tests pass, without
breaking the tests that pass now.

This is fix attempt {{attempt}} of at most {{max_attempts}} for proxy {{proxy}}.

## Placeholders

Every literal value below (element text, attribute values, string literals in
DataWeave, conditions and code, URL parts and query items, and the expected and
actual values in the test diffs) is shown as a placeholder such as `«v3»`.
Shown as they are: element and attribute names, the names of the proxy's
policies, steps, flows, variables, headers and parameters, object keys,
numbers, booleans, HTTP methods, MIME types and the structure of the code.
In a payload, a literal value, a template or other data of a policy, every
value is a placeholder, names and numbers included. A name is shown only where
it stands as a name: a string literal, a quoted selector such as
`vars['«v2»']`, a regular expression or any other value is a placeholder even
when it equals a name (an unquoted selector such as `vars.total` stays).

- The same placeholder always stands for the same value, wherever it appears:
  in the policies, the Mule files and the test diffs.
- To use a value that is shown as a placeholder, write its placeholder. a2m
  puts the exact value back before it checks and writes the file. For example,
  when a diff says `expected '«v7»', actual '«v5»'` and the Mule file holds
  `'«v5»'`, write `'«v7»'` there.
- Inside an expression, write a placeholder inside a string literal, between
  its quotes (`'«v7»'` or `"«v7»"`). a2m writes the value escaped for that
  string (quotes, backslashes, `$`, new lines), so do not escape it yourself.
  A placeholder outside any string literal of an expression is refused.
- You may write new literal values of your own as plain text.
- Never invent a placeholder: a2m refuses a fix that uses one it did not show.

## The original Apigee policies

The policy of every step a2m generated (from a template or translated by the
AI), with the source code of any custom code policy. The proxy's other
policies (unsupported, skipped, or used by no flow step) are listed by name
and type only:

{{original}}

## The current Mule configuration

Every file under `src/main/mule/` as it is now (this is the version the
failing tests ran against):

{{mule}}

## What failed

Each failing test, with what was expected and what the app actually did:

{{diff}}

## Steps you may change in place

These steps (`doc:name`) were translated by the AI. They are the only
generated elements you may change in place; every other element a2m generated
must stay exactly as it is:

{{ai_steps}}

## The previous attempt

{{previous}}

When the previous attempt was refused or undone, do not send the same answer
again: change what the reason above names.

## Rules for the fix

- Change only the Mule configuration files shown above (paths under
  `src/main/mule/`). a2m refuses any other path, and any file it was not
  shown.
- Send each file you change whole, as it should be after the fix. Keep every
  other part of the file exactly as it is: a2m checks every element you add
  or change, and refuses the whole fix when one of them is not allowed.
- Never remove, move or duplicate an element a2m generated, and never shorten
  a file ("rest unchanged"): a2m refuses a fix that does. You may change in
  place only a step that was translated by the AI (keep its `doc:name`), and
  you may add new elements between the generated ones.
- Never write a value shown masked as `*** (N chars)` (with or without a
  prefix): a2m refuses a fix that writes such a mask into a file.
- An element you add or change must be one of the Mule 4 core processors
  (set-variable, set-payload, remove-variable, logger, raise-error, choice,
  try, foreach, until-successful) with DataWeave in its expressions. No
  connectors, global configurations, flows, sub-flows, flow-ref, scripting,
  file or other modules, and no `${...}` property placeholders in the parts
  you add or change. Leave connector elements (http:, os:, and the like) and
  their attributes exactly as they are.
- A `choice` guard (`<when expression="#[...]">`) you add or change must be
  a simple test a2m can check: a header, query parameter, verb, flow
  variable or payload compared with a text literal, joined with `and` / `or`
  and `not (...)`, never a constant.
- Fix only what the failing tests show. Never invent behaviour the Apigee
  proxy does not have.

## Your answer

Answer with exactly one JSON object and nothing else:

```json
{"status": "fixed", "files": {"src/main/mule/proxy.xml": "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<mule ...>...</mule>\n"}, "confidence": "high", "notes": "what you changed and why"}
```

`files` maps each changed file's path (relative to the project folder, as
shown above) to its full new text. `confidence` is how sure you are that the
fix gives the Apigee proxy's behaviour: `high`, `medium` or `low`. When you
cannot fix it, answer instead:

```json
{"status": "cannot_fix", "reason": "why it cannot be fixed"}
```
