"""Standalone, source-free profile indexes using the shared bounded runtime storage."""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field, StrictInt, StrictStr, model_validator

from code_analysis.browse_index import (
    FileStamp,
    _connect,
    artifact_digest,
    builder_identity,
    file_stamp,
    index_directory,
)
from code_analysis.models import StrictModel
from code_analysis.profiling import render_profile_json
from code_analysis.projection_models import GraphProjection
from code_analysis.runtime_browse_models import (
    RuntimeBrowsePage,
    RuntimeBrowseRequest,
    RuntimeIndexSummary,
)
from code_analysis.runtime_browse_storage import (
    read_runtime_page,
    validate_runtime_records,
    write_runtime_records,
)
from code_analysis.runtime_comparison_browse import (
    RuntimeComparisonPage,
    RuntimeComparisonRequest,
    read_comparison_page,
)
from code_analysis.runtime_graph_browse import RuntimeGraphRequest, read_runtime_graph
from code_analysis.runtime_source_browse import RuntimeSourcePreview, RuntimeSourceUnavailable
from code_analysis.ui_runtime import RuntimeViewService
from code_analysis.workspace import WorkspacePaths, validate_workspace_ownership


class RuntimeIndexManifest(StrictModel):
    schema_name: Literal["code-analysis-runtime-index"] = "code-analysis-runtime-index"
    schema_version: Literal["1.0.0"] = "1.0.0"
    report_sha256: None = None
    builder_identity_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    database_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    database_bytes: StrictInt = Field(ge=1, le=2 * 1024**3)
    runtime: RuntimeIndexSummary

    @model_validator(mode="after")
    def no_static_binding(self) -> RuntimeIndexManifest:
        if self.runtime.overview.source_matches or (
            self.runtime.comparison is not None
            and self.runtime.comparison.candidate_overview.source_matches
        ):
            raise ValueError("standalone runtime index cannot claim a selected static report")
        return self


@dataclass(frozen=True, slots=True)
class RuntimeIndex:
    database: Path
    manifest: RuntimeIndexManifest
    stamp: FileStamp

    @classmethod
    def open(cls, directory: Path, *, profile_sha256: str) -> RuntimeIndex:
        directory = index_directory(directory)
        receipt = directory / "manifest.json"
        digest, stamp = artifact_digest(receipt, 1024**2)
        descriptor = os.open(receipt, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            raw = stream.read(1024**2 + 1)
        if hashlib.sha256(raw).hexdigest() != digest or file_stamp(receipt) != stamp:
            raise ValueError("runtime index receipt changed while reading")
        manifest = RuntimeIndexManifest.model_validate_json(raw)
        if manifest.runtime.profile_report_sha256 != profile_sha256:
            raise ValueError("runtime index belongs to a different profile")
        if manifest.builder_identity_sha256 != builder_identity():
            raise ValueError("runtime index builder changed; rebuild from saved JSON")
        database = directory / "index.sqlite3"
        digest, stamp = artifact_digest(database, 2 * 1024**3)
        if digest != manifest.database_sha256 or stamp.size != manifest.database_bytes:
            raise ValueError("runtime index database identity mismatch")
        with closing(_connect(database)) as connection:
            objects = connection.execute(
                "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            if objects != [("table", "runtime_records")]:
                raise ValueError("unexpected standalone runtime database objects")
            validate_runtime_records(connection, manifest.runtime)
        if file_stamp(database) != stamp:
            raise ValueError("runtime index changed while opening")
        return cls(database, manifest, stamp)

    def _read[T](self, reader: Callable[[sqlite3.Connection], T]) -> T:
        if file_stamp(self.database) != self.stamp:
            raise ValueError("runtime index changed")
        with closing(_connect(self.database)) as connection:
            value = reader(connection)
        if file_stamp(self.database) != self.stamp:
            raise ValueError("runtime index changed while reading")
        return value

    def runtime_page(self, request: RuntimeBrowseRequest) -> RuntimeBrowsePage:
        return self._read(
            lambda connection: read_runtime_page(connection, self.manifest.runtime, request)
        )

    def runtime_graph(self, request: RuntimeGraphRequest) -> GraphProjection:
        return self._read(
            lambda connection: read_runtime_graph(connection, self.manifest.runtime, request)
        )

    def runtime_comparison(self, request: RuntimeComparisonRequest) -> RuntimeComparisonPage:
        summary = self.manifest.runtime.comparison
        if summary is None:
            raise ValueError("runtime comparison unavailable; no candidate was indexed")
        return self._read(lambda connection: read_comparison_page(connection, summary, request))

    def runtime_source(self, frame_id: str) -> RuntimeSourcePreview:
        raise RuntimeSourceUnavailable(
            "standalone runtime index has no selected static source report"
        )

    def runtime_comparison_source(
        self, _frame_identity: str, _side: Literal["baseline", "candidate"]
    ) -> RuntimeSourcePreview:
        raise RuntimeSourceUnavailable(
            "standalone runtime index has no selected static source report"
        )


def build_runtime_index(service: RuntimeViewService, destination: Path) -> RuntimeIndexManifest:
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(errno.EEXIST, "runtime index already exists")
    index_directory(destination.parent)
    temporary = Path(tempfile.mkdtemp(prefix=".runtime-index-", dir=destination.parent))
    try:
        (temporary / ".gitignore").write_text("*\n", encoding="utf-8")
        database = temporary / "index.sqlite3"
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("PRAGMA trusted_schema = OFF")
            summary = write_runtime_records(connection, service, None)
        assert summary is not None
        digest, stamp = artifact_digest(database, 2 * 1024**3)
        manifest = RuntimeIndexManifest(
            builder_identity_sha256=builder_identity(),
            database_sha256=digest,
            database_bytes=stamp.size,
            runtime=summary,
        )
        payload = manifest.model_dump_json().encode()
        if len(payload) > 1024**2:
            raise ValueError("runtime index receipt exceeds byte bound")
        (temporary / "manifest.json").write_bytes(payload)
        RuntimeIndex.open(temporary, profile_sha256=summary.profile_report_sha256)
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(errno.EEXIST, "runtime index destination appeared")
        os.rename(temporary, destination)
        return manifest
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def prepare_runtime_index(workspace: WorkspacePaths, runtime: RuntimeViewService) -> RuntimeIndex:
    """Reuse only a derivative bound to both exact saved profile snapshots."""
    hashes = tuple(
        hashlib.sha256(render_profile_json(profile, compact=True).encode()).hexdigest()
        for profile in (runtime.profile, runtime.comparison)
        if profile is not None
    )
    key = hashlib.sha256(f"runtime|{builder_identity()}|{'|'.join(hashes)}".encode()).hexdigest()
    validate_workspace_ownership(workspace)
    destination = workspace.cache / f"runtime-{key}"
    if not destination.exists() and not destination.is_symlink():
        build_runtime_index(runtime, destination)
    index = RuntimeIndex.open(destination, profile_sha256=hashes[0])
    comparison = index.manifest.runtime.comparison
    actual = (index.manifest.runtime.profile_report_sha256,) + (
        (comparison.candidate_report_sha256,) if comparison is not None else ()
    )
    if actual != hashes:
        raise ValueError("cached runtime profiles do not match selected snapshots")
    return index
