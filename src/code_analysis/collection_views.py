"""Verified saved collection results shared by Web views and terminal indexes."""

from __future__ import annotations

import errno
import hashlib
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

from code_analysis.browse_index import BrowseIndex, builder_identity
from code_analysis.browse_index_build import build_browse_index
from code_analysis.profiling import render_profile_json
from code_analysis.report_models import AnalysisReport
from code_analysis.reports.json_report import canonical_json, load_json
from code_analysis.runtime_index import RuntimeIndex, prepare_runtime_index
from code_analysis.ui_runtime import RuntimeViewService
from code_analysis.web_service import ReportViewService
from code_analysis.workspace import validate_workspace_ownership

if TYPE_CHECKING:
    from code_analysis.collection_service import CollectionSession


@dataclass(frozen=True, slots=True)
class CollectionViews:
    service: ReportViewService | None
    runtime: RuntimeViewService | None


def collection_views(session: CollectionSession, identity: str) -> CollectionViews:
    jobs = session.selected()
    job = jobs.get(identity)
    if job.binding is None:
        raise OSError(errno.ENODATA, "job has no verified generation to open")
    result = jobs.result(identity)
    if isinstance(result.report, AnalysisReport):
        return CollectionViews(ReportViewService(result.report), None)
    if result.report is None:
        raise OSError(errno.ENODATA, "profile is unavailable")
    if job.request.source_report is None:
        return CollectionViews(None, RuntimeViewService(result.report))
    try:
        source = load_json(
            session.planner.read_input(
                job.request.source_report, max_bytes=256 * 1024 * 1024
            ).decode()
        )
    except ValueError as exc:
        raise OSError(errno.EBADMSG, "saved profile source report is malformed") from exc
    if (
        hashlib.sha256(canonical_json(source).encode()).hexdigest()
        != result.report.source_report_sha256
    ):
        raise OSError(errno.EBADMSG, "saved source report no longer matches profile")
    return CollectionViews(ReportViewService(source), RuntimeViewService(result.report))


def collection_index(session: CollectionSession, identity: str) -> BrowseIndex | RuntimeIndex:
    """Explicit saved-artifact preparation; no target source or latest-alias lookup."""
    views = collection_views(session, identity)
    if views.service is None:
        assert views.runtime is not None
        return _runtime_index(session, views.runtime)
    source_hash = hashlib.sha256(canonical_json(views.service.primary).encode()).hexdigest()
    runtime_hash = (
        hashlib.sha256(
            render_profile_json(views.runtime.profile, compact=True).encode()
        ).hexdigest()
        if views.runtime is not None
        else None
    )
    cache_key = hashlib.sha256(
        f"collection|{builder_identity()}|{source_hash}|{runtime_hash}".encode()
    ).hexdigest()
    workspace = session.selected().workspace
    validate_workspace_ownership(workspace)
    destination = workspace.cache / f"browse-{cache_key}"
    if not destination.exists() and not destination.is_symlink():
        build_browse_index(views.service, destination, runtime=views.runtime)
    try:
        index = BrowseIndex.open(destination, report_sha256=source_hash)
    except (ValueError, sqlite3.Error) as exc:
        raise OSError(errno.EBADMSG, "saved collection index is malformed") from exc
    cached = index.manifest.runtime
    if index.manifest.comparison is not None or (
        (cached is None) != (runtime_hash is None)
        or (
            cached is not None
            and (cached.profile_report_sha256 != runtime_hash or cached.comparison is not None)
        )
    ):
        raise OSError(errno.EBADMSG, "collection index does not match selected artifacts")
    return index


def _runtime_index(session: CollectionSession, runtime: RuntimeViewService) -> RuntimeIndex:
    try:
        return prepare_runtime_index(session.selected().workspace, runtime)
    except (ValueError, sqlite3.Error) as exc:
        raise OSError(errno.EBADMSG, "saved runtime index is malformed") from exc
