# bad: the fix request sends the original policy XML verbatim

The deliberately bad example for the credential canary check
(`tools/checks/credential_canary.py`), in the same layout as
`../credential-variants`: a minimal mini package shaped like the real `a2m`
package. Point the check at this directory.

`a2m/verify/fix_loop.py::build_request` masks the Mule files and the
failing-test diff through `Masker`, but puts the original Apigee policy XML
into the request's `original` field and into the prompt exactly as read from
the bundle. A literal backend credential an AssignMessage sets, for example
`<Header name="X-Partner-Api-Key">CANARY-...</Header>`, reaches the AI
provider unmasked, although the masker would hide it. This is the leak
reviewers found in CP8 round 1.

Where: `a2m/verify/fix_loop.py`, `build_request()`, the `original=policy_xml`
argument and the `{policy_xml}` part of the prompt.

No real credential is held here; the check plants obviously fake canaries.
