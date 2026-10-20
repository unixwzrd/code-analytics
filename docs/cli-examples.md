# CLI examples and artifacts

Use this guide for workflows; use the [CLI reference](cli.md) for exact arguments, defaults and exit behavior, and the [UI guide](ui-guide.md) for screen controls. Commands assume an installed `code-analysis`, the intended Python environment, and a project with a `src/` directory. Replace uppercase identities/paths with values from your own reports. `jq` and Graphviz are optional tools used by particular examples, not automatically installed dependencies.

Examples are independent unless they explicitly reuse an earlier file. Choose fresh destinations when repeating a no-overwrite command. **ENODATA can accompany useful partial artifacts.** Inspect completeness and the command status; do not treat these examples as promises of an exit-zero report.

## Check the environment

```bash
code-analysis doctor > doctor.json
code-analysis providers --json > providers.json
```

Produces `doctor.json` with actual local interpreter/tool provenance and `providers.json` with provider availability. These are not analysis results. Doctor output may contain host paths; keep it local if publishing portable analytical evidence. Missing optional tools remain unavailable.

## Analyze a project and open its saved result

Run from the project root:

```bash
code-analysis scan . --quiet
code-analysis workspace list . --json > generations.json
code-analysis ui
```

The scan creates/reuses an owned `.code-analysis/` and publishes an analysis generation. `ui` opens the saved result, not a second scan. It returns your prompt after the background server is ready and opens the browser on macOS; `ui --no-open` prints the session URL without opening it. Use **Stop Code Analysis** or `code-analysis ui-stop` to stop this project's single matching session. If several match, use the printed `--session ID`; active jobs require `--cancel-jobs`. Closing a browser tab leaves the server running. For terminal-controlled operation and Ctrl+C shutdown, use `code-analysis ui --foreground --no-open`.

| Artifact | Purpose |
| --- | --- |
| `.code-analysis/workspace.json` | Local ownership marker |
| `.code-analysis/.gitignore` | Keeps workspace contents untracked |
| `.code-analysis/generations/analysis/GENERATION_ID/report.json` | Authoritative compact analysis JSON |
| Same directory: `report.md` | Concise human summary |
| Same directory: `run.json`, `manifest.json` | Local execution/storage provenance and artifact identities |
| `.code-analysis/generations/analysis/latest` | Owned relative alias to the latest generation |
| `generations.json` | Explicit inventory export from `workspace list` |

The UI creates no replacement analysis report. Retention may later prune unpinned generations; pin a generation explicitly if it is a baseline you need to preserve.

## Source-only analysis without test discovery

Create `source-only.toml` with:

```toml
[tool.code-analysis]
strictness = "normal"
portable = true
test-roots = []
```

Then run:

```bash
mkdir -p analysis-artifacts
code-analysis refactor src --config source-only.toml --no-workspace \
  --json-compact --output analysis-artifacts/source-report.json
code-analysis summary analysis-artifacts/source-report.json \
  > analysis-artifacts/source-summary.md
```

Produces one authoritative `source-report.json` and a separate concise summary. `--no-workspace` prevents project workspace/cache writes; the explicit output directory still receives these files. Omit that flag if you want retained generations as well. Add `--language python` for Python-only source. Selecting `src` alone would not disable configured test discovery. Missing test evidence remains unavailable, not a coverage claim.

## Derive useful views without rescanning

These commands reuse `analysis-artifacts/source-report.json`:

```bash
code-analysis candidates analysis-artifacts/source-report.json --format markdown \
  --top 25 > analysis-artifacts/candidates.md
code-analysis export analysis-artifacts/source-report.json \
  --projection subject-findings --format tsv \
  --output analysis-artifacts/subject-findings.tsv
code-analysis export analysis-artifacts/source-report.json \
  --projection dependencies --format dot --max-nodes 60 --max-edges 120 \
  --output analysis-artifacts/dependencies.dot
code-analysis export analysis-artifacts/source-report.json \
  --projection dependencies --format svg --max-nodes 60 --max-edges 120 \
  --output analysis-artifacts/dependencies.svg
code-analysis html analysis-artifacts/source-report.json \
  --projection modules --projection dependencies --max-records 100 \
  --output analysis-artifacts/overview.html
```

