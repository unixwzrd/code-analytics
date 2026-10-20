"""Offline compiled-help contracts and development-only regeneration boundaries."""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from pydantic import ValidationError

from code_analysis.user_help_models import DOCUMENT_IDS, HelpCatalog
from code_analysis.user_help_store import (
    HelpUnavailableError,
    load_help_catalog,
    read_help_asset,
)

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("build_user_help", ROOT / "tools/build_user_help.py")
assert SPEC is not None and SPEC.loader is not None
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


def test_catalog_is_closed_strict_frozen_and_all_assets_are_readable() -> None:
    catalog = load_help_catalog()
    assert tuple(document.document_id for document in catalog.documents) == DOCUMENT_IDS
    assert len(catalog.assets) == 6
    for asset in catalog.assets:
        resource = read_help_asset(asset.name)
        assert builder.digest(resource.payload) == asset.sha256
        with pytest.raises(FrozenInstanceError):
            resource.payload = b"changed"  # type: ignore[misc]
    assert read_help_asset("index.json").content_type == "application/json; charset=utf-8"
    with pytest.raises(ValidationError):
        catalog.renderer = "other"  # type: ignore[misc]
    for changes in ({"schema_version": True}, {"surprise": "extra"}, {"documents": []}):
        payload = json.loads(read_help_asset("index.json").payload)
        payload.update(changes)
        with pytest.raises(ValidationError):
            HelpCatalog.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    "name",
    [
        "../README.md",
        "/etc/passwd",
        "user-guide.html?x=1",
        "user-guide.html/",
        "INDEX.JSON",
        "private.html",
        "",
    ],
)
def test_closed_resource_allowlist(name: str) -> None:
    with pytest.raises(HelpUnavailableError, match="Unknown"):
        read_help_asset(name)


def test_catalog_and_asset_corruption_are_local_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    import code_analysis.user_help_store as store

    original = store._read
    monkeypatch.setattr(
        store, "_read", lambda name: b"broken" if name == "index.json" else original(name)
    )
    with pytest.raises(HelpUnavailableError, match="catalog"):
        load_help_catalog()
    monkeypatch.setattr(
        store, "_read", lambda name: b"changed" if name == "styles.css" else original(name)
    )
    with pytest.raises(HelpUnavailableError, match="integrity"):
        load_help_catalog()


def test_bounded_resource_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import code_analysis.user_help_store as store

    resources = tmp_path / "user_help"
    resources.mkdir()
    (resources / "index.json").write_bytes(b" " * (1024 * 1024 + 1))
    monkeypatch.setattr(store, "files", lambda _: tmp_path)
    with pytest.raises(HelpUnavailableError, match="size limit"):
        load_help_catalog()


def test_generation_is_deterministic_and_current() -> None:
    pytest.importorskip("markdown_it")
    first = builder.compile_documents()
    assert first == builder.compile_documents()
    assert set(first) == {"index.json", "styles.css", *[f"{name}.html" for name in DOCUMENT_IDS]}
    assert all((builder.OUTPUT / name).read_bytes() == payload for name, payload in first.items())
    builder.check()


def test_all_clickable_local_links_and_index_anchors_resolve() -> None:
    catalog = load_help_catalog()
    known = {
        f"/help/{doc.document_id}.html": {heading.anchor for heading in doc.headings}
        for doc in catalog.documents
    }
    for document in catalog.documents:
        content = read_help_asset(f"{document.document_id}.html").payload.decode()
        assert '<main class="help-document">' in content
        assert not re.search(r"<(script|iframe|img|details|summary)\b", content)
        for target in re.findall(r'href="([^"]+)"', content):
            url = urlsplit(target)
            if url.scheme:
                assert url.scheme in ("https", "http", "mailto")
                continue
            if url.path == "/help/styles.css":
                continue
            assert url.path in known
            assert not url.fragment or url.fragment in known[url.path]
        assert {heading.anchor for heading in document.headings} == set(
            re.findall(r'<h[1-6] id="([^"]+)"', content)
        )
        assert "ux-workflow-refinement-plan.md" not in content
        assert "/Users/" not in content


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,evil",
        "file:///etc/passwd",
        "vbscript:evil",
        "//evil.example",
        "/etc/passwd",
        "java%73cript:alert(1)",
        "https://good.example/\nattack",
    ],
)
def test_unsafe_links_are_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        builder.local_target(url, "docs/ui-guide.md")


