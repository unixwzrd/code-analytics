"""Bounded ownership oracles; collector scope is not inferred from capture size."""

from __future__ import annotations

import errno
import hashlib
import json
import pstats
import subprocess
import sys
from pathlib import Path

import pytest

from code_analysis.models import Completeness
from code_analysis.profile_ingestion import ingest_profiles
from code_analysis.profile_models import CallerRepresentation
from code_analysis.runtime_identity import CollectionMetadata

FIXTURE = Path(__file__).parent / "fixtures/runtime_threads/workload.py"
COUNTS = {
    "cancel_leaf": 1,
    "cleanup_leaf": 4,
    "error_leaf": 1,
    "failure_leaf": 1,
    "generator_leaf": 5,
    "main_leaf": 4,
    "noise_leaf": 4,
    "scoped_leaf": 4,
}
PARENTS = {
    "cancel_leaf": "callback",
    "cleanup_leaf": "values",
    "error_leaf": "callback",
    "failure_leaf": "callback",
    "generator_leaf": "values",
    "scoped_leaf": "callback",
}


def run_fixture(mode: str, directory: Path):
    result = subprocess.run(
        (sys.executable, str(FIXTURE), mode, str(directory)),
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    accounting = json.loads(result.stdout)
    assert accounting["counts"] == COUNTS
    assert accounting["child_calls"] == 3
    assert accounting["owners"]["main_leaf"] == ["MainThread"]
    assert accounting["owners"]["noise_leaf"] == ["new-worker"]
    assert all(accounting["owners"][name] == ["existing-worker"] for name in PARENTS)
    return accounting


def scope_violations(stats) -> tuple[str, ...]:
    """Oracle for only fixture-owned markers; never deletes inconvenient frames."""
    failures = []
    for key, values in stats.items():
        if Path(key[0]).name != "workload.py":
            continue
        name = key[2]
        if name in {"main_leaf", "noise_leaf", "child_leaf"}:
            failures.append(f"outside-callback:{name}")
        if name in PARENTS:
            if values[1] != COUNTS[name]:
                failures.append(f"count:{name}")
            for caller in values[4]:
                if caller[2] != PARENTS[name] or caller[0] != key[0]:
                    failures.append(f"parent:{name}")
    observed = {key[2] for key in stats if Path(key[0]).name == "workload.py"}
    failures.extend(f"missing:{name}" for name in PARENTS.keys() - observed)
    return tuple(sorted(set(failures)))


def test_two_unprofiled_runs_have_identical_accounting(tmp_path):
    left = run_fixture("unprofiled", tmp_path)
    right = run_fixture("unprofiled", tmp_path)
    assert {k: v for k, v in left.items() if k != "pid"} == {
        k: v for k, v in right.items() if k != "pid"
    }


def test_serialized_stdlib_callbacks_have_exact_owned_edges(tmp_path):
    run_fixture("scoped-profile", tmp_path)
    path = tmp_path / "scoped.prof"
    stats = pstats.Stats(str(path)).stats
    assert not scope_violations(stats)
    for key, values in stats.items():
        if key[2] in PARENTS:
            assert sum(values[4].values()) == COUNTS[key[2]]
    generator = next(values for key, values in stats.items() if key[2] == "values")
    assert generator[1] == 9  # Five next, three close and one throw; not nine objects.
    metadata = CollectionMetadata.model_validate_json(
        json.dumps(
            {
                "collection": {"collection_scope": "serialized-warm-request-callbacks"},
                "producer_bindings": [
                    {
                        "profile_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "producer": "profile",
                    }
                ],
            }
        )
    )
    report, portable = ingest_profiles((path,), metadata=metadata)
    assert report.runtime.collection.collection_scope == "serialized-warm-request-callbacks"
    assert report.completeness is Completeness.PARTIAL
    assert all(
        edge.caller_representation is CallerRepresentation.COUNT for edge in report.runtime_edges
    )
    assert all(edge.total_seconds is None for edge in report.runtime_edges)
    assert len(report.runtime_edges) == sum(len(value[4]) for value in stats.values())
    restored = tmp_path / "restored.prof"
    restored.write_bytes(next(iter(portable.values())))
    normalized = pstats.Stats(str(restored)).stats

    def counts(value):
        return sorted(
            (key[1:], item[:2], sorted((caller[1:], count) for caller, count in item[4].items()))
            for key, item in value.items()
        )

    # Only synthetic profiler root labels may be sanitized; marker relationships stay exact.
    for name in PARENTS:
        original = {k: v for k, v in stats.items() if k[2] == name}
        copied = {k: v for k, v in normalized.items() if k[2] == name}
        assert counts(original) == counts(copied)


def test_scope_oracle_rejects_false_parent_even_with_correct_counts():
    stats = {
        ("workload.py", 1, name): (
            count,
            count,
            0.0,
            0.0,
            {("workload.py", 2, PARENTS[name]): count},
        )
        for name, count in COUNTS.items()
        if name in PARENTS
    }
    assert not scope_violations(stats)
    stats[("workload.py", 1, "scoped_leaf")] = (4, 4, 0.0, 0.0, {("threading.py", 2, "wait"): 4})
    assert scope_violations(stats) == ("parent:scoped_leaf",)


@pytest.mark.parametrize("mode", ["main-cprofile", "scoped-cprofile"])
def test_cprofile_observation_cannot_prove_scope_from_leaf_counts(tmp_path, mode):
    run_fixture(mode, tmp_path)
    path = tmp_path / ("main.prof" if mode == "main-cprofile" else "scoped.prof")
    stats = pstats.Stats(str(path)).stats
    assert not any(key[2] == "child_leaf" for key in stats)
    # Runtime-specific contamination is recorded, never asserted as desirable behavior.
    (tmp_path / "scope-audit.json").write_text(json.dumps(scope_violations(stats)))
    assert stats


@pytest.mark.parametrize(
    "field,value",
    [
        ("subject_pid", 1),
        ("parent_pid", 1),
        ("start_identity", "wrong"),
        ("run_nonce", "wrong"),
        ("profile_sha256", "0" * 64),
        ("collection_scope", "serialized-warm-request-callbacks"),
        ("serialization_complete", False),
    ],
)
def test_current_managed_scope_and_ownership_rejections(tmp_path, field, value):
    from test_runtime_commands import _spec

    from code_analysis.config import AnalysisConfig
    from code_analysis.runtime_runner import run_runtime
    from code_analysis.runtime_spec import RunnerMode

    script = (
        "import profile,json,os,psutil,hashlib\nfrom pathlib import Path\n"
        "p=profile.Profile(); p.runcall(sum,[1,2]); p.dump_stats('raw.prof')\n"
        "proof=dict(subject_pid=os.getpid(),parent_pid=os.getppid(),"
        "start_identity=str(psutil.Process().create_time()),run_nonce=os.environ['CODE_ANALYSIS_RUN_NONCE'],"
        "collection_scope='subject-interpreter',serialization_complete=True,"
        "profile_sha256=hashlib.sha256(Path('raw.prof').read_bytes()).hexdigest())\n"
        f"proof[{field!r}]={value!r}\n"
        "Path('sidecar.json').write_text(json.dumps(proof))\n"
    )
    spec = _spec(tmp_path, script).model_copy(update={"mode": RunnerMode.APPLICATION_MANAGED})
    result = run_runtime(spec, allow_execution=True, config=AnalysisConfig())
    assert result.status == errno.EPROTO
    assert result.report.completeness is Completeness.FAILED
