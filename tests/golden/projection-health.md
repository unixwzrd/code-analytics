# Analysis health and provenance

- Projection: `code-analysis-projection@1.0.0`
- Source: `code-analysis-report@1.0.0`
- Source SHA-256: `3c55cc8eb7e9ff80d84a33128144c15903261f8fa6491708a57a4b16cd2b1e16`
- Completeness: `complete`
- Records: 12
- Truncated: `false`

| Record type | Identity | State | Completeness | Detail |
| --- | --- | --- | --- | --- |
| provider | native-ast | success | complete | module-index, symbol-index, complexity, import-graph |
| provider | native-clones | success | complete |  |
| provider | native-cohesion | success | complete | architecture-contract-suggestions, bounded-refactor-proposals, responsibility-clusters, simulated-module-graphs |
| provider | native-commonality | success | complete | function-feature-profiles, inverted-feature-candidates |
| provider | native-contracts | success | complete | contract-declaration-inventory, contract-direct-aliases, contract-serialization-sites, contract-use-sites, normalized-type-expressions, schema-version-declarations |
| provider | native-dictionary | success | complete | dictionary-operations, dictionary-shapes, dictionary-flow, dictionary-lifecycle-transitions |
| provider | native-interfaces | success | complete | boundary-classification, producer-consumer-correlation, payload-variant-comparison |
| provider | native-state-effects | success | complete | mutable-state-resources, provenance-resolved-side-effects |
| provider | native-structures | success | complete | dictionary-topology, dictionary-lifecycle |
| provider | native-test-impact | unavailable | complete |  |
| provider | native-validation | success | complete | structural-dictionary-validation |
| provider | static-call-occurrences | success | complete | python |

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
