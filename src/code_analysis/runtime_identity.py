"""Local metadata validation and portable runtime identity reconciliation."""

from __future__ import annotations

import errno
import hashlib
from pathlib import Path
from typing import Self

from pydantic import Field, StrictInt, StrictStr, model_validator

from code_analysis.models import StrictModel
from code_analysis.profile_models import ProfileProducerBinding
from code_analysis.runtime_models import (
    CollectionIdentity,
    RuntimeArtifact,
    RuntimeProcessProof,
    RuntimeSourceHash,
)


class RuntimeRoot(StrictModel):
    physical: Path
    logical: StrictStr = Field(pattern=r"^[a-z][a-z0-9-]*$")
    classification: StrictStr = Field(
        pattern=r"^(project|standard-library|third-party|environment)$"
    )

    @model_validator(mode="after")
    def absolute_root(self) -> Self:
        if not self.physical.is_absolute():
            raise ValueError("collection roots must be absolute")
        return self


class CollectionMetadata(StrictModel):
    """Local supplied evidence, not independently authenticated attestation."""

    schema_name: StrictStr = Field(
        default="code-analysis-runtime-metadata", pattern=r"^code-analysis-runtime-metadata$"
    )
    schema_version: StrictStr = Field(default="1.0.0", pattern=r"^1\.0\.0$")
    collection: CollectionIdentity
    process: RuntimeProcessProof = RuntimeProcessProof()
    roots: tuple[RuntimeRoot, ...] = ()
    source_hashes: tuple[RuntimeSourceHash, ...] = ()
    observed_status: StrictInt | None = None
    producer_bindings: tuple[ProfileProducerBinding, ...] = ()

    @model_validator(mode="after")
    def unique_roots_sources(self) -> Self:
        bindings = [item.profile_sha256 for item in self.producer_bindings]
        if bindings != sorted(set(bindings)):
            raise ValueError("producer bindings must have unique sorted profile hashes")
        roots = [item.physical for item in self.roots]
        if len(roots) != len(set(roots)):
            raise ValueError("collection physical roots must be unique")
        paths = [item.path for item in self.source_hashes]
        if paths != sorted(set(paths)):
            raise ValueError("collection source hashes must be unique and sorted")
        return self


def read_bounded_artifact(
    path: Path, *, maximum: int, kind: str, allow_empty: bool = False
) -> tuple[bytes, RuntimeArtifact]:
    """Read regular unchanged bytes, with no symlink traversal or unbounded read."""
    import os
    import stat

    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise OSError(errno.EBADMSG, "runtime artifact is not a regular file")
            if before.st_size > maximum:
                raise OSError(errno.ENODATA, "runtime artifact exceeds byte bound")
            data = stream.read(maximum + 1)
            after = os.fstat(stream.fileno())
            if (before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                raise OSError(errno.EBADMSG, "runtime artifact changed during read")
        current = path.lstat()
        if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ):
            raise OSError(errno.EBADMSG, "runtime artifact replaced during read")
    except OSError as exc:
        raise OSError(exc.errno, "cannot read stable bounded runtime artifact") from None
    if not data and not allow_empty:
        raise OSError(errno.EBADMSG, "runtime artifact is empty")
    return data, RuntimeArtifact(
        sha256=hashlib.sha256(data).hexdigest(), byte_count=len(data), kind=kind
    )
