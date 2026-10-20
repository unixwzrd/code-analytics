"""Measure prepared index opening and bounded pages without reading the source report."""

from __future__ import annotations

import argparse
import cProfile
import hashlib
import json
import resource
import sys
import time
from pathlib import Path

from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_models import BrowseRequest
from code_analysis.projections import ProjectionName
from code_analysis.static_graph_browse import StaticGraphRequest
from code_analysis.ui_rows import BrowseTable


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("index", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--report-sha256", required=True)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--fixed-pages", action="store_true", help="repeat four fixed requests")
    parser.add_argument(
        "--static-graphs", action="store_true", help="repeat four bounded saved graph requests"
    )
    args = parser.parse_args()
    profiler = cProfile.Profile() if args.profile else None
    if profiler:
        profiler.enable()
    started = time.perf_counter()
    index = BrowseIndex.open(args.index, report_sha256=args.report_sha256)
    load_seconds = time.perf_counter() - started
    counts = {item.table: item.records for item in index.manifest.tables}
    samples = []
    operations = (
        (
            "ownership-alias-flow",
            "structure-lifecycle-graph",
            "dictionary-flow",
            "lexical-call-graph",
        )
        if args.static_graphs
        else ("bootstrap", "modules", "findings", "candidates")
    )
    for iteration in range(4):
        for operation in operations:
            started = time.perf_counter()
            if args.static_graphs:
                result = index.static_graph(StaticGraphRequest(name=ProjectionName(operation)))
            elif operation == "bootstrap":
                result = index.bootstrap()
            else:
                table = BrowseTable(operation)
                result = index.page(
                    BrowseRequest(
                        table=table,
                        offset=(
                            0
                            if args.fixed_pages or table is BrowseTable.CANDIDATES
                            else min(iteration * 25, counts[table])
                        ),
                        limit=25,
                    )
                )
            seconds = time.perf_counter() - started
            rendered = result.model_dump_json().encode()
            samples.append(
                {
                    "iteration": iteration,
                    "operation": operation,
                    "seconds": seconds,
                    "output_sha256": hashlib.sha256(rendered).hexdigest(),
                    "output_bytes": len(rendered),
                }
            )
    if profiler:
        profiler.disable()
        profiler.dump_stats(args.profile)
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    payload = {
        "interpreter": sys.executable,
        "python": sys.version,
        "report_sha256": index.manifest.report_sha256,
        "database_bytes": index.manifest.database_bytes,
        "profiled": bool(profiler),
        "fixed_pages": args.fixed_pages,
        "static_graphs": args.static_graphs,
        "load_seconds": load_seconds,
        "peak_rss_bytes": peak if sys.platform == "darwin" else peak * 1024,
        "samples": samples,
    }
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in payload.items() if key != "samples"}))


if __name__ == "__main__":
    main()
