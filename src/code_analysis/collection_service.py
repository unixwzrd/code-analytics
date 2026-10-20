"""Authenticated transports share this explicit, single-workspace collection session."""

from __future__ import annotations

import errno
from threading import RLock

from pydantic import Field, StrictBool, StrictStr

from code_analysis.collection_commands import (
    CollectionCommand,
    CollectionPlanner,
    CollectionRequest,
)
from code_analysis.collection_execution import CollectionExecutionLimits
from code_analysis.collection_job_models import CollectionJob
from code_analysis.collection_jobs import CollectionJobs
from code_analysis.config import load_project_config
from code_analysis.models import StrictModel
from code_analysis.project import discover_context, find_project_root
from code_analysis.workspace import open_workspace, prepare_workspace


class CollectionLaunch(StrictModel):
    request: CollectionRequest
    command: CollectionCommand
    limits: CollectionExecutionLimits
    confirmed: StrictBool = False


class CollectionWorkspace(StrictModel):
    workspace: StrictStr = Field(min_length=1, max_length=2048)


class CollectionJobSelection(StrictModel):
    job_id: StrictStr = Field(pattern=r"^[a-f0-9]{32}$")


class CollectionJobCatalog(StrictModel):
    workspace: StrictStr | None
    records: tuple[CollectionJob, ...]


class CollectionSession:
    def __init__(self, planner: CollectionPlanner):
        self.planner = planner
        self.jobs: CollectionJobs | None = None
        self._lock = RLock()
        self._closed = False

    def catalog(self) -> CollectionJobCatalog:
        with self._lock:
            return CollectionJobCatalog(
                workspace=str(self.jobs.workspace.root) if self.jobs else None,
                records=self.jobs.list() if self.jobs else (),
            )

    def connect(self, selection: CollectionWorkspace) -> CollectionJobCatalog:
        with self._lock:
            if self._closed:
                raise OSError(errno.EBADF, "collection session is closed")
            path = self.planner._path(selection.workspace, directory=True)
            if self.jobs is not None and path != self.jobs.workspace.root:
                raise OSError(errno.EBUSY, "this UI session already selects another workspace")
            if self.jobs is None:
                self.jobs = CollectionJobs(self.planner, open_workspace(path))
            return self.catalog()

    def launch(self, launch: CollectionLaunch) -> CollectionJob:
        with self._lock:
            if self._closed:
                raise OSError(errno.EBADF, "collection session is closed")
            if not launch.confirmed:
                raise OSError(errno.EPERM, "explicit collection confirmation is required")
            try:
                import psutil  # noqa: F401 - required before claiming managed process ownership
            except ImportError as exc:
                raise OSError(
                    errno.ENODATA,
                    "managed collection requires available psutil process identity support",
                ) from exc
            request = launch.request
            if request.workspace is None:
                raise OSError(errno.EINVAL, "choose an explicit workspace before launch")
            if self.planner.preview(request) != launch.command:
                raise OSError(errno.EINVAL, "rebuild the command preview after editing settings")
            path = self.planner.root / request.workspace
            if self.jobs is not None and path != self.jobs.workspace.root:
                raise OSError(errno.EBUSY, "this UI session already selects another workspace")
            if self.jobs is None:
                if request.operation == "import-profile":
                    self.jobs = CollectionJobs(self.planner, open_workspace(path))
                    return self.jobs.submit(request, launch.command, launch.limits)
                target = self.planner._path(
                    request.cwd if request.operation == "profile" else request.target
                )
                if request.operation == "profile":
                    context = discover_context(target, load_project_config(target))
                else:
                    root = find_project_root(target)
                    context = discover_context(target, load_project_config(root), project_root=root)
                workspace = prepare_workspace(context, explicit_path=path)
                self.jobs = CollectionJobs(self.planner, workspace)
            return self.jobs.submit(request, launch.command, launch.limits)

    def selected(self) -> CollectionJobs:
        with self._lock:
            if self.jobs is None:
                raise OSError(
                    errno.ENODATA, "connect an owned workspace or launch a collection first"
                )
            return self.jobs

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self.jobs is not None:
                self.jobs.close()
            self._closed = True
