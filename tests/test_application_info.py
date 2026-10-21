"""The running package identity is small, strict, and independent of reports."""

from importlib.metadata import PackageNotFoundError

import pytest
from pydantic import ValidationError

from code_analysis import web_identity


def test_application_info_uses_installed_metadata(monkeypatch):
    requested = []

    def metadata_version(distribution):
        requested.append(distribution)
        return "0.2.0b2"

    monkeypatch.setattr(web_identity, "version", metadata_version)
    monkeypatch.setattr(web_identity.sys, "platform", "darwin")
    result = web_identity.application_info()
    assert requested == ["code-analysis-suite"]
    assert result.application_version == "0.2.0b2"
    assert result.os_family == "macOS"
    assert set(result.model_dump()) == {
        "application_version",
        "os_family",
        "python_version",
    }
    assert result == web_identity.application_info()
    with pytest.raises(ValidationError):
        result.application_version = "changed"


def test_uninstalled_identity_is_explicit(monkeypatch):
    def missing(distribution):
        raise PackageNotFoundError(distribution)

    monkeypatch.setattr(web_identity, "version", missing)
    assert web_identity.application_info().application_version == "unavailable"


@pytest.mark.parametrize(
    "update",
    [
        {"application_version": True},
        {"application_version": ""},
        {"application_version": "/private/installation"},
        {"application_version": "0.2.0\nprivate-host"},
        {"os_family": "host.local"},
        {"python_version": "3.12.13 /private/interpreter"},
        {"path": "/private/interpreter"},
    ],
)
def test_identity_rejects_untyped_or_extra_fields(update):
    values = {
        "application_version": "0.2.0b2",
        "os_family": "macOS",
        "python_version": "3.12.13",
    }
    with pytest.raises(ValidationError):
        web_identity.ApplicationInfo.model_validate(values | update)
