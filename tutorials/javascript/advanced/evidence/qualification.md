# The Hotel From Hell — qualification

Qualified on 2026-10-20. This is sample-application and evidence qualification, not human UI acceptance.

## Application behavior

`node src/demo.mjs` → exit 0

```text
{"receipt":"Fawlty Towers | Ada owes £21.60","saved":1,"rooms_available":6,"total":2160,"batch_total":2160}

```

`node --test tests/demo.test.mjs` → exit 0

```text
✔ repeatable demo, real temporary persistence and safe ESM cycle (2.317041ms)
✔ billing branches and two object shapes (0.109459ms)
✔ clones agree and reject negative prices (0.17675ms)
✔ reserve mutates the input and rejects insufficient rooms (0.13425ms)
✔ queue states, cache, aliased jobs and JSON persistence (0.78825ms)
ℹ tests 5
ℹ suites 0
ℹ pass 5
ℹ fail 0
ℹ cancelled 0
ℹ skipped 0
ℹ todo 0
ℹ duration_ms 49.225542

```

## Static collection

`code-analysis scan . --no-workspace --json-compact -o evidence/actual-report.json` → exit 0.

`code-analysis scan . --no-workspace --json-compact -o <temporary-report.json>` repeated the same scan outside the source tree.

Repeat exit: 0; byte-identical report: **true**. Neither report was edited or normalized.

Analyzer: `0.1.0`; schema: `code-analysis-report@1.0.0`; completeness: **complete**.

Report SHA-256: `72e7a167683207b738338fa83c4883bb479d4a2a3945eb7ea7e04eb756808910`. Configuration SHA-256: `008c1e021627f2e7074e2c00299daaf16aca2ffeac4bff3743b9a40f4092557c`.

Source hashes were checked against every bundled file; source discovery contains only the configured source directory. Tests are separately discovered according to each provider; neither the evidence nor documentation is a source module.

| Runtime | Observed version |
| --- | --- |
| `python --version` | Python 3.12.13 |
| `node --version` | v24.15.0 |
| `ruby --version` | ruby 3.3.4 (2024-07-09 revision be1089c8ec) [arm64-darwin24] |

| Count | Actual |
| --- | ---: |
| modules | 6 |
| classes | 2 |
| functions | 16 |
| methods | 5 |
| findings | 0 |
| cycles | 0 |
| clone_clusters | 1 |
| dictionary_shapes | 0 |
| contracts | 0 |
| state_resources | 4 |
| side_effects | 3 |
| refactor_candidates | 0 |
| refactor_proposals | 0 |
| test_modules | 1 |
| test_cases | 0 |
| unresolved_edges | 0 |
| diagnostic rows | 0 |

| Provider | State | Completeness |
| --- | --- | --- |
| native-ast | success | complete |
| native-contracts | success | complete |
| native-dictionary | success | complete |
| native-validation | success | complete |
| native-structures | success | complete |
| native-clones | success | complete |
| native-state-effects | success | complete |
| native-commonality | success | complete |
| javascript-tree-sitter | success | complete |
| static-call-occurrences | success | complete |
| native-interfaces | success | complete |
| native-cohesion | success | complete |
| native-test-impact | unavailable | complete |

Frontend `javascript-tree-sitter`: complete; 6 discovered, 6 complete, 0 partial, 0 failed.

## Expected observations — answer key

Read the mission before this section.

