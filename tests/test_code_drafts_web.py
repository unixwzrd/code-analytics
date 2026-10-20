"""Session draft consumer module: model boundaries and browser qualification."""

from __future__ import annotations

import hashlib
import json
from http.client import HTTPConnection
from pathlib import Path

import pytest
from test_code_browser_web import TOKEN, get
from test_code_browser_web import browser_source as _browser_source
from test_graph_handoff_browser import served

from code_analysis.config import AnalysisConfig
from code_analysis.project import discover_context
from code_analysis.service import scan
from code_analysis.web_server import create_server
from code_analysis.web_service import ReportViewService

browser_source = _browser_source

DOCUMENT_ROUTE = "/api/code-browser/document"


def _launch_browser(runtime, engine: str):
    browser_type = getattr(runtime, engine)
    executable = Path(browser_type.executable_path)
    if not executable.is_file():
        pytest.skip(f"{engine} binary is unavailable; no automatic installation")
    return browser_type.launch()


@pytest.fixture
def draft_assets_available():
    static = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "code_analysis"
        / "web_static"
        / "code_drafts.js"
    )
    if not static.is_file():
        pytest.fail("code_drafts.js must be served from bundled web_static")


@pytest.mark.parametrize("engine", ("chromium", "webkit"))
def test_draft_session_capacity_dirty_and_no_storage(
    browser_source, draft_assets_available, engine
):
    playwright = pytest.importorskip("playwright.sync_api")
    _, service = browser_source
    with (
        served(create_server(service, token=TOKEN)) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser = _launch_browser(runtime, engine)
        try:
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}")
            page.locator(".cards").wait_for()
            result = page.evaluate("""async () => {
                  const {DraftSession} = await import('/code_drafts.js');
                  const session = new DraftSession();
                  const confirmCalls = [];
                  const original = globalThis.confirm;
                  globalThis.confirm = message => { confirmCalls.push(message); return false; };
                  try {
                    for (let index = 0; index < 8; index += 1) {
                      session.remember({
                        key: `k${index}`,
                        logicalPath: `file${index}.py`,
                        sourceSha256: `${index}`.padStart(64, 'a'),
                        dirty: false,
                      });
                    }
                    let threw = false;
                    try { session.remember({key: 'overflow', logicalPath: 'x.py',
                      sourceSha256: 'b'.repeat(64)}); }
                    catch (error) { threw = error.message.includes('full'); }
                    session.get('k0').dirty = true;
                    const allowed = session.allowSwitch('switch?');
                    const cleared = session.discardAll({confirmed: true});
                    const storageKeys = Object.keys(localStorage)
                      .concat(Object.keys(sessionStorage));
                    const historyDraft = history.state?.draft_text;
                    return {
                      sizeBeforeClear: 8,
                      sizeAfterClear: session.size(),
                      threw,
                      allowed,
                      confirmCalls,
                      cleared,
                      storageKeys,
                      historyDraft,
                    };
                  } finally {
                    globalThis.confirm = original;
                  }
                }""")
            assert result["sizeBeforeClear"] == 8
            assert result["sizeAfterClear"] == 0
            assert result["threw"]
            assert result["allowed"] is False
            assert result["confirmCalls"] == ["switch?"]
            assert result["cleared"] is True
            assert not result["storageKeys"]
            assert result["historyDraft"] is None
            assert "draft_text" not in json.dumps(result)
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ("chromium", "webkit"))
def test_serialize_and_document_validation_rejects_stale_hash(
    browser_source, draft_assets_available, engine
):
    playwright = pytest.importorskip("playwright.sync_api")
    source, service = browser_source
    digest = service.bootstrap().reports[0].report_sha256
    text = source.read_text(encoding="utf-8")
    payload = hashlib.sha256(text.encode()).hexdigest()
    with (
        served(create_server(service, source_root=source.parent, token=TOKEN)) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser = _launch_browser(runtime, engine)
        try:
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}")
            page.locator(".cards").wait_for()
            result = page.evaluate(
                """async ({digest, path, payload, text}) => {
                  const {serializeDraftText, mountDraftPane, DraftSession,
                    splitLinesCount} = await import('/code_drafts.js');
                  const crlf = serializeDraftText('line\\n', {newlineStyle: 'crlf',
                    hasFinalNewline: true});
                  const noFinal = serializeDraftText('tail\\n', {newlineStyle: 'lf',
                    hasFinalNewline: false});
                  const calls = [];
                  const api = async url => {
                    calls.push(url);
                    return {
                      report_sha256: digest,
                      revision: 3,
                      document: {
                        logical_path: 'other.py',
                        expected_sha256: payload,
                        state: 'bound',
                        message: 'ok',
                        text,
                        byte_count: new TextEncoder().encode(text).length,
                        line_count: splitLinesCount(text),
                        newline_style: 'lf',
                        has_final_newline: text.endsWith('\\n'),
                        editable: true,
                      },
                    };
                  };
                  const host = document.createElement('div');
                  document.body.append(host);
                  const session = new DraftSession();
                  const controller = new AbortController();
                  const pane = mountDraftPane(host, {
                    api,
                    signal: controller.signal,
                    reportSha256: digest,
                    path: 'demo.py',
                    revision: 3,
                    onDirtyChange() {},
                  }, session);
                  await pane.openDraft();
                  const status = host.querySelector('.code-drafts-status')?.textContent || '';
                  pane.destroy();
                  return {crlf, noFinal, calls, status};
                }""",
                {"digest": digest, "path": "demo.py", "payload": payload, "text": text},
            )
            assert result["crlf"] == "line\r\n"
            assert result["noFinal"] == "tail"
            assert len(result["calls"]) == 1
            assert DOCUMENT_ROUTE in result["calls"][0]
            assert "path does not match" in result["status"]
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ("chromium", "webkit"))
def test_edit_draft_fetches_once_normalizes_crlf_and_surfaces_script_literal(
    tmp_path, draft_assets_available, engine
):
    playwright = pytest.importorskip("playwright.sync_api")
    payload = b"# <script>window.pwned = true</script>\r\nvalue = 1\r\n"
    target = tmp_path / "demo.py"
    target.write_bytes(payload)
    service = ReportViewService(scan(discover_context(target, AnalysisConfig(test_roots=()))))
    digest = service.bootstrap().reports[0].report_sha256
    with (
        served(create_server(service, source_root=tmp_path, token=TOKEN)) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser = _launch_browser(runtime, engine)
        try:
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}")
            page.locator(".cards").wait_for()
            result = page.evaluate(
                """async ({digest, token}) => {
                  const {mountDraftPane, DraftSession} = await import('/code_drafts.js');
                  const calls = [];
                  const api = async (path, signal) => {
                    calls.push(path);
                    const response = await fetch(path,
                      {headers: {'X-Code-Analysis-Token': token}, signal});
                    return response.json();
                  };
                  const host = document.createElement('div');
                  document.body.append(host);
                  const dirty = [];
                  const session = new DraftSession();
                  const pane = mountDraftPane(host, {
                    api,
                    signal: new AbortController().signal,
                    reportSha256: digest,
                    path: 'demo.py',
                    revision: 4,
                    onDirtyChange: value => dirty.push(value),
                  }, session);
                  const before = calls.length;
                  await pane.openDraft();
                  await new Promise((resolve, reject) => {
                    const start = performance.now();
                    const tick = () => {
                      const textarea = host.querySelector('textarea');
                      if (textarea && textarea.value) return resolve();
                      if (performance.now() - start > 5000)
                        return reject(new Error('editor did not open'));
                      requestAnimationFrame(tick);
                    };
                    tick();
                  });
                  const textarea = host.querySelector('textarea');
                  const value = textarea ? textarea.value : '';
                  const hasScript = value.includes('<script>');
                  const hasBareCr = value.includes('\\r');
                  pane.destroy();
                  return {calls: calls.length - before, value, hasScript, hasBareCr, dirty};
                }""",
                {"digest": digest, "token": TOKEN},
            )
            assert result["calls"] == 1
            assert result["hasScript"]
            assert not result["hasBareCr"]
            assert "\\r\\n" not in result["value"]
            assert result["dirty"] == [False]
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ("chromium", "webkit"))
def test_mixed_newlines_stay_read_only(browser_source, draft_assets_available, engine):
    playwright = pytest.importorskip("playwright.sync_api")
    source, service = browser_source
    mixed = b"first = 1\r\nsecond = 2\n"
    source.write_bytes(mixed)
    service = ReportViewService(
        scan(discover_context(source.parent, AnalysisConfig(test_roots=())))
    )
    digest = service.bootstrap().reports[0].report_sha256
    with (
        served(create_server(service, source_root=source.parent, token=TOKEN)) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser = _launch_browser(runtime, engine)
        try:
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}")
            page.locator(".cards").wait_for()
            read_only = page.evaluate(
                """async ({digest, token, raw}) => {
                  const {mountDraftPane, DraftSession,
                    serializeDraftText} = await import('/code_drafts.js');
                  const api = async path => (await fetch(path,
                    {headers: {'X-Code-Analysis-Token': token}})).json();
                  const host = document.createElement('div');
                  document.body.append(host);
                  const pane = mountDraftPane(host, {
                    api,
                    signal: new AbortController().signal,
                    reportSha256: digest,
                    path: 'demo.py',
                    revision: 2,
                    onDirtyChange() {},
                  }, new DraftSession());
                  await pane.openDraft();
                  const textarea = host.querySelector('textarea');
                  const status = host.querySelector('.code-drafts-status')?.textContent || '';
                  const buttons = [...host.querySelectorAll('.lui-editor button')];
                  const copy = buttons.find(node => node.textContent === 'Copy');
                  let copied = '';
                  const originalClip = navigator.clipboard.writeText;
                  navigator.clipboard.writeText = async value => { copied = value; };
                  copy?.click();
                  await new Promise(resolve => setTimeout(resolve, 0));
                  pane.destroy();
                  navigator.clipboard.writeText = originalClip;
                  let serializeFailed = false;
                  try { serializeDraftText(textarea?.value || '', {newlineStyle: 'mixed',
                    hasFinalNewline: true}); }
                  catch { serializeFailed = true; }
                  return {readOnly: textarea?.readOnly ?? true, status, copied, raw,
                    serializeFailed};
                }""",
                {"digest": digest, "token": TOKEN, "raw": mixed.decode("latin-1")},
            )
            assert read_only["readOnly"] is True
            assert "read-only" in read_only["status"].lower()
            assert read_only["copied"] == read_only["raw"]
        finally:
            browser.close()


