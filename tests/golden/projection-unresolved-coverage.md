# Unresolved evidence coverage

- Projection: `code-analysis-projection@1.0.0`
- Source: `code-analysis-report@1.0.0`
- Source SHA-256: `03ed868a88e07238bba473aa5b1d1ee972f01ef8211541b2a0666b2b7bf39eb1`
- Completeness: `partial`
- Records: 16
- Truncated: `false`

| Record type | Identity | State | Operation | Reason | Unresolved | Truncated | Evidence path | Evidence line |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| section | clones | complete |  |  | 0 | 0 |  | 0 |
| section | cohesion-refactoring | complete |  |  | 0 | 0 |  | 0 |
| section | contracts | complete |  |  | 0 | 0 |  | 0 |
| section | coverage | disabled |  |  | 0 | 0 |  | 0 |
| section | data-structures | complete |  |  | 0 | 0 |  | 0 |
| section | dictionary-shapes | partial |  |  | 1 | 0 |  | 0 |
| section | function-commonality | complete |  |  | 0 | 0 |  | 0 |
| section | generation-history | disabled |  |  | 0 | 0 |  | 0 |
| section | git-history | disabled |  |  | 0 | 0 |  | 0 |
| section | interfaces | complete |  |  | 0 | 0 |  | 0 |
| section | r1-static-analysis | complete |  |  | 0 | 0 |  | 0 |
| section | state-side-effects | complete |  |  | 0 | 0 |  | 0 |
| section | static-call-occurrences | complete |  |  | 0 | 0 |  | 0 |
| section | test-impact | unavailable |  |  | 0 | 0 |  | 0 |
| section | validation | complete |  |  | 0 | 0 |  | 0 |
| unresolved-edge | unresolved-fdf67320704e4cc6a455 | partial | dictionary-argument | call target or parameter is not exactly resolved locally | 1 | 0 | package/consumer.py | 3 |

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
- Unresolved dictionary evidence constrains report completeness.
- Validation-relative mutation timing and data-structure ownership remain unavailable.
- runtime-binding-is-not-established-by-static-call-evidence
- static-call-evidence-is-syntactic-and-does-not-establish-runtime-dispatch
