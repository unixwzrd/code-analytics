# Runtime profile evidence

- Schema: `code-analysis-profile-report@1.0.0`
- Completeness: `partial`
- Producer: `unknown`
- Repetitions: 1
- Frames: 1
- Runtime edges: 1

| Function | Class | Calls | Total seconds | Cumulative seconds |
| --- | --- | ---: | ---: | ---: |
| `unresolved/ff561f757134c8f1c31c2717:1:work` | unresolved | 2 | 0.125000000 | 0.250000000 |

## Runtime call edges

| Caller | Callee | Representation | Calls | Primitive calls | Total seconds | Cumulative seconds |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| `profile-frame-48bdcd7debd362b4e7db` | `profile-frame-48bdcd7debd362b4e7db` | cprofile-tuple | 2 | 1 | 0.125000000 | 0.250000000 |

## Repetition variance

| Frame | Mean | Median | Minimum | Maximum | Stddev | CV |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |

## Process resources

| Repetition | Wall | User CPU | System CPU | Peak RSS | Processes |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | unavailable | unavailable | unavailable | unavailable | unavailable |

## Limitations

- Cumulative node times overlap and must not be summed as elapsed time.
- Ingestion is byte/deadline bounded; an OS address-space cap is not available on every host.
- Native work and child processes may be absent; unobserved code is not dead code.
- Self per-call uses total calls; cumulative per-call uses primitive calls; zero denominators are null.
- Standalone pstats evidence has no independently captured workload status or process resources.
- Standalone pstats evidence is unbound to source hashes and symbols unless captured metadata agrees.
- Static call-edge overlay is unavailable; module imports are not substituted for calls.
- Unobserved static subjects are not evidence of dead code or exhaustive workload coverage.
- cProfile timings include profiler overhead and do not establish an optimization win.
