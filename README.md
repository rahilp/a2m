# a2m

a2m migrates Apigee proxy bundle exports to Mule 4 projects. It reads a folder of exported bundles (zipped or
unzipped `apiproxy/` folders, plus any shared flow bundles they call), writes one Mule 4 project per proxy, checks
each project as far as the tools on the machine allow, and sorts every proxy into one of three buckets with a
report that says exactly what was migrated, how, and what a person still has to check.

Standard policies are translated by deterministic templates. An AI model is used only for custom code
(JavaScript, Python and Java callouts), conditions a2m's own translator cannot handle, and fixes for apps whose
tests fail. Every AI result carries a confidence level and notes, and low confidence always needs review.

## Install

a2m needs Python 3.11 or newer.

```sh
pip install .              # from a checkout of this repository
pip install ".[claude]"    # with the Anthropic SDK, needed for --llm claude (the default)
pip install ".[dev]"       # with pytest, ruff and mypy, for development
```

`uv pip install ".[claude]"` works the same way.

`--llm claude` reads the API key from the `ANTHROPIC_API_KEY` environment variable and nothing else; a missing key
stops the run before any proxy is processed, with one clear line. The model is `A2M_MODEL`, or a2m's default
model when that is not set. `--llm fake` uses canned answers, needs no key and never opens a network connection
(the folder named by `A2M_FAKE_LLM_DIR` can hold your own canned answers).

```sh
export ANTHROPIC_API_KEY=...   # only for --llm claude
```

## Local Mule toolchain (optional)

Building and running the generated apps needs Java, Maven and a Mule runtime. a2m is verified against this
toolchain:

- Java: Temurin 17
- Maven: 3.9
- Mule runtime: Mule Kernel CE 4.9.0 (the free Community Edition standalone runtime)

The repository's `mise.toml` pins Temurin 17 and Maven 3.9 for [mise](https://mise.jdx.dev). Install mise, then from
the repository root:

```sh
mise install                                   # installs Temurin 17 and Maven 3.9 from mise.toml
mise exec -- java -version                     # every tool runs through mise exec --
mise exec -- a2m migrate ./apigee-exports --out ./results --mock-backends
```

Download the Mule Kernel CE 4.9.0 standalone distribution (`org.mule.distributions:mule-standalone:4.9.0`, a
`.tar.gz` in the MuleSoft releases repository https://repository.mulesoft.org/nexus/content/repositories/releases/
under `org/mule/distributions/mule-standalone/4.9.0/`), unpack it, and point `MULE_HOME` at the unpacked folder (the
one holding `bin/mule`). `mise.toml` sets `MULE_HOME` to `~/.local/share/mule/mule-standalone-4.9.0`.
`A2M_MULE_HOME` overrides `MULE_HOME` when both are set, so you can point a2m at another runtime without changing
`MULE_HOME`.

The generated `pom.xml` pins the connector and plugin versions proven to deploy on Mule Kernel CE 4.9.0:

| Artifact | Version |
| --- | --- |
| mule-maven-plugin | 4.10.1 |
| mule-http-connector | 1.11.3 |
| mule-objectstore-connector | 1.2.2 |
| mule-validation-module | 2.0.9 |

Newer connector versions fail to deploy on Mule 4.9.0, so a2m keeps these pins until the runtime moves on.

Without these tools a2m still runs and labels each proxy static: it generates every project, and its run log
says which tool was not found and that the build was skipped. Nothing about the generation depends on Java or
Maven.

## Usage

```sh
a2m migrate ./apigee-exports --out ./results [--only NAME] [--resume | --force]
            [--golden ./recordings] [--golden-ignore-header NAME] [--mock-backends]
            [--max-fix-attempts 3] [--llm claude|fake] [--no-runtime]
```

Examples:

```sh
# Generate everything, run nothing (fast, no Java needed): every proxy is static at best.
a2m migrate ./apigee-exports --out ./results --llm fake --no-runtime

# Build, deploy and test every app on the local Mule runtime against a mock backend.
mise exec -- a2m migrate ./apigee-exports --out ./results --mock-backends

# Compare the apps against responses recorded from Apigee.
mise exec -- a2m migrate ./apigee-exports --out ./results --golden ./recordings
```

