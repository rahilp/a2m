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
actual values in the test diffs) is shown as a placeholder. A text
placeholder such as `«v3»` stands for text; a number placeholder such as
`«n4»` stands for a number literal (of a condition, custom code, DataWeave or
JSON).
Shown as they are: element and attribute names, the names of the proxy's
policies, steps, flows, variables, headers and parameters, a2m's structural
numbers (a status code, a time limit, a DataWeave index such as `items[0]` or
version), booleans, HTTP methods, MIME types and the structure of the code.
Every quoted object key (of DataWeave, JSON or code) is a placeholder, and so
is every other number: in a condition, in custom code, in DataWeave arithmetic
or comparisons, in a policy property, a header or query value, a JSON value
(with its sign) or any other setting of a policy or a Mule file.
In a payload, a literal value, a template or other data of a policy, every
value is a placeholder, names and numbers included. A name is shown only where
it stands as a name: a string literal, a quoted selector such as
`vars['«v2»']`, a regular expression or any other value is a placeholder even
when it equals a name (an unquoted selector such as `vars.total` stays).
In the test diffs, the status codes and the counts of calls and fields are
shown as they are; every value of a JSON body is a placeholder (a number, `true`
and `false` included, `null` stays), and so is an array index and every field
name that is not a plain word of letters (`body field «v6»[«n2»].total`).

- The same placeholder always stands for the same value, wherever it appears:
  in the policies, the Mule files and the test diffs.
- To use a value that is shown as a placeholder, write its placeholder. a2m
  puts the exact value back before it checks and writes the file. For example,
  when a diff says `expected '«v7»', actual '«v5»'` and the Mule file holds
  `'«v5»'`, write `'«v7»'` there.
- Inside DataWeave (every `#[...]` expression and the script of every
  Transform Message part, with or without `%dw 2.0`; a value that starts
  with `#[` and ends with `]` with no other `#[` is one expression) and JSON
  text (a value that is a JSON object or array as a whole, `${...}`
  properties in it included), write a text placeholder inside
  a string literal, between its quotes (`'«v7»'` or `"«v7»"`). a2m writes the
  value escaped for that string (quotes, backslashes, `$`, new lines), so do
  not escape it yourself. A text placeholder you write outside any string
  literal there is refused; one a2m showed bare (a word of JSON text) is put
  back only where you leave it exactly as shown. A regular expression a2m showed
  (`matches /«v3»/`, `replace /«v3»/ with ''`), the stretch after a "/"
  that could start one (`sum(payload.x) / «v4» / «n5»`), and a stretch of
  code a2m could not read and showed as one placeholder
  (`sum(payload.x) «v6»`), is put back as it was only when you leave it
  exactly as shown, in the same element, after the same code: you may edit
  the rest of that expression or script, but keep each such stretch as it
  is. Re-indenting the code around it, wrapping that code in `if` or
  `do { }`, and adding a new element before or after its element (even
  while you edit that element) keep it as shown: a2m pairs each element you
  write with one it showed by its other attributes, then by its content, so
  when two elements you write are equally like one it showed, it cannot
  tell which stands for it and refuses. Putting it inside a new `#[...]` or
  string, or taking it out of one, is a change. Any other placeholder in a
  regular expression or such a stretch (moved there, copied from another
  element, written there, or changed) is refused. Never move one into a
  string literal or quote it instead (that would match text literally, or
  turn code into text). A placeholder you write later on the line of such
  a "/", with another "/" or a quote on that line, is refused too: write
  that code on a line of its own. A line you add with such a "/" must close
  on that line every quote and comment it opens, or every placeholder below
  it is refused. In a plain attribute value or element text, write it as it is
  (`value="«v7»"`); a2m refuses a value there that would start a Mule
  expression (`#[`) where it did not show one, or make a property
  placeholder (`${«v7»}`). In a value that is JSON text, a new `#[...]`
  makes a2m refuse the text placeholders around it: write the whole value
  as one expression instead (`#[{"id": '«v7»'}]`).
- Write a number placeholder where a number goes, without quotes
  (`vars.count + «n4»`). In an expression a2m puts back the same number in a
  form DataWeave reads (a Java `5000L` becomes `5000`, a hexadecimal number
  becomes decimal, a negative number is put in parentheses); in an attribute
  value or element text it writes the number in plain digits (`5e3` and
  `5000.0` become `5000`, `2.5e-3` becomes `0.0025`). a2m refuses the fix when
  it cannot write that number exactly. Never put a number placeholder inside
  quotes: to make text of it, write `(«n4» as String)`.
  A number placeholder stands for the whole literal, its dot and exponent
  included (`.07` is one placeholder).
- Write every placeholder outside quotes, and every number placeholder,
  alone: one joined to a letter, a digit, a ".", "_", "$", a quote or another
  placeholder (`0.«n4»`, `.«n4»`, `«n4»0`, `«n4»e3`, `«n4»«n5»`) is refused;
  one a2m showed that way (`payload.«n4»`) is put back only where you leave
  it, and what is right next to it, exactly as shown.
- Inside the `$( )` of a DataWeave string you write code: put a text
  placeholder there in quotes of its own (`"Bearer $('«v7»')"`); one written
  bare there is refused.
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
