# Migration from `function-analysis`

The authoritative command is now `code-analysis scan PATH`. The package does not import or execute `~/bin/function-analysis`; that location is environment-specific and is only a read-only behavioral reference.

| Legacy flag | R1 command/flag | Status |
| --- | --- | --- |
| `PATH` | `scan PATH` | Command moved under `scan` |
| `--json` | `--json` | Authoritative versioned report replaces the legacy list schema |
| `--json-compact` | `--json-compact` | Preserved |
| `--summary` | default Markdown or `summary REPORT` | Accepted as a migration alias |
| `--only-refactor-targets` | `--only-refactor-targets` | Accepted; findings remain authoritative |
| `--only-local-imports` | default behavior | Preserved; use `--all-imports` to retain external edges |
| `--max-items` | accepted, ignored for saved reports | Reports are not silently truncated |
| `--strictness` | `--strictness` | Preserved |
| `--god-loc/branches/nesting/args/score` | same flags | Preserved as threshold aliases |

Phase 3 uses new authoritative commands rather than legacy compatibility flags: `code-analysis clones PATH` emphasizes clone/commonality evidence, while `code-analysis refactor PATH` emits the full report as JSON and a bounded proposal packet as default Markdown. Saved-report queries add `--clone`, `--responsibility`, and `--proposal`. These commands do not exist in the legacy script and make no byte-compatibility promise.

Intended differences include explicit interpreter provenance, no `secrets_kit` normalization, logical source paths, proper project/`src` discovery, namespace packages, deduplicated import edges, resolved relative imports, Tarjan SCCs, canonical directed cycles, nested-symbol qualified names, strict completeness/provider states, stable finding IDs, and deterministic portable JSON.

Legacy text formatting and JSON field layout are not byte-compatible. R1 does not count nested function branches against the enclosing function. Cycle counts are unique canonical cycles rather than the legacy sum of cycle lists attached repeatedly to modules.

The legacy script has no `--version`; invoking it with that flag attempts to scan a path named `--version`. R1 records it as an unversioned local script rather than treating that behavior as provider success.
