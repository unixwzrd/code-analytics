"""Explicit source-root permission and current project documents stay separate from reports."""

from __future__ import annotations

import errno
import hashlib
import json
from http.client import HTTPConnection
from urllib.parse import urlencode

import pytest
from browser_navigation import reveal_navigation
from pydantic import ValidationError
from test_graph_handoff_browser import served

from code_analysis.collection_commands import CollectionPlanner
from code_analysis.config import AnalysisConfig
from code_analysis.project import discover_context
from code_analysis.project_browsing import (
    ProjectDocumentAccess,
    ProjectDocumentCatalog,
    ProjectDocumentLink,
    SourceConnectionRequest,
    document_catalog,
    read_project_document,
    report_identity,
    validate_source_connection,
)
from code_analysis.reports.json_report import canonical_json
from code_analysis.service import scan
from code_analysis.ui_source import preview_source
from code_analysis.web_server import create_server
from code_analysis.web_service import ReportViewService

TOKEN = "project-source-test"


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "projects"
    folder = root / "hotel"
    (folder / "src").mkdir(parents=True)
    (folder / "src/reception.py").write_text("def book():\n    return 42\n")
    (folder / "README.md").write_text(
        "# Hotel investigation\nA report from Sybil.\n[Walkthrough](walkthrough.md)\n"
        "[Hints](HINTS.md)\n[Solutions](SOLUTIONS.md)\n"
        "[Unsafe](javascript:alert(1))\n[Outside](../outside.md)\n"
        "[Private](private/notes.md)\n<script>window.projectDocumentExecuted=true</script>\n"
    )
    (folder / "walkthrough.md").write_text("# Investigate\n[Back](README.md)\n")
    (folder / "HINTS.md").write_text("# Optional hints\n")
    (folder / "SOLUTIONS.md").write_text("# Solutions\n")
    (folder / "private").mkdir()
    (folder / "private/notes.md").write_text("restricted")
    (root / "outside.md").write_text("outside source root")
    report = scan(discover_context(folder, AnalysisConfig(test_roots=())))
    return root, folder, report


def http(server, method, route, payload=None, *, token=TOKEN, origin=True):
    connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    headers = {"X-Code-Analysis-Token": token}
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload)
    if origin:
        headers["Origin"] = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        connection.request(method, route, body=body, headers=headers)
        result = connection.getresponse()
        return result.status, json.loads(result.read())
    finally:
        connection.close()


def get_project(server, route, digest, **selectors):
    return http(server, "GET", route + "?" + urlencode({"report_sha256": digest, **selectors}))


def test_connect_source_without_execution_or_rescan(project, monkeypatch):
    root, folder, report = project
    before = canonical_json(report)
    digest = report_identity(report)
    monkeypatch.setattr("code_analysis.service.scan", lambda *a, **kw: pytest.fail("rescan"))
    with served(
        create_server(ReportViewService(report), collection_root=root, token=TOKEN)
    ) as server:
        assert server.app.collection_session is None
        status, data = get_project(server, "/api/project/source", digest)
        assert status == 200 and data["can_connect"] and not data["configured"]
        status, data = http(
            server,
            "POST",
            "/api/project/source",
            {
                "report_sha256": digest,
                "directory": "hotel",
                "confirmed": True,
                "logical_path": "src/reception.py",
            },
        )
        assert status == 200 and data["configured"] and data["source_root"] == str(folder)
        result = preview_source(report, server.app.source_root, "src/reception.py")
        assert result.state == "bound" and result.lines[0].text == "def book():"
        assert server.app.collection_session is None
        assert not (folder / ".code-analysis").exists()
        assert canonical_json(report) == before


@pytest.mark.parametrize("token,origin", [("wrong", True), (TOKEN, False)])
def test_source_connection_authentication_and_origin(project, token, origin):
    root, _, report = project
    with served(
        create_server(ReportViewService(report), collection_root=root, token=TOKEN)
    ) as server:
        status, _ = http(
            server,
            "POST",
            "/api/project/source",
            {
                "report_sha256": report_identity(report),
                "directory": "hotel",
                "confirmed": True,
            },
            token=token,
            origin=origin,
        )
        assert status == 403 and server.app.source_root is None


