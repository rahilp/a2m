> DRAFT — inferred by ratchet-app-scout; review and edit.

# a2m engineering standards

Greenfield project: there is no product code yet, so each rule cites the brief
(`.ratchet/runs/a2m-python-cli-that-migrates-apigee-proxy-bundle/reference/brief.md`,
cited as "brief"), a reference repo under `/home/rahil/Projects/a2m-refs/`, or the
file that will hold the canonical example once its checkpoint lands. Once a
real file exists, replace the citation with it.

## What counts as a review finding (Rahil, 2026-10-02)

R1. A review finding must show a functional impact for a realistic input or
    use of a2m: wrong output, a crash or hang, data loss or deletion, a
    security problem (reading, writing or sending anything outside the places
    a2m is allowed to touch, leaking a secret), a broken contract from the
    brief or the approved plan, or a report that claims more than was checked.
    State the concrete input and the wrong result.
R2. These are NOT findings and must not be filed (mention them under "notes"
    at most): wording, naming, docstrings, comments, code structure or style
    the linter accepts, extra tests for behaviour that already works, and
    theoretical edge cases no real Apigee export or normal run would produce
    (exotic filesystem states, names Apigee does not allow, hypothetical
    attackers when the input is the user's own local export and nothing
    outside the results folder can be read, written or sent).
R3. When unsure, ask: "would a user of a2m see a different result or lose
    something?" If not, it is not a finding.

## Language, packaging, tooling

1. **Python 3.11+ only.** `pyproject.toml` declares `requires-python = ">=3.11"`.
   The project venv is `.venv` (CPython 3.11.16, the supported floor), so 3.12+
   only syntax or stdlib (e.g. `type X = ...`, PEP 695 generics,
   `itertools.batched`) is not allowed. Example: brief "Hard rules".
2. **Package `a2m` with a console entry point** `a2m = "a2m.cli:main"` in
   `[project.scripts]`. `main(argv: list[str] | None = None) -> int` returns an
   exit code and never calls `sys.exit` inside library code. Example (CP1): `a2m/cli.py`.
3. **Runtime dependencies stay minimal and declared.** Every import outside the
   stdlib appears in `pyproject.toml`. The Anthropic SDK is an optional extra
   (e.g. `a2m[claude]`) and is imported lazily inside the claude provider, so
   `import a2m` and the whole test suite work without it. Dev deps (pytest)
   live in an optional `dev` extra or dependency group.
4. **Parse XML with the stdlib (`xml.etree.ElementTree`) or one declared parser**,
   never with regexes or string slicing. Untrusted bundle XML must not resolve
   external entities. Example (CP2): `a2m/parser/bundle.py`.

## Typing and data model

5. **Every public function and method has full type hints**, including return
   types. Use built-in generics (`list[str]`, `dict[str, X]`, `X | None`), not
   `typing.List` / `Optional`. Add `from __future__ import annotations` where
   forward references are needed.
6. **The IR is plain `@dataclass` (prefer `frozen=True`, `slots=True`) types
   with no behaviour beyond trivial helpers**, and must round-trip to JSON
   (`dataclasses.asdict` plus `json.dumps`) without custom objects. No pydantic
   unless the plan approves it. Example (CP2): `a2m/ir.py`; brief CP2.
7. **Closed vocabularies are `enum.StrEnum` (3.11+) or `Literal`**, not bare
   strings: buckets (`verified`, `needs-review`, `unsupported`), verification
   types (`golden`, `battery`, `static`, `failed` = the build or tests ran and something still fails, always needs-review), methods (`template`, `ai`, `skipped`),
   confidence (`high`, `medium`, `low`). Example: `a2m/ir.py`.

## Layout and layering

8. **Module layout follows the pipeline, one concern per package:**
   `a2m/cli.py` (argparse only) -> `a2m/engine.py` + `a2m/discovery.py` +
   `a2m/runlog.py` (batch engine, `.done`, resume) -> `a2m/parser/` (bundle
   loading and parsing to IR in `a2m/ir.py`) -> `a2m/generator/` (project
   generator) -> `a2m/policies/` (templates) -> `a2m/conditions/` (condition
   translator) -> `a2m/ai/` (providers) -> `a2m/verify/` (harness, mock
   backend, fix loop) -> `a2m/buckets.py`, `a2m/report.py`, `a2m/summary.py`
   (buckets, REPORT.md, SUMMARY.md, summary.json). The approved plan's
   `files_likely` is authoritative for exact file names. Reference for the parse step:
   `a2m-refs/Apigee_to_APIM_migration_tool/ApigeeToAzureApimMigrationTool.Logic/ApigeeXmlFileLoader.cs`.
