# JavaScript refactoring review packet

- Projection: `code-analysis-projection@1.0.0`
- Source: `code-analysis-report@1.0.0`
- Source SHA-256: `1d73ebe4f9fb36defdece2f88700ac55e5a301f57a68c803e562c658766ef2ea`
- Completeness: `complete`
- Records: 1
- Truncated: `false`

| Duplicate ID | Left subject | Right subject | Matched statements | Behavior differences | Divergence reasons | Owner state | Owner subject | Owner reason | Caller/test links | Limitations |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| duplicate-5f2c55a7148c0fea1242 | subject-8a8904dbf16de495a5e6 | subject-ed2f62b844893bbb2fdd | 7 | behavior-difference-0d1f47e55d2cb53984e9 |  | suggested | subject-ed2f62b844893bbb2fdd | one-way resolved local import direction supports the canonical owner | 1 | syntax evidence cannot establish semantic equivalence |

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
- JavaScript analysis is syntax-only; dynamic dispatch, package export maps, bundler aliases, and framework semantics remain unresolved.
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
