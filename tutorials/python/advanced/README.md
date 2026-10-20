# The Hotel From Hell — Python advanced

> **Sybil's incident report:** “The group reservation and cancellation both work. A busy weekend is coming. Please investigate before Basil announces that the system can run the entire holiday season unattended.”

Basil is confident the hotel software works. Manuel is eager to help, Polly is keeping operations moving, and Sybil has hired you to explain what they have built before the next change.

**Your assignment:** Follow a group reservation, a cancellation and a repeated request. Explain the responsibilities involved and what you would investigate before relying on the system for a longer, busier session. Separate demonstrated behavior from assumptions. Investigate and document first; do not change the application during this assignment.

## Launch the hotel and Code Analysis

Run these commands from this level directory with the existing runtimes and Code Analysis installation:

```bash
python src/demo.py
python -m unittest discover -s tests -v
code-analysis ui evidence/actual-report.json --source-root .
```

Ada's baseline is two standard rooms for one night: **£21.60**, one saved receipt and **six rooms remaining**. Start with [the independent investigation](evidence/walkthrough.md). It includes the checklist for your report to Sybil. Source is in `src/`; this level's behavior checks are in `tests/`.

## Investigate, then compare

Work without assistance first. If you get stuck, open [optional progressive hints](HINTS.md), one at a time. Complete [your findings checklist](evidence/walkthrough.md#consultants-findings-checklist) before opening [SOLUTIONS.md — spoilers](SOLUTIONS.md). Agreement with every suggested improvement is not required; support your own conclusions with evidence.

After comparing your report, choose an [optional further investigation](SOLUTIONS.md#optional-further-investigation). Common setup, the shared workflow and operational notes are in the [Python guide](../README.md) and [tutorial guide](../../README.md); you do not need either page to find the launch commands above.
