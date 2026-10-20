"""HTTP and browser coverage for bounded runtime comparison source previews."""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
from contextlib import contextmanager
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import urlencode

import pytest
from browser_navigation import reveal_navigation
from test_runtime_comparison_source_browsing import (
    GOLDEN,
    _project_frame,
    _source_hash,
)

from code_analysis.config import AnalysisConfig
from code_analysis.profile_models import ProfileReport
from code_analysis.profiling import build_profile_diff, render_profile_json
from code_analysis.project import discover_context
from code_analysis.reports.json_report import load_json
from code_analysis.runtime_models import RuntimeArtifact, RuntimeSourceHash, SourceBindingState
from code_analysis.service import scan
from code_analysis.ui_runtime import (
    RuntimeComparisonSourcePreview,
    RuntimeViewService,
)
from code_analysis.web_server import create_server
from code_analysis.web_service import ReportViewService

TOKEN = "web-runtime-comparison-source"
FIXTURES = Path(__file__).parent / "fixtures" / "ordinary"
MINIMAL = Path(__file__).parent / "golden" / "minimal-report.json"
OUTPUT = Path(__file__).resolve().parents[1] / "output" / "playwright"
LONG_SOURCE_PATH = "alpha/very/long/nested/directory/structure/for/responsive/source/preview/a.py"

DOCUMENT_WIDTH_CONTAINED = """() =>
  document.documentElement.scrollWidth <= document.documentElement.clientWidth"""

WAIT_TWO_ANIMATION_FRAMES = (
    "() => new Promise(resolve => " "requestAnimationFrame(() => requestAnimationFrame(resolve)))"
)

OVERFLOW_METRICS = """() => {
  const doc = document.documentElement;
  const offenders = [];
  for (const element of document.querySelectorAll('body *')) {
    const rect = element.getBoundingClientRect();
    if (rect.right > window.innerWidth + 0.5) {
      offenders.push({
        tag: element.tagName,
        class: element.className,
        scrollWidth: element.scrollWidth,
        clientWidth: element.clientWidth,
        right: Math.round(rect.right),
        viewport: window.innerWidth,
      });
    }
  }
  offenders.sort((left, right) => (right.right - right.viewport) - (left.right - left.viewport));
  const tableWrap = document.querySelector('#runtime-results .table-wrap');
  return {
    docScroll: doc.scrollWidth,
    docClient: doc.clientWidth,
    offenders: offenders.slice(0, 6),
    tableWrap: tableWrap
      ? {
          scrollWidth: tableWrap.scrollWidth,
          clientWidth: tableWrap.clientWidth,
        }
      : null,
  };
}"""


