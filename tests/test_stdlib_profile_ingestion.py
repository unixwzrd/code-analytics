"""Independent standard-library profiler controls; never execute consumer code."""

from __future__ import annotations

import cProfile
import errno
import hashlib
import marshal
import profile
import pstats
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from code_analysis.profile_ingestion import caller_metrics, ingest_profiles, native_signature
from code_analysis.profile_models import (
    CallerRepresentation,
    ProfileProducer,
    ProfileProducerBinding,
    RuntimeCallEdge,
)
from code_analysis.profiling import load_profile_json, render_profile_json, render_profile_markdown
from code_analysis.projections.runtime import build_runtime_graph, runtime_dot, runtime_table
from code_analysis.pstats_table import ProfileStats
from code_analysis.runtime_comparison import compare_runtime_profiles
from code_analysis.runtime_identity import CollectionMetadata
from code_analysis.runtime_models import RuntimeSelection
from code_analysis.runtime_spec import RuntimeAnalysisConfig


@pytest.mark.parametrize("profiler_type", [profile.Profile, cProfile.Profile])
def test_typed_pstats_reader_preserves_native_table_and_reload(tmp_path, profiler_type):
    capture = profiler_type()
    capture.runcall(recursive, 3)
    original = tmp_path / "original.prof"
    restored = tmp_path / "restored.prof"
    capture.dump_stats(str(original))
    reader = ProfileStats(str(original))
    assert reader.stats == pstats.Stats(str(original)).stats
    reader.dump_stats(str(restored))
    assert native_signature(ProfileStats(str(restored)).stats) == native_signature(reader.stats)
    assert pstats.Stats(str(restored)).stats == reader.stats


@pytest.mark.parametrize(
    "profiler_type,producer", [(profile.Profile, "profile"), (cProfile.Profile, "cprofile")]
)
def test_bound_producer_preserves_native_ingestion(tmp_path, profiler_type, producer):
    capture = profiler_type()
    capture.runcall(recursive, 3)
    path = tmp_path / "external.pstats"
    capture.dump_stats(str(path))
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    metadata = CollectionMetadata.model_validate_json(
        '{"collection":{},"producer_bindings":[{"profile_sha256":"'
        + sha
        + '","producer":"'
        + producer
        + '"}]}'
    )
    original, original_local = ingest_profiles((path,))
    declared, local = ingest_profiles((path,), metadata=metadata)
    repeated, _ = ingest_profiles((path,), metadata=metadata)
    assert declared.producer is ProfileProducer(producer)
    assert declared.producer_bindings == metadata.producer_bindings
    assert declared.frames == original.frames and declared.runtime_edges == original.runtime_edges
    assert local == original_local
    assert declared.completeness == original.completeness
    assert declared.profile_identity_sha256 != original.profile_identity_sha256
    assert render_profile_json(declared) == render_profile_json(repeated)
    assert load_profile_json(render_profile_json(declared)) == declared
    assert not any("Producer identity is unknown" in text for text in declared.limitations)
    assert any("not independently authenticated" in text for text in declared.limitations)
    with pytest.raises(OSError) as error:
        compare_runtime_profiles(declared, repeated)
    assert error.value.errno == errno.EINVAL
    delta = compare_runtime_profiles(declared, repeated, diagnostic_only=True)
    assert next(d for d in delta.decisions if d.field == "profiler_producer").outcome == "matched"
    assert not delta.claim_eligible
    old_metadata, _ = ingest_profiles((path,), metadata=CollectionMetadata(collection={}))
    assert old_metadata.producer is ProfileProducer.UNKNOWN


@pytest.mark.parametrize("producer", ["stdlib_profile", "unknown", "bogus", True, None])
def test_producer_binding_rejects_invalid_enum(producer):
    import json

    with pytest.raises(ValidationError):
        CollectionMetadata.model_validate_json(
            json.dumps(
                {
                    "collection": {},
                    "producer_bindings": [{"profile_sha256": "a" * 64, "producer": producer}],
                }
            )
        )


