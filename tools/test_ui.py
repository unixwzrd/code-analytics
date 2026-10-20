#!/usr/bin/env python3
"""Repeatable local UI gate; no dependency or browser installation is performed."""

from __future__ import annotations

import argparse
import errno
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from ui_suite import UiExecutionEvidence, UiTestModule, discover_ui_modules  # noqa: E402

CAPTURE_BYTE_LIMIT = 64 * 1024 * 1024
CAPTURE_FILE_LIMIT = 5000


class Record(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class FileDigest(Record):
    path: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class BrowserIdentity(Record):
    name: Literal["chromium", "webkit"]
    version: str


class Counts(Record):
    tests: int = Field(ge=0)
    failures: int = Field(ge=0)
    errors: int = Field(ge=0)
    skipped: int = Field(ge=0)

    @model_validator(mode="after")
    def reconcile(self) -> Counts:
        if self.failures + self.errors + self.skipped > self.tests:
            raise ValueError("JUnit outcome counts exceed the test count")
        return self


class UiTestRun(Record):
    schema_name: Literal["code-analysis-ui-test-run"] = "code-analysis-ui-test-run"
    schema_version: Literal["1.0.0"] = "1.0.0"
    scope: Literal["full", "smoke"]
    status: Literal["passed", "failed", "incomplete", "interrupted"]
    interpreter: str
    python_version: str
    pytest_version: str | None
    playwright_version: str | None
    started_at: str
    finished_at: str
    browsers: tuple[BrowserIdentity, ...]
    test_modules: tuple[str, ...]
    command: tuple[str, ...]
    pytest_exit_code: int | None
    counts: Counts | None
    execution: UiExecutionEvidence | None
    issues: tuple[str, ...]
    input_count: int
    artifacts: tuple[FileDigest, ...]

    @model_validator(mode="after")
    def reconcile(self) -> UiTestRun:
        if self.command[0:1] != (self.interpreter,):
            raise ValueError("test command must use the recorded interpreter")
        if self.status == "passed" and (
            self.counts is None
            or self.pytest_exit_code != 0
            or result_status(self.counts, self.pytest_exit_code) != "passed"
            or {item.name for item in self.browsers} != {"chromium", "webkit"}
            or len(self.browsers) != 2
            or self.issues
            or self.execution is None
            or not self.execution.cases
            or len(self.execution.cases) != self.counts.tests
            or set(self.execution.expected_node_ids)
            != {item.node_id for item in self.execution.cases}
            or any(item.outcome != "passed" for item in self.execution.cases)
            or not any(item.direct_browser_check for item in self.execution.cases)
        ):
            raise ValueError("passed UI receipt requires complete observed qualification")
        return self


def snapshot(root: Path) -> tuple[FileDigest, ...]:
    """Bind source, resources and trusted tests; exclude runtime caches/artifacts."""
    excluded = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv", ".code-analysis"}
    paths = [root / name for name in ("pyproject.toml", "MANIFEST.in")]
    for name in ("src", "tests", "tools", ".github"):
        for directory, children, files in os.walk(root / name, followlinks=False):
            children[:] = sorted(
                child
                for child in children
                if child not in excluded and not (Path(directory) / child).is_symlink()
            )
            paths.extend(Path(directory) / file for file in sorted(files))
    return tuple(
        FileDigest(
            path=path.relative_to(root).as_posix(),
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(paths)
        if path.is_file() and not path.is_symlink()
    )


@dataclass(frozen=True, slots=True)
class CaptureStamp:
    modified_ns: int
    size: int
    sha256: str


def read_capture(path: Path) -> tuple[CaptureStamp, bytes]:
    before = path.stat()
    with path.open("rb") as source:
        payload = source.read(CAPTURE_BYTE_LIMIT + 1)
    after = path.stat()
    if len(payload) > CAPTURE_BYTE_LIMIT:
        raise ValueError("capture exceeds 64 MiB bound")
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ValueError("capture changed while being retained")
    return (
        CaptureStamp(after.st_mtime_ns, len(payload), hashlib.sha256(payload).hexdigest()),
        payload,
    )


def capture_paths(root: Path) -> tuple[Path, ...]:
    paths = tuple(
        sorted(
            path
            for path in root.rglob("*")
            if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".svg"}
            and path.is_file()
            and not path.is_symlink()
        )
    )
    if len(paths) > CAPTURE_FILE_LIMIT:
        raise ValueError("capture inventory exceeds 5,000-file bound")
    return paths


def retain_captures(root: Path, run: Path, before: dict[str, CaptureStamp]) -> None:
    for path in capture_paths(root):
        relative = path.relative_to(root)
        stamp, payload = read_capture(path)
        if stamp != before.get(relative.as_posix()):
            target = run / "captures" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)


