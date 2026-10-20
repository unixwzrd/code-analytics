# Architecture

*Current ownership guide — 2026-10-20*

Code Analysis separates source collection, analytical evidence, saved-evidence investigation and authorized execution. The report is the analytical authority; a UI, index or diagram must not become a second analyzer. See [beta scope and limitations](beta.md) for current qualification boundaries and the [roadmap](roadmap.md) for future work.

## Ownership map

| Layer | Owns | Does not own |
| --- | --- | --- |
| Project/configuration | Discovery, logical roots, exclusions, source identity and configuration identity. | Executing target imports or guessing runtime dependencies. |
| Language frontends | Parser-private syntax traversal and normalized facts with capabilities/provenance. | A promise of equal analytical support across languages. |
| Analysis domains | Contracts, data flow, clones, state, test/change evidence and candidate admission. | Authorization to refactor or execute a workload. |
| Reports/services | Strict shared records, assembly, queries, comparisons and bounded projections. | Inventing missing evidence to fill a view. |
| Workspace | Owned local artifacts, run records, immutable generations and disposable caches. | Authority to read arbitrary source or delete unrelated files. |
| Web/TUI | Evidence selection, navigation, display and explicit user actions. | Independent analytical ranking, source saving or implicit collection. |
| Runtime/collection | Confirmed jobs, explicit execution acknowledgment, lifecycle and result binding. | A sandbox or proof that all descendants/native work were observed. |

## Language boundary

The built-in peer packages are `languages/python/`, `languages/javascript/` and `languages/ruby/`. `languages/base.py` defines immutable source units, normalized facts and the frontend protocol. `languages/registry.py` selects the built-in factory for each declared source language and validates registry coverage.

This is an explicit internal registry, not runtime plugin discovery. Adding a language requires integration and tests beyond registering a parser. Parser nodes stay private to their language implementation; shared consumers receive normalized facts and explicit capability states.

Python parser ownership is localized in:

- `languages/python/frontend.py` — standard-library AST normalization and frontend capabilities.
- `languages/python/native_ast.py` — Python symbol and complexity traversal.
- `languages/python/imports.py` — Python import collection and resolution.
- `languages/python/call_occurrences.py` — Python invocation-syntax collection.

JavaScript owns its Tree-sitter traversal, cache and call-occurrence adapter inside its package. Ruby owns its Prism-to-JSON driver and call-occurrence adapter inside its package; it invokes the configured external Ruby parser process without loading target Ruby code. Optional parsers are not installed implicitly.

Peer packaging does not make every analysis language-neutral. Python semantic contracts and dictionary analysis remain in the `analysis/` domain modules and reporting services. Python test discovery, coverage binding and cProfile collection also retain Python-specific ownership. JavaScript and Ruby do not acquire those capabilities merely by producing symbols or imports.

Each frontend/domain contribution records supported, partial, unsupported or unavailable capabilities. Failure isolation retains valid source/module/provenance records while excluding malformed downstream contributions. A complete report describes its declared scope, not feature parity. See the [provider contract](provider-contract.md).

## Shared analytical pipeline

`project.py` and configuration services determine the discovered source set and logical identities. Original source bytes are hashed before decoding; newline conversion must not change evidence identity. A newline-only edit invalidates a saved source binding just as another byte change does.

`service.py` assembles reports through enabled language and domain integrations. Domain services own contract/dictionary relationships, clone/commonality evidence, state/effects, test impact, interfaces and candidate admission. Candidates remain observations and bounded proposals, not automatic edits or approvals.

The shared boundary is strict typed evidence:

- `models.py`, `report_models.py` and related domain models define validated analytical records.
- Provider, persisted report and configuration boundaries reject unknown fields rather than accepting unvalidated payloads.
- Source hashes, logical paths, configuration identity, diagnostics, confidence, completeness and limitations remain attached to evidence.
- Missing, failed or truncated evidence stays distinguishable from a valid empty result.

`reports/` owns deterministic serialization. Query and comparison services consume validated saved reports; `projections/` owns the shared table, graph, hierarchy and review-packet builders. CLI, Web and terminal clients reuse these services instead of recalculating eligibility or evidence semantics.

Projection bounds disclose omissions. Intentional filtering is distinct from analytical truncation, and graph grouping does not create source authority. A syntactic call site is not a resolved runtime call; a saved lifecycle relationship is not proof of execution order. See the [report schema](report-schema.md).

## Persistence and derived views

`workspace.py` owns the self-ignored `.code-analysis/` workspace, ownership markers, local run records, atomic latest-report storage and immutable retained generations. Generation manifests bind their artifacts; opening a result uses its exact generation and hashes rather than following a mutable latest alias.

Local provenance and portable evidence have different purposes. Local records may retain physical paths, command details or captured streams needed for operation; portable analytical reports use logical source paths and bounded provenance. Portable paths do not anonymize source-derived names or topology.

Caches and browsing indexes are disposable derivatives, bound to canonical report/profile hashes, builder identity and preparation bounds. They cannot replace the authoritative JSON. Changed or corrupted derivatives fail closed and must be rebuilt rather than silently falling back to another source of evidence.

Indexed terminal reads use bounded saved rows for filtering, paging, navigation and export. Runtime evidence enters an index only when explicitly attached. Source access is independent of workspace/index access and is never inferred from a workspace root.

