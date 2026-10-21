# Coverage and exact-test binding for proposal subjects

- Projection: `code-analysis-projection@1.0.0`
- Source: `code-analysis-report@1.0.0`
- Source SHA-256: `8a9ea3ac8f72c4b3abfc1d8a1c72f4f099761fa0ec3dcc17c523b979e6495248`
- Completeness: `complete`
- Records: 10
- Truncated: `false`

| Proposal | Affected subject | Evidence state | Coverage observations | Executed test contexts |
| --- | --- | --- | --- | --- |
| refactor-proposal-a3df68f182fb37291540 | package.combine_b:transform | qualification-unresolved | 0 | 0 |
| refactor-proposal-a3df68f182fb37291540 | package.work:third | qualification-unresolved | 0 | 0 |
| refactor-proposal-9b0d4f7972943799b6ed | package.combine_a:transform | qualification-unresolved | 0 | 0 |
| refactor-proposal-9b0d4f7972943799b6ed | package.work:third | qualification-unresolved | 0 | 0 |
| refactor-proposal-9b0d4f7972943799b6ed | package.combine_b:transform | qualification-unresolved | 0 | 0 |
| refactor-proposal-a3df68f182fb37291540 | package.combine_a:transform | qualification-unresolved | 0 | 0 |
| refactor-proposal-a3df68f182fb37291540 | package.work:second | qualification-unresolved | 0 | 0 |
| refactor-proposal-9b0d4f7972943799b6ed | package.work:first | qualification-unresolved | 0 | 0 |
| refactor-proposal-a3df68f182fb37291540 | package.work:first | qualification-unresolved | 0 | 0 |
| refactor-proposal-9b0d4f7972943799b6ed | package.work:second | qualification-unresolved | 0 | 0 |

## Limitations

- Call resolution is bounded to direct local and imported syntax; dynamic dispatch is unresolved.
- Clone evidence is syntax-only; semantic equivalence and runtime behavior are unmeasured.
- Complexity and orchestration classifications are threshold heuristics, not refactor approval.
- Contract inventory is syntax-only and does not resolve producer/consumer call flow.
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
- Validation-relative mutation timing and data-structure ownership remain unavailable.
- runtime-binding-is-not-established-by-static-call-evidence
- static-call-evidence-is-syntactic-and-does-not-establish-runtime-dispatch
