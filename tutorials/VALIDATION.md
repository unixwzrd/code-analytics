# Fawlty Towers qualification — 2026-10-20

The nine hotel applications are qualified against the current analyzer checkout, starting from repository commit `a992618`. Only `tutorials/` was changed. The genuine compact reports retain the analyzer’s existing schema, IDs, source hashes, configuration identity, provider states and completeness; they were not edited, patched or normalized. Total static-report storage is approximately 2.2 MiB across all nine projects. Source discovery is confined to each configured `src/` or `lib/`; test discovery remains separate and no `evidence/` file is a source module.

## Qualification matrix

All application demos and behavior checks pass. All nine paired scans are byte-identical. “Complete” describes the report’s declared scope, not language feature parity. A zero below means no record was emitted, not clean code. Python’s two clone clusters describe the same calculator pair through different fingerprints.

| Language | Level | Demo/tests | Static state / exit | Modules | Findings | Cycles | Clone clusters | Contracts/shapes | Mutable globals | Repeat scan | Saved-report browser smoke |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | --- | ---: | --- | --- |
| python | [beginner](python/beginner/README.md) | pass | partial (96) | 4 | 6 | 1 | 2 | 1/7 | 1 | identical | bound; keyboard graph/help/empty state pass |
| python | [intermediate](python/intermediate/README.md) | pass | partial (96) | 5 | 6 | 1 | 2 | 1/15 | 1 | identical | bound; keyboard graph/help/empty state pass |
| python | [advanced](python/advanced/README.md) | pass | partial (96) | 6 | 7 | 1 | 2 | 1/25 | 4 | identical | bound; keyboard graph/help/empty state pass |
| javascript | [beginner](javascript/beginner/README.md) | pass | complete (0) | 4 | 0 | 0 | 1 | 0/0 | 1 | identical | bound; keyboard graph/help/empty state pass |
| javascript | [intermediate](javascript/intermediate/README.md) | pass | complete (0) | 5 | 0 | 0 | 1 | 0/0 | 1 | identical | bound; keyboard graph/help/empty state pass |
| javascript | [advanced](javascript/advanced/README.md) | pass | complete (0) | 6 | 0 | 0 | 1 | 0/0 | 4 | identical | bound; keyboard graph/help/empty state pass |
| ruby | [beginner](ruby/beginner/README.md) | pass | complete (0) | 4 | 0 | 1 | 0 | 0/0 | 0 | identical | bound; keyboard graph/help/empty state pass |
| ruby | [intermediate](ruby/intermediate/README.md) | pass | complete (0) | 5 | 0 | 1 | 0 | 0/0 | 0 | identical | bound; keyboard graph/help/empty state pass |
| ruby | [advanced](ruby/advanced/README.md) | pass | complete (0) | 6 | 0 | 1 | 0 | 0/0 | 0 | identical | bound; keyboard graph/help/empty state pass |

Every source file parsed; there are no diagnostic rows. Each `evidence/qualification.md` contains its exact demo/test/scan commands, observed output, versions, provider states, counts, report/configuration hashes, expected-versus-actual observations and limitations. The qualification helper executes only explicitly selected language/level targets: for example, `python tutorials/qualify.py python/beginner`. It does not install packages or run product tests. Expected observations are curated tutorial declarations checked against actual report records; they do not replace or masquerade as an analyzer report. Deferred cases are reported separately from supported checks.

Runtimes are Python 3.12.13, Node 24.15.0 and Ruby 3.3.4, with existing Tree-sitter 0.25.2 / JavaScript grammar 0.25.0 and Prism 1.9.0. `code-analysis providers` confirmed availability before conversion. No dependency was installed or upgraded. Python uses unittest; JavaScript uses node:test; Ruby uses standard-library assertion scripts rather than a test gem. Checks preserve the old numeric invariants with deliberate domain equivalents: two rooms, one night, 1200 pennies per room, gold-tier discount, 2160 pennies total, one temporary receipt and inventory 8 → 6. Intermediate’s additional coupon/postage/annex branches and advanced’s queue/cache/cancellation/hold/retry-limit/invalid-result checks remain functional.

## Observed, missing and deferred

