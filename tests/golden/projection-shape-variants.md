# Dictionary shape variants

- Projection: `code-analysis-projection@1.0.0`
- Source: `code-analysis-report@1.0.0`
- Source SHA-256: `47a7aad3c086f8acdda23a9bf045c80b398940de648719d0b50337f2a4371773`
- Completeness: `partial`
- Records: 4
- Truncated: `false`

| Owner | Shape ID | Variant | Variant ID | Fields | Field names | Discriminator | Discriminator values | Requiredness states | Lifecycle states | Producer defaults | Consumer fallbacks | Confidence | Evidence path | Evidence line |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| package.consumer:consume:value | shape-97b3d9ce5b3a6e698725 | observed | variant-9ecb9d514509df3886a9 | 1 | trace_id |  |  | optional |  | 0 | 1 | resolved | package/consumer.py | 2 |
| package.producer:advance:payload | shape-5f876cd2eaccd1907c68 | observed | variant-d73d391457a12cb7a241 | 1 | state |  |  | always | state="queued", state="running" | 0 | 0 | resolved | package/producer.py | 14 |
| package.producer:build:payload | shape-bc2f366f4dab190ba0d8 | if@5:4:body | variant-4fbbec4ccbac2d77e3a4 | 2 | job_id, kind | kind | "queued" | always |  | 0 | 0 | resolved | package/producer.py | 6 |
| package.producer:build:payload | shape-bc2f366f4dab190ba0d8 | if@5:4:else | variant-28a8b55969018ade94cf | 2 | kind, result | kind | "done" | always |  | 0 | 0 | resolved | package/producer.py | 8 |

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
