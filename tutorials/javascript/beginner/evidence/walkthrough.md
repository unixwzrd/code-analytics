# Independent investigation — The Reservation Desk

Read [Sybil's incident report and assignment](../README.md). Basil has offered to write the conclusion. Politely retain control of your own report.

## Open and check the evidence

From this level directory, use `code-analysis ui evidence/actual-report.json --source-root .`. In **Overview**, confirm the language, report identity and source root. Check **Providers** and **Diagnostics** and record the completeness state. A partial, empty or unavailable view describes the available evidence; it does not settle whether the application's design is appropriate.

Open a recorded source span and confirm that the source bytes match the selected report. If they do not, stop drawing source-based conclusions until the report/root selection is corrected. This exercise uses saved evidence and needs no rescan, profiling or model.

## Conduct your investigation

1. Begin in **Modules** or **Browse code**. Choose the application entry point, then trace how Ada's request reaches a receipt. Write a small responsibility map in your own terms; follow the recorded source spans instead of inferring behavior from names.

2. Choose the views that could test your first questions. **Findings** is one starting point, but a visible warning is not required to investigate a function. Follow any useful record to source and compare its observation with what the code actually establishes.

3. Form and check two further questions about how a future booking request or hotel-policy change would pass through this system. Explore the relevant records and callers. Include at least one place you would retain as designed, with a reason.

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
