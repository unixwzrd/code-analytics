from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from pathlib import Path
from typing import Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    model_validator,
)

from code_analysis.errors import ConfigurationError
from code_analysis.finding_suppression_models import (
    AcceptedFindingSuppression,
    validate_canonical_date_string,
)
from code_analysis.models import ModuleSystem
from code_analysis.report_models import BoundaryClass, GitPathScope
from code_analysis.runtime_spec import RuntimeAnalysisConfig


class Strictness(StrEnum):
    LENIENT = "lenient"
    NORMAL = "normal"
    STRICT = "strict"


class LanguageSelection(StrEnum):
    AUTO = "auto"
    PYTHON = "python"
    JAVASCRIPT = "javascript"
    RUBY = "ruby"


class JavaScriptModuleMode(StrEnum):
    AUTO = "auto"
    ESM = "esm"
    COMMONJS = "commonjs"
    SCRIPT = "script"


class JavaScriptModuleModeRule(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    pattern: str = Field(min_length=1)
    mode: ModuleSystem

    @model_validator(mode="after")
    def validate_mode(self) -> Self:
        if self.mode in {ModuleSystem.MIXED, ModuleSystem.UNRESOLVED}:
            raise ValueError("configured JavaScript module mode must be concrete")
        return self


class JavaScriptConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    default_module_mode: JavaScriptModuleMode = JavaScriptModuleMode.AUTO
    include_jsx: StrictBool = True
    module_mode_rules: tuple[JavaScriptModuleModeRule, ...] = ()

    @model_validator(mode="after")
    def validate_rules(self) -> Self:
        identities = [(item.pattern, item.mode.value) for item in self.module_mode_rules]
        if identities != sorted(identities) or len(identities) != len(set(identities)):
            raise ValueError("JavaScript module-mode rules must be unique and sorted")
        return self


class Thresholds(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    function_loc: StrictInt = Field(75, ge=1)
    branches: StrictInt = Field(12, ge=0)
    nesting: StrictInt = Field(4, ge=0)
    arguments: StrictInt = Field(7, ge=0)
    weighted_score: StrictInt = Field(15, ge=0)


STRICTNESS_PRESETS: dict[Strictness, Thresholds] = {
    Strictness.LENIENT: Thresholds(
        function_loc=120, branches=18, nesting=6, arguments=10, weighted_score=25
    ),
    Strictness.NORMAL: Thresholds(),
    Strictness.STRICT: Thresholds(
        function_loc=50, branches=8, nesting=3, arguments=5, weighted_score=10
    ),
}


class CloneThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    exact_min_lines: StrictInt = Field(6, ge=1)
    token_min_tokens: StrictInt = Field(48, ge=1)
    ast_min_nodes: StrictInt = Field(24, ge=1)
    statement_window: StrictInt = Field(4, ge=2)
    sequence_min_events: StrictInt = Field(5, ge=2)
    sequence_min_coverage: StrictFloat = Field(0.70, ge=0.0, le=1.0)
    commonality_min_ownership_dimensions: StrictInt = Field(2, ge=1, le=6)
    commonality_min_bilateral_coverage: StrictFloat = Field(0.25, gt=0.0, le=1.0)
    commonality_max_feature_subject_fraction: StrictFloat = Field(0.10, gt=0.0, le=1.0)
    near_clone_min_statements: StrictInt = Field(6, ge=2)
    near_clone_min_bilateral_coverage: StrictFloat = Field(0.70, gt=0.0, le=1.0)
    near_clone_max_changed_statements: StrictInt = Field(3, ge=0)
    max_clone_window_statements: StrictInt = Field(64, ge=4, le=10_000)


CLONE_THRESHOLD_PRESETS: dict[Strictness, CloneThresholds] = {
    Strictness.LENIENT: CloneThresholds(
        exact_min_lines=10,
        token_min_tokens=80,
        ast_min_nodes=40,
        statement_window=6,
        sequence_min_events=8,
        sequence_min_coverage=0.85,
        commonality_min_ownership_dimensions=2,
        commonality_min_bilateral_coverage=0.10,
        commonality_max_feature_subject_fraction=0.20,
        near_clone_min_statements=8,
        near_clone_min_bilateral_coverage=0.85,
        near_clone_max_changed_statements=2,
        max_clone_window_statements=96,
    ),
    Strictness.NORMAL: CloneThresholds(),
    Strictness.STRICT: CloneThresholds(
        exact_min_lines=4,
        token_min_tokens=32,
        ast_min_nodes=16,
        statement_window=3,
        sequence_min_events=4,
        sequence_min_coverage=0.60,
        commonality_min_ownership_dimensions=2,
        commonality_min_bilateral_coverage=0.50,
        commonality_max_feature_subject_fraction=0.05,
        near_clone_min_statements=4,
        near_clone_min_bilateral_coverage=0.60,
        near_clone_max_changed_statements=4,
        max_clone_window_statements=48,
    ),
}


class PhaseThreeLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    max_candidate_pairs: StrictInt = Field(50_000, ge=1)
    max_clusters: StrictInt = Field(10_000, ge=1)
    max_cluster_members: StrictInt = Field(100, ge=2)
    max_proposals: StrictInt = Field(100, ge=1)
    min_responsibility_ownership_dimensions: StrictInt = Field(2, ge=1)
    min_responsibility_clone_nonblank_lines: StrictInt = Field(12, ge=1)


class PhaseFourLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    max_tests: StrictInt = Field(50_000, ge=1)
    max_test_relationships: StrictInt = Field(250_000, ge=1)
    max_unresolved_test_references: StrictInt = Field(50_000, ge=1)
    max_git_commits: StrictInt = Field(500, ge=1)
    max_git_coupling_pairs: StrictInt = Field(50_000, ge=1)
    min_git_shared_changes: StrictInt = Field(3, ge=1)
    max_coverage_contexts_files: StrictInt = Field(100_000, ge=1)
    max_coverage_arcs: StrictInt = Field(1_000_000, ge=1)
    max_profile_frames: StrictInt = Field(100_000, ge=1)
    max_runtime_edges: StrictInt = Field(250_000, ge=1)
    max_repetitions: StrictInt = Field(20, ge=1, le=20)


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    timeout_seconds: StrictFloat = Field(300.0, gt=0.0)
    environment_allowlist: tuple[str, ...] = (
        "CI",
        "COLUMNS",
        "LANG",
        "LC_ALL",
        "NO_COLOR",
        "PATH",
        "PYTHONHASHSEED",
        "PYTHONPATH",
        "TERM",
        "TZ",
    )

    @model_validator(mode="after")
    def validate_allowlist(self) -> Self:
        if list(self.environment_allowlist) != sorted(self.environment_allowlist) or len(
            self.environment_allowlist
        ) != len(set(self.environment_allowlist)):
            raise ValueError("runtime environment allowlist must be unique and sorted")
        return self


class SemanticConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    enabled: StrictBool = False
    model_directory: str | None = None
    max_nominations: StrictInt = Field(1_000, ge=1, le=50_000)

    @model_validator(mode="after")
    def validate_local_model(self) -> Self:
        if not self.enabled:
            if self.model_directory is not None:
                raise ValueError("disabled semantic nomination cannot define a model directory")
            return self
        if self.model_directory is None:
            raise ValueError("enabled semantic nomination requires a local model directory")
        path = Path(self.model_directory)
        if not path.is_absolute() or not path.is_dir():
            raise ValueError("semantic model directory must be an existing absolute directory")
        return self


class ArchitectureLayer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    name: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9-]*$")
    module_patterns: tuple[str, ...] = Field(min_length=1)
    may_import_layers: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        for label, values in (
            ("module patterns", self.module_patterns),
            ("allowed layers", self.may_import_layers),
        ):
            if list(values) != sorted(values) or len(values) != len(set(values)):
                raise ValueError(f"architecture layer {label} must be unique and sorted")
        return self


class WorkspaceConfig(BaseModel):
    """Host-local storage settings excluded from portable report identity."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    enabled: StrictBool = True
    path: str | None = None
    cache_enabled: StrictBool = True
    cache_max_bytes: StrictInt = Field(default=512 * 1024 * 1024, ge=0)
    keep_older_generations: StrictInt = Field(default=5, ge=0)
    unpinned_generation_budget_bytes: StrictInt = Field(default=4 * 1024**3, ge=0)

    @model_validator(mode="after")
    def validate_mode(self) -> Self:
        if not self.enabled and self.path is not None:
            raise ValueError("disabled workspace cannot define a path")
        return self

    def identity(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(payload).hexdigest()


class ContractBoundaryRule(BaseModel):
    """Portable project-owned classification for an evidenced contract or dictionary use."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    contract_pattern: str = Field(min_length=1)
    site_pattern: str = Field(min_length=1)
    boundary_class: BoundaryClass

    @model_validator(mode="after")
    def validate_classification(self) -> Self:
        if self.boundary_class in {
            BoundaryClass.MODULE_LOCAL,
            BoundaryClass.TEST_QUALIFICATION_EVIDENCE,
            BoundaryClass.UNCLASSIFIED_INTERNAL,
        }:
            raise ValueError("contract boundary rules require a decisive nonlocal boundary class")
        return self

    def identity(self) -> tuple[str, str, str]:
        return (self.contract_pattern, self.site_pattern, self.boundary_class.value)


class AnalysisConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    strictness: Strictness = Strictness.NORMAL
    thresholds: Thresholds = Thresholds()
    only_local_imports: StrictBool = True
    portable: StrictBool = True
    source_roots: tuple[str, ...] = ()
    languages: tuple[LanguageSelection, ...] = (LanguageSelection.AUTO,)
    test_roots: tuple[str, ...] = ("test", "tests")
    javascript: JavaScriptConfig = JavaScriptConfig()
    excludes: tuple[str, ...] = (
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "htmlcov",
        "node_modules",
        "venv",
    )
    max_findings: StrictInt | None = Field(default=None, ge=0)
    max_static_call_occurrences: StrictInt = Field(default=100_000, ge=1, le=1_000_000)
    symbol_filter: str | None = None
    contract_boundaries: tuple[ContractBoundaryRule, ...] = ()
    clone_thresholds: CloneThresholds = CloneThresholds()
    phase_three_limits: PhaseThreeLimits = PhaseThreeLimits()
    phase_four_limits: PhaseFourLimits = PhaseFourLimits()
    git_path_scope: GitPathScope = GitPathScope.PYTHON
    runtime: RuntimeConfig = RuntimeConfig()
    runtime_analysis: RuntimeAnalysisConfig = RuntimeAnalysisConfig()
    semantic: SemanticConfig = SemanticConfig()
    architecture_layers: tuple[ArchitectureLayer, ...] = ()
    workspace: WorkspaceConfig = WorkspaceConfig()
    accepted_suppressions: tuple[AcceptedFindingSuppression, ...] = Field(
        default=(), max_length=10_000
    )
    suppression_evaluation_date: str | None = None

    @model_validator(mode="after")
    def validate_suppressions(self) -> Self:
        if self.accepted_suppressions and self.suppression_evaluation_date is None:
            raise ValueError(
                "suppression-evaluation-date is required when accepted suppressions are configured"
            )
        if self.suppression_evaluation_date is not None and not self.accepted_suppressions:
            raise ValueError(
                "suppression-evaluation-date cannot be set without accepted suppressions"
            )
        if self.suppression_evaluation_date is not None:
            validate_canonical_date_string(
                self.suppression_evaluation_date, label="suppression_evaluation_date"
            )
        finding_ids = [item.finding_id for item in self.accepted_suppressions]
        if finding_ids != sorted(finding_ids) or len(finding_ids) != len(set(finding_ids)):
            raise ValueError("accepted suppressions must be unique and sorted by finding_id")
        return self

    @model_validator(mode="after")
    def validate_portability(self) -> Self:
        if not self.portable:
            raise ValueError("Phase 1 reports require portable=true")
        language_values = tuple(item.value for item in self.languages)
        if not language_values or len(language_values) != len(set(language_values)):
            raise ValueError("languages must be nonempty and unique")
        if LanguageSelection.AUTO in self.languages and len(self.languages) != 1:
            raise ValueError("auto language selection cannot be combined with explicit languages")
        if tuple(self.test_roots) != tuple(sorted(set(self.test_roots))):
            raise ValueError("test roots must be unique and sorted")
        identities = [rule.identity() for rule in self.contract_boundaries]
        if identities != sorted(identities) or len(identities) != len(set(identities)):
            raise ValueError("contract boundary rules must be unique and sorted")
        layer_names = [layer.name for layer in self.architecture_layers]
        if layer_names != sorted(layer_names) or len(layer_names) != len(set(layer_names)):
            raise ValueError("architecture layers must be unique and sorted")
        known_layers = set(layer_names)
        unknown_layers = sorted(
            {
                allowed
                for layer in self.architecture_layers
                for allowed in layer.may_import_layers
                if allowed not in known_layers
            }
        )
        if unknown_layers:
            raise ValueError(
                "architecture layers reference unknown allowed layers: " + ", ".join(unknown_layers)
            )
        return self

    @classmethod
    def with_overrides(
        cls,
        *,
        strictness: Strictness | None = None,
        function_loc: int | None = None,
        branches: int | None = None,
        nesting: int | None = None,
        arguments: int | None = None,
        weighted_score: int | None = None,
        only_local_imports: bool = True,
        max_findings: int | None = None,
        symbol_filter: str | None = None,
        languages: tuple[LanguageSelection, ...] | None = None,
        javascript_module_mode: JavaScriptModuleMode | None = None,
        base: AnalysisConfig | None = None,
    ) -> AnalysisConfig:
        base = base or cls()
        selected = strictness or base.strictness
        preset = base.thresholds if strictness is None else STRICTNESS_PRESETS[selected]
        clone_preset = (
            base.clone_thresholds if strictness is None else CLONE_THRESHOLD_PRESETS[selected]
        )
        values = preset.model_dump()
        overrides = {
            "function_loc": function_loc,
            "branches": branches,
            "nesting": nesting,
            "arguments": arguments,
            "weighted_score": weighted_score,
        }
        values.update({key: value for key, value in overrides.items() if value is not None})
        return cls(
            strictness=selected,
            thresholds=Thresholds.model_validate(values),
            only_local_imports=only_local_imports,
            portable=base.portable,
            source_roots=base.source_roots,
            languages=languages if languages is not None else base.languages,
            test_roots=base.test_roots,
            javascript=(
                base.javascript.model_copy(update={"default_module_mode": javascript_module_mode})
                if javascript_module_mode is not None
                else base.javascript
            ),
            excludes=base.excludes,
            max_findings=max_findings if max_findings is not None else base.max_findings,
            max_static_call_occurrences=base.max_static_call_occurrences,
            symbol_filter=symbol_filter if symbol_filter is not None else base.symbol_filter,
            contract_boundaries=base.contract_boundaries,
            clone_thresholds=clone_preset,
            phase_three_limits=base.phase_three_limits,
            phase_four_limits=base.phase_four_limits,
            git_path_scope=base.git_path_scope,
            runtime=base.runtime,
            runtime_analysis=base.runtime_analysis,
            semantic=base.semantic,
            architecture_layers=base.architecture_layers,
            workspace=base.workspace,
            accepted_suppressions=base.accepted_suppressions,
            suppression_evaluation_date=base.suppression_evaluation_date,
        )

    def identity(self) -> str:
        payload = json.dumps(
            self.model_dump(
                mode="json",
                exclude={
                    "workspace",
                    "semantic",
                    "runtime_analysis",
                    "contract_boundaries",
                    "clone_thresholds",
                    "phase_three_limits",
                    "git_path_scope",
                    "architecture_layers",
                },
            ),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(payload).hexdigest()

    def phase_two_identity(self) -> str:
        payload = json.dumps(
            self.model_dump(
                mode="json",
                exclude={
                    "workspace",
                    "semantic",
                    "runtime_analysis",
                    "clone_thresholds",
                    "phase_three_limits",
                    "git_path_scope",
                    "architecture_layers",
                },
            ),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(payload).hexdigest()

    def phase_three_identity(self) -> str:
        payload = json.dumps(
            self.model_dump(
                mode="json", exclude={"workspace", "semantic", "git_path_scope", "runtime_analysis"}
            ),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(payload).hexdigest()

    def phase_four_identity(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json", exclude={"workspace", "semantic", "runtime_analysis"}),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(payload).hexdigest()


def load_project_config(path: Path) -> AnalysisConfig:
    """Load [tool.code-analysis] when present; CLI overrides are applied separately."""
    return load_config_file(path / "pyproject.toml")


def load_config_file(pyproject: Path) -> AnalysisConfig:
    """Load a TOML document containing [tool.code-analysis]."""
    if not pyproject.is_file():
        return AnalysisConfig()
    try:
        import tomllib

        document = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        raw = document.get("tool", {}).get("code-analysis", {})
        if not isinstance(raw, dict):
            raise ConfigurationError("[tool.code-analysis] must be a table")
        if not raw:
            return AnalysisConfig()
        raw = dict(raw)
        if "max-static-call-occurrences" in raw:
            if "max_static_call_occurrences" in raw:
                raise ConfigurationError("duplicate static call occurrence limit configuration")
            raw["max_static_call_occurrences"] = raw.pop("max-static-call-occurrences")
        if "source-roots" in raw:
            raw["source_roots"] = tuple(raw.pop("source-roots"))
        if "test-roots" in raw:
            raw["test_roots"] = tuple(raw.pop("test-roots"))
        if "languages" in raw:
            raw["languages"] = tuple(LanguageSelection(str(item)) for item in raw["languages"])
        if "excludes" in raw:
            raw["excludes"] = tuple(raw["excludes"])
        if "contract-boundaries" in raw:
            boundaries = raw.pop("contract-boundaries")
            if not isinstance(boundaries, list):
                raise ConfigurationError("contract-boundaries must be an array of tables")
            raw["contract_boundaries"] = tuple(
                _contract_boundary_rule(item) if isinstance(item, dict) else item
                for item in boundaries
            )
        if "clone-thresholds" in raw:
            raw["clone_thresholds"] = raw.pop("clone-thresholds")
        if "phase-three-limits" in raw:
            limits = raw.pop("phase-three-limits")
            if not isinstance(limits, dict):
                raise ConfigurationError("phase-three-limits must be a table")
            raw["phase_three_limits"] = {
                str(key).replace("-", "_"): value for key, value in limits.items()
            }
        if "phase-four-limits" in raw:
            limits = raw.pop("phase-four-limits")
            if not isinstance(limits, dict):
                raise ConfigurationError("phase-four-limits must be a table")
            raw["phase_four_limits"] = {
                str(key).replace("-", "_"): value for key, value in limits.items()
            }
        if "git-path-scope" in raw:
            raw["git_path_scope"] = GitPathScope(str(raw.pop("git-path-scope")))
        if "runtime-analysis" in raw:
            runtime_analysis = raw.pop("runtime-analysis")
            if not isinstance(runtime_analysis, dict):
                raise ConfigurationError("runtime-analysis must be a table")
            runtime_analysis = {
                str(key).replace("-", "_"): value for key, value in runtime_analysis.items()
            }
            if "category_rules" in runtime_analysis:
                runtime_analysis["category_rules"] = tuple(
                    {str(key).replace("-", "_"): value for key, value in item.items()}
                    for item in runtime_analysis["category_rules"]
                )
            raw["runtime_analysis"] = RuntimeAnalysisConfig.model_validate(runtime_analysis)
        if "architecture-layers" in raw:
            layers = raw.pop("architecture-layers")
            if not isinstance(layers, list):
                raise ConfigurationError("architecture-layers must be an array of tables")
            raw["architecture_layers"] = tuple(
                _architecture_layer(item) if isinstance(item, dict) else item for item in layers
            )
        if "accepted-suppressions" in raw and "accepted_suppressions" in raw:
            raise ConfigurationError(
                "accepted-suppressions cannot be combined with accepted_suppressions"
            )
        if "accepted-suppressions" in raw:
            suppressions = raw.pop("accepted-suppressions")
            if not isinstance(suppressions, list):
                raise ConfigurationError("accepted-suppressions must be an array of tables")
            raw["accepted_suppressions"] = tuple(
                _accepted_finding_suppression(item) if isinstance(item, dict) else item
                for item in suppressions
            )
        if "suppression-evaluation-date" in raw and "suppression_evaluation_date" in raw:
            raise ConfigurationError(
                "suppression-evaluation-date cannot be combined with suppression_evaluation_date"
            )
        if "suppression-evaluation-date" in raw:
            raw["suppression_evaluation_date"] = _suppression_evaluation_date(
                raw.pop("suppression-evaluation-date")
            )
        javascript = raw.get("javascript", {})
        if not isinstance(javascript, dict):
            raise ConfigurationError("javascript must be a table")
        javascript = {str(key).replace("-", "_"): value for key, value in javascript.items()}
        if "default_module_mode" in javascript:
            javascript["default_module_mode"] = JavaScriptModuleMode(
                str(javascript["default_module_mode"])
            )
        rules = javascript.get("module_mode_rules", ())
        if not isinstance(rules, (list, tuple)):
            raise ConfigurationError("javascript.module-mode-rules must be an array of tables")
        javascript["module_mode_rules"] = tuple(
            _javascript_module_mode_rule(item) if isinstance(item, dict) else item for item in rules
        )
        raw["javascript"] = JavaScriptConfig.model_validate(javascript)
        workspace = raw.get("workspace", {})
        if not isinstance(workspace, dict):
            raise ConfigurationError("workspace must be a table")
        raw["workspace"] = WorkspaceConfig.model_validate(workspace)
        semantic = raw.get("semantic", {})
        if not isinstance(semantic, dict):
            raise ConfigurationError("semantic must be a table")
        semantic = {str(key).replace("-", "_"): value for key, value in semantic.items()}
        raw["semantic"] = SemanticConfig.model_validate(semantic)
        strictness = Strictness(raw.get("strictness", "normal"))
        preset = STRICTNESS_PRESETS[strictness].model_dump()
        threshold_overrides = raw.pop("thresholds", {})
        if not isinstance(threshold_overrides, dict):
            raise ConfigurationError("thresholds must be a table")
        preset.update(threshold_overrides)
        clone_preset = CLONE_THRESHOLD_PRESETS[strictness].model_dump()
        clone_overrides = raw.pop("clone_thresholds", {})
        if not isinstance(clone_overrides, dict):
            raise ConfigurationError("clone-thresholds must be a table")
        clone_preset.update(
            {str(key).replace("-", "_"): value for key, value in clone_overrides.items()}
        )
        return AnalysisConfig.model_validate(
            {
                **raw,
                "strictness": strictness,
                "thresholds": preset,
                "clone_thresholds": clone_preset,
            }
        )
    except (OSError, ValueError, TypeError) as exc:
        if isinstance(exc, ConfigurationError):
            raise
        raise ConfigurationError(f"invalid configuration: {exc}") from exc


def _contract_boundary_rule(raw: dict[object, object]) -> ContractBoundaryRule:
    normalized = {str(key).replace("-", "_"): value for key, value in raw.items()}
    if "boundary_class" in normalized:
        normalized["boundary_class"] = BoundaryClass(str(normalized["boundary_class"]))
    return ContractBoundaryRule.model_validate(normalized)


def _javascript_module_mode_rule(
    raw: dict[object, object],
) -> JavaScriptModuleModeRule:
    normalized = {str(key).replace("-", "_"): value for key, value in raw.items()}
    if "mode" in normalized:
        normalized["mode"] = ModuleSystem(str(normalized["mode"]))
    return JavaScriptModuleModeRule.model_validate(normalized)


def _architecture_layer(raw: dict[object, object]) -> ArchitectureLayer:
    normalized = {str(key).replace("-", "_"): value for key, value in raw.items()}
    for key in ("module_patterns", "may_import_layers"):
        if key in normalized:
            normalized[key] = tuple(normalized[key])
    return ArchitectureLayer.model_validate(normalized)


def _accepted_finding_suppression(raw: dict[object, object]) -> AcceptedFindingSuppression:
    normalized = {str(key).replace("-", "_"): value for key, value in raw.items()}
    if "expires_on" in normalized:
        normalized["expires_on"] = _suppression_date_value(
            normalized["expires_on"], label="expires-on"
        )
    return AcceptedFindingSuppression.model_validate(normalized)


def _suppression_evaluation_date(value: object) -> str:
    return _suppression_date_value(value, label="suppression-evaluation-date")


def _suppression_date_value(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise ConfigurationError(f"{label} must be a quoted canonical ISO date string")
    return validate_canonical_date_string(value, label=label)