- **Python:** complexity findings, cycle, calculator clones, RoomOffer contract/shape observations and mutable globals are observed. Dictionary and static-test-impact completeness remain partial. Hidden inventory/job mutation is supported by source and behavior checks, not a dedicated mutation finding. Static references do not establish execution coverage.
- **JavaScript:** source, symbols, complexity metrics, clone pair, local cycle edges and state resources are observed. Finding rows, aggregated cycles/SCCs and these object-shape records are missing. Generic test-module/import relationships are recorded; individual node:test cases are not inventoried.
- **Ruby:** Prism symbols/spans, weighted/LOC metrics and local cycles are observed. Clone, shape, mutable-state and finding rows are missing. Recorded branch/depth/return counts are zero despite visible branches; do not interpret them as straight-line control flow. The Ruby assertion scripts run successfully but are not discovered as static test modules/cases.
- **Candidates and ownership:** mixed responsibility remains a manual review observation; all nine candidate/proposal inventories are empty. No fixture demonstrates an admitted proposal or a ready-to-apply refactor. The walkthroughs expose that gap rather than manufacturing a gate result. Negative controls identify legitimate coordination; absence of a finding is not proof of sound design.

## Browser scope

Python beginner was completed and reported as the first checkpoint before expansion. It exposed the overloaded desk method, the calculator pair, a safe cycle and shared quote history; source navigation, help and the empty Candidates state were exercised. After final source changes, all nine retained reports were rechecked against their exact current digests and explicit source roots in Chromium. Providers, dependency graph rendering/keyboard node activation, hash-bound source preview, Browse code, contextual Help/Escape and empty Candidates were checked for every level. Source files were not moved or changed to simulate absence.

Initial automated clicks at the center of SVG graph-node groups were intercepted by the surrounding SVG and timed out. Their failed attempts remain in ignored local evidence. Native focus/Enter activation worked for all nine graphs. This qualifies the keyboard route and rendering, not general pointer behavior. Tutorial instructions may use the accessible keyboard route if a node cannot be selected by pointer. No UI or analyzer code was changed to make the check pass.

The original tutorial work already exercised no-report Overview, Help, confirmed static collection and source-disabled browsing. Those earlier checks are not represented as fresh checks of every hotel project. Saved-report reopening was checked through the explicit CLI report command. General cross-browser/accessibility qualification, human first-use acceptance and the Saved runs picker were not repeated. Browser logs/screenshots stay in ignored `output/`, not the portable tutorial bundle. Successful browser automation does not close human usability acceptance.

## Explicit Python runtime collection

All three Python levels were profiled separately through the acknowledged module-based unittest workload recorded in their qualification notes. Each workload exited 0 and produced a complete profile: beginner 10 source-bound frames, intermediate 13 and advanced 17, with recorded call edges. Static missions never launch profiling. Timings are dominated by tiny workload, imports, tests and profiler overhead; no bottleneck or performance gain is claimed. JavaScript/Ruby runtime profiling was not attempted or claimed.

The preceding direct-script profiling attempts failed with workload status 1 and profile exit 100: `ModuleNotFoundError: No module named 'rooms'`. Direct `python src/demo.py` execution succeeds. The current profile script-launch context does not supply the sibling-import path used by direct execution; the existing unittest harness supplies its own source path and works as a module workload. Failed and successful runtime captures are retained under ignored `output/fawlty-qualification/profiles/`. They are not portable qualified artifacts in these bundles and were not checked in. No production workaround was introduced.

## Separate Code Analysis follow-ups

1. Investigate the profile script-launch import context for `src/` layouts; preserve the retained failing workload and successful module-workload comparison.
2. Investigate JavaScript finding/cycle aggregation and the unrepresented shape observations separately from the tutorial source.
3. Investigate Ruby branch/depth/return metrics, clone/state/shape detection and assertion-script test discovery as distinct capability gaps.
4. Review candidate-admission coverage on meaningful real or separately designed examples; these hotel projects have no admitted proposal to inspect.
5. Check pointer hit targets for graph nodes independently of keyboard navigation and distinguish painted-node clicks from SVG-group-center automation.

The tests for each hotel are bounded to that level. Product pytest still targets the repository’s `tests`, and CI scopes format/lint checks to `src tests`; no product configuration was changed. A fresh `python -m pytest --collect-only -q` collected 2,763 product tests and no tutorial test IDs. This checks separation, not execution of the full product suite. No new external services, databases, downloads, dependencies, separate Git repositories, profiling artifacts in evidence bundles, push, publication or deployment is part of this upgrade. The source and story are development material; publication review remains a separate release decision.