def test_raw_html_is_escaped_and_cli_anchors_become_headings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("markdown_it")
    source = tmp_path / "docs"
    source.mkdir()
    for _, _, _, name in builder.DOCUMENTS:
        target = tmp_path / name
        target.write_text((ROOT / name).read_text())
    guide = source / "ui-guide.md"
    guide.write_text(
        guide.read_text() + '\n<script>alert("bad")</script>\n\n'
        "<img src=x onerror=alert(1)>\n\n"
        '<a id="explicit-safe"></a>\n## Safe anchor\n\n'
        "```mermaid\ngraph TD; A-->B\n```\n"
    )
    monkeypatch.setattr(builder, "ROOT", tmp_path)
    payloads = builder.compile_documents()
    content = payloads["user-guide.html"].decode()
    assert "&lt;script&gt;" in content and "&lt;img" in content
    assert "<script>" not in content and "<img " not in content
    assert '<h2 id="explicit-safe">Safe anchor</h2>' in content
    assert 'class="language-mermaid"' in content
    assert '<h3 id="command-workspace-prune">' in payloads["cli-reference.html"].decode()
    guide.write_text(guide.read_text() + "\n[bad](javascript:alert(1))\n")
    with pytest.raises(ValueError, match="unsafe"):
        builder.compile_documents()


def test_disclosure_normalization_does_not_rewrite_nested_fenced_examples() -> None:
    source = (
        "\n".join(
            (
                "````markdown",
                "```",
                '<a id="literal-code"></a>',
                "## Example",
                "<details>",
                "```not-a-closing-fence",
                "</details>",
                "````",
            )
        )
        + "\n"
    )
    assert builder.normalize_markdown(source) == source


@pytest.mark.parametrize("field", ["title", "category"])
def test_help_document_labels_are_nonempty(field: str) -> None:
    payload = json.loads(read_help_asset("index.json").payload)
    payload["documents"][0][field] = ""
    with pytest.raises(ValidationError):
        HelpCatalog.model_validate_json(json.dumps(payload))


def test_stdlib_check_and_source_asset_staleness(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-S", str(ROOT / "tools/build_user_help.py"), "--check"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    for name in [
        "tools/build_user_help.py",
        "README.md",
        *[row[3] for row in builder.DOCUMENTS[1:]],
    ]:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    destination = tmp_path / "src/code_analysis/user_help"
    shutil.copytree(builder.OUTPUT, destination)
    command = [sys.executable, "-S", str(tmp_path / "tools/build_user_help.py"), "--check"]
    assert subprocess.run(command, capture_output=True).returncode == 0
    source = tmp_path / "README.md"
    source.write_bytes(source.read_bytes() + b"\nchanged\n")
    assert subprocess.run(command, capture_output=True).returncode == 1
    shutil.copyfile(ROOT / "README.md", source)
    (destination / "styles.css").write_bytes(b"changed")
    assert subprocess.run(command, capture_output=True).returncode == 1
    (destination / "styles.css").unlink()
    assert subprocess.run(command, capture_output=True).returncode == 1


def test_runtime_does_not_import_renderer_or_read_sources() -> None:
    code = """
import sys
class NoRenderer:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('markdown_it', 'mdit_py_plugins')):
            raise AssertionError('runtime imported optional renderer')
sys.meta_path.insert(0, NoRenderer())
from code_analysis.user_help_store import load_help_catalog, read_help_asset
assert len(load_help_catalog().documents) == 5
assert read_help_asset('user-guide.html').payload
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_cli_command_and_flag_references_match_parser() -> None:
    from code_analysis.cli import build_parser

    parser = build_parser()
    leaves: dict[str, argparse.ArgumentParser] = {}

    def walk(current: argparse.ArgumentParser, prefix: str) -> None:
        actions = [
            action for action in current._actions if isinstance(action, argparse._SubParsersAction)
        ]
        if not actions:
            leaves[prefix] = current
        for action in actions:
            for name, child in action.choices.items():
                walk(child, f"{prefix} {name}")

    walk(parser, "code-analysis")
    source = (ROOT / "docs/cli.md").read_text()
    sections = re.split(r"<summary><code>([^<>]+)</code></summary>", source)
    assert len(sections) > 1
    for command, body in zip(sections[1::2], sections[2::2], strict=True):
        assert command in leaves, command
        flags = set(re.findall(r"(?<![\w-])--[a-z][a-z0-9-]*", body.split("</details>", 1)[0]))
        known = {flag for action in leaves[command]._actions for flag in action.option_strings}
        assert flags <= known, (command, flags - known)