def test_producer_hash_mismatch_rejected_before_worker(tmp_path, monkeypatch):
    path = tmp_path / "input.prof"
    path.write_bytes(marshal.dumps({("source.py", 1, "f"): (1, 1, 0.1, 0.1, {})}))
    binding = ProfileProducerBinding(profile_sha256="a" * 64, producer=ProfileProducer.PROFILE)
    metadata = CollectionMetadata(collection={}, producer_bindings=(binding,))

    def forbidden(*args, **kwargs):
        pytest.fail("binding mismatch must fail before pstats worker")

    monkeypatch.setattr(subprocess, "run", forbidden)
    with pytest.raises(OSError) as error:
        ingest_profiles((path,), metadata=metadata)
    assert error.value.errno == errno.EBADMSG
    with pytest.raises(ValidationError):
        CollectionMetadata(collection={}, producer_bindings=(binding, binding))


def test_multiple_producers_bind_each_input_and_remain_unqualified(tmp_path):
    paths, bindings = [], []
    for number, producer in enumerate((ProfileProducer.PROFILE, ProfileProducer.CPROFILE)):
        path = tmp_path / f"{number}.prof"
        path.write_bytes(marshal.dumps({("source.py", number + 1, "f"): (1, 1, 0.1, 0.1, {})}))
        paths.append(path)
        bindings.append(
            ProfileProducerBinding(
                profile_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), producer=producer
            )
        )
    bindings.sort(key=lambda item: item.profile_sha256)
    mixed, _ = ingest_profiles(
        tuple(paths), metadata=CollectionMetadata(collection={}, producer_bindings=tuple(bindings))
    )
    assert mixed.producer is ProfileProducer.UNKNOWN
    assert mixed.producer_bindings == tuple(bindings)
    assert any("Mixed declared producers" in text for text in mixed.limitations)
    missing, _ = ingest_profiles(
        tuple(paths), metadata=CollectionMetadata(collection={}, producer_bindings=(bindings[0],))
    )
    assert missing.producer is ProfileProducer.UNKNOWN
    assert any("undeclared inputs" in text for text in missing.limitations)
    reports = []
    for path in paths:
        binding = next(
            item
            for item in bindings
            if item.profile_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
        )
        report, _ = ingest_profiles(
            (path, path), metadata=CollectionMetadata(collection={}, producer_bindings=(binding,))
        )
        assert report.producer is binding.producer
        reports.append(report)
    delta = compare_runtime_profiles(*reports, diagnostic_only=True)
    assert (
        next(d for d in delta.decisions if d.field == "profiler_producer").outcome == "mismatched"
    )
    assert not delta.claim_eligible
    payload = mixed.model_dump()
    payload["producer"] = ProfileProducer.PROFILE
    with pytest.raises(ValidationError):
        type(mixed).model_validate(payload)
    payload = mixed.model_dump()
    payload["producer_bindings"][0]["profile_sha256"] = "0" * 64
    with pytest.raises(ValidationError):
        type(mixed).model_validate(payload)


def leaf() -> int:
    return sum(range(5))


def ordinary() -> int:
    return leaf() + leaf()


def recursive(depth: int) -> int:
    return recursive(depth - 1) + 1 if depth else leaf()