def read_counts(path: Path) -> Counts:
    root = ET.parse(path).getroot()
    suites = (root,) if root.tag == "testsuite" else tuple(root.findall("testsuite"))
    if not suites:
        raise ValueError("JUnit contains no test suite")
    return Counts(
        **{
            key: sum(int(suite.attrib[key]) for suite in suites)
            for key in ("tests", "failures", "errors", "skipped")
        }
    )


def result_status(counts: Counts, returncode: int) -> Literal["passed", "failed", "incomplete"]:
    if counts.failures or counts.errors:
        return "failed"
    if counts.skipped or not counts.tests:
        return "incomplete"
    return "passed" if returncode == 0 else "failed"


def probe_browsers() -> tuple[BrowserIdentity, ...]:
    from playwright.sync_api import sync_playwright

    identities = []
    with sync_playwright() as runtime:
        for name in ("chromium", "webkit"):
            browser = getattr(runtime, name).launch(timeout=15_000)
            try:
                identities.append(BrowserIdentity(name=name, version=browser.version))
            finally:
                browser.close()
    return tuple(identities)


def run_pytest(command: tuple[str, ...], log: Path) -> int:
    with (
        log.open("w", encoding="utf-8") as stream,
        subprocess.Popen(
            command,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            bufsize=1,
            env={key: value for key, value in os.environ.items() if key != "PYTEST_ADDOPTS"},
        ) as process,
    ):
        try:
            assert process.stdout is not None
            for line in process.stdout:
                print(line, end="", flush=True)
                stream.write(line)
                stream.flush()
            return process.wait()
        except KeyboardInterrupt:
            # The terminal already delivers SIGINT to its foreground group.
            # Give pytest time to unwind its synthetic owned-job fixtures.
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            raise


