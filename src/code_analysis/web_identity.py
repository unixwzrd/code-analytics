"""Allowlisted running-server identity, independent of loaded analysis artifacts."""

from __future__ import annotations

import sys
from importlib.metadata import PackageNotFoundError, version
from typing import Literal

from pydantic import Field, StrictStr

from code_analysis.models import StrictModel

OSFamily = Literal["macOS", "Windows", "Linux", "Other"]


class ApplicationInfo(StrictModel):
    """Public troubleshooting facts; never installation paths or host identity."""

    application_version: StrictStr = Field(
        min_length=1, max_length=100, pattern=r"^(?:unavailable|[0-9][A-Za-z0-9.!+_-]*)$"
    )
    os_family: OSFamily
    python_version: StrictStr = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")


def application_info() -> ApplicationInfo:
    """Read installed package metadata, not a report producer or checkout Git state."""
    try:
        installed_version = version("code-analysis-suite")
    except PackageNotFoundError:
        installed_version = "unavailable"
    os_families: dict[str, OSFamily] = {
        "darwin": "macOS",
        "win32": "Windows",
        "linux": "Linux",
    }
    return ApplicationInfo(
        application_version=installed_version,
        os_family=os_families.get(sys.platform, "Other"),
        python_version=".".join(str(value) for value in sys.version_info[:3]),
    )
