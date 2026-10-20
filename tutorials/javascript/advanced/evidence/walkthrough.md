# Independent investigation — The Hotel From Hell

Read [Sybil's incident report and assignment](../README.md). Basil has offered to write the conclusion. Politely retain control of your own report.

## Open and check the evidence

From this level directory, use `code-analysis ui evidence/actual-report.json --source-root .`. In **Overview**, confirm the language, report identity and source root. Check **Providers** and **Diagnostics** and record the completeness state. A partial, empty or unavailable view describes the available evidence; it does not settle whether the application's design is appropriate.

Open a recorded source span and confirm that the source bytes match the selected report. If they do not, stop drawing source-based conclusions until the report/root selection is corrected. This exercise uses saved evidence and needs no rescan, profiling or model.

## Conduct your investigation

1. Trace a group reservation, a cancellation and a repeated request through **Modules / Browse code**. Note who is responsible at each handoff and what the caller can observe. Keep a distinction between the demonstrated examples and assumptions about a longer session.

2. Choose evidence views to test your account of the workflow. **Architecture**, **State / effects**, **Dictionary shapes** and **Findings** offer different kinds of evidence where supported. Follow relevant spans and record the observations before deciding whether a change would help.

3. Review the available **Test impact** and **Candidates** information under **Review & validate**. Explain what it supports and what remains unqualified. Propose one further question you would test before recommending a change, and one part of the current workflow you would retain.

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
