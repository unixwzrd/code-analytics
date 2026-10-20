# Optional hints — The Hotel From Hell

Try [the independent investigation](evidence/walkthrough.md) first. Open only the next hint you need; none is required to finish your report. Manuel recommends opening all three at once. Polly suggests trying the first one first.

<details>
<summary>Hint 1 — choose a view</summary>

Use **Browse code** to choose a workflow handoff; consult **State / effects** and **Test impact** for related evidence where available.

</details>

<details>
<summary>Hint 2 — choose a question</summary>

Track one request from loading through processing and back to the caller. Ask what a second submission of that request means and what would need checking in a longer session.

</details>

<details>
<summary>Hint 3 — narrow the starting point</summary>

Inspect `HotelOffice.processQueue` and its loading method in [src/hotel_queue.mjs](src/hotel_queue.mjs). Follow one normal path and one alternative path before suggesting a boundary change.

</details>

Return to [your findings checklist](evidence/walkthrough.md#consultants-findings-checklist). Compare with [SOLUTIONS.md — spoilers](SOLUTIONS.md) only after recording your conclusions.
