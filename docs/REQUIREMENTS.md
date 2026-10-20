# Code Analysis Suite Requirements and Implementation Specification

This public specification includes both implemented obligations and planned capabilities. It is not a claim that every requirement is complete in the beta; use [beta scope and limitations](beta.md) and the [roadmap](roadmap.md) for the delivered boundary. Environment-specific acceptance records are archived privately.

## 1. Purpose

Code Analysis Suite is a unified, multi-language reverse-engineering and refactoring analysis tool for developers and AI agents. Its implemented foundation analyzes Python, JavaScript and Ruby through the same report, query, comparison, and projection pipeline, with explicitly different capabilities. It combines native static analysis with established external tools, normalizes their evidence, and identifies architecture, coupling, data-contract, duplication, complexity, ownership, and maintainability risks.

The suite must help answer questions such as:

- Which modules or functions combine unrelated responsibilities?
- Which persisted, API, or cross-subsystem boundaries pass unconstrained dictionaries or `Any`?
- Where are dictionary schemas and validation rules duplicated or inconsistent?
- Which functions or blocks are genuinely similar after names and formatting are normalized?
- Which modules form import or symbol-level dependency cycles?
- Which globals, locks, caches, and registries create hidden process-wide coupling?
- Which extraction boundaries reduce coupling without changing behavior?
- Which tests and runtime paths protect a proposed refactor?
- Did a refactor improve the architecture without changing required behavior?

Line count, complexity, fan-in, fan-out, duplication, and cycle counts are indicators, not automatic proof that code should be split or merged. Every recommendation must cite at least one additional ownership, data-flow, dependency, duplication, state, or validation reason.

## 2. Product Principles

1. **Evidence before recommendation.** Every finding names exact files, symbols, spans, and the observations supporting it.
2. **Detection is not approval.** The tool proposes candidates; developers decide whether a refactor is appropriate.
3. **Stable machine output first.** JSON schemas are authoritative. CLI text, Markdown, HTML, diagrams, and TUI views render the same models.
4. **Fail closed on incomplete analysis.** Parse failures, unresolved project roots, tool failures, truncated output, and stale caches are represented explicitly and must not produce clean claims.
5. **Confidence is explicit.** Direct syntax evidence, inferred data flow, heuristic similarity, and runtime observations have distinct confidence levels.
6. **No source mutation by default.** Initial releases are read-only. Future codemods require separate commands, explicit approval, clean worktrees, and reversible patches.
7. **Repository conventions are authoritative.** Respect `pyproject.toml`, source roots, namespace packages, exclusions, generated files, and local architecture policy.
8. **One analysis engine.** CLI, TUI, CI, and AI-agent modes use the same service layer and report schemas.
9. **Composable providers.** Native analyzers and external tools emit normalized findings without making the core dependent on one vendor or parser.
10. **Deterministic reports.** Stable ordering, logical paths, content hashes, configuration identity, and tool provenance are required.

## 3. Users and Operating Modes

### 3.1 Developers

- Interactive CLI summaries and focused queries.
- TUI exploration of modules, symbols, contracts, cycles, clones, and proposed extraction boundaries.
- Baseline-versus-candidate architecture comparison.
- Exportable Markdown, JSON, Graphviz, and Mermaid reports.

### 3.2 AI agents

- Non-interactive commands with stable JSON output.
- Exact evidence references and confidence values.
- Bounded queries that avoid loading an entire repository report into context.
- Refactor-plan packets containing scope, risks, affected tests, and acceptance criteria.
- Explicit partial-analysis and unsupported-language states.

### 3.3 CI and maintainers

- Architecture contracts and regression budgets.
- Baseline comparison with configurable failure thresholds.
- SARIF or equivalent structured findings where appropriate.
- No TTY, color, animation, or prompts in automation mode.

## 4. Environment and Packaging

### 4.1 Development environment

The development and qualification environment is the Conda environment `venvutil`:

```text
/path/to/venvutil/bin/python
```

Development commands must verify and record:

- `sys.executable`
- Python version
- active Conda environment
- resolved external-tool paths
- external-tool versions
- project configuration hash

The current `function-analysis` script uses `#!/usr/bin/env python3`; PATH sanitization can resolve that to Apple/Xcode Python 3.9. The packaged suite must expose an installed console script and require Python 3.12 or newer. Development instructions may use `conda run -n venvutil` or the absolute interpreter path.

### 4.2 Project packaging

Create a `pyproject.toml` with an installable `src/code_analysis/` package. The core dependency set must stay narrow.

Core candidates:

- Pydantic 2
- Typer
- Rich
- NetworkX
- LibCST
- platformdirs
- pathspec

Optional extras should isolate integrations:

- `tui`: Textual
- `metrics`: Radon and Lizard
- `graphs`: Pydeps, Pylint/Pyreverse, Graphviz
- `dead-code`: Vulture
- `architecture`: Import Linter/Grimp
- `rules`: Semgrep
- `reports`: Jinja2, Markdown-It, Pygments
- `runtime`: coverage and psutil
- `javascript`: `tree-sitter==0.25.2` and `tree-sitter-javascript==0.25.0`
- `test`: pytest, pytest-cov, pytest-json-report, Hypothesis

Do not use the top-level environment snapshot as package requirements. MLX, Transformers, audio, vision, HTTP-server, cryptography, and data-science packages are not core dependencies of a Python architecture analyzer.

## 5. Proposed Package Architecture

```text
src/code_analysis/
  cli.py
  config.py
  errors.py
  project.py
  models/
    common.py
    data_structures.py
    evidence.py
    project.py
    symbols.py
    findings.py
    contracts.py
    refactor.py
    reports.py
  providers/
    base.py
    registry.py
    native_ast.py
    libcst.py
    ruff_graph.py
    pyreverse.py
    pydeps.py
    radon.py
    lizard.py
    vulture.py
    symilar.py
    import_linter.py
    basedpyright.py
    coverage.py
    cprofile.py
  analysis/
    symbols.py
    imports.py
    calls.py
    complexity.py
    contracts.py
    data_structures.py
    dict_shapes.py
    validation.py
    clones.py
    coupling.py
    state.py
    change_coupling.py
    test_impact.py
    extraction.py
  graph/
    model.py
    algorithms.py
    diff.py
  reports/
    json.py
    markdown.py
    html.py
    sarif.py
    diagrams.py
  runtime/
    tracing.py
    coverage_contexts.py
    profiling.py
  tui/
    app.py
    screens/
    widgets/
  web/
    app.py
    api.py
    static/
    views/
```

Avoid generic module names that can be confused with standard-library modules when exposed at the top level. Provider and analyzer modules should have explicit names such as `native_ast.py` rather than a top-level `ast.py`.

## 6. Authoritative Data Models

All reports use versioned Pydantic models with `extra="forbid"`. Strict models must reject bool-as-int and malformed source spans.

### 6.1 Analysis manifest

Required fields:

- report schema version
- run ID
- project logical root
- source roots
- analyzed commit and dirty-state summary when Git is available
- project configuration digest
- Python executable and version
- provider names, versions, commands, exit states, and output hashes
- included and excluded paths
- parsed, skipped, failed, and unsupported file counts
- cache identity
- start/end timestamps and duration
- overall completeness state

Host-specific absolute paths may be retained in local diagnostic output but portable reports use logical project-relative paths.

### 6.2 Evidence

Every finding includes one or more evidence records:

- logical file path
- symbol-qualified name when applicable
- start/end line and column
- evidence kind
- provider
- observation
- source content hash
- confidence: `exact`, `resolved`, `inferred`, `heuristic`, or `runtime-observed`
- limitations and unresolved edges

