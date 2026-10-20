"""Explicit tool-owned cProfile entry point, never an import hook or injection."""

from __future__ import annotations

import cProfile
import hashlib
import json
import os
import runpy
import sys
import tempfile
from pathlib import Path


def _atomic_json(path: Path, payload: dict[str, str | int | bool]) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".profile-sidecar-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main() -> None:
    import psutil

    output, metadata, nonce, kind, target, *arguments = sys.argv[1:]
    profile_path, metadata_path = Path(output), Path(metadata)
    owner = psutil.Process()
    proof: dict[str, str | int | bool] = {
        "subject_pid": os.getpid(),
        "parent_pid": os.getppid(),
        "start_identity": str(owner.create_time()),
        "run_nonce": nonce,
        "collection_scope": "subject-interpreter",
        "serialization_complete": False,
    }
    _atomic_json(metadata_path, proof)
    sys.argv = [target, *arguments]
    profiler = cProfile.Profile()
    try:
        profiler.enable()
        if kind == "module":
            runpy.run_module(target, run_name="__main__", alter_sys=True)
        elif kind == "script":
            sys.path.insert(0, str(Path(target).absolute().parent))
            runpy.run_path(target, run_name="__main__")
        else:
            raise ValueError("invalid bootstrap target kind")
    finally:
        profiler.disable()
        fd, temporary = tempfile.mkstemp(prefix=".profile-data-", dir=profile_path.parent)
        os.close(fd)
        try:
            profiler.dump_stats(temporary)
            data = Path(temporary).read_bytes()
            os.replace(temporary, profile_path)
            proof.update(
                {"serialization_complete": True, "profile_sha256": hashlib.sha256(data).hexdigest()}
            )
            _atomic_json(metadata_path, proof)
        finally:
            Path(temporary).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
