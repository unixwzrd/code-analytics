# Contract inventory

- Projection: `code-analysis-projection@1.0.0`
- Source: `code-analysis-report@1.0.0`
- Source SHA-256: `9951d5e650fd88641147b44a03a774a32813503555dc849e513c5f325acd10d8`
- Completeness: `complete`
- Records: 9
- Truncated: `false`

| Contract | Contract ID | Contract kind | Module | Members | Producer defaults | Consumer fallbacks | Boundaries | Boundary classes | Directions | Serialization formats | Schema versions | Unresolved behavior | Evidence path | Evidence line |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| package.base:RecordBase | contract-a5e15b5e42351683a5e6 | dataclass | package.base | 1 | 0 | 0 | 1 | module-local | definition |  |  |  | package/base.py | 16 |
| package.contracts:ArchivedJob | contract-32be8289d537a0c77321 | dataclass | package.contracts | 2 | 0 | 0 | 5 | cross-subsystem-internal, module-local | consumer, definition, producer | mapping |  |  | package/contracts.py | 24 |
| package.contracts:Job | contract-130507ce8f169648f461 | dataclass | package.contracts | 2 | 1 | 0 | 1 | module-local | definition |  |  |  | package/contracts.py | 8 |
| package.functional:Point | contract-d7d1079034c7decac849 | named-tuple | package.functional | 2 | 0 | 0 | 3 | module-local | consumer, definition | mapping |  |  | package/functional.py | 5 |
| package.base:ContractBase | contract-8bc30f46983e9029ce15 | pydantic-model | package.base | 1 | 0 | 0 | 1 | module-local | definition |  |  |  | package/base.py | 7 |
| package.contracts:Response | contract-a0e59398dcc4f370b0b4 | pydantic-model | package.contracts | 4 | 2 | 0 | 13 | cross-subsystem-internal, module-local, test-qualification-evidence | consumer, definition, producer | json, mapping | v1 |  | package/contracts.py | 18 |
| package.base:PayloadBase | contract-05bc3b7204bd1a8765e6 | typed-dict | package.base | 1 | 0 | 0 | 1 | module-local | definition |  |  |  | package/base.py | 11 |
| package.contracts:Request | contract-340527cbdacaabcd3006 | typed-dict | package.contracts | 3 | 0 | 0 | 2 | cross-subsystem-internal, module-local | consumer, definition |  |  |  | package/contracts.py | 13 |
| package.functional:LoosePayload | contract-bd776b8b56af5bfe7b9a | typed-dict | package.functional | 2 | 0 | 0 | 1 | module-local | definition |  |  |  | package/functional.py | 6 |

## Limitations

- Complexity and orchestration classifications are threshold heuristics, not refactor approval.
- Contract inventory is syntax-only and does not resolve producer/consumer call flow.
- Dictionary-shape, validation, lifecycle, and candidate analysis remain disabled.
- Dynamic imports, reflection, generated modules, and runtime-only edges are unresolved.
- External graph providers are inventoried separately and are not reconciled in Phase 1 scans.
- Serialization evidence resolves exact declaration kinds, local annotations, and direct straight-line aliases; control-flow aliases and runtime types remain unresolved.
