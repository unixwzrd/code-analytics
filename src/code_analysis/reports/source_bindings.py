"""Lossless typed source bindings for evidence storage normalization.

Evidence identity remains defined over the expanded analytical model,
independently of storage layout.
"""

from __future__ import annotations

import hashlib
from pathlib import PurePosixPath

from pydantic import Field, StrictInt, StrictStr, model_validator

from code_analysis.models import (
    Confidence,
    Evidence,
    SourceLanguage,
    SourceSpan,
    StrictModel,
    evidence_identity,
)


class SourceBinding(StrictModel):
    logical_path: StrictStr
    language: SourceLanguage
    source_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_path(self) -> SourceBinding:
        path = PurePosixPath(self.logical_path)
        if (
            not self.logical_path
            or self.logical_path == "."
            or path.is_absolute()
            or path.as_posix() != self.logical_path
            or ".." in path.parts
            or "\\" in self.logical_path
            or ":" in path.parts[0]
            or any(ord(character) < 32 for character in self.logical_path)
        ):
            raise ValueError("source binding requires a normalized portable relative path")
        return self

    def identity(self) -> str:
        payload = self.model_dump_json(include={"logical_path", "language", "source_sha256"})
        return "binding-" + hashlib.sha256(payload.encode()).hexdigest()[:20]


class BoundSource(SourceBinding):
    binding_id: StrictStr = Field(pattern=r"^binding-[0-9a-f]{20}$")

    @model_validator(mode="after")
    def validate_identity(self) -> BoundSource:
        binding = SourceBinding(
            logical_path=self.logical_path,
            language=self.language,
            source_sha256=self.source_sha256,
        )
        if self.binding_id != binding.identity():
            raise ValueError("source binding identity mismatch")
        return self


class BoundEvidence(StrictModel):
    evidence_id: StrictStr = Field(pattern=r"^evidence-[0-9a-f]{20}$")
    binding_id: StrictStr = Field(pattern=r"^binding-[0-9a-f]{20}$")
    start_line: StrictInt = Field(ge=1)
    start_column: StrictInt = Field(ge=0)
    end_line: StrictInt = Field(ge=1)
    end_column: StrictInt = Field(ge=0)
    provider: StrictStr
    observation: StrictStr
    confidence: Confidence

    @model_validator(mode="after")
    def validate_range(self) -> BoundEvidence:
        if (self.end_line, self.end_column) < (self.start_line, self.start_column):
            raise ValueError("evidence source range is reversed")
        return self

    def expand(self, source: BoundSource) -> Evidence:
        if source.binding_id != self.binding_id:
            raise ValueError("evidence source binding mismatch")
        evidence = Evidence(
            span=SourceSpan(
                path=source.logical_path,
                language=source.language,
                start_line=self.start_line,
                start_column=self.start_column,
                end_line=self.end_line,
                end_column=self.end_column,
            ),
            provider=self.provider,
            observation=self.observation,
            confidence=self.confidence,
            source_sha256=source.source_sha256,
            language=source.language,
        )
        if evidence_identity(evidence) != self.evidence_id:
            raise ValueError("expanded evidence identity mismatch")
        return evidence


class BoundEvidenceCatalog(StrictModel):
    sources: tuple[BoundSource, ...]
    evidence: tuple[BoundEvidence, ...]

    @model_validator(mode="after")
    def validate_references(self) -> BoundEvidenceCatalog:
        source_ids = tuple(item.binding_id for item in self.sources)
        evidence_ids = tuple(item.evidence_id for item in self.evidence)
        for identities in (source_ids, evidence_ids):
            if identities != tuple(sorted(set(identities))):
                raise ValueError("catalog identities must be unique and sorted")
        sources = {item.binding_id: item for item in self.sources}
        referenced = {item.binding_id for item in self.evidence}
        if referenced != set(sources):
            raise ValueError("dangling or unused source bindings")
        for item in self.evidence:
            item.expand(sources[item.binding_id])
        return self

    def expanded(self) -> tuple[Evidence, ...]:
        sources = {item.binding_id: item for item in self.sources}
        return tuple(item.expand(sources[item.binding_id]) for item in self.evidence)


def bind_evidence(values: tuple[Evidence, ...]) -> BoundEvidenceCatalog:
    sources: dict[str, BoundSource] = {}
    observations: dict[str, BoundEvidence] = {}
    for value in values:
        source = SourceBinding(
            logical_path=value.span.path,
            language=value.language,
            source_sha256=value.source_sha256,
        )
        binding = BoundSource(**source.model_dump(), binding_id=source.identity())
        prior = sources.setdefault(binding.binding_id, binding)
        if prior != binding:
            raise ValueError("source binding identity collision")
        item = BoundEvidence(
            evidence_id=evidence_identity(value),
            binding_id=binding.binding_id,
            start_line=value.span.start_line,
            start_column=value.span.start_column,
            end_line=value.span.end_line,
            end_column=value.span.end_column,
            provider=value.provider,
            observation=value.observation,
            confidence=value.confidence,
        )
        previous = observations.setdefault(item.evidence_id, item)
        if previous != item:
            raise ValueError("evidence identity collision")
    return BoundEvidenceCatalog(
        sources=tuple(sources[key] for key in sorted(sources)),
        evidence=tuple(observations[key] for key in sorted(observations)),
    )
