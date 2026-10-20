# The Reservation Desk — qualification

Qualified on 2026-10-20. This is sample-application and evidence qualification, not human UI acceptance.

## Application behavior

`ruby lib/demo.rb` → exit 0

```text
{"receipt":"Fawlty Towers | Ada owes £21.60","saved":1,"rooms_available":6}

```

`ruby test/test_demo.rb` → exit 0

```text
PASS: beginner demo, shapes, cycle, clones and mutation
PASS: front-desk requests, guest validation and persistence

```

## Static collection

`code-analysis scan . --no-workspace --json-compact -o evidence/actual-report.json` → exit 0.

`code-analysis scan . --no-workspace --json-compact -o <temporary-report.json>` repeated the same scan outside the source tree.

Repeat exit: 0; byte-identical report: **true**. Neither report was edited or normalized.

Analyzer: `0.1.0`; schema: `code-analysis-report@1.0.0`; completeness: **complete**.

Report SHA-256: `aa093a083347406bb5c39e463b4ebc4f0f05468db1fd21ca21981de98bd8697c`. Configuration SHA-256: `018763cae3bec5c28c1eee91f9f72290a29dc6a88c8edbf2fe06b252b022983c`.

Source hashes were checked against every bundled file; source discovery contains only the configured source directory. Tests are separately discovered according to each provider; neither the evidence nor documentation is a source module.

| Runtime | Observed version |
| --- | --- |
| `python --version` | Python 3.12.13 |
| `node --version` | v24.15.0 |
| `ruby --version` | ruby 3.3.4 (2024-07-09 revision be1089c8ec) [arm64-darwin24] |

| Count | Actual |
| --- | ---: |
| modules | 4 |
| classes | 4 |
| functions | 0 |
| methods | 13 |
| findings | 0 |
| cycles | 1 |
| clone_clusters | 0 |
| dictionary_shapes | 0 |
| contracts | 0 |
| state_resources | 0 |
| side_effects | 0 |
| refactor_candidates | 0 |
| refactor_proposals | 0 |
| test_modules | 0 |
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
| ruby-prism | success | complete |
| static-call-occurrences | success | complete |
| native-interfaces | success | complete |
| native-cohesion | success | complete |
| native-test-impact | unavailable | complete |

Frontend `ruby-prism`: complete; 4 discovered, 4 complete, 0 partial, 0 failed.

## Expected observations — answer key

Read the mission before this section.

| Tutorial ID | Expectation | Actual | Interpretation |
| --- | --- | --- | --- |
| RB-BEG-01 | supported | observed | Validation, optional requests, billing, formatting and persistence share one front-desk method. LOC and weighted metrics are available; branch/depth/return counts are zero despite visible branching, and finding rows are absent. |
| RB-BEG-02 | deferred | deferred | The direct and agency calculators duplicate validation and integer rate arithmetic. Ruby emits no clone records for this source; compare the source manually and defer detection. |
| RB-BEG-03 | supported | observed | There is a safe local import cycle, with imported values used after initialization. A cycle does not prove a startup failure. |
| RB-BEG-04 | deferred | deferred | Quoted totals or bookings remain in a shared process-global collection until explicitly cleared. Mutable Ruby arrays are visible in source, but no state-resource records are emitted. |
| RB-BEG-05 | deferred | deferred | Standard room offers and annex offers represent the same domain with different keys. These object/hash shapes have no contract or dictionary-shape records in this provider. |
| RB-BEG-06 | heuristic | observed | Reservation decrements the caller-owned room mapping in place; behavior checks verify object identity and remaining rooms. The dynamic stock field has no dedicated argument-mutation finding; source and tests supply that evidence. |
| RB-BEG-07 | negative_control | observed | The short demo legitimately coordinates a booking and disposable receipt persistence. Orchestration is intentional; no finding targets this coordinator. Absence of a finding is not proof of design quality. |
| RB-BEG-08 | heuristic | observed | One class owns guest-facing presentation, receipt state and persistence as well as billing coordination. No refactor proposal is emitted. Review ownership manually; the empty Candidates view is not approval. |

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
- Mutable-state and side-effect evidence is syntax-only; runtime ownership and outcomes are unmeasured.
- Names and literals remain significant for token clones and are categorical in AST clones.
- Operational filesystem, path, symlink, authorization, security, and runtime-state validation is excluded.
- Phase 4 test-impact evidence is unavailable.
- Ruby analysis is syntax-only; metaprogramming, runtime mixins, dynamic constants, and generated methods remain unresolved.
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
