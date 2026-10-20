import builtins
import hashlib
import shutil
from pathlib import Path

import pytest
from graphviz_assertions import assert_graphviz_golden
from pydantic import ValidationError

import code_analysis.cohesion_reporting as cohesion_reporting
from code_analysis.cache import ContentCache
from code_analysis.cli import ExitCode, main
from code_analysis.config import (
    AnalysisConfig,
    ArchitectureLayer,
    JavaScriptConfig,
    LanguageSelection,
    PhaseThreeLimits,
    load_config_file,
)
from code_analysis.languages.base import FrontendContext, SourceUnit
from code_analysis.languages.javascript import JavaScriptFrontend
from code_analysis.languages.python import PythonFrontend
from code_analysis.languages.registry import FRONTEND_REGISTRY, frontend_for
from code_analysis.models import (
    AvailabilityState,
    Completeness,
    ModuleSystem,
    ProviderAvailability,
    ProviderState,
    SourceLanguage,
    SourceParseState,
)
from code_analysis.project import discover_context
from code_analysis.projections import ProjectionName, build_projection
from code_analysis.providers.registry import inventory_tools
from code_analysis.queries import build_query
from code_analysis.query_models import QuerySelector, QuerySelectorKind
from code_analysis.report_models import (
    AdapterCapabilityKind,
    AdapterCapabilityState,
    ResponsibilityCluster,
    SectionState,
)
from code_analysis.reports.json_report import load_json, render_json
from code_analysis.reports.markdown import render_markdown
from code_analysis.reports.projection_views import (
    ProjectionFormat,
    render_projection,
    render_projection_binary,
)
from code_analysis.service import scan
from code_analysis.workspace import prepare_workspace

FIXTURE = Path(__file__).parent / "fixtures" / "javascript"
GOLDEN = Path(__file__).parent / "golden"


def _frontend_source(source: str, path: str = "sample.js"):
    data = source.encode()
    config = AnalysisConfig(languages=(LanguageSelection.JAVASCRIPT,))
    return JavaScriptFrontend().analyze(
        SourceUnit(
            logical_path=path,
            module_name=path,
            source=source,
            source_bytes=data,
            source_sha256=hashlib.sha256(data).hexdigest(),
            language=SourceLanguage.JAVASCRIPT,
        ),
        FrontendContext(config, config.identity()),
    )


def test_cohesion_finding_language_comes_from_recorded_module(tmp_path: Path) -> None:
    from code_analysis.browse_index import BrowseIndex
    from code_analysis.browse_index_build import build_browse_index
    from code_analysis.browse_index_models import BrowseRequest
    from code_analysis.ui_models import BrowseFacets
    from code_analysis.ui_rows import BrowseTable
    from code_analysis.web_service import ReportViewService

    (tmp_path / "owner.js").write_text(
        'import "./consumer0.js"; export function owned(value) { return value; }\n'
    )
    (tmp_path / "owner.py").write_text("def owned(value):\n    return value\n")
    for number in range(5):
        (tmp_path / f"consumer{number}.js").write_text('import "./owner.js";\n')
        (tmp_path / f"consumer{number}.py").write_text("import owner\n")
    config = AnalysisConfig(
        test_roots=(),
        architecture_layers=(
            ArchitectureLayer(name="consumer", module_patterns=("consumer0.js",)),
            ArchitectureLayer(name="owner", module_patterns=("owner.js",)),
        ),
    )
    report = scan(discover_context(tmp_path, config))
    modules = {item.name: item for item in report.modules}
    utility = [item for item in report.findings if item.rule_id == "high-coupling-utility-risk"]
    assert {item.language for item in utility} == {SourceLanguage.PYTHON, SourceLanguage.JAVASCRIPT}
    for finding in report.findings:
        assert finding.language == modules[finding.module].language
    assert any(item.rule_id == "configured-layer-direction" for item in report.findings)
    assert report.architecture_contract_suggestions
    for suggestion in report.architecture_contract_suggestions:
        for evidence in suggestion.evidence:
            assert evidence.language is SourceLanguage.JAVASCRIPT
            assert evidence.span.language is evidence.language
    retained = load_json(render_json(report))
    assert retained.findings == report.findings
    assert render_json(scan(discover_context(tmp_path, config))) == render_json(report)
    service = ReportViewService(retained)
    destination = tmp_path / "index"
    manifest = build_browse_index(service, destination)
    index = BrowseIndex.open(destination, report_sha256=manifest.report_sha256)
    for language in (SourceLanguage.PYTHON, SourceLanguage.JAVASCRIPT):
        facets = BrowseFacets(language=language)
        live = service.findings(offset=0, limit=500, facets=facets)
        indexed = index.page(BrowseRequest(table=BrowseTable.FINDINGS, limit=500, facets=facets))
        assert live.records
        assert live.model_dump(mode="json") == indexed.model_dump(mode="json")
        assert all(item.language is language for item in live.records)


