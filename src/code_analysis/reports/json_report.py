from __future__ import annotations

import json
from collections.abc import Mapping
from typing import cast

from pydantic import BaseModel

from code_analysis.models import Evidence, evidence_identity
from code_analysis.report_models import AnalysisReport
from code_analysis.reports.source_bindings import BoundEvidenceCatalog, bind_evidence

Report = AnalysisReport
type JsonObject = dict[str, object]

_CATALOG_FIELD = "evidence_catalog"
_SOURCES_FIELD = "source_bindings"
_EVIDENCE_FIELDS = {
    "alias_evidence": "alias_evidence_ids",
    "cleanup_evidence": "cleanup_evidence_ids",
    "counter_evidence": "counter_evidence_ids",
    "declaration_evidence": "declaration_evidence_ids",
    "evidence": "evidence_ids",
    "guard_evidence": "guard_evidence_ids",
    "mutations": "mutation_evidence_ids",
    "read_evidence": "read_evidence_ids",
    "serialization_sites": "serialization_evidence_ids",
    "sites": "site_evidence_ids",
    "supporting_evidence": "supporting_evidence_ids",
    "write_evidence": "write_evidence_ids",
}
_REFERENCE_FIELDS = {value: key for key, value in _EVIDENCE_FIELDS.items()}


def _evidence_id(evidence: Mapping[str, object]) -> str:
    normalized = Evidence.model_validate_json(json.dumps(evidence, ensure_ascii=False))
    return evidence_identity(normalized)


def _intern_payload(value: object, catalog: dict[str, JsonObject]) -> object:
    if isinstance(value, list):
        return [_intern_payload(item, catalog) for item in value]
    if not isinstance(value, dict):
        return value
    result: JsonObject = {}
    for key, item in value.items():
        if key in _EVIDENCE_FIELDS:
            if not isinstance(item, list):
                raise ValueError(f"{key} must be an evidence sequence")
            references: list[str] = []
            for evidence_value in item:
                if not isinstance(evidence_value, dict):
                    raise ValueError(f"{key} contains malformed evidence")
                validated = Evidence.model_validate_json(
                    json.dumps(evidence_value, ensure_ascii=False)
                ).model_dump(mode="json")
                evidence = cast(JsonObject, validated)
                identity = _evidence_id(evidence)
                previous = catalog.setdefault(identity, evidence)
                if previous != evidence:
                    raise ValueError(f"evidence identity collision: {identity}")
                references.append(identity)
            result[_EVIDENCE_FIELDS[key]] = references
        else:
            result[key] = _intern_payload(item, catalog)
    return result


def _retain_boundary_tags(model: object, sparse: object) -> None:
    """Language and primary evidence states remain explicit at default values."""
    if isinstance(model, BaseModel) and isinstance(sparse, dict):
        tags = {
            "language",
            "languages",
            "kind",
            "state",
            "parse_state",
            "confidence",
            "completeness",
            "fan_in",
            "fan_out",
        }
        sparse.update(model.model_dump(mode="json", include=tags))
        for key, value in sparse.items():
            _retain_boundary_tags(getattr(model, key), value)
    elif isinstance(model, dict) and isinstance(sparse, dict):
        for key, value in sparse.items():
            _retain_boundary_tags(model[key], value)
    elif isinstance(model, (list, tuple)) and isinstance(sparse, list):
        for original, reduced in zip(model, sparse, strict=True):
            _retain_boundary_tags(original, reduced)


def _report_payload(report: Report, *, compact: bool) -> JsonObject:
    if not compact:
        return cast(JsonObject, report.model_dump(mode="json"))
    sparse = report.model_dump(mode="json", exclude_defaults=True)
    # Never elide a report section or its aggregate identity/health contract.
    preserved = set(type(report).model_fields).difference(sparse) | {
        "configuration",
        "summary",
        "environment",
        "providers",
        "section_coverage",
        "source_files",
        "signal_quality",
    }
    sparse.update(report.model_dump(mode="json", include=preserved))
    _retain_boundary_tags(report, sparse)
    return cast(JsonObject, sparse)


def _persisted_payload(report: Report, *, compact: bool = False) -> JsonObject:
    catalog: dict[str, JsonObject] = {}
    payload = cast(JsonObject, _intern_payload(_report_payload(report, compact=compact), catalog))
    bound = bind_evidence(
        tuple(Evidence.model_validate_json(json.dumps(value)) for value in catalog.values())
    )
    payload[_CATALOG_FIELD] = [item.model_dump(mode="json") for item in bound.evidence]
    payload[_SOURCES_FIELD] = [item.model_dump(mode="json") for item in bound.sources]
    return payload


def _expand_payload(value: object, catalog: Mapping[str, JsonObject]) -> object:
    if isinstance(value, list):
        return [_expand_payload(item, catalog) for item in value]
    if not isinstance(value, dict):
        return value
    result: JsonObject = {}
    for key, item in value.items():
        if key.endswith("_catalog_ids"):
            if not isinstance(item, list) or not all(
                isinstance(identity, str) for identity in item
            ):
                raise ValueError(f"{key} must contain evidence identities")
            missing = sorted({identity for identity in item if identity not in catalog})
            if missing:
                raise ValueError(f"dangling evidence references: {', '.join(missing)}")
            result[key] = item
        elif key in _REFERENCE_FIELDS:
            if not isinstance(item, list) or not all(
                isinstance(identity, str) for identity in item
            ):
                raise ValueError(f"{key} must contain evidence identities")
            missing = sorted({identity for identity in item if identity not in catalog})
            if missing:
                raise ValueError(f"dangling evidence references: {', '.join(missing)}")
            # Expansion is read-only. Share validated JSON values until the final
            # strict report validation instead of copying each evidence occurrence.
            result[_REFERENCE_FIELDS[key]] = [catalog[identity] for identity in item]
        elif key in _EVIDENCE_FIELDS:
            raise ValueError(f"embedded persisted evidence is forbidden: {key}")
        else:
            result[key] = _expand_payload(item, catalog)
    return result


def render_json(report: Report, *, compact: bool = False) -> str:
    payload = _persisted_payload(report, compact=compact)
    if compact:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def canonical_json(report: Report) -> str:
    """Return the deterministic compact representation used for artifact identity."""

    return render_json(report, compact=True)


def load_json(text: str) -> Report:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("malformed report JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("report JSON must be an object")
    raw_catalog = payload.pop(_CATALOG_FIELD, None)
    if raw_catalog is None:
        raise ValueError("persisted report requires an evidence catalog")
    raw_sources = payload.pop(_SOURCES_FIELD, None)
    if raw_sources is None:
        raise ValueError("persisted report requires source bindings")
    bound = BoundEvidenceCatalog.model_validate_json(
        json.dumps({"sources": raw_sources, "evidence": raw_catalog}, ensure_ascii=False)
    )
    catalog = {
        item.evidence_id: cast(JsonObject, evidence.model_dump(mode="json"))
        for item, evidence in zip(bound.evidence, bound.expanded(), strict=True)
    }
    expanded = _expand_payload(payload, catalog)
    return AnalysisReport.model_validate_json(json.dumps(expanded, ensure_ascii=False))
