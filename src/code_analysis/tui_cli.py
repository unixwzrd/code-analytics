"""Optional terminal frontend entry; importing the core CLI never requires Textual."""

from __future__ import annotations

import errno
import importlib.util
import json
import re
import sqlite3
import sys
from pathlib import Path

from code_analysis.artifact_viewer import ViewerCommand, default_viewer
from code_analysis.browse_index import BrowseIndex
from code_analysis.display_preferences import load_display_preferences


def tui_command(
    directory: Path | None,
    report_sha256: str | None,
    page_size: int,
    source_root: Path | None = None,
    viewer_command: str | None = None,
    no_viewer: bool = False,
    workspace: Path | None = None,
    display_config: Path | None = None,
    collection_root: Path | None = None,
    allow_collection: bool = False,
    collection_pythons: tuple[Path, ...] = (),
) -> int:
    if allow_collection and collection_root is None:
        print("--allow-collection requires --collection-root", file=sys.stderr)
        return errno.EINVAL
    if collection_pythons and collection_root is None:
        print("--collection-python requires --collection-root", file=sys.stderr)
        return errno.EINVAL
    try:
        preferences = load_display_preferences(display_config)
        if no_viewer and viewer_command is not None:
            raise ValueError("--no-viewer cannot be combined with --viewer-command")
        viewer = (
            preferences.viewer_command or default_viewer() if preferences.viewer_enabled else None
        )
        if viewer_command is not None:
            viewer = ViewerCommand.model_validate_json(
                json.dumps({"arguments": json.loads(viewer_command)})
            )
        if no_viewer:
            viewer = None
        browser = preferences.browser_command or default_viewer()
    except OSError as exc:
        print(f"invalid display configuration: {exc.strerror}", file=sys.stderr)
        return exc.errno or errno.EINVAL
    except ValueError as exc:
        print(f"invalid viewer configuration: {exc}", file=sys.stderr)
        return errno.EINVAL
    valid_selection = (
        collection_root is not None and report_sha256 is None
        if directory is None
        else report_sha256 is not None and re.fullmatch(r"[0-9a-f]{64}", report_sha256)
    )
    if not 1 <= page_size <= 500 or not valid_selection:
        print(
            "require index with report SHA-256, or --collection-root without an index; "
            "page size must be 1-500",
            file=sys.stderr,
        )
        return errno.EINVAL
    if importlib.util.find_spec("textual") is None:
        print("TUI unavailable: install the optional tui extra explicitly", file=sys.stderr)
        return errno.ENODATA
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(
            "TUI requires an interactive terminal; use summary/export for redirected output",
            file=sys.stderr,
        )
        return errno.ENOTTY
    try:
        index = (
            BrowseIndex.open(directory, report_sha256=report_sha256)
            if directory is not None and report_sha256 is not None
            else None
        )
    except sqlite3.Error as exc:
        print(f"invalid index database: {exc}", file=sys.stderr)
        return errno.EBADMSG
    from code_analysis.tui_app import AnalysisTerminal

    if source_root is not None:
        source_root = source_root.resolve(strict=True)
        if not source_root.is_dir():
            print("source root must be a directory", file=sys.stderr)
            return errno.EINVAL
    from code_analysis.collection_commands import CollectionPlanner
    from code_analysis.collection_service import CollectionSession
    from code_analysis.tui_start import CollectionTerminal

    session = None
    app: AnalysisTerminal | None = None
    landing: CollectionTerminal | None = None
    if collection_root is not None:
        planner = CollectionPlanner(collection_root, interpreters=collection_pythons)
        if allow_collection:
            session = CollectionSession(planner)
        else:
            planner.close()
    try:
        if index is None:
            assert collection_root is not None
            landing = CollectionTerminal(
                collection_root,
                session,
                collection_pythons=collection_pythons,
                browser=browser,
            )
            index = landing.run()
            if landing.return_code:
                return errno.EFAULT
            if index is None:
                return 0
            if session is not None:
                workspace = session.selected().workspace.root
        app = AnalysisTerminal(
            index,
            page_size=page_size,
            source_root=source_root,
            viewer=viewer,
            workspace=workspace,
            collection_root=collection_root,
            collection_session=session,
            collection_pythons=collection_pythons,
            browser=browser,
        )
        result = app.run()
    finally:
        # The landing host owns a session even when no analysis application is reached.
        if landing is not None:
            landing.close_web_handoff()
        if app is not None:
            app.close_web_handoff()
        if session is not None:
            session.close()
            session.planner.close()
    if app.return_code:
        return errno.EFAULT
    return app.exit_status if result is None else result
