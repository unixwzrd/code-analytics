# Report and diagram catalog

## Synchronized code browser

The implemented [current scope and roadmap](roadmap.md) combines saved entity and relationship views into a synchronized outline, focused map and source pane. Containment, imports, static calls, runtime calls and data flow remain separately labelled; default graph bounds stay at 100 nodes/250 edges with omissions disclosed. No source rescan is needed for analytical maps or exports. Verified original source is a separately authorized transient view, and session drafts are neither analytical records nor report/projection artifacts. These controls do not add projection names. The [current scope and roadmap](roadmap.md) tracks planned guidance, tutorials and storage management around these delivered views, not new analytical report types.

`static-call-sites` answers where invocation syntax was recorded, with exact portable spans, language, scope/evaluation context and conditional/deferred/unknown flags. Unknown callee spellings and navigation anchors remain null. `static-call-graph` shows source-file containment of those distinct sites; it does not infer callee targets, runtime caller relationships or execution order. Both views are built from saved JSON; tables support JSON/TSV/Markdown and the graph supports JSON/Markdown/DOT/Mermaid plus optional Graphviz SVG/PNG. Source partiality and display omissions remain explicit. The prepared-index and UI integration is tracked in the [current scope and roadmap](roadmap.md); the existence of these builders alone does not establish browser acceptance.

`lexical-call-graph` displays unique callee names recorded in function profiles. A unique exact same-language subject-name match links to that saved subject; unmatched or ambiguous names remain caller-scoped nodes without invented source navigation. Edges do not carry call-site spans, occurrence counts or runtime dispatch proof. Declaration evidence is labeled as such; aggregate unresolved counts describe language-selected profiles before neighborhood/display bounds, with provider differences disclosed. Web Explore and terminal Static views → Graphs support saved-only language/focus/grouping/bounds, navigation where recorded, export and Web handoff. Missing function profiles are unavailable, not an empty successful graph. Rich call-site collection and static/runtime call-edge reconciliation remain separate work.

Saved structure diagrams are available as `structure-lifecycle-graph` (recorded roles and observed dictionary states without inferred transitions) and `conversion-graph` (actual saved conversion/serialization sites, preserving repeated sites). They support the common bounded graph selection and JSON/Markdown/DOT/Mermaid/HTML renderers plus optional Graphviz SVG/PNG. The existing `structure-lifecycle` and `conversion-churn` tables remain available. Neither graph claims temporal execution order, runtime ownership or successful conversion; lifecycle prose remains in the table instead of becoming fabricated edges.

`runtime query REPORT --section categories|correlations|process|lifecycle|steps` provides typed saved-only JSON, TSV and Markdown views with canonical source identity, bounds and omission counts. Default bound: 500 records. Source partiality and truncation return `ENODATA`. General static query/export selector convergence and true static call-edge overlays remain separate unfinished gates.

The Phase 4P runtime command family additionally produces `runtime-summary.json` as authority; Markdown summary; functions/callers/callees TSV; category/identity/diagnostic JSON; bounded top-down DOT/SVG/PNG callgraphs; hotspot SVG; and a hash-bound subject overlay SVG. `manifest.json` records every artifact, missing renderer, and retained/omitted graph scope. These runtime views operate from saved JSON, outside the static projection-name registry. Full runtime registry/query integration and static call-edge overlays remain outstanding; see [current scope and roadmap](roadmap.md).

Status: implementation plan with `code-analysis-projection@1.0.0`, deterministic JSON, and report-only health/module/symbol/finding/hierarchy/complexity/dependency overview/neighborhood/SCC/canonical-cycle/contract inventory/boundary table and graph projections implemented. Availability is stated per view; inclusion here does not imply that another analyzer, projection builder, or renderer is already implemented.

Code Analysis Suite should provide multiple views of the same validated evidence for developers, coding agents, CI, architecture reviews, refactoring work, and system documentation. The versioned JSON report remains authoritative. Tables, review packets, DOT, Mermaid, Markdown, SVG, PNG, static HTML, and interactive views are projections and must not require another source scan.

## Design rules

