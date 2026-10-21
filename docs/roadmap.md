# Code Analysis Suite roadmap

## Current beta scope

The suite provides static Python, JavaScript/JSX and Ruby front ends, strict deterministic JSON reports, saved-report queries/comparisons/projections, optional Python runtime evidence, a loopback Web UI and an optional terminal UI. Language support is capability-scoped, not feature parity. See [beta scope and limitations](beta.md) and the [provider contract](provider-contract.md).

All three languages are peer packages under `src/code_analysis/languages/`, sharing `LanguageFrontend` and the normalized immutable fact boundary. Parser syntax belongs to adapters; report policy, queries, projections and execution permissions remain application-owned. Unsupported capabilities must not manufacture empty success.

The [Fawlty Towers tutorials](../tutorials/README.md) support saved-evidence investigation and first-use feedback. The [UI testing guide](ui-testing.md) defines automated gates and a separate human walkthrough. Passing tests does not establish comprehension or release readiness.

The first post-public-beta browsing refinement adds one-activation graph-to-source navigation, explicitly confirmed in-app source-folder connection, and related README/local-text reading without abandoning the investigation. These reuse the existing LocalOps panel and collection picker. They do not provide persistent project profiles, a tutorial catalog, rendered arbitrary Markdown/HTML, unrestricted file browsing or a tutorial execution engine.

Local collection opening now restores the source location from its owned JSON metadata within the session's collection boundary; Overview explains missing/moved/invalid locations before investigation. Finding investigations open source-first, with concise context and full evidence available on demand rather than an obligatory evidence modal. Source hashes, saved-evidence partiality and authorization boundaries remain unchanged.

## Next code-investigation usability improvements

Recent walkthrough feedback puts source-centered investigation ahead of persistent project setup. The source workspace, bounded saved relationships, finding markers and version/issue-report controls are implemented; their integrated qualification and renewed human walkthrough remain separate gates. Broader per-view navigation refinement remains ongoing:

1. Give verified source and session drafts enough desktop space for at least 80 actual monospace columns, with explicit source-focused layout and collapsible neighboring panes rather than squeezed editors.
2. Reveal recorded problem ranges and show concise, accessible finding annotations beside the code. Reusable editor annotation behavior belongs in LocalOps UI Kit; exact toolkit delivery and independent integration qualification remain prerequisites. Edited drafts must not display original findings as newly analyzed results.
3. Link dependency/analytical rows directly to recorded entities and source, retaining return context. Show severity only when backed by a retained finding; fan-in/fan-out alone does not establish a defect.
4. Make callers, callees and existing structure/dictionary lineage discoverable. Keep lexical/name-matched, resolved static, unresolved and runtime-observed relationships distinct; saved evidence must not imply complete dispatch or general variable tracing.
5. Display the running application version persistently and add a nearby GitHub issue-reporting link with a repository-owned bug-report form/template. Distinguish the running version from older report producers; offer only reviewed, editable, privacy-conscious diagnostic prefills, never automatic source/report/log uploads.

These views use retained JSON/profile evidence without rescanning source. Verified source reads remain separately hash-bound and confined. Desktop remains primary, tablet secondary; human walkthrough acceptance stays separate from automated qualification. Broader variable def/use analysis requires a separate design, not a claim that cProfile records variable values.

## Other user-workflow improvements

1. Review first-use feedback: begin unaided, understand unavailable evidence, investigate/return with context, and use Help without losing work.
2. Add explicit project selection and local profiles with clear source target, browse boundary, trusted interpreter and evidence destination. Preserve active jobs/drafts and single-workspace session ownership.
3. Discover Python entry points from bounded metadata/syntax without importing or executing the project. A console-script declaration is not automatically equivalent to `python -m`.
4. Add optional in-app tutorial entry points and concise evidence-backed next actions, not an opaque recommendation score.
5. Add storage inventory and preview/confirmed cleanup over owned workspace policy, protecting pins, active jobs, referenced evidence and recoverability.

These are planned capabilities, not delivered controls. Saved settings must not restore execution permission or widen filesystem access. Generic widgets/accessibility belong in LocalOps UI Kit; application content, workflow and authorization belong here.

## Analysis quality and runtime follow-ups

- Address tutorial capability observations, including JavaScript aggregate graphs/findings and Ruby metrics, clones, state and test discovery.
- Expand substantive candidate examples and test/coverage qualification without weakening ownership, behavior, cost or graph gates.
- Investigate Python script profiling import context, verified thread/scoped capture and source/runtime reconciliation. Missing times remain null; frame duration is not measured native/device work.
- Measure dependency-aware incremental collection against clean full scans before exposing it. Presentation changes over saved JSON already need no rescan; source changes currently need a fresh report assembly.
- Qualify more interpreter/browser/platform combinations. Desktop remains primary and tablet secondary.

## Future language adapters

Rust, Go, COBOL and TypeScript/TSX are candidates for separate adapters, not supported inputs or promised releases. Each needs a reviewed parser/license, static non-execution boundary, normalized facts, deterministic identities/spans, capability declarations, malformed/unavailable behavior, synthetic tests and retained-report interoperability. Reading saved evidence must not require its parser/runtime.

Cross-language clones, framework-specific semantics and semantic nomination need independent false-positive controls. Names or embeddings alone cannot authorize refactoring.

## Deliberately separate work

Live tracing, direct-network serving, AI/IDE integrations, source writes, codemods and automatic refactoring require separate scope and authorization. AI must not become a required dependency or receive private source by default. Source stays read-only; candidates are review packets.

## Documentation and release policy

Start with the [README](../README.md), [UI guide](ui-guide.md), [CLI reference](cli.md), [examples](cli-examples.md) and [report catalog](report-and-diagram-catalog.md). Technical readers can use [architecture](architecture.md), [report schema](report-schema.md), [provider contract](provider-contract.md) and [development gates](development.md).

Internal coordination, machine-specific qualification records and original development history are archived privately. Public-beta publication follows review of sanitized history, package artifacts and known limitations. No timeline, stable compatibility guarantee or automatic package publication is implied.
