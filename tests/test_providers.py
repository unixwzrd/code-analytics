from __future__ import annotations

import sys

from conftest import FIXTURES

from code_analysis.config import AnalysisConfig
from code_analysis.models import AvailabilityState, Completeness, ProviderState
from code_analysis.project import discover_context
from code_analysis.providers.registry import JsonCommandProvider, inventory_tools


def _context():
    return discover_context(FIXTURES / "ordinary", AnalysisConfig())


def test_unavailable_provider_is_not_empty_success() -> None:
    provider = JsonCommandProvider("missing", ("definitely-not-a-real-provider",))
    result = provider.run(_context())
    assert result.state is ProviderState.UNAVAILABLE
    assert result.completeness is Completeness.FAILED
    assert result.observations == ()
    assert result.diagnostics


def test_provider_nonzero_is_failure() -> None:
    provider = JsonCommandProvider("nonzero", (sys.executable, "-c", "raise SystemExit(7)"))
    result = provider.run(_context())
    assert result.state is ProviderState.NONZERO
    assert result.exit_code == 7
    assert result.completeness is Completeness.FAILED


def test_provider_malformed_output_is_failure() -> None:
    provider = JsonCommandProvider("malformed", (sys.executable, "-c", "print('not-json')"))
    result = provider.run(_context())
    assert result.state is ProviderState.MALFORMED
    assert result.completeness is Completeness.FAILED


def test_provider_timeout_is_failure() -> None:
    provider = JsonCommandProvider(
        "timeout",
        (sys.executable, "-c", "import time; time.sleep(1)"),
        timeout_seconds=0.01,
    )
    result = provider.run(_context())
    assert result.state is ProviderState.TIMEOUT
    assert result.completeness is Completeness.FAILED


def test_provider_success_requires_strict_observation_envelope() -> None:
    command = "import json; print(json.dumps({'observations': ['edge']}))"
    result = JsonCommandProvider("ok", (sys.executable, "-c", command)).run(_context())
    assert result.state is ProviderState.SUCCESS
    assert result.observations == ("edge",)


def test_registry_marks_missing_optional_tools_unavailable() -> None:
    providers = {item.provider_id: item for item in inventory_tools(portable=True)}
    assert providers["libcst"].state is AvailabilityState.UNAVAILABLE
    assert providers["import-linter"].state is AvailabilityState.UNAVAILABLE
    assert providers["libcst"].executable is None
