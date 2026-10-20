# Tooling and provider provenance

`code-analysis doctor` reports local interpreter and tool provenance. `code-analysis providers` reports availability, not proof that a particular analysis ran. Inspect the saved report's provider states, completeness, diagnostics and limitations for actual collection results. An unavailable tool never becomes successful empty evidence.

## Collection and optional dependencies

- **Python:** the base installation uses the standard-library AST frontend and Python-owned analysis domains. Static scans do not import target modules or execute their tests.
- **JavaScript/JSX:** the explicit `javascript` extra supplies pinned Tree-sitter bindings and grammar. Parsing does not require Node, npm, package scripts or target-code execution. Missing selected parsers produce explicit unavailable/partial evidence.
- **Ruby:** the optional frontend uses an existing configured Ruby/Prism installation and the bundled parser driver. It invokes a bounded external parsing process without loading the target application. Ruby support is narrower than Python support.
- **Coverage and profiling:** Python coverage/cProfile workflows are separate from static scans. Execution requires explicit authorization; offline profile ingestion reads saved artifacts without running the workload. The `runtime` extra supplies optional coverage and process-telemetry dependencies.
- **Semantic nomination:** the separate `semantic` extra requires an explicitly configured existing local model. It does not download a model or replace deterministic evidence with a model's judgment.
- **Presentation:** the `tui` extra enables terminal browsing. Graphviz is an optional external renderer for graphics, not an analysis prerequisite. Browser investigation and retained-report operations do not require language parsers.

No optional provider package, runtime or model is installed implicitly. Availability of a formatter, checker, test runner or other inventoried executable does not mean that `scan` invokes it. See the [provider contract](provider-contract.md) for execution states and exact boundaries.

## Local versus portable provenance

Local diagnostics intentionally include executable paths and versions. Local run manifests and captured streams can also contain sensitive command arguments or physical paths; review them before sharing.

Portable reports retain bounded parser/provider identities, source hashes, configuration identity and explicit result states rather than host installation paths. They can still disclose source-derived names, literals and topology. Report portability is not anonymization or permission to publish private evidence.

For current qualification limits, see [beta scope](beta.md). For contributor setup and validation, see [development guidance](development.md).
