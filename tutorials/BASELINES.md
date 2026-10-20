# Hotel architecture before any repairs

These are the working hotel iterations qualified on 2026-10-20. Source LOC includes physical lines, comments and blanks under `src/` or `lib/`, and excludes tests and evidence. The maximum weighted score is the analyzer’s recorded measurement, not a judgment about how much work is justified. Ruby’s LOC/weighted metrics are available, but its branch/depth/return fields are not reliable for these examples.

| Language | Level | Source files | Source LOC | Maximum weighted score | Admitted proposals |
| --- | --- | ---: | ---: | ---: | ---: |
| python | beginner | 4 | 184 | 54 | 0 |
| python | intermediate | 5 | 255 | 75 | 0 |
| python | advanced | 6 | 332 | 75 | 0 |
| javascript | beginner | 4 | 159 | 52 | 0 |
| javascript | intermediate | 5 | 246 | 75 | 0 |
| javascript | advanced | 6 | 329 | 75 | 0 |
| ruby | beginner | 4 | 163 | 51 | 0 |
| ruby | intermediate | 5 | 248 | 72 | 0 |
| ruby | advanced | 6 | 333 | 72 | 0 |

The beginner reservation method intentionally carries guest validation, optional requests, pricing, receipt formatting and persistence. Intermediate adds the tariff rulebook and entangled booking records. Advanced retains those rules and adds queue/cache/retry/event ownership. Each level contains a mix of problems, and each is an independent application. Comparing two different levels is not a behavior-preserving before/after experiment.

The first assignment is investigation. Use each level’s genuine `evidence/actual-report.json`, source hashes, expected observations and qualification results to support your report to Sybil. Every expectation is either observed, heuristic, deferred or an intentional negative control; there are no missing `supported` expectations in this qualified snapshot. Missing candidate admission, JavaScript aggregation/shape gaps and Ruby detection gaps remain explicit in [VALIDATION.md](VALIDATION.md).

For a later, separately chosen refactor exercise, preserve the original report outside `evidence/`, work in a copy of the level and keep the documented behavior checks. Collect with identical roots, configuration and thresholds, then use `code-analysis diff BEFORE.json AFTER.json --format markdown`. Check the source identity as well as the counts; fewer findings alone do not prove a better ownership boundary. No improved implementation or automatic edit is part of this investigation assignment. The previous storefront examples remain recoverable in Git commit `a992618`.
