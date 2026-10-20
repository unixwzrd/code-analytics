# Code Analysis Suite roadmap

## Current beta scope

The suite provides static Python, JavaScript/JSX and Ruby front ends, strict deterministic JSON reports, saved-report queries/comparisons/projections, optional Python runtime evidence, a loopback Web UI and an optional terminal UI. Language support is capability-scoped, not feature parity. See [beta scope and limitations](beta.md) and the [provider contract](provider-contract.md).

All three languages are peer packages under `src/code_analysis/languages/`, sharing `LanguageFrontend` and the normalized immutable fact boundary. Parser syntax belongs to adapters; report policy, queries, projections and execution permissions remain application-owned. Unsupported capabilities must not manufacture empty success.

The [Fawlty Towers tutorials](../tutorials/README.md) support saved-evidence investigation and first-use feedback. The [UI testing guide](ui-testing.md) defines automated gates and a separate human walkthrough. Passing tests does not establish comprehension or release readiness.

The first post-public-beta browsing refinement adds one-activation graph-to-source navigation, explicitly confirmed in-app source-folder connection, and related README/local-text reading without abandoning the investigation. These reuse the existing LocalOps panel and collection picker. They do not provide persistent project profiles, a tutorial catalog, rendered arbitrary Markdown/HTML, unrestricted file browsing or a tutorial execution engine.

## Next user-workflow improvements

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
