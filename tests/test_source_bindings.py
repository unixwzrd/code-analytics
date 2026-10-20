from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from code_analysis.models import Confidence, Evidence, SourceLanguage, SourceSpan, evidence_identity
from code_analysis.reports.source_bindings import BoundEvidenceCatalog, SourceBinding, bind_evidence


def _evidence(line: int = 1, language: SourceLanguage = SourceLanguage.PYTHON) -> Evidence:
    return Evidence(
        span=SourceSpan(
            path="src/example.py",
            start_line=line,
            end_line=line,
            start_column=0,
            end_column=5,
            language=language,
        ),
        source_sha256="a" * 64,
        provider="synthetic",
        observation="exact evidence",
        confidence=Confidence.EXACT,
        language=language,
    )


def test_bindings_round_trip_without_changing_evidence_identities() -> None:
    values = (_evidence(), _evidence(2), _evidence())
    catalog = bind_evidence(values)
    assert len(catalog.sources) == 1
    expected = tuple(sorted(set(values), key=evidence_identity))
    assert catalog.expanded() == expected
    assert catalog == bind_evidence(tuple(reversed(values)))
    assert BoundEvidenceCatalog.model_validate_json(catalog.model_dump_json()) == catalog
    assert catalog.sources[0].identity() == catalog.sources[0].binding_id


@pytest.mark.parametrize(
    "path",
    [
        "",
        ".",
        "/tmp/file.py",
        "../file.py",
        "a/../b",
        "a//b",
        "a/./b",
        "a\\b",
        "C:/file.py",
        "a\nfile.py",
    ],
)
def test_bindings_reject_nonportable_paths(path: str) -> None:
    with pytest.raises(ValidationError):
        SourceBinding(logical_path=path, language=SourceLanguage.PYTHON, source_sha256="a" * 64)


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate-source",
        "duplicate-evidence",
        "dangling",
        "unused",
        "hash",
        "language",
        "observation",
        "bool-line",
        "reverse-range",
        "extra",
    ],
)
def test_bindings_fail_closed(mutation: str) -> None:
    payload = json.loads(bind_evidence((_evidence(),)).model_dump_json())
    match mutation:
        case "duplicate-source":
            payload["sources"] *= 2
        case "duplicate-evidence":
            payload["evidence"] *= 2
        case "dangling":
            payload["sources"] = []
        case "unused":
            payload["evidence"] = []
        case "hash":
            payload["sources"][0]["source_sha256"] = "b" * 64
        case "language":
            payload["sources"][0]["language"] = "javascript"
        case "observation":
            payload["evidence"][0]["observation"] = "changed"
        case "bool-line":
            payload["evidence"][0]["start_line"] = True
        case "reverse-range":
            payload["evidence"][0]["start_line"] = 2
        case "extra":
            payload["evidence"][0]["extra"] = "ignored?"
    with pytest.raises(ValidationError):
        BoundEvidenceCatalog.model_validate_json(json.dumps(payload))


def test_language_and_source_versions_have_distinct_bindings() -> None:
    first = _evidence()
    second = _evidence(language=SourceLanguage.JAVASCRIPT)
    third = Evidence.model_validate({**first.model_dump(), "source_sha256": "b" * 64})
    catalog = bind_evidence((first, second, third))
    assert len(catalog.sources) == 3
    assert set(catalog.expanded()) == {first, second, third}
    assert bind_evidence(()) == BoundEvidenceCatalog(sources=(), evidence=())