### 6.3 Finding

Required fields:

- stable finding ID and rule ID
- category and severity
- title and concise explanation
- evidence list
- affected symbols and modules
- why the issue matters
- false-positive considerations
- recommendation, if any
- recommendation confidence
- estimated behavior and compatibility risk
- affected tests or missing test coverage
- suppression status and rationale

### 6.4 Refactor candidate

A refactor candidate must add:

- current responsibilities
- proposed ownership boundaries
- producer/consumer changes
- dependency edges added and removed
- compatibility facade, if needed
- state and side-effect ownership
- serialization changes
- rollback boundary
- required tests
- validation commands
- unresolved decisions

## 7. Core Analysis Requirements

### 7.1 Project discovery

- Discover `pyproject.toml`, source roots, test roots, namespace packages, and package mappings.
- Resolve absolute and relative imports without package-specific hardcoding.
- Honor Git ignore rules, configured exclusions, generated paths, virtual environments, caches, build outputs, and symlink policy.
- Detect duplicate module names and ambiguous roots.
- Record parse failures rather than silently skipping them.

### 7.2 Symbol and structure index

Index:

- modules and packages
- classes and inheritance
- functions and methods
- nested functions and closures
- parameters, annotations, defaults, decorators, and returns
- assignments, globals, nonlocals, and class attributes
- raised and caught exceptions
- context managers and resource ownership
- source spans and docstrings

Use the standard AST for fast structural analysis and LibCST for exact source representation, qualified names, scopes, and future codemods.

### 7.3 Import and architecture graph

- File, module, package, class, and resolved-symbol dependency graphs.
- Separate runtime imports, `TYPE_CHECKING` imports, optional imports, lazy imports, and string/dynamic imports.
- Strongly connected components and simple cycles.
- Fan-in, fan-out, afferent/efferent coupling, instability, centrality, and community clustering.
- Layer and forbidden-import policy evaluation.
- Independent provider comparison between native resolution, Ruff, Pyreverse, and Pydeps.
- Disagreements remain visible; absence from one graph is not proof of absence.

### 7.4 Function and method complexity

Measure at least:

- physical and logical LOC
- cyclomatic complexity
- branch count
- nesting depth
- parameter count
- number of returns and raises
- local-variable count
- external calls
- side-effect count
- mutable-state access
- exception-path count

Thresholds must be configurable. The report distinguishes orchestration functions from calculation, parsing, validation, persistence, and rendering functions.

### 7.5 Data-contract inventory

Identify public and cross-boundary values involving:

- Pydantic models
- dataclasses
- `TypedDict`
- `NamedTuple`
- protocols
- enums and literals
- dictionaries and mappings
- `Any`
- untyped containers
- JSON, YAML, TOML, CSV/TSV, and binary serialization

Classify each boundary as:

- persisted
- API/HTTP
- cross-subsystem internal
- module-local
- test/qualification-only

Classification is not an instruction to convert every dictionary.

### 7.6 Dictionary-shape and JSON-flow analysis

This is a primary product capability.

Infer dictionary shapes from:

- dictionary literals and comprehensions
- assignments and updates
- `payload[key]`
- `payload.get(key)` and defaults
- membership tests
- key-set comparisons
- `pop`, `setdefault`, merge, and unpacking
- return values
- function arguments and call sites
- JSON load/dump boundaries
- Pydantic `model_validate` and `model_dump`
- dataclass conversion
- HTTP request and response construction

For each inferred shape, report:

- required and optional keys
- inferred value types
- conflicting producers or consumers
- unknown-field policy
- schema-version handling
- exact-type checks versus coercion
- bool-as-int hazards
- permissive `.get()` or truthiness checks
- default synthesis
- mutation after validation
- serialization and deserialization sites
- producer/consumer path with confidence

Detect likely candidates for:

- Pydantic persisted/API contracts
- dataclass internal runtime state
- `TypedDict` static-only structures
- protocol interfaces
- intentionally untyped external compatibility payloads
- dictionaries that should remain local implementation details

The tool must never claim a complete cross-file data flow when unresolved dynamic calls remain.

### 7.7 Validation-sprawl analysis

Detect repeated or distributed validation patterns, including:

- manual required-key and exact-key checks
- repeated type checks
- schema-version checks
- duplicate enum/state validation
- timestamp and hash validation
- path traversal and canonicalization checks
- symlink, inode, device, and ownership validation
- repeated response-envelope parsing
- fallback and fail-open behavior
- validation performed differently by producers and consumers

Differentiate structural validation suitable for a model from operational validation that must remain in filesystem, runtime, or security code.

### 7.8 Data-structure analytics

Analyze how data is represented, transformed, owned, mutated, retained, serialized, and shared. This capability is broader than dictionary-shape inference and must cover:

- built-in containers and nested generic types
- Pydantic models, dataclasses, `TypedDict`, `NamedTuple`, enums, protocols, and custom collections
- graph, tree, queue, cache, registry, index, journal, manifest, event-row, tensor-metadata, and tabular structures
- mutable versus immutable representations
- optional, union, sentinel, and state-discriminated representations
- recursive and self-referential structures
- aliases and multiple names referencing mutable values
- class, instance, module, thread, process, request, and persistent ownership
- construction, validation, mutation, copy, serialization, persistence, and destruction lifecycle
- conversions among models, dataclasses, dictionaries, tuples, rows, JSON, and binary records
- repeated conversions or normalization passes
- schema-version and migration boundaries
- indexing and lookup strategies
- ordering, uniqueness, identity, and key-normalization assumptions
- bounded versus unbounded growth
- cache eviction and cleanup behavior
- thread-safety and lock coverage for shared structures
- approximate shallow and retained-size opportunities when runtime evidence is explicitly enabled

For each significant structure, produce a topology and lifecycle record containing:

- defining and owning module
- canonical type or inferred shape
- constructors and producers
- readers, writers, mutators, and serializers
- persistence or wire boundaries
- invariants and where they are enforced
- duplicate or conflicting representations
- conversion edges
- mutability and aliasing risk
- concurrency scope
- lifetime and cleanup path
- size/growth indicators
- candidate simplification or consolidation
- evidence confidence and unresolved dynamic behavior

Detect potential issues such as:

- the same conceptual record represented as several incompatible dictionaries
- models converted to dictionaries and manually revalidated downstream
- duplicated DTOs or Pydantic models with divergent constraints
- dictionary keys serving as an implicit state machine
- parallel lists or dictionaries that should be one record collection
- repeated linear scans that imply a missing index
- mutable defaults or shared class/module state
- unbounded registries, caches, queues, or histories
- deep-copy churn and unnecessary serialization round trips
- data ownership that crosses subsystem boundaries without a stable contract
- cleanup paths that do not match allocation or registration paths

Recommendations must distinguish maintainability improvements from performance claims. Memory or speed recommendations require runtime measurement; static structure alone is not a performance result.

### 7.9 Duplicate and commonality analysis

Support multiple clone classes:

- exact text clones
- normalized token clones
- AST clones after identifier/literal normalization
- similar statement sequences
- similar control-flow and call sequences
- repeated validation and serialization idioms

For function commonality, compare:

- called qualified symbols
- parameters and return shapes
- accessed attributes and globals
- raised exceptions
- side effects
- control-flow features
- dictionary keys and schema operations
- docstring and name semantics as weak signals only

Findings must show side-by-side evidence and explain whether consolidation would reduce or increase coupling. Similar code owned by different domains is not automatically a shared helper.

### 7.10 State, concurrency, and side-effect analysis

Identify:

