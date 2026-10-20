# Public beta scope and known limitations

Code Analysis Suite is preparing its first public beta. It is a developer investigation tool, not a certification that code is correct, secure, fast or safe to refactor. The candidate is not a stable API or report-format compatibility promise. Publication and release assets require a qualified, maintainer-reviewed commit.

## What to try

Start with the [Fawlty Towers tutorials](../tutorials/README.md), especially the [Python reservation desk](../tutorials/python/beginner/README.md). Supplied reports let you investigate without running or rescanning an application. Then try a static scan of your own source, inspect findings and relationships, and compare two saved runs. Read the [UI guide](ui-guide.md), [CLI reference](cli.md) and [task examples](cli-examples.md) for exact operations and artifacts.

Python, JavaScript/JSX and Ruby have peer parser-adapter packages, but they do not have identical analytical capabilities. Python has the broadest contracts, dictionary, test and runtime support. JavaScript requires its optional Tree-sitter extra for collection; Ruby requires an available Ruby/Prism executable. Retained reports do not require those parsers. Rust, Go, COBOL, TypeScript and other languages are future work, not supported inputs.

## Candidate qualification

On Python 3.12.13, the candidate passed 2,808 product tests across a disjoint 852-test UI/support gate and 1,956-test non-UI partition, with no failures or skips. The separate 13-case smoke gate also passed; its cases overlap the full UI gate and must not be added to that total. Black, Ruff, documentation links, compiled-help freshness, archived package builds and installed offline Chromium/WebKit checks passed. The source distribution retains all 135 tutorial files; all nine supplied reports' source hashes were checked without rerunning or rewriting the exercises.

These technical checks do not establish unfamiliar-user comprehension, real Safari/hardware IME or every interpreter/platform combination. Local type checkers were unavailable and were not installed or claimed passed. Source self-analysis retains partiality for bounded/unresolved evidence rather than returning a clean verdict.

## Known limitations

- Dynamic dispatch, reflection, metaprogramming, generated code and runtime-only dependencies remain unresolved where no exact evidence exists. Static evidence does not establish semantic equivalence or runtime success.
- Tutorial qualification exposed incomplete JavaScript finding/aggregate-cycle and Ruby branch/clone/state/test-discovery contributions. Consult [the tutorial capability observations](../tutorials/VALIDATION.md#observed-missing-and-deferred). A complete collection state describes declared scope, not language feature parity or proof of clean code.
- Candidate admission is conservative. Empty candidate/proposal inventories are possible even for deliberately awkward code. Complexity alone cannot authorize an extraction; missing coverage or ownership evidence remains visible.
- cProfile measures supported Python scopes, not every thread, child process or native/device operation. Unavailable timing stays unavailable. See [profiling scope](threaded-profile-investigation.md). JavaScript and Ruby runtime profiling are not supported.
- The tutorial's direct Python script profiling exercise exposed an import-context failure while its module-based unittest workload succeeded. Do not assume every script workload has the same import path as direct interpreter invocation; preserve failures and use the documented supported workload rather than claiming a complete capture.
- Browser qualification covers Chromium and WebKit automation. Real Safari, hardware IME, unfamiliar-user comprehension and broader platform/runtime combinations need further review. Python 3.12.13 is the qualification interpreter; Python 3.13 exposed an authoritative-golden difference and is not qualified by that result.
- Project profiles, an in-session change-project workflow, entry-point suggestions, tutorials integrated into the UI, storage-management controls, incremental collection, live tracing and AI/IDE integration remain planned. The tutorial files are available from a source checkout, not an installed-wheel tutorial browser.

## Safety boundaries

Static collection does not import or execute target code. It can create an owned local evidence workspace; use `--no-workspace` or an explicit external workspace for read-only targets. Rendering, querying and comparing saved reports do not rescan source. Source preview is explicit, confined and hash-verified.

Runtime collection is different: it executes the supplied Python workload with the operator's permissions, requires explicit authorization and is not a sandbox. The UI is loopback-only; operator-managed SSH tunneling does not widen filesystem or execution authority. Closing a browser tab does not stop a job or server. Session drafts do not modify analyzed source.

Portable reports omit host paths, but source identifiers, names, literals, topology and diagnostic evidence can still disclose information about a private project. Local manifests, command lines and captured streams can contain sensitive material. Review artifacts before sharing; do not attach entire workspaces or raw profiles to public issues by default.

## Reporting problems

Use [GitHub issues](https://github.com/unixwzrd/code-analytics/issues) for nonsensitive bugs and feature requests. Include the version, platform/interpreter/browser, command or UI task, expected/observed result, native exit status and a minimal synthetic reproduction. Keep partiality and diagnostic messages intact. Redact personal paths, proprietary source, secrets and workload arguments. For sensitive reports, follow [the security policy](../SECURITY.md).

Automated tests and tutorial reports do not replace human usability acceptance. Public beta feedback is intended to find gaps; it is not a statement that every planned feature or release gate has been completed.