Existing caches reuse selected clone fingerprints and JavaScript frontend facts. They do not make the entire analysis dependency-incremental. General incremental collection and storage-management UI remain roadmap work.

## Investigation interfaces and toolkit boundary

The browser is a loopback investigation client over saved static/profile services. The optional TUI uses prepared indexes and shared policy. Readiness and workflow services explain available, missing, partial and failed prerequisites without changing original report completeness or collecting evidence.

LocalOps UI Kit **0.1.5** supplies the native JavaScript shell, theme tokens, report tables, CodeEditor and ContentPanel. Bundled assets are unmodified, hash-pinned and MIT-attributed; reusable component changes belong upstream. Shipping additional toolkit assets does not imply their adoption.

Code Analysis owns analytical meaning, command composition, forms, evidence bindings, graph navigation, help content and execution permissions. Table adapters may compose trusted application DOM with literal report text, but report data cannot supply executable markup. Backend ordering, paging and candidate policy remain authoritative.

Selection revisions, abort signals and exact report/source bindings prevent stale responses from replacing a newer investigation. Returning from evidence or source inspection restores the originating selection and view context. Tab-local navigation state is not persisted analytical evidence.

Bundled offline help is compiled from curated public documentation and validated package assets. Runtime help reads bounded packaged resources rather than requiring a checkout or Markdown engine. Documentation failure stays isolated from analytical navigation and readiness.

## Source browsing and drafts

Source preview requires an explicitly authorized source root, a recorded source identity and a matching full-file hash. Confined readers reject path escapes, symlinks and nonregular files and enforce byte/line bounds. Missing or changed source shows no text; it does not erase saved analytical records.

`project_browsing.py` owns transient source-connection and project-document contracts. Explicit same-origin source connection selects a directory inside the startup collection boundary and validates a recorded file before changing session access; it is separate from collection/execution authorization. The browser reuses the existing collection picker. A graph activation uses the existing guarded selection path, rather than requiring a second source button. Related README/text documents are current local reading material in a LocalOps ContentPanel, not analyzer inputs or portable report evidence. Their bounded reads bind report, directory and content identities and display literal text without interpreting target HTML. Project-reader failures cannot disable saved evidence navigation.

Saved evidence, verified original source and session drafts are separate authorities:

- Saved reports determine findings, relationships and recorded spans.
- Verified source permits bounded preview and explicit loading of a complete original document.
- An explicitly created draft exists only in browser memory and is labelled **Draft—not analyzed**.
- Explicit copy/download exports draft content; there is no source-save route, automatic draft persistence or automatic reanalysis.

The editor accepts normalized text while Code Analysis retains original-byte identity and newline metadata for explicit export. Dirty-draft and composition guards protect navigation; original-span highlighting is disabled when it would misleadingly annotate changed draft text. Draft/source text does not enter reports or browser history.

## Managed collection and process lifecycle

Opening a report or starting the UI does not scan source or execute target code. Managed collection is implemented: the UI's configured collection root confines planning and launch, each job requires confirmation, and profiling additionally requires execution acknowledgment. Preview-only sessions disable launch capability.

Static collection may write an owned evidence workspace but never imports or executes target modules. Ruby collection can invoke its bounded parser process. Coverage/profiling are separate explicit execution workflows with the operator's permissions; they are not sandboxes. See the [provider contract](provider-contract.md).

Collection owns validated commands, captured output, cancellation/reconnection, lifecycle states and exact-result opening. Runtime process services use bounded capture and shutdown with process identity checks. Unavailable telemetry, detached descendants, failed cleanup and incomplete scope remain explicit; a supplied sidecar alone is not independent process proof.

`web_launcher.py` owns local launch/session records, compatible-session reuse, readiness and authenticated stop. Background launches use the analyzer interpreter in isolated mode with argument arrays and private logs. Reuse checks roots, inputs, interpreter/environment and source/execution permissions, not just a listening port.

Closing or reloading a tab does not stop a job or server. Shutdown is authenticated, serialized with launch admission and respects cancellation consent; embedded owners may declare shutdown unavailable. Session drafts must be exported before shutdown. Private process receipts and logs are not portable analytical evidence.

After authenticated shutdown acceptance, `session.js` replaces the initiating page with a noninteractive terminal screen and attempts normal `window.close()`. Application-owned teardown aborts pending view/handoff requests and prevents subsequent rendering, requests and history handlers from reviving the UI. Browser refusal leaves the terminal screen, not a live-looking dashboard. Acceptance is bound to the same server instance and is not a claim that asynchronous server/job cleanup has finished. Failed/cancelled shutdown does not enter this terminal state. No toolkit widget or browser-automation permission is required.

## Saved-only exports and extension rules

Summary, query, comparison, static HTML and projection exports read validated saved evidence without rescanning source or launching workloads. Graphviz is an optional renderer; missing or failed rendering remains an explicit error. Source preview is a separately authorized action, not an export prerequisite.

Extend the owning layer, preserve shared schemas and evidence identities, and add focused tests before widening a claim. Do not infer runtime behavior from syntax, promote display grouping into recorded ownership, silently install optional tools or turn provider failure into empty success.

See [development guidance](development.md), the [UI guide](ui-guide.md), [beta limitations](beta.md) and the [roadmap](roadmap.md) for validation, user workflows and planned changes. Automated qualification remains distinct from human usability acceptance.