- Commonality omission counts carry exact-versus-lower-bound certainty. `commonality-admission` exposes a Count kind column; `signal-quality` states when the remaining total is unknown. A bounded prefix must never be presented as an exact census of omitted pairs.
- Every projection answers a named engineering question. A diagram is not generated merely because nodes and edges exist.
- Public, persisted, projection, query, and report records use strict frozen Pydantic v2 models. Internal aggregation and layout inputs use frozen slotted dataclasses.
- Every projection binds the source report schema/version and SHA-256, projection schema/version, configuration, filters, stable ordering, completeness, limitations, and truncation.
- Findings remain evidence for review. No table, score, cluster, or diagram authorizes combining, extracting, rewriting, or deleting code.
- Refactoring views include supporting evidence, counter-evidence, affected boundaries, unresolved behavior, and likely validation needs.
- Portable artifacts contain logical paths and stable identities, never host paths, temporary paths, environment prefixes, or repository-external absolute paths.
- Large graphs use bounded overview, cluster, neighborhood, path, and cycle projections. The default must not be an unreadable whole-repository hairball.
- A missing renderer produces an unavailable rendering state, not an empty successful graph. Canonical projection JSON remains usable.
- Renderers escape labels and URLs for their target format. Source text is excluded from labels by default and is available only through bounded evidence references.

## Projection families

Language selection in ownership/alias and structure-lifecycle graphs matches the recorded structure owner's language and retains that structure's related subjects. Dictionary-flow and conversion graphs match either endpoint language and retain both endpoints; selecting one language does not sever a recorded cross-language relationship. Filters apply before exact-node focus and display bounds. A language selection with no matching structures/flows is explicitly unavailable rather than returning unrelated-language records or a successful empty graph. These shared saved-report rules apply to CLI/Web projections and prepared terminal graph requests.

Prepared terminal indexes now retain ownership/alias flow, structure lifecycle, conversion and dictionary-flow graphs as bounded individual records. **Static views → Graphs** provides exact-node neighborhoods, typed node/edge inspection and shared graph exports without the source tree or report file. **Graph filters** / Ctrl+L stages multiple language choices and None/Module/Package collapse; Apply loads the selected scope using prepared membership and exact-owner metadata. Individual node details offer **Navigate recorded spans** with 25-row paging; source preview is separately enabled and hash-checked. Group details explain their display-only identity and offer no invented source span. Unmapped nodes and edges receive no inferred source location. Preparation availability and omissions are distinct from intentional focus/grouping and display truncation; source failure/partiality is retained. Grouping reads the focused input within explicit byte budgets and can require a narrower focus.

The projection envelope is implemented as `code-analysis-projection@1.0.0`. Concrete Pydantic models include declared records rather than arbitrary payload dictionaries:

- `TableProjection`: typed columns, typed rows, stable sort, grouping, totals, and truncation.
- `GraphProjection`: typed nodes and edges, direction, clusters, metrics, legend, and pruning explanation.
- Matrix views use a strict sparse `MatrixProjection` with bounded row/column identities and evidence-linked cells; unbounded dense materialization is prohibited.
- `HierarchyProjection`: parent/child records for package, symbol, ownership, and structure trees.
- `SequenceProjection`: participants and ordered events for lifecycle and producer/consumer paths.
- `StateProjection`: states, transitions, guards, validation, and terminal/error states.
- `ComparisonProjection`: compatible baseline/candidate identities, added/removed/changed records, deltas, and compatibility diagnostics.
- `ReviewPacketProjection`: bounded findings, evidence, counter-evidence, impacted identities, tests/runtime evidence when available, limitations, and next review questions.

TSV is produced only from declared scalar table columns. DOT and Mermaid currently render `GraphProjection` and `HierarchyProjection`; sequence and state renderers remain planned. SVG and PNG are renderer outputs from DOT or Mermaid. Markdown and HTML may compose several validated projections but cannot reinterpret severity, confidence, completeness, or authorization.

## Foundation and architecture views

