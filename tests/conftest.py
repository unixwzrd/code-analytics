from __future__ import annotations

from pathlib import Path

import pytest
from ui_suite import UiExecutionRecorder, classify_module

from code_analysis.config import AnalysisConfig
from code_analysis.project import discover_context
from code_analysis.service import scan

FIXTURES = Path(__file__).parent / "fixtures"


def pytest_addoption(parser):
    parser.addoption("--ui-evidence", type=Path, help="Write selected/executed UI test evidence")


def pytest_configure(config):
    path = config.getoption("--ui-evidence")
    if path is not None:
        config.pluginmanager.register(UiExecutionRecorder(path), "code-analysis-ui-evidence")


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    """Include browser modules and supporting UI contracts in the repeatable gate."""
    modules = {path: classify_module(path) for path in {item.path for item in items}}
    for item in items:
        module = modules.get(item.path)
        if module is not None or item.get_closest_marker("ui_smoke"):
            item.add_marker(pytest.mark.ui)
        if module is not None and module.browser:
            item.add_marker(pytest.mark.ui_browser)
            if getattr(item, "originalname", item.name) in module.browser_tests:
                item.add_marker(pytest.mark.ui_direct_browser)
    recorder = config.pluginmanager.get_plugin("code-analysis-ui-evidence")
    if recorder is not None:
        recorder.expect(items, config.option.markexpr)


@pytest.fixture
def ordinary_report():
    context = discover_context(FIXTURES / "ordinary", AnalysisConfig())
    return scan(context)