| Tutorial ID | Expectation | Actual | Interpretation |
| --- | --- | --- | --- |
| JS-ADV-01 | supported | observed | The tariff routine nests guest tier, room representation, group rates, coupons and statement-delivery policy. Metrics and source are available but finding rows are absent. |
| JS-ADV-02 | supported | observed | The direct and agency calculators duplicate validation and integer rate arithmetic. Clone evidence does not decide whether the two policies should share an owner. |
| JS-ADV-03 | deferred | deferred | There is a safe local import cycle, with imported values used after initialization. All local edges are recorded, but the report has no aggregated cycle or SCC record. |
| JS-ADV-03-EDGES | supported | observed | The individual local imports trace the source-level cycle. This checks edges separately from the missing cycle aggregation. |
| JS-ADV-04 | supported | observed | Quoted totals or bookings remain in a shared process-global collection until explicitly cleared. Syntax evidence does not prove concurrency safety or an unbounded production leak. |
| JS-ADV-05 | deferred | deferred | Standard room offers and annex offers represent the same domain with different keys. These object/hash shapes have no contract or dictionary-shape records in this provider. |
| JS-ADV-06 | heuristic | observed | Reservation decrements the caller-owned room mapping in place; behavior checks verify object identity and remaining rooms. The dynamic stock field has no dedicated argument-mutation finding; source and tests supply that evidence. |
| JS-ADV-07 | negative_control | observed | The short demo legitimately coordinates a booking and disposable receipt persistence. Orchestration is intentional; no finding targets this coordinator. Absence of a finding is not proof of design quality. |
| JS-ADV-08 | heuristic | observed | One class owns guest-facing presentation, receipt state and persistence as well as billing coordination. No refactor proposal is emitted. Review ownership manually; the empty Candidates view is not approval. |
| JS-ADV-09 | heuristic | observed | Booking creation and guest preview repeat name presence, type and blank checks. The repeated source is visible, but no validation-pattern record is promised. |
| JS-ADV-10 | supported | observed | Cancellation, cached replay, guest holds, retry limits, booking creation and event history interact in one processor. A weighted score does not establish a runtime bottleneck; Ruby branch metrics are incomplete. |
| JS-ADV-11 | supported | observed | Pending jobs, receipt cache and event history are module-owned mutable resources. Static ownership does not prove safe reentrancy, cache lifetime or cleanup outcomes. |
| JS-ADV-12 | heuristic | observed | Loading keeps caller job references; processing mutates attempts/state and returns different success and skipped-result shapes. Tests prove the demonstrated alias effects; broad lifecycle correctness and non-Python shape inference are unestablished. |

Additional finding rows beyond symbol-specific expectations: none.

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
- JavaScript analysis is syntax-only; dynamic dispatch, package export maps, bundler aliases, and framework semantics remain unresolved.
- Mutable-state and side-effect evidence is syntax-only; runtime ownership and outcomes are unmeasured.
- Names and literals remain significant for token clones and are categorical in AST clones.
- Operational filesystem, path, symlink, authorization, security, and runtime-state validation is excluded.
- Phase 4 test-impact evidence is unavailable.
- Runtime ownership, lock behavior and coverage, persistence success, cleanup effectiveness, and growth remain unmeasured.
- Serialization evidence resolves exact declaration kinds, local annotations, and direct straight-line aliases; control-flow aliases and runtime types remain unresolved.
- Shadowing, dynamic dispatch, cleanup effectiveness, and arbitrary receiver provenance remain unresolved.
- Static graph simulation does not establish runtime behavior or authorize source changes.
- Structural validation currently covers proven dictionary membership, truthiness, and coercion checks.
- Validation-relative mutation timing and data-structure ownership remain unavailable.
- runtime-binding-is-not-established-by-static-call-evidence
- static-call-evidence-is-syntactic-and-does-not-establish-runtime-dispatch

## Browser verification

Verified in Chromium against this exact retained report digest and matching source root: Overview reopened saved evidence; Providers rendered recorded health; Architecture rendered the dependency graph and its reception node activated with keyboard focus/Enter; the source evidence preview and Browse code both reported matching source bytes; contextual Help/Escape preserved the source view; Candidates showed its empty state. Native pointer activation at the SVG group center was intercepted by the surrounding SVG in automation; keyboard activation worked. This does not establish general pointer behavior or human usability acceptance. Browser captures stay outside the committed evidence directory.
