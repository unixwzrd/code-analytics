# The Booking Mix-Up — qualification

Qualified on 2026-10-20. This is sample-application and evidence qualification, not human UI acceptance.

## Application behavior

`python src/demo.py` → exit 0

```text
{"receipt": "Fawlty Towers | Ada owes £21.60", "rooms_available": 6, "saved": 1, "total": 2160}

```

`python -m unittest discover -s tests -v` → exit 0

```text

test_clone_behavior_and_validation (test_demo.HotelTest.test_clone_behavior_and_validation) ... ok
test_demo_is_repeatable (test_demo.HotelTest.test_demo_is_repeatable) ... ok
test_discount_branches_and_legacy_shape (test_demo.HotelTest.test_discount_branches_and_legacy_shape) ... ok
test_state_mutation_and_persistence (test_demo.HotelTest.test_state_mutation_and_persistence) ... ok

----------------------------------------------------------------------
Ran 4 tests in 0.002s

OK
```

## Static collection

`code-analysis scan . --no-workspace --json-compact -o evidence/actual-report.json` → exit 96.

`code-analysis scan . --no-workspace --json-compact -o <temporary-report.json>` repeated the same scan outside the source tree.

Repeat exit: 96; byte-identical report: **true**. Neither report was edited or normalized.

Analyzer: `0.1.0`; schema: `code-analysis-report@1.0.0`; completeness: **partial**.

Report SHA-256: `62b37f9971d5de472c7bc9aa8740bb7af9a6b8eddbe47bf8de5dea29bf2f0823`. Configuration SHA-256: `c887524335f013accfdbed36f98f3276a1672ac779535fb9c26ad0e2ff9c4bd7`.

Source hashes were checked against every bundled file; source discovery contains only the configured source directory. Tests are separately discovered according to each provider; neither the evidence nor documentation is a source module.

| Runtime | Observed version |
| --- | --- |
| `python --version` | Python 3.12.13 |
| `node --version` | v24.15.0 |
| `ruby --version` | ruby 3.3.4 (2024-07-09 revision be1089c8ec) [arm64-darwin24] |

| Count | Actual |
| --- | ---: |
| modules | 5 |
| classes | 2 |
| functions | 15 |
| methods | 3 |
| findings | 6 |
| cycles | 1 |
| clone_clusters | 2 |
| dictionary_shapes | 15 |
| contracts | 1 |
| state_resources | 1 |
| side_effects | 0 |
| refactor_candidates | 0 |
| refactor_proposals | 0 |
| test_modules | 1 |
| test_cases | 4 |
| unresolved_edges | 2 |
| diagnostic rows | 0 |

| Provider | State | Completeness |
| --- | --- | --- |
| native-ast | success | complete |
| native-contracts | success | complete |
| native-dictionary | partial | partial |
| native-validation | success | complete |
| native-structures | success | complete |
| native-candidates | success | complete |
| native-clones | success | complete |
| native-state-effects | success | complete |
| native-commonality | success | complete |
| static-call-occurrences | success | complete |
| native-interfaces | success | complete |
| native-cohesion | success | complete |
| native-test-impact | partial | partial |

Frontend `python-ast`: complete; 5 discovered, 5 complete, 0 partial, 0 failed.

## Expected observations — answer key

Read the mission before this section.

| Tutorial ID | Expectation | Actual | Interpretation |
| --- | --- | --- | --- |
| PY-INT-01 | supported | observed | The tariff routine nests guest tier, room representation, group rates, coupons and statement-delivery policy. Metrics prompt maintenance review, not a failed booking. |
| PY-INT-02 | supported | observed | The direct and agency calculators duplicate validation and integer rate arithmetic. Clone evidence does not decide whether the two policies should share an owner. |
| PY-INT-03 | supported | observed | There is a safe local import cycle, with imported values used after initialization. A cycle does not prove a startup failure. |
| PY-INT-04 | supported | observed | Quoted totals or bookings remain in a shared process-global collection until explicitly cleared. Syntax evidence does not prove concurrency safety or an unbounded production leak. |
| PY-INT-05 | supported | observed | Standard room offers and annex offers represent the same domain with different keys. The RoomOffer declaration and shape observations are descriptive, not a proven runtime violation. |
| PY-INT-06 | heuristic | observed | Reservation decrements the caller-owned room mapping in place; behavior checks verify object identity and remaining rooms. The dynamic stock field has no dedicated argument-mutation finding; source and tests supply that evidence. |
| PY-INT-07 | negative_control | observed | The short demo legitimately coordinates a booking and disposable receipt persistence. Orchestration is intentional; no finding targets this coordinator. Absence of a finding is not proof of design quality. |
| PY-INT-08 | heuristic | observed | One class owns guest-facing presentation, receipt state and persistence as well as billing coordination. No refactor proposal is emitted. Review ownership manually; the empty Candidates view is not approval. |
| PY-INT-09 | heuristic | observed | Booking creation and guest preview repeat name presence, type and blank checks. The repeated source is visible, but no validation-pattern record is promised. |

Additional finding rows beyond symbol-specific expectations: complexity: agency_booking_total; import-cycle: Local module import cycle; duplication: normalized-ast clone cluster; duplication: control-call-sequence clone cluster.

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
- Unresolved dictionary evidence constrains report completeness.
- Validation-relative mutation timing and data-structure ownership remain unavailable.
- runtime-binding-is-not-established-by-static-call-evidence
- static-call-evidence-is-syntactic-and-does-not-establish-runtime-dispatch

## Browser verification

Verified in Chromium against this exact retained report digest and matching source root: Overview reopened saved evidence; Providers rendered recorded health; Architecture rendered the dependency graph and its reception node activated with keyboard focus/Enter; the source evidence preview and Browse code both reported matching source bytes; contextual Help/Escape preserved the source view; Candidates showed its empty state. Native pointer activation at the SVG group center was intercepted by the surrounding SVG in automation; keyboard activation worked. This does not establish general pointer behavior or human usability acceptance. Browser captures stay outside the committed evidence directory.

## Explicit Python runtime exercise

The normal static mission does not execute any workload. The following optional collection was run separately with execution acknowledgment:

```bash
code-analysis profile --source-report evidence/actual-report.json --allow-execution --workspace ../../../output/fawlty-qualification/profiles/python-intermediate-test-workspace --output ../../../output/fawlty-qualification/profiles/python-intermediate-tests.json -- python -m unittest discover -s tests -v
```

Observed exit 0; profile completeness **complete**; workload statuses `[0]`; 13 source-bound frames and 1806 recorded runtime edges. All of these are workload observations, not exhaustive coverage or an optimization result. The explicit workspace/report paths above are ignored repository output, not bundled artifacts.

A preceding script workload used the same profile command with `-- python src/demo.py`. It returned exit 100 (EPROTO on this host), with workload status 1 and `ModuleNotFoundError: No module named 'rooms'`. Running that script directly succeeds. The current profile script runner does not provide the source-directory import context used by direct script launch for this fixture. Its failed capture is retained in local ignored output; the application and analyzer were not modified to hide the failure. The supported module-based unittest workload already establishes its source import path and completes successfully. This import-context behavior is a separate Code Analysis issue.

Tiny timings include profiler/import/test overhead and do not demonstrate meaningful performance bottlenecks. Profiling artifacts and host-specific raw captures are not checked into this tutorial.
