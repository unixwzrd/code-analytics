"""Optional, local-only embedding nomination over retained typed features."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol, runtime_checkable

from code_analysis.comparisons import ComparisonBuildError
from code_analysis.models import Completeness
from code_analysis.report_models import AnalysisReport
from code_analysis.reports.json_report import canonical_json
from code_analysis.semantic_models import (
    SemanticModelProvenance,
    SemanticNomination,
    SemanticNominationDisposition,
    SemanticNominationReport,
)


class SemanticProviderUnavailable(ComparisonBuildError):
    """The optional local semantic provider is not importable."""


class _EmbeddingVector(Protocol):
    def tolist(self) -> Iterable[float]: ...


@runtime_checkable
class _EmbeddingModel(Protocol):
    def encode(
        self,
        texts: list[str],
        *,
        normalize_embeddings: bool,
        convert_to_numpy: bool,
        show_progress_bar: bool,
    ) -> Iterable[_EmbeddingVector]: ...


@runtime_checkable
class _EmbeddingFactory(Protocol):
    def __call__(
        self, path: str, *, local_files_only: bool, trust_remote_code: bool
    ) -> _EmbeddingModel: ...


def _hash_directory(root: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(item for item in root.rglob("*") if item.is_file() and not item.is_symlink())
    if not files:
        raise ComparisonBuildError("semantic model directory contains no regular files")
    for path in files:
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode())
        digest.update(b"\0")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _license_identity(root: Path) -> tuple[str | None, tuple[str, ...]]:
    candidates = sorted(
        item
        for item in root.iterdir()
        if item.is_file() and item.name.lower().startswith(("license", "copying"))
    )
    if not candidates:
        return None, ()
    digest = hashlib.sha256()
    for item in candidates:
        digest.update(item.name.encode())
        digest.update(item.read_bytes())
    return digest.hexdigest(), tuple(item.name for item in candidates)


def _candidate_pairs(report: AnalysisReport, limit: int) -> tuple[tuple[tuple[str, str], ...], int]:
    postings: dict[str, list[str]] = defaultdict(list)
    for profile in report.function_profiles:
        identities = {
            *profile.callees,
            *profile.attributes_globals,
            *profile.exceptions,
            *profile.side_effects,
            *profile.dictionary_schema_operations,
            *profile.contract_ids,
            *profile.weak_semantic_signals,
        }
        for identity in sorted(identities):
            postings[identity].append(profile.subject_id)
    pairs: set[tuple[str, str]] = set()
    seen: set[tuple[str, str]] = set()
    omitted = 0
    for identity in sorted(postings):
        subjects = sorted(set(postings[identity]))
        for position, left in enumerate(subjects):
            for right in subjects[position + 1 :]:
                pair = (left, right)
                if pair in seen:
                    continue
                seen.add(pair)
                if len(pairs) >= limit:
                    omitted += 1
                    continue
                pairs.add(pair)
    return tuple(sorted(pairs)), omitted


def _profile_text(report: AnalysisReport) -> dict[str, str]:
    subjects = {item.subject_id: item for item in report.subjects}
    return {
        item.subject_id: " ".join(
            (
                subjects[item.subject_id].logical_name,
                *item.callees,
                *item.signature_shapes,
                *item.attributes_globals,
                *item.exceptions,
                *item.side_effects,
                *item.control_flow,
                *item.dictionary_schema_operations,
                *item.contract_ids,
                *item.weak_semantic_signals,
            )
        )
        for item in report.function_profiles
    }


def _deterministic_support(report: AnalysisReport) -> dict[tuple[str, str], tuple[str, ...]]:
    support: dict[tuple[str, str], set[str]] = defaultdict(set)
    for item in report.function_commonalities:
        left, right = sorted((item.left_subject_id, item.right_subject_id))
        pair = (left, right)
        support[pair].add(item.commonality_id)
    for cluster in report.clone_clusters:
        for position, left in enumerate(cluster.subject_ids):
            for right in cluster.subject_ids[position + 1 :]:
                support[(left, right)].add(cluster.cluster_id)
    return {key: tuple(sorted(value)) for key, value in support.items()}


def _vectors(model_directory: Path, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
    try:
        provider = importlib.import_module("sentence_transformers")
    except ImportError as exc:
        raise SemanticProviderUnavailable("sentence-transformers is unavailable") from exc
    factory: object = getattr(provider, "SentenceTransformer", None)
    if not isinstance(factory, _EmbeddingFactory):
        raise ComparisonBuildError("semantic provider has no callable model factory")
    model = factory(str(model_directory), local_files_only=True, trust_remote_code=False)
    if not isinstance(model, _EmbeddingModel):
        raise ComparisonBuildError("semantic provider model has no encoding capability")
    encoded = model.encode(
        list(texts),
        normalize_embeddings=True,
        convert_to_numpy=False,
        show_progress_bar=False,
    )
    vectors = tuple(tuple(float(value) for value in vector.tolist()) for vector in encoded)
    if len(vectors) != len(texts):
        raise ComparisonBuildError("semantic provider returned an unexpected vector count")
    dimensions = {len(vector) for vector in vectors}
    if dimensions and (0 in dimensions or len(dimensions) != 1):
        raise ComparisonBuildError("semantic provider returned inconsistent vector dimensions")
    if any(not math.isfinite(value) for vector in vectors for value in vector):
        raise ComparisonBuildError("semantic provider returned a non-finite vector value")
    return vectors


def build_semantic_nomination_report(
    report: AnalysisReport,
    model_directory: Path,
    *,
    max_nominations: int = 1_000,
) -> SemanticNominationReport:
    root = model_directory.expanduser()
    if not root.is_absolute() or not root.is_dir():
        raise ComparisonBuildError("semantic model must be an existing absolute local directory")
    if isinstance(max_nominations, bool) or not 1 <= max_nominations <= 50_000:
        raise ComparisonBuildError("semantic nomination limit must be between 1 and 50000")
    pairs, omitted = _candidate_pairs(report, max_nominations)
    texts = _profile_text(report)
    subject_ids = tuple(sorted({subject for pair in pairs for subject in pair}))
    vectors = _vectors(root, tuple(texts[item] for item in subject_ids)) if subject_ids else ()
    vector_by_subject = dict(zip(subject_ids, vectors, strict=True))
    support = _deterministic_support(report)
    candidate_by_pair: dict[tuple[str, str], set[str]] = defaultdict(set)
    for candidate in report.refactor_candidates:
        if len(candidate.participating_subject_ids) == 2:
            left, right = candidate.participating_subject_ids
            candidate_by_pair[(left, right)].add(candidate.candidate_id)
    nominations = []
    for left, right in pairs:
        similarity = math.fsum(
            left_value * right_value
            for left_value, right_value in zip(
                vector_by_subject[left], vector_by_subject[right], strict=True
            )
        )
        evidence = support.get((left, right), ())
        candidates = tuple(sorted(candidate_by_pair.get((left, right), ())))
        supported = bool(evidence or candidates)
        nominations.append(
            SemanticNomination(
                nomination_id="semantic-nomination-"
                + hashlib.sha256(f"{left}\0{right}".encode()).hexdigest()[:20],
                left_subject_id=left,
                right_subject_id=right,
                cosine_similarity=max(-1.0, min(1.0, similarity)),
                disposition=(
                    SemanticNominationDisposition.DETERMINISTICALLY_SUPPORTED
                    if supported
                    else SemanticNominationDisposition.REJECTED_DETERMINISTIC_EVIDENCE
                ),
                deterministic_evidence_ids=evidence,
                candidate_ids=candidates,
                reason=(
                    "Deterministic clone/commonality/candidate evidence independently "
                    "supports review."
                    if supported
                    else "Deterministic structure, behavior, contract, and ownership evidence "
                    "did not support a candidate."
                ),
            )
        )
    model_hash = _hash_directory(root)
    configuration_hash = hashlib.sha256(
        json.dumps(
            {"limit": max_nominations, "model": model_hash},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    source_sha = hashlib.sha256(canonical_json(report).encode()).hexdigest()
    identity = hashlib.sha256(f"{source_sha}\0{configuration_hash}".encode()).hexdigest()
    dimension = len(vectors[0]) if vectors else 1
    supported_count = sum(
        item.disposition is SemanticNominationDisposition.DETERMINISTICALLY_SUPPORTED
        for item in nominations
    )
    rejection_examples = tuple(
        sorted(
            item.nomination_id
            for item in nominations
            if item.disposition is SemanticNominationDisposition.REJECTED_DETERMINISTIC_EVIDENCE
        )[:25]
    )
    license_identity, license_files = _license_identity(root)
    completeness = (
        Completeness.PARTIAL
        if omitted or report.completeness is Completeness.PARTIAL
        else Completeness.COMPLETE
    )
    limitations = {
        "Embedding similarity only nominates pairs and is never a gate, confidence, "
        "authorization, or refactoring score.",
        "Every retained nomination is separately classified by deterministic static evidence.",
    }
    if omitted:
        limitations.add("Candidate pair generation was bounded.")
    return SemanticNominationReport(
        source_report_sha256=source_sha,
        nomination_identity_sha256=identity,
        provenance=SemanticModelProvenance(
            model_sha256=model_hash,
            configuration_sha256=configuration_hash,
            vector_dimension=dimension,
            provider_version=importlib.metadata.version("sentence-transformers"),
            license_identity=license_identity,
            license_files=license_files,
        ),
        considered_pairs=len(pairs) + omitted,
        retained_nominations=len(nominations),
        supported_nominations=supported_count,
        rejected_nominations=len(nominations) - supported_count,
        omitted_pairs=omitted,
        nominations=tuple(sorted(nominations, key=lambda item: item.nomination_id)),
        rejection_example_ids=rejection_examples,
        completeness=completeness,
        limitations=tuple(sorted(limitations)),
    )


def render_semantic_nomination(report: SemanticNominationReport, compact: bool = False) -> str:
    return (
        json.dumps(
            report.model_dump(mode="json"),
            ensure_ascii=False,
            indent=None if compact else 2,
            sort_keys=True,
            separators=(",", ":") if compact else None,
        )
        + "\n"
    )