- mutable module globals
- process-global locks, registries, caches, and singletons
- class-level mutable state
- lock acquisition sites and possible ordering
- thread/process creation
- subprocess and signal lifecycle
- filesystem writes, renames, deletes, and atomic replacements
- network listeners and HTTP clients
- environment-variable reads and mutations
- implicit HOME/cache/config dependencies

Report likely ownership violations, hidden cross-instance coupling, and cleanup paths. Static deadlock claims require explicit evidence or runtime confirmation.

### 7.11 Cohesion and extraction analysis

Cluster methods and functions using shared:

- fields and globals
- callees and dependencies
- data contracts
- side effects
- exception types
- tests
- change history

Propose extractions only when clusters indicate distinct ownership. Estimate the before/after dependency graph and flag proposed helpers that would become high-coupling utility modules.

### 7.12 Git change coupling

When Git history is available:

- files and symbols changed together
- churn and contributor concentration
- defect/fix concentration when inferable from commit metadata
- frequently changed interfaces
- stable modules with high static coupling

History is supporting evidence, not proof of architecture intent. It must be collected explicitly rather than included in ordinary static scans. Default code-coupling evidence to Python paths and require repeated co-change before retaining a pair; repository-wide scope must be an explicit opt-in. Allow date and commit-range bounds and exclude bulk formatting commits.

### 7.13 Tests and runtime evidence

- Map test imports, patches, fixtures, and direct symbol references.
- Ingest coverage.py line, branch, and context data.
- Identify changed or extracted symbols without direct coverage.
- Optionally collect a dynamic call graph using Python 3.12 monitoring facilities or another low-overhead provider.
- Reconcile static and runtime edges without treating one observed workload as exhaustive.
- Do not run tests or runtime tracing unless explicitly requested.

### 7.14 Runtime performance and call-graph analysis

Runtime profiling is opt-in and must never occur during a default static scan. The initial provider uses Python's standard `cProfile` and `pstats` modules so projects can retain and compare conventional `.prof` artifacts without adding a profiler dependency to the analyzed application.

Required capabilities:

- run an explicitly supplied Python module, script, test command, or command argument array under `cProfile`
- record working directory, bounded environment identity, interpreter, command, timeout, exit status, and workload identity
- retain the raw `.prof` artifact and a normalized, portable derived report
- parse total and primitive call counts, total time, cumulative time, per-call time, callers, and callees
- generate function-level call graphs with configurable pruning by cumulative time, total time, call count, package, and depth
- distinguish project, standard-library, environment, generated, and third-party frames
- map profile frames to indexed modules and symbols where source is available
- compare baseline and candidate profiles only when workload, interpreter, configuration, and repetition identity are compatible
- report absolute and relative deltas without claiming an optimization win from one profiled run
- support repeated profile runs and report variance for selected functions
- export hotspot tables, caller/callee tables, Graphviz, Mermaid, SVG, PNG, JSON, and Markdown artifacts
- allow the TUI and Web UI to navigate from a hotspot or call-graph node to source and static-analysis findings

Profiling reports must disclose:

- deterministic profiling overhead and why profiled wall time is not an unprofiled benchmark
- whether threads, child processes, native extensions, async tasks, or subprocess work are incomplete or unattributed
- recursive-call treatment
- frames removed by filtering or path normalization
- command failures and incomplete profile artifacts
- whether the workload completed successfully

Portable `.prof` processing must use `pstats.Stats`, not byte-level string replacement. Portableization must preserve function keys, call counts, timings, and caller relationships, reload successfully, and graph-compare equal to the source profile after path substitution. Path collisions must fail closed.

Runtime safety requirements:

- execution requires an explicit CLI profiling command and `--allow-execution`; the initial TUI and Web UI expose no execution route
- commands use argument arrays without shell interpolation
- environment additions are allowlisted and recorded
- timeout and process-tree cleanup are mandatory
- the suite must not terminate unrelated processes
- output and temporary paths must be newly created under a run-owned directory
- profiling an untrusted repository is equivalent to executing untrusted code and requires a visible warning

Future optional providers may include `pyinstrument`, `scalene`, or `memray`, but they must implement the same provider/result contract. Their absence must not affect core `cProfile` support.

#### 7.14.1 cProfile ingestion and managed-service expansion

The Phase 4 profile commands and managed runner are unreleased interfaces over one bounded process-control, ingestion, comparison, and rendering architecture. There is no legacy Code Analysis Suite runtime contract. The normative [cProfile runtime-analysis specification](cprofile-runtime-analysis-specification.md) defines their current responsibilities.

The managed runner proves and controls the direct subject plus descendants whose PID and process-start identity it observes. It rejects observed detached sessions and never signals an identity it cannot prove it owns. This is a process-ownership contract, not an operating-system sandbox: a deliberately daemonized child that escapes observation cannot be claimed as contained. The limitation must remain visible, and execution of untrusted code still requires an external sandbox appropriate to the operator's threat model. Read-only Phase 5 report exploration does not expose runtime execution and is therefore independent of this limitation.

The expanded runtime boundary must:

- keep offline `.prof` ingestion separate from explicitly authorized managed execution;
- accept multiple unchanged source profiles through `pstats.Stats`, retain their hashes, and preserve project, dependency, standard-library, native/built-in, generated, unresolved, caller/callee, recursion, and primitive-call evidence;
- use a strict declarative run specification for argv commands, readiness, workloads, expected status, artifacts, graceful/forced shutdown, cleanup, redaction, workload identity, and external fingerprints;
- support `python-module` and `application-managed` collection without monkeypatching, `sitecustomize`, import interception, hidden injection, shell interpolation, or operator interaction;
- prove that the actual subject PID owns the profile, wait for graceful shutdown and serialization, and fail closed on startup, readiness, workload, PID, shutdown, serialization, artifact, or profile failures;
- bind comparisons to compatible source, interpreter, workload, external-artifact, profiler, PID, and collection-scope identities; diagnostic overrides remain ineligible for performance claims;
- correlate runtime frames and edges with static symbols, calls, complexity, clones, duplicates, responsibilities, ownership candidates, contracts, boundaries, and dependency rules without replacing static evidence;
- retain unobserved static symbols as unobserved rather than dead and keep self time, cumulative time, amplification, per-call cost, maintainability, ownership, and runtime coverage as separate review dimensions;
- classify generic runtime frames through explicit deterministic project rules rather than hard-coded framework, model, MLX, database, or web names;
- emit the standard JSON, Markdown, TSV, DOT, SVG, PNG, identity, diagnostics, category, overlay, and manifest artifacts with complete hashes, filters, omissions, and limitations;
- retain complete machine-readable oversized graph components while bounding only derived presentation views;
- remain partial or failed whenever identity, profile integrity, collection scope, shutdown, serialization, correlation, or analytical evidence is incomplete.

Profile-derived cumulative times must never be summed or described as partitioning wall time. Waits, locks, selectors, futures, queues, joins, generators, asynchronous work, native work, threads, and subprocesses must retain their actual evidence limitations. A profile is call-ownership and timing evidence for one declared workload, not a throughput baseline.

### 7.15 Dead and unreachable code

- Integrate Vulture findings.
- Add import reachability from declared entry points.
- Distinguish framework callbacks, plugin entry points, decorators, reflection, and dynamically imported names.
- Require confidence and suppression mechanisms.

### 7.16 Architecture contracts

- Read and evaluate Import Linter contracts when present.
- Allow native configuration for layers, forbidden imports, protected packages, independent siblings, and acyclic siblings.
- Generate proposed contracts from an accepted architecture plan, but never silently write or enforce them.

### 7.17 Language front ends and JavaScript static analysis

