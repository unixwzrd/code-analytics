"""Regenerate deterministic report and projection fixtures after schema changes."""

import shutil
from pathlib import Path

from code_analysis.config import (
    AnalysisConfig,
    LanguageSelection,
    load_config_file,
    load_project_config,
)
from code_analysis.models import Completeness, ProviderResult, ProviderState, SourceLanguage
from code_analysis.profile_models import (
    FrameClassification,
    ProfileFrame,
    ProfileReport,
    ProfileStatistic,
    ResourceObservation,
    RuntimeCallEdge,
)
from code_analysis.profiling import (
    render_profile_graph,
    render_profile_json,
    render_profile_markdown,
    render_profile_tsv,
)
from code_analysis.project import discover_context
from code_analysis.projection_models import (
    ProjectionFindingCategory,
    ProjectionFindingSeverity,
)
from code_analysis.projections import ProjectionName, build_projection
from code_analysis.reports.json_report import load_json, render_json
from code_analysis.reports.markdown import render_markdown
from code_analysis.reports.projection_views import (
    ProjectionFormat,
    render_projection,
    render_projection_binary,
)
from code_analysis.service import scan, scan_contract_inventory, scan_refactor

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
GOLDEN = ROOT / "tests" / "golden"


def write(name: str, value: str) -> None:
    (GOLDEN / name).write_text(value, encoding="utf-8", newline="\n")


def write_bytes(name: str, value: bytes) -> None:
    (GOLDEN / name).write_bytes(value)


def projection(
    report, name: ProjectionName, output: ProjectionFormat, filename: str, *, limit=None
):
    built = (
        build_projection(report, name)
        if limit is None
        else build_projection(report, name, max_records=limit)
    )
    write(filename, render_projection(built, output))


