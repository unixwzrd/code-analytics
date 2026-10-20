# Structural validation divergence

- Projection: `code-analysis-projection@1.0.0`
- Source: `code-analysis-report@1.0.0`
- Source SHA-256: `16777420af2bfee5688ae6a83cacaed0ee9e72a6a1df2dab879f0e3c166ea62d`
- Completeness: `complete`
- Records: 10
- Truncated: `false`

| Validation ID | Classification | Rule | Field identity | Sites | Subjects | Divergent | Fail open | Limitations | Evidence path | Evidence line |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| validation-8faae44f15f1f306771e | structural | dictionary key present: literal-string:enabled | package.checks:divergent_payload:payload:literal-string:enabled | 1 | package.checks:divergent_payload:payload | true | false |  | package/checks.py | 20 |
| validation-00645ba3bd286873518c | structural | dictionary key value truthy: literal-string:enabled | package.checks:divergent_payload:payload:literal-string:enabled | 1 | package.checks:divergent_payload:payload | true | false | Truthy/falsy value checks do not distinguish absent, null, zero, false, or empty values. | package/checks.py | 22 |
| validation-51ba6e1e361c557a0730 | structural | dictionary keys not equal to: [enabled,item_id] |  | 1 | package.checks:validate_shape:payload | false | false |  | package/checks.py | 30 |
| validation-c1c495e7692b6dcfe117 | operational | filesystem path exists |  | 2 | package.checks:path_available:operational-validation, package.checks:validate_path:operational-validation | false | false | Operational checks must remain with filesystem, runtime, or security ownership.; Syntax evidence does not prove validation outcome, ordering, or effectiveness. | package/checks.py | 39 |
| validation-a52ed39a3c91f06383e6 | operational | filesystem path type checked: file |  | 1 | package.checks:validate_path:operational-validation | false | false | Operational checks must remain with filesystem, runtime, or security ownership.; Syntax evidence does not prove validation outcome, ordering, or effectiveness. | package/checks.py | 39 |
| validation-5f12438a7f198187cd63 | structural | dictionary key absent: literal-string:item_id | package.checks:accept_payload:payload:literal-string:item_id | 2 | package.checks:validate_payload:payload, package.checks:accept_payload:payload | false | false |  | package/checks.py | 5 |
| validation-a5ad2047903602d68350 | structural | dictionary key value is not instance of: literal-string:item_id:str | package.checks:validate_shape:payload:literal-string:item_id | 1 | package.checks:validate_shape:payload | false | false |  | package/checks.py | 28 |
| validation-2bf46175638611b3ef15 | structural | dictionary key value not-equals: literal-string:schema_version:[2] | package.checks:validate_shape:payload:literal-string:schema_version | 1 | package.checks:validate_shape:payload | false | false |  | package/checks.py | 32 |
| validation-795beb318c55a7c9d2f3 | structural | dictionary key value not-in: literal-string:state:["complete","ready"] | package.checks:validate_shape:payload:literal-string:state | 1 | package.checks:validate_shape:payload | false | false |  | package/checks.py | 34 |
| validation-951bc9c268a0849f436c | structural | dictionary key absent: literal-string:warning | package.checks:warn_payload:payload:literal-string:warning | 1 | package.checks:warn_payload:payload | false | true | A negative literal-field or exact-key-set branch contains only pass or standard-library logging; runtime acceptance remains unmeasured. | package/checks.py | 14 |

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
- Operational validation is limited to syntax-evidenced path and filesystem checks; outcomes and effectiveness remain unmeasured.
- Phase 4 test-impact evidence is unavailable.
- Runtime ownership, lock behavior and coverage, persistence success, cleanup effectiveness, and growth remain unmeasured.
- Serialization evidence resolves exact declaration kinds, local annotations, and direct straight-line aliases; control-flow aliases and runtime types remain unresolved.
- Shadowing, dynamic dispatch, cleanup effectiveness, and arbitrary receiver provenance remain unresolved.
- Static graph simulation does not establish runtime behavior or authorize source changes.
- Structural validation currently covers proven dictionary membership, truthiness, and coercion checks.
- Validation-relative mutation timing and data-structure ownership remain unavailable.
- runtime-binding-is-not-established-by-static-call-evidence
- static-call-evidence-is-syntactic-and-does-not-establish-runtime-dispatch
