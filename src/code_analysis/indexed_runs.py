"""Explicit generation selection and reusable indexes without retaining full reports."""

from __future__ import annotations

import hashlib
from pathlib import Path
from threading import RLock

from code_analysis.browse_index import BrowseIndex, builder_identity
from code_analysis.browse_index_build import build_browse_index
from code_analysis.profile_runs import ProfileRunCatalog, profile_catalog, select_profiles
from code_analysis.profiling import render_profile_json
from code_analysis.reports.json_report import canonical_json
from code_analysis.runtime_index import RuntimeIndex, prepare_runtime_index
from code_analysis.ui_runs import UIRun, UIRunCatalog
from code_analysis.web_service import ReportViewService
from code_analysis.workspace import (
    GenerationKind,
    list_generations,
    open_workspace,
    validate_workspace_ownership,
)


class IndexedRunRepository:
    """Load selected saved reports only during explicit cache preparation."""

    def __init__(self, root: Path, *, include_analysis: bool = True) -> None:
        self.workspace = open_workspace(root)
        self._lock = RLock()
        self._profile_catalog: ProfileRunCatalog | None = None
        self.manifests = {
            item.generation_id: item
            for item in (
                list_generations(self.workspace, kind=GenerationKind.ANALYSIS)
                if include_analysis
                else ()
            )
        }

    def refresh(self) -> UIRunCatalog:
        with self._lock:
            incoming = {
                item.generation_id: item
                for item in list_generations(self.workspace, kind=GenerationKind.ANALYSIS)
            }
            if any(
                key in incoming and incoming[key] != previous
                for key, previous in self.manifests.items()
            ):
                raise ValueError("an immutable generation changed during refresh")
            self.manifests = incoming
            records = tuple(
                UIRun(
                    generation_id=item.generation_id,
                    created_at=item.created_at,
                    report_sha256=item.portable_identity_sha256,
                )
                for item in reversed(tuple(incoming.values()))
                if any(artifact.name == "report.json" for artifact in item.artifacts)
            )
            return UIRunCatalog(records=records[:500], omitted=max(0, len(records) - 500))

    def profiles(self) -> ProfileRunCatalog:
        with self._lock:
            incoming = profile_catalog(self.workspace)
            current = {item.generation_id: item for item in incoming.records}
            if self._profile_catalog is not None and any(
                item.generation_id in current and current[item.generation_id] != item
                for item in self._profile_catalog.records
            ):
                raise ValueError("an immutable profile generation changed during refresh")
            self._profile_catalog = incoming
            return incoming

    def select(
        self,
        baseline: str,
        candidate: str | None = None,
        *,
        profile: str | None = None,
        compare_profile: str | None = None,
    ) -> BrowseIndex:
        with self._lock:
            visible = {item.generation_id for item in self.refresh().records}
            selected = (baseline,) if candidate is None else (baseline, candidate)
            if any(item not in visible for item in selected):
                raise ValueError("selected run is absent from the bounded workspace catalog")
            expected = tuple(self.manifests[item] for item in selected)
            if profile is not None:
                self.profiles()
            runtime = select_profiles(self.workspace, profile, compare_profile)
            runtime_hashes = (
                ()
                if runtime is None
                else tuple(
                    hashlib.sha256(render_profile_json(item, compact=True).encode()).hexdigest()
                    for item in (runtime.profile, runtime.comparison)
                    if item is not None
                )
            )
            identity = hashlib.sha256(
                (
                    builder_identity()
                    + "|"
                    + "|".join(item.portable_identity_sha256 for item in expected)
                    + "|profiles:"
                    + "|".join(runtime_hashes)
                ).encode()
            ).hexdigest()
            validate_workspace_ownership(self.workspace)
            destination = self.workspace.cache / f"browse-{identity}"
            if destination.exists() or destination.is_symlink():
                index = BrowseIndex.open(
                    destination, report_sha256=expected[0].portable_identity_sha256
                )
                comparison = index.manifest.comparison
                if (
                    (comparison is None) != (candidate is None)
                    or (
                        comparison is not None
                        and comparison.candidate_sha256 != expected[-1].portable_identity_sha256
                    )
                    or (index.manifest.runtime is None) != (runtime is None)
                ):
                    raise ValueError("cached index does not match the selected run pair")
                if index.manifest.runtime is not None:
                    cached = index.manifest.runtime
                    hashes = (
                        (cached.profile_report_sha256,)
                        if cached.comparison is None
                        else (
                            cached.profile_report_sha256,
                            cached.comparison.candidate_report_sha256,
                        )
                    )
                    if hashes != runtime_hashes:
                        raise ValueError("cached profile snapshots do not match selection")
                return index
            paths = tuple(
                self.workspace.kind_directory(GenerationKind.ANALYSIS) / item / "report.json"
                for item in selected
            )
            service = ReportViewService.load(paths[0], paths[1] if candidate is not None else None)
            reports = (
                (service.primary,)
                if service.comparison is None
                else (service.primary, service.comparison)
            )
            if tuple(
                hashlib.sha256(canonical_json(report).encode()).hexdigest() for report in reports
            ) != tuple(item.portable_identity_sha256 for item in expected):
                raise ValueError("selected report identity does not match generation manifest")
            self.refresh()
            if tuple(self.manifests.get(item) for item in selected) != expected:
                raise ValueError("selected generation changed during index preparation")
            validate_workspace_ownership(self.workspace)
            build_browse_index(service, destination, runtime=runtime)
            return BrowseIndex.open(destination, report_sha256=expected[0].portable_identity_sha256)

    def select_runtime(self, baseline: str, candidate: str | None = None) -> RuntimeIndex:
        with self._lock:
            self.profiles()
            runtime = select_profiles(self.workspace, baseline, candidate)
            assert runtime is not None
            return prepare_runtime_index(self.workspace, runtime)
