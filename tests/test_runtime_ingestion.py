"""Synthetic offline runtime evidence; no target code is imported or executed."""

from __future__ import annotations

import errno
import hashlib
import marshal
from pathlib import Path

import pytest
from pydantic import ValidationError

from code_analysis.models import Completeness
from code_analysis.profile_ingestion import ingest_profiles, metrics
from code_analysis.profiling import render_profile_json
from code_analysis.runtime_identity import CollectionMetadata, RuntimeRoot
from code_analysis.runtime_models import CollectionIdentity, ProcessProofState, RuntimeProcessProof
from code_analysis.runtime_spec import (
    ArgumentRole,
    RuntimeAnalysisConfig,
    RuntimeArgument,
    RuntimeCategoryRule,
)


def profile(tmp_path: Path, name: str = "one.prof") -> Path:
    path = tmp_path / name
    path.write_bytes(
        marshal.dumps(
            {
                ("/capture/app/service.py", 10, "serve"): (
                    1,
                    3,
                    0.125,
                    0.5,
                    {
                        ("/capture/app/service.py", 1, "missing_caller"): (3, 1, 0.125, 0.5),
                    },
                ),
                ("~", 0, "<built-in method read>"): (2, 2, 0.1, 0.1, {}),
            }
        )
    )
    return path


@pytest.mark.parametrize("value", [True, -1.0, float("nan"), float("inf"), 0.0])
def test_runtime_deadlines_reject_invalid_values(value) -> None:
    with pytest.raises(ValidationError):
        RuntimeAnalysisConfig(total_seconds=value)


@pytest.mark.parametrize("value", [True, 0, -1, 21])
def test_runtime_input_bounds(value) -> None:
    with pytest.raises(ValidationError):
        RuntimeAnalysisConfig(max_inputs=value)


@pytest.mark.parametrize(
    "value",
    [
        (True, 1, 0.1, 0.1),
        (2, 1, 0.0, 0.1),
        (1, 1, float("nan"), 0.1),
        (1, 1, -0.1, 0.1),
        (1, 1, "0.1", 0.1),
        1,
        (),
        (1, 1, 0.0),
    ],
)
def test_native_metrics_fail_closed(value) -> None:
    with pytest.raises(ValueError):
        metrics(value)


def test_offline_unknown_metrics_and_complete_graph(tmp_path: Path) -> None:
    path = profile(tmp_path)
    before = path.read_bytes()
    report, portable = ingest_profiles((path,))
    assert report.observed_statuses == (None,)
    assert report.resources[0].wall_seconds is None
    assert report.source_report_sha256 is None
    assert report.workload_identity_sha256 is None
    assert len(report.frames) == 3
    assert len(report.runtime_edges) == 1
    missing = next(item for item in report.frames if item.function == "missing_caller")
    assert missing.metrics_observed is False
    assert missing.total_calls is None
    assert missing.cumulative_seconds is None
    native = next(item for item in report.frames if item.function.startswith("<built-in"))
    assert native.classification.value == "native"
    assert report.completeness == Completeness.PARTIAL
    assert path.read_bytes() == before
    assert len(portable) == 1
    assert b"/capture/" not in next(iter(portable.values()))


def test_identical_artifact_normalization_and_duplicate_inputs(tmp_path: Path) -> None:
    path = profile(tmp_path)
    first, _ = ingest_profiles((path,))
    second, _ = ingest_profiles((path, path))
    assert render_profile_json(first, compact=True) == render_profile_json(second, compact=True)
    assert len(second.runtime.artifacts) == 1
    assert second.runtime.artifacts[0].sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


def test_separate_captures_not_pooled(tmp_path: Path) -> None:
    left = profile(tmp_path)
    right = tmp_path / "other.prof"
    right.write_bytes(
        marshal.dumps({("/capture/app/service.py", 10, "serve"): (1, 1, 0.25, 0.75, {})})
    )
    report, _ = ingest_profiles((left, right))
    serves = [item for item in report.frames if item.function == "serve"]
    assert len(serves) == 2
    assert {item.total_seconds for item in serves} == {0.125, 0.25}
    assert len({item.artifact_sha256 for item in serves}) == 2
    assert report.repetitions == 1


def test_prevalent_limits_preserve_partiality(tmp_path: Path) -> None:
    report, _ = ingest_profiles(
        (profile(tmp_path),), config=RuntimeAnalysisConfig(max_frames=1, max_edges=1)
    )
    assert report.runtime.original_frame_count == 3
    assert len(report.frames) == 1
    assert report.completeness == Completeness.PARTIAL
    assert any(item.code == "runtime-profile-truncated" for item in report.diagnostics)


def test_unknown_same_basename_remains_distinct(tmp_path: Path) -> None:
    path = tmp_path / "same.prof"
    path.write_bytes(
        marshal.dumps(
            {(f"/{root}/same.py", 1, "f"): (1, 1, 0.1, 0.1, {}) for root in ("left", "right")}
        )
    )
    report, _ = ingest_profiles((path,))
    assert len({item.path for item in report.frames}) == 2


