# cProfile runtime-analysis specification

Status: implemented runtime contract with explicit portable limitations. The Phase 4 `profile`, `profile-report`, `profile-diff`, and `runtime` commands are unreleased façades over shared bounded services, not compatibility generations.

This protocol specification is distinct from release qualification. See [current scope and roadmap](roadmap.md), [beta limitations](beta.md) and the [provider contract](provider-contract.md) for supported behavior and unresolved work. Private workload examples and qualification receipts are not public protocol requirements.

## Purpose and boundaries

Code Analysis Suite must ingest existing Python cProfile artifacts and, only when explicitly authorized, run declared workloads under cProfile without operator interaction. Runtime evidence augments static module, symbol, dependency, ownership, complexity, clone, contract, and refactor-candidate evidence. It never authorizes optimization, refactoring, or source modification.

Static scans never invoke runtime collection. Offline ingestion never executes the subject application. Managed execution treats the target as untrusted code, requires explicit authorization, uses argument arrays without `shell=True`, and stores its resolved redacted run specification before starting the subject.

## Offline profile ingestion

Offline ingestion accepts one or more existing `.prof` files and produces normalized runtime artifacts without executing target code.

- Read profile data through `pstats.Stats`; never parse human-readable pstats text.
- Preserve each source profile unchanged and record its SHA-256.
- Normalize source paths against the configured repository root and correlate Python frames with stable modules and symbols.
- Retain unresolved, interpreter, built-in, generated, dependency, native-extension, standard-library, third-party, and project-owned frames rather than silently dropping them.
- Record total and primitive calls, self and cumulative time, per-call values, recursion, source locations, and caller/callee edges.
- Support project-only, dependency-only, package, module, file, symbol, and call-depth filters.
- Retain complete oversized graph/component identity and membership. Presentation bounds apply only to derived views and disclose omitted node and edge counts.
- Missing, empty, unreadable, malformed, truncated, or source-incompatible artifacts are failed or partial evidence, never empty success.

## Declarative managed execution

The Code Analysis-owned runner uses a strict declarative run specification containing:

- repository root and working directory;
- target command as an argv array;
- environment additions and removals with secret-value redaction;
- profile output path and report directory;
- optional URL or argv readiness probe with interval and timeout;
- ordered workload argv arrays with individual and overall timeouts;
- expected workload exit codes and optional required output artifacts;
- graceful shutdown signal and timeout;
- forced termination signal and timeout, used only after graceful shutdown fails;
- optional cleanup argv arrays;
- sensitive path and argument redaction rules;
- workload identity and external artifact fingerprints.

The initial execution modes are:

1. `python-module`: execute a Python module or script inside a Code Analysis cProfile bootstrap.
2. `application-managed`: start an application with its own process-lifetime cProfile switch and verify the emitted PID/profile metadata.

The runner must:

- profile the actual subject process rather than only a request-driving harness;
- record harness PID, subject PID, process start identity, status, signals, and local timestamps;
- fail closed when the expected profile PID differs from the subject PID unless declared child-process profiling proves the relationship;
- wait for graceful subject termination and completed cProfile serialization before report assembly;
- treat startup, readiness, workload, shutdown, serialization, required-artifact, and PID failures as distinct evidence states;
- preserve workload stdout/stderr separately from Code Analysis logs and store only bounded hashes/counts in portable evidence;
- redact secrets, tokens, private model paths, sensitive arguments, and environment values;
- run non-interactively and never require UI or operator responses;
- terminate only owned processes, escalating to forced termination only after the configured graceful timeout;
- describe thread and subprocess collection limits accurately;
- never characterize profiled timing as a throughput benchmark.

Monkeypatching, `sitecustomize`, import interception, and hidden profiler injection are prohibited.

## Runtime identity and comparison

Every runtime report contains a strict identity document with:

- Code Analysis schema and tool version;
- repository commit, dirty state, and source fingerprint;
- Python implementation/version and platform/architecture;
- relevant dependency versions;
- redacted subject-command fingerprint;
- workload identity and ordered workload-step fingerprints;
- declared external artifact fingerprints;
- profiler type and configuration;
- collection scope and known thread/subprocess limitations;
- subject PID identity and start time.

Collection identity excludes temporary output paths, labels, timestamps, and other non-semantic values. Comparison fails closed when source, workload, external artifacts, profiler configuration, PID proof, or collection scope is incomplete or incompatible. An explicit override may produce a diagnostic comparison, but that comparison remains ineligible for a performance claim.

## Static/runtime correlation

Runtime facts remain distinct from static facts. Required correlations include:

- frame to static module and symbol;
- caller/callee edge to a static call edge when available;
- hot symbol to complexity, clone, duplicate-function, responsibility, ownership-candidate, contract, and cross-subsystem evidence;
- observed module to configured dependency-direction rules.

Unobserved static symbols remain visible and are labeled `unobserved`, never `dead`. Runtime ranking keeps self time, cumulative time, call amplification, per-call cost, static maintainability evidence, boundary/ownership findings, and unexecuted/incomplete runtime coverage as separate dimensions. High cumulative time alone is not a bottleneck claim because it may enclose dependency work, waits, generators, futures, locks, selectors, or asynchronous execution.

## Runtime classification

The report distinguishes:

- project Python;
- third-party Python;
- standard-library Python;
- native or built-in execution unattributed by cProfile;
- synchronization and scalar-materialization boundaries;
- locks, queues, selectors, futures, joins, and event-loop waits;
- serialization and persistence;
- network or stream publication;
- configured domain categories such as tokenization;
- unknown or unresolved frames.

