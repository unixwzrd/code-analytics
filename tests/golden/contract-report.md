# Code Analysis Summary

Completeness: **complete**

Analyzed 1/1 Python files, 1 modules, and 2 callable symbols.

## Stable counts

- Classes: 1
- Functions: 2
- Methods: 0
- Import edges: 0
- Strongly connected components: 0
- Canonical cycles: 0
- Findings: 0
- Dictionary shapes: 0
- Dictionary observations: 0
- Resolved flow edges: 0
- Unresolved edges: 0
- Validation patterns: 0
- Data structures: 0
- Clone clusters: 0
- Function commonalities: 0
- Mutable state resources: 0
- Side effects: 0
- Responsibility clusters: 0
- Refactoring proposals: 0
- Contract subjects: 5
- Contracts: 1

## Contract inventory

- **pydantic-model** `golden_contract:Envelope` — 2 members; boundaries: module-local; serialization: json; schema versions: v1 (`contract-2530f986268e20763e58`)

## Dictionary shapes

No evidenced dictionary shapes were reported.

## Validation patterns

No structural validation patterns were reported.

## Data structures

No significant data structures were reported.

## Findings

No threshold findings were reported.

## Limitations

- Complexity and orchestration classifications are threshold heuristics, not refactor approval.
- Contract inventory is syntax-only and does not resolve producer/consumer call flow.
- Dictionary-shape, validation, lifecycle, and candidate analysis remain disabled.
- Dynamic imports, reflection, generated modules, and runtime-only edges are unresolved.
- External graph providers are inventoried separately and are not reconciled in Phase 1 scans.
- Serialization evidence resolves exact declaration kinds, local annotations, and direct straight-line aliases; control-flow aliases and runtime types remain unresolved.
