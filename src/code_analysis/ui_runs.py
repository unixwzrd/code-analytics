"""Read-only selection of validated, owned analysis generations."""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from pathlib import Path
from threading import RLock

from pydantic import Field, StrictInt, StrictStr

from code_analysis.models import StrictModel
from code_analysis.profile_runs import ProfileRunCatalog, profile_catalog, select_profiles
from code_analysis.project_browsing import retained_source_location
from code_analysis.report_models import AnalysisReport
from code_analysis.reports.json_report import canonical_json
from code_analysis.ui_runtime import RuntimeViewService
from code_analysis.web_service import ReportViewService
from code_analysis.workspace import GenerationKind, list_generations, open_workspace


class UIRun(StrictModel):
    generation_id: StrictStr = Field(pattern=r"^[0-9]{8}T[0-9]{6}\.[0-9]{6}Z-[0-9a-f]{12}$")
    created_at: StrictStr
    report_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")


class UIRunCatalog(StrictModel):
    records: tuple[UIRun, ...] = Field(max_length=500)
    omitted: StrictInt = Field(default=0, ge=0)


class RunRepository:
    """Keep only a bounded number of validated report selections in memory."""

    def __init__(self, root: Path, initial: ReportViewService | None) -> None:
        self.workspace = open_workspace(root)
        self.initial = initial
        manifests = (
            list_generations(self.workspace, kind=GenerationKind.ANALYSIS) if initial else ()
        )
        self.manifests = {item.generation_id: item for item in manifests}
        self._cache: OrderedDict[tuple[str, str], ReportViewService] = OrderedDict()
        self._profiles: OrderedDict[tuple[str, str], RuntimeViewService] = OrderedDict()
        self._lock = RLock()

    def profiles(self) -> ProfileRunCatalog:
        with self._lock:
            return profile_catalog(self.workspace)

    def select_profiles(self, primary: str, comparison: str) -> RuntimeViewService | None:
        """Load explicit immutable snapshots, never infer a static/runtime binding."""
        with self._lock:
            key = (primary, comparison)
            if key in self._profiles:
                self._profiles.move_to_end(key)
                return self._profiles[key]
            runtime = select_profiles(
                self.workspace,
                None if primary == "none" else primary,
                None if comparison == "none" else comparison,
            )
            if runtime is not None:
                self._profiles[key] = runtime
                while len(self._profiles) > 2:
                    self._profiles.popitem(last=False)
            return runtime

    def catalog(self) -> UIRunCatalog:
        with self._lock:
            return self._catalog()

    def refresh(self) -> UIRunCatalog:
        """Publish a new catalog only after every retained generation validates."""
        with self._lock:
            if self.initial is None:
                return UIRunCatalog(records=())
            manifests = list_generations(self.workspace, kind=GenerationKind.ANALYSIS)
            incoming = {item.generation_id: item for item in manifests}
            if any(
                identity in incoming and incoming[identity] != previous
                for identity, previous in self.manifests.items()
            ):
                raise ValueError("an immutable generation changed during catalog refresh")
            # Existing cache entries are immutable in-memory snapshots, not
            # claims that a pruned generation is still available on disk.
            self.manifests = incoming
            return self._catalog()

    def _catalog(self) -> UIRunCatalog:
        records = tuple(
            UIRun(
                generation_id=item.generation_id,
                created_at=item.created_at,
                report_sha256=item.portable_identity_sha256,
            )
            for item in reversed(tuple(self.manifests.values()))
            if any(artifact.name == "report.json" for artifact in item.artifacts)
        )
        return UIRunCatalog(records=records[:500], omitted=max(0, len(records) - 500))

    def _report(self, identity: str) -> AnalysisReport:
        if identity == "current":
            if self.initial is None:
                raise ValueError("no static report selected")
            return self.initial.primary
        if identity not in self.manifests:
            raise ValueError("generation identity is not in the selected workspace")
        # Recheck ownership, symlinks, manifests and artifact hashes before loading.
        current = {
            item.generation_id: item
            for item in list_generations(self.workspace, kind=GenerationKind.ANALYSIS)
        }
        if current.get(identity) != self.manifests[identity]:
            raise ValueError("generation changed or was removed since the UI opened")
        path = self.workspace.kind_directory(GenerationKind.ANALYSIS) / identity / "report.json"
        report = ReportViewService.load(path).primary
        digest = hashlib.sha256(canonical_json(report).encode()).hexdigest()
        if digest != self.manifests[identity].portable_identity_sha256:
            raise ValueError("generation report identity does not match its manifest")
        return report

    def select(self, primary: str, comparison: str) -> ReportViewService:
        key = (primary, comparison)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
            baseline = self._report(primary)
            candidate = None if comparison == "none" else self._report(comparison)
            location = (
                self.initial.source_location
                if primary == "current" and self.initial
                else retained_source_location(
                    self.workspace.kind_directory(GenerationKind.ANALYSIS)
                    / primary
                    / "report.json",
                    baseline,
                )
            )
            service = ReportViewService(baseline, candidate, source_location=location)
            self._cache[key] = service
            while len(self._cache) > 2:
                self._cache.popitem(last=False)
            return service
