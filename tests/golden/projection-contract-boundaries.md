# Contract boundary matrix

- Projection: `code-analysis-projection@1.0.0`
- Source: `code-analysis-report@1.0.0`
- Source SHA-256: `9951d5e650fd88641147b44a03a774a32813503555dc849e513c5f325acd10d8`
- Completeness: `partial`
- Records: 5
- Truncated: `true`

| Contract | Contract ID | Contract kind | Boundary class | Direction | Site | Site module | Confidence | Serialization formats | Schema versions | Evidence path | Evidence line |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| package.base:ContractBase | contract-8bc30f46983e9029ce15 | pydantic-model | module-local | definition | package.base:ContractBase | package.base | exact |  |  | package/base.py | 7 |
| package.base:PayloadBase | contract-05bc3b7204bd1a8765e6 | typed-dict | module-local | definition | package.base:PayloadBase | package.base | exact |  |  | package/base.py | 11 |
| package.base:RecordBase | contract-a5e15b5e42351683a5e6 | dataclass | module-local | definition | package.base:RecordBase | package.base | exact |  |  | package/base.py | 16 |
| package.contracts:ArchivedJob | contract-32be8289d537a0c77321 | dataclass | cross-subsystem-internal | consumer | package.usage:archive:parameter:record:package.contracts.ArchivedJob | package.usage | resolved | mapping |  | package/usage.py | 11 |
| package.contracts:ArchivedJob | contract-32be8289d537a0c77321 | dataclass | cross-subsystem-internal | consumer | package.usage:archive_mapping:call@24:11 | package.usage | resolved | mapping |  | package/usage.py | 24 |

## Limitations

- Complexity and orchestration classifications are threshold heuristics, not refactor approval.
- Contract inventory is syntax-only and does not resolve producer/consumer call flow.
- Dictionary-shape, validation, lifecycle, and candidate analysis remain disabled.
- Dynamic imports, reflection, generated modules, and runtime-only edges are unresolved.
- External graph providers are inventoried separately and are not reconciled in Phase 1 scans.
- Projection output is truncated by its declared record limit.
- Serialization evidence resolves exact declaration kinds, local annotations, and direct straight-line aliases; control-flow aliases and runtime types remain unresolved.