def test_declared_root_collision_fails(tmp_path: Path) -> None:
    path = tmp_path / "same.prof"
    path.write_bytes(
        marshal.dumps(
            {(f"/{root}/same.py", 1, "f"): (1, 1, 0.1, 0.1, {}) for root in ("left", "right")}
        )
    )
    metadata = CollectionMetadata(
        collection=CollectionIdentity(),
        roots=tuple(
            RuntimeRoot(physical=Path(f"/{root}"), logical="project", classification="project")
            for root in ("left", "right")
        ),
    )
    with pytest.raises(OSError) as error:
        ingest_profiles((path,), metadata=metadata)
    assert error.value.errno == errno.EBADMSG


def test_categories_are_explicit_and_deterministic(tmp_path: Path) -> None:
    rules = (
        RuntimeCategoryRule(rule_id="first", category="io", priority=1, function_pattern="serve"),
        RuntimeCategoryRule(rule_id="second", category="work", priority=2),
    )
    report, _ = ingest_profiles(
        (profile(tmp_path),), config=RuntimeAnalysisConfig(category_rules=rules)
    )
    frame = next(item for item in report.frames if item.function == "serve")
    category = next(item for item in report.runtime.categories if item.frame_id == frame.frame_id)
    assert category.primary_category == "io"
    assert category.rule_ids == ("first", "second")
    assert category.categories == ("io", "work")


@pytest.mark.parametrize(
    "data",
    [b"", b"garbage", marshal.dumps({}), marshal.dumps({("p.py", 1, "f"): (1, 1, -0.1, 0.1, {})})],
)
def test_malformed_profile_is_never_empty_success(tmp_path: Path, data: bytes) -> None:
    path = tmp_path / "bad.prof"
    path.write_bytes(data)
    with pytest.raises(OSError) as error:
        ingest_profiles((path,))
    assert error.value.errno == errno.EBADMSG


def test_profile_byte_bound(tmp_path: Path) -> None:
    with pytest.raises(OSError) as error:
        ingest_profiles((profile(tmp_path),), config=RuntimeAnalysisConfig(max_profile_bytes=1))
    assert error.value.errno == errno.ENODATA


def test_secret_redaction_cannot_prove_semantic_equality() -> None:
    argument = RuntimeArgument(role=ArgumentRole.SECRET_REFERENCE, value="not-portable")
    assert argument.semantic_token() is None
    assert (
        RuntimeArgument(role=ArgumentRole.OUTPUT_PATH, value="/tmp/one").semantic_token()
        == RuntimeArgument(role=ArgumentRole.OUTPUT_PATH, value="/tmp/two").semantic_token()
    )
    with pytest.raises(ValidationError):
        RuntimeProcessProof(state=ProcessProofState.VERIFIED)


def test_collection_unknown_and_physical_paths_rejected() -> None:
    assert "workload_sha256" in CollectionIdentity().missing_fields()
    with pytest.raises(ValidationError):
        CollectionIdentity(semantic_argv=("/Users/private/secret",))


def test_captured_source_hash_required_for_static_subject_binding(tmp_path: Path) -> None:
    from code_analysis.config import AnalysisConfig
    from code_analysis.project import discover_context
    from code_analysis.runtime_models import RuntimeSourceHash, SourceBindingState
    from code_analysis.service import scan

    root = tmp_path / "source"
    root.mkdir()
    module = root / "work.py"
    module.write_text("def work():\n    return 1\n", encoding="utf-8")
    source = scan(discover_context(root, AnalysisConfig()))
    path = tmp_path / "bound.prof"
    path.write_bytes(marshal.dumps({(str(module), 1, "work"): (1, 1, 0.1, 0.1, {})}))
    unbound, _ = ingest_profiles((path,), repo=root, source_report=source)
    assert unbound.frames[0].subject_id is None
    metadata = CollectionMetadata(
        collection=CollectionIdentity(),
        roots=(RuntimeRoot(physical=root, logical="project", classification="project"),),
        source_hashes=(
            RuntimeSourceHash(
                path="work.py", sha256=hashlib.sha256(module.read_bytes()).hexdigest()
            ),
        ),
    )
    bound, _ = ingest_profiles((path,), metadata=metadata, source_report=source)
    assert bound.runtime.source_binding == SourceBindingState.MATCHED
    assert bound.frames[0].subject_id is not None
    assert bound.runtime.correlations[0].complexity is not None
    mismatch = metadata.model_copy(
        update={"source_hashes": (RuntimeSourceHash(path="work.py", sha256="a" * 64),)}
    )
    rejected, _ = ingest_profiles((path,), metadata=mismatch, source_report=source)
    assert rejected.runtime.source_binding == SourceBindingState.MISMATCHED
    assert rejected.frames[0].subject_id is None


def test_caller_recursion_count_order_is_total_then_primitive() -> None:
    from code_analysis.profile_ingestion import caller_metrics

    caller = caller_metrics((7, 1, 0.1, 0.2))
    assert caller.primitive == 1
    assert caller.total == 7