| Option | What it does |
| --- | --- |
| `EXPORTS` | The folder that holds the bundles: one folder or `.zip` per proxy or shared flow bundle. |
| `--out DIR` | The results folder (required). It must be new, empty, or an earlier a2m results folder used with `--resume` or `--force`. |
| `--only NAME` | Process only the proxy with this name. The other proxies already in the results folder stay as they are, and the summary still lists them. |
| `--resume` | Continue an earlier run: proxies with a `.done` marker are skipped. |
| `--force` | Redo every selected proxy, even finished ones. Cannot be combined with `--resume`. |
| `--golden DIR` | Recorded Apigee exchanges, one sub-folder per proxy holding one JSON file per exchange. A proxy with recordings is replayed against them instead of running a2m's test battery. |
| `--golden-ignore-header NAME` | A header that differs on every call (for example `X-Apigee-Message-ID`) and is not compared in a golden replay. Repeat it for more headers. |
| `--mock-backends` | Run each app against a local mock backend that records the calls it gets. a2m never calls a proxy's real backends. |
| `--max-fix-attempts N` | How many times the AI may try to fix an app whose tests fail (default 3; 0 turns the fix loop off). |
| `--llm claude\|fake` | The AI provider for custom code, untranslatable conditions and fixes (default `claude`). |
| `--no-runtime` | Skip everything that needs Java, Maven or the Mule runtime. Every proxy is then static at best. |

a2m runs the apps only with `--mock-backends` or `--golden`, only without `--no-runtime`, and only when Java,
Maven and the Mule runtime are found. One proxy failing never stops the batch, and an interrupted run can be
finished with `--resume`.

## The three buckets

Every proxy in the input ends up in exactly one bucket, with a `REPORT.md` that says why.

- **verified**: the app ran on the local Mule runtime and passed every test (verification type golden or
  battery, nothing failing), every step, policy and condition was mapped by a template or by an AI translation
  given with medium or high confidence (nothing skipped), and no review flag was raised. A policy the test battery
  has no test for is listed in the report but does not by itself keep a proxy out of verified. A golden replay
  does not measure which policies the recordings reached, so its report lists every generated policy step for a
  person to check against the recordings. A proxy that was only built is never verified.
- **needs-review**: a2m produced a Mule project, but a person must check something first: an item was skipped
  (for example a policy type a2m has no template for), the AI translated something with low confidence, a test still fails
  after the AI fix attempts, the build or deploy failed, the app was never run, or a time-window policy
  (SpikeArrest, Quota) needs its timing checked by hand. The report asks a specific question for each and
  suggests a fix.
- **unsupported**: a2m could not produce a project at all: the bundle could not be read (for example malformed
  XML, or a step that names a policy file that does not exist) or a2m failed on it. Its folder holds only a
  `REPORT.md` naming the cause.

## Verification types

Each proxy with a project gets one of four labels in its report and in the summary.

- **golden**: the app was built, deployed on the local runtime and every exchange recorded from Apigee (given with
  `--golden`) was replayed: every response (status, headers, body) and every call the backend received matched.
  It proves the app answers those recorded requests the way Apigee did. It does not prove anything about requests
  that were not recorded, nor about policies the recorded requests never reached: a2m does not track which
  policies a replay exercised, so the report says coverage is not measured and lists the generated policy steps.
- **battery**: the app was built, deployed on the local runtime and passed a2m's own test cases for its policies
  (for example a valid, a missing and a bad API key, or a call under a rate limit) against a local mock backend,
  with the calls the backend received compared too. It proves the generated policies behave as a2m's model of
  Apigee expects for those cases. It does not prove behaviour a2m has no test case for, which the report lists as
  untested policies, nor timing over a real time window.
