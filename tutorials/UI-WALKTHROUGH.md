# Sybil's investigation workflow

Start with [Python beginner](python/beginner/README.md), then follow the independent investigation linked from that level. The other eight levels use the same sequence, with different hotel scenarios. Your first deliverable is a consultant's report, not edited source.

1. Read Sybil's incident report and your assignment. Run the demo and behavior checks using the commands in the level README if you wish to observe the supplied scenario.
2. Open `code-analysis ui evidence/actual-report.json --source-root .` from the level directory. Confirm report identity, language, source binding, provider health and completeness. Saved-report investigation requires no collection or workload execution.
3. Trace the scenario independently through **Modules / Browse code**. Decide which questions to ask and which views can answer them. **Findings**, **Architecture**, **Clones**, **Contracts / Dictionary shapes**, **State / effects**, **Candidates** and **Test impact** provide different evidence where supported; this list prescribes no conclusion about the hotel.
4. Follow actual source spans and record what the evidence does and does not establish. Use contextual **Help**, then Escape back; check the selected symbol and any returning finding filter. Graphs support keyboard focus and Enter/Space when pointer selection is awkward.
5. If stuck, open the level's optional HINTS.md one step at a time. Complete its consultant's findings checklist before opening SOLUTIONS.md. Compare the reasoning and supporting records, not just a count of matching observations.
6. Only after evaluation, choose optional further work. A defensible decision to retain an existing design can be as useful as a proposed change.

## Handling incomplete or unavailable evidence

Partial, empty and unavailable results describe the limits of a particular saved view. They neither establish sound architecture nor authorize an edit. Check Providers and the recorded limitations, use source to test your interpretation, and state what remains unknown. The actual report is separate from instructional material; expected-evidence and qualification documents are spoilers to read through the solutions afterward.

Keep the supplied applications, tests and evidence intact during the initial assignment. Do not move source to simulate its absence or regenerate analytical evidence merely to browse it. The explicit report/root command avoids selecting an older local workspace by accident.

## Optional interface and runtime exploration

After completing your report, an untouched copy can support an empty-start interface check: `code-analysis ui --collection-root .` opens without a selected report. Review any command before confirming a static collection. To inspect source-unavailable behavior, open an explicit saved report in a separate session without `--source-root`; outlines/evidence should remain accessible while source preview explains its prerequisite. Leave the original source in place.

Profiling is separate, explicit opt-in work, not a requirement for this static investigation. The Python qualification notes retain the acknowledged workload commands and limitations; JavaScript/Ruby runtime profiling is not claimed. Consult those notes after the mission rather than assuming that small demo timings reveal a performance bottleneck.

For recorded qualification scope and provider-specific observations, read the [qualification matrix — spoilers](VALIDATION.md) and [baseline measurements — spoilers](BASELINES.md) after your investigation. Those preserved results do not substitute for your human first-use acceptance.