| View | Engineering question | Canonical projection | Useful renderings | Phase |
| --- | --- | --- | --- | --- |
| Analysis health and provenance | Can this evidence be trusted and what was unavailable, partial, malformed, or truncated? | table/review packet | JSON, Markdown, TSV | R1/P2 |
| Repository overview | What packages, modules, symbols, LOC, findings, and analyzer coverage exist? | table/hierarchy | JSON, Markdown, TSV, Mermaid | R1/P2 |
| Package and module tree | Where is code physically and logically organized? | hierarchy | JSON, Markdown tree, Mermaid | R1/P2 |
| Symbol inventory | Where are classes, functions, methods, nested symbols, parameters, returns, and decorators? | table/hierarchy | JSON, Markdown, TSV | R1/P2 |
| Symbol declaration graph (`symbol-graph`) | Which symbols are recorded in each module? Exact membership only, not calls or inferred lexical parentage. | graph | JSON, Markdown, DOT, Mermaid, SVG, PNG, HTML | P5 |
| Complexity hotspots | Which symbols exceed structural thresholds and why? | table/review packet | JSON, Markdown, TSV | R1 |
| Complexity distribution | Is complexity concentrated or broadly distributed? | table/matrix | JSON, TSV, static HTML chart | P2/P5 |
| Module coupling | Which modules have high fan-in, fan-out, or instability? | table/graph | JSON, Markdown, TSV, DOT, Mermaid | R1/P2 |
| Dependency overview | What are the principal package/module relationships? | graph | JSON, DOT, Mermaid, SVG, PNG | R1/P2 |
| Dependency neighborhood | What directly depends on or is depended on by one selected identity? | graph/review packet | JSON, Markdown, DOT, Mermaid | P2 |
| Dependency path | How can one module or symbol reach another? | graph/sequence | JSON, Markdown, DOT, Mermaid | P2 |
| SCC and cycle report | Which dependency loops exist and what edges form each canonical cycle? | table/graph | JSON, Markdown, TSV, DOT, Mermaid | R1/P2 |
| Layer-policy report | Which edges violate declared architecture boundaries? | table/graph/review packet | JSON, Markdown, TSV, DOT, Mermaid | P3 |
| Provider agreement | Where do native, Ruff, Pyreverse, Pydeps, or later providers agree or disagree? | matrix/review packet | JSON, Markdown, TSV | P3 |

Dependency diagrams should support package, module, class, and resolved-symbol levels when corresponding evidence exists. Overview graphs default to packages or bounded high-signal modules. Focused module/symbol graphs use explicit selectors, depth, edge-kind filters, and node limits. Runtime, type-checking, optional, lazy, and dynamic/unresolved edges remain visually distinct.

## Data-contract and data-structure views

| View | Engineering question | Canonical projection | Useful renderings | Phase |
| --- | --- | --- | --- | --- |
| Contract inventory | Which Pydantic models, dataclasses, `TypedDict`s, protocols, enums, mappings, and serialization contracts exist? | table/hierarchy | JSON, Markdown, TSV, Mermaid | P2-C (table implemented) |
| Contract boundary map | Which contracts cross persistence, API, subsystem, module, or test boundaries? | graph/matrix | JSON, Markdown, TSV, DOT, Mermaid | P2-C (table and graph implemented) |
| Contract boundary matrix | Which contracts have multiple evidenced producer, consumer, persistence, API, or subsystem boundary roles? | matrix/table | JSON, Markdown, TSV | P2-C (table implemented) |
| Dictionary-shape report | What keys, types, defaults, requiredness, coercions, conflicts, and unknown-field behavior were observed? | table/review packet | JSON, Markdown, TSV | P2-D |
| Shape variants and states | Which discriminator values, lifecycle states, and conditional fields distinguish non-flattened dictionary variants? | table/state | JSON, Markdown, TSV, Mermaid | P2-D/P2-F |
| Producer/consumer flow | Where is a contract or shape produced, transformed, validated, serialized, and consumed? | graph/sequence | JSON, Markdown, DOT, Mermaid | P2-D |
| Conversion graph | Where do models, dataclasses, dictionaries, rows, tuples, JSON, or binary records convert between representations? | graph | JSON, TSV, DOT, Mermaid | P2-D/P2-E |
| Validation-sprawl report | Which structural validations repeat or diverge, and which operational checks must remain in their owner? | table/matrix/review packet | JSON, Markdown, TSV | P2-E |
| Conversion churn | Which subjects repeatedly convert or revalidate compatible representations? | table/graph | JSON, Markdown, TSV, DOT, Mermaid | P2-E/P2-F |
| Untyped-boundary blast radius | Which consumers and persisted/API/subsystem boundaries depend on intentionally or accidentally untyped values? | graph/matrix | JSON, Markdown, TSV, DOT, Mermaid | P2-F |
| Structure topology | How is a significant graph, tree, queue, cache, registry, index, journal, manifest, event row, or tensor metadata structure composed? | graph/hierarchy | JSON, DOT, Mermaid, SVG, PNG | P2-E |
| Structure lifecycle | How is a structure constructed, owned, mutated, copied, persisted, shared, cleaned up, or leaked? | sequence/state | JSON, Markdown, Mermaid | P2-E |
| Ownership and alias map | Which mutable values cross module, instance, thread, request, process, or persistence owners? | graph | JSON, DOT, Mermaid | P2-E |
| Unresolved-evidence coverage | Which subjects, flows, shapes, and findings are affected by unresolved, ambiguous, failed, or truncated evidence? | table/review packet | JSON, Markdown, TSV | P2-F |
| Schema/version state machine | Which schema states, migrations, compatibility paths, and rejected transitions exist? | state | JSON, Markdown, Mermaid | P2-E |
| Growth and cleanup review | Which collections may grow, what bounds/eviction exist, and where is cleanup enforced? | table/state/review packet | JSON, Markdown, TSV, Mermaid | P2-E |