| File | What it answers |
| --- | --- |
| `candidates.md` | Which bounded candidates warrant human review? |
| `subject-findings.tsv` | Which subjects have findings, by category/severity/rule? |
| `dependencies.dot` | What directed module relationships were retained? |
| `dependencies.svg` | The same bounded graph rendered by Graphviz |
| `overview.html` | A self-contained offline document with selected views |

The report's subjects—not filenames guessed from source—supply identities. Optional Graphviz failure must be handled explicitly. A graph exceeding the selected bounds is usable partial presentation, not a complete graph. HTML opens directly from disk; it does not launch a server or fetch source. Other projection names and format restrictions are in the [catalog](report-and-diagram-catalog.md).

## Compare two saved analysis runs

Keep a copy of the earlier JSON as `baseline.json`; generate `candidate.json` using the same target and analytical configuration after your change. Do not overwrite the baseline.

```bash
code-analysis diff baseline.json candidate.json --format json-compact \
  --output changes.json
code-analysis diff baseline.json candidate.json --format markdown \
  --output changes.md
code-analysis diff baseline.json candidate.json --include-unchanged \
  --format tsv --output inventory-delta.tsv
```

Produces an authoritative comparison (`changes.json`), a readable changes-only view (`changes.md`) and a bounded full inventory (`inventory-delta.tsv`). Global counts remain distinct from retained rows. Identical reports produce zero records with `identical=true`, but source partiality remains. Incompatible target/configuration identities return `EINVAL`; matching filenames or dates do not make reports compatible. Comparison does not rescan either checkout.

## Prepare an indexed terminal view

```bash
mkdir -p analysis-artifacts
code-analysis index analysis-artifacts/source-report.json \
  --output analysis-artifacts/browse-index > analysis-artifacts/index-receipt.json
code-analysis tui analysis-artifacts/browse-index \
  --report-sha256 "$(jq -r .report_sha256 analysis-artifacts/index-receipt.json)"
```