def source_root(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    shutil.copytree(FIXTURES, root)
    return root


def build_comparison_runtime(
    ordinary_report,
    tmp_path,
    *,
    candidate_hash: str | None = None,
    candidate_binding: SourceBindingState = SourceBindingState.MATCHED,
    baseline_frames=None,
    candidate_frames=None,
    multi_artifact: bool = False,
):
    static_sha = ReportViewService(ordinary_report).bootstrap().reports[0].report_sha256
    original = RuntimeViewService.load(GOLDEN).profile
    source = _source_hash(ordinary_report)
    baseline_frame = _project_frame(
        original,
        path=source.path,
        line=1,
        function="baseline_fn",
        frame_id="profile-frame-aaaaaaaaaaaaaaaaaaaa",
    )
    candidate_frame = baseline_frame.model_copy(
        update={"frame_id": "profile-frame-bbbbbbbbbbbbbbbbbbbb"}
    )
    artifact_a = "a" * 64
    artifact_b = "b" * 64
    if multi_artifact:
        baseline_frame = baseline_frame.model_copy(
            update={"artifact_sha256": artifact_a, "function": "shared_fn"}
        )
        candidate_frame = candidate_frame.model_copy(
            update={"artifact_sha256": artifact_a, "function": "shared_fn"}
        )
        artifacts = (
            RuntimeArtifact(sha256=artifact_a, byte_count=1, kind="profile"),
            RuntimeArtifact(sha256=artifact_b, byte_count=1, kind="profile"),
        )
    else:
        artifacts = ()
    baseline_frames = baseline_frames if baseline_frames is not None else (baseline_frame,)
    candidate_frames = candidate_frames if candidate_frames is not None else (candidate_frame,)
    candidate_hash_value = candidate_hash if candidate_hash is not None else source.sha256
    runtime_kwargs = {
        "source_binding": SourceBindingState.MATCHED,
        "source_hashes": (source,),
        "artifacts": artifacts,
    }
    baseline = ProfileReport.model_validate(
        {
            **original.model_dump(),
            "frames": baseline_frames,
            "runtime_edges": (),
            "statistics": (),
            "source_report_sha256": static_sha,
            "runtime": original.runtime.model_copy(update=runtime_kwargs),
        }
    )
    candidate = ProfileReport.model_validate(
        {
            **baseline.model_dump(),
            "profile_identity_sha256": "c" * 64,
            "frames": candidate_frames,
            "runtime": baseline.runtime.model_copy(
                update={
                    "source_binding": candidate_binding,
                    "source_hashes": (
                        RuntimeSourceHash(path=source.path, sha256=candidate_hash_value),
                    ),
                }
            ),
        }
    )
    delta = build_profile_diff(baseline, candidate, diagnostic_only=True)
    return RuntimeViewService(baseline, candidate, delta), delta


@contextmanager
def served(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def fetch(server, path: str, *, token: str | None = TOKEN) -> tuple[int, dict]:
    connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    try:
        headers = {"X-Code-Analysis-Token": token} if token is not None else {}
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        body = json.loads(response.read())
        return response.status, body
    finally:
        connection.close()


def comparison_server_with_long_source_path(
    tmp_path,
    *,
    logical_path: str = LONG_SOURCE_PATH,
) -> tuple:
    root = source_root(tmp_path)
    target = root / logical_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text((root / "alpha" / "a.py").read_text(encoding="utf-8"), encoding="utf-8")
    extended_report = scan(discover_context(root, AnalysisConfig()))
    source = _source_hash(extended_report, logical_path)
    original = RuntimeViewService.load(GOLDEN).profile
    frame = _project_frame(
        original,
        path=logical_path,
        line=17,
        function="later_function",
        frame_id="profile-frame-aaaaaaaaaaaaaaaaaaaa",
    )
    runtime_service, delta = build_comparison_runtime(
        extended_report,
        tmp_path,
        baseline_frames=(frame,),
        candidate_frames=(frame,),
    )
    profile = runtime_service.profile.model_validate(
        {
            **runtime_service.profile.model_dump(),
            "frames": (frame,),
            "runtime": runtime_service.profile.runtime.model_copy(
                update={
                    "source_hashes": (RuntimeSourceHash(path=logical_path, sha256=source.sha256),)
                }
            ),
        }
    )
    candidate = runtime_service.comparison
    assert candidate is not None
    candidate = candidate.model_validate(
        {
            **candidate.model_dump(),
            "frames": (frame,),
            "runtime": candidate.runtime.model_copy(
                update={
                    "source_hashes": (RuntimeSourceHash(path=logical_path, sha256=source.sha256),)
                }
            ),
        }
    )
    delta = build_profile_diff(profile, candidate, diagnostic_only=True)
    runtime_service = RuntimeViewService(profile, candidate, delta)
    baseline_path = tmp_path / "baseline-profile.json"
    candidate_path = tmp_path / "candidate-profile.json"
    baseline_path.write_text(render_profile_json(runtime_service.profile, compact=True))
    candidate_path.write_text(render_profile_json(candidate, compact=True))
    server = create_server(
        ReportViewService(extended_report),
        source_root=root,
        profile_report=baseline_path,
        compare_profile=candidate_path,
        token=TOKEN,
    )
    return server, delta, logical_path, source.sha256


def comparison_server(ordinary_report, tmp_path, *, source_root_path: Path | None = None, **kwargs):
    runtime_service, delta = build_comparison_runtime(ordinary_report, tmp_path, **kwargs)
    baseline_path = tmp_path / "baseline-profile.json"
    candidate_path = tmp_path / "candidate-profile.json"
    baseline_path.write_text(render_profile_json(runtime_service.profile, compact=True))
    candidate_path.write_text(render_profile_json(runtime_service.comparison, compact=True))
    server = create_server(
        ReportViewService(ordinary_report),
        source_root=source_root_path,
        profile_report=baseline_path,
        compare_profile=candidate_path,
        token=TOKEN,
    )
    return server, delta


def source_path(identity: str, side: str, **extra: str) -> str:
    params = {"frame_identity": identity, "side": side, "start": "1", "end": "40", **extra}
    return "/api/runtime/comparison-source?" + urlencode(params)


def test_comparison_source_requires_token(ordinary_report, tmp_path) -> None:
    server, delta = comparison_server(
        ordinary_report, tmp_path, source_root_path=source_root(tmp_path)
    )
    identity = delta.records[0].frame_identity
    with served(server):
        status, _ = fetch(server, source_path(identity, "baseline"), token=None)
    assert status == 403


def test_comparison_source_rejects_invalid_side(ordinary_report, tmp_path) -> None:
    server, delta = comparison_server(
        ordinary_report, tmp_path, source_root_path=source_root(tmp_path)
    )
    identity = delta.records[0].frame_identity
    with served(server):
        status, body = fetch(server, source_path(identity, "typo"))
    assert status == 400
    assert "baseline or candidate" in body["error"]["message"]


def test_comparison_source_rejects_unknown_parameters(ordinary_report, tmp_path) -> None:
    server, delta = comparison_server(
        ordinary_report, tmp_path, source_root_path=source_root(tmp_path)
    )
    identity = delta.records[0].frame_identity
    with served(server):
        status, body = fetch(
            server,
            source_path(identity, "baseline", extra="1"),
        )
    assert status == 400
    assert "unsupported runtime comparison source parameters" in body["error"]["message"]


def test_comparison_source_rejects_duplicate_parameters(ordinary_report, tmp_path) -> None:
    server, delta = comparison_server(
        ordinary_report, tmp_path, source_root_path=source_root(tmp_path)
    )
    identity = delta.records[0].frame_identity
    path = (
        f"/api/runtime/comparison-source?side=baseline&side=candidate"
        f"&frame_identity={identity}&start=1&end=40"
    )
    with served(server):
        status, body = fetch(server, path)
    assert status == 400
    assert "side must be supplied once" in body["error"]["message"]


def test_comparison_source_get_does_not_build_indexes(
    ordinary_report, tmp_path, monkeypatch
) -> None:
    server, delta = comparison_server(
        ordinary_report, tmp_path, source_root_path=source_root(tmp_path)
    )
    identity = delta.records[0].frame_identity

    def forbid_build(*_args, **_kwargs):
        raise AssertionError("browse index build attempted during comparison-source GET")

    def forbid_tempdir(*_args, **_kwargs):
        raise AssertionError("temporary browse directory requested during comparison-source GET")

    monkeypatch.setattr("code_analysis.browse_index_build.build_browse_index", forbid_build)
    monkeypatch.setattr(tempfile, "mkdtemp", forbid_tempdir)
    with served(server):
        status, body = fetch(server, source_path(identity, "baseline"))
    assert status == 200
    assert body["state"] == "bound"


def test_baseline_source_preview_is_bound(ordinary_report, tmp_path) -> None:
    server, delta = comparison_server(
        ordinary_report, tmp_path, source_root_path=source_root(tmp_path)
    )
    identity = delta.records[0].frame_identity
    with served(server):
        status, body = fetch(server, source_path(identity, "baseline"))
    assert status == 200
    RuntimeComparisonSourcePreview.model_validate_json(json.dumps(body))
    assert body["side"] == "baseline"
    assert body["frame_identity"] == identity
    assert body["state"] == "bound"
    assert body["preview"]["logical_path"] == "alpha/a.py"


def test_unavailable_response_has_null_optional_fields(ordinary_report, tmp_path) -> None:
    server, delta = comparison_server(
        ordinary_report,
        tmp_path,
        source_root_path=source_root(tmp_path),
        candidate_hash="0" * 64,
    )
    identity = delta.records[0].frame_identity
    with served(server):
        status, body = fetch(server, source_path(identity, "candidate"))
    assert status == 200
    assert body["state"] == "unavailable"
    assert body["frame_id"] is None
    assert body["preview"] is None


def test_candidate_only_row_marks_baseline_unavailable(ordinary_report, tmp_path) -> None:
    original = RuntimeViewService.load(GOLDEN).profile
    source = _source_hash(ordinary_report)
    only_baseline = _project_frame(
        original,
        path=source.path,
        line=1,
        function="only_baseline",
        frame_id="profile-frame-cccccccccccccccccccc",
    )
    only_candidate = only_baseline.model_copy(
        update={
            "frame_id": "profile-frame-dddddddddddddddddddd",
            "function": "only_candidate",
        }
    )
    server, delta = comparison_server(
        ordinary_report,
        tmp_path,
        source_root_path=source_root(tmp_path),
        baseline_frames=(only_baseline,),
        candidate_frames=(only_candidate,),
    )
    candidate_only = next(
        record for record in delta.records if record.candidate_total_seconds is not None
    )
    with served(server):
        status, body = fetch(server, source_path(candidate_only.frame_identity, "baseline"))
    assert status == 200
    assert body["state"] == "unavailable"
    assert body["preview"] is None


def test_baseline_only_row_marks_candidate_unavailable(ordinary_report, tmp_path) -> None:
    original = RuntimeViewService.load(GOLDEN).profile
    source = _source_hash(ordinary_report)
    only_baseline = _project_frame(
        original,
        path=source.path,
        line=1,
        function="only_baseline",
        frame_id="profile-frame-cccccccccccccccccccc",
    )
    only_candidate = only_baseline.model_copy(
        update={
            "frame_id": "profile-frame-dddddddddddddddddddd",
            "function": "only_candidate",
        }
    )
    server, delta = comparison_server(
        ordinary_report,
        tmp_path,
        source_root_path=source_root(tmp_path),
        baseline_frames=(only_baseline,),
        candidate_frames=(only_candidate,),
    )
    baseline_only = next(
        record for record in delta.records if record.baseline_total_seconds is not None
    )
    with served(server):
        status, body = fetch(server, source_path(baseline_only.frame_identity, "candidate"))
    assert status == 200
    assert body["state"] == "unavailable"


def test_candidate_side_unavailable_when_captured_hash_mismatches_static(
    ordinary_report, tmp_path
) -> None:
    server, delta = comparison_server(
        ordinary_report,
        tmp_path,
        source_root_path=source_root(tmp_path),
        candidate_hash="0" * 64,
    )
    identity = delta.records[0].frame_identity
    with served(server):
        baseline = fetch(server, source_path(identity, "baseline"))
        candidate = fetch(server, source_path(identity, "candidate"))
    assert baseline[1]["state"] == "bound"
    assert candidate[1]["state"] == "unavailable"


def test_candidate_side_requires_matching_selected_static_report(ordinary_report, tmp_path) -> None:
    runtime_service, delta = build_comparison_runtime(ordinary_report, tmp_path)
    candidate = runtime_service.comparison
    assert candidate is not None
    candidate = candidate.model_validate(
        {
            **candidate.model_dump(),
            "source_report_sha256": "f" * 64,
        }
    )
    delta = build_profile_diff(runtime_service.profile, candidate, diagnostic_only=True)
    runtime_service = RuntimeViewService(runtime_service.profile, candidate, delta)
    baseline_path = tmp_path / "baseline-profile.json"
    candidate_path = tmp_path / "candidate-profile.json"
    baseline_path.write_text(render_profile_json(runtime_service.profile, compact=True))
    candidate_path.write_text(render_profile_json(candidate, compact=True))
    server = create_server(
        ReportViewService(ordinary_report),
        source_root=source_root(tmp_path),
        profile_report=baseline_path,
        compare_profile=candidate_path,
        token=TOKEN,
    )
    identity = delta.records[0].frame_identity
    with served(server):
        status, body = fetch(server, source_path(identity, "candidate"))
    assert status == 200
    assert body["state"] == "unavailable"
    assert "candidate profile does not match" in body["message"]


def test_loaded_distinct_candidate_static_uses_own_captured_hash(
    ordinary_report,
    tmp_path,
) -> None:
    minimal = load_json(MINIMAL.read_text(encoding="utf-8"))
    minimal_sha = ReportViewService(minimal).bootstrap().reports[0].report_sha256
    minimal_source = minimal.source_files[0]
    original = RuntimeViewService.load(GOLDEN).profile
    baseline_frame = _project_frame(
        original,
        path=_source_hash(ordinary_report).path,
        line=1,
        function="baseline_fn",
        frame_id="profile-frame-aaaaaaaaaaaaaaaaaaaa",
    )
    candidate_frame = baseline_frame.model_copy(
        update={
            "frame_id": "profile-frame-bbbbbbbbbbbbbbbbbbbb",
            "path": minimal_source.logical_path,
            "line": 1,
            "function": "visible",
        }
    )
    runtime_service, delta = build_comparison_runtime(
        ordinary_report,
        tmp_path,
        baseline_frames=(baseline_frame,),
        candidate_frames=(candidate_frame,),
    )
    candidate = runtime_service.comparison
    assert candidate is not None
    candidate = candidate.model_validate(
        {
            **candidate.model_dump(),
            "source_report_sha256": minimal_sha,
            "runtime": candidate.runtime.model_copy(
                update={
                    "source_hashes": (
                        RuntimeSourceHash(
                            path=minimal_source.logical_path,
                            sha256=minimal_source.source_sha256,
                        ),
                    ),
                }
            ),
        }
    )
    delta = build_profile_diff(runtime_service.profile, candidate, diagnostic_only=True)
    runtime_service = RuntimeViewService(runtime_service.profile, candidate, delta)
    candidate_only = next(
        record for record in delta.records if record.candidate_total_seconds is not None
    )
    baseline_path = tmp_path / "baseline-profile.json"
    candidate_path = tmp_path / "candidate-profile.json"
    baseline_path.write_text(render_profile_json(runtime_service.profile, compact=True))
    candidate_path.write_text(render_profile_json(candidate, compact=True))
    root = source_root(tmp_path)
    shutil.copy2(
        Path(__file__).parent / "fixtures" / "excluded" / "visible.py",
        root / "visible.py",
    )
    server = create_server(
        ReportViewService(ordinary_report, minimal),
        source_root=root,
        profile_report=baseline_path,
        compare_profile=candidate_path,
        token=TOKEN,
    )
    with served(server):
        status, body = fetch(server, source_path(candidate_only.frame_identity, "candidate"))
    assert status == 200
    assert body["state"] == "bound"
    assert body["preview"]["expected_sha256"] == minimal_source.source_sha256


def test_source_disabled_without_configured_root(ordinary_report, tmp_path) -> None:
    server, delta = comparison_server(ordinary_report, tmp_path, source_root_path=None)
    identity = delta.records[0].frame_identity
    with served(server):
        status, body = fetch(server, source_path(identity, "baseline"))
    assert status == 200
    assert body["state"] == "disabled"
    assert body["preview"] is None


def test_symlink_escape_returns_unavailable_not_bound(ordinary_report, tmp_path) -> None:
    root = source_root(tmp_path)
    target = root / "outside.py"
    (root / "alpha" / "a.py").rename(target)
    (root / "alpha" / "a.py").symlink_to(target)
    server, delta = comparison_server(ordinary_report, tmp_path, source_root_path=root)
    identity = delta.records[0].frame_identity
    with served(server):
        status, body = fetch(server, source_path(identity, "baseline"))
    assert status == 200
    assert body["state"] == "unavailable"
    assert body["preview"] is None


def test_browser_row_selection_and_keyboard_source_preview(ordinary_report, tmp_path) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    original = RuntimeViewService.load(GOLDEN).profile
    frame = _project_frame(
        original,
        path="alpha/a.py",
        line=17,
        function="later_function",
        frame_id="profile-frame-aaaaaaaaaaaaaaaaaaaa",
    )
    server, delta = comparison_server(
        ordinary_report,
        tmp_path,
        source_root_path=source_root(tmp_path),
        baseline_frames=(frame,),
        candidate_frames=(frame,),
    )
    identity = delta.records[0].frame_identity
    with served(server), playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 900, "height": 700})
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}")
            toggle = page.locator(".lui-shell__menu")
            if toggle.is_visible():
                toggle.click()
            reveal_navigation(page, "Runtime").click()
            page.get_by_label("Runtime view", exact=True).select_option("comparison")
            page.get_by_role("button", name="Show runtime evidence").click()
            row = page.locator(f'[data-comparison-row="{identity}"]')
            row.wait_for()
            row.focus()
            page.keyboard.press("Enter")
            page.get_by_role("button", name="Baseline source", exact=True).focus()
            page.keyboard.press("Enter")
            preview = page.locator(".runtime-comparison-source-preview")
            page.wait_for_function(
                "element => element.textContent.includes('bound:')",
                arg=preview.element_handle(),
            )
            assert "alpha/a.py" in preview.inner_text()
            assert _source_hash(ordinary_report).sha256 in preview.inner_text()
            assert preview.locator("pre").inner_text().startswith("17  ")
            assert preview.locator("script").count() == 0
        finally:
            browser.close()