Dictionary and contract diagrams distinguish exact, resolved, inferred, and heuristic edges. Ambiguous names, unresolved dynamic calls, recursion limits, and truncated paths remain visible. A classification such as “Pydantic candidate” or “dataclass candidate” is a typed evidence record with counter-evidence, not a migration instruction.

## Refactoring and change views

| View | Engineering question | Canonical projection | Useful renderings | Phase |
| --- | --- | --- | --- | --- |
| Orchestration review | Is a large function coordinating cohesive responsibilities or mixing unrelated ownership? | review packet | JSON, Markdown | R1/P2 |
| Subject findings | Which findings, severities, rules, evidence, and proposals converge on each analyzed symbol within an exact subject or dotted-module scope? | filtered table | JSON, Markdown, TSV | R1/P3 |
| Proposal admission | Which responsibility-derived proposal candidates were admitted, rejected as call-resolution-only, or omitted by the proposal bound? | funnel/table | JSON, Markdown, TSV | P3 |
| Actionable candidate worklist | Which stable candidates should a maintainer review after explicit filters, decisions, gates, costs, behavior, and qualification state? | table | JSON, Markdown, TSV | P4R |
| Abstraction cost breakdown | What lower, likely, and upper deletion, helper, migration, and net production LOC ranges support a candidate? | table | JSON, Markdown, TSV | P4R |
| Existing-owner alternatives | Which existing semantic owner, primitive, or domain module can own shared behavior, and why were alternatives rejected? | table | JSON, Markdown, TSV | P4R |
| Candidate behavioral differences | Which exact behavior events or proven ordering constraints block or constrain a candidate? | table | JSON, Markdown, TSV | P4R |
| Cross-module duplication | Which clone clusters span distinct logical modules while preserving their original evidence? | table | JSON, Markdown, TSV | P4R |
| Internal contract risks | Which internal dictionary boundaries have mutable, configuration, cross-subsystem, or unresolved ownership? | table | JSON, Markdown, TSV | P4R |
| Producer/consumer schema drift | Which statically correlated backend and frontend payload fields agree or diverge? | table | JSON, Markdown, TSV | P4R |
| Frontend/backend interface mismatches | Which exact route, method, field, null, status, error, event, retry, or lifecycle assumptions differ? | table | JSON, Markdown, TSV | P4R |
| Candidate qualification | Which direct, likely-path, coverage, positive, adversarial, parity, rollback, failure, and canonical-discovery requirements are evidenced, missing, or unresolved? | table | JSON, Markdown, TSV | P4R |
| Ownership and contract flow | Which statically correlated producer and consumer endpoints participate in a contract flow? | graph | JSON, DOT, Mermaid, SVG, PNG | P4R |
| Candidate architecture changes | What dependency-edge, SCC, and cycle deltas are predicted for a candidate? | table/graph | JSON, Markdown, TSV, DOT, Mermaid | P4R |
| Domain regression summary | Which duplication, ownership, internal-contract, growth, candidate, edge, SCC, or cycle observations changed between compatible saved reports? | comparison/table | JSON, Markdown, TSV | P4R |
| Semantic nomination audit | Which locally nominated pairs were supported or rejected by deterministic evidence, and what bounded evidence was omitted? | table/audit packet | JSON | P4R |
| Ruby inventory and duplicates | Which `.rb` modules, classes, methods, literal dependencies, calls, exception/effect facts, and bounded duplicate methods were observed by Prism? | table/graph | JSON, Markdown, TSV, DOT, Mermaid | P4R |
| Module responsibility report | Do symbols, contracts, imports, and state indicate one or several ownership clusters? | matrix/review packet | JSON, Markdown, TSV | P3 |
| Clone clusters | Which exact, token, AST, statement, validation, or serialization fragments repeat? | table/graph | JSON, Markdown, TSV, DOT, Mermaid | P3 |
| Function commonality | Which functions share inputs, outputs, calls, state, exceptions, and contract operations? | matrix | JSON, Markdown, TSV, static HTML | P3 |
| Extraction candidate | Does a cohesive cluster have a plausible owner and improve dependency direction? | review packet/graph | JSON, Markdown, DOT, Mermaid | P3 |
| Combine-module candidate | Would combining modules reduce a false boundary without producing excessive coupling or ownership ambiguity? | review packet/comparison | JSON, Markdown, DOT, Mermaid | P3 |
| Before/after architecture estimate | How would a proposed extraction or combination change dependencies, cycles, fan-in/out, and layer rules? | comparison/graph | JSON, Markdown, TSV, DOT, Mermaid | P3 |
| Test-impact map | Which tests, fixtures, patches, coverage contexts, and qualification paths protect affected symbols? | graph/table | JSON, Markdown, TSV, DOT, Mermaid | P4 |
| Git change coupling | Which files or symbols change together after excluding bulk or formatting-only changes? | matrix/graph | JSON, Markdown, TSV, DOT, Mermaid | P4 |
| Refactor candidate packet | What evidence, counter-evidence, affected contracts, tests, runtime paths, risks, and unresolved questions belong in review? | review packet | JSON, Markdown, HTML | P3/P4 |
| Refactor shortlist | Which proposal should be reviewed first based on explicit state, failed/unresolved gates, and bounded affected scope? | table | JSON, Markdown, TSV | P3/P4 |
| Baseline/candidate architecture diff | Did a change add/remove/change symbols, edges, cycles, findings, contracts, shapes, or policy violations, and how much unchanged inventory was omitted from the default view? | comparison | JSON, Markdown, TSV, DOT, Mermaid, HTML | P3/P5 |
| Compatible P2 evidence delta | Between compatible reports, which contracts, boundaries, variants, validations, conversions, unresolved records, and findings changed? | comparison | JSON, Markdown, TSV | P2-F |
| Bounded refactoring review packet | What supporting and counter-evidence, affected identities, limitations, completeness, and validation needs should a developer or agent review? | review packet | JSON, Markdown | P2-F |

