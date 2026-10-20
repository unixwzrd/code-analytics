"""Public static test-impact evidence integration."""

from __future__ import annotations

import hashlib
import json
import platform

from code_analysis.analysis.test_impact import discover_test_paths, parse_test_module
from code_analysis.context import AnalysisContext
from code_analysis.models import (
    Completeness,
    Confidence,
    Diagnostic,
    DiagnosticLevel,
    Evidence,
    ProviderResult,
    ProviderState,
    SourceSpan,
)
from code_analysis.report_models import (
    AnalysisReport,
    SectionCoverage,
    SectionState,
    SummaryCounts,
    TestCaseRecord,
    TestFixtureRecord,
    TestModuleRecord,
    TestRelationshipKind,
    TestRelationshipRecord,
)

TEST_IMPACT_LIMITATIONS = (
    "Static test references indicate likely impact, not execution or behavioral coverage.",
    "Dynamic patches, generated tests, indirect parametrization, reflection, and framework "
    "discovery may remain unresolved.",
)


def integrate_test_impact(context: AnalysisContext, report: AnalysisReport) -> AnalysisReport:
    paths = discover_test_paths(context)
    if not paths:
        coverage = _replace_coverage(
            report,
            SectionCoverage(section="test-impact", state=SectionState.UNAVAILABLE, required=False),
        )
        provider = ProviderResult(
            provider_id="native-test-impact",
            state=ProviderState.UNAVAILABLE,
            duration_ms=0,
            observations=(),
            diagnostics=(),
            completeness=Completeness.COMPLETE,
        )
        return report.model_copy(
            update={
                "section_coverage": report.section_coverage if report.test_modules else coverage,
                "providers": (*report.providers, provider),
            }
        )

    parsed = []
    diagnostics: list[Diagnostic] = []
    for path in paths:
        try:
            parsed.append(parse_test_module(context, path))
        except (OSError, SyntaxError, UnicodeError) as exc:
            relative = path.relative_to(context.source.physical_root).as_posix()
            diagnostics.append(
                Diagnostic(
                    code="test-source-unavailable",
                    level=DiagnosticLevel.ERROR,
                    message=f"test source could not be analyzed: {type(exc).__name__}",
                    path=relative,
                    provider="native-test-impact",
                )
            )

    module_records = tuple(
        sorted(
            (
                TestModuleRecord(
                    test_module_id=_stable_id("test-module", item.module.path),
                    module=item.module.module,
                    path=item.module.path,
                    source_sha256=item.module.source_sha256,
                    framework=item.module.framework,
                )
                for item in parsed
            ),
            key=lambda item: item.test_module_id,
        )
    )
    module_ids = {item.module: item.test_module_id for item in module_records}
    source_hashes = {item.module.module: item.module.source_sha256 for item in parsed}
    facts = sorted(
        (fact for item in parsed for fact in item.cases), key=lambda item: item.qualified_name
    )
    limits = context.config.phase_four_limits
    remaining_tests = max(0, limits.max_tests - len(report.test_cases))
    truncated = max(0, len(facts) - remaining_tests)
    facts = facts[:remaining_tests]
    test_ids = {fact.qualified_name: _stable_id("test-case", fact.qualified_name) for fact in facts}
    test_records = tuple(
        sorted(
            (
                TestCaseRecord(
                    test_id=test_ids[fact.qualified_name],
                    test_module_id=module_ids[fact.module],
                    qualified_name=fact.qualified_name,
                    parameter_ids=fact.parameter_ids,
                    qualification=fact.qualification,
                    evidence=(
                        _evidence(
                            fact.path,
                            fact.line,
                            fact.end_line,
                            source_hashes[fact.module],
                            "test case declaration",
                        ),
                    ),
                )
                for fact in facts
            ),
            key=lambda item: item.test_id,
        )
    )

    fixture_facts = sorted(
        (fact for item in parsed for fact in item.fixtures), key=lambda item: item.qualified_name
    )
    fixture_ids = {
        fact.qualified_name: _stable_id("test-fixture", fact.qualified_name)
        for fact in fixture_facts
    }
    fixture_names: dict[str, list[str]] = {}
    for fact in fixture_facts:
        fixture_names.setdefault(fact.local_name, []).append(fixture_ids[fact.qualified_name])
    fixture_records = tuple(
        sorted(
            (
                TestFixtureRecord(
                    fixture_id=fixture_ids[fact.qualified_name],
                    test_module_id=module_ids[fact.module],
                    qualified_name=fact.qualified_name,
                    scope=fact.scope,
                    dependency_fixture_ids=tuple(
                        sorted(
                            fixture_names[name][0]
                            for name in fact.dependencies
                            if len(fixture_names.get(name, ())) == 1
                        )
                    ),
                    evidence=(
                        _evidence(
                            fact.path,
                            fact.line,
                            fact.end_line,
                            source_hashes[fact.module],
                            "pytest fixture declaration",
                        ),
                    ),
                )
                for fact in fixture_facts
            ),
            key=lambda item: item.fixture_id,
        )
    )

    subject_by_logical = {item.logical_name: item.subject_id for item in report.subjects}
    known_modules = {item.name for item in report.modules}
    source_ids = {**test_ids, **fixture_ids}
    relationship_records: list[TestRelationshipRecord] = []
    prior_unresolved = sum(not item.resolved for item in report.test_relationships)
    unresolved = 0
    for parsed_module in parsed:
        for fact in parsed_module.relationships:
            source_id = source_ids.get(fact.source_qualified_name)
            if source_id is None:
                continue
            target_subject = None
            target_fixture = None
            resolved = fact.resolved
            limitation = fact.limitation
            if fact.kind is TestRelationshipKind.FIXTURE_USE and fact.target_symbol:
                candidates = fixture_names.get(fact.target_symbol, ())
                if len(candidates) == 1:
                    target_fixture = candidates[0]
                    resolved = True
                    limitation = None
            elif fact.target_module and fact.target_symbol:
                logical = f"{fact.target_module}:{fact.target_symbol}"
                target_subject = subject_by_logical.get(logical)
                if target_subject is None:
                    matches = sorted(
                        subject_id
                        for name, subject_id in subject_by_logical.items()
                        if name == logical or name.endswith(f":{fact.target_symbol}")
                    )
                    target_subject = matches[0] if len(matches) == 1 else None
                resolved = target_subject is not None
                if not resolved:
                    limitation = "Imported target did not resolve to one analyzed symbol."
            elif fact.target_module:
                resolved = fact.target_module in known_modules or any(
                    name.startswith(fact.target_module + ".") for name in known_modules
                )
                if not resolved:
                    limitation = "Imported module is outside the analyzed target or unresolved."
            if not resolved:
                unresolved += 1
                if unresolved + prior_unresolved > limits.max_unresolved_test_references:
                    truncated += 1
                    continue
            relationship_records.append(
                TestRelationshipRecord(
                    relationship_id=_stable_id(
                        "test-relationship",
                        source_id,
                        fact.kind.value,
                        fact.target_module or "",
                        fact.target_symbol or "",
                        fact.target_text or "",
                        str(fact.line),
                        str(fact.column),
                    ),
                    source_test_id=source_id,
                    kind=fact.kind,
                    target_subject_id=target_subject,
                    target_fixture_id=target_fixture,
                    target_module=fact.target_module,
                    target_text=fact.target_text or fact.target_symbol,
                    resolved=resolved,
                    evidence=(
                        _evidence(
                            parsed_module.module.path,
                            fact.line,
                            fact.line,
                            parsed_module.module.source_sha256,
                            fact.observation,
                            Confidence.RESOLVED if resolved else Confidence.INFERRED,
                        ),
                    ),
                    limitations=tuple(sorted({limitation} if limitation else set())),
                )
            )
    relationships = tuple(
        sorted(
            (*report.test_relationships, *relationship_records),
            key=lambda item: item.relationship_id,
        )
    )
    if len(relationships) > limits.max_test_relationships:
        truncated += len(relationships) - limits.max_test_relationships
        relationships = relationships[: limits.max_test_relationships]
    if truncated:
        diagnostics.append(
            Diagnostic(
                code="test-impact-bound-truncated",
                level=DiagnosticLevel.WARNING,
                message=f"test-impact bounds omitted {truncated} records",
                provider="native-test-impact",
            )
        )
    state = (
        SectionState.FAILED
        if paths and not parsed
        else (
            SectionState.TRUNCATED
            if truncated
            else SectionState.PARTIAL if diagnostics or unresolved else SectionState.COMPLETE
        )
    )
    overall = (
        Completeness.FAILED
        if state is SectionState.FAILED or report.completeness is Completeness.FAILED
        else (
            Completeness.PARTIAL
            if state in {SectionState.PARTIAL, SectionState.TRUNCATED}
            or report.completeness is Completeness.PARTIAL
            else Completeness.COMPLETE
        )
    )
    replacement = SectionCoverage(
        section="test-impact",
        state=state,
        parsed=len(parsed),
        failed=len(paths) - len(parsed),
        unresolved=min(
            unresolved, max(0, limits.max_unresolved_test_references - prior_unresolved)
        ),
        truncated=truncated,
        diagnostic_codes=tuple(sorted({item.code for item in diagnostics})),
    )
    if report.test_modules:
        prior = next(item for item in report.section_coverage if item.section == "test-impact")
        replacement = _merge_language_coverage(prior, replacement)
        if replacement.state is not SectionState.COMPLETE:
            overall = (
                Completeness.PARTIAL
                if report.completeness is not Completeness.FAILED
                else Completeness.FAILED
            )
    module_records = tuple(
        sorted((*report.test_modules, *module_records), key=lambda item: item.test_module_id)
    )
    test_records = tuple(sorted((*report.test_cases, *test_records), key=lambda item: item.test_id))
    fixture_records = tuple(
        sorted((*report.test_fixtures, *fixture_records), key=lambda item: item.fixture_id)
    )
    coverage = _replace_coverage(
        report,
        replacement,
    )
    summary = SummaryCounts(
        **{
            **report.summary.model_dump(),
            "test_modules": len(module_records),
            "test_cases": len(test_records),
            "test_fixtures": len(fixture_records),
            "test_relationships": len(relationships),
        }
    )
    provider = ProviderResult(
        provider_id="native-test-impact",
        state=(
            ProviderState.FAILED
            if state is SectionState.FAILED
            else (
                ProviderState.PARTIAL
                if state in {SectionState.PARTIAL, SectionState.TRUNCATED}
                else ProviderState.SUCCESS
            )
        ),
        executable="python",
        version=platform.python_version(),
        duration_ms=0,
        observations=(
            "direct-symbol-references",
            "fixture-dependency-chains",
            "module-import-protection",
            "parametrization",
            "patch-targets",
            "test-inventory",
        ),
        diagnostics=tuple(diagnostics),
        completeness=overall,
    )
    configuration = report.configuration.model_copy(
        update={"digest_sha256": context.config.phase_four_identity()}
    )
    identity_payload = json.dumps(
        {
            "prior": report.run_identity_sha256,
            "configuration": configuration.digest_sha256,
            "modules": [item.model_dump(mode="json") for item in module_records],
            "tests": [item.model_dump(mode="json") for item in test_records],
            "fixtures": [item.model_dump(mode="json") for item in fixture_records],
            "relationships": [item.model_dump(mode="json") for item in relationships],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return report.model_copy(
        update={
            "run_identity_sha256": hashlib.sha256(identity_payload).hexdigest(),
            "configuration": configuration,
            "test_modules": module_records,
            "test_cases": test_records,
            "test_fixtures": fixture_records,
            "test_relationships": relationships,
            "section_coverage": coverage,
            "providers": (*report.providers, provider),
            "diagnostics": (*report.diagnostics, *diagnostics),
            "completeness": overall,
            "limitations": tuple(sorted({*report.limitations, *TEST_IMPACT_LIMITATIONS})),
            "summary": summary,
        }
    )


def _merge_language_coverage(prior: SectionCoverage, current: SectionCoverage) -> SectionCoverage:
    """Keep successful language contributions when another language is incomplete."""
    states = {prior.state, current.state}
    parsed = prior.parsed + current.parsed
    failed = prior.failed + current.failed
    unresolved = prior.unresolved + current.unresolved
    truncated = prior.truncated + current.truncated
    state = (
        SectionState.TRUNCATED
        if truncated or SectionState.TRUNCATED in states
        else (
            SectionState.FAILED
            if failed and not parsed
            else (
                SectionState.PARTIAL
                if failed or unresolved or states != {SectionState.COMPLETE}
                else SectionState.COMPLETE
            )
        )
    )
    return SectionCoverage(
        section="test-impact",
        state=state,
        required=prior.required or current.required,
        parsed=parsed,
        skipped=prior.skipped + current.skipped,
        failed=failed,
        unresolved=unresolved,
        truncated=truncated,
        diagnostic_codes=tuple(sorted({*prior.diagnostic_codes, *current.diagnostic_codes})),
        languages=tuple(
            sorted({*prior.languages, *current.languages}, key=lambda item: item.value)
        ),
    )


def _replace_coverage(
    report: AnalysisReport, replacement: SectionCoverage
) -> tuple[SectionCoverage, ...]:
    return tuple(
        replacement if item.section == replacement.section else item
        for item in report.section_coverage
    )


def _stable_id(prefix: str, *values: str) -> str:
    payload = "\x1f".join(values).encode()
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:20]}"


def _evidence(
    path: str,
    start: int,
    end: int,
    source_sha256: str,
    observation: str,
    confidence: Confidence = Confidence.EXACT,
) -> Evidence:
    return Evidence(
        span=SourceSpan(
            path=path,
            start_line=start,
            start_column=0,
            end_line=end,
            end_column=0,
        ),
        provider="native-test-impact",
        observation=observation,
        confidence=confidence,
        source_sha256=source_sha256,
    )
