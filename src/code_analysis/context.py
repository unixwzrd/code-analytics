from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from code_analysis.config import AnalysisConfig


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    physical_root: Path
    logical_root: str
    source_roots: tuple[Path, ...]
    logical_source_roots: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AnalysisContext:
    target: Path
    source: SourceIdentity
    config: AnalysisConfig
