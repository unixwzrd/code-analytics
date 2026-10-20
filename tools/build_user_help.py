#!/usr/bin/env python3
"""Compile curated Markdown (--write), or check freshness with stdlib only (--check).

The check is intentionally independent of the package, Pydantic and the optional renderer.
Changing generator code, any selected source, configuration or assets requires regeneration.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import importlib.metadata
import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "src/code_analysis/user_help"
DOCUMENTS = (
    ("getting-started", "Getting started", "Start here", "README.md"),
    ("user-guide", "Web and terminal usage", "User guide", "docs/ui-guide.md"),
    ("cli-reference", "CLI reference", "Reference", "docs/cli.md"),
    ("examples", "CLI examples and artifacts", "Examples", "docs/cli-examples.md"),
    ("reports", "Report and diagram catalog", "Reference", "docs/report-and-diagram-catalog.md"),
)
CONFIGURATION = "commonmark;html=false;tables=true;highlight=false;links=closed-v1"
RENDERER = "markdown-it-py==4.0.0"
CSS = """/* Compiled local user help. No external fonts, scripts or imports. */
.help-document { color: inherit; font: inherit; line-height: 1.6; overflow-wrap: anywhere; }
.help-document h1,.help-document h2,.help-document h3 {
  line-height: 1.25; scroll-margin-top: 1rem;
}
.help-document pre {
  padding: .8rem; overflow: auto; background: color-mix(in srgb, currentColor 7%, transparent);
}
.help-document code { font-family: ui-monospace, monospace; }
.help-document table { border-collapse: collapse; display: block; overflow: auto; max-width: 100%; }
.help-document th,.help-document td {
  border: 1px solid currentColor; padding: .35rem .6rem; text-align: left;
}
.help-document th { overflow-wrap: normal; word-break: normal; }
.help-document a { color: inherit; text-decoration: underline; }
.help-document a:focus-visible { outline: 2px solid currentColor; outline-offset: 2px; }
"""


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def source_hashes() -> list[dict[str, str]]:
    return [
        {"name": name, "sha256": digest((ROOT / name).read_bytes())}
        for name in sorted({row[3] for row in DOCUMENTS})
    ]


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def check() -> None:
    """Reject stale/missing artifacts without importing any third-party package."""
    catalog = json.loads((OUTPUT / "index.json").read_bytes())
    if set(catalog) != {
        "schema_version",
        "renderer",
        "configuration",
        "generator_sha256",
        "sources",
        "assets",
        "documents",
    }:
        raise ValueError("unexpected help catalog fields")
    if (
        type(catalog["schema_version"]) is not int
        or catalog["schema_version"] != 1
        or catalog["renderer"] != RENDERER
        or catalog["configuration"] != CONFIGURATION
        or catalog["generator_sha256"] != digest(Path(__file__).read_bytes())
        or catalog["sources"] != source_hashes()
    ):
        raise ValueError("stale help sources/generator; run tools/build_user_help.py --write")
    names = sorted([f"{row[0]}.html" for row in DOCUMENTS] + ["styles.css"])
    if {path.name for path in OUTPUT.iterdir()} != {*names, "index.json"}:
        raise ValueError("unexpected files in compiled help directory")
    expected_assets = [
        {"name": name, "sha256": digest((OUTPUT / name).read_bytes())} for name in names
    ]
    if catalog["assets"] != expected_assets:
        raise ValueError("missing or changed compiled help asset")
    if [row["document_id"] for row in catalog["documents"]] != [row[0] for row in DOCUMENTS]:
        raise ValueError("invalid help document set")
    if (OUTPUT / "index.json").read_bytes() != json_bytes(catalog):
        raise ValueError("noncanonical help catalog")
    # Reconstruct metadata from compiled headings: catches edited index titles/anchors too.
    for document, definition in zip(catalog["documents"], DOCUMENTS, strict=True):
        rendered = (OUTPUT / f"{definition[0]}.html").read_text()
        headings = [
            {
                "anchor": anchor,
                "title": html.unescape(re.sub(r"<[^>]+>", "", title)),
                "level": int(level),
            }
            for level, anchor, title in re.findall(
                r'<h([1-6]) id="([a-z0-9_-]+)">(.*?)</h\1>', rendered
            )
        ]
        if document != {
            "document_id": definition[0],
            "title": definition[1],
            "category": definition[2],
            "headings": headings,
        }:
            raise ValueError("help heading metadata differs from compiled document")


def normalize_markdown(source: str) -> str:
    """Only CLI disclosure wrappers and exact safe explicit anchors are interpreted."""
    lines = []
    anchor = None
    fence = None
    for line in source.splitlines():
        fence_match = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence is not None:
            lines.append(line)
            if (
                fence_match
                and fence_match[1][0] == fence[0]
                and len(fence_match[1]) >= fence[1]
                and not fence_match[2].strip()
            ):
                fence = None
            continue
        if fence_match and not (fence_match[1][0] == "`" and "`" in fence_match[2]):
            fence = (fence_match[1][0], len(fence_match[1]))
            lines.append(line)
            continue
        explicit = re.fullmatch(r'<a id="([a-z0-9][a-z0-9_-]*)"></a>', line)
        if explicit:
            if anchor is not None:
                raise ValueError("consecutive explicit help anchors")
            anchor = explicit[1]
            continue
        if line in ("<details>", "</details>"):
            continue
        summary = re.fullmatch(r"<summary><code>([^<>]+)</code></summary>", line)
        if summary:
            line = f"### `{summary[1]}`"
        if anchor is not None and re.match(r"^#{1,6} ", line):
            line += f" {{#{anchor}}}"
            anchor = None
        elif anchor is not None and line.strip():
            raise ValueError("explicit anchor must precede a heading")
        lines.append(line)
    if anchor is not None:
        raise ValueError("dangling explicit help anchor")
    return "\n".join(lines) + "\n"


def slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "section"


def local_target(url: str, source_name: str) -> tuple[str, str] | None:
    decoded = unquote(html.unescape(url)).strip()
    parsed = urlsplit(decoded)
    if any(ord(char) < 32 for char in decoded) or "\\" in decoded:
        raise ValueError(f"unsafe help link: {url!r}")
    if parsed.scheme:
        if parsed.scheme.lower() not in ("https", "http", "mailto"):
            raise ValueError(f"unsafe help URL scheme: {url!r}")
        if parsed.scheme in ("http", "https") and not parsed.netloc:
            raise ValueError(f"invalid external help URL: {url!r}")
        return None
    if parsed.netloc or parsed.query or parsed.path.startswith("/"):
        raise ValueError(f"unsafe local help URL: {url!r}")
    path = str(Path(source_name).parent / parsed.path) if parsed.path else source_name
    mapping = {row[3]: row[0] for row in DOCUMENTS}
    document_id = mapping.get(path)
    return (document_id, parsed.fragment) if document_id else ("", "")


def compile_documents() -> dict[str, bytes]:
    # This is the sole optional dependency boundary; --check never reaches it.
    from markdown_it import MarkdownIt

    if importlib.metadata.version("markdown-it-py") != "4.0.0":
        raise ValueError("regeneration requires markdown-it-py==4.0.0")
    md = MarkdownIt("commonmark", {"html": False}).enable("table")
    # Inspect every URL ourselves, including schemes the default parser would leave literal.
    md.validateLink = lambda _url: True
    token_sets = []
    documents = []
    for document_id, title, category, source_name in DOCUMENTS:
        source = (ROOT / source_name).read_text()
        if document_id == "getting-started":
            source = (
                "# Getting started\n\n## Installation\n\n"
                + source.split("## Installation\n", 1)[1].split("## What a finding means\n", 1)[0]
            )
            # Installation excerpts are user-facing; checkout qualification belongs to development.
            source = re.sub(
                r"For development,.*?(?=The core dependency set)", "", source, flags=re.DOTALL
            )
        tokens = md.parse(normalize_markdown(source))
        headings = []
        used = set()
        for index, token in enumerate(tokens):
            if token.type != "heading_open":
                continue
            inline = tokens[index + 1]
            explicit = re.search(r" \{#([a-z0-9][a-z0-9_-]*)\}$", inline.content)
            if explicit:
                inline.content = inline.content[: explicit.start()]
                inline.children = md.parseInline(inline.content)[0].children
            heading_title = "".join(
                child.content
                for child in inline.children or []
                if child.type in ("text", "code_inline")
            )
            anchor = explicit[1] if explicit else slug(heading_title)
            original = anchor
            suffix = 1
            while anchor in used and not explicit:
                anchor = f"{original}-{suffix}"
                suffix += 1
            if anchor in used:
                raise ValueError(f"duplicate explicit help anchor: {anchor}")
            used.add(anchor)
            token.attrSet("id", anchor)
            headings.append({"anchor": anchor, "title": heading_title, "level": int(token.tag[1])})
        documents.append(
            {"document_id": document_id, "title": title, "category": category, "headings": headings}
        )
        token_sets.append((document_id, source_name, tokens))
    anchors = {
        doc["document_id"]: {heading["anchor"] for heading in doc["headings"]} for doc in documents
    }
    assets = {"styles.css": CSS.encode()}
    for document_id, source_name, tokens in token_sets:
        for block in tokens:
            stack = []
            for token in block.children or []:
                if token.type == "image":
                    token.type, token.tag, token.content = "text", "", token.content
                if token.type == "link_open":
                    target = local_target(token.attrGet("href") or "", source_name)
                    excluded = target == ("", "")
                    stack.append(excluded)
                    if excluded:
                        token.tag, token.attrs = "span", {"class": "help-unbundled-reference"}
                    elif target is None:
                        token.attrSet("target", "_blank")
                        token.attrSet("rel", "noopener noreferrer")
                    else:
                        target_id, anchor = target
                        if anchor and anchor not in anchors[target_id]:
                            raise ValueError(f"unresolved bundled help anchor: {target}")
                        token.attrSet(
                            "href", f"/help/{target_id}.html" + (f"#{anchor}" if anchor else "")
                        )
                elif token.type == "link_close" and stack.pop():
                    token.tag = "span"
        body = md.renderer.render(tokens, md.options, {})
        title = next(d["title"] for d in documents if d["document_id"] == document_id)
        assets[f"{document_id}.html"] = (
            '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{html.escape(title)}</title>"
            '<link rel="stylesheet" href="/help/styles.css"></head><body>'
            f'<main class="help-document">{body}</main></body></html>\n'
        ).encode()
    assets["index.json"] = json_bytes(
        {
            "schema_version": 1,
            "renderer": RENDERER,
            "configuration": CONFIGURATION,
            "generator_sha256": digest(Path(__file__).read_bytes()),
            "sources": source_hashes(),
            "assets": [
                {"name": name, "sha256": digest(payload)}
                for name, payload in sorted(assets.items())
            ],
            "documents": documents,
        }
    )
    return assets


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    args = parser.parse_args()
    try:
        if args.write:
            assets = compile_documents()
            OUTPUT.mkdir(parents=True, exist_ok=True)
            for name, payload in assets.items():
                (OUTPUT / name).write_bytes(payload)
        check()
    except (OSError, ValueError, KeyError, TypeError, ImportError) as exc:
        print(f"User help build failed: {exc}", file=sys.stderr)
        return 1
    print("Bundled user help is current (5 documents).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
