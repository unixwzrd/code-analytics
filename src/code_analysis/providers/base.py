from __future__ import annotations

from typing import Protocol

from code_analysis.context import AnalysisContext
from code_analysis.models import ProviderAvailability, ProviderResult


class AnalysisProvider(Protocol):
    provider_id: str

    def availability(self, context: AnalysisContext) -> ProviderAvailability: ...

    def run(self, context: AnalysisContext) -> ProviderResult: ...