@pytest.mark.parametrize(
    "changes",
    [
        {"confirmed": False},
        {"confirmed": 1},
        {"directory": "hotel/src"},
        {"directory": "../hotel"},
        {"directory": "/tmp"},
        {"report_sha256": "0" * 64},
        {"unknown": True},
        {"run": "missing"},
        {"logical_path": "unrecorded.py"},
    ],
)
def test_rejected_source_connection_does_not_mutate_session(project, changes):
    root, _, report = project
    with served(
        create_server(ReportViewService(report), collection_root=root, token=TOKEN)
    ) as server:
        status, data = http(
            server,
            "POST",
            "/api/project/source",
            {
                "report_sha256": report_identity(report),
                "directory": "hotel",
                "confirmed": True,
                **changes,
            },
        )
        assert status >= 400 and "error" in data
        assert server.app.source_root is None


def test_mismatched_source_and_symlink_directory_are_rejected(project):
    root, folder, report = project
    planner = CollectionPlanner(root)
    try:
        (root / "linked").symlink_to(folder, target_is_directory=True)
        with pytest.raises(OSError):
            validate_source_connection(
                report,
                planner,
                SourceConnectionRequest(
                    report_sha256=report_identity(report),
                    directory="linked",
                    confirmed=True,
                ),
            )
        (folder / "src/reception.py").write_text("changed = True\n")
        with pytest.raises(OSError) as error:
            validate_source_connection(
                report,
                planner,
                SourceConnectionRequest(
                    report_sha256=report_identity(report),
                    directory="hotel",
                    confirmed=True,
                ),
            )
        assert error.value.errno == errno.ENODATA
    finally:
        planner.close()


def test_preview_refuses_replaced_root_ancestor(project):
    root, folder, report = project
    (root / "alias").symlink_to(folder, target_is_directory=True)
    result = preview_source(report, root / "alias/src/..", "src/reception.py")
    assert result.state == "unavailable" and not result.lines


def test_project_readme_and_links_are_current_text_not_report_evidence(project):
    _, folder, report = project
    before = canonical_json(report)
    catalog = document_catalog(report, folder, "src/reception.py")
    assert [record.path for record in catalog.records] == ["README.md"]
    selected = catalog.records[0]
    document = read_project_document(
        report, folder, catalog.root_identity, selected.path, selected.sha256
    )
    assert "report from Sybil" in document.text
    assert "<script>" in document.text
    assert {link.path for link in document.links} == {"walkthrough.md", "HINTS.md", "SOLUTIONS.md"}
    assert any("spoilers" in link.label for link in document.links)
    assert canonical_json(report) == before


def test_project_document_routes_bind_report_root_and_hash(project):
    root, folder, report = project
    digest = report_identity(report)
    with served(
        create_server(
            ReportViewService(report), source_root=folder, collection_root=root, token=TOKEN
        )
    ) as server:
        status, catalog = get_project(
            server, "/api/project/documents", digest, path="src/reception.py"
        )
        assert status == 200
        record = catalog["records"][0]
        selectors = {
            "root_identity": catalog["root_identity"],
            "path": record["path"],
            "sha256": record["sha256"],
        }
        status, document = get_project(server, "/api/project/document", digest, **selectors)
        assert status == 200 and "Hotel investigation" in document["text"]
        status, _ = get_project(server, "/api/project/document", "0" * 64, **selectors)
        assert status == 400
        status, _ = get_project(
            server, "/api/project/document", digest, **{**selectors, "root_identity": "0" * 64}
        )
        assert status == 400 and "error" in _ and "text" not in _
        (folder / "README.md").write_text("changed")
        status, data = get_project(server, "/api/project/document", digest, **selectors)
        assert status == 400 and "text" not in data


