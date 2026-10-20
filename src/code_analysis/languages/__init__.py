"""Language-front-end contracts and registry."""

from code_analysis.languages.base import (
    FrontendContext,
    FrontendResult,
    LanguageFrontend,
    NormalizedExportFact,
    NormalizedImportFact,
    NormalizedSymbolFact,
    SourceUnit,
)
from code_analysis.languages.registry import FRONTEND_REGISTRY, frontend_for

__all__ = (
    "FRONTEND_REGISTRY",
    "FrontendContext",
    "FrontendResult",
    "LanguageFrontend",
    "NormalizedExportFact",
    "NormalizedImportFact",
    "NormalizedSymbolFact",
    "SourceUnit",
    "frontend_for",
)
