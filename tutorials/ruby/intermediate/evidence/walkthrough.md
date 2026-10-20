# Independent investigation — The Booking Mix-Up

Read [Sybil's incident report and assignment](../README.md). Basil has offered to write the conclusion. Politely retain control of your own report.

## Open and check the evidence

From this level directory, use `code-analysis ui evidence/actual-report.json --source-root .`. In **Overview**, confirm the language, report identity and source root. Check **Providers** and **Diagnostics** and record the completeness state. A partial, empty or unavailable view describes the available evidence; it does not settle whether the application's design is appropriate.

Open a recorded source span and confirm that the source bytes match the selected report. If they do not, stop drawing source-based conclusions until the report/root selection is corrected. This exercise uses saved evidence and needs no rescan, profiling or model.

## Conduct your investigation

1. Use **Modules / Browse code** to trace a standard-room booking, then the annex example. Sketch where guest information, room availability and tariff decisions are used. Note any assumptions you need to verify; do not decide in advance that the paths ought to be identical.

2. Choose evidence views that can test your map: **Architecture**, **Findings**, **Contracts / Dictionary shapes**, **State / effects** or **Clones** may be relevant. Record only what you actually observe, and open its source before drawing a design conclusion.

3. Imagine a requested tariff change. Identify which responsibilities and behavior checks you would review, and why. Contrast one place you might change with one you would leave alone. If the evidence cannot answer a question, record the uncertainty instead of guessing.

Use contextual **Help** when a view is unfamiliar. Escape should return to your selected source; if you arrived from a finding, check the filter after **Return to Findings**. Graph nodes support keyboard focus and Enter/Space if pointer selection is awkward. These are navigation checks, not architectural conclusions.

If you need assistance, use [HINTS.md](../HINTS.md), starting with Hint 1. You can complete the assignment without opening it. Keep the answer key closed until you have written your own account.

## Consultant's findings checklist

- [ ] Record what you investigated and which questions guided you.
- [ ] Cite the actual view/record and state what evidence you found, including unavailable or incomplete evidence.
- [ ] Identify supporting source files, symbols and recorded spans.
- [ ] Separate demonstrated behavior, maintenance risks and assumptions.
- [ ] State what you would investigate next and which behavior checks would help.
- [ ] Recommend a change, or explain why you would retain the design, with evidence and a reason.

Deliver a concise report to Sybil before comparing it with [SOLUTIONS.md — spoilers](../SOLUTIONS.md). Different recommendations can be defensible. A useful report explains the boundary of its evidence instead of merely collecting warnings.

After comparison, the solutions offer optional further investigations. Source, tests, actual report, expected-evidence manifest and existing qualification remain the authoritative supplied material; nothing in this walkthrough performs an automatic edit.