def test_held_comparison_source_response_is_discarded_after_selection_changes(
    ordinary_report,
    tmp_path,
) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    original = RuntimeViewService.load(GOLDEN).profile
    frames = tuple(
        _project_frame(
            original,
            path="alpha/a.py",
            line=number,
            function=f"function_{number}",
            frame_id=f"profile-frame-{number:020x}",
        )
        for number in (1, 17)
    )
    server, delta = comparison_server(
        ordinary_report,
        tmp_path,
        source_root_path=source_root(tmp_path),
        baseline_frames=frames,
        candidate_frames=frames,
    )
    records = delta.records
    assert len(records) == 2
    first = records[0].frame_identity
    second = records[1].frame_identity
    with served(server), playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}")
            page.evaluate("""() => {
                    const original = window.fetch;
                    window.fetch = async (...args) => {
                        const response = await original(...args);
                        if (String(args[0]).includes('/api/runtime/comparison-source')) {
                            const body = await response.json();
                            response.json = () => new Promise(resolve => {
                                window.releaseComparisonSource = () => resolve(body);
                                document.body.dataset.comparisonSourceReady = 'true';
                            });
                        }
                        return response;
                    };
                }""")
            reveal_navigation(page, "Runtime").click()
            page.get_by_label("Runtime view", exact=True).select_option("comparison")
            page.get_by_role("button", name="Show runtime evidence").click()
            page.locator(f'[data-comparison-row="{first}"]').click()
            page.get_by_role("button", name="Baseline source", exact=True).click()
            playwright.expect(page.locator("body")).to_have_attribute(
                "data-comparison-source-ready", "true"
            )
            page.locator(f'[data-comparison-row="{second}"]').click()
            page.evaluate("window.releaseComparisonSource()")
            page.evaluate(
                "() => new Promise(resolve => "
                "requestAnimationFrame(() => requestAnimationFrame(resolve)))"
            )
            preview = page.locator(".runtime-comparison-source-preview").inner_text()
            assert "bound:" not in preview
            assert f"Selected frame identity: {second}" in preview
        finally:
            browser.close()