Domain categories come from explicit sorted project rules. Generic Code Analysis code must not hard-code MLX, model, web, database, or framework names. Classification precedence is deterministic and reported. Overlap either retains all matches with one declared primary category or applies one documented winning rule.

## Reports and diagrams

One runtime-analysis report directory contains:

```text
runtime-summary.json
runtime-summary.md
runtime-functions.tsv
runtime-callers.tsv
runtime-callees.tsv
runtime-categories.json
runtime-identity.json
runtime-diagnostics.json
runtime-callgraph.dot
runtime-callgraph.svg
runtime-callgraph.png
runtime-hotspots.svg
runtime-static-overlay.svg
manifest.json
```

The manifest records relative paths, sizes, SHA-256 values, generation status, and limitations. JSON remains authoritative.

Graphs default to top-to-bottom and permit explicit left-to-right layout. Each graph includes a legend for ownership/category color, node border, edge width, and timing labels. Nodes show symbol, module, calls, self time, and cumulative time. Edge width uses a documented call-count or edge-time measure. Project, standard-library, dependency, native/built-in, and unresolved frames are visually distinct; static boundary violations are independent of heat. Reports never sum cumulative node times or imply they partition wall time. Filters, retained and excluded counts, and truncation are explicit. Overview and project-only graphs are bounded views over a complete machine-readable graph. A flame-style view derived from aggregate caller data is labeled an aggregate call-edge visualization, never a sampled flame graph.

## Completeness and diagnostics

Runtime analysis is partial or failed when any of these apply:

- a profile is missing, unreadable, empty, malformed, truncated, or PID-mismatched;
- the workload fails or remains incomplete;
- graceful shutdown or profile serialization does not complete;
- repository, source, command, workload, external-artifact, or collection identity is incomplete;
- source-frame normalization exceeds configured unresolved limits;
- required thread or subprocess coverage is unavailable;
- report or graph generation omits evidence due to an analytical bound;
- required project-symbol correlation cannot be resolved.

Presentation truncation produces usable partial views but never discards underlying component identities or membership. Wildcard allowlists cannot hide unresolved frames, missing identity, boundary problems, or incomplete coverage.

## Configuration

The strict runtime-analysis configuration owns:

- sorted runtime category rules and deterministic precedence;
- project and dependency path roots;
- normalization and redaction rules;
- default graph direction and bounded-view limits;
- required identity fields;
- allowed readiness-probe types;
- maximum startup, workload, run, graceful-shutdown, forced-shutdown, and cleanup durations;
- required subprocess and thread coverage policy.

Every semantic setting participates in collection identity. Coercive booleans, invalid durations, unsafe paths, unknown signal names, shell strings, and contradictory coverage requirements are rejected.

## CLI contract

The eventual command surface must provide operations equivalent to:

```text
code-analysis runtime ingest --profile PROFILE --repo ROOT --out REPORT_DIR
code-analysis runtime run --spec RUN_SPEC --out REPORT_DIR
code-analysis runtime compare --baseline REPORT --candidate REPORT --out REPORT_DIR
code-analysis runtime render --report REPORT --direction top-to-bottom
```

Exact names may be reconciled with the existing `profile`, `profile-report`, and `profile-diff` commands during implementation. Offline ingestion and managed execution remain distinct operations. Every command supports deterministic machine diagnostics and existing POSIX errno meanings.

## Tests

Synthetic deterministic coverage must include:

- single-process profiles, recursion, primitive-call counts, same-named functions in different modules, built-in/native frames, path classification, caller/callee normalization, static-symbol correlation, and unresolved source frames;
- oversized components retained completely with bounded rendered views;
- startup failure, readiness timeout, workload failure/timeout, graceful shutdown, forced shutdown with missing/incomplete profile, PID mismatch, and malformed input;
- secret/private-path redaction and compatible/incompatible comparisons;
- top-to-bottom and left-to-right layout plus stable JSON, TSV, Markdown, DOT, SVG, PNG, and manifest ordering;
- a long-lived HTTP-service fixture in which a separate harness drives requests while the server process owns cProfile.

Tests must not depend on MLXForge, MLX, model artifacts, GPUs, network access, or wall-clock performance.

## Read-only real-project qualification

After generic tests and package gates pass, ingest an explicitly supplied existing profile against its source identity without rerunning the workload. Verify permitted source hashes before and after. Retain project, dependency, standard-library, wait, native and unresolved frames. Workload counts must come from explicitly supplied fingerprinted artifacts, never inference from cProfile.

Produce top-down graphs and static/runtime correlations with complete limitations, identifying call-ownership evidence rather than a throughput baseline. Keep private paths, captured inputs and project-specific expected counts in local qualification records. Offline-ingestion permission does not authorize executing, modifying, testing or profiling the consumer project.

## Acceptance checkpoint

- Offline ingestion and managed execution are separate tested commands.
- The actual subject PID is profiled and proven.
- Long-lived services can be driven and stopped non-interactively.
- Graceful shutdown waits for profile serialization; forced termination yields failed evidence.
- Identity and comparison fail closed.
- Static/runtime correlation retains unresolved and unobserved evidence.
- Top-down graphs contain complete legends and disclosed filtering.
- Reports make no cumulative-time arithmetic, wait-time misclassification, or profiled-throughput claim.
- Generic core code contains no consumer-specific behavior.
- Tests, quality gates, package installation and independently authorized read-only qualification pass.
- Review artifacts and publication scope separately; never modify consumer source during qualification.
