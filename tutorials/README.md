# Fawlty Towers Software Consulting

**Sybil wants answers before Basil improves it again.**

Basil and Manuel have completed a programming correspondence course from Barcelona. Basil is certain the hotel software is ready for anything. Manuel is keen to demonstrate it, Polly is keeping operations moving, and Sybil has hired you to find out how the system works before the next change.

These nine independent command-line applications tell successive episodes of the same hotel story. The browser is the Code Analysis investigation interface; the applications need no web service. Their documented examples work. Your assignment is to investigate, document the evidence and reach your own conclusions about future maintenance.

**Start with [Python beginner](python/beginner/README.md).** The tutorials and supplied evidence are distributed in the GitHub checkout, not bundled in the installed wheel. Install [Code Analysis Suite](../README.md#installation) first; it requires Python 3.12+. Opening the saved reports needs no Node, Ruby or optional language parser. Running each application's demos/tests requires its corresponding existing Python, Node or Ruby runtime. Maintainer qualification used Python 3.12.13, Node 24.15.0 and Ruby 3.3.4; these are tested versions, not newly asserted minimum versions. Fresh JavaScript scans additionally need the optional Tree-sitter dependencies, and fresh Ruby scans need Ruby/Prism.

**Preserve the supplied exercise before experimenting:** copy the checkout before changing source or running `tutorials/qualify.py`. The helper executes demos/tests and replaces evidence in the selected directory; it is not needed to read or investigate the supplied reports.

| Language | The Reservation Desk | The Booking Mix-Up | The Hotel From Hell |
| --- | --- | --- | --- |
| [Python](python/README.md) | [Beginner](python/beginner/README.md) | [Intermediate](python/intermediate/README.md) | [Advanced](python/advanced/README.md) |
| [JavaScript](javascript/README.md) | [Beginner](javascript/beginner/README.md) | [Intermediate](javascript/intermediate/README.md) | [Advanced](javascript/advanced/README.md) |
| [Ruby](ruby/README.md) | [Beginner](ruby/beginner/README.md) | [Intermediate](ruby/intermediate/README.md) | [Advanced](ruby/advanced/README.md) |

Each level's README gives Sybil's incident report, your mission and the exact demo, test and saved-report launch commands. Follow its independent investigation, use the optional three-step hints only if needed, complete your findings checklist, then compare with its clearly marked SOLUTIONS.md. Optional further work comes after that comparison.

Ada's common baseline books two standard rooms for one night at 1200 pennies per room. Her gold-tier discount produces **£21.60**, one saved receipt and six rooms remaining from the original eight. Beginner follows the desk operation; intermediate expands the reservation scenarios; advanced includes group requests, cancellations and repeated requests. These are independent iterations, not before/after versions of one refactor. Fixture code, incidents and dialogue are original. This unofficial educational material is not affiliated with or endorsed by the producers or owners of Fawlty Towers; no rights to third-party names or characters are claimed.

## Shared operating notes

Python/JavaScript use `src/` and `tests/`; Ruby uses `lib/` and `test/`. Each level has its own source, tests and `evidence/` directory. Receipts/test exports use temporary directories. No level imports another, needs an installation step, external service, credentials or database, or has a separate Git repository. Tutorial behavior checks are separate from product acceptance tests.

The saved-report command in each README opens genuine supplied evidence against an explicit source root. Opening a report does not run the application, its tests or a static scan. Check source binding, provider health and completeness before relying on a record; a missing or empty result is not a verdict. The assignment requires no profiling, coverage execution or live model.

Use [the shared investigation workflow](UI-WALKTHROUGH.md) for navigation and evidence-handling guidance. Leave each level's expected-evidence manifest and qualification note closed until you are ready for its solutions. The [maintainer qualification matrix — spoilers](VALIDATION.md) and [baseline measurements — spoilers](BASELINES.md) are evaluation material, not the starting brief or a record of human usability acceptance. Supported capabilities differ by language; complete reports do not imply feature parity.

## Optional later work

After writing and evaluating your consultant report, each SOLUTIONS.md offers further questions. To requalify an intentionally changed copy of the checkout, explicitly select a language/level with `python tutorials/qualify.py python/beginner` from that copied repository root. The helper replaces reports/qualification for the selected level; do not run it merely to read this supplied exercise. Preserve original evidence and recheck source binding after any change. Generated workspaces, profiling artifacts and browser captures remain outside the portable evidence bundles; publication is a separate release decision.
