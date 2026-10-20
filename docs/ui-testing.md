# UI testing and usability review

This is the repeatable Web UI test entry point. It combines real-browser journeys with the service, authorization, source-binding, collection and documentation contracts those journeys depend on. It does not replace the complete analyzer suite or human usability review.

## Run it

From the repository root, using the interpreter in which this checkout and its test dependencies are already available:

```bash
python tools/test_ui.py --smoke
python tools/test_ui.py
```

For the local qualification environment, replace `python` with `/path/to/venvutil/bin/python`. Run the smoke gate while changing UI behavior and the full UI gate before accepting an integrated UI checkpoint. Neither command installs or upgrades anything.

Both gates first launch and close Chromium and WebKit using the installed Playwright. Missing dependencies or browser binaries leave qualification incomplete, with a nonzero status and a diagnostic receipt. The full suite also needs the optional tools used by its existing integration fixtures (including runtime, JavaScript, terminal-handoff and Markdown documentation test dependencies); ordinary application usage does not require these developer dependencies. CI provisions its own isolated environment separately. Do not treat CI configuration as permission to install packages locally.

Useful inventory and focused commands:

```bash
python tools/test_ui.py --list
python -m pytest -m ui --collect-only -q
python -m pytest -m ui_browser -q
python -m pytest tests/test_collection_commands.py -k picker_up -q
```

`ui` includes every discovered real-browser module and supporting UI contracts. `ui_browser` selects modules containing real-browser checks, including their HTTP/service tests. `ui_smoke` is the deliberately small set of critical journeys. These are ordinary registered pytest markers, not new application commands. Direct pytest is useful for diagnosis but can conventionally succeed with skips; use `tools/test_ui.py` for qualification.

The internal `ui_direct_browser` marker identifies trusted test bodies that explicitly invoke Playwright. Execution evidence records those declarations separately from support cases; this is not runtime instrumentation of browser internals. At least one declared direct-browser case must pass, and support-only inventories cannot qualify the UI. Ordinary targeted pytest classifies only the files actually collected, so an unrelated unfinished test does not break a focused run. The full gate inventories all UI candidates fail-closed.

The shared inventory in `tests/ui_suite.py` recognizes actual Playwright import/importorskip syntax, explicit UI declarations and the existing UI/Web/collection naming conventions. New browser modules are included automatically; a string merely mentioning Playwright is not sufficient. New support-only tests outside those conventions should declare `pytest.mark.ui` or be added to the support inventory. Use `pytest.mark.ui_smoke` sparingly for a critical, short journey. Selection is shared by local runs and CI; there is no second handwritten CI test list.

## What is checked

| Area | Observable acceptance |
| --- | --- |
| Starting and navigation | Empty/static/profile/combined sessions retain their real evidence states; Overview offers an appropriate action; all 18 view identities remain reachable, with purpose, readiness and corrective guidance. |
| Project/path selection | Display the actual directory and collection-root boundary; keyboard and double-click opening, repeated Up and root shortcut work; browsing does not silently apply a field, create directories, escape the root or execute code. |
| Collection | Inputs, Advanced settings, summaries, exact command preview, explicit confirmations and execution acknowledgment retain their existing validation; settings return without restoring authorization or stale previews. |
| Jobs and server lifecycle | Launch/reconnect/cancel/result opening, ownership checks, retained diagnostics, detached startup/reopen/explicit stop and consent remain correct; closing a tab is not shutdown. |
| Analytical views | Filters, paging, long values, literal text, disclosures, column changes, table/graph bounds, downloads and saved-report identities remain intact. Partial/unavailable evidence is not a successful empty result. |
| Investigation and source | Finding-to-evidence/source and Back/Forward/Return preserve the selected run, scope, paging and focus; changed/unbound source is not disclosed as verified. |
| Comparison | Exact baseline/candidate identity, filtering/export, explicit source choices and previews, return after column changes, and cancelled/stale responses remain correct. |
| Runtime | Saved hotspots, calls, profile comparisons, source binding, downloads and missing measurements retain provenance; opening views never launches a workload. |
| Help | Focused guide first, explicit full reference, safe links, offline resources, history/search/scroll, Escape and restored focus; errors stay local without blocking navigation or losing a draft/job. |
| Drafts | Dirty/composing navigation guards, selection, undo/redo and explicit copy/download preserve the session-only boundary; no source writes or browser-storage persistence. |
| Layout and keyboard | Desktop is primary and tablet secondary, in both themes and engines for the dedicated layout matrices; controls stay task-sized, actions adjacent and analytical workspaces useful. Existing small-screen regressions remain, without making phone polish the primary target. |
| Security and failure | Authentication, same-origin policy, closed routes, path confinement, literal markup-shaped values, malformed artifacts and delayed-response cancellation retain their fail-closed behavior. |

The full suite includes existing feature-specific tests rather than replacing them with one superficial “click every tab” test. The all-view keyboard smoke journey additionally checks discoverability, explanations and contextual guide access. Not every historical browser case is parameterized over both engines/themes; the dedicated cross-engine/layout cases and preflight establish the stated matrix, not exhaustive coverage of every possible combination. These are automated usability heuristics, not proof of human comprehension or a complete accessibility audit. Real Safari/hardware IME and unfamiliar-user outcomes remain manual gates.

## Results and artifacts