@pytest.mark.parametrize("profiler_type", [profile.Profile, cProfile.Profile])
@pytest.mark.parametrize("recurse", [False, True])
def test_generated_profiles_preserve_every_metric_and_roundtrip(tmp_path, profiler_type, recurse):
    capture = profiler_type()
    capture.runcall(recursive, 4) if recurse else capture.runcall(ordinary)
    path = tmp_path / "independent.pstats"
    capture.dump_stats(str(path))
    raw = pstats.Stats(str(path)).stats
    input_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    report, artifacts = ingest_profiles((path,))
    repeated, _ = ingest_profiles((path,))
    assert report.producer == ProfileProducer.UNKNOWN
    assert report.runtime.original_frame_count == len(report.frames) == len(raw)
    assert (
        report.runtime.original_edge_count
        == len(report.runtime_edges)
        == sum(len(value[4]) for value in raw.values())
    )
    assert render_profile_json(report, compact=True) == render_profile_json(repeated, compact=True)
    assert load_profile_json(render_profile_json(report)) == report
    assert hashlib.sha256(path.read_bytes()).hexdigest() == input_sha
    restored_path = tmp_path / "portable.prof"
    restored_path.write_bytes(next(iter(artifacts.values())))
    restored = pstats.Stats(str(restored_path)).stats
    frames = {(f.path, f.line, f.function): f for f in report.frames}
    edges = {(e.caller_frame_id, e.callee_frame_id): e for e in report.runtime_edges}
    for key, value in restored.items():
        frame = frames[key]
        assert (
            frame.primitive_calls,
            frame.total_calls,
            frame.total_seconds,
            frame.cumulative_seconds,
        ) == value[:4]
        for caller, count in value[4].items():
            edge = edges[(frames[caller].frame_id, frame.frame_id)]
            if profiler_type is profile.Profile:
                assert type(count) is int
                assert edge.total_calls == count
                assert edge.caller_representation == CallerRepresentation.COUNT
                assert edge.primitive_calls is edge.total_seconds is edge.cumulative_seconds is None
            else:
                assert type(count) is tuple
                assert (
                    edge.total_calls,
                    edge.primitive_calls,
                    edge.total_seconds,
                    edge.cumulative_seconds,
                ) == count

    # Portable identities change paths, never frame metrics or caller representations/counts.
    def observations(stats):
        return sorted(
            (key[1:], value[:4], sorted((caller[1:], count) for caller, count in value[4].items()))
            for key, value in stats.items()
        )

    assert observations(raw) == observations(restored)
    assert "/Users/" not in render_profile_json(report)
    assert native_signature(restored) == native_signature(marshal.loads(restored_path.read_bytes()))
    if recurse:
        frame = next(f for f in report.frames if f.function == "recursive")
        assert frame.total_calls > frame.primitive_calls


def integer_profile(tmp_path: Path, caller_value=3) -> Path:
    path = tmp_path / "integer.prof"
    path.write_bytes(
        marshal.dumps(
            {
                ("/capture/service.py", 10, "work"): (
                    1,
                    3,
                    0.1,
                    0.3,
                    {("/capture/service.py", 1, "caller_only"): caller_value},
                )
            }
        )
    )
    return path


def test_mixed_representation_never_guesses_producer(tmp_path):
    path = integer_profile(tmp_path, 0)
    raw = marshal.loads(path.read_bytes())
    raw[("/capture/service.py", 20, "timed")] = (
        1,
        3,
        0.1,
        0.3,
        {("/capture/service.py", 1, "caller_only"): (3, 1, 0.1, 0.3)},
    )
    path.write_bytes(marshal.dumps(raw))
    report, artifacts = ingest_profiles((path,))
    assert report.producer == ProfileProducer.UNKNOWN
    assert {e.caller_representation for e in report.runtime_edges} == set(CallerRepresentation)
    counts = [
        count
        for v in marshal.loads(next(iter(artifacts.values()))).values()
        for count in v[4].values()
    ]
    assert 0 in counts and (3, 1, 0.1, 0.3) in counts


