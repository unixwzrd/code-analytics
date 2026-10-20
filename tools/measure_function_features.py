"""Fixed four-file feature workload; source is parsed, never imported or executed."""

from __future__ import annotations

import argparse
import cProfile
import hashlib
import json
import resource
import sys
import time
from dataclasses import asdict
from pathlib import Path

from code_analysis.analysis.clones import CloneSource
from code_analysis.analysis.function_features import discover_function_features

FILES = (
    "src/code_analysis/report_models.py",
    "src/code_analysis/commonality_reporting.py",
    "src/code_analysis/projections/_shared.py",
    "src/code_analysis/web_server.py",
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_root", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--profile", type=Path)
    args = parser.parse_args()
    sources = []
    for name in FILES:
        raw = (args.source_root / name).read_bytes()
        sources.append(
            CloneSource(
                module=name.removeprefix("src/").removesuffix(".py").replace("/", "."),
                logical_path=name,
                source_sha256=hashlib.sha256(raw).hexdigest(),
                source=raw.decode("utf-8"),
            )
        )
    profiler = cProfile.Profile() if args.profile else None
    if profiler:
        profiler.enable()
    started = time.perf_counter()
    facts = discover_function_features(tuple(sources))
    seconds = time.perf_counter() - started
    if profiler:
        profiler.disable()
        profiler.dump_stats(args.profile)
    records = [asdict(fact) for fact in facts]
    encoded = json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    metadata = {
        "interpreter": sys.executable,
        "python": sys.version,
        "inputs": {source.logical_path: source.source_sha256 for source in sources},
        "profiled": bool(profiler),
        "seconds": seconds,
        "peak_rss_bytes": peak if sys.platform == "darwin" else peak * 1024,
        "output_sha256": hashlib.sha256(encoded).hexdigest(),
        "record_count": len(records),
    }
    args.output.write_text(json.dumps({**metadata, "records": records}, indent=2) + "\n")
    print(json.dumps(metadata, sort_keys=True))


if __name__ == "__main__":
    main()
