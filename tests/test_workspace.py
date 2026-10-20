from __future__ import annotations

import errno
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from code_analysis.cache import CacheKey, CacheLookupState, ContentCache
from code_analysis.config import AnalysisConfig
from code_analysis.project import discover_context
from code_analysis.reports.json_report import render_json
from code_analysis.service import scan
from code_analysis.workspace import (
    CacheStatistics,
    GenerationKind,
    commit_generation,
    create_run_record,
    list_generations,
    prepare_workspace,
    prune_generations,
    set_generation_pin,
    write_workspace_results,
)


def _context(root: Path):
    root.mkdir()
    (root / "sample.py").write_text("value = 1\n", encoding="utf-8")
    return discover_context(root, AnalysisConfig())


def _cache_key(name: str = "one") -> CacheKey:
    digest = hashlib.sha256(name.encode()).hexdigest()
    return CacheKey(
        namespace="module-observations",
        source_sha256=digest,
        analyzer_version="2",
        python_version="3.12.13",
        configuration_sha256="0" * 64,
    )


def test_workspace_initialization_is_owned_ignored_and_idempotent(tmp_path: Path) -> None:
    context = _context(tmp_path / "project")
    first = prepare_workspace(context)
    second = prepare_workspace(context)
    assert first == second
    assert first.root == context.source.physical_root / ".code-analysis"
    assert (first.root / ".gitignore").read_text(encoding="utf-8") == "*\n"
    marker = json.loads(first.marker.read_text(encoding="utf-8"))
    assert marker["schema_name"] == "code-analysis-workspace"
    assert marker["cache_layout_version"] == "current"
    assert first.reports.is_dir()
    assert first.runs.is_dir()
    assert first.generations.is_dir()
    assert first.pins.is_dir()
    assert all(first.kind_directory(kind).is_dir() for kind in GenerationKind)
    assert first.cache.is_dir()
    assert first.decisions.is_dir()
    subprocess.run(["git", "init", "-q"], cwd=context.source.physical_root, check=True)
    ignored = subprocess.run(
        ["git", "check-ignore", ".code-analysis/workspace.json"],
        cwd=context.source.physical_root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert ignored.returncode == 0


def test_directory_without_marker_is_not_claimed(tmp_path: Path) -> None:
    context = _context(tmp_path / "project")
    workspace = context.source.physical_root / ".code-analysis"
    workspace.mkdir()
    sentinel = workspace / "owner.txt"
    sentinel.write_text("not ours", encoding="utf-8")
    with pytest.raises(OSError) as exc_info:
        prepare_workspace(context)
    assert exc_info.value.errno == errno.EBADMSG
    assert sentinel.read_text(encoding="utf-8") == "not ours"


def test_symlinked_workspace_is_rejected(tmp_path: Path) -> None:
    context = _context(tmp_path / "project")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (context.source.physical_root / ".code-analysis").symlink_to(
        elsewhere, target_is_directory=True
    )
    with pytest.raises(OSError) as exc_info:
        prepare_workspace(context)
    assert exc_info.value.errno == errno.ELOOP


def test_explicit_external_workspace_is_supported(tmp_path: Path) -> None:
    context = _context(tmp_path / "project")
    workspace = prepare_workspace(context, explicit_path=tmp_path / "external-workspace")
    assert workspace.root == tmp_path / "external-workspace"
    assert not (context.source.physical_root / ".code-analysis").exists()


def test_external_workspace_cannot_be_reused_for_another_project(tmp_path: Path) -> None:
    first = _context(tmp_path / "first")
    second = _context(tmp_path / "second")
    external = tmp_path / "external-workspace"
    prepare_workspace(first, explicit_path=external)
    with pytest.raises(OSError) as exc_info:
        prepare_workspace(second, explicit_path=external)
    assert exc_info.value.errno == errno.EBADMSG


def test_atomic_latest_reports_and_local_run_record(tmp_path: Path) -> None:
    context = _context(tmp_path / "project")
    workspace = prepare_workspace(context)
    report_json = '{"schema_name":"code-analysis-report"}\n'
    report_hash = hashlib.sha256(report_json.encode()).hexdigest()
    run = create_run_record(
        context,
        workspace,
        started_at=datetime(2026, 8, 21, tzinfo=UTC),
        duration_ms=12,
        command=("scan", "."),
        providers=(),
        portable_report_sha256=report_hash,
        interpreter_path="/host/python",
        cache=CacheStatistics(hits=1, misses=2, bytes=3),
    )
    manifest = write_workspace_results(
        workspace,
        report_json=report_json,
        report_markdown="# Report\n",
        run=run,
    )
    latest = workspace.kind_directory(GenerationKind.ANALYSIS) / "latest"
    assert latest.is_symlink()
    assert latest.readlink() == Path(manifest.generation_id)
    assert (latest / "report.json").read_text(encoding="utf-8") == report_json
    assert (latest / "report.md").read_text(encoding="utf-8") == "# Report\n"
    restored = json.loads((latest / "run.json").read_text(encoding="utf-8"))
    assert restored["portable_report_sha256"] == report_hash
    assert restored["storage_configuration_sha256"] == context.config.workspace.identity()
    assert restored["cache"] == {
        "bytes": 3,
        "enabled": True,
        "evictions": 0,
        "hits": 1,
        "invalidations": 0,
        "misses": 2,
    }
    assert list_generations(workspace) == (manifest,)


def test_generation_pinning_and_prune_are_bounded(tmp_path: Path) -> None:
    context = _context(tmp_path / "project")
    workspace = prepare_workspace(context)
    generations = [
        commit_generation(
            workspace,
            kind=GenerationKind.PROFILE,
            artifacts={"profile.json": f"{index}\n".encode()},
            command=("profile", str(index)),
            interpreter_path="/host/python",
            portable_identity_sha256=f"{index:064x}",
            created_at=datetime(2026, 8, 21, 0, 0, index, tzinfo=UTC),
        )
        for index in range(3)
    ]
    set_generation_pin(workspace, generations[0].generation_id, pinned=True)
    planned = prune_generations(workspace, kind=GenerationKind.PROFILE, keep_older=0, dry_run=True)
    assert planned == (generations[1].generation_id,)
    assert len(list_generations(workspace, kind=GenerationKind.PROFILE)) == 3
    assert prune_generations(workspace, kind=GenerationKind.PROFILE, keep_older=0) == planned
    assert {item.generation_id for item in list_generations(workspace)} == {
        generations[0].generation_id,
        generations[2].generation_id,
    }
    set_generation_pin(workspace, generations[0].generation_id, pinned=False)


def test_generation_rejects_artifact_symlink_and_hash_tampering(tmp_path: Path) -> None:
    context = _context(tmp_path / "project")
    workspace = prepare_workspace(context)
    manifest = commit_generation(
        workspace,
        kind=GenerationKind.COVERAGE,
        artifacts={"coverage.json": b"{}\n"},
        command=("coverage-import",),
        interpreter_path="/host/python",
        portable_identity_sha256="1" * 64,
    )
    artifact = (
        workspace.kind_directory(GenerationKind.COVERAGE) / manifest.generation_id / "coverage.json"
    )
    artifact.write_text("changed\n", encoding="utf-8")
    with pytest.raises(OSError) as exc_info:
        list_generations(workspace, kind=GenerationKind.COVERAGE)
    assert exc_info.value.errno == errno.EBADMSG


def test_generation_byte_budget_keeps_latest_and_pins(tmp_path: Path) -> None:
    context = _context(tmp_path / "project")
    workspace = prepare_workspace(context)
    first = commit_generation(
        workspace,
        kind=GenerationKind.COVERAGE,
        artifacts={"coverage.json": b"a" * 200},
        command=("coverage-import",),
        interpreter_path="/host/python",
        portable_identity_sha256="2" * 64,
        created_at=datetime(2026, 8, 21, 0, 0, 1, tzinfo=UTC),
    )
    set_generation_pin(workspace, first.generation_id, pinned=True)
    latest = commit_generation(
        workspace,
        kind=GenerationKind.COVERAGE,
        artifacts={"coverage.json": b"b" * 200},
        command=("coverage-import",),
        interpreter_path="/host/python",
        portable_identity_sha256="3" * 64,
        created_at=datetime(2026, 8, 21, 0, 0, 2, tzinfo=UTC),
    )
    assert (
        prune_generations(
            workspace,
            kind=GenerationKind.COVERAGE,
            keep_older=5,
            max_unpinned_bytes=0,
        )
        == ()
    )
    assert {item.generation_id for item in list_generations(workspace)} == {
        first.generation_id,
        latest.generation_id,
    }


def test_content_cache_reports_hit_miss_and_corruption(tmp_path: Path) -> None:
    context = _context(tmp_path / "project")
    workspace = prepare_workspace(context)
    cache = ContentCache(workspace)
    key = _cache_key()
    assert cache.get(key).state is CacheLookupState.MISS
    cache.put(key, b"payload")
    hit = cache.get(key)
    assert hit.state is CacheLookupState.HIT
    assert hit.payload == b"payload"
    (workspace.cache / f"{key.digest()}.json").write_text("{}", encoding="utf-8")
    corrupt = cache.get(key)
    assert corrupt.state is CacheLookupState.CORRUPT
    assert corrupt.payload is None

    workspace.marker.write_text("{}", encoding="utf-8")
    with pytest.raises(OSError) as exc_info:
        ContentCache(workspace)
    assert exc_info.value.errno == errno.EBADMSG


def test_phase_three_fingerprints_are_reused_from_typed_source_cache(tmp_path: Path) -> None:
    context = _context(tmp_path / "project")
    workspace = prepare_workspace(context)
    first_cache = ContentCache(workspace)
    first = scan(context, first_cache)
    assert first_cache.misses == 1
    assert first_cache.hits == 0

    second_cache = ContentCache(workspace)
    second = scan(context, second_cache)
    assert second_cache.hits == 1
    assert second_cache.misses == 0
    assert render_json(second) == render_json(first)


def test_fingerprint_cache_separates_identical_sources_by_logical_identity() -> None:
    from code_analysis.analysis.clones import CloneSource
    from code_analysis.fingerprint_cache import fingerprint_cache_key

    config = AnalysisConfig()
    source = "def same():\n    return 1\n"
    first = CloneSource("first", "first.py", "a" * 64, source)
    moved = CloneSource("first", "nested/first.py", "a" * 64, source)
    renamed = CloneSource("second", "first.py", "a" * 64, source)
    keys = {fingerprint_cache_key(item, config).digest() for item in (first, moved, renamed)}
    assert len(keys) == 3
    assert fingerprint_cache_key(first, config) == fingerprint_cache_key(first, config)


def test_report_write_revalidates_workspace_marker(tmp_path: Path) -> None:
    context = _context(tmp_path / "project")
    workspace = prepare_workspace(context)
    workspace.marker.write_text("{}", encoding="utf-8")
    report_json = "{}\n"
    run = create_run_record(
        context,
        workspace,
        started_at=datetime(2026, 8, 21, tzinfo=UTC),
        duration_ms=0,
        command=("scan", "."),
        providers=(),
        portable_report_sha256=hashlib.sha256(report_json.encode()).hexdigest(),
        interpreter_path="/host/python",
    )
    with pytest.raises(OSError) as exc_info:
        write_workspace_results(
            workspace,
            report_json=report_json,
            report_markdown="# Report\n",
            run=run,
        )
    assert exc_info.value.errno == errno.EBADMSG
    assert not (workspace.reports / "latest.json").exists()


def test_zero_sized_cache_disables_writes_and_eviction_is_bounded(tmp_path: Path) -> None:
    context = _context(tmp_path / "project")
    workspace = prepare_workspace(context)
    disabled = ContentCache(workspace, maximum_bytes=0)
    disabled.put(_cache_key("disabled"), b"payload")
    assert not tuple(workspace.cache.glob("*.json"))

    bounded = ContentCache(workspace, maximum_bytes=350)
    bounded.put(_cache_key("first"), b"a" * 100)
    bounded.put(_cache_key("second"), b"b" * 100)
    assert bounded.size_bytes() <= 350
    assert workspace.reports.is_dir()
    assert workspace.runs.is_dir()