def test_language_frontend_registry_is_exhaustive_and_python_adapter_is_typed() -> None:
    assert set(FRONTEND_REGISTRY) == set(SourceLanguage)
    assert isinstance(frontend_for(SourceLanguage.PYTHON), PythonFrontend)
    source = "import os\n\ndef work(value: int) -> int:\n    return value + 1\n"
    data = source.encode()
    config = AnalysisConfig(languages=(LanguageSelection.PYTHON,))
    result = frontend_for(SourceLanguage.PYTHON).analyze(
        SourceUnit(
            logical_path="sample.py",
            module_name="sample",
            source=source,
            source_bytes=data,
            source_sha256=hashlib.sha256(data).hexdigest(),
            language=SourceLanguage.PYTHON,
        ),
        FrontendContext(config, config.identity()),
    )

    assert result.completeness is Completeness.COMPLETE
    assert result.symbols[0].qualified_name == "work"
    assert result.symbols[0].complexity is not None
    assert result.imports[0].specifier == "os"
    assert result.provenance.language is SourceLanguage.PYTHON
    assert tuple(
        item.capability for item in frontend_for(SourceLanguage.PYTHON).capabilities
    ) == tuple(AdapterCapabilityKind)
    assert frontend_for(SourceLanguage.JAVASCRIPT).capabilities[-1].state is (
        AdapterCapabilityState.UNSUPPORTED
    )


def test_invalid_javascript_cohesion_record_preserves_python_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shutil.copytree(FIXTURE / "src", tmp_path / "src")
    (tmp_path / "src" / "python_owner.py").write_text(
        "def python_owner(value: int) -> int:\n    return value + 1\n",
        encoding="utf-8",
    )
    original = ResponsibilityCluster

    def reject_one_evidence_javascript_cluster(**values):
        evidence = values["evidence"]
        if evidence and evidence[0].language is SourceLanguage.JAVASCRIPT:
            values["evidence"] = evidence[:1]
        return original(**values)

    monkeypatch.setattr(
        cohesion_reporting,
        "ResponsibilityCluster",
        reject_one_evidence_javascript_cluster,
    )
    report = scan(discover_context(tmp_path, AnalysisConfig()))

    assert any(item.language is SourceLanguage.PYTHON for item in report.modules)
    javascript = next(
        item for item in report.language_frontends if item.language is SourceLanguage.JAVASCRIPT
    )
    assert javascript.state is SectionState.PARTIAL
    assert javascript.assembly_failures
    assert all(
        item.record_kind == "responsibility-cluster" for item in javascript.assembly_failures
    )
    assert any(item.code == "cohesion-record-invalid" for item in report.diagnostics)
    assert report.completeness is Completeness.PARTIAL


def test_provider_inventory_reports_javascript_runtime_grammar_and_provenance() -> None:
    providers = {item.provider_id: item for item in inventory_tools(portable=False)}

    assert providers["tree-sitter"].version == "0.25.2"
    grammar = providers["tree-sitter-javascript"]
    assert grammar.version == "0.25.0"
    assert grammar.executable is not None
    assert grammar.detail == "grammar ABI 15; language 0.25.0"