Language-specific parsers must implement one normalized language-front-end protocol. Parser-specific AST or concrete-syntax nodes remain private to that front end; core analysis, report assembly, queries, comparisons, and projections consume typed normalized facts for modules, symbols, imports/exports, calls, control flow, mutable state, side effects, clone fingerprints, commonality dimensions, diagnostics, and completeness.

Source language is an explicit bounded identity on source files, modules, symbols, spans, parser evidence, and cache records. JavaScript also records syntax mode and module system. Stable entity identities include source language and logical source identity but exclude physical paths and parser versions; observation and cache identities additionally bind source hash, parser/grammar and normalization identities, language/module modes, and relevant configuration.

Initial JavaScript scope is `.js`, `.mjs`, `.cjs`, and `.jsx`. JSX passed the same explicit span, recovery, determinism, and portability fixtures as ordinary JavaScript. TypeScript and TSX are later additive front ends. Static scans must never execute, import, bundle, transpile, or otherwise run target JavaScript.

The initial JavaScript front end must support static evidence for:

- ESM imports/exports and literal CommonJS `require`/export forms;
- default and named exports;
- functions, methods, classes, fields, private fields, object methods, closures, and nested functions;
- async functions, `await`, Promise chains, callbacks, and event listeners;
- module-level mutable state and exact mutation/cleanup syntax;
- syntax-proven fetch, WebSocket/EventSource, storage, DOM, timer, filesystem, subprocess, and network side-effect categories with lexical provenance;
- `JSON.parse` and `JSON.stringify` boundaries;
- repeated event-stream lifecycle, request/response normalization, validation, error translation, and retry/reconnect idioms.

Dynamic imports, computed properties, runtime module resolution, framework-generated behavior, monkey patching, shadowed globals, and arbitrary method-name matches remain unresolved unless a later provider supplies exact evidence. Syntax does not prove runtime success, resource ownership, cleanup effectiveness, performance, or semantic equivalence.

Duplicate-behavior analysis must layer exact-text, normalized-token, normalized-structure, overlapping statement-window, control/call sequence, validation/error, serialization/persistence, dependency/side-effect, and signature/contract evidence. Bounded near-clone comparison may tolerate renamed variables, inserted checks, changed constants, and small statement edits, but it must expose each component and difference rather than one opaque similarity score. Names and comments/docstrings are weak supporting signals and cannot originate a candidate. Initial clone admission is same-language only.

Parser absence, syntax failure, unsupported syntax, truncation, and malformed output are explicit unavailable, failed, partial, unsupported, or truncated evidence. If JavaScript is selected or discovered in scope, these states cannot become successful empty analysis. Python-only scans do not require a JavaScript parser and record that front end as disabled or not requested.

The implemented parser is an in-process Python Tree-sitter front end using `tree-sitter==0.25.2` and the official `tree-sitter-javascript==0.25.0` grammar. A bounded external parser may provide future optional conformance evidence. Node/npm must never be required to load, query, compare, export, or render retained reports, and dependencies must never be installed silently.

### 7.18 Actionable refactoring candidates

The suite must distinguish evidenced duplication from a favorable, behavior-preserving refactor. It must provide a first-class saved-report worklist with bounded filters for module scope, exclusions, production/test subjects, cross-module candidates, proposal/decision state, shared dimensions, source language, candidate kind, and existing-owner preference. Worklist ordering must expose its independent dimensions and must not collapse cost, risk, ownership, qualification, or graph evidence into an opaque score.

Every candidate must have a stable identity distinct from its report-bound observation and proposed solution. Compatible runs retain that identity when proposed ownership, strategy, evidence, cost, coverage, or dependencies change; changing the participating subjects creates a different candidate. Proposal identities bind candidate identities to proposed ownership and strategy. A separate local decision ledger must retain accepted, implemented, rejected, deferred, and superseded maintainer decisions with the reason, report identity, observation identity, optional proposal identity, and strategy/evidence/dependency signature. Unchanged rejected candidates are hidden from the default worklist but remain queryable. A decision is review state, never authorization to modify source.

Every proposal must retain a transparent abstraction-cost estimate covering existing duplicated production LOC, expected deletions, new helper/type/module LOC, adapter/migration LOC, estimated net production LOC, new files/types/callbacks/parameters/options/imports, dependency/fan/SCC/cycle deltas, and expected test additions. Unknown values remain unresolved rather than zero. Machinery equal to or greater than expected deletion must downgrade or block a proposal unless separately justified by evidenced ownership or contract value.

Owner selection must prefer an existing semantic owner, then an existing primitive, then an existing domain module, and propose a new owner only when no suitable owner is evidenced. Reports must identify owner alternatives and typed warnings for unowned common modules, forwarding or re-export layers, compatibility shims, option-heavy helpers, parallel contract families, and abstractions whose only visible purpose is changing analyzer metrics. Threshold-only complexity findings cannot become extraction candidates without independent ownership or duplication evidence.

Behavioral comparison must retain, where the language adapter can prove syntax and order, exception types/messages/precedence, effects before failure, file-read order, mutation/publication order, branch predicates, defaults and missing/null/malformed/unknown-input behavior, serialization key presence/order, atomic replacement/rollback, resource acquisition/cleanup order, and external-call order. Unresolved dynamic dispatch, callbacks, concurrency, and runtime outcomes remain explicit. Structural similarity cannot imply semantic interchangeability or erase counter-evidence.

Clone analysis defaults to function or method boundaries, bounds maximum windows, suppresses overlapping subfragments within a representation, treats module-level aggregates separately, and exposes separate exact, token, structural, call-sequence, data-flow, and semantic-candidate rankings. Renamed variables and small literal differences remain discoverable, while broad statement clusters cannot outrank coherent duplication solely by span or member count.

### 7.19 Language capabilities, boundary correlation, and partial failure

Every language front end must declare whether parsing/provenance, spans, module identity, symbols, dependencies, calls, control flow, data flow, exceptions, effects, contracts/shapes, clone normalization, test discovery, and coverage binding are supported, partial, unsupported, or unavailable. Core analysis must not infer evidence for an undeclared capability.

Frontend and downstream section results must validate before report integration. An invalid adapter or section record makes the affected language/section failed or partial, emits a diagnostic, and returns host `ENODATA`, while already validated evidence from other languages remains available. The final report remains strict and never persists malformed records. A JavaScript responsibility-cluster validation failure, for example, must not discard valid Python evidence.

Boundary evidence must distinguish persisted documents, HTTP requests, HTTP responses, CLI/render output, external opaque payloads, internal mutable indexes, internal record-shaped mappings, configuration merges, hash/fingerprint inputs, test/qualification evidence, and third-party payloads. Model or dataclass recommendations must not target legitimate HTTP, render, fingerprint, opaque, or serialization boundaries without evidence of internal schema drift.

Cross-language interface analysis must correlate syntax-evidenced backend endpoints and frontend consumers by HTTP method/path, request/response fields, status and public error codes, SSE events, WebSocket variants, missing/null assumptions, retry/polling behavior, and lifecycle states. It reports mismatches with source evidence on both sides, preserves ambiguity and partiality, and leaves domain semantics and authorization consumer-owned.

Each actionable candidate must report direct test references, likely call-path tests, bound coverage when available, canonical test discovery, and missing positive, negative/adversarial, parity, rollback, and failure cases. Unresolved test impact or mismatched coverage keeps the candidate incomplete.

Additional adapters, including a future Ruby/Prism front end, must use this capability contract and keep parser/framework nodes private. Optional embeddings may nominate bounded candidate pairs only; deterministic structure, call, data-flow, effect, behavior, ownership, and qualification evidence remains governing.

