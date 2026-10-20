"""Bounded content-addressed cache for immutable analyzer observations."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import Field, StrictStr

from code_analysis.models import StrictModel
from code_analysis.workspace import WorkspacePaths, validate_workspace_ownership

CACHE_ENTRY_SCHEMA_NAME = "code-analysis-cache-entry"
CACHE_ENTRY_SCHEMA_VERSION = "1.0.0"
DEFAULT_CACHE_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class CacheKey:
    namespace: str
    source_sha256: str
    analyzer_version: str
    python_version: str
    configuration_sha256: str
    provider_versions: tuple[str, ...] = ()
    dependency_identities: tuple[str, ...] = ()

    def digest(self) -> str:
        payload = {
            "analyzer_version": self.analyzer_version,
            "configuration_sha256": self.configuration_sha256,
            "dependency_identities": sorted(self.dependency_identities),
            "namespace": self.namespace,
            "provider_versions": sorted(self.provider_versions),
            "python_version": self.python_version,
            "source_sha256": self.source_sha256,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


class CacheLookupState(StrEnum):
    HIT = "hit"
    MISS = "miss"
    CORRUPT = "corrupt"


@dataclass(frozen=True, slots=True)
class CacheLookup:
    state: CacheLookupState
    payload: bytes | None = None
    detail: str | None = None


class CacheEntry(StrictModel):
    schema_name: Literal["code-analysis-cache-entry"] = CACHE_ENTRY_SCHEMA_NAME
    schema_version: Literal["1.0.0"] = CACHE_ENTRY_SCHEMA_VERSION
    key_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    payload_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    payload_base64: StrictStr


class ContentCache:
    """Cache that never treats malformed or stale bytes as valid evidence."""

    def __init__(self, workspace: WorkspacePaths, *, maximum_bytes: int = DEFAULT_CACHE_BYTES):
        if maximum_bytes < 0:
            raise ValueError("maximum cache size must be non-negative")
        validate_workspace_ownership(workspace)
        self.root = workspace.cache
        self.maximum_bytes = maximum_bytes
        self.hits = 0
        self.misses = 0
        self.invalidations = 0
        if not self.root.is_dir() or self.root.is_symlink():
            raise ValueError("cache root must be an owned workspace directory")

    def get(self, key: CacheKey) -> CacheLookup:
        path = self._path(key)
        if not path.is_file():
            self.misses += 1
            return CacheLookup(CacheLookupState.MISS)
        try:
            entry = CacheEntry.model_validate_json(path.read_text(encoding="utf-8"))
            if entry.key_sha256 != key.digest():
                self.invalidations += 1
                return CacheLookup(CacheLookupState.CORRUPT, detail="cache key mismatch")
            payload = base64.b64decode(entry.payload_base64, validate=True)
            if hashlib.sha256(payload).hexdigest() != entry.payload_sha256:
                self.invalidations += 1
                return CacheLookup(CacheLookupState.CORRUPT, detail="cache payload digest mismatch")
        except (OSError, ValueError) as exc:
            self.invalidations += 1
            return CacheLookup(
                CacheLookupState.CORRUPT,
                detail=f"cache entry could not be validated: {type(exc).__name__}",
            )
        os.utime(path, None)
        self.hits += 1
        return CacheLookup(CacheLookupState.HIT, payload=payload)

    def put(self, key: CacheKey, payload: bytes) -> None:
        if self.maximum_bytes == 0:
            return
        digest = key.digest()
        entry = CacheEntry(
            key_sha256=digest,
            payload_sha256=hashlib.sha256(payload).hexdigest(),
            payload_base64=base64.b64encode(payload).decode("ascii"),
        )
        text = json.dumps(entry.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        target = self._path(key)
        temporary = self.root / f".{digest}.tmp"
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(text + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        self.evict()

    def evict(self) -> int:
        entries = tuple(
            sorted(
                (
                    path
                    for path in self.root.glob("*.json")
                    if path.is_file() and not path.is_symlink()
                ),
                key=lambda path: (path.stat().st_mtime_ns, path.name),
            )
        )
        total = sum(path.stat().st_size for path in entries)
        if total <= self.maximum_bytes:
            return 0
        target = int(self.maximum_bytes * 0.9)
        removed = 0
        for path in entries:
            if total <= target:
                break
            size = path.stat().st_size
            path.unlink()
            total -= size
            removed += 1
        return removed

    def size_bytes(self) -> int:
        return sum(
            path.stat().st_size
            for path in self.root.glob("*.json")
            if path.is_file() and not path.is_symlink()
        )

    def _path(self, key: CacheKey) -> Path:
        return self.root / f"{key.digest()}.json"
