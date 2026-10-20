"""Independent qualification of the exact bundled LocalOps editor artifact."""

from pathlib import Path

import pytest
from browser_navigation import reveal_navigation
from test_graph_handoff_browser import served

from code_analysis.config import AnalysisConfig
from code_analysis.project import discover_context
from code_analysis.service import scan
from code_analysis.web_server import create_server
from code_analysis.web_service import ReportViewService


@pytest.fixture
def draft_source(tmp_path):
    source = tmp_path / "draft.py"
    source.write_bytes(b'def original():\r\n    return "original"\r\n')
    report = scan(discover_context(tmp_path, AnalysisConfig(test_roots=())))
    return source, ReportViewService(report)


@pytest.mark.parametrize("engine", ("chromium", "webkit"))
@pytest.mark.parametrize("width,height", ((1440, 900), (820, 1024), (390, 844)))
@pytest.mark.parametrize("theme", ("light", "dark"))
def test_installed_editor_layout_and_native_keyboard(ordinary_report, engine, width, height, theme):
    playwright = pytest.importorskip("playwright.sync_api")
    with (
        served(create_server(ReportViewService(ordinary_report), token="editor-test")) as server,
        playwright.sync_playwright() as runtime,
    ):
        provider = getattr(runtime, engine)
        if not Path(provider.executable_path).is_file():
            pytest.skip(f"{engine} binary unavailable; no automatic installation")
        browser = provider.launch()
        try:
            page = browser.new_page(viewport={"width": width, "height": height}, color_scheme=theme)
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token=editor-test")
            page.locator(".cards").wait_for()
            page.evaluate("""async () => {
                  const {CodeEditor} = await import('/localops/index.js');
                  const host = document.querySelector('#content');
                  host.replaceChildren(); host.style.height = '70vh';
                  const c = new CodeEditor({id:'adoption-editor', label:'Session draft'});
                  host.append(c.root); c.root.style.height = '100%';
                  c.setDocument({id:'original',revision:1,
                    text:'😀 original\\nsecond\\n',readOnly:false});
                  window.adoptedEditor = c;
                }""")
            editor = page.get_by_role("textbox", name="Session draft", exact=True)
            editor.focus()
            editor.press("End")
            editor.press_sequentially(" <script>literal</script>")
            assert "<script>literal</script>" in editor.input_value()
            editor.press("Tab")
            assert not editor.evaluate("node => node === document.activeElement")
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            assert page.evaluate("adoptedEditor.getSnapshot().dirty && !window.pwned")
            page.evaluate("adoptedEditor.destroy()")
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ("chromium", "webkit"))
def test_editor_bound_hydration_transactions_composition_and_detachment(ordinary_report, engine):
    playwright = pytest.importorskip("playwright.sync_api")
    with (
        served(create_server(ReportViewService(ordinary_report), token="editor-test")) as server,
        playwright.sync_playwright() as runtime,
    ):
        provider = getattr(runtime, engine)
        if not Path(provider.executable_path).is_file():
            pytest.skip(f"{engine} binary unavailable; no automatic installation")
        browser = provider.launch()
        try:
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token=editor-test")
            result = page.evaluate(r"""async () => {
                  const {CodeEditor} = await import('/localops/index.js');
                  const c = new CodeEditor({id:'test-editor', label:'Session draft'});
                  document.querySelector('#content').replaceChildren(c.root);
                  c.setDocument({id:'original',revision:1,text:'😀 one\none',readOnly:false});
                  c.setDraft({documentId:'original',revision:1,text:'😀 draft\none',
                    selection:{start:3,end:8}});
                  const detached = c.getSnapshot(); detached.text = 'unrelated';
                  const captured = c.getSnapshot(); let rejected = 0;
                  for (const operation of [
                    () => c.setDocument({id:'original',revision:1,text:'x',readOnly:false}),
                    () => c.setDocument({id:'different',revision:1,text:'x',readOnly:false}),
                    () => c.setSelection({start:1,end:2}),
                    () => c.setDraft({documentId:'different',revision:1,text:'x',
                      selection:{start:0,end:0}}),
                    () => c.setDocument({id:'different',revision:true,text:'x',readOnly:false}),
                    () => c.setDocument({id:'different',revision:1,text:'x\r\n',readOnly:false})
                  ]) { try { operation(); } catch { rejected++; } }
                  const preserved = c.getSnapshot().text === captured.text;
                  c.setDocument({id:'original',revision:2,text:'😀 one\none',readOnly:false});
                  const passive = c.getSnapshot().text === captured.text;
                  c.replaceAll('one', '$&'); const replaced = c.getSnapshot().text;
                  c.undo(); const undone = c.getSnapshot().text;
                  c.redo(); const redone = c.getSnapshot().text;
                  c.setDocument({id:'ime',revision:1,text:'',readOnly:false},{discardChanges:true});
                  c.input.dispatchEvent(new CompositionEvent('compositionstart'));
                  c.input.value = 'に'; c.input.dispatchEvent(new InputEvent('input'));
                  c.input.value = '日本😀'; c.input.dispatchEvent(new InputEvent('input'));
                  c.input.dispatchEvent(new CompositionEvent('compositionend'));
                  const transactions = c.getSnapshot().history.undo;
                  c.undo(); const compositionUndo = c.getSnapshot().text;
                  c.destroy(); let destroyed = false;
                  try { c.getSnapshot(); } catch { destroyed = true; }
                  return {rejected,preserved,passive,replaced,undone,redone,
                    transactions,compositionUndo,destroyed};
                }""")
            assert result["rejected"] == 6
            assert result["preserved"] and result["passive"] and result["destroyed"]
            assert result["replaced"] == result["redone"] == "😀 draft\n$&"
            assert result["undone"] == "😀 draft\none"
            assert result["transactions"] == 1 and result["compositionUndo"] == ""
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ("chromium", "webkit"))
@pytest.mark.parametrize("width,height", ((1440, 900), (820, 1024), (390, 844)))
@pytest.mark.parametrize("theme", ("light", "dark"))
def test_application_drafts_are_session_only_and_keep_original_authority(
    draft_source, monkeypatch, engine, width, height, theme
):
    playwright = pytest.importorskip("playwright.sync_api")
    source, service = draft_source
    original = source.read_bytes()
    report_bytes = service.primary.model_dump_json()

    def forbidden_scan(*args, **kwargs):
        pytest.fail("source browsing and drafts must not rescan")

    monkeypatch.setattr("code_analysis.service.scan", forbidden_scan)
    with (
        served(create_server(service, source_root=source.parent, token="editor-test")) as server,
        playwright.sync_playwright() as runtime,
    ):
        provider = getattr(runtime, engine)
        if not Path(provider.executable_path).is_file():
            pytest.skip(f"{engine} binary unavailable; no automatic installation")
        browser = provider.launch()
        try:
            page = browser.new_page(viewport={"width": width, "height": height}, color_scheme=theme)
            errors, requests = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: requests.append((request.method, request.url)))
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token=editor-test#browse")
            file = page.locator('.code-browser-list [data-browse-identity^="source-file-"]')
            file.click()
            page.get_by_role("button", name="Edit draft from verified original source").click()
            editor = page.get_by_role("textbox", name="Draft for draft.py", exact=True)
            playwright.expect(editor).to_have_value('def original():\n    return "original"\n')
            editor.fill('def original():\n    return "<script>literal 😀</script>"\n')
            playwright.expect(page.locator(".code-drafts-open")).to_contain_text("dirty")
            playwright.expect(page.locator(".code-browser-source")).to_contain_text(
                "Draft—not analyzed"
            )
            assert page.locator('.code-browser-line[data-selected="true"]').count() == 0
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

            screenshots = Path("output/playwright")
            screenshots.mkdir(parents=True, exist_ok=True)
            page.screenshot(
                path=str(screenshots / f"20261011-code-draft-{engine}-{width}-{theme}.png")
            )
            with page.expect_download() as captured:
                page.get_by_role("button", name="Export", exact=True).click()
            assert Path(captured.value.path()).read_bytes() == (
                'def original():\r\n    return "<script>literal 😀</script>"\r\n'.encode()
            )
            page.get_by_role("button", name="Edit draft from verified original source").click()
            assert "literal 😀" in editor.input_value()
            # Native composition guards prevent teardown by a view change.
            editor.dispatch_event("compositionstart")
            nav = page.locator('[data-view="overview"]')
            if not nav.is_visible():
                page.get_by_role("button", name="Toggle navigation").click()
            nav.click()
            assert editor.is_visible()
            editor.dispatch_event("compositionend")
            nav.click()
            page.locator(".cards").wait_for()
            browse = reveal_navigation(page, "Browse code")
            if not browse.is_visible():
                page.get_by_role("button", name="Toggle navigation").click()
            browse.click()
            page.get_by_role("button", name="Verified original source", exact=True).click()
            page.get_by_role("button", name="Open draft for draft.py").click()
            assert (
                "literal 😀" in page.get_by_role("textbox", name="Draft for draft.py").input_value()
            )
            assert page.evaluate("!history.state?.codeAnalysisBrowse?.text")
            assert page.evaluate("localStorage.length === 0 && sessionStorage.length === 0")
            assert all(method == "GET" for method, _ in requests)
            assert source.read_bytes() == original and not errors
            assert service.primary.model_dump_json() == report_bytes
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ("chromium", "webkit"))
def test_cached_draft_survives_revalidation_and_changed_original(draft_source, engine):
    playwright = pytest.importorskip("playwright.sync_api")
    source, service = draft_source
    with (
        served(create_server(service, source_root=source.parent, token="editor-test")) as server,
        playwright.sync_playwright() as runtime,
    ):
        provider = getattr(runtime, engine)
        if not Path(provider.executable_path).is_file():
            pytest.skip(f"{engine} binary unavailable; no automatic installation")
        browser = provider.launch()
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token=editor-test#browse")
            page.locator('.code-browser-list [data-browse-identity^="source-file-"]').click()
            page.get_by_role("button", name="Edit draft from verified original source").click()
            editor = page.get_by_role("textbox", name="Draft for draft.py", exact=True)
            editor.fill("retained draft 😀\n")
            page.locator('[data-view="overview"]').click()
            reveal_navigation(page, "Browse code").click()
            page.get_by_role("button", name="Open draft for draft.py").click()
            page.on("dialog", lambda dialog: dialog.accept())
            page.get_by_role("button", name="Edit draft from verified original source").click()
            playwright.expect(editor).to_have_value("retained draft 😀\n")
            page.locator('[data-view="overview"]').click()
            reveal_navigation(page, "Browse code").click()
            page.get_by_role("button", name="Open draft for draft.py").click()
            playwright.expect(editor).to_have_value("retained draft 😀\n")
            page.locator('[data-view="overview"]').click()
            source.write_bytes(b"changed_externally = True\r\n")
            reveal_navigation(page, "Browse code").click()
            playwright.expect(page.locator(".code-browser-source")).to_contain_text("mismatch")
            playwright.expect(
                page.get_by_role("button", name="Edit draft from verified original source")
            ).to_be_disabled()
            page.get_by_role("button", name="Open draft for draft.py").click()
            playwright.expect(editor).to_have_value("retained draft 😀\n")
            with page.expect_download() as captured:
                page.get_by_role("button", name="Export", exact=True).click()
            assert Path(captured.value.path()).read_bytes() == "retained draft 😀\r\n".encode()
            assert source.read_bytes() == b"changed_externally = True\r\n"
        finally:
            browser.close()
