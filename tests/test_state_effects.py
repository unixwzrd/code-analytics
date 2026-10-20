from __future__ import annotations

from conftest import FIXTURES

from code_analysis.config import AnalysisConfig
from code_analysis.project import discover_context
from code_analysis.report_models import SideEffectKind, StateResourceKind
from code_analysis.service import scan


def test_state_resources_retain_reads_mutations_aliases_locks_and_cleanup() -> None:
    report = scan(discover_context(FIXTURES / "state_effect_project", AnalysisConfig()))
    resources = {item.logical_name: item for item in report.state_resources}
    state = resources["package.stateful:STATE"]
    assert state.kind is StateResourceKind.MUTABLE_GLOBAL
    assert state.declaration_evidence
    assert state.read_evidence
    assert state.write_evidence
    assert state.alias_evidence
    assert resources["package.stateful:CACHE"].cleanup_evidence
    assert resources["package.stateful:LOCK"].guard_evidence
    assert resources["package.stateful:Manager:registry"].write_evidence
    shadow = resources["package.stateful:SHADOW"]
    assert not shadow.read_evidence
    assert not shadow.write_evidence


def test_side_effects_require_resolved_provenance_and_preserve_atomic_replacement() -> None:
    report = scan(discover_context(FIXTURES / "state_effect_project", AnalysisConfig()))
    kinds = {item.kind for item in report.side_effects}
    assert {
        SideEffectKind.FILESYSTEM_WRITE,
        SideEffectKind.FILESYSTEM_RENAME,
        SideEffectKind.ENVIRONMENT_READ,
        SideEffectKind.ENVIRONMENT_WRITE,
        SideEffectKind.SUBPROCESS,
        SideEffectKind.THREAD_CREATE,
        SideEffectKind.NETWORK_CLIENT,
    } <= kinds
    assert any(
        item.target_identity == "os.replace-atomic-replacement" for item in report.side_effects
    )
    subjects = {item.subject_id: item.logical_name for item in report.subjects}
    assert not any(
        subjects[item.subject_id].endswith(":misleading") for item in report.side_effects
    )
    assert any(item.category == "side-effect-ownership" for item in report.findings)
    perform = next(
        item
        for item in report.function_profiles
        if subjects[item.subject_id] == "package.stateful:perform_effects"
    )
    assert any(value.startswith("filesystem-write:") for value in perform.side_effects)