def main() -> None:
    minimal = scan(discover_context(FIXTURES / "excluded", AnalysisConfig()))
    write("minimal-report.json", render_json(minimal))
    write("minimal-report.md", render_markdown(minimal))
    for name, output, filename in (
        (ProjectionName.MODULES, ProjectionFormat.JSON, "projection-modules.json"),
        (ProjectionName.MODULES, ProjectionFormat.TSV, "projection-modules.tsv"),
        (ProjectionName.HEALTH, ProjectionFormat.MARKDOWN, "projection-health.md"),
        (ProjectionName.SYMBOLS, ProjectionFormat.TSV, "projection-symbols.tsv"),
        (ProjectionName.FINDINGS, ProjectionFormat.TSV, "projection-findings.tsv"),
        (ProjectionName.HIERARCHY, ProjectionFormat.MERMAID, "projection-hierarchy.mmd"),
        (ProjectionName.DEPENDENCIES, ProjectionFormat.DOT, "projection-dependencies.dot"),
        (
            ProjectionName.DEPENDENCIES,
            ProjectionFormat.MERMAID,
            "projection-dependencies.mmd",
        ),
    ):
        projection(minimal, name, output, filename)

    ordinary = scan(discover_context(FIXTURES / "ordinary", AnalysisConfig()))
    neighborhood = build_projection(
        ordinary,
        ProjectionName.DEPENDENCY_NEIGHBORHOOD,
        selected_ids=("alpha.a",),
    )
    for output, filename in (
        (ProjectionFormat.DOT, "projection-dependency-neighborhood.dot"),
        (ProjectionFormat.MERMAID, "projection-dependency-neighborhood.mmd"),
    ):
        write(filename, render_projection(neighborhood, output))

    contract = scan_contract_inventory(
        discover_context(FIXTURES / "contract_golden", AnalysisConfig())
    )
    write("contract-report.json", render_json(contract))
    write("contract-report.md", render_markdown(contract))

    contracts_project = FIXTURES / "contracts_project"
    contracts = scan_contract_inventory(
        discover_context(contracts_project, load_project_config(contracts_project))
    )
    projection(
        contracts,
        ProjectionName.CONTRACT_BOUNDARIES,
        ProjectionFormat.MARKDOWN,
        "projection-contract-boundaries.md",
        limit=5,
    )
    projection(
        contracts,
        ProjectionName.CONTRACTS,
        ProjectionFormat.MARKDOWN,
        "projection-contracts.md",
    )

    dictionary = scan(discover_context(FIXTURES / "dictionary_project", AnalysisConfig()))
    for name, filename in (
        (ProjectionName.SHAPE_VARIANTS, "projection-shape-variants.md"),
        (ProjectionName.UNRESOLVED_COVERAGE, "projection-unresolved-coverage.md"),
        (ProjectionName.REPRESENTATION_CANDIDATES, "projection-representation-candidates.md"),
    ):
        projection(dictionary, name, ProjectionFormat.MARKDOWN, filename)
    projection(
        dictionary,
        ProjectionName.UNRESOLVED_COVERAGE,
        ProjectionFormat.TSV,
        "projection-unresolved-coverage.tsv",
    )

    validation = scan(discover_context(FIXTURES / "validation_project", AnalysisConfig()))
    projection(
        validation,
        ProjectionName.VALIDATION_DIVERGENCE,
        ProjectionFormat.MARKDOWN,
        "projection-validation-divergence.md",
    )

    test_impact = scan(discover_context(FIXTURES / "test_impact_project", AnalysisConfig()))
    for name, output, filename in (
        (ProjectionName.TEST_IMPACT, ProjectionFormat.JSON, "phase4-test-impact.json"),
        (ProjectionName.TEST_IMPACT, ProjectionFormat.MARKDOWN, "phase4-test-impact.md"),
        (ProjectionName.TEST_IMPACT, ProjectionFormat.TSV, "phase4-test-impact.tsv"),
        (ProjectionName.TEST_IMPACT_GRAPH, ProjectionFormat.DOT, "phase4-test-impact.dot"),
        (
            ProjectionName.TEST_IMPACT_GRAPH,
            ProjectionFormat.MERMAID,
            "phase4-test-impact.mmd",
        ),
    ):
        projection(test_impact, name, output, filename)
    if shutil.which("dot") is not None:
        test_graph = build_projection(test_impact, ProjectionName.TEST_IMPACT_GRAPH)
        write_bytes(
            "phase4-test-impact.svg",
            render_projection_binary(test_graph, ProjectionFormat.SVG),
        )
        write_bytes(
            "phase4-test-impact.png",
            render_projection_binary(test_graph, ProjectionFormat.PNG),
        )
    # This is a retained module.py capture, not the test-impact-project fixture.
    # Re-render its saved evidence; do not replace source/workload identity or
    # invent a new capture while regenerating unrelated static report goldens.
    covered = load_json((GOLDEN / "phase4-coverage-report.json").read_text(encoding="utf-8"))
    write("phase4-coverage-report.json", render_json(covered))
    write("phase4-coverage-report.md", render_markdown(covered))
    for name, output, filename in (
        (ProjectionName.COVERAGE_FILES, ProjectionFormat.TSV, "phase4-coverage-files.tsv"),
        (ProjectionName.COVERAGE_FILES, ProjectionFormat.MARKDOWN, "phase4-coverage-files.md"),
        (ProjectionName.COVERAGE_MATRIX, ProjectionFormat.TSV, "phase4-coverage-matrix.tsv"),
    ):
        projection(covered, name, output, filename)

    structures = scan(discover_context(FIXTURES / "structure_project", AnalysisConfig()))
    for name, filename in (
        (ProjectionName.STRUCTURE_LIFECYCLE, "projection-structure-lifecycle.md"),
        (ProjectionName.CONVERSION_CHURN, "projection-conversion-churn.md"),
    ):
        projection(structures, name, ProjectionFormat.MARKDOWN, filename)

    config = load_config_file(FIXTURES / "proposal-config.toml")
    phase_three = scan_refactor(discover_context(FIXTURES / "proposal_project", config))
    write("phase3-report.json", render_json(phase_three))
    scan_report = scan(discover_context(FIXTURES / "proposal_project", config))
    write("phase3-report.md", render_markdown(scan_report))
    projection(
        phase_three,
        ProjectionName.REFACTOR_PACKET,
        ProjectionFormat.MARKDOWN,
        "phase3-refactor-packet.md",
    )
    projection(
        phase_three,
        ProjectionName.FUNCTION_COMMONALITY,
        ProjectionFormat.TSV,
        "projection-function-commonality.tsv",
    )
    for output, filename in (
        (ProjectionFormat.JSON, "projection-proposal-admission.json"),
        (ProjectionFormat.TSV, "projection-proposal-admission.tsv"),
        (ProjectionFormat.MARKDOWN, "projection-proposal-admission.md"),
    ):
        projection(phase_three, ProjectionName.PROPOSAL_ADMISSION, output, filename)
    for output, filename in (
        (ProjectionFormat.JSON, "projection-subject-findings.json"),
        (ProjectionFormat.TSV, "projection-subject-findings.tsv"),
        (ProjectionFormat.MARKDOWN, "projection-subject-findings.md"),
    ):
        projection(phase_three, ProjectionName.SUBJECT_FINDINGS, output, filename)
    filtered_subjects = build_projection(
        phase_three,
        ProjectionName.SUBJECT_FINDINGS,
        module_prefixes=("package.combine_a",),
        finding_categories=(ProjectionFindingCategory.DUPLICATION,),
        finding_severities=(ProjectionFindingSeverity.WARNING,),
        finding_rule_ids=("duplication.normalized-ast",),
    )
    for output, filename in (
        (ProjectionFormat.JSON, "projection-subject-findings-filtered.json"),
        (ProjectionFormat.TSV, "projection-subject-findings-filtered.tsv"),
        (ProjectionFormat.MARKDOWN, "projection-subject-findings-filtered.md"),
    ):
        write(filename, render_projection(filtered_subjects, output))
    projection(
        phase_three,
        ProjectionName.UNCOVERED_PROPOSAL_SUBJECTS,
        ProjectionFormat.TSV,
        "phase4-uncovered-proposal-subjects.tsv",
    )
    projection(
        phase_three,
        ProjectionName.UNCOVERED_PROPOSAL_SUBJECTS,
        ProjectionFormat.MARKDOWN,
        "phase4-uncovered-proposal-subjects.md",
    )

    profile = _profile_golden()
    write("phase4-profile-report.json", render_profile_json(profile))
    write("phase4-profile-report.md", render_profile_markdown(profile))
    write("phase4-profile-hotspots.tsv", render_profile_tsv(profile))
    write("phase4-profile-call-graph.dot", render_profile_graph(profile, mermaid=False))
    write("phase4-profile-call-graph.mmd", render_profile_graph(profile, mermaid=True))

    javascript = scan(
        discover_context(
            FIXTURES / "javascript",
            AnalysisConfig(languages=(LanguageSelection.JAVASCRIPT,)),
        )
    )
    write("javascript-report.json", render_json(javascript))
    write("javascript-report.md", render_markdown(javascript))
    for name, output, filename in (
        (ProjectionName.DUPLICATE_FUNCTIONS, ProjectionFormat.TSV, "duplicate-functions.tsv"),
        (ProjectionName.BEHAVIOR_DIFFERENCES, ProjectionFormat.MARKDOWN, "behavior-differences.md"),
        (ProjectionName.DUPLICATE_OWNERSHIP, ProjectionFormat.JSON, "duplicate-ownership.json"),
        (
            ProjectionName.DUPLICATE_CALLERS_TESTS,
            ProjectionFormat.TSV,
            "duplicate-callers-tests.tsv",
        ),
        (
            ProjectionName.JAVASCRIPT_REFACTOR_PACKET,
            ProjectionFormat.MARKDOWN,
            "refactor-packet.md",
        ),
        (ProjectionName.DEPENDENCIES, ProjectionFormat.DOT, "dependencies.dot"),
        (ProjectionName.DEPENDENCIES, ProjectionFormat.MERMAID, "dependencies.mmd"),
    ):
        built = build_projection(javascript, name, languages=(SourceLanguage.JAVASCRIPT,))
        write(f"projection-javascript-{filename}", render_projection(built, output))
    if shutil.which("dot") is not None:
        graph = build_projection(
            javascript,
            ProjectionName.DEPENDENCIES,
            languages=(SourceLanguage.JAVASCRIPT,),
        )
        for output, filename in (
            (ProjectionFormat.SVG, "projection-javascript-dependencies.svg"),
            (ProjectionFormat.PNG, "projection-javascript-dependencies.png"),
        ):
            write_bytes(filename, render_projection_binary(graph, output))