- **static**: the project was generated, and maybe built, but never run against tests: built only or not built at
  all (`--no-runtime`, a missing tool, no `--mock-backends` or `--golden`, golden recordings that cannot be read,
  no test case for any of its policies, or an app that needs Mule Enterprise).
  It proves only that a2m wrote a well-formed Mule project. It does not prove that the project builds, deploys or
  behaves like the Apigee proxy, because it was never run. A static proxy is always in needs-review.
- **failed**: the Maven build failed, the app did not deploy or start, or at least one test still fails after the
  AI fix attempts. The report shows the failing tests or the build log (also saved in `diffs/`), and the proxy
  is always in needs-review.

## What a local runtime check proves

A battery or golden label means the project built with Maven, deployed on Mule Kernel CE 4.9.0 on this machine,
and passed its tests against a local mock backend. That is a real build and a real deployment, not a simulation.

It does not prove behaviour on:

- Mule Enterprise runtimes (apps that need Enterprise components are never started locally, so they stay static),
- CloudHub or any other hosted deployment,
- API Manager policies applied on top of the app,
- real backends (only the mock backend is ever called),
- production load and timing (rate limits and quotas are checked with a few calls, not over their real window).

## The results folder

```
results/
├── SUMMARY.md        counts per bucket, per policy type and per verification type
├── summary.json      the same data, machine-readable
├── run.log           timestamped log of every step
├── verified/<proxy>/      mule-app/  REPORT.md
├── needs-review/<proxy>/  mule-app/  REPORT.md  diffs/
└── unsupported/<proxy>/   REPORT.md
```

A finished proxy also has a `.done` marker (used by `--resume`), its verification result as `verification.json`,
and `.a2m-summary.json`, the facts the batch summary is built from. `diffs/` holds the diff of each failing test,
the end of the Maven or Mule log when the build or deploy failed, and the diff of each AI fix attempt (it may be
empty). Shared flow bundles get no folder of their own: their steps and policies appear in the report of every
proxy that calls them.

Each `REPORT.md` has:

- one table row per step, policy file and condition in the bundle (and in the shared flows it calls), with its
  Mule result and the method used: `template`, `ai` (with the AI's confidence) or `skipped` (with the reason).
  Nothing in the input is left out: the number of rows of each kind equals the number of steps, policy files and
  conditions in the bundle;
- the verification type, the test results and the policies no test covered;
- the AI fix attempts, each with what it changed and whether it helped;
- for a needs-review proxy, a question for a human and a suggested fix for each open item.

Reports never contain credentials (API keys, Authorization values and cookies are masked), callout source code
or timestamps, and they show the results, input and recordings folders as `<results>`, `<exports>` and
`<golden>` instead of their absolute paths.

## Limitations

- OAuthV2 is not supported: OAuthV2 policies are listed as skipped (unsupported) and the proxy goes to
  needs-review.
- Fault rules (FaultRules and DefaultFaultRule) are not generated: their steps and conditions are listed as
  skipped with a reason naming the fault rule.
- Policy types without a template (for example KeyValueMapOperations or any custom type) are skipped and listed.
- Steps in PostClientFlow and EventFlow are not generated.
- The local runtime is Mule Kernel (Community Edition); apps that need Mule Enterprise are generated but not run.
- Local files only: a2m does not download from the Apigee Management API, and it does not deploy to Anypoint
  Platform or upload API Manager policies.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | The batch finished (proxies may still be needs-review or unsupported because their bundle was refused). |
| 1 | At least one proxy failed with an error inside a2m, or the results could not be written. |
| 2 | Usage error: bad flags, a missing input folder, or a results folder in use or not reusable. |
| 130 | Interrupted with Ctrl-C (128 plus the signal number when stopped by SIGTERM or SIGHUP). |

## Development

```sh
pip install -e ".[dev]"
pytest -q                                   # the regression suite: no Java, Maven, network or API key needed
A2M_REQUIRE_RUNTIME=1 mise exec -- pytest -q -m runtime   # the tests on the real local Mule runtime
ruff check a2m tests && mypy a2m
```

Golden copies of the end-to-end run live in `tests/golden/e2e/` and are only rewritten with `A2M_UPDATE_GOLDEN=1`.