def _open_runtime_comparison_source_preview(page, identity: str) -> None:
    toggle = page.locator(".lui-shell__menu")
    if toggle.is_visible() and toggle.get_attribute("aria-expanded") == "false":
        toggle.click()
    reveal_navigation(page, "Runtime").click()
    page.get_by_label("Runtime view", exact=True).select_option("comparison")
    page.get_by_role("button", name="Show runtime evidence").click()
    page.locator(f'[data-comparison-row="{identity}"]').click()
    page.get_by_role("button", name="Candidate source", exact=True).click()
    preview = page.locator(".runtime-comparison-source-preview")
    page.wait_for_function(
        "element => element.textContent.includes('bound:')",
        arg=preview.element_handle(),
    )


@pytest.mark.parametrize(
    ("viewport_name", "width", "height"),
    (
        ("desktop", 1280, 900),
        ("tablet", 768, 1024),
        ("mobile", 390, 844),
    ),
)
def test_comparison_source_preview_keeps_document_width_contained(
    tmp_path,
    viewport_name: str,
    width: int,
    height: int,
) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    server, delta, logical_path, source_sha = comparison_server_with_long_source_path(tmp_path)
    identity = delta.records[0].frame_identity
    with served(server), playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": width, "height": height})
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}")
            _open_runtime_comparison_source_preview(page, identity)
            page.evaluate(WAIT_TWO_ANIMATION_FRAMES)
            page.evaluate("() => window.scrollTo(0, 0)")
            page.evaluate(WAIT_TWO_ANIMATION_FRAMES)
            metrics = page.evaluate(OVERFLOW_METRICS)
            assert page.evaluate(
                "() => [document.documentElement, document.body].every("
                "node => !['hidden', 'clip'].includes(getComputedStyle(node).overflowX))"
            ), "document clipping must not disguise inaccessible content"
            if metrics["docScroll"] > metrics["docClient"]:
                raise AssertionError(
                    "document overflow: " f"{json.dumps(metrics, indent=2, sort_keys=True)}"
                )
            preview = page.locator(".runtime-comparison-source-preview")
            preview_text = preview.inner_text()
            assert logical_path in preview_text
            assert source_sha in preview_text
            assert page.locator("#runtime-results .downloads button").count() >= 4
            table_wrap = page.locator("#runtime-results .table-wrap")
            playwright.expect(table_wrap).to_be_visible()
            if width <= 768:
                assert table_wrap.evaluate("node => node.scrollWidth > node.clientWidth")
                assert table_wrap.evaluate(
                    "node => { node.scrollLeft = node.scrollWidth; return node.scrollLeft > 0; }"
                ), "wide comparison columns must remain reachable"
            assert not errors
            OUTPUT.mkdir(parents=True, exist_ok=True)
            page.screenshot(
                path=str(
                    OUTPUT / f"20260922-runtime-comparison-source-{viewport_name}-{width}.png"
                ),
                full_page=True,
            )
        finally:
            browser.close()