def test_document_permissions_follow_offered_links_and_clear_on_reconnect(project):
    root, folder, report = project
    digest = report_identity(report)
    (folder / "unlinked.md").write_text("not offered by a README or a followed link")
    with served(
        create_server(
            ReportViewService(report), source_root=folder, collection_root=root, token=TOKEN
        )
    ) as server:
        _, catalog = get_project(server, "/api/project/documents", digest, path="src/reception.py")
        selectors = {"root_identity": catalog["root_identity"]}
        for name in ("unlinked.md", "walkthrough.md"):
            status, result = get_project(
                server,
                "/api/project/document",
                digest,
                **selectors,
                path=name,
                sha256=hashlib.sha256((folder / name).read_bytes()).hexdigest(),
            )
            assert status == 400 and "error" in result and "text" not in result
        readme = catalog["records"][0]
        _, document = get_project(
            server,
            "/api/project/document",
            digest,
            **selectors,
            path=readme["path"],
            sha256=readme["sha256"],
        )
        link = next(item for item in document["links"] if item["path"] == "walkthrough.md")
        status, result = get_project(
            server,
            "/api/project/document",
            digest,
            **selectors,
            path=link["path"],
            sha256=link["sha256"],
        )
        assert status == 200 and "Investigate" in result["text"]
        status, _ = http(
            server,
            "POST",
            "/api/project/source",
            {
                "report_sha256": digest,
                "directory": "hotel",
                "confirmed": True,
            },
        )
        assert status == 200
        status, result = get_project(
            server,
            "/api/project/document",
            digest,
            **selectors,
            path=link["path"],
            sha256=link["sha256"],
        )
        assert status == 400 and "error" in result and "text" not in result


def test_document_grants_are_bounded_and_bound_to_every_identity():
    access = ProjectDocumentAccess()
    for index in range(501):
        link = ProjectDocumentLink(path=f"file-{index}.md", label="file", sha256="a" * 64, bytes=0)
        access.allow("b" * 64, "c" * 64, (link,))
    with pytest.raises(OSError):
        access.require("b" * 64, "c" * 64, "file-0.md", "a" * 64)
    access.require("b" * 64, "c" * 64, "file-500.md", "a" * 64)
    for arguments in (
        ("d" * 64, "c" * 64, "file-500.md", "a" * 64),
        ("b" * 64, "d" * 64, "file-500.md", "a" * 64),
        ("b" * 64, "c" * 64, "file-500.md", "d" * 64),
    ):
        with pytest.raises(OSError):
            access.require(*arguments)


def test_document_reader_rejects_symlink_even_after_offering(project):
    root, folder, report = project
    catalog = document_catalog(report, folder, "src/reception.py")
    readme = catalog.records[0]
    (folder / "README.md").unlink()
    (folder / "README.md").symlink_to(root / "outside.md")
    with pytest.raises(OSError):
        read_project_document(report, folder, catalog.root_identity, readme.path, readme.sha256)


@pytest.mark.parametrize(
    "path",
    ["../outside.md", "/tmp/file.md", "private/notes.md", ".env", "src/reception.py", "image.svg"],
)
def test_project_document_paths_fail_closed(project, path):
    _, folder, report = project
    catalog = document_catalog(report, folder, "src/reception.py")
    with pytest.raises((ValueError, OSError)):
        read_project_document(report, folder, catalog.root_identity, path, "0" * 64)


def test_source_disabled_documents_do_not_become_empty_available(project):
    _, _, report = project
    result = document_catalog(report, None, "src/reception.py")
    assert result.state == "disabled" and result.limitations and not result.records


@pytest.mark.parametrize("content", [b"\xff", b"x" * (128 * 1024 + 1)])
def test_unreadable_document_data_is_not_disclosed(project, content):
    _, folder, report = project
    (folder / "README.md").write_bytes(content)
    with pytest.raises((UnicodeError, OSError)):
        read_project_document(
            report,
            folder,
            document_catalog(report, folder, "").root_identity,
            "README.md",
            hashlib.sha256(content).hexdigest(),
        )


