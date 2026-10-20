# Solutions — The Reservation Desk (Python beginner)

**Spoilers. Complete [your findings checklist](evidence/walkthrough.md#consultants-findings-checklist) before reading this page.** These are evidence-backed interpretations, not required opinions about how to refactor the hotel.

> **Sybil's follow-up:** “The reservations balance. So do the receipts. Basil says that proves the system is finished. Polly has asked whether she can finish it somewhere else.”

The supplied demo and behavior tests passed in the preserved [qualification](evidence/qualification.md). No runtime booking defect is demonstrated by this exercise. The observations below can indicate technical debt, uncertain ownership or legitimate design choices. They must not be presented as fabricated application failures. Suggested improvements are directions for review, not automatic edits.

## How to read the evidence

The [retained analyzer report](evidence/actual-report.json) is authoritative for actual records. The [expected-evidence manifest](evidence/expected-evidence.json) supplies stable tutorial-local observation IDs and `supported`, `heuristic`, `deferred` or `negative_control` expectations. Those expectation labels do not declare defects or approve refactoring. Source spans below come from this report; they must be rechecked if source changes.

Distinguish direct report records from interpretation of those records, source/test observations without automatic detection, and legitimate complexity or design decisions. Python report completeness is **partial**. It contains 0 candidate records and 0 refactor proposals. An empty Candidates view is neither a recommendation nor proof that a particular admission gate failed.

## 1. Basil has occupied the whole reservation desk

**Observation — `PY-BEG-01`:** Validation, optional requests, billing, formatting and persistence share one front-desk method.

**Source:** [src/reception.py](src/reception.py), `ReceptionDesk.make_reservation` (recorded lines 44–93).

**Evidence:** The symbol's `complexity` record has physical LOC 50, weighted score 54, branch count 18 and nesting 4. Finding rows: `CA100-e5c3050b29e09e61`. Inspect **Findings** where available, and compare the actual records with the source.

**Assessment:** Directly reported metrics; the design conclusion requires interpretation. The threshold warning, where present, is heuristic. Possible technical debt, not a demonstrated booking defect.

**Why it matters:** Different policy or presentation changes require reviewing the same routine, increasing the amount of behavior a maintainer must understand and protect. The routine currently completes the supplied bookings correctly.

**Possible improvement:** Separate one stable policy decision or formatting/persistence responsibility only after tracing its inputs, callers and owner. Keep integer-penny arithmetic, discount rounding and the behavior checks unchanged; merely dividing a method into shorter pieces may keep the same coupling.

**Limits:** Metrics prompt maintenance review, not a failed booking.

## 2. Manuel has supplied two calculators

**Observation — `PY-BEG-02`:** The direct and agency calculators duplicate validation and integer rate arithmetic.

**Source:** [src/reception.py](src/reception.py) — `direct_booking_total` (recorded lines 6–20); `agency_booking_total` (recorded lines 23–37).

**Evidence:** `clone_clusters` contains `clone-cluster-62f43251ba2ce841a60a` (control-call-sequence), `clone-cluster-a02c523cebc0e3813124` (normalized-ast). These are observations of the same calculator pair, not separate billing errors. Inspect **Clones** where available, and compare the actual records with the source.

**Assessment:** Direct structural detection with an interpreted maintenance consequence; potential technical debt, not proof of incorrect totals.

**Why it matters:** If the two desks are intended to follow one rule, a future correction could reach only one copy. If their policies are meant to diverge, retaining separate ownership could be legitimate. Their presence does not charge Ada twice.

**Possible improvement:** Ask who owns direct and agency policy. Share a named calculation only if those rules are intended to change together; otherwise retain separate entry points and make the intentional policy distinction explicit in tests.

**Limits:** Clone evidence does not decide whether the two policies should share an owner.

## 3. The desk sends the paperwork in a circle

**Observation — `PY-BEG-03`:** There is a safe local import cycle, with imported values used after initialization.

**Source:** [src/billing.py](src/billing.py), `quote_stay` (recorded lines 11–24). Import declarations are in the participating files: [src/billing.py](src/billing.py); [src/reception.py](src/reception.py).

**Evidence:** `cycles` records billing → reception → billing. The corresponding module relationships can be followed in Architecture. Inspect **Architecture** where available, and compare the actual records with the source.

**Assessment:** Directly reported dependency relationship; deciding it is technical debt requires interpretation.

**Why it matters:** A change at one boundary may require understanding the other participants. This cycle uses imported values after initialization, so the working demo is not evidence of a startup failure. Intentional mutual collaboration is possible.

**Possible improvement:** Identify the direction in which policy or data should belong before moving an import. A small shared contract or a caller that coordinates both sides may remove the dependency, but moving imports inside functions solely to silence a graph would hide rather than settle ownership.

**Limits:** A cycle does not prove a startup failure.

## 4. Basil keeps a process-wide notebook

**Observation — `PY-BEG-04`:** Quoted totals or bookings remain in a shared process-global collection until explicitly cleared.

**Source:** [src/billing.py](src/billing.py), `quote_stay` (recorded lines 11–24).

**Evidence:** `state_resources` records `state-resource-e1b52ffcfbe038de168a` (`billing:QUOTES_SEEN`, mutable-global). Inspect **State / effects** where available, and compare the actual records with the source.

**Assessment:** Direct state-resource observation; the lifetime/ownership risk requires interpretation.

**Why it matters:** Independent requests or tests need an agreed lifecycle for process-wide data. Reset behavior in a small demo can make isolation look simpler than it would be in a long-running service. That is a risk to investigate, not evidence of a measured production leak.

**Possible improvement:** Give a session or hotel-operation object explicit ownership and reset/retention responsibilities if isolation is required. Preserve any intended cross-request history rather than replacing all shared collections with locals.

**Limits:** Syntax evidence does not prove concurrency safety or an unbounded production leak.

## 5. Basil and Polly disagree on the room form

**Observation — `PY-BEG-05`:** Standard room offers and annex offers represent the same domain with different keys.

**Source:** [src/rooms.py](src/rooms.py) — `RoomOffer` (recorded lines 6–10); `standard_room` (recorded lines 13–14); `annex_room` (recorded lines 17–19).

**Evidence:** `contracts` includes `contract-a4a4b6f28ceb80a32466` (typed-dict); the report also retains 7 dictionary-shape records across this project. Compare the room producer/access evidence, not just the project-wide count. Inspect **Contracts / Dictionary shapes** where available, and compare the actual records with the source.

**Assessment:** Interpretation of reported declarations/access observations; possible representation debt, not a detected runtime contract violation.

**Why it matters:** Equivalent business information reaches consumers through different field names. A tariff or availability change may need updates to several branches. Supporting a legacy annex format can also be a deliberate compatibility choice.

**Possible improvement:** Consider normalizing at the room-data boundary or introducing a clearly owned room-offer interface, while preserving the supported annex input and its prices. A TypedDict declaration alone does not prove every runtime value conforms.

**Limits:** The RoomOffer declaration and shape observations are descriptive, not a proven runtime violation.

## 6. Polly adjusts the actual room notebook

**Observation — `PY-BEG-06`:** Reservation decrements the caller-owned room mapping in place; behavior checks verify object identity and remaining rooms.

**Source:** [src/rooms.py](src/rooms.py), `reserve` (recorded lines 30–38).

**Evidence:** The report retains the referenced symbol/span. The source updates the passed mapping/object through a selected availability field; the existing level-local behavior tests check identity and the remaining inventory. No dedicated argument-mutation finding is emitted. Inspect **Browse code** where available, and compare the actual records with the source.

**Assessment:** Source inspection plus existing functional-test evidence; the surprise/ownership judgment is interpretive, not an automatic defect detection.

**Why it matters:** A caller retaining the same object sees the availability change. That can surprise a caller expecting a quotation-only operation, but is legitimate if the object is the authoritative inventory record.

**Possible improvement:** Clarify whether this operation reserves real inventory or produces an independent offer. Make mutation explicit in the interface, or return a changed copy if value semantics are intended; do not silently change shared-inventory behavior.

**Limits:** The dynamic stock field has no dedicated argument-mutation finding; source and tests supply that evidence.

## 7. Polly coordinates one successful booking

**Observation — `PY-BEG-07`:** The short demo legitimately coordinates a booking and disposable receipt persistence.

**Source:** [src/demo.py](src/demo.py), `run_demo` (recorded lines 9–17).

**Evidence:** The symbol and source span are retained. No `findings` row targets `run_demo`; this project has 0 `refactor_proposals`. This is an intentional negative control, not a ranking result. Inspect **Browse code** where available, and compare the actual records with the source.

**Assessment:** Legitimate orchestration in the demonstrated use case. Absence of a finding is not proof that every design choice is optimal.

**Why it matters:** One application entry point must coordinate the successful booking and temporary receipt. Condemning every sequence of calls would reward extra indirection without a clearer responsibility boundary.

**Possible improvement:** Keep the coordinator unless an actual ownership or reuse requirement justifies a change. Protect the externally observed receipt, total, remaining availability and deterministic cleanup behavior.

**Limits:** Orchestration is intentional; no finding targets this coordinator. Absence of a finding is not proof of design quality.

## 8. One desk owns billing and the filing cabinet

**Observation — `PY-BEG-08`:** One class owns guest-facing presentation, receipt state and persistence as well as billing coordination.

**Source:** [src/reception.py](src/reception.py), `ReceptionDesk` (recorded lines 40–97).

**Evidence:** The class and method spans are recorded, but the report has 0 `refactor_candidates` and 0 `refactor_proposals`. Candidates is empty. There is no admitted proposal or specific admission-gate decision to infer from that emptiness. Inspect **Candidates / Browse code** where available, and compare the actual records with the source.

**Assessment:** Responsibility/technical-debt interpretation from source. No responsibility-specific class finding or approved refactor is established.

**Why it matters:** Business rules, guest-facing formatting and file persistence can be requested by different owners and change for different reasons. One class may be entirely reasonable at this size; the concern is the future change boundary, not its name.

**Possible improvement:** Trace likely changes and decide whether a receipt formatter or repository boundary would reduce the actual review burden. Keep the present class if the extra abstraction would cost more than the problem it solves.

**Limits:** No refactor proposal is emitted. Review ownership manually; the empty Candidates view is not approval.

## Optional further investigation

Choose one question, not a wholesale rewrite:

- Compare a maintenance change you would recommend with one you would reject. Explain the ownership benefit and the extra abstraction cost rather than using line count alone.
- Trace an alternative booking path through the existing behavior checks. Distinguish what the tests execute from what static test references merely suggest.
- Investigate an unanswered provider or runtime question using the preserved [qualification notes](evidence/qualification.md). Keep missing evidence separate from a claim of correct design.

For an explicitly chosen later improvement experiment, preserve the original report and work in a copy. Keep the level's documented demo/test commands and business behavior. Compare compatible before/after scans at the same settings; do not overwrite this supplied evidence bundle just to make a score smaller. Common reproduction details and known limits are in the [shared workflow](../../UI-WALKTHROUGH.md), [qualification matrix — spoilers](../../VALIDATION.md) and [baseline measurements — spoilers](../../BASELINES.md).

Give Sybil your rationale, uncertainties and a bounded next step. Basil's confidence is useful context, not a qualification result.
