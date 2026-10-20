"""Typed registry for built-in language front ends."""

from __future__ import annotations

from collections.abc import Callable

from code_analysis.languages.base import LanguageFrontend
from code_analysis.languages.javascript import JavaScriptFrontend
from code_analysis.languages.python import PythonFrontend
from code_analysis.languages.ruby import RubyPrismFrontend
from code_analysis.models import SourceLanguage

FrontendFactory = Callable[[], LanguageFrontend]

FRONTEND_REGISTRY: dict[SourceLanguage, FrontendFactory] = {
    SourceLanguage.PYTHON: PythonFrontend,
    SourceLanguage.JAVASCRIPT: JavaScriptFrontend,
    SourceLanguage.RUBY: RubyPrismFrontend,
}


def frontend_for(language: SourceLanguage) -> LanguageFrontend:
    """Create the registered front end for a supported language."""

    try:
        factory = FRONTEND_REGISTRY[language]
    except KeyError as exc:  # defensive against future enum/registry drift
        raise LookupError(f"language front end is not registered: {language.value}") from exc
    return factory()


def validate_frontend_registry() -> None:
    """Fail initialization/tests when enum and registry coverage diverge."""

    if set(FRONTEND_REGISTRY) != set(SourceLanguage):
        raise RuntimeError("language front-end registry must cover every source language exactly")


validate_frontend_registry()
