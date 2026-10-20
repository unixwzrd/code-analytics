# The Booking Mix-Up — Ruby intermediate

> **Sybil's incident report:** “The annex bookings are correct. Basil assures me next week's rate change will be straightforward. Polly would like that assurance accompanied by a map.”

Basil is confident the hotel software works. Manuel is eager to help, Polly is keeping operations moving, and Sybil has hired you to explain what they have built before the next change.

**Your assignment:** Trace the standard-room and annex booking paths. Explain where guest information, availability and hotel policy are used, and what you would review before changing the tariff. Let the evidence determine your conclusions. Investigate and document first; do not change the application during this assignment.

## Launch the hotel and Code Analysis

Run these commands from this level directory with the existing runtimes and Code Analysis installation:

```bash
ruby lib/demo.rb
ruby test/test_demo.rb
code-analysis ui evidence/actual-report.json --source-root .
```

Ada's baseline is two standard rooms for one night: **£21.60**, one saved receipt and **six rooms remaining**. Start with [the independent investigation](evidence/walkthrough.md). It includes the checklist for your report to Sybil. Source is in `lib/`; this level's behavior checks are in `test/`.

## Investigate, then compare

Work without assistance first. If you get stuck, open [optional progressive hints](HINTS.md), one at a time. Complete [your findings checklist](evidence/walkthrough.md#consultants-findings-checklist) before opening [SOLUTIONS.md — spoilers](SOLUTIONS.md). Agreement with every suggested improvement is not required; support your own conclusions with evidence.

After comparing your report, choose an [optional further investigation](SOLUTIONS.md#optional-further-investigation). Common setup, the shared workflow and operational notes are in the [Ruby guide](../README.md) and [tutorial guide](../../README.md); you do not need either page to find the launch commands above.
