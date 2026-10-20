"""Reject ambiguous comparison identities before disclosing source."""

from test_web_runtime_comparison_source import (
    GOLDEN,
    _project_frame,
    comparison_server,
    fetch,
    served,
    source_path,
    source_root,
)

from code_analysis.ui_runtime import RuntimeViewService


def test_duplicate_frame_join_keys_cannot_choose_an_arbitrary_source_frame(
    ordinary_report, tmp_path
):
    original = RuntimeViewService.load(GOLDEN).profile
    first = _project_frame(
        original,
        path="alpha/a.py",
        line=1,
        function="ambiguous",
        frame_id="profile-frame-aaaaaaaaaaaaaaaaaaaa",
    )
    second = first.model_copy(update={"frame_id": "profile-frame-" + "b" * 20})
    server, delta = comparison_server(
        ordinary_report,
        tmp_path,
        source_root_path=source_root(tmp_path),
        baseline_frames=(first, second),
        candidate_frames=(first,),
    )
    with served(server):
        status, body = fetch(server, source_path(delta.records[0].frame_identity, "baseline"))
    assert status == 400
    assert "ambiguous" in body["error"]["message"]