def pytest_command(modules: tuple[UiTestModule, ...], run: Path, smoke: bool) -> tuple[str, ...]:
    return (
        sys.executable,
        "-m",
        "pytest",
        *(f"tests/{module.name}" for module in modules),
        "-m",
        "ui_smoke" if smoke else "ui",
        "-q",
        "-ra",
        "--strict-config",
        "--strict-markers",
        "-o",
        "addopts=",
        "-o",
        "xfail_strict=true",
        f"--junitxml={run / 'junit.xml'}",
        f"--ui-evidence={run / 'cases.json'}",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--smoke", action="store_true", help="Run critical journeys, not full qualification"
    )
    parser.add_argument(
        "--list", action="store_true", help="List UI modules without launching browsers"
    )
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output/ui-tests")
    args = parser.parse_args(argv)
    modules = discover_ui_modules(ROOT / "tests")
    if args.list:
        for module in modules:
            category = "browser + integration" if module.browser else "support contracts"
            print(f"{category:22} tests/{module.name}")
        return 0
    output = args.output_dir.resolve()
    if any(output.is_relative_to(ROOT / name) for name in ("src", "tests", "tools", ".github")):
        parser.error("test artifacts must be outside source/test/tool directories")
    output.mkdir(parents=True, exist_ok=True)
    started = datetime.now(UTC)
    run = Path(tempfile.mkdtemp(prefix=started.strftime("%Y%m%dT%H%M%S.%fZ-"), dir=output))
    marker = "ui_smoke" if args.smoke else "ui"
    command = pytest_command(modules, run, args.smoke)
    print(f"UI {marker} gate: {len(modules)} modules; artifacts: {run}", flush=True)
    browsers, issues = (), []
    counts, returncode, execution = None, None, None
    status = "incomplete"
    captures_before: dict[str, CaptureStamp] | None = None
    before = snapshot(ROOT)
    (run / "inputs-before.json").write_text(
        json.dumps([record.model_dump() for record in before], indent=2) + "\n", encoding="utf-8"
    )
    try:
        if not any(module.browser_tests for module in modules):
            raise ValueError(
                "No direct browser test cases discovered; support tests cannot qualify the UI."
            )
        capture_root = ROOT / "output/playwright"
        captures_before = {
            path.relative_to(capture_root).as_posix(): read_capture(path)[0]
            for path in capture_paths(capture_root)
        }
        browsers = probe_browsers()
        returncode = run_pytest(command, run / "pytest.log")
        counts = read_counts(run / "junit.xml")
        status = result_status(counts, returncode)
        execution = UiExecutionEvidence.model_validate_json((run / "cases.json").read_text())
        actual = {item.node_id for item in execution.cases}
        if not execution.cases or set(execution.expected_node_ids) != actual:
            issues.append(
                "Expected and selected UI test identities differ; selection is incomplete."
            )
        if len(execution.cases) != counts.tests:
            issues.append("JUnit and UI execution-evidence case counts do not reconcile.")
        if not any(
            item.direct_browser_check and item.outcome == "passed" for item in execution.cases
        ):
            issues.append("No declared direct-browser case completed successfully.")
        if any(item.outcome != "passed" for item in execution.cases):
            issues.append("Some selected UI test cases did not pass; inspect cases.json.")
        if any(item.outcome == "failed" for item in execution.cases):
            status = "failed"
        elif issues and status == "passed":
            status = "incomplete"
        if status == "incomplete":
            issues.append(
                "Skipped/expected-failure or zero tests cannot qualify the UI; "
                "inspect JUnit and log."
            )
    except KeyboardInterrupt:
        status = "interrupted"
        issues.append("Interrupted; this run does not qualify the UI.")
    except Exception as error:
        # This is the outer test-tool boundary, not analytical provider recovery.
        issues.append(f"{type(error).__name__}: {str(error)[:2000]}")
        if status == "passed":
            status = "incomplete"
    if captures_before is not None:
        try:
            retain_captures(ROOT / "output/playwright", run, captures_before)
        except Exception as error:
            issues.append(f"Capture retention failed: {type(error).__name__}: {str(error)[:2000]}")
            if status == "passed":
                status = "incomplete"
    after = snapshot(ROOT)
    (run / "inputs-after.json").write_text(
        json.dumps([record.model_dump() for record in after], indent=2) + "\n", encoding="utf-8"
    )
    if after != before:
        issues.append(
            "Source/test/resource inputs changed during this run; "
            "results are not a fixed-snapshot qualification."
        )
        if status == "passed":
            status = "incomplete"
    artifacts = tuple(
        FileDigest(
            path=path.relative_to(run).as_posix(),
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(run.rglob("*"))
        if path.is_file()
    )
    receipt = UiTestRun(
        scope="smoke" if args.smoke else "full",
        status=status,
        interpreter=sys.executable,
        python_version=platform.python_version(),
        pytest_version=distribution_version("pytest"),
        playwright_version=distribution_version("playwright"),
        started_at=started.isoformat(),
        finished_at=datetime.now(UTC).isoformat(),
        browsers=browsers,
        test_modules=tuple(module.name for module in modules),
        command=command,
        pytest_exit_code=returncode,
        counts=counts,
        execution=execution,
        issues=tuple(issues),
        input_count=len(before),
        artifacts=artifacts,
    )
    (run / "result.json").write_text(receipt.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(f"\nUI {receipt.scope}: {receipt.status}; receipt: {run / 'result.json'}", flush=True)
    for issue in issues:
        print(issue, file=sys.stderr)
    return {
        "passed": 0,
        "failed": errno.EPROTO,
        "incomplete": errno.ENODATA,
        "interrupted": errno.EINTR,
    }[receipt.status]


def distribution_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


if __name__ == "__main__":
    raise SystemExit(main())