9. **Imports point downstream only.** `a2m/parser/` and `a2m/ir.py` import
   nothing from `generator`, `policies`, `conditions`, `ai`, `verify`,
   `buckets`/`report`/`summary` or `cli`. Nothing
   imports `a2m.cli`. Business logic never lives in `cli.py`.
10. **One module per Apigee policy type** in `a2m/policies/` (e.g.
    `spike_arrest.py`, `quota.py`, `verify_api_key.py`, `assign_message.py`,
    `extract_variables.py`, `raise_fault.py`, `basic_authentication.py`,
    `access_control.py`), each registered in a single registry
    (`a2m/policies/__init__.py`). Mirrors
    `a2m-refs/Apigee_to_APIM_migration_tool/ApigeeToAzureApimMigrationTool.Logic/Transformations/PolicyTransformationFactory.cs`.
11. **LLM prompts are data, not code.** They live in `a2m/prompts/*.md` (or
    `.txt`), are loaded with `importlib.resources`, are shipped as package
    data, and can be edited without touching Python. No prompt text longer than
    one line inside `.py` files. Example (CP6): `a2m/prompts/`; brief CP6.
12. **The LLM sits behind one `Provider` protocol** (`a2m/ai/provider.py`) with
    `fake` and `claude` implementations, selected only by `--llm`. Nothing
    outside `a2m/ai/` imports the Anthropic SDK.

## Correctness rules that define the product

13. **Nothing is silently dropped.** Every policy, step, condition, fault rule
    and route rule in the input ends up as exactly one report row with a method
    of `template`, `ai` or `skipped` (unsupported, with a reason). Code paths
    that cannot handle something append to the unsupported list; there is no
    bare `continue`, `pass` or `except: pass` that discards input. A test
    counts input steps against report rows. Example: brief "Hard rules";
    pattern in `a2m-refs/mule-migration-assistant/mule-migration-tool-library/src/main/resources/report.yaml`.
14. **Unknown policy types are unsupported, never ignored.** The registry lookup
    returns an explicit unsupported result for unknown types (including
    OAuthV2 for this run). Contrast:
    `Transformations/NullTransformation.cs` in the Azure tool silently emits
    nothing; a2m must not copy that behaviour.
15. **Translators return "can't translate" rather than guess.** The expression
    translator returns a typed result (e.g. `Translated(dw: str)` or
    `Untranslatable(reason: str)`), never a best-effort string. Callers route
    `Untranslatable` to the AI layer or review. Example (CP5):
    `a2m/conditions/dataweave.py`; brief CP5.
16. **Verification type is honest.** `golden` only when responses were compared
    to recorded Apigee responses; `battery` only when the app actually ran and
    passed policy tests against mocks; `static` when it was only generated or
    built. "Verified" bucket requires every step mapped and every test passed;
    a build alone never yields `verified`. Example: brief CP7/CP8.
17. **AI output carries confidence and notes; low confidence goes to
    needs-review.** The report states the method `ai` for each such row, never
    `template`.
