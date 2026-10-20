# Findings grouped by analysis subject

- Projection: `code-analysis-projection@1.0.0`
- Source: `code-analysis-report@1.0.0`
- Source SHA-256: `e4d9d35a0825f29c294a383e70f32dff29cd72365dacea6425e9d37317300eb1`
- Completeness: `complete`
- Records: 6
- Truncated: `false`

| Subject ID | Kind | Module | Logical name | Findings | Errors | Warnings | Info | Category counts | Rule counts | Unique evidence | Affected proposals |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| subject-dc537fbe8077aaa3b39a | symbol | package.combine_a | package.combine_a:transform | 3 | 0 | 2 | 1 | duplication=1, extraction-candidate=1, module-combination=1 | duplication.normalized-ast=1, extraction-candidate=1, module-combination-candidate=1 | 13 | 2 |
| subject-b7e780419d3c0149f3aa | symbol | package.combine_b | package.combine_b:transform | 3 | 0 | 2 | 1 | duplication=1, extraction-candidate=1, module-combination=1 | duplication.normalized-ast=1, extraction-candidate=1, module-combination-candidate=1 | 13 | 2 |
| subject-6e6c378abe407b0f54a7 | symbol | package.work | package.work:first | 3 | 0 | 2 | 1 | duplication=1, extraction-candidate=1, module-combination=1 | duplication.normalized-ast=1, extraction-candidate=1, module-combination-candidate=1 | 13 | 2 |
| subject-7f565e090115d3911cc8 | symbol | package.work | package.work:second | 3 | 0 | 2 | 1 | duplication=1, extraction-candidate=1, module-combination=1 | duplication.normalized-ast=1, extraction-candidate=1, module-combination-candidate=1 | 13 | 2 |
| subject-8c8c85262b5b8d5aa3c4 | symbol | package.work | package.work:third | 3 | 0 | 2 | 1 | duplication=1, extraction-candidate=1, module-combination=1 | duplication.normalized-ast=1, extraction-candidate=1, module-combination-candidate=1 | 13 | 2 |
| subject-f7326b273e0da8dcf15e | symbol | package.domain_bad | package.domain_bad:violates_direction | 1 | 0 | 1 | 0 | architecture-contract=1 | configured-layer-direction=1 | 1 | 0 |

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
