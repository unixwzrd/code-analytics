"""Explicit saved-report index preparation; no scanning or workload execution."""

from __future__ import annotations

import errno
import sqlite3
import sys
from pathlib import Path

from code_analysis.browse_index import index_directory
from code_analysis.browse_index_build import BrowseIndexLimitError, build_browse_index
from code_analysis.comparisons import ComparisonBuildError
from code_analysis.models import Completeness
from code_analysis.runtime_browse_storage import RuntimeBrowseLimitError
from code_analysis.static_browse import StaticProjectionLimitError
from code_analysis.ui_runtime import RuntimeViewService
from code_analysis.web_service import ReportViewService


def prepare_index_command(
    report: Path,
    destination: Path,
    comparison: Path | None = None,
    profile_report: Path | None = None,
    compare_profile: Path | None = None,
) -> int:
    if compare_profile is not None and profile_report is None:
        print("--compare-profile requires --profile-report", file=sys.stderr)
        return errno.EINVAL
    try:
        index_directory(destination.parent)
    except ValueError as exc:
        print(f"invalid index destination: {exc}", file=sys.stderr)
        return errno.EINVAL
    try:
        service = ReportViewService.load(report, comparison)
    except ComparisonBuildError as exc:
        print(f"incompatible comparison: {exc}", file=sys.stderr)
        return errno.EINVAL
    try:
        runtime = (
            RuntimeViewService.load(profile_report, compare_profile)
            if profile_report is not None
            else None
        )
        manifest = build_browse_index(service, destination, runtime=runtime)
    except (BrowseIndexLimitError, RuntimeBrowseLimitError, StaticProjectionLimitError) as exc:
        print(f"index preparation incomplete: {exc}", file=sys.stderr)
        return errno.ENODATA
    except sqlite3.Error as exc:
        print(f"index database failure: {exc}", file=sys.stderr)
        return errno.EBADMSG
    print(manifest.model_dump_json())
    if (
        (
            manifest.runtime is not None
            and (
                manifest.runtime.overview.completeness is not Completeness.COMPLETE
                or not manifest.runtime.overview.source_matches
                or (
                    manifest.runtime.comparison is not None
                    and (
                        manifest.runtime.comparison.candidate_overview.completeness
                        is not Completeness.COMPLETE
                        or not manifest.runtime.comparison.candidate_overview.source_matches
                        or any(
                            decision.outcome != "matched"
                            for decision in manifest.runtime.comparison.header.decisions
                        )
                    )
                )
            )
        )
        or service.primary.completeness is not Completeness.COMPLETE
        or any(
            item.source_truncated or item.completeness is not Completeness.COMPLETE
            for item in manifest.static_projections
        )
        or (
            manifest.comparison is not None
            and (
                manifest.comparison.completeness is not Completeness.COMPLETE
                or manifest.comparison.omitted
            )
        )
    ):
        print(
            "index created; source report, prepared views, runtime binding, or comparison "
            "remains incomplete",
            file=sys.stderr,
        )
        return errno.ENODATA
    return 0
