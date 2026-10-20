"""Explicit local display preferences, separate from analytical configuration."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

from pydantic import StrictBool, ValidationError

from code_analysis.artifact_viewer import ViewerCommand
from code_analysis.models import StrictModel


class DisplayPreferences(StrictModel):
    browser_command: ViewerCommand | None = None
    browser_auto_open: StrictBool | None = None
    viewer_command: ViewerCommand | None = None
    viewer_enabled: StrictBool = True


def load_display_preferences(path: Path | None) -> DisplayPreferences:
    """Read only an explicitly selected regular file; never discover project hooks."""
    if path is None:
        return DisplayPreferences()
    descriptor = os.open(path.expanduser(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > 65536:
            raise OSError(errno.EINVAL, "display configuration must be a regular file <= 64 KiB")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(65537)
        after = os.fstat(descriptor)
        if len(data) != before.st_size or (
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise OSError(errno.EBADMSG, "display configuration changed while reading")
        try:
            return DisplayPreferences.model_validate_json(data)
        except ValidationError as exc:
            # Do not echo executable arguments or values in diagnostics.
            raise OSError(errno.EINVAL, "invalid display configuration") from exc
    finally:
        os.close(descriptor)