def test_document_api_remains_available_for_draft_consumer(browser_source):
    source, service = browser_source
    params = {
        "report_sha256": service.bootstrap().reports[0].report_sha256,
        "revision": 5,
        "path": "demo.py",
    }
    with served(create_server(service, source_root=source.parent, token=TOKEN)) as server:
        connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        try:
            status, payload = get(connection, DOCUMENT_ROUTE, params)
            assert status == 200
            document = payload["document"]
            assert document["state"] == "bound"
            assert "<script>" in document["text"]
        finally:
            connection.close()


@pytest.mark.parametrize("engine", ("chromium", "webkit"))
def test_late_document_response_is_ignored(browser_source, draft_assets_available, engine):
    playwright = pytest.importorskip("playwright.sync_api")
    source, service = browser_source
    digest = service.bootstrap().reports[0].report_sha256
    with (
        served(create_server(service, source_root=source.parent, token=TOKEN)) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser = _launch_browser(runtime, engine)
        try:
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}")
            page.locator(".cards").wait_for()
            status_text = page.evaluate(
                """async ({digest, token}) => {
                  const {mountDraftPane, DraftSession} = await import('/code_drafts.js');
                  let release;
                  const api = (path, signal) => new Promise(resolve => {
                    release = () => resolve(fetch(path,
                      {headers: {'X-Code-Analysis-Token': token}, signal}).then(r => r.json()));
                  });
                  const host = document.createElement('div');
                  document.body.append(host);
                  const controller = new AbortController();
                  const pane = mountDraftPane(host, {
                    api,
                    signal: controller.signal,
                    reportSha256: digest,
                    path: 'demo.py',
                    revision: 9,
                    onDirtyChange() {},
                  }, new DraftSession());
                  const pending = pane.openDraft();
                  controller.abort();
                  release();
                  await pending;
                  const status = host.querySelector('.code-drafts-status')?.textContent || '';
                  return status;
                }""",
                {"digest": digest, "token": TOKEN},
            )
            assert "Draft open" not in status_text
            assert "unavailable" not in status_text.lower()
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ("chromium",))
def test_wire_validation_rejects_extra_fields_and_metadata_mismatch(
    browser_source, draft_assets_available, engine
):
    playwright = pytest.importorskip("playwright.sync_api")
    _, service = browser_source
    digest = service.bootstrap().reports[0].report_sha256
    with (
        served(create_server(service, token=TOKEN)) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser = _launch_browser(runtime, engine)
        try:
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}")
            page.locator(".cards").wait_for()
            errors = page.evaluate(
                """async ({digest}) => {
                  const {mountDraftPane, DraftSession,
                    splitLinesCount} = await import('/code_drafts.js');
                  const text = 'a\\n';
                  const base = {
                    logical_path: 'demo.py',
                    expected_sha256: 'a'.repeat(64),
                    state: 'bound',
                    message: 'ok',
                    text,
                    byte_count: new TextEncoder().encode(text).length,
                    line_count: splitLinesCount(text),
                    newline_style: 'lf',
                    has_final_newline: true,
                    editable: true,
                  };
                  const cases = [
                    {report_sha256: digest, revision: 1, document: {...base, extra: true}},
                    {report_sha256: digest, revision: 1, document: {...base,
                      has_final_newline: false}},
                    {report_sha256: digest, revision: 1, document: {...base, editable: 0}},
                    {report_sha256: digest, revision: 1, document: {...base, line_count: 99}},
                  ];
                  const host = document.createElement('div');
                  document.body.append(host);
                  const pane = mountDraftPane(host, {
                    api: async () => cases.shift(),
                    signal: new AbortController().signal,
                    reportSha256: digest,
                    path: 'demo.py',
                    revision: 1,
                    onDirtyChange() {},
                  }, new DraftSession());
                  const messages = [];
                  for (let index = 0; index < 4; index += 1) {
                    await pane.openDraft();
                    messages.push(host.querySelector('.code-drafts-status')?.textContent || '');
                  }
                  pane.destroy();
                  return messages;
                }""",
                {"digest": digest},
            )
            assert any("unexpected field" in message for message in errors)
            assert any("final-newline" in message for message in errors)
            assert any("boolean" in message.lower() for message in errors)
            assert any("line count" in message for message in errors)
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ("chromium",))
def test_splitlines_count_matches_python(browser_source, draft_assets_available, engine):
    playwright = pytest.importorskip("playwright.sync_api")
    samples = ["", "a", "a\n", "a\r\n", "a\u2028b", "a\v b"]
    expected = [len(sample.splitlines()) for sample in samples]
    _, service = browser_source
    with (
        served(create_server(service, token=TOKEN)) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser = _launch_browser(runtime, engine)
        try:
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}")
            page.locator(".cards").wait_for()
            counts = page.evaluate(
                """async samples => {
                  const {splitLinesCount} = await import('/code_drafts.js');
                  return samples.map(sample => splitLinesCount(sample));
                }""",
                samples,
            )
            assert counts == expected
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ("chromium",))
def test_repeated_edit_draft_retains_dirty_text(tmp_path, draft_assets_available, engine):
    playwright = pytest.importorskip("playwright.sync_api")
    target = tmp_path / "demo.py"
    target.write_text("base = 1\n", encoding="utf-8")
    service = ReportViewService(scan(discover_context(target, AnalysisConfig(test_roots=()))))
    digest = service.bootstrap().reports[0].report_sha256
    with (
        served(create_server(service, source_root=tmp_path, token=TOKEN)) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser = _launch_browser(runtime, engine)
        try:
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}")
            page.locator(".cards").wait_for()
            result = page.evaluate(
                """async ({digest, token}) => {
                  const {mountDraftPane, DraftSession} = await import('/code_drafts.js');
                  const api = async (path, signal) => (await fetch(path,
                    {headers: {'X-Code-Analysis-Token': token}, signal})).json();
                  const host = document.createElement('div');
                  document.body.append(host);
                  const confirms = [];
                  const original = globalThis.confirm;
                  globalThis.confirm = () => { confirms.push(true); return true; };
                  const session = new DraftSession();
                  const pane = mountDraftPane(host, {
                    api,
                    signal: new AbortController().signal,
                    reportSha256: digest,
                    path: 'demo.py',
                    revision: 1,
                    onDirtyChange() {},
                  }, session);
                  await pane.openDraft();
                  const textarea = host.querySelector('textarea');
                  textarea.focus();
                  textarea.setRangeText('\\nedited', textarea.value.length,
                    textarea.value.length, 'end');
                  textarea.dispatchEvent(new InputEvent('input', {bubbles: true}));
                  await pane.openDraft();
                  const value = textarea.value;
                  pane.destroy();
                  globalThis.confirm = original;
                  return {value, dirtyFlag: session.list()[0]?.dirty, confirms};
                }""",
                {"digest": digest, "token": TOKEN},
            )
            assert "edited" in result["value"]
            assert result["dirtyFlag"] is True
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ("chromium",))
def test_switch_cancel_preserves_active_and_close_frees_capacity(
    tmp_path, draft_assets_available, engine
):
    playwright = pytest.importorskip("playwright.sync_api")
    a_text = "a = 1\n"
    b_text = "b = 2\n"
    (tmp_path / "a.py").write_text(a_text, encoding="utf-8")
    (tmp_path / "b.py").write_text(b_text, encoding="utf-8")
    service = ReportViewService(
        scan(discover_context(tmp_path / "a.py", AnalysisConfig(test_roots=())))
    )
    digest = service.bootstrap().reports[0].report_sha256
    b_hash = hashlib.sha256(b_text.encode()).hexdigest()
    with (
        served(create_server(service, source_root=tmp_path, token=TOKEN)) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser = _launch_browser(runtime, engine)
        try:
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}")
            page.locator(".cards").wait_for()
            result = page.evaluate(
                """async ({digest, token, bText, bHash}) => {
                  const {mountDraftPane, DraftSession} = await import('/code_drafts.js');
                  const api = async (path, signal) => (await fetch(path,
                    {headers: {'X-Code-Analysis-Token': token}, signal})).json();
                  const host = document.createElement('div');
                  document.body.append(host);
                  const session = new DraftSession();
                  let confirmAnswer = true;
                  globalThis.confirm = () => confirmAnswer;
                  const pane = mountDraftPane(host, {
                    api,
                    signal: new AbortController().signal,
                    reportSha256: digest,
                    path: 'a.py',
                    revision: 1,
                    onDirtyChange() {},
                  }, session);
                  await pane.openDraft();
                  const keyA = session.list()[0].key;
                  const textarea = host.querySelector('textarea');
                  textarea.focus();
                  textarea.setRangeText('!', textarea.value.length, textarea.value.length, 'end');
                  textarea.dispatchEvent(new InputEvent('input', {bubbles: true}));
                  const keyB = `${digest}\\0b.py\\0${bHash}`;
                  session.remember({
                    key: keyB,
                    reportSha256: digest,
                    logicalPath: 'b.py',
                    sourceSha256: bHash,
                    revision: 1,
                    originalText: bText,
                    baselineLfText: bText,
                    newlineStyle: 'lf',
                    hasFinalNewline: true,
                    editable: true,
                    editorId: null,
                    draftSnapshot: null,
                    dirty: false,
                  });
                  confirmAnswer = false;
                  await pane.switchDraft(keyB);
                  const activeAfterCancel = host.querySelector('.code-drafts-identity')
                    ?.textContent || '';
                  confirmAnswer = true;
                  await pane.switchDraft(keyB);
                  await pane.closeDraft(keyA, {discard: false});
                  const sizeAfterClose = session.size();
                  pane.destroy();
                  return {activeAfterCancel, sizeAfterClose};
                }""",
                {"digest": digest, "token": TOKEN, "bText": b_text, "bHash": b_hash},
            )
            assert "path a.py" in result["activeAfterCancel"]
            assert result["sizeAfterClose"] == 1
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ("chromium",))
def test_switch_roundtrip_restores_identity_and_selection(tmp_path, draft_assets_available, engine):
    playwright = pytest.importorskip("playwright.sync_api")
    a_text = "a = 1\n"
    b_text = "b = 2\n"
    (tmp_path / "a.py").write_text(a_text, encoding="utf-8")
    (tmp_path / "b.py").write_text(b_text, encoding="utf-8")
    service = ReportViewService(
        scan(discover_context(tmp_path / "a.py", AnalysisConfig(test_roots=())))
    )
    digest = service.bootstrap().reports[0].report_sha256
    b_hash = hashlib.sha256(b_text.encode()).hexdigest()
    with (
        served(create_server(service, source_root=tmp_path, token=TOKEN)) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser = _launch_browser(runtime, engine)
        try:
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}")
            page.locator(".cards").wait_for()
            result = page.evaluate(
                """async ({digest, token, bText, bHash}) => {
                  const {mountDraftPane, DraftSession} = await import('/code_drafts.js');
                  const api = async (path, signal) => (await fetch(path,
                    {headers: {'X-Code-Analysis-Token': token}, signal})).json();
                  const host = document.createElement('div');
                  document.body.append(host);
                  const session = new DraftSession();
                  globalThis.confirm = () => true;
                  const pane = mountDraftPane(host, {
                    api,
                    signal: new AbortController().signal,
                    reportSha256: digest,
                    path: 'a.py',
                    revision: 1,
                    onDirtyChange() {},
                  }, session);
                  await pane.openDraft();
                  const keyA = session.list()[0].key;
                  const keyB = `${digest}\\0b.py\\0${bHash}`;
                  session.remember({
                    key: keyB,
                    reportSha256: digest,
                    logicalPath: 'b.py',
                    sourceSha256: bHash,
                    revision: 1,
                    originalText: bText,
                    baselineLfText: bText,
                    newlineStyle: 'lf',
                    hasFinalNewline: true,
                    editable: true,
                    editorId: null,
                    draftSnapshot: null,
                    dirty: false,
                  });
                  await pane.switchDraft(keyB);
                  const entryA = session.get(keyA);
                  entryA.draftSnapshot = {
                    documentId: entryA.editorId,
                    revision: 1,
                    text: 'a = 1\\nmarker\\n',
                    selection: {start: 7, end: 7, direction: 'none'},
                  };
                  entryA.dirty = true;
                  await pane.switchDraft(keyA);
                  const textarea = host.querySelector('textarea');
                  const identity = host.querySelector('.code-drafts-identity')?.textContent || '';
                  pane.destroy();
                  return {
                    identity,
                    value: textarea?.value || '',
                    selectionStart: textarea?.selectionStart,
                  };
                }""",
                {"digest": digest, "token": TOKEN, "bText": b_text, "bHash": b_hash},
            )
            assert "path a.py" in result["identity"]
            assert "marker" in result["value"]
            assert result["selectionStart"] == 7
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ("chromium",))
def test_serialize_final_newline_and_double_terminal_error(
    browser_source, draft_assets_available, engine
):
    playwright = pytest.importorskip("playwright.sync_api")
    _, service = browser_source
    with (
        served(create_server(service, token=TOKEN)) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser = _launch_browser(runtime, engine)
        try:
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}")
            page.locator(".cards").wait_for()
            result = page.evaluate("""async () => {
                  const {serializeDraftText} = await import('/code_drafts.js');
                  let doubleError = '';
                  try { serializeDraftText('a\\n\\n', {newlineStyle: 'lf',
                    hasFinalNewline: false}); }
                  catch (error) { doubleError = error.message; }
                  return {
                    crlfNoFinal: serializeDraftText('x\\n', {newlineStyle: 'crlf',
                      hasFinalNewline: false}),
                    lfFinal: serializeDraftText('x', {newlineStyle: 'lf', hasFinalNewline: true}),
                    doubleError,
                  };
                }""")
            assert result["crlfNoFinal"] == "x"
            assert result["lfFinal"] == "x\n"
            assert "final-newline" in result["doubleError"]
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ("chromium",))
def test_dirty_callback_only_when_active_draft_matches_browse(
    tmp_path, draft_assets_available, engine
):
    playwright = pytest.importorskip("playwright.sync_api")
    a_text = "a = 1\n"
    b_text = "b = 2\n"
    (tmp_path / "a.py").write_text(a_text, encoding="utf-8")
    (tmp_path / "b.py").write_text(b_text, encoding="utf-8")
    service = ReportViewService(
        scan(discover_context(tmp_path / "a.py", AnalysisConfig(test_roots=())))
    )
    digest = service.bootstrap().reports[0].report_sha256
    b_hash = hashlib.sha256(b_text.encode()).hexdigest()
    with (
        served(create_server(service, source_root=tmp_path, token=TOKEN)) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser = _launch_browser(runtime, engine)
        try:
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}")
            page.locator(".cards").wait_for()
            dirty = page.evaluate(
                """async ({digest, token, bText, bHash}) => {
                  const {mountDraftPane, DraftSession} = await import('/code_drafts.js');
                  const api = async (path, signal) => (await fetch(path,
                    {headers: {'X-Code-Analysis-Token': token}, signal})).json();
                  const host = document.createElement('div');
                  document.body.append(host);
                  const session = new DraftSession();
                  const flags = [];
                  globalThis.confirm = () => true;
                  const pane = mountDraftPane(host, {
                    api,
                    signal: new AbortController().signal,
                    reportSha256: digest,
                    path: 'a.py',
                    revision: 1,
                    onDirtyChange: value => flags.push(value),
                  }, session);
                  await pane.openDraft();
                  const textarea = host.querySelector('textarea');
                  textarea.focus();
                  textarea.setRangeText('!', textarea.value.length, textarea.value.length, 'end');
                  textarea.dispatchEvent(new InputEvent('input', {bubbles: true}));
                  const keyB = `${digest}\\0b.py\\0${bHash}`;
                  session.remember({
                    key: keyB,
                    reportSha256: digest,
                    logicalPath: 'b.py',
                    sourceSha256: bHash,
                    revision: 1,
                    originalText: bText,
                    baselineLfText: bText,
                    newlineStyle: 'lf',
                    hasFinalNewline: true,
                    editable: true,
                    editorId: null,
                    draftSnapshot: null,
                    dirty: false,
                  });
                  await pane.switchDraft(keyB);
                  pane.destroy();
                  return flags;
                }""",
                {"digest": digest, "token": TOKEN, "bText": b_text, "bHash": b_hash},
            )
            assert dirty[0] is False
            assert True in dirty
            assert dirty[-1] is False
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ("chromium",))
def test_composition_blocks_navigation(tmp_path, draft_assets_available, engine):
    playwright = pytest.importorskip("playwright.sync_api")
    target = tmp_path / "demo.py"
    target.write_text("text\n", encoding="utf-8")
    service = ReportViewService(scan(discover_context(target, AnalysisConfig(test_roots=()))))
    digest = service.bootstrap().reports[0].report_sha256
    with (
        served(create_server(service, source_root=tmp_path, token=TOKEN)) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser = _launch_browser(runtime, engine)
        try:
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}")
            page.locator(".cards").wait_for()
            blocked = page.evaluate(
                """async ({digest, token}) => {
                  const {mountDraftPane, DraftSession} = await import('/code_drafts.js');
                  const api = async (path, signal) => (await fetch(path,
                    {headers: {'X-Code-Analysis-Token': token}, signal})).json();
                  const host = document.createElement('div');
                  document.body.append(host);
                  const pane = mountDraftPane(host, {
                    api,
                    signal: new AbortController().signal,
                    reportSha256: digest,
                    path: 'demo.py',
                    revision: 1,
                    onDirtyChange() {},
                  }, new DraftSession());
                  await pane.openDraft();
                  const textarea = host.querySelector('textarea');
                  textarea.dispatchEvent(new CompositionEvent('compositionstart', {bubbles: true}));
                  const before = pane.canNavigate();
                  await pane.switchDraft('missing');
                  const status = host.querySelector('.code-drafts-status')?.textContent || '';
                  textarea.dispatchEvent(new CompositionEvent('compositionend', {bubbles: true}));
                  pane.destroy();
                  return {before, status};
                }""",
                {"digest": digest, "token": TOKEN},
            )
            assert blocked["before"] is False
            assert "composition" in blocked["status"].lower()
        finally:
            browser.close()