A combine-module candidate requires more than a cycle or bidirectional imports. It should include responsibility overlap, contract ownership, public API compatibility, state ownership, test impact, dependency changes, and counter-evidence for preserving separate modules. Extraction candidates likewise require cohesion and ownership evidence and must identify potential “utility module” coupling risk.

## JavaScript duplicate-behavior views

These implemented Phase 4J views use the same strict projection envelopes and saved authoritative report as Python views. Source language, syntax mode, module system, parser provenance, completeness, and unresolved dynamic behavior remain visible in every applicable result.

| View | Engineering question | Canonical projection | Useful renderings | Phase |
| --- | --- | --- | --- | --- |
| Cross-module duplicate functions | Which JavaScript functions implement substantially the same operation across modules? | sparse matrix/table | JSON, Markdown, TSV | P4J |
| Clone membership | Which exact, token, structural, statement, control/call, validation, serialization, retry, or lifecycle clusters contain a selected function? | table/graph | JSON, Markdown, TSV, DOT, Mermaid | P4J |
| Normalized structure comparison | Which source-bound structural events match without retaining raw source text? | sequence/comparison | JSON, Markdown, TSV | P4J |
| Behavioral differences | Which branches, calls, constants, defaults, exceptions, state accesses, and side effects differ? | comparison/table | JSON, Markdown, TSV | P4J |
| Ownership and consumers | Which module/package appears to own the behavior, and which callers/tests depend on each copy? | graph/review packet | JSON, Markdown, DOT, Mermaid | P4J |
| Divergence reasons | Which typed validation, constant, exception, effect, lifecycle, caller/test, or unresolved-ownership asymmetries merit review? | table | JSON, Markdown, TSV | P4J |
| Canonical-owner evidence | Does dependency, caller, contract, state, and side-effect evidence support one owner, or is ownership unresolved? | review packet/graph | JSON, Markdown, DOT, Mermaid | P4J |
| JavaScript refactor packet | What matched evidence, differences, affected consumers/tests, blockers, limitations, and validation needs belong in review? | review packet | JSON, Markdown | P4J |

