"""Fixed saved-report UI workload; no source scan or target execution.

Use PYTHONPATH to select an archived checkout for paired measurements. Output
is local qualification evidence, not an authoritative analysis report.
"""

from __future__ import annotations

import argparse
import cProfile
import hashlib
import json
import resource
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from code_analysis.web_service import ReportViewService


@dataclass(frozen=True, slots=True)
class Sample:
    iteration: int
    operation: str
    seconds: float
    output_sha256: str
    output_bytes: int


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--profile-scope", choices=("load", "requests"), default="requests")
    parser.add_argument("--fixed-pages", action="store_true", help="repeat four fixed requests")
    args = parser.parse_args()
    profiler = cProfile.Profile() if args.profile else None
    if profiler and args.profile_scope == "load":
        profiler.enable()
    started = time.perf_counter()
    service = ReportViewService.load(args.report)
    load_seconds = time.perf_counter() - started
    if profiler and args.profile_scope == "load":
        profiler.disable()
    samples: list[Sample] = []
    if profiler and args.profile_scope == "requests":
        profiler.enable()
    for iteration in range(4):
        for name, operation in (
            ("bootstrap", service.bootstrap),
            (
                "modules",
                lambda page=0 if args.fixed_pages else iteration: service.modules(
                    offset=min(page * 25, len(service.primary.modules)), limit=25
                ),
            ),
            (
                "findings",
                lambda page=0 if args.fixed_pages else iteration: service.findings(
                    offset=min(page * 25, len(service.primary.findings)), limit=25
                ),
            ),
            ("candidates", lambda: service.candidates(offset=0, limit=25)),
        ):
            start = time.perf_counter()
            result = operation()
            elapsed = time.perf_counter() - start
            rendered = result.model_dump_json().encode()
            samples.append(
                Sample(
                    iteration, name, elapsed, hashlib.sha256(rendered).hexdigest(), len(rendered)
                )
            )
    if profiler:
        profiler.disable()
        profiler.dump_stats(args.profile)
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    payload = {
        "interpreter": sys.executable,
        "python": sys.version,
        "report_sha256": hashlib.sha256(args.report.read_bytes()).hexdigest(),
        "report_bytes": args.report.stat().st_size,
        "load_seconds": load_seconds,
        "peak_rss_bytes": peak if sys.platform == "darwin" else peak * 1024,
        "profiled": bool(profiler),
        "fixed_pages": args.fixed_pages,
        "profile_scope": args.profile_scope if profiler else None,
        "samples": [asdict(sample) for sample in samples],
    }
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in payload.items() if key != "samples"}))


if __name__ == "__main__":
    main()