def _profile_golden() -> ProfileReport:
    frames = (
        ProfileFrame(
            frame_id="profile-frame-11111111111111111111",
            path="package/module.py",
            line=10,
            function="caller",
            classification=FrameClassification.PROJECT,
            subject_id=None,
            primitive_calls=2,
            total_calls=2,
            total_seconds=0.125,
            cumulative_seconds=0.375,
            per_call_seconds=0.0625,
        ),
        ProfileFrame(
            frame_id="profile-frame-22222222222222222222",
            path="package/module.py",
            line=20,
            function="callee",
            classification=FrameClassification.PROJECT,
            subject_id=None,
            primitive_calls=4,
            total_calls=4,
            total_seconds=0.25,
            cumulative_seconds=0.25,
            per_call_seconds=0.0625,
        ),
    )
    return ProfileReport(
        tool_version="0.1.0",
        profile_identity_sha256="1" * 64,
        source_report_sha256="2" * 64,
        workload_identity_sha256="3" * 64,
        repetitions=1,
        expected_status="SUCCESS",
        observed_statuses=(0,),
        frames=frames,
        runtime_edges=(
            RuntimeCallEdge(
                runtime_edge_id="runtime-edge-11111111111111111111",
                caller_frame_id=frames[0].frame_id,
                callee_frame_id=frames[1].frame_id,
                primitive_calls=4,
                total_calls=4,
                total_seconds=0.25,
                cumulative_seconds=0.25,
            ),
        ),
        statistics=tuple(
            ProfileStatistic(
                frame_id=frame.frame_id,
                call_count_mean=float(frame.total_calls),
                total_seconds_mean=frame.total_seconds,
                total_seconds_median=frame.total_seconds,
                total_seconds_minimum=frame.total_seconds,
                total_seconds_maximum=frame.total_seconds,
                total_seconds_standard_deviation=0.0,
                total_seconds_coefficient_of_variation=0.0,
            )
            for frame in frames
        ),
        resources=(
            ResourceObservation(
                repetition=1,
                wall_seconds=0.5,
                user_cpu_seconds=0.4,
                system_cpu_seconds=0.05,
                peak_rss_bytes=1048576,
                maximum_owned_processes=1,
            ),
        ),
        providers=(
            ProviderResult(
                provider_id="stdlib-cprofile",
                state=ProviderState.SUCCESS,
                executable="python-current-interpreter",
                version="3.12.13",
                duration_ms=500,
                observations=("callers", "callees", "cumulative-time", "total-time"),
                completeness=Completeness.COMPLETE,
            ),
        ),
        diagnostics=(),
        completeness=Completeness.COMPLETE,
        limitations=(
            "Runtime evidence describes only the supplied workload and is not exhaustive.",
        ),
    )


if __name__ == "__main__":
    main()
