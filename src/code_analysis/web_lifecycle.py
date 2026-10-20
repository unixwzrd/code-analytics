"""Application-owned lifecycle contracts, independent of analytical evidence."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, StrictBool, StrictInt, StrictStr

from code_analysis.models import StrictModel


class UIShutdownRequest(StrictModel):
    confirmed: StrictBool
    cancel_jobs: StrictBool = False


class UILifecycleStatus(StrictModel):
    schema_name: Literal["code-analysis-ui-session"] = "code-analysis-ui-session"
    instance_id: StrictStr = Field(pattern=r"^[a-f0-9]{32}$")
    pid: StrictInt = Field(gt=0)
    active_jobs: StrictInt = Field(ge=0)
    stopping: StrictBool
    shutdown_available: StrictBool
