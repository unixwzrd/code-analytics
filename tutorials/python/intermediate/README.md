# The Booking Mix-Up — Python intermediate

> **Sybil's incident report:** “Today's room bookings are correct. Next week we may change the tariff. Basil says it will take a moment; Polly has asked which moment he intends to use.”

Basil is confident the hotel software works. Manuel is eager to help, Polly is keeping operations moving, and Sybil has hired you to explain what they have built before the next change.

**Your assignment:** Trace the standard-room and annex booking paths. Explain where guest information, availability and hotel policy are used, and what you would review before changing the tariff. Let the evidence determine your conclusions. Investigate and document first; do not change the application during this assignment.

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