## 8. External Provider Contract

Each provider implements a common interface:

```python
class AnalysisProvider(Protocol):
    provider_id: str

    def availability(self, context: AnalysisContext) -> ProviderAvailability: ...
    def run(self, context: AnalysisContext) -> ProviderResult: ...
```

Provider results include:

- exact executable path and version
- invocation arguments
- bounded environment identity
- exit code and duration
- stdout/stderr hashes
- normalized observations
- parse warnings
- completeness and confidence

External providers must run without shell interpolation, with argument arrays and timeouts. Missing optional providers produce an unavailable state, not a successful empty result.

## 9. CLI Requirements

Proposed command surface:

```text
code-analysis doctor
code-analysis scan PATH
code-analysis summary REPORT
code-analysis query REPORT
code-analysis contracts PATH
code-analysis dict-shapes PATH
code-analysis clones PATH
code-analysis graph PATH
code-analysis refactor PATH
code-analysis diff BASELINE CANDIDATE
code-analysis profile -- COMMAND [ARG...]
code-analysis profile-report PROFILE
code-analysis profile-diff BASELINE CANDIDATE
code-analysis runtime ingest --profile PROFILE --repo ROOT --out REPORT_DIR
code-analysis runtime run --spec RUN_SPEC --out REPORT_DIR
code-analysis runtime compare --baseline REPORT --candidate REPORT --out REPORT_DIR
code-analysis runtime render --report REPORT --direction top-to-bottom
code-analysis tui [PATH|REPORT]
code-analysis serve [PATH|REPORT]
code-analysis export REPORT
code-analysis providers
```

An optional short alias such as `coda` may be added only after confirming it does not conflict with another installed command.

Required common options:

- source and test roots
- source-language and language/module-mode selection
- include/exclude patterns
- provider selection
- strictness profile
- config path
- output path and format
- cache control
- timeout
- Git range
- symbol/module filter
- maximum findings
- portable-path mode
- `--no-color`
- `--quiet`
- `--verbose`

Exit statuses must use names and meanings defined by the host C library's `<errno.h>` interface. The implementation must obtain these values through Python's `errno` module rather than assigning application-specific meanings to arbitrary integers. Numeric values are intentionally host-native because POSIX specifies the symbolic error names but does not require identical numbers on every platform.

The required status contract is:

- `0`: successful complete analysis with no selected policy violation
- `EPERM`: the configured policy does not permit the selected findings
- `EINVAL`: command-line syntax or an option argument is invalid
- `ENODATA`: analysis cannot provide complete data
- `EBADMSG`: configuration or a persisted report is malformed
- `EPROTO`: a provider violates or cannot complete its execution protocol
- `EFAULT`: the CLI reaches an invalid internal boundary

Filesystem and operating-system failures must preserve a positive `OSError.errno` such as `ENOENT`, `EACCES`, `ENOTDIR`, `ELOOP`, `EROFS`, or `ENOSPC`; they must not be collapsed into `EBADMSG` or another application category. The symbolic mapping is the contract; documentation, tests, and monitoring integrations must not assume cross-platform numeric equality. Argument parsing must use `EINVAL` instead of `argparse`'s default status `2`. Do not use BSD `sysexits.h` values as though they were POSIX error numbers.

JSON output goes to stdout only when requested; diagnostics go to stderr.

## 10. TUI Requirements

The TUI is a view over saved or live analysis results, not a separate analyzer. Deliver the clarified collection workflow in the Web UI first, then expose equivalent collection, import, exploration, comparison and export operations through shared backend contracts in the TUI. Equivalent semantics and terminology are required; embedded graphical parity is not.

Required screens:

- project overview and completeness
- module/package dependency graph
- module and symbol browser
- complexity and responsibility candidates
- data-contract and dictionary-shape explorer
- duplicate cluster comparison
- cycles and architecture violations
- global state and side effects
- runtime hotspots, callers/callees, and profile call graphs
- test-impact view
- refactor candidate packet
- baseline/candidate diff
- provider health and diagnostics

Required interactions:

- keyboard-first navigation
- filters by category, severity, confidence, package, and ownership
- evidence source preview
- graph-to-symbol navigation
- export current filtered view
- open a selected SVG, PNG, PDF, or HTML artifact through a configured external viewer
- hand off the current report, finding, graph, or symbol selection to the loopback Web UI
- no source editing in the initial release
- usable terminal fallback when graph rendering is unavailable

The TUI must handle large reports through lazy loading or indexed report storage rather than constructing every widget at startup.

External viewer launch must use an argument array rather than shell interpolation. On macOS the default adapter may use `open`, allowing Preview or the user's selected application to display generated artifacts. Other platforms and headless sessions require configurable adapters and a path-only fallback. Opening an artifact is an explicit user action; report loading must not launch applications automatically.

## 11. Web UI Requirements

The Web UI is the preferred rich visual explorer for large graphs, source comparisons, data-structure topology, interactive filtering, and report sharing. It is not the analysis engine and must consume the same versioned report models and query service as the CLI and TUI.

Required operating modes:

- loopback-only live server for local analysis and report exploration
- read-only opening of an existing report without rerunning analysis
- exportable static HTML report where interactive server features are not required
- loopback-only binding in Phase 5; remote browser access may use an operator-managed SSH tunnel

The current implementation supports local report browsing and self-contained static HTML export. Per the September 23 maintainer decision, Phase 5 retains loopback-only binding and permits operator-managed SSH tunneling. Direct non-loopback serving is deferred to a separately designed and security-reviewed follow-up, not an open Phase 5 deliverable. That follow-up must require explicit opt-in and reviewed authentication, authorization and transport protection; a tunnel does not authorize collection or change server-side filesystem confinement.

Required views:

- project and provider overview
- interactive package, module, symbol, and call graphs
- SCC and dependency-path exploration
- source evidence with synchronized graph selection
- complexity and ownership heatmaps
- dictionary/data-contract flow diagrams
- data-structure topology, lifecycle, conversion, and ownership diagrams
- clone and function-commonality comparison
- state, concurrency, and side-effect inventory
- runtime profile hotspots, caller/callee exploration, and profile comparison
- test and coverage impact
- refactor candidate packet and baseline/candidate diff
- downloadable JSON, Markdown, Graphviz, Mermaid, SVG, and PNG artifacts

The Web UI must be responsive, keyboard accessible, and usable on desktop and tablet-sized displays. Mobile support is a responsive convenience view, not a separate application or primary analysis workflow.

Security and operation:

- bind to `127.0.0.1` by default
- do not expose source or reports remotely without explicit configuration
- apply confined path handling to source and artifact requests
- do not allow arbitrary command execution through the UI
- keep analysis execution, process state, and report browsing separate
- show provider failure and incomplete-analysis states prominently
- preserve source read-only behavior

### 11.1 Shared UI architecture

Bare invocation and `ui` must launch a detached, readiness-verified loopback session and return the terminal prompt, reopening only sessions with matching project/input, interpreter/environment, source and execution permissions. Retain `ui --foreground` and lower-level foreground `web` for debugging/tunneling. Application-owned private session records and capability verification must prevent unrelated processes from being stopped; stale recorded PIDs are never signal targets. Provide explicit authenticated same-origin browser shutdown and `ui-stop`, refusing active jobs unless cancellation is explicitly confirmed. Tab closure/reload is not shutdown. Preserve existing bounded owned-worker cleanup, source confinement and per-job execution authorization. This lifecycle is Code Analysis-owned, composed with existing LocalOps controls rather than a toolkit daemon dependency.

The CLI, TUI, and Web UI must share:

