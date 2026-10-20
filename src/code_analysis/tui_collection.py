"""Keyboard collection planning using the same typed request and confined browser as Web."""

from __future__ import annotations

from typing import ClassVar

from pydantic import ValidationError
from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Select, Static, TextArea

from code_analysis.browse_index import BrowseIndex
from code_analysis.collection_commands import (
    CollectionCommand,
    CollectionPlanner,
    CollectionRequest,
)
from code_analysis.collection_service import CollectionSession
from code_analysis.tui_app import plain_text


class CollectionPlan(ModalScreen[BrowseIndex | None]):
    BINDINGS: ClassVar[list[BindingType]] = [("escape", "close", "Close")]
    CSS = """
    CollectionPlan { align: center middle; }
    #collection-dialog { width: 95%; height: 95%; border: round $accent; padding: 1;
                         background: $surface; }
    #collection-preview { height: 8; }
    #collection-arguments, #collection-exclude, #collection-profiles { height: 4; }
    #collection-status { height: auto; }
    """

    def __init__(
        self, planner: CollectionPlanner, session: CollectionSession | None = None
    ) -> None:
        super().__init__()
        self.planner = planner
        self.session = session
        self.preview: CollectionCommand | None = None

    def compose(self) -> ComposeResult:
        context = self.planner.context(report_available=True)
        with VerticalScroll(id="collection-dialog"):
            yield Static(
                plain_text(
                    f"Collect / plan · {context.host}\nRoot: {context.root}\n"
                    f"Default interpreter: {context.interpreter}"
                )
            )
            yield Static("Trusted interpreter")
            yield Select(
                [(item.path, item.id) for item in context.interpreters],
                value="current",
                allow_blank=False,
                id="collection-interpreter",
            )
            yield Static(
                "Planning never executes code or writes artifacts. "
                + (
                    "Review and explicitly confirm launch in Run and monitor below. "
                    if self.session is not None
                    else "Copy the reviewed command to run in a terminal. "
                )
                + "Runtime execution is not a sandbox. Paths refer to this host."
            )
            yield Select(
                [(x, x) for x in ("scan", "clones", "refactor", "profile", "import-profile")],
                value="scan",
                allow_blank=False,
                id="collection-operation",
            )
            for name, value in (("target", "."), ("cwd", "."), ("workspace", ".code-analysis")):
                yield Static(name.title() + " (root-relative)")
                yield Input(value=value, id=f"collection-{name}")
            yield Static("Confined filesystem browsing (path then List; select entry then Use)")
            yield Input(value=".", id="collection-directory")
            yield Button("List directory", id="collection-list")
            yield Select([], prompt="Directory entry", id="collection-entry")
            yield Select(
                [
                    (x, x)
                    for x in (
                        "target",
                        "cwd",
                        "workspace",
                        "source-report",
                        "workload",
                        "profiles",
                        "metadata",
                    )
                ],
                value="target",
                allow_blank=False,
                id="collection-destination",
            )
            yield Button("Use selected path", id="collection-use")
            yield Static("Static languages (comma-separated; auto alone)")
            yield Input(value="auto", id="collection-languages")
            yield Select(
                [(x, x) for x in ("lenient", "normal", "strict")],
                value="normal",
                allow_blank=False,
                id="collection-strictness",
            )
            yield Static("Exclude exact directory names, one per line (static only)")
            yield TextArea(id="collection-exclude")
            yield Static("Saved source report (required for capture; optional for import)")
            yield Input(
                placeholder="Saved source report, root-relative", id="collection-source-report"
            )
            yield Static("Import only: saved profile paths, one per line; browsing appends a path")
            yield TextArea(id="collection-profiles")
            yield Input(
                placeholder="Optional capture metadata, root-relative", id="collection-metadata"
            )
            yield Static(
                "Import never executes application code. Managed import requires an existing "
                "owned workspace. Missing capture/source facts remain partial. Without a static "
                "report, opening a verified import shows runtime-only evidence; source previews "
                "remain unavailable."
            )
            yield Static("Runtime execution settings (used only for profile)")
            yield Select(
                [(x, x) for x in ("script", "module")],
                value="script",
                allow_blank=False,
                id="collection-workload-kind",
            )
            yield Input(placeholder="Python script path or dotted module", id="collection-workload")
            yield Static("Arguments, one per line; internal empty lines preserve empty arguments")
            yield TextArea(id="collection-arguments")
            for name, value in (
                ("timeout", "300"),
                ("repeat", "1"),
                ("expected-status", "SUCCESS"),
            ):
                yield Static(name)
                yield Input(value=value, id=f"collection-{name}")
            yield Checkbox(
                "I acknowledge profiling executes untrusted Python", id="collection-allow"
            )
            yield Button("Build command", id="collection-build")
            yield Static("No command reviewed", id="collection-status")
            yield TextArea(read_only=True, id="collection-preview")
            yield Button("Copy command", id="collection-copy", disabled=True)
            if self.session is not None:
                from code_analysis.tui_collection_jobs import CollectionJobsPanel

                def open_index(index: BrowseIndex) -> None:
                    self.dismiss(index)

                yield CollectionJobsPanel(self.session, open_index)
            yield Button("Close", id="collection-close")

    def value(self, name: str) -> str:
        return self.query_one(f"#collection-{name}", Input).value

    def selection(self, name: str) -> str:
        return str(self.query_one(f"#collection-{name}", Select).value)

    def request(self) -> CollectionRequest:
        common = {
            "operation": self.selection("operation"),
            "interpreter_id": self.selection("interpreter"),
            "cwd": self.value("cwd"),
            "workspace": self.value("workspace") or None,
        }
        if common["operation"] == "import-profile":
            return CollectionRequest.model_validate(
                {
                    **common,
                    "profiles": tuple(
                        value.strip()
                        for value in self.query_one(
                            "#collection-profiles", TextArea
                        ).text.splitlines()
                        if value.strip()
                    ),
                    "metadata": self.value("metadata") or None,
                    "source_report": self.value("source-report") or None,
                }
            )
        if common["operation"] == "profile":
            text = self.query_one("#collection-arguments", TextArea).text
            return CollectionRequest.model_validate(
                {
                    **common,
                    "source_report": self.value("source-report"),
                    "workload_kind": self.selection("workload-kind"),
                    "workload": self.value("workload"),
                    "arguments": tuple(text.split("\n")) if text else (),
                    "timeout": int(self.value("timeout")),
                    "repeat": int(self.value("repeat")),
                    "expected_status": self.value("expected-status"),
                    "allow_execution": self.query_one("#collection-allow", Checkbox).value,
                }
            )
        return CollectionRequest.model_validate(
            {
                **common,
                "target": self.value("target"),
                "languages": tuple(x.strip() for x in self.value("languages").split(",")),
                "strictness": self.selection("strictness"),
                "exclude": tuple(self.query_one("#collection-exclude", TextArea).text.splitlines()),
            }
        )

    def invalidate(self) -> None:
        self.preview = None
        if self.session is not None:
            from code_analysis.tui_collection_jobs import CollectionJobsPanel

            self.query_one(CollectionJobsPanel).set_review(None, None)
        self.query_one("#collection-copy", Button).disabled = True
        self.query_one("#collection-preview", TextArea).load_text("")
        self.query_one("#collection-status", Static).update(
            "Settings changed; rebuild the command."
        )

    def on_input_changed(self, event: Input.Changed) -> None:
        self.invalidate()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id not in {"collection-entry", "collection-destination"}:
            self.invalidate()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        self.invalidate()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id != "collection-preview":
            self.invalidate()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        status = self.query_one("#collection-status", Static)
        try:
            match event.button.id:
                case "collection-build":
                    request = self.request()
                    self.preview = self.planner.preview(request)
                    if self.session is not None:
                        from code_analysis.tui_collection_jobs import CollectionJobsPanel

                        self.query_one(CollectionJobsPanel).set_review(request, self.preview)
                    self.query_one("#collection-preview", TextArea).load_text(self.preview.command)
                    self.query_one("#collection-copy", Button).disabled = False
                    status.update(
                        plain_text(
                            "Preview ready. Review sensitive arguments before copying.\n"
                            + "\n".join(self.preview.limitations)
                        )
                    )
                case "collection-list":
                    directory = self.planner.browse(self.value("directory"))
                    self.query_one("#collection-entry", Select).set_options(
                        [(f"{x.kind}: {x.name}", x.path) for x in directory.entries]
                    )
                    status.update(
                        f"{len(directory.entries)} entries; {directory.omitted} omitted; "
                        f"{directory.skipped} skipped. Enter a child path to browse further."
                    )
                case "collection-use":
                    selected = self.query_one("#collection-entry", Select).value
                    if selected is Select.BLANK:
                        raise ValueError("select an entry first")
                    if self.selection("destination") == "profiles":
                        profiles = self.query_one("#collection-profiles", TextArea)
                        values = tuple(value for value in profiles.text.splitlines() if value)
                        profiles.load_text("\n".join(dict.fromkeys((*values, str(selected)))))
                    else:
                        self.query_one(
                            f"#collection-{self.selection('destination')}", Input
                        ).value = str(selected)
                case "collection-copy":
                    if self.preview is not None:
                        self.app.copy_to_clipboard(self.preview.command)
                        status.update(
                            "Copy requested; terminal clipboard support is required. "
                            "Command remains visible."
                        )
                case "collection-close":
                    self.action_close()
        except (ValueError, OSError) as exc:
            self.preview = None
            self.query_one("#collection-copy", Button).disabled = True
            self.query_one("#collection-preview", TextArea).load_text("")
            reason = (
                "invalid collection options; check the selected fields"
                if isinstance(exc, ValidationError)
                else str(exc)
            )
            status.update(plain_text(f"Cannot prepare selection: {reason}"))

    def action_close(self) -> None:
        self.dismiss(None)

    def on_unmount(self) -> None:
        if self.session is None:
            self.planner.close()
