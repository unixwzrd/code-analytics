# Python profiling scope and interpretation

Code Analysis separates capture ownership from analysis of saved profiles. Standard-library `pstats` ingestion and SHA-bound producer declarations remain supported. A declared scope is not evidence of whole-program coverage, and missing historical identity/scope facts cannot be reconstructed by assumption.

## Capture boundaries

A conventional `cProfile.Profile` enabled in one thread does not automatically profile all existing/new threads or child processes. Main-thread captures, subject-owned scoped callbacks, generators, worker threads and subprocesses retain their actual producer/source/workload and declared scope. The runner cannot invent capture ownership for subject-owned profiles.

Valid partial captures still provide useful counts and caller relationships. Unrelated-thread frames must not acquire false parents. Generator resume/close, cancellation, callbacks and exceptions require explicit evidence, not a whole-interpreter claim.

## Timings and comparisons

Distinguish wall time, CPU, waits and opaque native/device work. Python frame duration is not measured GPU time, proof of Python overhead or an unprofiled benchmark. Unavailable caller-edge timings remain null, not zero. Overlapping cumulative times cannot be summed into wall time.

Source-bound identity and complete collection of a declared scope are separate from exhaustive program coverage. Unexpected exits, ownership/hash mismatch, timeout, unbound input and missing scope facts remain failures or partial evidence. A diagnostic override cannot qualify a performance claim.

## Practical workflow

Use the [CLI reference](cli.md) and [runtime examples](cli-examples.md) for explicitly authorized collection or subject-owned profile import. Review producer/hash binding, source mapping, workload completion and limitations before comparing runs.

Tutorial qualification records a direct-script import-context failure and a successful module-based unittest workload. Preserve that distinction rather than hiding the failed capture or claiming arbitrary script parity. See [beta limitations](beta.md).

Broader threaded capture, live tracing and a new profiler framework remain separate follow-ups. Internal experimental logs are archived privately; this guide does not claim additional qualification.
