"""Deterministic serialization for validated report projections."""

from __future__ import annotations

import json

from code_analysis.projection_models import PROJECTION_ADAPTER, Projection


def render_projection_json(projection: Projection, *, compact: bool = False) -> str:
    payload = PROJECTION_ADAPTER.dump_python(projection, mode="json")
    if compact:
        return (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def load_projection_json(text: str) -> Projection:
    return PROJECTION_ADAPTER.validate_json(text)
