"""Measure generator input facts retained in a frozen report, without rescanning.

This local experiment reads only feature/catalog/subject/clone records to compare
implementations across a funnel-schema edit; it is not a report migration loader.
"""

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

from code_analysis.analysis.commonality import generate_commonality_candidates
from code_analysis.analysis.function_features import FunctionFeatures
from code_analysis.reports.source_bindings import BoundEvidenceCatalog


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--max-pairs", type=int, default=50_000)
    parser.add_argument("--profile", type=Path)
    args = parser.parse_args()
    raw = args.report.read_bytes()
    report = json.loads(raw)
    subjects = {item["subject_id"]: item for item in report["subjects"]}
    bound = BoundEvidenceCatalog.model_validate_json(
        json.dumps({"sources": report["source_bindings"], "evidence": report["evidence_catalog"]})
    )
    catalog = {
        item.evidence_id: value.model_dump(mode="json")
        for item, value in zip(bound.evidence, bound.expanded(), strict=True)
    }
    features = []
    names = {}
    for item in report["function_profiles"]:
        if item["language"] != "python":
            continue
        subject = subjects[item["subject_id"]]
        evidence = catalog[item["evidence_ids"][0]]
        span = evidence["span"]
        names[item["subject_id"]] = subject["logical_name"]
        features.append(
            FunctionFeatures(
                logical_name=subject["logical_name"],
                module=subject["module"],
                logical_path=span["path"],
                source_sha256=evidence["source_sha256"],
                start_line=span["start_line"],
                start_column=span["start_column"],
                end_line=span["end_line"],
                end_column=span["end_column"],
                **{
                    key: tuple(item[key])
                    for key in (
                        "callees",
                        "signature_shapes",
                        "attributes_globals",
                        "exceptions",
                        "side_effects",
                        "control_flow",
                        "dictionary_schema_operations",
                        "contract_ids",
                        "weak_semantic_signals",
                    )
                },
                unresolved_call_count=item["unresolved_call_count"],
            )
        )
    clone_pairs = set()
    for cluster in report["clone_clusters"]:
        members = sorted(
            names[identity] for identity in cluster["subject_ids"] if identity in names
        )
        for index, left in enumerate(members):
            clone_pairs.update((left, right) for right in members[index + 1 :])
    profiler = cProfile.Profile() if args.profile else None
    if profiler:
        profiler.enable()
    started = time.perf_counter()
    result = generate_commonality_candidates(
        tuple(features),
        tuple(sorted(clone_pairs)),
        minimum_ownership_dimensions=2,
        minimum_bilateral_coverage=0.25,
        maximum_feature_subject_fraction=0.1,
        maximum_pairs=args.max_pairs,
    )
    seconds = time.perf_counter() - started
    if profiler:
        profiler.disable()
        profiler.dump_stats(args.profile)
    payload = asdict(result)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    summary = {
        "input_sha256": hashlib.sha256(raw).hexdigest(),
        "max_pairs": args.max_pairs,
        "interpreter": sys.executable,
        "python": sys.version,
        "profiled": bool(profiler),
        "seconds": seconds,
        "peak_rss_bytes": peak if sys.platform == "darwin" else peak * 1024,
        "output_sha256": hashlib.sha256(encoded).hexdigest(),
        "admitted": len(result.pairs),
        "unique_candidates": result.unique_candidates,
        "truncated": result.truncated_pairs,
    }
    args.output.write_text(json.dumps({**summary, "result": payload}, indent=2) + "\n")
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
