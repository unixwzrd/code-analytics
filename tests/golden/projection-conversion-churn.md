# Evidenced conversion and serialization sites

- Projection: `code-analysis-projection@1.0.0`
- Source: `code-analysis-report@1.0.0`
- Source SHA-256: `b07b04d68ce4b1f748d9473765bdd3d744092b890e46d4f33c94b76fc44ad775`
- Completeness: `complete`
- Records: 2
- Truncated: `false`

| Structure ID | Owner | Flow ID | Kind | Target | Confidence | Evidence path | Evidence line |
| --- | --- | --- | --- | --- | --- | --- | --- |
| structure-f42fb998549a30e7ca35 | package.state:build:payload | flow-4c3f4c2c0ba0a5e92351 | serialization | json.dumps | exact | package/state.py | 14 |
| structure-f42fb998549a30e7ca35 | package.state:build:payload | flow-567b4cc43a13b393a6e7 | serialization | json.dumps | exact | package/state.py | 15 |

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