18. **Missing Java, Maven or Mule runtime is a clean skip, not a failure.**
    Detect with `shutil.which` (and an overridable env var such as
    `A2M_MULE_HOME`), log one clear message ("Maven not found; skipping build,
    verification type: static"), and continue. `--no-runtime` forces the same
    path. Tests never require these tools; tests that would use them are
    marked and skipped with `pytest.mark.skipif(shutil.which("mvn") is None, ...)`.
19. **One proxy failing never stops the batch.** The per-proxy loop catches
    exceptions at the proxy boundary only, records the traceback in `run.log`
    and the proxy REPORT.md, and continues. Inner code raises specific
    exceptions; no broad `except Exception` below the proxy boundary.
20. **Runs are resumable and idempotent.** A proxy's `.done` marker is written
    last, after its outputs are complete (write to a temp path then rename).
    `--resume` skips `.done` proxies; `--force` regenerates; the two are
    mutually exclusive.
21. **Generated Mule XML is well-formed with correct Mule 4 namespaces**
    (`http://www.mulesoft.org/schema/mule/core` etc. plus matching
    `xsi:schemaLocation`), built with an XML API, not string concatenation of
    untrusted values. User-derived values are escaped. Reference outputs:
    `a2m-refs/mule-migration-assistant/mule-migration-tool-e2e-tests/src/test/resources/e2e/proxy/http/output/src/main/mule/proxy.xml`
    and `.../e2e/http/http1/output/pom.xml` (`<packaging>mule-application</packaging>`).
22. **Output is deterministic.** Same input produces byte-identical files
    (sorted iteration over dicts and files, no timestamps or absolute paths in
    generated projects). Timestamps belong only in `run.log` and in the
    summary's run metadata fields.
23. **The output contract is the layout in the brief**: `SUMMARY.md`,
    `summary.json`, `run.log`, and `verified|needs-review|unsupported/<proxy>/`
    with `mule-app/`, `REPORT.md`, `diffs/` as specified. Path names are
    constants in one module, not repeated string literals.

## Errors, logging, security

24. **User-facing errors are one clear line plus a non-zero exit code**, no raw
    traceback at the CLI unless `--verbose`/debug. Exit codes are documented
    in the README.
25. **Use `logging`, not `print`, inside the library.** Only `cli.py` writes
    to stdout/stderr directly. `run.log` gets timestamped entries for every
    step.
26. **Secrets come from the environment only** (`ANTHROPIC_API_KEY`). Never log
    or write a key to `run.log`, reports or fixtures. Missing key with
    `--llm claude` fails fast with a clear message.
27. **Input paths are untrusted.** Zip extraction rejects absolute paths and
    `..` members (zip-slip); outputs are written only under `--out`.

## Tests

28. **pytest only; tests live in `tests/` mirroring the package**
    (`tests/policies/test_spike_arrest.py`, etc.), fixtures in
    `tests/fixtures/`. Run with `.venv/bin/python -m pytest -q`.
29. **Test names carry the planned case ID**, e.g.
    `def test_CP2_T03_conditional_flow_steps_parsed():` or a docstring /
    `pytest.param(..., id="CP2-T03")`, so every case in
    `.ratchet/runs/<slug>/tests/CPn.json` is traceable to a test.
30. **No network and no API keys in tests.** All LLM calls use the `fake`
    provider with canned responses checked into `tests/fixtures/`. A test (or
    autouse fixture) fails if the claude provider or a socket to a non-local
    host is used. The mock backend binds to `127.0.0.1` only.
31. **Tests are hermetic.** Write only to `tmp_path`; never to the repo or
    `~`. No dependence on test order, wall-clock time or the machine's Java or
    Maven install.
32. **Golden-file snapshots for generated output** live under
    `tests/fixtures/expected/` (or `tests/golden/`), compared after XML
    canonicalisation; updating them requires an explicit flag (e.g.
    `A2M_UPDATE_GOLDEN=1`), never automatic.
33. **Third-party fixtures keep attribution.** Bundles copied from
    Azure/Apigee_to_APIM_migration_tool (MIT) sit under
    `tests/fixtures/apigee/azure/` with a `NOTICE`/`ATTRIBUTION.md` naming the
    source repo, commit `ab6cb88263f4f42dd470e444ed950fd3d97baec5` and MIT
    licence. Nothing from mule-migration-assistant (Apache-2.0) is copied
    verbatim without its licence header.
34. **Every bug fix ships with a test that fails on the pre-fix code.**

## Writing and commits

35. **No em dashes in user-facing text**: CLI output, REPORT.md, SUMMARY.md,
    README, prompts, error messages. Use a comma, colon, or plain hyphen.
36. **Copy is honest and matches behaviour.** Reports and README never claim
    more than was checked (see rule 16).
37. **No AI or agent attribution** in commits, PRs, code comments or generated
    files: no `Co-Authored-By` naming a model, no "Generated with" footers.
    Never bypass a git hook (`--no-verify`).