- analysis orchestration services
- report repository and query APIs
- filtering and sorting semantics
- finding and evidence models
- artifact generation
- selection and deep-link identities

Do not duplicate interpretation logic in JavaScript. The backend or shared report model determines finding state, confidence, severity, and relationships; UI clients render those values.

The current Web UI uses a per-process capability token for report-data requests, validates the `Host` header as loopback, sends restrictive browser security headers, and serves bounded typed pages. Its existing browsing routes do not rescan a target, mutate decisions, or execute workloads. Source reading is disabled by default; an explicit `--source-root`, or an authenticated same-origin **Connect source folder** confirmation inside the startup-configured collection root, enables confined, bounded previews whose bytes must match the selected saved report hash before disclosure. UI connection must validate a recorded source file before changing session access, reject stale report identities and invalid roots without changing the previous connection, and grant no workload-execution authority. Never infer source permission from an imported report or current directory alone.

#### Collection and display workflow clarification

The intended shared workflow is select project, collect or import, explore, compare, then export/open. Per the September 23 maintainer clarification, bare `code-analysis` and `code-analysis ui` enable confirmed managed collection by default, confined to the current directory unless `--collection-root` specifies another root. `ui --no-collection` disables launch capability. Startup and previews must not run analysis or create a workspace; each job requires explicit confirmation and runtime workloads require a separate execution acknowledgment. Lower-level `web` and terminal launch capability remains opt-in. Shared contracts provide static/profile planning, saved-profile import, trusted interpreter selection, report-free startup and verified result opening. Standalone saved-profile browsing/comparison does not require a static report. The detailed implementation and acceptance additions are in the [current scope and roadmap](roadmap.md).

- Static collection selects source paths, languages, exclusions and settings without executing target code. Runtime collection selects an interpreter, script/module, arguments, working directory and capture settings, or imports saved profiles. Runtime execution requires explicit confirmation and existing ownership/scope checks; no whole-program or thread-coverage guarantee follows from a saved profile.
- Web/TUI controls construct a shared typed command specification. Display a live copyable command preview, execution host and working directory; execute the same arguments through existing CLI/backend mechanisms without a shell. Disclose required run-spec files and inputs. Keep secrets out of previews/logs. No arbitrary shell execution, scheduler or cron configuration is provided.
- Add confined server-side filesystem navigation for targets and saved collections alongside retained-run selectors. Selection alone grants no execution authority. Keep source, output/workspace and preview roots distinct, including external workspaces for read-only targets.
- Keep job ownership on the server with inspectable progress, logs, native exit status, completeness and artifact locations. Define queued/running/completed/partial/failed/cancelled outcomes, reconnectable status and explicit bounded cancellation. Forced termination may prevent capture publication and must not appear complete.
- Browsing, correlation, comparison and rendering consume saved evidence without recollection. Preserve compatibility checks and diagnostic-only states. New collection is always explicit.
- Prefer packaged HTML/JavaScript/CSS and existing Python services over new frameworks. Use system-associated external viewers for CLI/TUI artifacts and browser presentation for the Web UI. Trusted browser/viewer overrides and URL/path-only fallback are user preferences, excluded from analytical identities.
- Keep loopback-first service operation and optional SSH-tunnel access distinct from reviewed non-loopback exposure. Clearly label the execution host/filesystem; local browser display does not make server paths local or authorize remote execution.
- Keep shared schema models and validated loaders importable independently of UIs and optional collection providers. Additional analytical tools consume those contracts rather than duplicating structures. Automatic migration/conversion and replacement of original artifacts remain out of scope; unsupported formats fail explicitly.

Collection routes require their own authorization, request-origin, path and process-ownership validation; read-only report access must not implicitly grant workload execution. Existing Phase 5 safety, accessibility and qualification requirements remain in force.

### 11.2 Synchronized code browser and session drafts

The approved follow-up is a Web-first browser over saved entities and relationships, with an independently qualified reusable LocalOps editor for session-only drafts. The [current scope and roadmap](roadmap.md) define four checkpoints; documentation is not implementation acceptance.

The browser must synchronize file/entity outlines, focused maps, recorded details and hash-verified original source. A single pointer activation or Enter/Space on a source-bound graph node must select its recorded source without a second button; initial highlights and collapse redraws must not generate navigation loops. Rejected draft/IME navigation must not leave a misleading selection. Containment, imports, static calls, runtime calls and data flow are distinct layers with explicit unknowns and bounds. Source-span containment may be a labelled visual grouping, not invented ownership. Ambiguous line selections offer the recorded choices. Saved analytical navigation works without source; source access requires an explicit root and complete hash verification. No rescan or execution occurs during navigation.

Provide read-only project reading alongside code investigation. Offer README files beside the selected saved source and in up to 16 ancestor directories, plus explicitly followed local Markdown/reStructuredText/plain-text links inside the connected source root. Project documents are current filesystem text, not saved analytical evidence: do not add them to source inventories, report schemas, analytical identities or exports. Bound documents to 128 KiB and lists/history to 50 entries; disclose omissions. Bind each document read to the selected report, connected directory identity and observed content hash. Reject private/hidden paths, escapes, symlinks, nonregular files, stale hashes and invalid UTF-8. Render literal text in existing LocalOps panels; do not execute examples, HTML, scripts, external links or embedded images. Preserve the investigation and label hints/solutions rather than opening spoilers automatically.

LocalOps owns a transport-neutral native textarea editor and its accessibility/qualification; Code Analysis owns source retrieval, report identity, selection, draft provenance and copy/download. Do not fork bundled toolkit assets. Drafts are explicit full-file reads limited to 1 MiB and 20,000 lines, with at most eight session-memory documents and bounded 100-transaction/8 MiB history per document. Larger or mixed-newline files stay read-only. Dirty drafts are labelled **Draft—not analyzed**, cannot inherit original source-span claims, and require explicit discard/export rather than silent replacement. Preserve uniform newline and final-newline state on download.

No source save route, automatic browser/workspace draft persistence, target execution, automatic draft analysis, IDE services or codemods are introduced. Analytical JSON remains authoritative and contains neither source nor draft text. Session loss and absent/changed/unavailable source remain visible. Existing loopback, path, revision, accessibility and failure-state rules still apply.

The bounded full-document read is exposed through authenticated `/api/code-browser/document`, retaining exact saved-report/selection binding and an explicit source root. It supplies the implemented LocalOps CodeEditor/session-draft workflow without granting source-write authority. Source identity hashes original bytes rather than universal-newline-normalized text; unchanged CRLF source must remain verifiable and newline-only changes must be detected. Failed interface source reads retain explicit partial diagnostics and cannot become successful empty collection.

### 11.3 MLXForge UI reuse

MLXForge may provide useful implementation patterns and generic visual components, but Code Analysis Suite must not depend directly on MLXForge application modules.

Candidates for reuse or extraction into a neutral UI toolkit include:

- design tokens and responsive layout primitives
- navigation, tabs, tables, filters, and segmented controls
- status, error, loading, empty, and no-data states
- accessible modals and destructive-action confirmation patterns
- path fields, copy/download controls, and artifact manifests
- chart containers and responsive overflow handling
- keyboard-focus and layout-oracle test techniques

Do not reuse MLXForge-specific model lifecycle, conversion, serving, metrics-control, or daemon assumptions. Reused components must have neutral names, independent tests, and no import or runtime dependency on `forge`.

The first implementation should inventory reusable MLXForge assets and choose one of these approaches:

1. Reimplement small generic patterns in Code Analysis Suite with provenance documented.
2. Extract genuinely shared components into a separately owned neutral package.
3. Leave domain-specific components in MLXForge.

