"""Typed content-addressed cache for normalized JavaScript front-end facts."""

from __future__ import annotations

import importlib.metadata
import platform
from typing import Literal, Self

from pydantic import Field, model_validator

from code_analysis import __version__
from code_analysis.cache import CacheKey, CacheLookupState, ContentCache
from code_analysis.config import AnalysisConfig
from code_analysis.languages.base import (
    FrontendResult,
    NormalizedExportFact,
    NormalizedFunctionFact,
    NormalizedImportFact,
    NormalizedStateFact,
    NormalizedSymbolFact,
    SourceUnit,
)
from code_analysis.models import (
    Completeness,
    Diagnostic,
    ModuleSystem,
    ParserProvenance,
    SourceLanguage,
    StrictModel,
)

NORMALIZATION_IDENTITY = "javascript-normalized-facts:v1"


class JavaScriptFrontendCachePayload(StrictModel):
    schema_name: Literal["code-analysis-javascript-frontend-cache"] = (
        "code-analysis-javascript-frontend-cache"
    )
    schema_version: Literal["1.0.0"] = "1.0.0"
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    symbols: tuple[NormalizedSymbolFact, ...]
    imports: tuple[NormalizedImportFact, ...]
    exports: tuple[NormalizedExportFact, ...]
    functions: tuple[NormalizedFunctionFact, ...]
    states: tuple[NormalizedStateFact, ...]
    module_system: ModuleSystem
    syntax_mode: str
    provenance: ParserProvenance
    diagnostics: tuple[Diagnostic, ...]
    completeness: Completeness

    @model_validator(mode="after")
    def validate_binding(self) -> Self:
        if self.provenance.language is not SourceLanguage.JAVASCRIPT:
            raise ValueError("JavaScript cache provenance has the wrong language")
        if self.provenance.source_sha256 != self.source_sha256:
            raise ValueError("JavaScript cache provenance has the wrong source hash")
        if self.provenance.normalization_identity != NORMALIZATION_IDENTITY:
            raise ValueError("JavaScript cache normalization identity is unsupported")
        return self


def javascript_cache_key(
    unit: SourceUnit,
    config: AnalysisConfig,
    module_system: ModuleSystem | None,
) -> CacheKey:
    return CacheKey(
        namespace="javascript-frontend",
        source_sha256=unit.source_sha256,
        analyzer_version=__version__,
        python_version=platform.python_version(),
        configuration_sha256=config.identity(),
        provider_versions=(
            f"tree-sitter:{_version('tree-sitter')}",
            f"tree-sitter-javascript:{_version('tree-sitter-javascript')}",
        ),
        dependency_identities=(
            NORMALIZATION_IDENTITY,
            f"language:{unit.language.value}",
            f"module-system:{module_system.value if module_system else 'auto'}",
        ),
    )


def load_cached_javascript_result(
    cache: ContentCache,
    unit: SourceUnit,
    config: AnalysisConfig,
    module_system: ModuleSystem | None,
) -> FrontendResult | None:
    lookup = cache.get(javascript_cache_key(unit, config, module_system))
    if lookup.state is not CacheLookupState.HIT or lookup.payload is None:
        return None
    try:
        payload = JavaScriptFrontendCachePayload.model_validate_json(lookup.payload)
    except ValueError:
        cache.invalidations += 1
        return None
    if payload.source_sha256 != unit.source_sha256:
        cache.invalidations += 1
        return None
    return FrontendResult(
        unit=unit,
        symbols=payload.symbols,
        imports=payload.imports,
        exports=payload.exports,
        functions=payload.functions,
        states=payload.states,
        module_system=payload.module_system,
        syntax_mode=payload.syntax_mode,
        provenance=payload.provenance.model_copy(update={"logical_path": unit.logical_path}),
        diagnostics=payload.diagnostics,
        completeness=payload.completeness,
    )


def store_cached_javascript_result(
    cache: ContentCache,
    result: FrontendResult,
    config: AnalysisConfig,
    module_system: ModuleSystem | None,
) -> None:
    payload = JavaScriptFrontendCachePayload(
        source_sha256=result.unit.source_sha256,
        symbols=result.symbols,
        imports=result.imports,
        exports=result.exports,
        functions=result.functions,
        states=result.states,
        module_system=result.module_system,
        syntax_mode=result.syntax_mode,
        provenance=result.provenance,
        diagnostics=result.diagnostics,
        completeness=result.completeness,
    )
    cache.put(
        javascript_cache_key(result.unit, config, module_system),
        payload.model_dump_json().encode("utf-8"),
    )


def _version(distribution: str) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return "unavailable"
