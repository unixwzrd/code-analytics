# Coverage files, lines, branches, and contexts

- Projection: `code-analysis-projection@1.0.0`
- Source: `code-analysis-report@1.0.0`
- Source SHA-256: `963282794570f0f8e0fcfe4b3cdf7b0d5186065823f2ffe8b5efa258e91111ab`
- Completeness: `complete`
- Records: 1
- Truncated: `false`

| Path | Binding | Covered lines | Missing lines | Arcs | Missing arcs | Contexts |
| --- | --- | --- | --- | --- | --- | --- |
| module.py | bound | 2 | 0 | 1 | 0 | 1 |

## Limitations

- Call resolution is bounded to direct local and imported syntax; dynamic dispatch is unresolved.
- Clone evidence is syntax-only; semantic equivalence and runtime behavior are unmeasured.
- Complexity and orchestration classifications are threshold heuristics, not refactor approval.
- Contract inventory is syntax-only and does not resolve producer/consumer call flow.
- Coverage proves only that the supplied workload traversed a line or branch.
- Data-structure topology is syntax-only and limited to evidenced dictionary operations and declarations.
- Dictionary analysis is syntax-only and bounded; dynamic calls and keys remain unresolved.
- Dictionary evidence is descriptive and does not authorize a representation change.
- Dynamic imports, reflection, generated modules, and runtime-only edges are unresolved.
- External graph providers are inventoried separately and are not reconciled in Phase 1 scans.
- Feature profiles describe syntax and do not establish runtime behavior or common ownership.
- Growth indicators are measurement prompts, not performance findings.
- Interface evidence is static and does not transfer authorization or domain semantics.
- Mutable-state and side-effect evidence is syntax-only; runtime ownership and outcomes are unmeasured.
- Names and literals remain significant for token clones and are categorical in AST clones.
- Operational filesystem, path, symlink, authorization, security, and runtime-state validation is excluded.
- Phase 4 test-impact evidence is unavailable.
- Runtime ownership, lock behavior and coverage, persistence success, cleanup effectiveness, and growth remain unmeasured.
- Serialization evidence resolves exact declaration kinds, local annotations, and direct straight-line aliases; control-flow aliases and runtime types remain unresolved.
- Shadowing, dynamic dispatch, cleanup effectiveness, and arbitrary receiver provenance remain unresolved.
- Static graph simulation does not establish runtime behavior or authorize source changes.
- Structural validation currently covers proven dictionary membership, truthiness, and coercion checks.
- Threads, subprocesses, native extensions, and unobserved code may be absent.
- Validation-relative mutation timing and data-structure ownership remain unavailable.