def test_javascript_inventory_is_language_aware() -> None:
    config = AnalysisConfig(languages=(LanguageSelection.JAVASCRIPT,))
    report = scan(discover_context(FIXTURE, config))

    assert {item.path for item in report.modules} == {
        "src/main.js",
        "src/shared.js",
        "src/widget.jsx",
    }
    assert all(item.language is SourceLanguage.JAVASCRIPT for item in report.modules)
    assert all(item.parse_state is SourceParseState.COMPLETE for item in report.modules)
    assert {item.module_system for item in report.modules} == {ModuleSystem.ESM}
    assert {item.syntax_mode.value for item in report.modules} == {"javascript", "jsx"}
    assert report.summary.source_files == 3
    assert report.summary.module_exports == 3
    assert report.language_frontends[0].complete == 3
    assert report.refactor_proposals
    modules = {item.name: item for item in report.modules}
    proposal_findings = [item for item in report.findings if item.affected_proposal_ids]
    assert proposal_findings
    assert all(
        item.language == modules[item.module].language == SourceLanguage.JAVASCRIPT
        for item in proposal_findings
    )
    assert all(
        item.state.value != "ready-for-maintainer-review" for item in report.refactor_proposals
    )
    assert next(
        item for item in report.section_coverage if item.section == "cohesion-refactoring"
    ).languages == (SourceLanguage.JAVASCRIPT,)


