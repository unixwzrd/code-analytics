"""A baseline-only export must not inherit an unrelated candidate's state."""

from dataclasses import replace

import pytest
from test_runtime_browse_export import runtime_export_index  # noqa: F401

from code_analysis.models import Completeness
from code_analysis.runtime_browse_export import build_runtime_browse_export
from code_analysis.runtime_browse_models import RuntimeBrowseRequest
from code_analysis.runtime_queries import RuntimeQuerySection


@pytest.mark.parametrize("candidate_state", (Completeness.PARTIAL, Completeness.FAILED))
@pytest.mark.parametrize("candidate_matches", (False, True))
def test_baseline_export_ignores_candidate_health(
    runtime_export_index, candidate_state, candidate_matches  # noqa: F811
):
    index = runtime_export_index
    runtime = index.manifest.runtime
    candidate = runtime.comparison
    # Isolate the export reducer; these are not newly qualified capture claims.
    runtime = runtime.model_copy(
        update={
            "overview": runtime.overview.model_copy(
                update={
                    "completeness": Completeness.COMPLETE,
                    "source_matches": True,
                }
            ),
            "comparison": candidate.model_copy(
                update={
                    "candidate_overview": candidate.candidate_overview.model_copy(
                        update={
                            "completeness": candidate_state,
                            "source_matches": candidate_matches,
                        }
                    ),
                }
            ),
        }
    )
    index = replace(index, manifest=index.manifest.model_copy(update={"runtime": runtime}))
    projection = build_runtime_browse_export(
        index, RuntimeBrowseRequest(section=RuntimeQuerySection.HOTSPOTS, limit=100)
    )
    assert not projection.truncated
    assert projection.completeness is Completeness.COMPLETE