Produces `browse-index/index.sqlite3`, `browse-index/manifest.json` and the explicitly redirected `index-receipt.json`. The destination must be new; its parent must exist. The receipt binds the cache to the canonical report identity. The terminal requires the optional Textual dependency and a TTY. Browsing does not rescan source or recreate reports. Exports are generated only when you request them; see [terminal controls](ui-guide.md#indexed-terminal-browsing).

## Ingest an existing profile offline

To retain an import alongside existing project runs, use an already owned workspace:

```bash
code-analysis runtime ingest --profile capture.pstats \
  --workspace .code-analysis > imported-profile.json
```

This creates a dated `.code-analysis/generations/profile/GENERATION/` containing `profile.json`, available `portable-HASH.prof` artifacts and its local generation manifest. It also writes the explicitly redirected JSON copy. Existing retention/pins apply. The raw input is unchanged, target code is not executed, and the importing process identity does not fill gaps in the original capture. Partial evidence returns ENODATA even when these artifacts are successfully retained. Do not combine this mode with `--output`.

`capture.pstats` must already exist. This example does **not** run the profiled application:

```bash
code-analysis runtime ingest --profile capture.pstats \
  --output analysis-artifacts/runtime-import --direction TB
code-analysis runtime query analysis-artifacts/runtime-import \
  --section hotspots --format tsv --max-records 100 \
  > analysis-artifacts/hotspots.tsv
```

The runtime destination must be new. It contains:

| Artifact | Role |
| --- | --- |
| `runtime-summary.json` | Authoritative normalized profile evidence |
| `runtime-summary.md` | Human-readable profile summary and limitations |
| `runtime-functions.tsv`, `runtime-callers.tsv`, `runtime-callees.tsv` | Frame and caller/callee tables |
| `runtime-identity.json`, `runtime-categories.json`, `runtime-diagnostics.json` | Identity, classification and diagnostic views |
| `runtime-callgraph.dot` | Directed graph source |
| `runtime-callgraph.svg`, `runtime-callgraph.png`, `runtime-static-overlay.svg` | Optional rendered graphs; inspect manifest availability |
| `runtime-hotspots.svg` | Hotspot visualization |
| `manifest.json` | Artifact hashes, availability, omissions and completeness |

`hotspots.tsv` is the separate query output outside the bundle. Offline bundle publication does not copy raw pstats into it. Bare input usually remains unbound/partial and returns `ENODATA`. Stdlib integer callers retain exact counts and null unavailable timings. No graph constitutes a throughput baseline or whole-program coverage proof.

## Bind an external profiler declaration

If your collector actually recorded stdlib `profile.Profile`, compute the captured file hash:

```bash
python -c 'import hashlib,pathlib; print(hashlib.sha256(pathlib.Path("capture.pstats").read_bytes()).hexdigest())'
```

Place the resulting 64-character lowercase digest in `metadata.json`:

```json
{
  "schema_name": "code-analysis-runtime-metadata",
  "schema_version": "1.0.0",
  "collection": {},
  "producer_bindings": [
    {"profile_sha256": "REPLACE_WITH_CAPTURE_SHA256", "producer": "profile"}
  ]
}
```

The placeholder is intentionally invalid until replaced. Use `cprofile` only when that is what the collector recorded. For multiple inputs, sort bindings by unique digest. Do not infer producer from a filename or caller representation.

```bash
code-analysis runtime ingest --profile capture.pstats --metadata metadata.json \
  --output analysis-artifacts/runtime-bound
```

Produces the same bundle layout in a different fresh directory. Producer identification does not fill missing source/process/workload facts. Wrong hashes return `EBADMSG`; incomplete evidence still returns `ENODATA`. Never fabricate facts for an old capture to obtain exit zero.

## Render and compare retained runtime evidence

```bash
code-analysis runtime render analysis-artifacts/runtime-bound \
  --output analysis-artifacts/runtime-small --max-nodes 40 --max-edges 80
code-analysis runtime compare BASELINE_RUNTIME_JSON CANDIDATE_RUNTIME_JSON \
  --diagnostic-only --markdown --output runtime-comparison.md
```

`runtime-small/` is a newly rendered bundle from saved JSON, with bounded graph counts. `runtime-comparison.md` records comparison decisions and deltas; diagnostic-only mode is explicitly claim-ineligible. Neither command executes or rescans an application. Different producer/scope/workload identities and unavailable timings remain visible.

## Preserve status in automation

This shell pattern works with `set -e` because the command is inside an `if`. Use the same environment's Python to resolve errno symbols:

```bash
enodata=$(python -c 'import errno; print(errno.ENODATA)')
if code-analysis scan . --no-workspace --json-compact \
    --output report.json 2> diagnostics.log; then
  status=0
else
  status=$?
fi

case "$status" in
  0) printf '%s\n' 'Operation completed under the selected policy' ;;
  "$enodata") printf '%s\n' 'Partial evidence: inspect report and diagnostics' >&2 ;;
  *) printf 'Analysis failed with host status %s\n' "$status" >&2 ;;
esac
exit "$status"
```

Produces `report.json` when publication succeeds and `diagnostics.log` from stderr. A failed invocation may leave no report; don't read a stale pre-existing file as if it came from this run. Use a fresh output location per job. The script preserves the actual status rather than converting incomplete evidence into success. JSON output and diagnostics remain separate.

## Configure an evidenced contract boundary

For an exact declared contract and consumer site, add a rule to your project TOML:

```toml
[[tool.code-analysis.contract-boundaries]]
contract-pattern = "package.contracts.*"
site-pattern = "package.api:*"
boundary-class = "http-request"
```

Patterns match portable logical identities, not host paths. Unmatched or conflicting cross-module uses remain unclassified/partial. For dictionary serialization sites, `contract-pattern` matches the owned dictionary identity (for example `package.store:save:payload`) and `site-pattern` can match `json.dump`. Only a uniquely configured `persisted-document` class establishes a persistence-boundary link; serialization syntax alone does not. These rules alter a subsequent analysis report and its configuration identity, not any source file or historical report.

## Execution is a separate, explicit step

Coverage/profile collection requires a workload and `--allow-execution`; viewing existing evidence does not. Consult the [runtime reference](cli.md#runtime-evidence) and [run specification](cprofile-runtime-analysis-specification.md) before running an application. The examples above intentionally do not launch tests, profilers or consumer workloads. Do not use a model benchmark merely to test report rendering.
