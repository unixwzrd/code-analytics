# Changelog

## 0.2.0b2 — synchronized code and project reading

- Offer source connection directly when selecting a disconnected graph node/entity, with a retry action inside the source pane. Keep the selected method through confirmation and return focus to its verified source without another click; cancellation preserves saved navigation and stale offers cannot reopen after leaving the view.

- After confirmed server shutdown, try closing the initiating tab and replace the interface with a noninteractive session-closed screen if the browser keeps it open. Abort pending UI work and prevent history navigation from reviving controls; failed/cancelled shutdown leaves the UI usable.

- Selecting a source-bound relationship node now opens its verified source directly with pointer, Enter or Space; remove the redundant source-navigation button. Unresolved nodes remain inspection-only.
- Add an explicitly confirmed **Connect source folder** workflow using the existing confined folder picker. Validate a recorded file hash before changing session access; connecting does not collect evidence, execute code or create a workspace.
- Offer nearby README files and linked Markdown/reStructuredText/plain-text documents through the existing LocalOps content panel, with separate provenance, bounded history, literal safe text and spoiler labels. Project documents never become analyzed source or report evidence.
- Preserve source/graph history, dirty-draft guards, same-origin authentication and full-file source hashes. Refuse symlinks in configured-root ancestors as well as descendants.
- Include the tool version in UI session matching so an upgrade opens a new backend instead of silently reopening a previous-version process; existing owned sessions are not killed automatically.

## 0.2.0b1 — public beta candidate

- Exclude private directories entirely from the published tree, including policy markers; local copies remain ignored.
- Correct strict static typing without weakening runtime validation or analytical evidence requirements.
- Add native profile-reader parity checks and synchronize browser assertions with relationship responses and loaded finding details; held-response regressions exercise the loading state explicitly.
- Make optional semantic-provider absence explicit, keep its unit tests independent of installed ML packages, and qualify graphics/Ruby tests with portable tool discovery.
- Validate rendered graph meaning and same-provider determinism without requiring cross-platform Graphviz pixel/layout equality; authoritative JSON/DOT goldens remain byte-exact.

This is the first public-beta candidate. Preparing these notes does not publish it. Original development history and internal qualification records are preserved privately.

### Analysis and reports

- Python, JavaScript/JSX and Ruby parser front ends share strict language-aware report, query, comparison and projection contracts, with explicit capability differences.
- Python's AST inventory, import resolution and call-occurrence parser now live in the peer `languages/python/` package. No compatibility wrappers or analytical semantics changed in that restructuring.
- Inventory, complexity, imports, cycles, clones/commonality, state/effects, Python contracts/dictionaries, test impact and bounded refactoring packets retain evidence and completeness limits.
- Saved JSON remains authoritative. Tables, graphs, images and comparisons can be derived without rescanning source. A proposal does not authorize source changes.
- Optional Python coverage/cProfile workflows retain execution consent, source/producer binding, process ownership and scope limitations.

### Browser and tutorials

- The loopback Web UI supports grouped workflows, contextual offline help, comparisons, hash-verified source browsing and session-only drafts using unmodified LocalOps UI Kit resources.
- Detached UI startup returns the terminal; explicit shutdown/cancellation remains separate from closing a browser tab.
- Quick/full UI gates retain test identities, logs, JUnit, input hashes and current-run captures. Human usability acceptance remains separate.
- Nine original Fawlty Towers-inspired exercises provide Python, JavaScript and Ruby investigations, supplied source-bound evidence, optional hints and separate solutions.

### Publication preparation

- Extend ignore rules for nested workspaces, dependency/cache trees, local process/database files and credential containers while preserving deliberate synthetic fixtures and tutorial evidence.
- Archive internal implementation notes and original Git history before preparing sanitized public history for maintainer review.
- Consolidate documentation around installation, tutorials, architecture/contracts, task guides, limitations and the roadmap. No dependency upgrade or automatic refactoring is included.

### Known limitations

See [beta scope](docs/beta.md), [tutorial capability observations](tutorials/VALIDATION.md#observed-missing-and-deferred) and [profiling scope](docs/threaded-profile-investigation.md). Human first-use acceptance, broader platform/runtime qualification and planned project/storage workflows remain open.
