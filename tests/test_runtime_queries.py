"""Saved-only runtime query and metadata-retention coverage."""

from __future__ import annotations

import errno
import json
import marshal
from pathlib import Path

import pytest
from pydantic import ValidationError

from code_analysis.cli import main
from code_analysis.profile_ingestion import ingest_profiles
from code_analysis.profiling import load_profile_json, render_profile_json
from code_analysis.runtime_artifacts import publish_runtime_report
from code_analysis.runtime_identity import CollectionMetadata
from code_analysis.runtime_models import (
    CategoryMatch,
    LifecycleOutcome,
    LifecycleState,
    RuntimeStepObservation,
)
from code_analysis.runtime_queries import (
    RuntimeQueryProjection,
    RuntimeQueryRequest,
    RuntimeQuerySection,
    build_runtime_query,
)


def _report(tmp_path: Path):
    profile = tmp_path / "query.prof"
    profile.write_bytes(marshal.dumps({("module.py", 1, "work"): (1, 1, 0.1, 0.1, {})}))
    report, _ = ingest_profiles((profile,))
    runtime = report.runtime.model_copy(
        update={
            "lifecycle": (
                LifecycleOutcome(sequence=0, state=LifecycleState.INGESTION, reason="synthetic"),
            ),
            "steps": (
                RuntimeStepObservation(
                    step="subject",
                    observed_status=0,
                    stdout_sha256="a" * 64,
                    stderr_sha256="b" * 64,
                    stdout_bytes=0,
                    stderr_bytes=0,
                ),
            ),
        }
    )
    return report.model_copy(update={"runtime": runtime})


def _saved_report(tmp_path: Path) -> Path:
    report = _report(tmp_path)
    saved = tmp_path / "runtime.json"
    saved.write_text(render_profile_json(report, compact=True), encoding="utf-8")
    return saved


def test_runtime_query_registry_uses_retained_typed_records(tmp_path: Path) -> None:
    report = _report(tmp_path)
    for section, expected in (
        (RuntimeQuerySection.CATEGORIES, report.runtime.categories),
        (RuntimeQuerySection.CORRELATIONS, report.runtime.correlations),
        (RuntimeQuerySection.PROCESS, (report.runtime.process,)),
        (RuntimeQuerySection.LIFECYCLE, report.runtime.lifecycle),
        (RuntimeQuerySection.STEPS, report.runtime.steps),
    ):
        projection = build_runtime_query(report, RuntimeQueryRequest(section=section))
        assert projection.records == expected
        assert projection.eligible_count == len(expected)
        assert projection.retained_count == len(expected)
        assert projection.omitted_count == 0
        assert projection.source_report_sha256


def test_runtime_query_projection_rejects_bool_bounds_and_section_type_mismatch() -> None:
    with pytest.raises(ValidationError):
        RuntimeQueryRequest(section=RuntimeQuerySection.PROCESS, max_records=True)
    with pytest.raises(ValidationError):
        RuntimeQueryProjection(
            source_report_sha256="a" * 64,
            source_profile_identity_sha256="b" * 64,
            section=RuntimeQuerySection.PROCESS,
            max_records=1,
            eligible_count=1,
            retained_count=1,
            omitted_count=0,
            truncated=False,
            source_completeness="partial",
            records=(CategoryMatch(frame_id="profile-frame-00000000000000000000"),),
            limitations=(),
        )


def test_runtime_query_cli_is_saved_only_and_marks_partial_source(tmp_path: Path, capsys) -> None:
    saved = _saved_report(tmp_path)
    assert (
        main(["runtime", "query", str(saved), "--section", "steps", "--format", "json"])
        == errno.ENODATA
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["section"] == "steps"
    assert payload["retained_count"] == 1
    assert payload["source_completeness"] == "partial"


def test_runtime_query_bound_and_invalid_bound_are_explicit(tmp_path: Path, capsys) -> None:
    saved = _saved_report(tmp_path)
    assert (
        main(
            [
                "runtime",
                "query",
                str(saved),
                "--section",
                "process",
                "--max-records",
                "0",
            ]
        )
        == errno.EINVAL
    )
    assert (
        main(
            [
                "runtime",
                "query",
                str(saved),
                "--section",
                "process",
                "--max-records",
                "1",
                "--format",
                "tsv",
            ]
        )
        == errno.ENODATA
    )
    assert capsys.readouterr().out.startswith("section\tindex\trecord_json\nprocess\t0\t")


def test_runtime_query_validates_manifest_summary_digest(tmp_path: Path, capsys) -> None:
    report = _report(tmp_path)
    rendered = tmp_path / "rendered"
    publish_runtime_report(report, rendered)
    (rendered / "runtime-summary.json").write_text("{}", encoding="utf-8")

    assert main(["runtime", "query", str(rendered), "--section", "process"]) == errno.EBADMSG
    assert "runtime summary does not match manifest" in capsys.readouterr().err


def test_offline_metadata_artifact_is_retained_and_reidentifies_report(
    tmp_path: Path, capsys
) -> None:
    profile = tmp_path / "offline.prof"
    profile.write_bytes(marshal.dumps({("module.py", 1, "work"): (1, 1, 0.1, 0.1, {})}))
    metadata = tmp_path / "metadata.json"
    metadata.write_text(CollectionMetadata(collection={}).model_dump_json(), encoding="utf-8")

    assert (
        main(
            [
                "runtime",
                "ingest",
                "--profile",
                str(profile),
                "--metadata",
                str(metadata),
                "--json",
            ]
        )
        == errno.ENODATA
    )
    report = load_profile_json(capsys.readouterr().out)
    assert {item.kind for item in report.runtime.artifacts} == {"metadata", "profile"}
    from code_analysis.runtime_correlation import profile_content_identity

    assert report.profile_identity_sha256 == profile_content_identity(report)