Avoid copying components repeatedly between repositories because that recreates the divergence this tool is intended to detect.

### 11.4 Guided workflows and usability follow-up

The [current scope and roadmap](roadmap.md) records the October 18 maintainer walkthrough and planned improvements. Technical browser qualification is not human usability acceptance or public-release readiness. Preserve current behavior while making the workflow explicit: choose project, collect, understand, investigate, validate and compare. Group related views into accessible collapsible navigation without hiding expert routes. Each view explains its purpose, required evidence, disabled controls and next actions; essential help must work with keyboard/touch as well as hover.

Add explicit local project setup and statically evidenced entry-point choices without executing target code. Reuse shared command planning and confirmation; a selected project or suggested command is not execution authorization. Keep recommendations deterministic, evidence-linked and honest about missing/partial evidence. Complexity alone cannot prove poor performance, and a recommendation cannot authorize a refactor.

Provide a synthetic navigation tutorial, followed by opt-in problematic-code exercises and an inspectable refactoring guide with rule identities, counterexamples, qualification and rollback guidance. Optional future AI assistance consumes the same evidence and must distinguish agent interpretation from observations. It must not become a required dependency or silently modify source.

Expose storage inventory, retention, pins and a preview/confirmed cleanup workflow through shared backend policy. Protect unique evidence, referenced comparison inputs, current/pinned runs and active jobs; revalidate ownership and identities before deletion. Clearly distinguish regenerable derivatives from raw captures and reports. This is a planned UI capability, not an existing storage screen. Live tracing and verified threaded capture remain separately scoped work; job polling is not live profiling.

LocalOps owns reusable navigation/help/accessibility primitives; Code Analysis owns analytical content and workflow/authorization semantics. Request and independently qualify toolkit additions rather than editing bundled assets. Web-first refinements must preserve shared TUI/CLI semantics and avoid new framework dependencies.

## 12. Reports and AI-Agent Interface

### 12.1 Formats

- versioned JSON: authoritative
- compact JSON/JSONL: agent and streaming workflows
- Markdown: review packets
- HTML: interactive human reports
- Graphviz and Mermaid: diagrams
- SARIF: compatible findings where semantics fit

The authoritative saved report remains directly inspectable JSON. Source coordinates retain named values rather than anonymous positional arrays. Existing evidence/source catalogs may deduplicate repeated facts, but additional indirection is not justified solely by crossing a fixed report-size target. Measure bytes, full-report loading cost and bounded indexed browsing separately. The former 15 MiB and 1,500-commonality-pair self-analysis targets are historical review baselines, superseded by the 2026-09-19 maintainer decision: enforce specified admission rules, false-positive regression controls, metric integrity, explicit bounds/partiality and bounded review views, not an arbitrary cap on retained valid relationships.

### 12.2 Focused queries

Agents must be able to request bounded subsets:

- one module or class
- one finding
- one dependency path
- one clone cluster
- one dictionary shape and its producers/consumers
- one proposed extraction and affected tests

### 12.3 Refactor packet

A generated packet includes:

- problem statement
- exact evidence
- current ownership
- proposed boundary
- allowed-file suggestion
- dependencies and consumers
- data-contract migration
- compatibility and rollback
- focused tests
- architecture metrics before/expected after
- unresolved maintainer decisions

The packet must state that it is a proposal and not authorization to modify code.

## 13. Configuration

Support `[tool.code-analysis]` in `pyproject.toml` and an optional explicit configuration file.

Configuration areas:

- source/test/tool roots
- enabled source languages, extension mappings, JavaScript syntax/module modes, and statically resolved package-metadata policy
- exclusions and generated paths
- package aliases
- architecture layers and contracts
- complexity thresholds
- dictionary-boundary classification
- serialization functions
- framework callbacks and entry points
- clone thresholds
- Git-history exclusions
- runtime commands
- profiling workload identity, frame classification, pruning, and repetition policy
- report portability
- accepted suppressions with rationale and expiry

Do not hardcode project names such as `secrets_kit` or `forge` in core resolution logic. Project-specific rules belong in configuration or plugins.

## 14. Caching and Performance

- Cache by source content hash, source language and mode, analyzer version, provider/parser and grammar identity, Python version, normalization identity, and relevant configuration digest.
- Incremental scans must invalidate dependents when imports, exported symbols, or contract shapes change.
- Report cache hits and misses.
- Never use stale cache entries after a parser/provider version change.
- Support repositories with at least 100,000 source LOC without requiring all report data in memory for TUI use.
- Establish performance budgets after the first representative scans; do not optimize away evidence quality prematurely.

Current selective fingerprint/frontend caches do not establish full dependency-aware incremental analysis. A small source edit currently requires a new scan/report assembly, though valid cached facts may be reused; presentation changes over saved JSON never require recollection. The incremental follow-up must prove canonical equivalence with a clean full scan, invalidate both dependents and affected global calculations, and publish a new immutable generation. Preserve source/workload binding for runtime evidence instead of copying qualification across changed files. See the [current scope and roadmap](roadmap.md).

## 15. Safety, Privacy, and Reproducibility

- Read-only by default.
- Never execute project code during static scans.
- Never import analyzed modules merely to inspect them.
- Runtime and test commands require explicit user authorization.
- Do not follow symlinks outside configured roots unless explicitly enabled.
- Do not delete caches or reports without target verification.
- Portable reports must detect and redact or tokenize host paths and secrets.
- Preserve raw local diagnostics separately from portable exports.
- Record source and configuration hashes so results can be independently reproduced.

## 16. Testing Strategy

### 16.1 Unit fixtures

Synthetic projects must cover:

- `src/` and flat layouts
- namespace packages
- relative, aliased, optional, lazy, and dynamic imports
- cycles and acyclic controls
- nested functions and classes
- Pydantic, dataclass, `TypedDict`, protocol, and raw-dict boundaries
- data-structure topology, lifecycle, aliasing, ownership, and conversion paths
- `cProfile`/`pstats` artifacts, portable call graphs, and compatible/incompatible profile comparisons
- offline multi-profile ingestion, actual-subject PID proof, readiness/workload/shutdown orchestration, redaction, failed profile serialization, and long-lived service-owned cProfile
- conflicting dictionary producers/consumers
- malformed and permissive validation
- global locks and mutable registries
- exact and normalized clones
- framework callbacks and false dead-code controls
- Unicode paths and portable-path handling
- syntax errors and unsupported syntax
- JavaScript `.js`, `.mjs`, and `.cjs` modules; ESM/CommonJS resolution; nested/async/callback syntax; malformed and unsupported JavaScript; and optional JSX mode
- JavaScript exact, renamed-variable, validation-divergent, constant-divergent, exception-divergent, and side-effect-divergent clone pairs plus non-duplicate algorithm and boilerplate controls
- JavaScript parser unavailable, deterministic repeats, and cache invalidation after source/parser/configuration changes

### 16.2 Golden reports

- Stable JSON ordering and schema validation.
- Expected findings, evidence spans, and confidence.
- No host paths in portable fixtures.
- Report migration tests when schema versions change.

### 16.3 Provider integration

- Available, unavailable, timeout, nonzero, malformed-output, and version-mismatch states.
- Independent graph-provider disagreement fixtures.
- No successful empty result after provider failure.

### 16.4 Dogfooding

Run against:

- Code Analysis Suite itself
- MLXForge
- at least one smaller conventional Python package
- at least one namespace-package project

Baseline reports should be retained so architectural changes to the analyzer can be evaluated by the analyzer.

## 17. Delivery Phases

### Phase 0: Repository foundation