def test_worker_deadline_remains_timeout(tmp_path, monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("private input must not be disclosed", 0.1)

    monkeypatch.setattr("code_analysis.profile_ingestion.subprocess.run", timeout)
    with pytest.raises(OSError) as exc:
        ingest_profiles((integer_profile(tmp_path),))
    assert exc.value.errno == errno.ETIMEDOUT
    assert exc.value.strerror == "pstats ingestion worker timed out"


def test_integer_caller_only_source_free_render_and_comparison(tmp_path):
    report, artifacts = ingest_profiles((integer_profile(tmp_path),))
    assert len(report.frames) == 2 and len(report.runtime_edges) == 1
    assert len(report.runtime.caller_only_frame_ids) == 1
    restored = marshal.loads(next(iter(artifacts.values())))
    assert len(restored) == 1
    assert list(next(iter(restored.values()))[4].values()) == [3]
    saved = load_profile_json(render_profile_json(report))
    (tmp_path / "integer.prof").unlink()
    graph = runtime_dot(saved, RuntimeSelection(direction="TB"))
    assert "rankdir=TB" in graph and "edge timing unavailable" in graph
    assert "Node weights are frame timings" in graph and "Producer: unknown" in graph
    assert "unavailable" in render_profile_markdown(saved)
    assert "0.0" not in runtime_table(saved, kind="callers", maximum=100)
    assert (
        "edge timing unavailable" in build_runtime_graph(saved, RuntimeSelection()).edges[0].label
    )
    with pytest.raises(OSError) as exc:
        compare_runtime_profiles(saved, saved)
    assert exc.value.errno == errno.EINVAL
    delta = compare_runtime_profiles(saved, saved, diagnostic_only=True)
    assert not delta.claim_eligible
    assert (
        next(d for d in delta.decisions if d.field == "caller_edge_timings").outcome == "unresolved"
    )
    assert (
        next(d for d in delta.decisions if d.field == "profiler_producer").outcome == "unresolved"
    )
    different = saved.model_copy(update={"producer": ProfileProducer.CPROFILE})
    known = saved.model_copy(update={"producer": ProfileProducer.PROFILE})
    mismatch = compare_runtime_profiles(known, different, diagnostic_only=True)
    assert (
        next(d for d in mismatch.decisions if d.field == "profiler_producer").outcome
        == "mismatched"
    )


@pytest.mark.parametrize(
    "value",
    [
        True,
        False,
        -1,
        1.0,
        "1",
        (),
        (1, 2),
        (1, 2, 0.1, 0.2),
        (True, 1, 0.1, 0.2),
        (1, 1, -0.1, 0.2),
    ],
)
def test_malformed_callers_have_specific_errors(tmp_path, value):
    with pytest.raises(ValueError):
        caller_metrics(value)
    with pytest.raises(OSError) as exc:
        ingest_profiles((integer_profile(tmp_path, value),))
    assert exc.value.errno == errno.EBADMSG
    assert exc.value.strerror == "pstats worker rejected invalid caller representation"


def test_unavailable_metrics_strict_and_count_bound(tmp_path):
    report, _ = ingest_profiles((integer_profile(tmp_path),))
    edge = report.runtime_edges[0].model_dump()
    for field, value in (
        ("primitive_calls", 0),
        ("total_seconds", 0.0),
        ("total_calls", True),
        ("total_calls", -1),
    ):
        with pytest.raises(ValidationError):
            RuntimeCallEdge.model_validate({**edge, field: value})
    bounded, _ = ingest_profiles(
        (tmp_path / "integer.prof",), config=RuntimeAnalysisConfig(max_frames=1)
    )
    assert bounded.runtime.original_frame_count == 2
    assert bounded.runtime.original_edge_count == 1
    assert len(bounded.runtime_edges) == 0
    assert any(d.code == "runtime-profile-truncated" for d in bounded.diagnostics)


@pytest.mark.parametrize(
    "status,expected",
    [(errno.ENOMEM, errno.ENODATA), (-9, errno.ENODATA), (errno.EBADMSG, errno.EBADMSG)],
)
def test_worker_resource_failure_distinct(tmp_path, monkeypatch, status, expected):
    monkeypatch.setattr(
        "code_analysis.profile_ingestion.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(a, status),
    )
    with pytest.raises(OSError) as exc:
        ingest_profiles((integer_profile(tmp_path),))
    assert exc.value.errno == expected
    assert ("resources" in exc.value.strerror) == (expected == errno.ENODATA)