Each invocation creates a unique ignored directory under `output/ui-tests/`; `--output-dir PATH` selects another artifact parent. Existing runs are never overwritten or automatically pruned. Source/test/tool directories cannot be used as output parents.

Each run retains:

- `result.json`: strict local qualification receipt with full/smoke scope, result, interpreter and tool/browser versions, exact command, native pytest status, counts, limitations and artifact hashes.
- `pytest.log` and `junit.xml`: actual execution output, failures, errors and skipped/expected-failure reasons when pytest ran.
- `cases.json`: expected case identities before selection, the cases actually selected, their setup/call/teardown outcomes and declared direct-browser status. Hidden deselection, extra cases, unrun cases and count disagreement cannot qualify as passed.
- `inputs-before.json` and `inputs-after.json`: sorted named regular-file hashes for source/resources, tests, tooling and configuration, excluding ephemeral cache/workspace directories. A changed snapshot cannot qualify as passed.

New or rewritten images observed under `output/playwright/` during the gate are copied into the unique run's `captures/` directory and included in its artifact hashes. Unchanged older captures are excluded, and originals are preserved. Repeated historical filenames are not freshness evidence. Copies are bounded to 5,000 inventoried images and 64 MiB per image; changed-during-read or over-bound captures make qualification incomplete rather than silently disappearing. Not every failure has a screenshot, and this inventory is run-interval evidence rather than per-case ownership instrumentation. Run UI gates serially in a checkout; concurrent direct browser tests or other screenshot writers are not supported provenance. Logs/JUnit/case evidence and the unique receipt are the per-run authority. A screenshot alone is not a functional assertion. CI uploads only the run directories, not an unrelated historical screenshot tree. The receipt contains local paths and is operational evidence, not a portable analytical report or a change to the analysis schema. It records no environment-variable values.

Skipped tests, expected failures, unavailable tools and zero executed tests are incomplete qualification, never a green gate. Unexpected passes are strict failures. The runner preserves native pytest status in JSON, returning host `EPROTO` for failed qualification, `ENODATA` for incomplete qualification, `EINTR` for interruption and zero for a completed passing gate. A passing smoke result is explicitly labelled smoke and is not full UI acceptance. Analytical CLI errno meanings remain unchanged.

The runner clears inherited `PYTEST_ADDOPTS` and configured `addopts`, then explicitly supplies strict configuration/marker checks, so an old shell/configuration setting such as `--collect-only`, `--lf` or `-k` cannot silently weaken the recorded gate. Expected/executed case reconciliation additionally detects selection drift. Use the direct focused pytest commands for intentional subsets. The application is not launched and no real project is collected merely by listing the suite or probing browser availability.

All automated collections and workloads use temporary synthetic fixtures or saved synthetic JSON. Do not point this suite at MLXForge, another consumer or a user's live project. Workloads requiring execution are already explicitly authorized inside their bounded fixtures. LocalOps assets remain unchanged; generic widget gaps go to Conductor through the existing handoff, not into a parallel consumer widget implementation.

## Human usability walkthrough

Run this separately with the maintainer and at least one participant unfamiliar with the UI. Automated gates do not fill in this result. Use a disposable synthetic project and compatible saved static/profile/comparison fixtures prepared by the reviewer, not a live consumer project. Do not coach by telling the participant which control to click; record what they try and whether the in-app guidance is sufficient.

| Task | Success criterion |
| --- | --- |
| Begin collecting static evidence | Identify the starting action without external documentation; distinguish static collection from executing Python. Review the exact command before confirming a synthetic collection. |
| Choose a directory | Enter a child folder, return with Up, identify the configured root and explain why Up stops there. Understand that Choose applies the field and browsing alone does not. |
| Understand an unavailable view | Explain which prerequisite is missing and find a corrective action without treating absence as zero findings. |
| Investigate one finding | Find its supporting evidence and recorded source, then return with run/filter/page/position context intact. Explain a deliberately changed-source rejection. |
| Get help in place | Find relevant short guidance, follow a related topic and return without losing a job, investigation or draft. |
| Compare two runs | Identify baseline and candidate, inspect a changed item, return to its row and explain inherited partiality without assuming an optimization win. |
| Work with a draft | Make a harmless session-only draft, cancel a navigation/discard prompt, return to it and explicitly export it. Explain why it does not modify analyzed source. |
| Read a bounded graph/table | Find the relevant relationship or row, change a bound/filter and explain omitted counts instead of assuming the picture is exhaustive. |

Record commit and dirty-state/input identity, OS/browser/version, viewport/theme, fixture identities, participant role (maintainer or unfamiliar), task, observed actions, hesitation/error, recovery, outcome (`pass`, `minor-friction`, `blocking`, `not-reviewed`) and linked evidence/issue. Record assistance explicitly; do not silently turn a coached completion into an unaided pass. Do not publish personal information. Preserve failed outcomes and compare observations after fixes rather than replacing the original review.

Required human outcomes remain: begin unaided, understand unavailable views, investigate/return without losing context, and use relevant help without abandoning the workflow. A blocker in any of these keeps human acceptance open. Neither automated test counts nor familiarity with the code establishes public-release readiness. Project profiles, a dedicated Change project workflow, tutorials, storage-management UI, incremental analysis and live tracing remain separately scoped follow-ups.