No view uses an opaque similarity, quality, risk, or refactoring score. Names and comments/docstrings cannot originate a candidate. Cross-language comparisons, embeddings, framework semantics, and UI-owned interpretation remain deferred. All views must render from saved JSON without a JavaScript parser, Node/npm, or source access.

## Runtime and performance views

These views are Phase 4 and require explicit runtime authorization. Static scans never collect them implicitly.

| View | Engineering question | Canonical projection | Useful renderings |
| --- | --- | --- | --- |
| Profile hotspots | Where did the authorized workload spend total and cumulative time? | table | JSON, Markdown, TSV |
| Caller/callee report | Which functions call or are called by a selected hotspot? | table/graph | JSON, Markdown, TSV, DOT, Mermaid |
| Dynamic call graph | What call relationships were observed under the bounded workload? | graph | JSON, DOT, Mermaid, SVG, PNG |
| Repetition and variance | Which calls repeat and how stable are selected measurements across compatible runs? | table/matrix | JSON, Markdown, TSV, HTML chart |
| Baseline/candidate profile diff | What absolute and relative timing/call-count changes exist between compatible profiles? | comparison | JSON, Markdown, TSV, HTML |
| Static/runtime overlay | Which runtime hotspots coincide with complexity, coupling, contract, clone, or ownership findings? | graph/review packet | JSON, Markdown, DOT, Mermaid, HTML |

Profile comparisons fail closed when workload, interpreter, configuration, repetition, or source identity is incompatible. A single profile is observation, not proof of an optimization win.

## Documentation artifacts

Selected diagrams and tables may be published as system documentation. Documentation output uses the same projection JSON with a deterministic documentation rendering profile:

- stable title, caption, legend, logical-root label, schema identity, and source-report digest
- bounded node/row limits and a visible pruning or truncation note
- portable relative links when source documentation is included
- light and dark compatible palettes where the renderer supports them
- accessible text alternative in Markdown or TSV for every graphical view
- `.mmd` or `.dot` source retained beside SVG/PNG when the documentation build accepts it
- an artifact manifest containing input projection hashes and output hashes
- no timestamps unless explicitly requested as local publication metadata
- no absolute paths, environment values, source secrets, or unrestricted source excerpts

Generated documentation artifacts are not automatically added to Git. The user selects an output directory and decides whether generated sources or rendered files belong in project documentation. Re-rendering documentation from retained JSON must not require the analyzed repository to remain present.

## Recommended report bundles

### Quick architecture review

- analysis health and provenance
- repository overview
- top complexity and orchestration candidates
- module coupling table
- bounded dependency overview
- SCC/cycle report

### Data-contract review

- contract inventory and boundary map
- dictionary-shape report
- producer/consumer and conversion graphs
- validation-sprawl matrix
- structure topology and lifecycle
- ownership, alias, growth, and cleanup review

### Refactoring review packet

- selected candidate with supporting and counter-evidence
- responsibility and dependency neighborhood
- affected contracts and data flows
- clone/commonality evidence when available
- test and Git impact when available
- compatible before/after architecture estimate
- unresolved behavior, completeness, limitations, and suggested validation commands

### Documentation bundle

- package/module hierarchy
- bounded architecture overview
- selected subsystem dependency views
- contract boundary and producer/consumer diagrams
- significant structure topology/lifecycle diagrams
- Markdown captions and TSV text alternatives
- projection and rendered-artifact manifest

## Delivery order

1. R1/P2 report-only projections: health, inventory, module metrics, dependency graph, SCC/cycles, complexity, contracts, shapes, and flows.
2. P2-F deterministic JSON, TSV, DOT, and Mermaid exporters operating only on retained reports.
3. P3 clone, responsibility, extraction/combination, and architecture-estimate projections.
4. P4 test, Git, runtime, profile, compatible comparison, DOT/Mermaid, and optional Graphviz SVG/PNG projections — implemented.
5. P4J language-aware JavaScript duplicate, ownership, divergence, caller/test, and review-packet projections over the same backend models — implemented and internally qualified.
6. P5 bounded read-only WebUI overview, modules, findings, candidates, providers, and report comparison — prototype implemented. Static HTML, rendered documentation bundles, TUI navigation, source evidence, runtime profiles, and large-graph interaction remain deferred.

This order keeps analysis semantics in the service and report models, presentation in projections/renderers, and source mutation deferred to separately authorized Phase 6 codemods.
