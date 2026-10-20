# Static test-impact relationships

- Projection: `code-analysis-projection@1.0.0`
- Source: `code-analysis-report@1.0.0`
- Source SHA-256: `c49d809d7043da38cf8f65343277ad6904f95f8800cba99fac0b03a73577ea3b`
- Completeness: `partial`
- Records: 20
- Truncated: `false`

| Test, fixture or module | Relationship | Target | Target module | Resolution | Evidence path | Evidence line |
| --- | --- | --- | --- | --- | --- | --- |
| tests.test_core:test_calculate | fixture-use | monkeypatch |  | unresolved | tests/test_core.py | 10 |
| tests.conftest:client | direct-symbol | pytest:fixture | pytest | unresolved | tests/conftest.py | 9 |
| tests.test_core:test_calculate | direct-symbol | pytest:mark.parametrize | pytest | unresolved | tests/test_core.py | 9 |
| tests.conftest:configuration | direct-symbol | pytest:fixture | pytest | unresolved | tests/conftest.py | 4 |
| tests.test_core:test_qualification_path | fixture-use | tests.conftest:client |  | resolved | tests/test_core.py | 16 |
| tests.test_core:test_calculate | patch | sample.core:helper | sample.core | resolved | tests/test_core.py | 11 |
| tests.test_core:test_calculate | fixture-use | value |  | unresolved | tests/test_core.py | 10 |
| tests.test_core:test_calculate | direct-symbol | unittest.mock:patch | unittest.mock | unresolved | tests/test_core.py | 12 |
| tests.test_core:test_dynamic_patch | direct-symbol | unittest.mock:patch | unittest.mock | unresolved | tests/test_core.py | 21 |
| tests.test_core:test_calculate | fixture-use | expected |  | unresolved | tests/test_core.py | 10 |
| tests.test_core:test_qualification_path | module-import | sample.core | sample.core | resolved | tests/test_core.py | 16 |
| tests.test_core:test_calculate | fixture-use | tests.conftest:client |  | resolved | tests/test_core.py | 10 |
| tests.test_core:test_calculate | patch | sample.core:helper | sample.core | resolved | tests/test_core.py | 12 |
| tests.test_core:test_dynamic_patch | fixture-use | target_name |  | unresolved | tests/test_core.py | 20 |
| tests.test_core:test_calculate | module-import | sample.core | sample.core | resolved | tests/test_core.py | 10 |
| tests.test_core:test_dynamic_patch | module-import | sample.core | sample.core | resolved | tests/test_core.py | 20 |
| tests.test_core:test_calculate | direct-symbol | sample.core:calculate | sample.core | resolved | tests/test_core.py | 13 |
| tests.test_core:test_calculate | direct-symbol | pytest:mark | pytest | unresolved | tests/test_core.py | 9 |
| tests.test_core:test_dynamic_patch | patch | target_name |  | unresolved | tests/test_core.py | 21 |
| tests.conftest:client | fixture-use | tests.conftest:configuration |  | resolved | tests/conftest.py | 10 |

## Limitations

- Call resolution is bounded to direct local and imported syntax; dynamic dispatch is unresolved.
- Clone evidence is syntax-only; semantic equivalence and runtime behavior are unmeasured.
- Complexity and orchestration classifications are threshold heuristics, not refactor approval.
- Contract inventory is syntax-only and does not resolve producer/consumer call flow.
- Data-structure topology is syntax-only and limited to evidenced dictionary operations and declarations.
- Dictionary analysis is syntax-only and bounded; dynamic calls and keys remain unresolved.
- Dictionary evidence is descriptive and does not authorize a representation change.
- Dynamic imports, reflection, generated modules, and runtime-only edges are unresolved.
- Dynamic patches, generated tests, indirect parametrization, reflection, and framework discovery may remain unresolved.
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
- Static test references indicate likely impact, not execution or behavioral coverage.
- Structural validation currently covers proven dictionary membership, truthiness, and coercion checks.
- Validation-relative mutation timing and data-structure ownership remain unavailable.
- runtime-binding-is-not-established-by-static-call-evidence
- static-call-evidence-is-syntactic-and-does-not-establish-runtime-dispatch
