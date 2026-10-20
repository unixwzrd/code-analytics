"""Typed fail-closed section assembly values."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from pydantic import ValidationError

from code_analysis.models import SourceLanguage


@dataclass(frozen=True, slots=True)
class AssemblyFailureFact:
    section: str
    record_kind: str
    language: SourceLanguage
    affected_ids: tuple[str, ...]
    field_paths: tuple[str, ...]
    diagnostic_code: str

    @property
    def identity(self) -> str:
        payload = json.dumps(
            (
                self.section,
                self.record_kind,
                self.language.value,
                self.affected_ids,
                self.field_paths,
                self.diagnostic_code,
            ),
            separators=(",", ":"),
        ).encode("utf-8")
        return f"assembly-failure-{hashlib.sha256(payload).hexdigest()[:20]}"


@dataclass(frozen=True, slots=True)
class SectionAssemblyResult[T]:
    records: tuple[T, ...]
    failures: tuple[AssemblyFailureFact, ...]


def validation_failure(
    error: ValidationError,
    *,
    section: str,
    record_kind: str,
    language: SourceLanguage,
    affected_ids: tuple[str, ...],
    diagnostic_code: str,
) -> AssemblyFailureFact:
    """Reduce a validation error to stable bounded field paths."""

    paths = tuple(
        sorted(
            {
                ".".join(str(part) for part in item["loc"])
                for item in error.errors(
                    include_url=False,
                    include_context=False,
                    include_input=False,
                )
            }
        )
    )
    return AssemblyFailureFact(
        section=section,
        record_kind=record_kind,
        language=language,
        affected_ids=tuple(sorted(set(affected_ids))),
        field_paths=paths or ("record",),
        diagnostic_code=diagnostic_code,
    )
