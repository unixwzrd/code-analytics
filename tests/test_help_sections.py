"""Contextual guides are bounded sections, not an implicit full manual."""

from __future__ import annotations

import threading
from contextlib import contextmanager

import pytest

from code_analysis.web_server import create_server


@contextmanager
def help_page(tmp_path, engine):
    playwright = pytest.importorskip("playwright.sync_api")
    server = create_server(None, collection_root=tmp_path, token="help-sections-test")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as runtime:
            browser = getattr(runtime, engine).launch()
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token=help-sections-test")
                page.locator("[data-global-help]").wait_for()
                yield page, playwright.expect
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def open_section(page, anchor):
    page.evaluate(
        """anchor => {
          document.querySelector('#section-test-link')?.remove();
          const link = document.createElement('a');
          link.id = 'section-test-link';
          link.href = `/help/user-guide.html#${anchor}`;
          link.textContent = 'Read selected guide section';
          document.querySelector('#content').prepend(link);
        }""",
        anchor,
    )
    page.locator("#section-test-link").click()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.ui_smoke
def test_first_help_is_focused_and_full_reference_is_explicit(tmp_path, engine):
    with help_page(tmp_path, engine) as (page, expect):
        page.locator("[data-global-help]").click()
        article = page.locator("#user-help article")
        expect(article.get_by_role("heading").first).to_have_text("Your first investigation")
        expect(article).to_have_attribute("data-help-mode", "section")
        assert article.locator("#review-findings").count() == 0
        page.get_by_role("button", name="Browse full documentation", exact=True).click()
        expect(article).to_have_attribute("data-help-mode", "full")
        expect(article.get_by_role("heading").first).to_have_text("Web and terminal usage")
        assert article.locator("#review-findings").count() == 1
        page.get_by_role("combobox", name="Documentation topic").select_option("getting-started")
        expect(article.get_by_role("heading").first).to_have_text("Getting started")
        expect(article).to_have_attribute("data-help-mode", "full")
        page.keyboard.press("Escape")
        page.locator("[data-global-help]").click()
        expect(article.get_by_role("heading").first).to_have_text("Getting started")
        expect(article).to_have_attribute("data-help-mode", "full")


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_section_links_and_nested_headings_stay_in_reader(tmp_path, engine):
    with help_page(tmp_path, engine) as (page, expect):
        open_section(page, "review-findings")
        article = page.locator("#user-help article")
        expect(article.get_by_role("heading").first).to_have_text("Review findings")
        assert article.locator("#understand-state-and-effects").count() == 0
        original_url = page.url
        # Same-document links can target a heading outside the detached section.
        article.evaluate("""node => {
          const link = document.createElement('a');
          link.href = '/help/user-guide.html#recover-storage-with-today-s-tools';
          link.textContent = 'Related storage guide'; node.append(link);
        }""")
        article.get_by_role("link", name="Related storage guide").click()
        expect(article.get_by_role("heading").first).to_have_text(
            "Recover storage with today's tools"
        )
        assert article.locator("#code-browsing-and-session-draft-editing").count() == 0
        assert page.url == original_url
        page.get_by_role("button", name="Previous help topic", exact=True).click()
        expect(article.get_by_role("heading").first).to_have_text("Review findings")


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_section_and_full_document_have_independent_history_positions(tmp_path, engine):
    with help_page(tmp_path, engine) as (page, expect):
        paragraphs = "".join(f"<p>Evidence paragraph {index}.</p>" for index in range(100))
        page.route(
            "**/help/user-guide.html",
            lambda route: route.fulfill(
                content_type="text/html",
                body=(
                    '<main><h1 id="web-and-terminal-usage">Whole guide</h1>'
                    '<section><h2 id="review-findings">Review findings</h2>'
                    f"{paragraphs}</section>"
                    '<h2 id="understand-state-and-effects">Next section</h2><p>Outside.</p></main>'
                ),
            ),
        )
        open_section(page, "review-findings")
        article = page.locator("#user-help article")
        expect(article.get_by_role("heading").first).to_have_text("Review findings")
        assert article.locator("p").count() == 100
        assert article.locator("#understand-state-and-effects").count() == 0
        scroller = page.locator("#user-help .lui-content-panel__body")
        scroller.evaluate("node => {node.scrollTop = 300}")
        # Invoke without Playwright scrolling the control into view first;
        # this isolates saved scope positions from ordinary pointer scrolling.
        page.get_by_role("button", name="Browse full documentation", exact=True).evaluate(
            "node => node.click()"
        )
        expect(article).to_have_attribute("data-help-mode", "full")
        scroller.evaluate("node => {node.scrollTop = 1000}")
        # Likewise retain the current scroll position when invoking history.
        page.get_by_role("button", name="Previous help topic", exact=True).evaluate(
            "node => node.click()"
        )
        expect(article).to_have_attribute("data-help-mode", "section")
        assert scroller.evaluate("node => node.scrollTop") == 300
        page.get_by_role("button", name="Next help topic", exact=True).evaluate(
            "node => node.click()"
        )
        expect(article).to_have_attribute("data-help-mode", "full")
        assert scroller.evaluate("node => node.scrollTop") == 1000


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_missing_section_fails_locally_without_showing_full_document(tmp_path, engine):
    with help_page(tmp_path, engine) as (page, expect):
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        open_section(page, "no-such-packaged-heading")
        article = page.locator("#user-help article")
        expect(article.get_by_role("heading")).to_have_text("Help is unavailable")
        expect(article).to_contain_text("This guide section is not in the packaged documentation")
        assert article.locator("#review-findings").count() == 0
        page.keyboard.press("Escape")
        page.locator("[data-global-help]").click()
        expect(article.get_by_role("heading").first).to_have_text("Your first investigation")
        assert not errors