def test_disabled_document_model_rejects_contradictory_state(project):
    _, folder, report = project
    result = document_catalog(report, folder, "src/reception.py")
    with pytest.raises(ValidationError):
        ProjectDocumentCatalog.model_validate({**result.model_dump(), "state": "disabled"})


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_source_connection_waits_for_permission_capability(project, engine):
    playwright = pytest.importorskip("playwright.sync_api")
    root, _, report = project
    with (
        served(
            create_server(ReportViewService(report), collection_root=root, token=TOKEN)
        ) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser = getattr(runtime, engine).launch()
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            pending = []
            page.route("**/api/project/source?*", lambda route: pending.append(route))
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}")
            page.locator(".cards").wait_for()
            reveal_navigation(page, "Browse code").click()
            control = page.get_by_role("button", name="Connect source folder", exact=True)
            playwright.expect(control).to_be_disabled()
            for _ in range(100):
                if pending:
                    break
                page.wait_for_timeout(10)
            assert pending, "permission request was not issued"
            for route in pending:
                route.continue_()
            playwright.expect(control).to_be_enabled()
            control.click()
            playwright.expect(
                page.get_by_role("dialog", name="Connect source folder", exact=True)
            ).to_be_visible()
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("width", [1440, 1024])
def test_leaf_offers_source_setup_in_place_and_keeps_selection(project, monkeypatch, engine, width):
    playwright = pytest.importorskip("playwright.sync_api")
    root, folder, report = project
    original = canonical_json(report)
    source = (folder / "src/reception.py").read_bytes()
    monkeypatch.setattr("code_analysis.service.scan", lambda *a, **kw: pytest.fail("rescan"))
    with (
        served(
            create_server(ReportViewService(report), collection_root=root, token=TOKEN)
        ) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser = getattr(runtime, engine).launch()
        try:
            page = browser.new_page(viewport={"width": width, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}")
            page.locator(".cards").wait_for()
            reveal_navigation(page, "Browse code").click()
            page.locator('.code-browser-list [data-browse-identity^="source-file-"]').click()
            pane = page.locator(".code-browser-source")
            connect = pane.get_by_role(
                "button", name="Connect source folder to show code", exact=True
            )
            playwright.expect(connect).to_be_visible()
            page.get_by_role("button", name="Recorded relationships", exact=True).click()
            page.locator(".code-browser-map").get_by_role(
                "button", name="Inspect book", exact=True
            ).click()
            setup = page.get_by_role("dialog", name="Connect source folder", exact=True)
            playwright.expect(setup).to_be_visible()
            assert server.app.source_root is None
            playwright.expect(pane.locator("h3")).to_have_text("book")
            setup.get_by_role("button", name="Close", exact=True).click()
            playwright.expect(connect).to_be_focused()
            # Cancellation keeps saved navigation and never grants source access.
            page.get_by_role("button", name="Recorded relationships", exact=True).click()
            page.locator(".code-browser-map").get_by_role(
                "button", name="Inspect book", exact=True
            ).click()
            playwright.expect(setup).not_to_be_visible()
            connect.click()
            playwright.expect(setup).to_be_visible()
            directory = setup.get_by_role(
                "textbox", name="Project source folder (relative to collection root)"
            )
            consent = setup.get_by_role(
                "checkbox",
                name="Allow read-only source and related text documents from this folder.",
            )
            directory.fill("hotel/src")
            consent.check()
            setup.get_by_role("button", name="Connect and display source", exact=True).click()
            playwright.expect(setup.get_by_role("alert")).not_to_be_empty()
            assert server.app.source_root is None
            directory.fill("hotel")
            assert not consent.is_checked()
            consent.check()
            setup.get_by_role("button", name="Connect and display source", exact=True).click()
            playwright.expect(setup).not_to_be_visible()
            playwright.expect(pane.locator("h3")).to_have_text("book")
            playwright.expect(pane.locator(".code-browser-lines")).to_contain_text("return 42")
            playwright.expect(
                page.get_by_role("heading", name="Verified original source", exact=True)
            ).to_be_focused()
            assert server.app.source_root == folder and not errors
            assert canonical_json(report) == original
            assert (folder / "src/reception.py").read_bytes() == source
            assert not (folder / ".code-analysis").exists()
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_leaving_view_during_source_offer_cannot_open_stale_dialog(project, engine):
    playwright = pytest.importorskip("playwright.sync_api")
    root, _, report = project
    with (
        served(
            create_server(ReportViewService(report), collection_root=root, token=TOKEN)
        ) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser = getattr(runtime, engine).launch()
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}")
            page.locator(".cards").wait_for()
            reveal_navigation(page, "Browse code").click()
            page.locator('.code-browser-list [data-browse-identity^="source-file-"]').click()
            graph = page.locator(".code-browser-map")
            graph.get_by_role("button", name="Inspect book", exact=True).wait_for()
            held = []
            page.route("**/api/project/source?*", lambda route: held.append(route))
            graph.get_by_role("button", name="Inspect book", exact=True).click()
            for _ in range(100):
                if held:
                    break
                page.wait_for_timeout(10)
            assert held
            reveal_navigation(page, "Overview").click()
            for route in held:
                route.fulfill(content_type="application/json", body="{}")
            page.wait_for_timeout(100)
            assert page.locator("#project-source-connection[open]").count() == 0
            assert server.app.source_root is None
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("width", [1440, 1024])
def test_connect_folder_browse_graph_and_read_tutorial_without_rescan(
    project, monkeypatch, engine, width
):
    playwright = pytest.importorskip("playwright.sync_api")
    root, folder, report = project
    original = canonical_json(report)
    original_source = (folder / "src/reception.py").read_bytes()
    monkeypatch.setattr("code_analysis.service.scan", lambda *a, **kw: pytest.fail("source rescan"))
    with (
        served(
            create_server(ReportViewService(report), collection_root=root, token=TOKEN)
        ) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser = getattr(runtime, engine).launch()
        try:
            page = browser.new_page(viewport={"width": width, "height": 1000})
            errors = []
            page.on("pageerror", lambda failure: errors.append(str(failure)))
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}")
            page.locator(".cards").wait_for()
            reveal_navigation(page, "Browse code").click()
            page.locator('.code-browser-list [data-browse-identity^="source-file-"]').click()
            playwright.expect(page.locator(".code-browser-source")).to_contain_text(
                "Source files are not connected"
            )
            page.get_by_role("button", name="Connect source folder", exact=True).click()
            setup = page.get_by_role("dialog", name="Connect source folder", exact=True)
            setup.get_by_role("button", name="Browse source folders", exact=True).click()
            picker = page.locator("#collection-browser")
            picker.locator('[data-path="hotel"]').dblclick()
            playwright.expect(picker.locator("#browser-path")).to_contain_text(str(folder))
            picker.get_by_role("button", name="Up", exact=True).click()
            playwright.expect(picker.locator("#browser-path")).to_have_text(
                f"Current directory: {root}"
            )
            picker.locator('[data-path="hotel"]').dblclick()
            picker.get_by_role("button", name="Choose this directory", exact=True).click()
            consent = setup.get_by_role(
                "checkbox",
                name="Allow read-only source and related text documents from this folder.",
            )
            assert not consent.is_checked()
            setup.get_by_role("button", name="Connect and display source", exact=True).click()
            playwright.expect(setup.get_by_role("alert")).to_contain_text(
                "Confirm read-only access"
            )
            consent.check()
            setup.get_by_role("button", name="Connect and display source", exact=True).click()
            playwright.expect(setup).not_to_be_visible()
            playwright.expect(page.locator(".code-browser-lines")).to_contain_text("return 42")
            page.get_by_role("button", name="Recorded relationships", exact=True).click()
            graph = page.locator(".code-browser-map")
            graph.get_by_role("button", name="Inspect book", exact=True).click()
            playwright.expect(page.locator(".code-browser-source h3")).to_have_text("book")
            assert (
                graph.get_by_role("button", name="Browse recorded source", exact=True).count() == 0
            )
            readme = page.get_by_role("button", name="Read README.md", exact=True)
            readme.click()
            reader = page.get_by_role("dialog", name="Project document — read only", exact=True)
            playwright.expect(reader.locator(".project-document-text")).to_contain_text(
                "report from Sybil"
            )
            assert reader.locator("script").count() == 0
            assert page.evaluate("window.projectDocumentExecuted") is None
            reader.get_by_role("button", name="Open walkthrough.md", exact=True).click()
            playwright.expect(reader.locator(".project-document-text")).to_contain_text(
                "# Investigate"
            )
            reader.get_by_role("button", name="Previous project document", exact=True).click()
            playwright.expect(reader.locator(".project-document-text")).to_contain_text(
                "Hotel investigation"
            )
            assert (
                reader.get_by_role(
                    "button", name="Open SOLUTIONS.md — solutions (spoilers)", exact=True
                ).count()
                == 1
            )
            page.keyboard.press("Escape")
            playwright.expect(reader).not_to_be_visible()
            playwright.expect(readme).to_be_focused()
            playwright.expect(page.locator(".code-browser-source h3")).to_have_text("book")
            assert not errors
            assert server.app.collection_session is None
            assert canonical_json(report) == original
            assert (folder / "src/reception.py").read_bytes() == original_source
            assert not (folder / ".code-analysis").exists()
        finally:
            browser.close()
