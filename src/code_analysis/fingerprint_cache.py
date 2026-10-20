"""Strict content-addressed cache boundary for Phase 3 source artifacts."""

from __future__ import annotations

import platform
from dataclasses import asdict
from typing import Literal

from pydantic import Field, StrictInt, StrictStr

from code_analysis import __version__
from code_analysis.analysis.clones import CloneFingerprint, CloneSource
from code_analysis.cache import CacheKey, CacheLookupState, ContentCache
from code_analysis.config import AnalysisConfig
from code_analysis.models import StrictModel
from code_analysis.report_models import CloneKind


class CachedCloneFingerprint(StrictModel):
    kind: CloneKind
    logical_name: StrictStr
    module: StrictStr
    logical_path: StrictStr
    source_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    start_line: StrictInt = Field(ge=1)
    start_column: StrictInt = Field(ge=0)
    end_line: StrictInt = Field(ge=1)
    end_column: StrictInt = Field(ge=0)
    fingerprint_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    structural_signature: StrictStr
    nonblank_lines: StrictInt = Field(ge=0)
    token_count: StrictInt = Field(ge=0)
    ast_node_count: StrictInt = Field(ge=0)
    statement_count: StrictInt = Field(ge=0)

    @classmethod
    def from_internal(cls, item: CloneFingerprint) -> CachedCloneFingerprint:
        return cls(**asdict(item))

    def to_internal(self) -> CloneFingerprint:
        return CloneFingerprint(**self.model_dump())


class PhaseThreeFingerprintPayload(StrictModel):
    schema_name: Literal["code-analysis-phase-three-fingerprints"] = (
        "code-analysis-phase-three-fingerprints"
    )
    schema_version: Literal["1.0.0"] = "1.0.0"
    source_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    fingerprints: tuple[CachedCloneFingerprint, ...]


def fingerprint_cache_key(source: CloneSource, config: AnalysisConfig) -> CacheKey:
    return CacheKey(
        namespace="phase-three-fingerprints",
        source_sha256=source.source_sha256,
        analyzer_version=__version__,
        python_version=platform.python_version(),
        configuration_sha256=config.phase_three_identity(),
        dependency_identities=(f"module:{source.module}", f"path:{source.logical_path}"),
    )


def load_cached_fingerprints(
    cache: ContentCache, source: CloneSource, config: AnalysisConfig
) -> tuple[CloneFingerprint, ...] | None:
    lookup = cache.get(fingerprint_cache_key(source, config))
    if lookup.state is not CacheLookupState.HIT or lookup.payload is None:
        return None
    try:
        payload = PhaseThreeFingerprintPayload.model_validate_json(lookup.payload)
    except ValueError:
        cache.invalidations += 1
        return None
    if payload.source_sha256 != source.source_sha256:
        cache.invalidations += 1
        return None
    return tuple(item.to_internal() for item in payload.fingerprints)


def store_cached_fingerprints(
    cache: ContentCache,
    source: CloneSource,
    config: AnalysisConfig,
    fingerprints: tuple[CloneFingerprint, ...],
) -> None:
    payload = PhaseThreeFingerprintPayload(
        source_sha256=source.source_sha256,
        fingerprints=tuple(CachedCloneFingerprint.from_internal(item) for item in fingerprints),
    )
    cache.put(
        fingerprint_cache_key(source, config),
        payload.model_dump_json().encode("utf-8"),
    )
