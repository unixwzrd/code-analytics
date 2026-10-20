# CLI reference

Reference for the current unreleased CLI: inputs, outputs, side effects, exit behavior and exact command syntax. Implementation history and planned capabilities are not part of this reference.

- [Worked examples and produced artifacts](cli-examples.md)
- [Web and terminal usage](ui-guide.md)
- [Report and diagram catalog](report-and-diagram-catalog.md)
- [Report schemas](report-schema.md)

## Contents

[Invocation](#invocation) · [Command families](#command-families) · [Static analysis](#static-analysis) · [Saved evidence](#saved-evidence) · [Project workspace](#project-workspace) · [Runtime evidence](#runtime-evidence) · [Configuration](#configuration) · [Output streams](#output-streams) · [Exit statuses](#posix-errno-based-exit-statuses) · [Detailed syntax](#detailed-command-syntax)

## Invocation

Collection: `code-analysis ui [REPORT]` defaults to the current directory as its confined collection root and enables confirmed managed jobs. Use `--collection-root PATH` to choose another root or `--no-collection` for previews without launch capability. `--allow-collection` explicitly selects the default enabled state. No startup or preview executes a workload or writes a workspace; every job requires confirmation and profiling separately requires execution acknowledgment. The lower-level `web` command remains opt-in through both `--collection-root` and `--allow-collection`. See [the collection guide](ui-guide.md#collection-command-planning).

Trusted environments: repeat `--collection-python /absolute/path/to/python` with `--collection-root` on `ui`, `web` or `tui` to offer additional interpreter choices. The default is the current interpreter. Each selected environment must already contain a compatible Code Analysis installation; no package installation or project-environment discovery occurs. Both analyzer and profile workload use the selected launcher. The UI accepts only startup-approved choice identities, not arbitrary executable paths.

Web and terminal collection forms also support `import-profile`, generating `runtime ingest` for saved cProfile/pstats inputs. Managed imports require an existing owned workspace and confirmed writes, not workload authorization. Imports without a static report open runtime-only diagnostic views in either UI; missing capture/source evidence stays partial and source previews remain unavailable.

Full static commands (`scan`, `clones`, `refactor`) accept repeatable `--exclude-dir NAME` to add exact directory basenames to configured exclusions. These are not path/glob patterns; invalid names return `EINVAL`.

```text
code-analysis COMMAND [OPTIONS]
code-analysis COMMAND --help
code-analysis runtime SUBCOMMAND --help
```

Options belong to the selected command, not to a universal global option set. The [detailed syntax](#detailed-command-syntax) lists every implemented leaf command and its options.

With **no arguments**, `code-analysis` launches `ui`: it discovers the latest validated analysis in an owned workspace in the current directory or its parents. If no workspace or analysis generation exists, it opens collection setup instead. Collection defaults to the current directory even when an existing report is loaded. On macOS it opens the browser by default. An explicitly missing report still returns `ENOENT`; damaged workspace/report errors are not hidden. Startup does not scan a project, create a workspace or authorize a runtime workload. Automation should always name a batch command.

`ui [REPORT]` starts a detached loopback server or reopens a compatible session, waits for readiness, and returns the prompt. `--no-open` only suppresses browser launch. `ui --foreground` and lower-level `web REPORT` remain terminal-controlled services; `web` launches a browser only with `--open`. Explicit UI reports can be JSON files or generation directories. Read-only source previews require an explicit root and matching source hashes.

`ui-stop [--session ID] [--cancel-jobs]` stops a capability-verified background session. Without an ID, it selects the single running session whose collection root is the current directory; multiple matches return `EINVAL` and list their IDs. Active jobs return `EBUSY` unless cancellation is explicitly authorized with `--cancel-jobs`. Browser **Stop Code Analysis** provides equivalent confirmation. Closing/reloading a tab never stops the server. Sessions with different roots, reports/input hashes, source permissions, interpreter/environment or collection permissions are not silently reused. Private launch receipts and logs live in `~/.cache/code-analysis/ui/` (override `CODE_ANALYSIS_UI_STATE_DIR` with an owned mode-0700 directory); they are local process state, not analytical artifacts. Startup logs/session URLs contain a capability token: do not share them.

## Command families

| Purpose | Commands | Inputs and effects |
| --- | --- | --- |
| Environment | `doctor`, `providers` | Read local interpreter/provider provenance; availability is not a successful analysis |
| Static analysis | `scan`, `clones`, `refactor`, `contracts`, `dict-shapes` | Read source without target imports/execution; normally publish a workspace generation |
| Saved investigation | `summary`, `query`, `candidates` | Validate saved reports; no source scan |
| Analysis comparison | `diff`, `regress` | Compare compatible saved reports; no source scan |
| Presentation | `export`, `html` | Derive requested artifacts from JSON |
| Browsing | `ui`, `web`, `index`, `tui` | Serve saved evidence or explicitly prepare/read a disposable index |
| UI lifecycle | `ui-stop` | Stop an authenticated background session; active jobs require `--cancel-jobs` |
| Retention | `workspace list/pin/unpin/prune` | List is read-only; other operations change owned local retention state |
| Decisions | `decisions list/set/export/import` | Maintain an explicit local ledger; never change analytical reports |
| Change evidence | `history` | Explicit Git collection, or retained snapshots with `--generations` |
| Coverage | `coverage-import`, `coverage-run` | Import data or execute an explicitly authorized workload |
| Simple profiles | `profile`, `profile-report`, `profile-diff` | Authorized collection or saved-profile presentation/comparison |
| Runtime evidence | `runtime ingest/run/render/compare/query` | Offline artifacts except explicitly authorized `run` |
| Semantic nomination | `semantic-nominate` | Optional model execution from an existing local directory; no download or target execution |

## Static analysis

`scan`, `clones` and `refactor` share the enabled analysis pipeline with different presentation emphasis. `contracts` and `dict-shapes` are Python-focused and disable unrelated later sections. The default source path is `.`.

| Option | Meaning |
| --- | --- |
| `--language` | Repeatable selection. General scans accept `auto/python/javascript/ruby`; focused commands accept `auto/python`. Do not combine `auto` with explicit values. |
| `--javascript-module-mode` | General-scan override: `auto/esm/commonjs/script` |
| `--config PATH` | Explicit TOML file |
| `--strictness` | Preset; supported CLI threshold overrides take precedence |
| `--json`, `--json-compact`, `--markdown` | Select output format; compact JSON is canonical analysis storage |
| `--output PATH`, `-o PATH` | Additional output file; does not disable workspace publication |
| `--no-workspace` | Disable project workspace/cache writes |
| `--workspace PATH` | Explicit owned storage, mutually exclusive with `--no-workspace` |
| `--quiet` | Suppress ordinary stdout, not diagnostics or status |
| `--fail-on-findings` | Apply the finding-policy exit condition |

Supported suffixes are `.py`, `.js`, `.mjs`, `.cjs`, `.jsx` and `.rb`, subject to language selection and optional providers. Unsupported explicit file targets return `EINVAL`; no selected supported source returns `ENODATA`. Unavailable parsers, malformed source and analytical bounds remain explicit, not successful empty reports.

**Selecting `src` does not disable test discovery.** Full commands still inspect configured test roots; set `test-roots = []` to omit test-impact discovery. Static analysis never executes tests. Git collection is explicit through `history`, not an ordinary scan side effect.

For predecessor script aliases and threshold mappings, see [the flag map](migration-from-function-analysis.md).

## Saved evidence

`query REPORT --call-site CALL_OCCURRENCE_ID [--json]` selects a saved syntactic invocation and its recorded navigation relationships. It does not resolve runtime dispatch. `export REPORT --projection static-call-sites` produces a bounded table; `--projection static-call-graph` produces source-to-invocation containment, not a runtime call graph. The table accepts occurrence, source-file, module and enclosing-subject selection; the graph currently accepts source-file and occurrence node IDs. Both retain source partiality and read only saved JSON.

`query` requires exactly one supported identity/path selector. Unknown IDs, invalid limits and unrelated continuation tokens fail with `EINVAL`. Applicable profile queries accept frame/edge identities. `candidates` applies filters before its visible deterministic ordering and bounds; decisions may affect the worklist, not analytical facts.

`diff BASELINE CANDIDATE` requires compatible schema, target and static configuration identities; command names can differ. Canonically identical reports produce `identical=true`, zero records and no comparison truncation, while retaining source partiality. Non-identical comparisons report global counts and show changes only by default; `--include-unchanged` restores the bounded inventory. Hidden unchanged records are not truncation.

`export` requires a projection and supported format. Defaults are 500 records, 100 nodes and 250 edges. Selection precedes bounds. Omitted eligible records remain explicit and return `ENODATA`; intentional filtering alone is not incompleteness. SVG/PNG require optional Graphviz. Missing or failed rendering never becomes a successful empty image.

`subject-findings` supports module-prefix/category/severity/rule filters: OR within a dimension, AND across dimensions. Dotted prefixes match a module or descendants; exact subject selection cannot be combined with module prefixes. `dependency-neighborhood` requires one module and supports depth and recorded edge-kind filters. See the [catalog](report-and-diagram-catalog.md) for supported selectors/formats per view.

## Project workspace

Source analysis normally creates/reuses a self-ignored `.code-analysis/` at the discovered logical project root. Explicit output files are additional exports.

```text
.code-analysis/
  workspace.json
  .gitignore
  generations/
    analysis/GENERATION_ID/{report.json,report.md,run.json,manifest.json}
    coverage/GENERATION_ID/...
    profile/GENERATION_ID/...
    analysis/latest -> GENERATION_ID
    coverage/latest -> GENERATION_ID
    profile/latest -> GENERATION_ID
  pins/
  cache/
  decisions/ledger.json          # when decisions are recorded
```

Manifests describe actual artifacts. Generations publish atomically; local manifests may contain physical provenance and timing, whereas portable JSON must not. Workspace/cache settings have a separate storage identity and do not change portable analytical configuration.

`workspace prune --dry-run` previews removals. Pruning validates owned direct children, does not follow arbitrary symlinks, does not remove pins and ignores live-process temporary generations. Foreign or malformed workspaces are not silently adopted.

Decision imports default to conflict error (`EEXIST`), with explicit `keep-local` or `use-incoming` alternatives. Records are append-only within atomically replaced validated ledgers. `history --generations` compares retained source hashes, not atomic commit coupling. Ordinary `history` defaults to Python Git paths and three shared changes for coupling.

### Indexed terminal browsing

`index REPORT --output NEW_DIRECTORY` prepares a disposable SQLite cache and manifest. The parent must exist and the destination must be new. Optional `--compare`, `--profile-report` and `--compare-profile` attach saved evidence. Preparation emits a JSON receipt; a valid partial-source cache still returns `ENODATA`. Preparation-bound failures do not publish a misleading cache.

`tui INDEX --report-sha256 SHA256` validates the expected canonical report hash and requires a terminal plus the optional Textual extra. Rebuild indexes after artifact/implementation changes. See the [UI guide](ui-guide.md#indexed-terminal-browsing) for navigation and exports.

**Ctrl+W** prepares a read-only loopback Web handoff of the current report. Finding, module and symbol dialogs also expose exact-selection handoff actions. Select the original saved JSON; its canonical hash must match the index. Browser opening is explicit through the trusted display configuration, with a copyable URL fallback. Exiting the terminal closes its Web server. See [terminal handoff](ui-guide.md#terminal-handoff-to-the-web-ui) for scope and lifecycle details.

## Runtime evidence

Only `coverage-run`, `profile` and `runtime run` execute workloads. They require `--allow-execution`; the first two also require a command after `--`. Workloads use argument arrays, bounded owned processes and controlled environments. Missing authorization returns `EPERM` before execution.

| Command | Input/output contract |
| --- | --- |
| `coverage-import REPORT DATA...` | CoverageData or supported coverage JSON; source mismatch stays unbound/partial |
| `coverage-run REPORT --allow-execution -- COMMAND...` | Authorized coverage workload and local raw/normalized evidence |
| `profile --source-report REPORT --allow-execution -- COMMAND...` | Simple Python profiling/repetitions and resource observations |
| `profile-report PROFILE` | Saved profile/generation or supported standalone input; missing binding remains partial |
| `profile-diff BASELINE CANDIDATE` | Comparison subject to source/workload identity and diagnostic-only rules |
| `runtime ingest --profile FILE...` | Offline stdlib/cProfile normalization; repeat `--profile` per input |
| `runtime render REPORT --output NEW_DIRECTORY` | Saved-only runtime bundle |
| `runtime query REPORT --section SECTION` | Saved JSON/TSV/Markdown with bounded records and optional frame |
| `runtime compare BASELINE CANDIDATE` | Complete matching identity, or explicit claim-ineligible `--diagnostic-only` |
| `runtime run --spec SPEC --allow-execution` | Managed lifecycle; output paths come from the strict local specification |

Offline runtime ingestion emits JSON on stdout without `--output`; with it, the command publishes a new report directory instead. The bundle contains `runtime-summary.json`, Markdown, functions/callers/callees TSV, identity/category/diagnostic JSON, DOT, graphics and a manifest. Optional rendering failures and bounds are recorded there. Portable pstats is returned by the ingestion API and retained by managed capture, not automatically published by offline bundle export.

`runtime ingest --workspace EXISTING_WORKSPACE` additionally retains the imported report and available sanitized portable pstats in an immutable profile generation, while still emitting JSON on stdout. It uses normal workspace retention and pinning. The workspace must already be owned; ingestion does not claim a new project or discover source. `--workspace` and standalone `--output` are mutually exclusive. The local generation identifies the importing command/process, not the historical capture's producer or process. Portable report identity, missing provenance and ENODATA remain unchanged. Imported generations are available to saved-profile selectors and the shared Web/terminal import workflow.

External `--metadata` accepts strict `CollectionMetadata`, not arbitrary application JSON. Sorted `producer_bindings` declare `profile` or `cprofile` against exact input SHA-256 values. Invalid/mismatched bindings return `EBADMSG`. Missing producers remain unknown; mixed producers are retained individually. Declarations are supplied evidence, not authenticated attestation. See the [metadata example](cli-examples.md#bind-an-external-profiler-declaration).

Managed specifications contain absolute paths, typed argv, lifecycle controls and optional source/external-input evidence. The current sidecar checks direct subject PID/start/parent/nonce, profile hash, completed serialization and `subject-interpreter` scope. **This is not whole-program or thread-coverage proof.** Local cProfile tests reproduced false cross-thread parents; managed callback-scope certification is proposed, not implemented. See the [threaded investigation](threaded-profile-investigation.md) and [runtime design](cprofile-runtime-analysis-specification.md). The syntax below—not design-stage examples—is the implemented interface.

Integer caller edges preserve counts and null unavailable timings. Process wall time, CPU telemetry, Python frame duration and opaque native/device work are distinct. Historical missing facts stay missing. Producer identification alone never qualifies performance comparisons.

## Configuration

Use `[tool.code-analysis]` in project TOML or an explicit `--config` file on commands that expose that option. See the [source-only configuration example](cli-examples.md#source-only-analysis-without-test-discovery).

| Setting | Role |
| --- | --- |
| `languages`, `source-roots`, `excludes`, `test-roots` | Analytical scope |
| `strictness`, `[tool.code-analysis.thresholds]` | Presets and thresholds; supported CLI overrides win |
| `[[tool.code-analysis.contract-boundaries]]` | Classification rules over portable contract/site identities; not runtime proof |
| `git-path-scope`, `[tool.code-analysis.phase-four-limits]` | Explicit history scope/limits |
| `[tool.code-analysis.workspace]` | Local cache/storage; overridden by explicit workspace CLI selection |

Configuration is strict and analysis-affecting values enter report identity. Storage paths do not. This is an entry point, not the complete TOML schema: domain details are in [architecture](architecture.md), [report schemas](report-schema.md) and the current configuration models.

## Output streams

- Batch stdout contains the requested representation unless redirected by a command-specific output option. JSON is not universal; `summary` renders text.
- Diagnostics and usage errors go to stderr; UI services also print their session URL there.
- `--quiet` applies only where listed and does not change completeness/status.
- `--output` is not uniform: analysis/export name a file; runtime bundles/index preparation require a new directory. Do not assume universal no-overwrite behavior.
- A partial artifact may be written with `ENODATA`. Neither file existence nor a nonzero status alone establishes artifact validity.
- Query, comparison, export and rendering do not rescan source.
- No-argument launch, `ui`, `web` and `tui` are interactive exceptions; do not use them as unattended health checks.

See [status-preserving automation](cli-examples.md#preserve-status-in-automation).

## POSIX errno-based exit statuses

The application uses host Python `errno` values as its nonzero status policy without redefining their symbolic meanings. Resolve values on the executing host; numeric values are not portable. This is an application exit contract based on errno—not a claim that POSIX defines errno numbers as universal process exit statuses. Some symbols are platform extensions.

| Status | Application use |
| --- | --- |
| `0` | Operation succeeded under its policy; not refactoring authorization or exhaustive coverage |
| `EPERM` | Missing execution authorization, forbidden environment addition or finding-policy rejection |
| `EINVAL` | Invalid usage/selector/limit, unsupported options or incompatible comparison |
| `ENODATA` | Incomplete/unavailable evidence or bounded omissions |
| `EBADMSG` | Malformed configuration/artifact or inconsistent hash binding |
| `EPROTO` | Provider/workload failure, unexpected status or managed ownership mismatch |
| `ETIMEDOUT` | Owned execution exceeded its deadline |
| `EFAULT` | Invalid internal boundary reached |
| `ENOENT`, `EACCES`, `ENOTDIR`, `EROFS` | Native filesystem failures |
| `EEXIST` | Existing no-overwrite destination or decision conflict |
| `ELOOP` | Disallowed symlink traversal |
| `ENOTTY` | Terminal invocation without a terminal |

Positive filesystem `OSError.errno` values are preserved where surfaced. Invalid argument parsing returns `EINVAL`, not argparse's usual `2`. Do not hard-code the qualification machine's values in monitoring.

## Detailed command syntax

[ui-stop](#command-ui-stop) stops a background browser session; its syntax is listed immediately after `ui`.

[doctor](#command-doctor) · [providers](#command-providers) · [scan](#command-scan) · [clones](#command-clones) · [refactor](#command-refactor) · [contracts](#command-contracts) · [dict-shapes](#command-dict-shapes) · [summary](#command-summary) · [index](#command-index) · [tui](#command-tui) · [html](#command-html) · [web](#command-web) · [ui](#command-ui) · [candidates](#command-candidates) · [decisions list](#command-decisions-list) · [decisions set](#command-decisions-set) · [decisions export](#command-decisions-export) · [decisions import](#command-decisions-import) · [query](#command-query) · [diff](#command-diff) · [regress](#command-regress) · [semantic-nominate](#command-semantic-nominate) · [export](#command-export) · [workspace list](#command-workspace-list) · [workspace pin](#command-workspace-pin) · [workspace unpin](#command-workspace-unpin) · [workspace prune](#command-workspace-prune) · [history](#command-history) · [coverage-import](#command-coverage-import) · [coverage-run](#command-coverage-run) · [profile](#command-profile) · [profile-report](#command-profile-report) · [profile-diff](#command-profile-diff) · [runtime ingest](#command-runtime-ingest) · [runtime render](#command-runtime-render) · [runtime compare](#command-runtime-compare) · [runtime run](#command-runtime-run) · [runtime query](#command-runtime-query)

Each expandable entry is captured from the current parser's help interface. This is the full flag inventory; the sections above explain constraints beyond argparse help. Recheck installed `--help` when using another checkout. Entries follow parser order.

<a id="command-doctor"></a>

<details>
<summary><code>code-analysis doctor</code></summary>

```text
usage: code-analysis doctor [-h] [--json]

options:
  -h, --help  show this help message and exit
  --json      retained for explicit output identity
```

</details>

<a id="command-providers"></a>

<details>
<summary><code>code-analysis providers</code></summary>

```text
usage: code-analysis providers [-h] [--json]

options:
  -h, --help  show this help message and exit
  --json
```

</details>

<a id="command-scan"></a>

<details>
<summary><code>code-analysis scan</code></summary>

```text
usage: code-analysis scan [-h] [--config CONFIG] [--exclude-dir EXCLUDE_DIR]
                          [--json | --json-compact | --markdown]
                          [--output OUTPUT]
                          [--no-workspace | --workspace WORKSPACE] [--summary]
                          [--only-refactor-targets] [--only-local-imports]
                          [--all-imports] [--max-items MAX_ITEMS]
                          [--max-findings MAX_FINDINGS]
                          [--strictness {lenient,normal,strict}]
                          [--god-loc GOD_LOC] [--god-branches GOD_BRANCHES]
                          [--god-nesting GOD_NESTING] [--god-args GOD_ARGS]
                          [--god-score GOD_SCORE]
                          [--symbol-filter SYMBOL_FILTER] [--fail-on-findings]
                          [--no-color] [--quiet] [--verbose]
                          [--language {auto,python,javascript,ruby}]
                          [--javascript-module-mode {auto,esm,commonjs,script}]
                          [path]

positional arguments:
  path

options:
  -h, --help            show this help message and exit
  --config CONFIG
  --exclude-dir EXCLUDE_DIR
                        add an exact directory name to exclusions (repeatable;
                        no glob matching)
  --json                emit authoritative JSON
  --json-compact        emit compact JSON
  --markdown            emit concise Markdown
  --output OUTPUT, -o OUTPUT
  --no-workspace        disable project-local reports and cache
  --workspace WORKSPACE
                        use an explicit owned workspace
  --summary             function-analysis alias for Markdown summary
  --only-refactor-targets
  --only-local-imports
  --all-imports
  --max-items MAX_ITEMS
                        function-analysis display flag; accepted but ignored
  --max-findings MAX_FINDINGS
  --strictness {lenient,normal,strict}
  --god-loc GOD_LOC
  --god-branches GOD_BRANCHES
  --god-nesting GOD_NESTING
  --god-args GOD_ARGS
  --god-score GOD_SCORE
  --symbol-filter SYMBOL_FILTER
  --fail-on-findings
  --no-color
  --quiet
  --verbose
  --language {auto,python,javascript,ruby}
                        select auto, python, or javascript; explicit languages
                        may be repeated
  --javascript-module-mode {auto,esm,commonjs,script}
```

</details>

<a id="command-clones"></a>

<details>
<summary><code>code-analysis clones</code></summary>

```text
usage: code-analysis clones [-h] [--config CONFIG]
                            [--json | --json-compact | --markdown]
                            [--output OUTPUT] [--exclude-dir EXCLUDE_DIR]
                            [--no-workspace | --workspace WORKSPACE]
                            [--only-local-imports | --all-imports]
                            [--max-findings MAX_FINDINGS]
                            [--strictness {lenient,normal,strict}]
                            [--symbol-filter SYMBOL_FILTER] [--quiet]
                            [--language {auto,python,javascript,ruby}]
                            [--javascript-module-mode {auto,esm,commonjs,script}]
                            [path]

positional arguments:
  path

options:
  -h, --help            show this help message and exit
  --config CONFIG
  --json
  --json-compact
  --markdown
  --output OUTPUT, -o OUTPUT
  --exclude-dir EXCLUDE_DIR
                        add an exact directory name to exclusions (repeatable;
                        no glob matching)
  --no-workspace
  --workspace WORKSPACE
  --only-local-imports
  --all-imports
  --max-findings MAX_FINDINGS
  --strictness {lenient,normal,strict}
  --symbol-filter SYMBOL_FILTER
  --quiet
  --language {auto,python,javascript,ruby}
                        select auto, python, or javascript; explicit languages
                        may be repeated
  --javascript-module-mode {auto,esm,commonjs,script}
```

</details>

<a id="command-refactor"></a>

<details>
<summary><code>code-analysis refactor</code></summary>

```text
usage: code-analysis refactor [-h] [--config CONFIG]
                              [--json | --json-compact | --markdown]
                              [--output OUTPUT] [--exclude-dir EXCLUDE_DIR]
                              [--no-workspace | --workspace WORKSPACE]
                              [--only-local-imports | --all-imports]
                              [--max-findings MAX_FINDINGS]
                              [--strictness {lenient,normal,strict}]
                              [--symbol-filter SYMBOL_FILTER] [--quiet]
                              [--language {auto,python,javascript,ruby}]
                              [--javascript-module-mode {auto,esm,commonjs,script}]
                              [path]

positional arguments:
  path

options:
  -h, --help            show this help message and exit
  --config CONFIG
  --json
  --json-compact
  --markdown
  --output OUTPUT, -o OUTPUT
  --exclude-dir EXCLUDE_DIR
                        add an exact directory name to exclusions (repeatable;
                        no glob matching)
  --no-workspace
  --workspace WORKSPACE
  --only-local-imports
  --all-imports
  --max-findings MAX_FINDINGS
  --strictness {lenient,normal,strict}
  --symbol-filter SYMBOL_FILTER
  --quiet
  --language {auto,python,javascript,ruby}
                        select auto, python, or javascript; explicit languages
                        may be repeated
  --javascript-module-mode {auto,esm,commonjs,script}
```

</details>

<a id="command-contracts"></a>

<details>
<summary><code>code-analysis contracts</code></summary>

```text
usage: code-analysis contracts [-h] [--config CONFIG]
                               [--json | --json-compact | --markdown]
                               [--output OUTPUT]
                               [--no-workspace | --workspace WORKSPACE]
                               [--only-local-imports | --all-imports]
                               [--quiet]
                               [--language {auto,python,javascript,ruby}]
                               [path]

positional arguments:
  path

options:
  -h, --help            show this help message and exit
  --config CONFIG
  --json                emit authoritative JSON
  --json-compact        emit compact JSON
  --markdown            emit concise Markdown
  --output OUTPUT, -o OUTPUT
  --no-workspace        disable project-local reports and cache
  --workspace WORKSPACE
                        use an explicit owned workspace
  --only-local-imports
  --all-imports
  --quiet
  --language {auto,python,javascript,ruby}
                        select auto or python; other languages are
                        inapplicable to this focused command
```

</details>

<a id="command-dict-shapes"></a>

<details>
<summary><code>code-analysis dict-shapes</code></summary>

```text
usage: code-analysis dict-shapes [-h] [--config CONFIG]
                                 [--json | --json-compact | --markdown]
                                 [--output OUTPUT]
                                 [--no-workspace | --workspace WORKSPACE]
                                 [--only-local-imports | --all-imports]
                                 [--quiet]
                                 [--language {auto,python,javascript,ruby}]
                                 [path]

positional arguments:
  path

options:
  -h, --help            show this help message and exit
  --config CONFIG
  --json                emit authoritative JSON
  --json-compact        emit compact JSON
  --markdown            emit concise Markdown
  --output OUTPUT, -o OUTPUT
  --no-workspace        disable project-local reports and cache
  --workspace WORKSPACE
                        use an explicit owned workspace
  --only-local-imports
  --all-imports
  --quiet
  --language {auto,python,javascript,ruby}
                        select auto or python; other languages are
                        inapplicable to this focused command
```

</details>

<a id="command-summary"></a>

<details>
<summary><code>code-analysis summary</code></summary>

```text
usage: code-analysis summary [-h] [--max-findings MAX_FINDINGS] report

positional arguments:
  report

options:
  -h, --help            show this help message and exit
  --max-findings MAX_FINDINGS
```

</details>

<a id="command-index"></a>

<details>
<summary><code>code-analysis index</code></summary>

```text
usage: code-analysis index [-h] [--compare-profile COMPARE_PROFILE] -o OUTPUT
                           [--profile-report PROFILE_REPORT]
                           [--compare COMPARE]
                           report

positional arguments:
  report

options:
  -h, --help            show this help message and exit
  --compare-profile COMPARE_PROFILE
                        saved candidate Python profile
  -o OUTPUT, --output OUTPUT
  --profile-report PROFILE_REPORT
                        saved Python profile to index; no execution
  --compare COMPARE     candidate report; positional report is the baseline
```

</details>

<a id="command-tui"></a>

<details>
<summary><code>code-analysis tui</code></summary>

```text
usage: code-analysis tui [-h] [--workspace WORKSPACE]
                         [--report-sha256 REPORT_SHA256]
                         [--page-size PAGE_SIZE] [--source-root SOURCE_ROOT]
                         [--display-config DISPLAY_CONFIG]
                         [--collection-root COLLECTION_ROOT]
                         [--collection-python COLLECTION_PYTHON]
                         [--allow-collection]
                         [--viewer-command VIEWER_COMMAND] [--no-viewer]
                         [index]

positional arguments:
  index

options:
  -h, --help            show this help message and exit
  --workspace WORKSPACE
                        owned workspace for explicit saved-run selection
  --report-sha256 REPORT_SHA256
                        required with an existing index
  --page-size PAGE_SIZE
  --source-root SOURCE_ROOT
  --display-config DISPLAY_CONFIG
                        trusted local display JSON
  --collection-root COLLECTION_ROOT
                        confined terminal collection planning root
  --collection-python COLLECTION_PYTHON
                        additional trusted Python interpreter for collection (repeatable)
  --allow-collection    enable confirmed terminal collection jobs
  --viewer-command VIEWER_COMMAND
                        JSON argument array for an absolute viewer launcher
  --no-viewer           show artifact paths without launching a viewer
```

</details>

<a id="command-html"></a>

<details>
<summary><code>code-analysis html</code></summary>

```text
usage: code-analysis html [-h] [--compare COMPARE]
                          [--projection {signal-quality,commonality-admission,prevalent-features,proposal-admission,proposal-decisions,proposal-gates,refactor-shortlist,health,modules,symbols,symbol-graph,findings,subject-findings,hierarchy,complexity,dependencies,dependency-neighborhood,sccs,cycles,cycle-list,contracts,contract-boundaries,contract-boundary-graph,shape-variants,dictionary-flow,validation-divergence,ownership-alias-flow,structure-lifecycle,structure-lifecycle-graph,conversion-churn,conversion-graph,representation-candidates,review-packet,unresolved-coverage,clone-clusters,clone-graph,function-commonality,module-responsibilities,state-side-effects,state-side-effect-graph,extraction-candidates,module-combination-candidates,architecture-estimates,architecture-contract-suggestions,refactor-packet,test-impact,test-impact-graph,git-churn,change-coupling,change-coupling-graph,generation-changes,coverage-files,coverage-matrix,uncovered-proposal-subjects,duplicate-functions,clone-membership,behavior-differences,duplicate-ownership,duplicate-callers-tests,divergence-reasons,canonical-owner,javascript-refactor-packet,candidate-worklist,abstraction-cost,existing-owner-alternatives,candidate-behavioral-differences,cross-module-duplication,internal-contract-risks,producer-consumer-schema-drift,frontend-backend-interface-mismatches,candidate-qualification,ownership-contract-flow,candidate-architecture-changes}]
                          [--max-records MAX_RECORDS] -o OUTPUT
                          report

positional arguments:
  report

options:
  -h, --help            show this help message and exit
  --compare COMPARE
  --projection {signal-quality,commonality-admission,prevalent-features,proposal-admission,proposal-decisions,proposal-gates,refactor-shortlist,health,modules,symbols,symbol-graph,findings,subject-findings,hierarchy,complexity,dependencies,dependency-neighborhood,sccs,cycles,cycle-list,contracts,contract-boundaries,contract-boundary-graph,shape-variants,dictionary-flow,validation-divergence,ownership-alias-flow,structure-lifecycle,structure-lifecycle-graph,conversion-churn,conversion-graph,representation-candidates,review-packet,unresolved-coverage,clone-clusters,clone-graph,function-commonality,module-responsibilities,state-side-effects,state-side-effect-graph,extraction-candidates,module-combination-candidates,architecture-estimates,architecture-contract-suggestions,refactor-packet,test-impact,test-impact-graph,git-churn,change-coupling,change-coupling-graph,generation-changes,coverage-files,coverage-matrix,uncovered-proposal-subjects,duplicate-functions,clone-membership,behavior-differences,duplicate-ownership,duplicate-callers-tests,divergence-reasons,canonical-owner,javascript-refactor-packet,candidate-worklist,abstraction-cost,existing-owner-alternatives,candidate-behavioral-differences,cross-module-duplication,internal-contract-risks,producer-consumer-schema-drift,frontend-backend-interface-mismatches,candidate-qualification,ownership-contract-flow,candidate-architecture-changes}
  --max-records MAX_RECORDS
  -o OUTPUT, --output OUTPUT
```

</details>

<a id="command-web"></a>

<details>
<summary><code>code-analysis web</code></summary>

```text
usage: code-analysis web [-h] [--compare COMPARE]
                         [--profile-report PROFILE_REPORT]
                         [--compare-profile COMPARE_PROFILE]
                         [--source-root SOURCE_ROOT] [--workspace WORKSPACE]
                         [--collection-root COLLECTION_ROOT]
                         [--collection-python COLLECTION_PYTHON] [--host HOST]
                         [--allow-collection] [--port PORT]
                         [--display-config DISPLAY_CONFIG] [--open]
                         report

positional arguments:
  report

options:
  -h, --help            show this help message and exit
  --compare COMPARE
  --profile-report PROFILE_REPORT
  --compare-profile COMPARE_PROFILE
  --source-root SOURCE_ROOT
                        explicit root for hash-checked source previews
  --workspace WORKSPACE
                        owned workspace for saved-run selection
  --collection-root COLLECTION_ROOT
                        enable confined collection command planning beneath
                        this directory; no execution
  --collection-python COLLECTION_PYTHON
                        additional trusted Python interpreter for collection (repeatable)
  --host HOST
  --allow-collection    enable explicitly confirmed browser collection jobs
  --port PORT
  --display-config DISPLAY_CONFIG
                        trusted local display JSON
  --open                open the session URL in a browser
```

</details>

<a id="command-ui"></a>

<details>
<summary><code>code-analysis ui</code></summary>

```text
usage: code-analysis ui [-h] [--compare COMPARE]
                        [--profile-report PROFILE_REPORT]
                        [--compare-profile COMPARE_PROFILE]
                        [--source-root SOURCE_ROOT] [--workspace WORKSPACE]
                        [--collection-root COLLECTION_ROOT]
                        [--collection-python COLLECTION_PYTHON] [--host HOST]
                        [--allow-collection | --no-collection] [--port PORT]
                        [--foreground] [--display-config DISPLAY_CONFIG]
                        [--open | --no-open]
                        [report]

positional arguments:
  report                report JSON or generation directory

options:
  -h, --help            show this help message and exit
  --compare COMPARE     optional comparison report or generation
  --profile-report PROFILE_REPORT
  --compare-profile COMPARE_PROFILE
  --source-root SOURCE_ROOT
                        explicit root for hash-checked source previews
  --workspace WORKSPACE
                        owned workspace for saved-run selection
  --collection-root COLLECTION_ROOT
                        enable collection planning, including startup without
                        a saved report
  --collection-python COLLECTION_PYTHON
                        additional trusted Python interpreter for collection
                        (repeatable)
  --host HOST
  --allow-collection    enable explicitly confirmed browser collection jobs
                        (default)
  --no-collection       allow previews but disable browser collection jobs
  --port PORT
  --foreground          serve in this terminal for debugging
  --display-config DISPLAY_CONFIG
                        trusted local display JSON
  --open                open the session URL in a browser
  --no-open             serve the UI without opening a browser
```

</details>

<a id="command-ui-stop"></a>

<details>
<summary><code>code-analysis ui-stop</code></summary>

```text
usage: code-analysis ui-stop [-h] [--session SESSION] [--cancel-jobs]

options:
  -h, --help         show this help message and exit
  --session SESSION  session identity printed at launch; default is current
                     project
  --cancel-jobs      explicitly cancel active collection/profile jobs
```

</details>

<a id="command-candidates"></a>

<details>
<summary><code>code-analysis candidates</code></summary>

```text
usage: code-analysis candidates [-h] [--workspace WORKSPACE]
                                [--format {json,tsv,markdown}]
                                [--module-filter MODULE_FILTER]
                                [--exclude EXCLUDE]
                                [--kind {responsibility-extraction,module-combination,duplicate-functions,architecture-contract}]
                                [--cross-module-only] [--production-only]
                                [--state {observed,blocked,reviewable,reviewable-incomplete,ready-for-maintainer-review}]
                                [--language {python,javascript,ruby}]
                                [--min-shared-dimensions MIN_SHARED_DIMENSIONS]
                                [--prefer-existing-owner] [--top TOP]
                                [--decision {accepted,implemented,rejected-semantic-divergence,rejected-abstraction-cost,rejected-ownership-mismatch,rejected-behavior-change,deferred-missing-coverage,deferred-prerequisite,superseded,changed-since-decision,undecided}]
                                report

positional arguments:
  report

options:
  -h, --help            show this help message and exit
  --workspace WORKSPACE
  --format {json,tsv,markdown}
  --module-filter MODULE_FILTER
  --exclude EXCLUDE
  --kind {responsibility-extraction,module-combination,duplicate-functions,architecture-contract}
  --cross-module-only
  --production-only
  --state {observed,blocked,reviewable,reviewable-incomplete,ready-for-maintainer-review}
  --language {python,javascript,ruby}
  --min-shared-dimensions MIN_SHARED_DIMENSIONS
  --prefer-existing-owner
  --top TOP
  --decision {accepted,implemented,rejected-semantic-divergence,rejected-abstraction-cost,rejected-ownership-mismatch,rejected-behavior-change,deferred-missing-coverage,deferred-prerequisite,superseded,changed-since-decision,undecided}
```

</details>

<a id="command-decisions-list"></a>

<details>
<summary><code>code-analysis decisions list</code></summary>

```text
usage: code-analysis decisions list [-h] [--workspace WORKSPACE] [--json] path

positional arguments:
  path

options:
  -h, --help            show this help message and exit
  --workspace WORKSPACE
  --json
```

</details>

<a id="command-decisions-set"></a>

<details>
<summary><code>code-analysis decisions set</code></summary>

```text
usage: code-analysis decisions set [-h] --report REPORT --decision
                                   {accepted,implemented,rejected-semantic-divergence,rejected-abstraction-cost,rejected-ownership-mismatch,rejected-behavior-change,deferred-missing-coverage,deferred-prerequisite,superseded}
                                   --reason REASON [--proposal PROPOSAL]
                                   [--workspace WORKSPACE]
                                   path candidate_id

positional arguments:
  path
  candidate_id

options:
  -h, --help            show this help message and exit
  --report REPORT
  --decision {accepted,implemented,rejected-semantic-divergence,rejected-abstraction-cost,rejected-ownership-mismatch,rejected-behavior-change,deferred-missing-coverage,deferred-prerequisite,superseded}
  --reason REASON
  --proposal PROPOSAL
  --workspace WORKSPACE
```

</details>

<a id="command-decisions-export"></a>

<details>
<summary><code>code-analysis decisions export</code></summary>

```text
usage: code-analysis decisions export [-h] [--workspace WORKSPACE] path output

positional arguments:
  path
  output

options:
  -h, --help            show this help message and exit
  --workspace WORKSPACE
```

</details>

<a id="command-decisions-import"></a>

<details>
<summary><code>code-analysis decisions import</code></summary>

```text
usage: code-analysis decisions import [-h] [--workspace WORKSPACE]
                                      [--conflict {error,keep-local,use-incoming}]
                                      [--dry-run]
                                      path input

positional arguments:
  path
  input

options:
  -h, --help            show this help message and exit
  --workspace WORKSPACE
  --conflict {error,keep-local,use-incoming}
  --dry-run
```

</details>

<a id="command-query"></a>

<details>
<summary><code>code-analysis query</code></summary>

```text
usage: code-analysis query [-h]
                           (--contract CONTRACT | --shape SHAPE | --structure STRUCTURE | --finding FINDING | --clone CLONE | --duplicate DUPLICATE | --responsibility RESPONSIBILITY | --proposal PROPOSAL | --refactor-candidate REFACTOR_CANDIDATE | --symbol SYMBOL | --test TEST | --coverage COVERAGE | --git-change GIT_CHANGE | --generation GENERATION | --profile-frame PROFILE_FRAME | --runtime-edge RUNTIME_EDGE | --dependency-path SOURCE TARGET)
                           [--json | --json-compact | --markdown]
                           [--limit LIMIT] [--after AFTER] [--output OUTPUT]
                           report

positional arguments:
  report

options:
  -h, --help            show this help message and exit
  --contract CONTRACT
  --shape SHAPE
  --structure STRUCTURE
  --finding FINDING
  --clone CLONE
  --duplicate DUPLICATE
  --responsibility RESPONSIBILITY
  --proposal PROPOSAL
  --refactor-candidate REFACTOR_CANDIDATE
  --symbol SYMBOL
  --test TEST
  --coverage COVERAGE
  --git-change GIT_CHANGE
  --generation GENERATION
  --profile-frame PROFILE_FRAME
  --runtime-edge RUNTIME_EDGE
  --dependency-path SOURCE TARGET
  --json
  --json-compact
  --markdown
  --limit LIMIT
  --after AFTER
  --output OUTPUT, -o OUTPUT
```

</details>

<a id="command-diff"></a>

<details>
<summary><code>code-analysis diff</code></summary>

```text
usage: code-analysis diff [-h] [--format {json,json-compact,tsv,markdown}]
                          [--max-records MAX_RECORDS] [--include-unchanged]
                          [--output OUTPUT]
                          baseline candidate

positional arguments:
  baseline
  candidate

options:
  -h, --help            show this help message and exit
  --format {json,json-compact,tsv,markdown}
  --max-records MAX_RECORDS
  --include-unchanged   include unchanged records in the bounded comparison
                        inventory
  --output OUTPUT, -o OUTPUT
```

</details>

<a id="command-regress"></a>

<details>
<summary><code>code-analysis regress</code></summary>

```text
usage: code-analysis regress [-h] [--workspace WORKSPACE]
                             [--format {json,json-compact,tsv,markdown}]
                             [--top TOP] [--output OUTPUT]
                             baseline candidate

positional arguments:
  baseline
  candidate

options:
  -h, --help            show this help message and exit
  --workspace WORKSPACE
  --format {json,json-compact,tsv,markdown}
  --top TOP
  --output OUTPUT, -o OUTPUT
```

</details>

<a id="command-semantic-nominate"></a>

<details>
<summary><code>code-analysis semantic-nominate</code></summary>

```text
usage: code-analysis semantic-nominate [-h] --model-directory MODEL_DIRECTORY
                                       [--top TOP] [--json | --json-compact]
                                       [--output OUTPUT]
                                       report

positional arguments:
  report

options:
  -h, --help            show this help message and exit
  --model-directory MODEL_DIRECTORY
  --top TOP
  --json
  --json-compact
  --output OUTPUT, -o OUTPUT
```

</details>

<a id="command-export"></a>

<details>
<summary><code>code-analysis export</code></summary>

```text
usage: code-analysis export [-h] --projection
                            {signal-quality,commonality-admission,prevalent-features,proposal-admission,proposal-decisions,proposal-gates,refactor-shortlist,health,modules,symbols,symbol-graph,findings,subject-findings,hierarchy,complexity,dependencies,dependency-neighborhood,sccs,cycles,cycle-list,contracts,contract-boundaries,contract-boundary-graph,shape-variants,dictionary-flow,validation-divergence,ownership-alias-flow,structure-lifecycle,structure-lifecycle-graph,conversion-churn,conversion-graph,representation-candidates,review-packet,unresolved-coverage,clone-clusters,clone-graph,function-commonality,module-responsibilities,state-side-effects,state-side-effect-graph,extraction-candidates,module-combination-candidates,architecture-estimates,architecture-contract-suggestions,refactor-packet,test-impact,test-impact-graph,git-churn,change-coupling,change-coupling-graph,generation-changes,coverage-files,coverage-matrix,uncovered-proposal-subjects,duplicate-functions,clone-membership,behavior-differences,duplicate-ownership,duplicate-callers-tests,divergence-reasons,canonical-owner,javascript-refactor-packet,candidate-worklist,abstraction-cost,existing-owner-alternatives,candidate-behavioral-differences,cross-module-duplication,internal-contract-risks,producer-consumer-schema-drift,frontend-backend-interface-mismatches,candidate-qualification,ownership-contract-flow,candidate-architecture-changes}
                            [--format {html,json,json-compact,tsv,markdown,dot,mermaid,svg,png}]
                            [--max-records MAX_RECORDS]
                            [--max-nodes MAX_NODES] [--max-edges MAX_EDGES]
                            [--collapse {none,module,package}] [--select ID]
                            [--edge-kind {runtime,type_checking,optional,lazy}]
                            [--depth DEPTH] [--module-prefix MODULE]
                            [--category {complexity,orchestration,import-cycle,contract-conflict,shape-conflict,coercion-risk,validation-sprawl,ownership-risk,lifecycle-risk,conversion-churn,growth-risk,contract-candidate,duplication,commonality,state-coupling,side-effect-ownership,cohesion,extraction-candidate,module-combination,architecture-contract}]
                            [--severity {info,warning,error}] [--rule RULE_ID]
                            [--kind {responsibility-extraction,module-combination,duplicate-functions,architecture-contract}]
                            [--output OUTPUT]
                            [--language {python,javascript,ruby}]
                            report

positional arguments:
  report

options:
  -h, --help            show this help message and exit
  --projection {signal-quality,commonality-admission,prevalent-features,proposal-admission,proposal-decisions,proposal-gates,refactor-shortlist,health,modules,symbols,symbol-graph,findings,subject-findings,hierarchy,complexity,dependencies,dependency-neighborhood,sccs,cycles,cycle-list,contracts,contract-boundaries,contract-boundary-graph,shape-variants,dictionary-flow,validation-divergence,ownership-alias-flow,structure-lifecycle,structure-lifecycle-graph,conversion-churn,conversion-graph,representation-candidates,review-packet,unresolved-coverage,clone-clusters,clone-graph,function-commonality,module-responsibilities,state-side-effects,state-side-effect-graph,extraction-candidates,module-combination-candidates,architecture-estimates,architecture-contract-suggestions,refactor-packet,test-impact,test-impact-graph,git-churn,change-coupling,change-coupling-graph,generation-changes,coverage-files,coverage-matrix,uncovered-proposal-subjects,duplicate-functions,clone-membership,behavior-differences,duplicate-ownership,duplicate-callers-tests,divergence-reasons,canonical-owner,javascript-refactor-packet,candidate-worklist,abstraction-cost,existing-owner-alternatives,candidate-behavioral-differences,cross-module-duplication,internal-contract-risks,producer-consumer-schema-drift,frontend-backend-interface-mismatches,candidate-qualification,ownership-contract-flow,candidate-architecture-changes}
  --format {html,json,json-compact,tsv,markdown,dot,mermaid,svg,png}
  --max-records MAX_RECORDS
  --max-nodes MAX_NODES
  --max-edges MAX_EDGES
  --collapse {none,module,package}
  --select ID           select a module or contract ID for a focused
                        projection; may be repeated
  --edge-kind {runtime,type_checking,optional,lazy}
                        retain one dependency edge kind; may be repeated
  --depth DEPTH
  --module-prefix MODULE
                        retain findings for an exact dotted module or its
                        descendants; may be repeated
  --category {complexity,orchestration,import-cycle,contract-conflict,shape-conflict,coercion-risk,validation-sprawl,ownership-risk,lifecycle-risk,conversion-churn,growth-risk,contract-candidate,duplication,commonality,state-coupling,side-effect-ownership,cohesion,extraction-candidate,module-combination,architecture-contract}
                        retain one finding category; may be repeated
  --severity {info,warning,error}
                        retain one finding severity; may be repeated
  --rule RULE_ID        retain one finding rule ID; may be repeated
  --kind {responsibility-extraction,module-combination,duplicate-functions,architecture-contract}
                        retain one refactor candidate kind; may be repeated
                        for candidate-worklist
  --output OUTPUT, -o OUTPUT
  --language {python,javascript,ruby}
```

</details>

<a id="command-workspace-list"></a>

<details>
<summary><code>code-analysis workspace list</code></summary>

```text
usage: code-analysis workspace list [-h] [--workspace EXPLICIT_WORKSPACE]
                                    [--json]
                                    [path]

positional arguments:
  path

options:
  -h, --help            show this help message and exit
  --workspace EXPLICIT_WORKSPACE
  --json
```

</details>

<a id="command-workspace-pin"></a>

<details>
<summary><code>code-analysis workspace pin</code></summary>

```text
usage: code-analysis workspace pin [-h] [--workspace EXPLICIT_WORKSPACE]
                                   path generation_id

positional arguments:
  path
  generation_id

options:
  -h, --help            show this help message and exit
  --workspace EXPLICIT_WORKSPACE
```

</details>

<a id="command-workspace-unpin"></a>

<details>
<summary><code>code-analysis workspace unpin</code></summary>

```text
usage: code-analysis workspace unpin [-h] [--workspace EXPLICIT_WORKSPACE]
                                     path generation_id

positional arguments:
  path
  generation_id

options:
  -h, --help            show this help message and exit
  --workspace EXPLICIT_WORKSPACE
```

</details>

<a id="command-workspace-prune"></a>

<details>
<summary><code>code-analysis workspace prune</code></summary>

```text
usage: code-analysis workspace prune [-h] [--workspace EXPLICIT_WORKSPACE]
                                     [--keep-older KEEP_OLDER] [--dry-run]
                                     [path]

positional arguments:
  path

options:
  -h, --help            show this help message and exit
  --workspace EXPLICIT_WORKSPACE
  --keep-older KEEP_OLDER
  --dry-run
```

</details>

<a id="command-history"></a>

<details>
<summary><code>code-analysis history</code></summary>

```text
usage: code-analysis history [-h] [--config CONFIG] [--generations]
                             [--workspace EXPLICIT_WORKSPACE] [--markdown]
                             [--output OUTPUT]
                             [path]

positional arguments:
  path

options:
  -h, --help            show this help message and exit
  --config CONFIG
  --generations         compare retained analysis generations instead of
                        reading Git
  --workspace EXPLICIT_WORKSPACE
  --markdown
  --output OUTPUT, -o OUTPUT
```

</details>

<a id="command-coverage-import"></a>

<details>
<summary><code>code-analysis coverage-import</code></summary>

```text
usage: code-analysis coverage-import [-h] [--source-root SOURCE_ROOT]
                                     [--workspace WORKSPACE] [--replace]
                                     [--json-compact] [--output OUTPUT]
                                     report coverage_data [coverage_data ...]

positional arguments:
  report
  coverage_data

options:
  -h, --help            show this help message and exit
  --source-root SOURCE_ROOT
  --workspace WORKSPACE
  --replace
  --json-compact
  --output OUTPUT, -o OUTPUT
```

</details>

<a id="command-coverage-run"></a>

<details>
<summary><code>code-analysis coverage-run</code></summary>

```text
usage: code-analysis coverage-run [-h] [--allow-execution] [--cwd CWD]
                                  [--workspace WORKSPACE] [--timeout TIMEOUT]
                                  [--expected-status EXPECTED_STATUS]
                                  [--repeat REPEAT] [--env ENV] [--quiet]
                                  [--verbose] [--json-compact]
                                  [--output OUTPUT]
                                  report

positional arguments:
  report

options:
  -h, --help            show this help message and exit
  --allow-execution
  --cwd CWD
  --workspace WORKSPACE
  --timeout TIMEOUT
  --expected-status EXPECTED_STATUS
  --repeat REPEAT
  --env ENV
  --quiet
  --verbose
  --json-compact
  --output OUTPUT, -o OUTPUT
```

</details>

<a id="command-profile"></a>

<details>
<summary><code>code-analysis profile</code></summary>

```text
usage: code-analysis profile [-h] --source-report SOURCE_REPORT
                             [--allow-execution] [--cwd CWD]
                             [--workspace WORKSPACE] [--timeout TIMEOUT]
                             [--expected-status EXPECTED_STATUS]
                             [--repeat REPEAT] [--env ENV] [--quiet]
                             [--verbose] [--json-compact] [--output OUTPUT]

options:
  -h, --help            show this help message and exit
  --source-report SOURCE_REPORT
  --allow-execution
  --cwd CWD
  --workspace WORKSPACE
  --timeout TIMEOUT
  --expected-status EXPECTED_STATUS
  --repeat REPEAT
  --env ENV
  --quiet
  --verbose
  --json-compact
  --output OUTPUT, -o OUTPUT
```

</details>

<a id="command-profile-report"></a>

<details>
<summary><code>code-analysis profile-report</code></summary>

```text
usage: code-analysis profile-report [-h] [--source-report SOURCE_REPORT]
                                    [--source-root SOURCE_ROOT]
                                    [--static-report STATIC_REPORT]
                                    [--format {json,json-compact,markdown,tsv,dot,mermaid,overlay-dot,overlay-mermaid}]
                                    [--output OUTPUT]
                                    profile

positional arguments:
  profile

options:
  -h, --help            show this help message and exit
  --source-report SOURCE_REPORT
  --source-root SOURCE_ROOT
  --static-report STATIC_REPORT
  --format {json,json-compact,markdown,tsv,dot,mermaid,overlay-dot,overlay-mermaid}
  --output OUTPUT, -o OUTPUT
```

</details>

<a id="command-profile-diff"></a>

<details>
<summary><code>code-analysis profile-diff</code></summary>

```text
usage: code-analysis profile-diff [-h] [--markdown] [--diagnostic-only]
                                  [--output OUTPUT]
                                  baseline candidate

positional arguments:
  baseline
  candidate

options:
  -h, --help            show this help message and exit
  --markdown
  --diagnostic-only
  --output OUTPUT, -o OUTPUT
```

</details>

<a id="command-runtime-ingest"></a>

<details>
<summary><code>code-analysis runtime ingest</code></summary>

```text
usage: code-analysis runtime ingest [-h] --profile PROFILE
                                    [--metadata METADATA]
                                    [--source-report SOURCE_REPORT]
                                    [--repo REPO] [--config CONFIG]
                                    [--output OUTPUT] [--workspace WORKSPACE]
                                    [--json] [--max-records MAX_RECORDS]
                                    [--max-nodes MAX_NODES]
                                    [--max-edges MAX_EDGES]
                                    [--direction {TB,LR}] [--project-only]

options:
  -h, --help            show this help message and exit
  --profile PROFILE
  --metadata METADATA
  --source-report SOURCE_REPORT
  --repo REPO
  --config CONFIG
  --output OUTPUT
  --workspace WORKSPACE
                        retain import in an existing owned workspace
  --json
  --max-records MAX_RECORDS
  --max-nodes MAX_NODES
  --max-edges MAX_EDGES
  --direction {TB,LR}
  --project-only
```

</details>

<a id="command-runtime-render"></a>

<details>
<summary><code>code-analysis runtime render</code></summary>

```text
usage: code-analysis runtime render [-h] --output OUTPUT
                                    [--max-records MAX_RECORDS]
                                    [--max-nodes MAX_NODES]
                                    [--max-edges MAX_EDGES]
                                    [--direction {TB,LR}] [--project-only]
                                    report

positional arguments:
  report

options:
  -h, --help            show this help message and exit
  --output OUTPUT
  --max-records MAX_RECORDS
  --max-nodes MAX_NODES
  --max-edges MAX_EDGES
  --direction {TB,LR}
  --project-only
```

</details>

<a id="command-runtime-compare"></a>

<details>
<summary><code>code-analysis runtime compare</code></summary>

```text
usage: code-analysis runtime compare [-h] [--diagnostic-only] [--markdown]
                                     [--output OUTPUT]
                                     baseline candidate

positional arguments:
  baseline
  candidate

options:
  -h, --help         show this help message and exit
  --diagnostic-only
  --markdown
  --output OUTPUT
```

</details>

<a id="command-runtime-run"></a>

<details>
<summary><code>code-analysis runtime run</code></summary>

```text
usage: code-analysis runtime run [-h] --spec SPEC
                                 [--source-report SOURCE_REPORT]
                                 [--allow-execution]

options:
  -h, --help            show this help message and exit
  --spec SPEC
  --source-report SOURCE_REPORT
  --allow-execution
```

</details>

<a id="command-runtime-query"></a>

<details>
<summary><code>code-analysis runtime query</code></summary>

```text
usage: code-analysis runtime query [-h] --section
                                   {hotspots,calls,variance,resources,categories,correlations,process,lifecycle,steps}
                                   [--max-records MAX_RECORDS] [--frame FRAME]
                                   [--format {json,tsv,markdown}]
                                   report

positional arguments:
  report

options:
  -h, --help            show this help message and exit
  --section {hotspots,calls,variance,resources,categories,correlations,process,lifecycle,steps}
  --max-records MAX_RECORDS
  --frame FRAME         exact frame identity for hotspots, calls, or variance
  --format {json,tsv,markdown}
```

</details>