- Establish Git history before implementation.
- Add `pyproject.toml`, `src/` package, tests, development instructions, and CI.
- Define strict report schemas and provider protocol.
- Add `doctor` with `venvutil`/Python/tool provenance reporting.

### Phase 1: Migrate `function-analysis`

- Move the existing script into package modules.
- Remove hardcoded package assumptions.
- Correct import resolution and cycle canonicalization.
- Preserve compatible CLI capabilities through aliases or documented migration.
- Add AST, complexity, module graph, JSON, and Markdown tests.

### Phase 2: Data contracts and dictionary shapes

- Contract inventory.
- Dictionary key/type inference.
- JSON producer/consumer boundaries.
- Validation-sprawl findings.
- Data-structure topology, ownership, lifecycle, and conversion analysis.
- Focused CLI queries and Markdown reports.

This phase is the first MLXForge-critical release.

### Phase 3: Duplication, coupling, and extraction candidates

- Text/token/AST clone providers.
- Function commonality.
- Mutable-state and side-effect ownership.
- Cohesion clustering and proposed extraction packets.
- Import Linter contract suggestions.

### Phase 4: Test, Git, and runtime evidence

- Coverage contexts and test impact.
- Git change coupling.
- Optional runtime call observations.
- `cProfile` capture, `pstats` analysis, call graphs, and profile diffing.
- Static/runtime reconciliation.

The implemented Phase 4 profiler is the single-process foundation. Its required pre-UI expansion adds offline multi-profile ingestion, a strict managed-service run specification, actual-subject PID proof, readiness/workload/graceful-shutdown orchestration, complete runtime identity, generic category rules, standard artifact manifests, and fail-closed comparison. See [cprofile-runtime-analysis-specification.md](cprofile-runtime-analysis-specification.md).

### Phase 4J: JavaScript language front end and duplicate behavior

Status: implemented and internally qualified under the single unreleased report contract.

- Add explicit source-language identity and a normalized language-front-end protocol.
- Qualify a reviewed optional JavaScript parser and grammar without making Node/npm a retained-report dependency.
- Index `.js`, `.mjs`, `.cjs`, and `.jsx` symbols, modules, imports/exports, calls, control flow, state, side effects, and unresolved dynamic behavior.
- Extend clone/commonality analysis with transparent bounded near-clone differences and JavaScript-specific behavior idioms.
- Extend the authoritative report, queries, comparisons, and report-only projections directly; do not add compatibility schemas.
- Qualify synthetic fixtures first, followed by separately authorized read-only LocalOps UI Kit and MLXForge JavaScript scans.

The reviewed implementation sequence and explicit deferrals are defined in [current scope and roadmap](roadmap.md). Phase 5 cannot begin by inventing UI-owned JavaScript interpretations; it consumes the same language-neutral saved evidence.

### Phase 4R: Actionable refactoring decisions and interface evidence

- Isolate language-adapter and section failures before adding ranking features.
- Add stable candidates, report-bound observations, transparent worklist filters, and a separate maintainer decision ledger.
- Add abstraction-cost, existing-owner, behavioral-preservation, and clone-noise evidence and gates.
- Expand boundary classifications, Python/JavaScript interface correlation, and candidate-specific test/coverage requirements.
- Add domain-specific regression projections from retained reports.
- Defer Ruby and optional embedding nomination until the P0/P1 gates pass.

The implementation sequence and audited existing/partial/missing capability map are defined in [current scope and roadmap](roadmap.md). These items are planned and must not be reported as implemented until their qualification record exists.

### Phase 5: TUI, Web UI, and rich reports

- TUI screens over stable report models.
- Loopback Web UI over the shared report/query service.
- Static HTML reports and diagrams.
- MLXForge generic-component inventory and reuse decision.
- Baseline/candidate architecture diff workflow.

### Phase 6: Optional assisted codemods

- LibCST-based transformations only after analysis accuracy is established.
- Preview patch, clean-tree check, tests, rollback, and explicit approval required.
- Never combine detection and automatic application in the same default command.

## 18. Representative-project acceptance scenario

Evaluate representative orchestration, validation, persistence, API, state and profiling boundaries in an independently authorized real project, after synthetic/self qualification. Keep project-specific module lists, private captures and machine paths in local acceptance records, not the public specification. Consumer source stays read-only; runtime execution needs its own explicit authorization and cannot be inferred from static-scan permission.

The report must:

1. Identify high-complexity functions with exact evidence.
2. Inventory persisted, API, and cross-subsystem dictionary contracts.
3. Trace at least one dictionary shape from producer to consumer.
4. Trace at least one significant data structure through construction, mutation, serialization, ownership, and cleanup.
5. Detect repeated manual validation patterns.
6. Report conversion/publication/cleanup commonality without automatically recommending one shared utility.
7. Identify process-global mutable state and ownership.
8. Produce import SCCs and compare at least two graph providers.
9. Generate at least one bounded extraction proposal with affected tests and rollback criteria.
10. Profile one deterministic bounded workload and generate a source-linked call graph without treating profiled timing as an unprofiled benchmark.
11. Distinguish confident findings from unresolved dynamic behavior.
12. Emit deterministic JSON plus a concise Markdown review packet.

The suite is not accepted merely because it reproduces line counts or complexity thresholds already produced by `function-analysis`.

## 19. Initial Definition of Done

The initial production-capable release is complete when:

- it is installable in `venvutil` from `pyproject.toml`;
- CLI and TUI use the same strict report models;
- static scans do not import or execute target code;
- malformed files and provider failures fail closed;
- module, symbol, complexity, graph, contract, dictionary-shape, data-structure, validation-sprawl, clone, and `cProfile` analyses have tested fixtures;
- JSON reports are deterministic and versioned;
- focused agent queries return bounded evidence;
- MLXForge acceptance scenario requirements pass;
- documentation explains limitations and confidence;
- source remains unmodified unless a future explicit codemod command is used;
- a clean environment can reproduce the test suite and a representative MLXForge report.

## 20. Explicit Non-Goals for Initial Releases

- Proving semantic equivalence of arbitrary Python functions.
- Proving semantic equivalence of arbitrary JavaScript functions or cross-language implementations.
- Fully resolving reflection, dynamic imports, monkey patching, or generated code.
- Automatically rewriting large modules.
- Treating all dictionaries as defects.
- Treating all duplicate code as a shared-abstraction opportunity.
- Treating high LOC or complexity as proof of poor design.
- Replacing language servers, type checkers, linters, profilers, or coverage tools.
- Loading ML models or requiring ML/AI dependencies for analysis.
- Sending private source code to external services.
- Requiring Node/npm to query, compare, export, or render retained reports.

## 21. Existing Assets to Incorporate

### `function-analysis`

Source (this path is environment-specific; discover or configure it on other hosts):

```text
~/bin/function-analysis
```

Useful capabilities to retain:

- AST symbol hierarchy
- function LOC and approximate complexity
- orchestration classification
- import fan-in/fan-out
- simple cycle reporting
- JSON and text output
- strictness presets

Known limitations to correct:

- standalone script rather than installable package
- ambiguous shebang interpreter
- hardcoded package normalization for `secrets_kit`
- module-level rather than symbol-level dependency analysis
- simplistic cycle canonicalization
- no contract or dictionary-shape flow
- no clone/commonality analysis
- no provider provenance or completeness model
- no tests in this project

### Project-local audit scripts

Project-local audit scripts can demonstrate loose-signature analysis, contract classification, graph reconciliation and measurement-artifact tracing. Treat them as evidence, not automatically reusable or licensed production tools. Reimplement authorized general capabilities behind tested analyzers; do not import private temporary evidence or hardcode consumer paths.
