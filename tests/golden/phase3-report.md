# Code Analysis Summary

Completeness: **complete**

Analyzed 6/6 Python files, 6 modules, and 7 callable symbols.

## Stable counts

- Classes: 0
- Functions: 7
- Methods: 0
- Import edges: 5
- Strongly connected components: 0
- Canonical cycles: 0
- Findings: 4
- Dictionary shapes: 5
- Dictionary observations: 10
- Resolved flow edges: 0
- Unresolved edges: 0
- Validation patterns: 0
- Data structures: 5
- Clone clusters: 1
- Function commonalities: 10
- Mutable state resources: 0
- Side effects: 0
- Responsibility clusters: 1
- Refactoring proposals: 2
- Representation candidates: 5
- Contract subjects: 12
- Contracts: 0

## Contract inventory

No statically declared contracts were reported.

## Dictionary shapes

- `package.work:first:dict@5:13` — 1 variants, 1 variant fields, 0 lifecycle states (`shape-016db1feec3b1af1a11f`)
- `package.work:second:dict@12:13` — 1 variants, 1 variant fields, 0 lifecycle states (`shape-110b6e0a6bc182d3039b`)
- `package.work:third:dict@19:13` — 1 variants, 1 variant fields, 0 lifecycle states (`shape-7525bc4f62b52267af81`)
- `package.combine_b:transform:dict@5:13` — 1 variants, 1 variant fields, 0 lifecycle states (`shape-ca36084f8f64a4c9e041`)
- `package.combine_a:transform:dict@6:13` — 1 variants, 1 variant fields, 0 lifecycle states (`shape-ce076f447805f53c83d8`)

## Validation patterns

No structural validation patterns were reported.

## Data structures

- `package.work:third:dict@19:13` — local with 1 readers, 0 writers, and 0 aliases (`structure-27a364d2f23907493ffa`)
- `package.work:first:dict@5:13` — local with 1 readers, 0 writers, and 0 aliases (`structure-a4080b063ec77f192830`)
- `package.work:second:dict@12:13` — local with 1 readers, 0 writers, and 0 aliases (`structure-c77ce2c31fe946fe2604`)
- `package.combine_a:transform:dict@6:13` — local with 1 readers, 0 writers, and 0 aliases (`structure-ee206b1ee47c08fdc003`)
- `package.combine_b:transform:dict@5:13` — local with 1 readers, 0 writers, and 0 aliases (`structure-fe0a5a818b56add93f6a`)

## Representation candidates

- **local-implementation-dictionary** `package.combine_a:transform:dict@6:13` — exact confidence, 0 counter-evidence sites (`candidate-12708f794ce8796fd856`)
- **local-implementation-dictionary** `package.work:first:dict@5:13` — exact confidence, 0 counter-evidence sites (`candidate-38c3835b514e16433a42`)
- **local-implementation-dictionary** `package.combine_b:transform:dict@5:13` — exact confidence, 0 counter-evidence sites (`candidate-5e959788651a67f8fa33`)
- **local-implementation-dictionary** `package.work:third:dict@19:13` — exact confidence, 0 counter-evidence sites (`candidate-c96d41ab3b0bd115db7c`)
- **local-implementation-dictionary** `package.work:second:dict@12:13` — exact confidence, 0 counter-evidence sites (`candidate-e4399efe838b54d82e0c`)

## Findings

- **module-combination-candidate** `package.combine_a` — combine-modules proposal is blocked-insufficient-evidence (`package/combine_a.py:5`)
- **duplication.normalized-ast** `package.work` — normalized-ast clone cluster (`package/combine_a.py:6`)
- **extraction-candidate** `package.combine_a` — extract-responsibility proposal is reviewable-incomplete (`package/combine_a.py:5`)
- **configured-layer-direction** `package.domain_bad` — Configured architecture layer direction is violated (`package/domain_bad.py:1`)

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