def test_auto_discovery_builds_one_mixed_language_report(tmp_path: Path) -> None:
    (tmp_path / "alpha.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    (tmp_path / "beta.js").write_text("export function beta() { return 2; }\n", encoding="utf-8")

    mixed = scan(discover_context(tmp_path, AnalysisConfig()))
    python_only = scan(
        discover_context(
            tmp_path,
            AnalysisConfig(languages=(LanguageSelection.PYTHON,)),
        )
    )
    javascript_only = scan(
        discover_context(
            tmp_path,
            AnalysisConfig(languages=(LanguageSelection.JAVASCRIPT,)),
        )
    )

    assert mixed.target.languages == (SourceLanguage.JAVASCRIPT, SourceLanguage.PYTHON)
    assert {item.language for item in mixed.modules} == {
        SourceLanguage.PYTHON,
        SourceLanguage.JAVASCRIPT,
    }
    assert {item.path for item in python_only.modules} == {"alpha.py"}
    assert {item.path for item in javascript_only.modules} == {"beta.js"}
    assert all(item.language is SourceLanguage.PYTHON for item in python_only.source_files)
    python_sections = {
        item.section: item.languages
        for item in mixed.section_coverage
        if item.section in {"contracts", "dictionary-shapes", "structural-validation"}
    }
    assert all(languages == (SourceLanguage.PYTHON,) for languages in python_sections.values())


def test_python_only_scan_does_not_import_optional_javascript_packages(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "only.py").write_text("value = 1\n", encoding="utf-8")
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name in {"tree_sitter", "tree_sitter_javascript"}:
            raise AssertionError("Python-only scan imported the JavaScript parser")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    report = scan(
        discover_context(
            tmp_path,
            AnalysisConfig(languages=(LanguageSelection.PYTHON,)),
        )
    )

    assert report.completeness is Completeness.COMPLETE
    assert report.target.languages == (SourceLanguage.PYTHON,)


def test_javascript_imports_resolve_relative_extensions() -> None:
    config = AnalysisConfig(languages=(LanguageSelection.JAVASCRIPT,))
    report = scan(discover_context(FIXTURE, config))

    assert [(edge.source, edge.target, edge.local) for edge in report.import_edges] == [
        ("main.js", "shared.js", True)
    ]
    modules = {item.name: item for item in report.modules}
    assert (modules["main.js"].fan_in, modules["main.js"].fan_out) == (0, 1)
    assert (modules["shared.js"].fan_in, modules["shared.js"].fan_out) == (1, 0)
    assert (modules["widget.jsx"].fan_in, modules["widget.jsx"].fan_out) == (0, 0)


def test_esm_reexports_and_commonjs_exports_are_typed() -> None:
    esm = _frontend_source(
        "export default function run() {}\n" "export { value as renamed } from './dep.js';\n"
    )
    commonjs = _frontend_source(
        "module.exports = { value: 1 };\nexports.named = 2;\n",
        "legacy.cjs",
    )

    assert [(item.name, item.kind, item.source_specifier) for item in esm.exports] == [
        ("default", "default", None),
        ("renamed", "re-export", "./dep.js"),
    ]
    assert [(item.name, item.kind) for item in commonjs.exports] == [
        ("default", "commonjs"),
        ("named", "commonjs"),
    ]


def test_javascript_near_duplicates_are_explicit_and_language_local() -> None:
    config = AnalysisConfig(languages=(LanguageSelection.JAVASCRIPT,))
    report = scan(discover_context(FIXTURE, config))

    assert len(report.duplicate_functions) == 1
    duplicate = report.duplicate_functions[0]
    assert duplicate.language is SourceLanguage.JAVASCRIPT
    assert duplicate.matched_statements >= 6
    assert duplicate.canonical_owner.state.value == "suggested"
    owner = next(
        item for item in report.subjects if item.subject_id == duplicate.canonical_owner.subject_id
    )
    assert owner.module == "shared.js"
    assert duplicate.behavior_difference_ids
    assert report.behavior_differences[0].kind.value == "constant"
    assert report.function_commonalities
    assert report.summary.function_profiles == 3


def test_shared_api_call_in_different_algorithms_is_not_duplicate_evidence(
    tmp_path: Path,
) -> None:
    (tmp_path / "algorithms.js").write_text(
        "export function iterative(values) {\n"
        "  let total = 0;\n  for (const value of values) { total += value; }\n"
        "  console.log(total);\n  if (total < 0) { throw new Error('negative'); }\n"
        "  total += values.length;\n  return total;\n}\n"
        "export function declarative(values) {\n"
        "  const filtered = values.filter(Boolean);\n"
        "  const mapped = filtered.map(String);\n  console.log(mapped);\n"
        "  try { return JSON.stringify(mapped); } catch (error) { return '[]'; }\n"
        "}\n",
        encoding="utf-8",
    )
    report = scan(
        discover_context(
            tmp_path,
            AnalysisConfig(languages=(LanguageSelection.JAVASCRIPT,)),
        )
    )

    assert report.duplicate_functions == ()


def test_generic_javascript_test_modules_do_not_claim_framework_semantics() -> None:
    config = AnalysisConfig(languages=(LanguageSelection.JAVASCRIPT,))
    report = scan(discover_context(FIXTURE, config))

    assert len(report.test_modules) == 1
    assert report.test_modules[0].framework == "javascript-generic"
    assert report.test_modules[0].language is SourceLanguage.JAVASCRIPT
    assert report.test_cases == ()
    assert any(
        relationship.source_test_module_id == report.test_modules[0].test_module_id
        for relationship in report.test_relationships
    )


def test_javascript_duplicate_query_and_projection_use_saved_json_only() -> None:
    config = AnalysisConfig(languages=(LanguageSelection.JAVASCRIPT,))
    retained = load_json(render_json(scan(discover_context(FIXTURE, config))))
    duplicate = retained.duplicate_functions[0]

    response = build_query(
        retained,
        QuerySelector(kind=QuerySelectorKind.DUPLICATE, identity=duplicate.duplicate_id),
    )
    projection = build_projection(
        retained,
        ProjectionName.DUPLICATE_FUNCTIONS,
        languages=(SourceLanguage.JAVASCRIPT,),
    )

    assert response.items[0].identity == duplicate.duplicate_id
    assert projection.record_count == 1
    assert projection.selection.languages == (SourceLanguage.JAVASCRIPT,)


def test_javascript_report_and_projections_are_authoritative_goldens() -> None:
    config = AnalysisConfig(languages=(LanguageSelection.JAVASCRIPT,))
    scanned = scan(discover_context(FIXTURE, config))
    report_json = render_json(scanned)
    retained = load_json(report_json)

    assert report_json == (GOLDEN / "javascript-report.json").read_text(encoding="utf-8")
    assert render_markdown(scanned) == (GOLDEN / "javascript-report.md").read_text(encoding="utf-8")
    for name, output_format, filename in (
        (
            ProjectionName.DUPLICATE_FUNCTIONS,
            ProjectionFormat.TSV,
            "projection-javascript-duplicate-functions.tsv",
        ),
        (
            ProjectionName.BEHAVIOR_DIFFERENCES,
            ProjectionFormat.MARKDOWN,
            "projection-javascript-behavior-differences.md",
        ),
        (
            ProjectionName.DUPLICATE_OWNERSHIP,
            ProjectionFormat.JSON,
            "projection-javascript-duplicate-ownership.json",
        ),
        (
            ProjectionName.DUPLICATE_CALLERS_TESTS,
            ProjectionFormat.TSV,
            "projection-javascript-duplicate-callers-tests.tsv",
        ),
        (
            ProjectionName.JAVASCRIPT_REFACTOR_PACKET,
            ProjectionFormat.MARKDOWN,
            "projection-javascript-refactor-packet.md",
        ),
        (
            ProjectionName.DEPENDENCIES,
            ProjectionFormat.DOT,
            "projection-javascript-dependencies.dot",
        ),
        (
            ProjectionName.DEPENDENCIES,
            ProjectionFormat.MERMAID,
            "projection-javascript-dependencies.mmd",
        ),
    ):
        projection = build_projection(
            retained,
            name,
            languages=(SourceLanguage.JAVASCRIPT,),
        )
        assert render_projection(projection, output_format) == (GOLDEN / filename).read_text(
            encoding="utf-8"
        )
    for forbidden in ("/Users/", "/Volumes/", "/tmp/", "/private/var/"):
        assert forbidden not in report_json
    if shutil.which("dot") is not None:
        graph = build_projection(
            retained,
            ProjectionName.DEPENDENCIES,
            languages=(SourceLanguage.JAVASCRIPT,),
        )
        assert_graphviz_golden(
            lambda output_format: render_projection_binary(graph, output_format),
            GOLDEN / "projection-javascript-dependencies.dot",
        )


def test_all_javascript_projections_use_only_retained_report_evidence(monkeypatch) -> None:
    retained = load_json((GOLDEN / "javascript-report.json").read_text(encoding="utf-8"))

    def source_access_forbidden(*args, **kwargs):
        raise AssertionError("saved-report projection attempted source access")

    monkeypatch.setattr(Path, "read_bytes", source_access_forbidden)
    monkeypatch.setattr(Path, "read_text", source_access_forbidden)
    for name in (
        ProjectionName.DUPLICATE_FUNCTIONS,
        ProjectionName.CLONE_MEMBERSHIP,
        ProjectionName.BEHAVIOR_DIFFERENCES,
        ProjectionName.DUPLICATE_OWNERSHIP,
        ProjectionName.DUPLICATE_CALLERS_TESTS,
        ProjectionName.DIVERGENCE_REASONS,
        ProjectionName.CANONICAL_OWNER,
        ProjectionName.JAVASCRIPT_REFACTOR_PACKET,
    ):
        projection = build_projection(
            retained,
            name,
            languages=(SourceLanguage.JAVASCRIPT,),
        )
        assert render_projection(projection, ProjectionFormat.JSON).endswith("\n")


def test_javascript_frontend_fails_closed_on_error_nodes() -> None:
    result = _frontend_source("export function broken( {", "broken.js")

    assert result.completeness is Completeness.PARTIAL
    assert {item.code for item in result.diagnostics} == {"javascript-syntax-error"}


def test_javascript_class_fields_and_effects_remain_typed_syntax_evidence() -> None:
    result = _frontend_source(
        "class Client { static cache = new Map(); #value = 1; "
        "get value(){ return this.#value; } "
        "set value(v){ this.#value = v; } "
        "async load(url){ const response = await fetch(url); "
        "localStorage.setItem('last', url); return response; } }"
    )

    assert {item.kind for item in result.symbols} >= {"class", "field", "getter", "setter"}
    assert {item.name for item in result.states} == {"cache", "#value"}
    effects = {value for item in result.functions for value in item.side_effects}
    assert effects >= {"network-client", "storage"}


def test_javascript_async_generator_defaults_and_complexity_are_explicit() -> None:
    result = _frontend_source(
        "export async function* generate(value = 1, ...rest) { "
        "if (value) { yield value; } else { throw new Error('missing'); } }"
    )
    symbol = next(item for item in result.symbols if item.name == "generate")

    assert symbol.async_
    assert symbol.generator
    assert symbol.parameters == ("value", "rest")
    assert symbol.complexity is not None
    assert symbol.complexity.branches == 1
    assert symbol.complexity.raises == 1


def test_javascript_effects_require_lexical_provenance_and_retain_async_events() -> None:
    result = _frontend_source(
        "import fs from 'node:fs';\n"
        "function shadowed(fetch, fs) { fetch('/x'); fs.writeFile('x', 'y'); }\n"
        "async function observed(url) { window.addEventListener('load', observed); "
        "await fetch(url).then(value => value); fs.writeFile('x', 'y'); }\n"
    )
    functions = {item.qualified_name: item for item in result.functions}

    assert functions["shadowed"].side_effects == ()
    assert set(functions["observed"].side_effects) >= {
        "event-listener",
        "filesystem",
        "network-client",
        "promise-chain",
    }
    assert set(functions["observed"].control_events) >= {
        "call:event-registration",
        "call:promise-chain",
        "control:await_expression",
    }


def test_auto_language_selection_cannot_be_mixed() -> None:
    with pytest.raises(ValidationError, match="auto language selection"):
        AnalysisConfig(languages=(LanguageSelection.AUTO, LanguageSelection.JAVASCRIPT))


def test_javascript_toml_configuration_is_strict_and_ordered(tmp_path: Path) -> None:
    config_path = tmp_path / "pyproject.toml"
    config_path.write_text(
        """
[tool.code-analysis]
languages = ["javascript"]
test-roots = ["spec", "tests"]

[tool.code-analysis.javascript]
default-module-mode = "auto"
include-jsx = true

[[tool.code-analysis.javascript.module-mode-rules]]
pattern = "packages/web/static/**"
mode = "esm"
""".strip() + "\n",
        encoding="utf-8",
    )

    config = load_config_file(config_path)

    assert config.languages == (LanguageSelection.JAVASCRIPT,)
    assert config.javascript.module_mode_rules[0].mode is ModuleSystem.ESM


def test_jsx_can_be_explicitly_excluded_without_affecting_javascript(tmp_path: Path) -> None:
    (tmp_path / "main.js").write_text("export const value = 1;\n", encoding="utf-8")
    (tmp_path / "view.jsx").write_text("export const View = () => <main />;\n", encoding="utf-8")
    config = AnalysisConfig(
        languages=(LanguageSelection.JAVASCRIPT,),
        javascript=JavaScriptConfig(include_jsx=False),
    )

    report = scan(discover_context(tmp_path, config))

    assert {item.path for item in report.modules} == {"main.js"}


def test_javascript_module_mode_precedence_and_conflicts_fail_closed(tmp_path: Path) -> None:
    (tmp_path / "module.mjs").write_text("export const value = 1;\n", encoding="utf-8")
    (tmp_path / "legacy.cjs").write_text("module.exports = { value: 1 };\n", encoding="utf-8")
    config = AnalysisConfig(languages=(LanguageSelection.JAVASCRIPT,))

    report = scan(discover_context(tmp_path, config))

    assert {item.path: item.module_system for item in report.modules} == {
        "legacy.cjs": ModuleSystem.COMMONJS,
        "module.mjs": ModuleSystem.ESM,
    }
    (tmp_path / "mixed.js").write_text(
        "import value from './module.mjs';\nmodule.exports = value;\n",
        encoding="utf-8",
    )
    mixed = scan(discover_context(tmp_path, config))
    assert mixed.completeness is Completeness.PARTIAL
    assert next(item for item in mixed.modules if item.path == "mixed.js").module_system is (
        ModuleSystem.MIXED
    )
    assert {item.code for item in mixed.diagnostics} == {"javascript-mixed-module-mode"}


def test_malformed_decisive_package_metadata_is_partial(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"type": ', encoding="utf-8")
    (tmp_path / "main.js").write_text("const value = 1;\n", encoding="utf-8")
    config = AnalysisConfig(languages=(LanguageSelection.JAVASCRIPT,))

    report = scan(discover_context(tmp_path, config))

    assert report.completeness is Completeness.PARTIAL
    assert report.modules[0].module_system is ModuleSystem.UNRESOLVED
    assert {item.code for item in report.diagnostics} == {"javascript-module-metadata-unresolved"}


def test_computed_and_missing_relative_imports_are_unresolved_partial_evidence(
    tmp_path: Path,
) -> None:
    (tmp_path / "main.js").write_text(
        "import missing from './missing.js';\n"
        "export async function load(name) { return import(name); }\n",
        encoding="utf-8",
    )
    config = AnalysisConfig(languages=(LanguageSelection.JAVASCRIPT,))

    report = scan(discover_context(tmp_path, config))

    assert report.completeness is Completeness.PARTIAL
    assert len(report.unresolved_edges) == 2
    assert all(item.effect_on_completeness for item in report.unresolved_edges)
    assert not report.import_edges
    assert {item.code for item in report.diagnostics} == {"javascript-import-unresolved"}


def test_javascript_frontend_cache_is_typed_bound_and_contains_no_source_text(
    tmp_path: Path,
) -> None:
    (tmp_path / "main.js").write_text(
        "export function calculate(value) { return value + 1; }\n",
        encoding="utf-8",
    )
    config = AnalysisConfig(languages=(LanguageSelection.JAVASCRIPT,))
    context = discover_context(tmp_path, config)
    workspace = prepare_workspace(context)
    first_cache = ContentCache(workspace)
    first = scan(context, first_cache)
    assert first_cache.misses == 1

    entries = tuple(workspace.cache.glob("*.json"))
    assert len(entries) == 1
    assert b"export function calculate" not in entries[0].read_bytes()
    second_cache = ContentCache(workspace)
    second = scan(context, second_cache)
    assert second_cache.hits == 1
    assert render_json(second) == render_json(first)

    entries[0].write_text("{}", encoding="utf-8")
    corrupted_cache = ContentCache(workspace)
    recovered = scan(context, corrupted_cache)
    assert corrupted_cache.invalidations == 1
    assert render_json(recovered) == render_json(first)


def test_javascript_candidate_bounds_are_explicit_truncated_evidence(tmp_path: Path) -> None:
    body = "{ let a = 1; let b = 2; let c = 3; a++; b++; return a + b + c; }"
    (tmp_path / "many.js").write_text(
        "\n".join(f"export function f{index}() {body}" for index in range(3)) + "\n",
        encoding="utf-8",
    )
    config = AnalysisConfig(
        languages=(LanguageSelection.JAVASCRIPT,),
        phase_three_limits=PhaseThreeLimits(max_candidate_pairs=1),
    )

    report = scan(discover_context(tmp_path, config))

    assert report.completeness is Completeness.PARTIAL
    assert {item.code for item in report.diagnostics} == {"javascript-analysis-truncated"}
    section = next(
        item for item in report.section_coverage if item.section == "function-commonality"
    )
    assert section.state.value == "truncated"
    assert section.truncated > 0
    admission = report.signal_quality.commonality_admission
    assert admission is not None
    assert admission.candidate_bound_omissions > 0
    assert admission.candidate_bound_omissions_exact is False


def test_javascript_entity_ids_survive_source_changes_but_observations_rebind(
    tmp_path: Path,
) -> None:
    source = tmp_path / "main.js"
    source.write_text("export function value() { return 1; }\n", encoding="utf-8")
    config = AnalysisConfig(languages=(LanguageSelection.JAVASCRIPT,))
    first = scan(discover_context(tmp_path, config))
    source.write_text("export function value() { return 2; }\n", encoding="utf-8")
    second = scan(discover_context(tmp_path, config))

    assert [item.subject_id for item in first.subjects] == [
        item.subject_id for item in second.subjects
    ]
    assert first.source_files[0].source_sha256 != second.source_files[0].source_sha256
    assert first.parser_provenance[0].source_sha256 != second.parser_provenance[0].source_sha256


def test_anonymous_and_duplicate_declarations_receive_deterministic_discriminators() -> None:
    result = _frontend_source(
        "function host(items) { items.map(value => value); "
        "items.map(value => value + 1); }\n"
        "function same() { return 1; }\nfunction same() { return 2; }\n"
    )
    qualified = [item.qualified_name for item in result.symbols]

    assert len(qualified) == len(set(qualified))
    assert "host.<anonymous>" in qualified
    assert "host.<anonymous>#2" in qualified
    assert "same" in qualified
    assert "same#2" in qualified


def test_report_rejects_module_source_language_or_hash_disagreement() -> None:
    config = AnalysisConfig(languages=(LanguageSelection.JAVASCRIPT,))
    report = scan(discover_context(FIXTURE, config))
    source = report.source_files[0]
    with pytest.raises(ValidationError, match="module and source-file identity disagree"):
        type(report).model_validate(
            {
                **report.model_dump(),
                "source_files": (
                    source.model_copy(update={"source_sha256": "0" * 64}),
                    *report.source_files[1:],
                ),
            }
        )


def test_identical_javascript_files_keep_distinct_path_bound_provenance(
    tmp_path: Path,
) -> None:
    source = "export function value() { return 1; }\n"
    (tmp_path / "first.js").write_text(source, encoding="utf-8")
    (tmp_path / "second.js").write_text(source, encoding="utf-8")

    report = scan(
        discover_context(
            tmp_path,
            AnalysisConfig(languages=(LanguageSelection.JAVASCRIPT,)),
        )
    )

    assert [item.logical_path for item in report.parser_provenance] == [
        "first.js",
        "second.js",
    ]
    assert len({item.source_sha256 for item in report.parser_provenance}) == 1


def test_invalid_utf8_is_a_failed_source_not_an_empty_success(tmp_path: Path) -> None:
    (tmp_path / "broken.js").write_bytes(b"export const value = '\xff';")
    config = AnalysisConfig(languages=(LanguageSelection.JAVASCRIPT,))

    report = scan(discover_context(tmp_path, config))

    assert report.completeness is Completeness.PARTIAL
    assert report.modules[0].parse_state is SourceParseState.FAILED
    assert {item.code for item in report.diagnostics} == {"javascript-source-read-error"}


def test_unavailable_javascript_parser_fails_closed(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "main.js").write_text("export const value = 1;\n", encoding="utf-8")
    monkeypatch.setattr(
        JavaScriptFrontend,
        "availability",
        lambda self, context: ProviderAvailability(
            provider_id=self.frontend_id,
            state=AvailabilityState.UNAVAILABLE,
            detail="forced unavailable",
        ),
    )
    config = AnalysisConfig(languages=(LanguageSelection.JAVASCRIPT,))

    report = scan(discover_context(tmp_path, config))

    assert report.completeness is Completeness.PARTIAL
    assert report.providers[-1].state is ProviderState.UNAVAILABLE
    assert report.modules[0].parse_state is SourceParseState.UNAVAILABLE


def test_javascript_cli_empty_target_and_invalid_language_are_errno_failures(
    tmp_path: Path,
) -> None:
    assert (
        main(["scan", str(tmp_path), "--language", "javascript", "--no-workspace", "--quiet"])
        == ExitCode.ENODATA
    )
    assert (
        main(
            [
                "scan",
                str(tmp_path),
                "--language",
                "auto",
                "--language",
                "javascript",
                "--no-workspace",
                "--quiet",
            ]
        )
        == ExitCode.EINVAL
    )
