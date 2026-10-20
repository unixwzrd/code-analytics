# Runtime profile evidence

- Schema: `code-analysis-profile-report@1.0.0`
- Completeness: `complete`
- Producer: `unknown`
- Repetitions: 1
- Frames: 2
- Runtime edges: 1

| Function | Class | Calls | Total seconds | Cumulative seconds |
| --- | --- | ---: | ---: | ---: |
| `package/module.py:10:caller` | project | 2 | 0.125000000 | 0.375000000 |
| `package/module.py:20:callee` | project | 4 | 0.250000000 | 0.250000000 |

## Runtime call edges

| Caller | Callee | Representation | Calls | Primitive calls | Total seconds | Cumulative seconds |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| `profile-frame-11111111111111111111` | `profile-frame-22222222222222222222` | cprofile-tuple | 4 | 4 | 0.250000000 | 0.250000000 |

## Repetition variance

| Frame | Mean | Median | Minimum | Maximum | Stddev | CV |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `profile-frame-11111111111111111111` | 0.125000000 | 0.125000000 | 0.125000000 | 0.125000000 | 0.000000000 | 0.0 |
| `profile-frame-22222222222222222222` | 0.250000000 | 0.250000000 | 0.250000000 | 0.250000000 | 0.000000000 | 0.0 |

## Process resources

| Repetition | Wall | User CPU | System CPU | Peak RSS | Processes |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 0.500000000 | 0.400000000 | 0.050000000 | 1048576 | 1 |

## Limitations

- Runtime evidence describes only the supplied workload and is not exhaustive.
