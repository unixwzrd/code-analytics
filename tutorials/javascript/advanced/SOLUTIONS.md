# Solutions — The Hotel From Hell (Javascript advanced)

**Spoilers. Complete [your findings checklist](evidence/walkthrough.md#consultants-findings-checklist) before reading this page.** These are evidence-backed interpretations, not required opinions about how to refactor the hotel.

> **Sybil's follow-up:** “A repeated booking request gets its cached receipt. Basil calls this efficiency. Polly asks who owns the cached object. Basil has scheduled a meeting about the question.”

The supplied demo and behavior tests passed in the preserved [qualification](evidence/qualification.md). No runtime booking defect is demonstrated by this exercise. The observations below can indicate technical debt, uncertain ownership or legitimate design choices. They must not be presented as fabricated application failures. Suggested improvements are directions for review, not automatic edits.

## How to read the evidence

The [retained analyzer report](evidence/actual-report.json) is authoritative for actual records. The [expected-evidence manifest](evidence/expected-evidence.json) supplies stable tutorial-local observation IDs and `supported`, `heuristic`, `deferred` or `negative_control` expectations. Those expectation labels do not declare defects or approve refactoring. Source spans below come from this report; they must be rechecked if source changes.

Distinguish direct report records from interpretation of those records, source/test observations without automatic detection, and legitimate complexity or design decisions. Javascript report completeness is **complete**. It contains 0 candidate records and 0 refactor proposals. An empty Candidates view is neither a recommendation nor proof that a particular admission gate failed.

## 1. Basil has expanded the tariff rulebook

**Observation — `JS-ADV-01`:** The tariff routine nests guest tier, room representation, group rates, coupons and statement-delivery policy.

**Source:** [src/billing.mjs](src/billing.mjs), `quoteStay` (recorded lines 8–105).

**Evidence:** The symbol's `complexity` record has physical LOC 98, weighted score 75, branch count 26 and nesting 8. No finding row targets this symbol; this is recorded metric/source evidence. Inspect **Modules / Browse code** where available, and compare the actual records with the source.

**Assessment:** Directly reported metrics; the design conclusion requires interpretation. The threshold warning, where present, is heuristic. Possible technical debt, not a demonstrated booking defect.

**Why it matters:** Different policy or presentation changes require reviewing the same routine, increasing the amount of behavior a maintainer must understand and protect. The routine currently completes the supplied bookings correctly.

**Possible improvement:** Separate one stable policy decision or formatting/persistence responsibility only after tracing its inputs, callers and owner. Keep integer-penny arithmetic, discount rounding and the behavior checks unchanged; merely dividing a method into shorter pieces may keep the same coupling.

**Limits:** Metrics and source are available but finding rows are absent.

## 2. Manuel has supplied two calculators

**Observation — `JS-ADV-02`:** The direct and agency calculators duplicate validation and integer rate arithmetic.

**Source:** [src/reception.mjs](src/reception.mjs) — `directBookingTotal` (recorded lines 5–19); `agencyBookingTotal` (recorded lines 21–35).

**Evidence:** `clone_clusters` contains `clone-cluster-66df4ba8ff6408b00827` (normalized-ast). These are observations of the same calculator pair, not separate billing errors. Inspect **Clones** where available, and compare the actual records with the source.

**Assessment:** Direct structural detection with an interpreted maintenance consequence; potential technical debt, not proof of incorrect totals.

**Why it matters:** If the two desks are intended to follow one rule, a future correction could reach only one copy. If their policies are meant to diverge, retaining separate ownership could be legitimate. Their presence does not charge Ada twice.

**Possible improvement:** Ask who owns direct and agency policy. Share a named calculation only if those rules are intended to change together; otherwise retain separate entry points and make the intentional policy distinction explicit in tests.

**Limits:** Clone evidence does not decide whether the two policies should share an owner.

## 3. The desk sends the paperwork in a circle

**Observation — `JS-ADV-03`:** There is a safe local import cycle, with imported values used after initialization.

**Source:** [src/billing.mjs](src/billing.mjs), `quoteStay` (recorded lines 8–105). Import declarations are in the participating files: [src/billing.mjs](src/billing.mjs); [src/bookings.mjs](src/bookings.mjs); [src/rooms.mjs](src/rooms.mjs).

**Evidence:** `cycles` and `strongly_connected_components` contain no records here. Local imports in `import_edges` still form the source-level relationship; the JavaScript aggregation is missing. Inspect **Architecture** where available, and compare the actual records with the source.

**Assessment:** Source/import-edge interpretation with deferred aggregate detection; not a detected cycle finding.

**Why it matters:** A change at one boundary may require understanding the other participants. This cycle uses imported values after initialization, so the working demo is not evidence of a startup failure. Intentional mutual collaboration is possible.

**Possible improvement:** Identify the direction in which policy or data should belong before moving an import. A small shared contract or a caller that coordinates both sides may remove the dependency, but moving imports inside functions solely to silence a graph would hide rather than settle ownership.

**Limits:** All local edges are recorded, but the report has no aggregated cycle or SCC record.

## 4. Polly traces the individual handoffs

**Observation — `JS-ADV-03-EDGES`:** The individual local imports trace the source-level cycle.

**Source:** [src/billing.mjs](src/billing.mjs), `quoteStay` (recorded lines 8–105). Import declarations are in the participating files: [src/billing.mjs](src/billing.mjs); [src/bookings.mjs](src/bookings.mjs); [src/rooms.mjs](src/rooms.mjs).

**Evidence:** `import_edges` records rooms.mjs → billing.mjs; billing.mjs → bookings.mjs; bookings.mjs → rooms.mjs. There is still no aggregated JavaScript cycle/SCC record. Inspect **Architecture** where available, and compare the actual records with the source.

**Assessment:** Directly reported edges; interpreting the complete relationship is separate from automatic cycle detection.

**Why it matters:** These edges let a consultant reconstruct how modules depend on one another even when the aggregate view has a capability gap. Missing aggregation must not be reported as an absent source relationship.

**Possible improvement:** Use the concrete edge directions to discuss a clearer policy boundary. Record the aggregation gap as a separate analyzer question; do not change imports merely to manufacture a tutorial finding.

**Limits:** This checks edges separately from the missing cycle aggregation.

## 5. Basil keeps a process-wide notebook

**Observation — `JS-ADV-04`:** Quoted totals or bookings remain in a shared process-global collection until explicitly cleared.

**Source:** [src/bookings.mjs](src/bookings.mjs), `createBooking` (recorded lines 6–13).

**Evidence:** `state_resources` records `state-resource-c0eb60c06703dda69c26` (`bookings.mjs:bookings`, mutable-global). Inspect **State / effects** where available, and compare the actual records with the source.

**Assessment:** Direct state-resource observation; the lifetime/ownership risk requires interpretation.

**Why it matters:** Independent requests or tests need an agreed lifecycle for process-wide data. Reset behavior in a small demo can make isolation look simpler than it would be in a long-running service. That is a risk to investigate, not evidence of a measured production leak.

**Possible improvement:** Give a session or hotel-operation object explicit ownership and reset/retention responsibilities if isolation is required. Preserve any intended cross-request history rather than replacing all shared collections with locals.

**Limits:** Syntax evidence does not prove concurrency safety or an unbounded production leak.

## 6. Basil and Polly disagree on the room form

**Observation — `JS-ADV-05`:** Standard room offers and annex offers represent the same domain with different keys.

**Source:** [src/rooms.mjs](src/rooms.mjs) — `standardRoom` (recorded lines 4–6); `annexRoom` (recorded lines 8–10).

**Evidence:** `contracts` and `dictionary_shapes` have no entries for these objects/hashes. Standard and annex field names are visible in source; no automatic contract mismatch was reported. Inspect **Contracts / Dictionary shapes** where available, and compare the actual records with the source.

**Assessment:** Source-inspection observation with deferred automatic shape/contract inference.

**Why it matters:** Equivalent business information reaches consumers through different field names. A tariff or availability change may need updates to several branches. Supporting a legacy annex format can also be a deliberate compatibility choice.

**Possible improvement:** Consider normalizing at the room-data boundary or introducing a clearly owned room-offer interface, while preserving the supported annex input and its prices. A TypedDict declaration alone does not prove every runtime value conforms.

**Limits:** These object/hash shapes have no contract or dictionary-shape records in this provider.

## 7. Polly adjusts the actual room notebook

**Observation — `JS-ADV-06`:** Reservation decrements the caller-owned room mapping in place; behavior checks verify object identity and remaining rooms.

**Source:** [src/rooms.mjs](src/rooms.mjs), `reserve` (recorded lines 16–22).

**Evidence:** The report retains the referenced symbol/span. The source updates the passed mapping/object through a selected availability field; the existing level-local behavior tests check identity and the remaining inventory. No dedicated argument-mutation finding is emitted. Inspect **Browse code** where available, and compare the actual records with the source.

**Assessment:** Source inspection plus existing functional-test evidence; the surprise/ownership judgment is interpretive, not an automatic defect detection.

**Why it matters:** A caller retaining the same object sees the availability change. That can surprise a caller expecting a quotation-only operation, but is legitimate if the object is the authoritative inventory record.

**Possible improvement:** Clarify whether this operation reserves real inventory or produces an independent offer. Make mutation explicit in the interface, or return a changed copy if value semantics are intended; do not silently change shared-inventory behavior.

**Limits:** The dynamic stock field has no dedicated argument-mutation finding; source and tests supply that evidence.

## 8. Polly coordinates one successful booking

**Observation — `JS-ADV-07`:** The short demo legitimately coordinates a booking and disposable receipt persistence.

**Source:** [src/demo.mjs](src/demo.mjs), `runDemo` (recorded lines 12–27).

**Evidence:** The symbol and source span are retained. No `findings` row targets `runDemo`; this project has 0 `refactor_proposals`. This is an intentional negative control, not a ranking result. Inspect **Browse code** where available, and compare the actual records with the source.

**Assessment:** Legitimate orchestration in the demonstrated use case. Absence of a finding is not proof that every design choice is optimal.

**Why it matters:** One application entry point must coordinate the successful booking and temporary receipt. Condemning every sequence of calls would reward extra indirection without a clearer responsibility boundary.

**Possible improvement:** Keep the coordinator unless an actual ownership or reuse requirement justifies a change. Protect the externally observed receipt, total, remaining availability and deterministic cleanup behavior.

**Limits:** Orchestration is intentional; no finding targets this coordinator. Absence of a finding is not proof of design quality.

## 9. One desk owns billing and the filing cabinet

**Observation — `JS-ADV-08`:** One class owns guest-facing presentation, receipt state and persistence as well as billing coordination.

**Source:** [src/reception.mjs](src/reception.mjs), `ReceptionDesk` (recorded lines 37–54).

**Evidence:** The class and method spans are recorded, but the report has 0 `refactor_candidates` and 0 `refactor_proposals`. Candidates is empty. There is no admitted proposal or specific admission-gate decision to infer from that emptiness. Inspect **Candidates / Browse code** where available, and compare the actual records with the source.

**Assessment:** Responsibility/technical-debt interpretation from source. No responsibility-specific class finding or approved refactor is established.

**Why it matters:** Business rules, guest-facing formatting and file persistence can be requested by different owners and change for different reasons. One class may be entirely reasonable at this size; the concern is the future change boundary, not its name.

**Possible improvement:** Trace likely changes and decide whether a receipt formatter or repository boundary would reduce the actual review burden. Keep the present class if the extra abstraction would cost more than the problem it solves.

**Limits:** No refactor proposal is emitted. Review ownership manually; the empty Candidates view is not approval.

## 10. Manuel copied the guest checks too

**Observation — `JS-ADV-09`:** Booking creation and guest preview repeat name presence, type and blank checks.

**Source:** [src/bookings.mjs](src/bookings.mjs), `previewGuest` (recorded lines 15–20).

**Evidence:** The source spans retain this preview helper and booking creation's repeated name checks. `validation_patterns` contains 0 entries; no validation-pattern finding for these checks is promised or emitted. Inspect **Browse code** where available, and compare the actual records with the source.

**Assessment:** Source-inspection observation; possible validation-policy debt requiring interpretation.

**Why it matters:** Presence, string type and nonblank checks can drift when guest policy changes. Similar syntax is not proof that both entry points should accept precisely the same contract.

**Possible improvement:** Document the intended entry-point contracts, then centralize a guest validator only if the rules and ownership really match. Preserve accepted names and current error behavior in the tests.

**Limits:** The repeated source is visible, but no validation-pattern record is promised.

## 11. Sybil requests a queue; Basil adds departments

**Observation — `JS-ADV-10`:** Cancellation, cached replay, guest holds, retry limits, booking creation and event history interact in one processor.

**Source:** [src/hotel_queue.mjs](src/hotel_queue.mjs), `HotelOffice.processQueue` (recorded lines 16–65).

**Evidence:** The symbol's `complexity` record has physical LOC 50, weighted score 31, branch count 7 and nesting 7. No finding row targets this symbol; this is recorded metric/source evidence. Inspect **Modules / Browse code** where available, and compare the actual records with the source.

**Assessment:** Directly reported metrics; the design conclusion requires interpretation. The threshold warning, where present, is heuristic. Possible technical debt, not a demonstrated booking defect.

**Why it matters:** Different policy or presentation changes require reviewing the same routine, increasing the amount of behavior a maintainer must understand and protect. The routine currently completes the supplied bookings correctly.

**Possible improvement:** Separate one stable policy decision or formatting/persistence responsibility only after tracing its inputs, callers and owner. Keep integer-penny arithmetic, discount rounding and the behavior checks unchanged; merely dividing a method into shorter pieces may keep the same coupling.

**Limits:** A weighted score does not establish a runtime bottleneck; Ruby branch metrics are incomplete.

## 12. The queue has three competing notebooks

**Observation — `JS-ADV-11`:** Pending jobs, receipt cache and event history are module-owned mutable resources.

**Source:** [src/hotel_queue.mjs](src/hotel_queue.mjs), `HotelOffice.processQueue` (recorded lines 16–65).

**Evidence:** `state_resources` records `state-resource-ff2184469db29d9204d8` (`hotel_queue.mjs:pending`, mutable-global). Inspect **State / effects** where available, and compare the actual records with the source.

**Assessment:** Direct state-resource observation; the lifetime/ownership risk requires interpretation.

**Why it matters:** Independent requests or tests need an agreed lifecycle for process-wide data. Reset behavior in a small demo can make isolation look simpler than it would be in a long-running service. That is a risk to investigate, not evidence of a measured production leak.

**Possible improvement:** Give a session or hotel-operation object explicit ownership and reset/retention responsibilities if isolation is required. Preserve any intended cross-request history rather than replacing all shared collections with locals.

**Limits:** Static ownership does not prove safe reentrancy, cache lifetime or cleanup outcomes.

## 13. Polly receives the same job objects back

**Observation — `JS-ADV-12`:** Loading keeps caller job references; processing mutates attempts/state and returns different success and skipped-result shapes.

**Source:** [src/hotel_queue.mjs](src/hotel_queue.mjs), `HotelOffice.load` (recorded lines 12–14).

**Evidence:** The `load` symbol/span is retained. Source keeps references to caller jobs; processing changes attempts/state, and success/held/cancelled results have different fields. Existing advanced tests check alias effects. The report retains 0 shape records overall; that does not by itself prove this lifecycle or its timing. Inspect **Dictionary shapes / Browse code** where available, and compare the actual records with the source.

**Assessment:** Source/test-based alias and result-contract interpretation. Broad lifecycle correctness is not automatically detected.

**Why it matters:** The caller can observe job changes and receive shared cached result objects. That may be intentional workflow ownership, but it needs an explicit contract before concurrent or repeated use grows more complicated.

**Possible improvement:** Decide whether jobs are commands handed over to the hotel or caller-owned values. Define result variants and cache ownership explicitly, preserving repeated-ID replay and the tested cancellation/hold/retry-limit outcomes.

**Limits:** Tests prove the demonstrated alias effects; broad lifecycle correctness and non-Python shape inference are unestablished.

## Optional further investigation

Choose one question, not a wholesale rewrite:

- Compare a maintenance change you would recommend with one you would reject. Explain the ownership benefit and the extra abstraction cost rather than using line count alone.
- Trace an alternative booking path through the existing behavior checks. Distinguish what the tests execute from what static test references merely suggest.
- Investigate an unanswered provider or runtime question using the preserved [qualification notes](evidence/qualification.md). Keep missing evidence separate from a claim of correct design.

For an explicitly chosen later improvement experiment, preserve the original report and work in a copy. Keep the level's documented demo/test commands and business behavior. Compare compatible before/after scans at the same settings; do not overwrite this supplied evidence bundle just to make a score smaller. Common reproduction details and known limits are in the [shared workflow](../../UI-WALKTHROUGH.md), [qualification matrix — spoilers](../../VALIDATION.md) and [baseline measurements — spoilers](../../BASELINES.md).

Give Sybil your rationale, uncertainties and a bounded next step. Basil's confidence is useful context, not a qualification result.
