from __future__ import annotations

import errno
import os
import tomllib
from pathlib import Path

from code_analysis.config import AnalysisConfig, LanguageSelection
from code_analysis.context import AnalysisContext, SourceIdentity
from code_analysis.errors import ConfigurationError, InvalidArgumentError


def find_project_root(target: Path) -> Path:
    """Find the nearest project boundary without requiring a Git repository."""

    resolved = target.expanduser().resolve()
    current = resolved.parent if resolved.is_file() else resolved
    git_fallback: Path | None = None
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").is_file():
            configured_roots = _configured_source_roots(candidate)
            if (
                current == candidate
                or not configured_roots
                or any(current == root or root in current.parents for root in configured_roots)
            ):
                return candidate
            # A target outside this project's declared source roots is an independent
            # analysis target (for example, a synthetic fixture directory).
            return current
        if git_fallback is None and (candidate / ".git").exists():
            git_fallback = candidate
    return git_fallback or current


def discover_context(
    target: Path,
    config: AnalysisConfig,
    *,
    project_root: Path | None = None,
) -> AnalysisContext:
    resolved = target.expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(errno.ENOENT, "analysis target does not exist", str(target))
    if resolved.is_file() and resolved.suffix not in SUPPORTED_SOURCE_SUFFIXES:
        raise InvalidArgumentError(
            "a file target must have a .py, .js, .mjs, .cjs, .jsx, or .rb suffix"
        )

    physical_root = (
        project_root.expanduser().resolve()
        if project_root is not None
        else resolved.parent if resolved.is_file() else resolved
    )
    if resolved != physical_root and physical_root not in resolved.parents:
        raise InvalidArgumentError("analysis target must be within the project root")
    roots = (
        _explicit_source_roots(physical_root, config.source_roots)
        if config.source_roots
        else _configured_source_roots(physical_root)
    )
    if not roots:
        src = physical_root / "src"
        roots = (src,) if _contains_source(src) else (physical_root,)

    logical_roots = tuple(
        "." if root == physical_root else root.relative_to(physical_root).as_posix()
        for root in roots
    )
    return AnalysisContext(
        target=resolved,
        source=SourceIdentity(
            physical_root=physical_root,
            logical_root=".",
            source_roots=roots,
            logical_source_roots=logical_roots,
        ),
        config=config,
    )


def _explicit_source_roots(root: Path, values: tuple[str, ...]) -> tuple[Path, ...]:
    """Honor loaded configuration without silently dropping invalid selected roots."""
    roots: list[Path] = []
    for value in values:
        relative = Path(value)
        if (
            not value
            or relative.is_absolute()
            or ".." in relative.parts
            or "\\" in value
            or relative.as_posix() != value
            or any(ord(character) < 32 for character in value)
        ):
            raise ConfigurationError("source roots must be normalized relative directories")
        path = root / relative
        if any(
            (root / Path(*relative.parts[:index])).is_symlink()
            for index in range(1, len(relative.parts) + 1)
        ):
            raise ConfigurationError("explicit source roots must not traverse symlinks")
        if not path.is_dir() or path in roots:
            raise ConfigurationError("explicit source roots must be existing unique directories")
        roots.append(path)
    return tuple(roots)


def _configured_source_roots(root: Path) -> tuple[Path, ...]:
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return ()
    try:
        document = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return ()

    candidates: list[str] = []
    tool = document.get("tool", {})
    if isinstance(tool, dict):
        code_analysis = tool.get("code-analysis", {})
        if isinstance(code_analysis, dict):
            configured = code_analysis.get("source-roots", code_analysis.get("source_roots", ()))
            if isinstance(configured, list) and all(isinstance(item, str) for item in configured):
                candidates.extend(configured)
        setuptools = tool.get("setuptools", {})
        if isinstance(setuptools, dict):
            packages = setuptools.get("packages", {})
            if isinstance(packages, dict):
                find = packages.get("find", {})
                if isinstance(find, dict):
                    where = find.get("where", ())
                    if isinstance(where, list) and all(isinstance(item, str) for item in where):
                        candidates.extend(where)

    roots: list[Path] = []
    for candidate in candidates:
        path = (root / candidate).resolve()
        if (path == root or root in path.parents) and path.is_dir() and path not in roots:
            roots.append(path)
    return tuple(roots)


PYTHON_SUFFIXES = frozenset({".py"})
JAVASCRIPT_SUFFIXES = frozenset({".js", ".mjs", ".cjs", ".jsx"})
RUBY_SUFFIXES = frozenset({".rb"})
SUPPORTED_SOURCE_SUFFIXES = PYTHON_SUFFIXES | JAVASCRIPT_SUFFIXES | RUBY_SUFFIXES


def _contains_source(path: Path) -> bool:
    if not path.is_dir():
        return False
    return any(item.suffix in SUPPORTED_SOURCE_SUFFIXES for item in path.rglob("*"))


def iter_python_files(context: AnalysisContext) -> tuple[Path, ...]:
    if LanguageSelection.PYTHON not in selected_languages(context.config):
        return ()
    return _iter_source_files(context, PYTHON_SUFFIXES)


def iter_javascript_files(context: AnalysisContext) -> tuple[Path, ...]:
    if LanguageSelection.JAVASCRIPT not in selected_languages(context.config):
        return ()
    suffixes = JAVASCRIPT_SUFFIXES
    if not context.config.javascript.include_jsx:
        suffixes = suffixes - {".jsx"}
    return _iter_source_files(context, suffixes)


def iter_ruby_files(context: AnalysisContext) -> tuple[Path, ...]:
    if LanguageSelection.RUBY not in selected_languages(context.config):
        return ()
    return _iter_source_files(context, RUBY_SUFFIXES)


def selected_languages(config: AnalysisConfig) -> tuple[LanguageSelection, ...]:
    if config.languages == (LanguageSelection.AUTO,):
        return (LanguageSelection.PYTHON, LanguageSelection.JAVASCRIPT, LanguageSelection.RUBY)
    return config.languages


def iter_selected_source_files(context: AnalysisContext) -> tuple[Path, ...]:
    suffixes: set[str] = set()
    languages = selected_languages(context.config)
    if LanguageSelection.PYTHON in languages:
        suffixes.update(PYTHON_SUFFIXES)
    if LanguageSelection.JAVASCRIPT in languages:
        suffixes.update(JAVASCRIPT_SUFFIXES)
        if not context.config.javascript.include_jsx:
            suffixes.discard(".jsx")
    if LanguageSelection.RUBY in languages:
        suffixes.update(RUBY_SUFFIXES)
    return _iter_source_files(context, frozenset(suffixes))


def _iter_source_files(context: AnalysisContext, suffixes: frozenset[str]) -> tuple[Path, ...]:
    if context.target.is_file():
        return (context.target,) if context.target.suffix in suffixes else ()
    found: set[Path] = set()
    excluded = set(context.config.excludes)
    for source_root in context.source.source_roots:
        if context.target == context.source.physical_root or context.target in source_root.parents:
            scan_root = source_root
        elif context.target == source_root or source_root in context.target.parents:
            scan_root = context.target
        else:
            continue
        for dirpath, dirnames, filenames in os.walk(scan_root, followlinks=False):
            dirnames[:] = sorted(
                name for name in dirnames if name not in excluded and not name.startswith(".")
            )
            directory = Path(dirpath)
            for filename in sorted(filenames):
                path = directory / filename
                if path.suffix in suffixes and not path.is_symlink():
                    found.add(path)
    return tuple(sorted(found, key=lambda item: logical_path(context, item)))


def source_root_for(context: AnalysisContext, path: Path) -> Path:
    matches = [root for root in context.source.source_roots if path == root or root in path.parents]
    if not matches:
        raise ConfigurationError(f"source path is outside logical roots: {path.name}")
    return max(matches, key=lambda item: len(item.parts))


def logical_path(context: AnalysisContext, path: Path) -> str:
    return path.relative_to(context.source.physical_root).as_posix()


def module_name(context: AnalysisContext, path: Path) -> str:
    relative = path.relative_to(source_root_for(context, path)).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    if not parts:
        return path.parent.name
    return ".".join(parts)


def javascript_module_name(context: AnalysisContext, path: Path) -> str:
    """Return the portable source-root-relative module path, retaining its extension."""

    return path.relative_to(source_root_for(context, path)).as_posix()


def ruby_module_name(context: AnalysisContext, path: Path) -> str:
    """Return the portable source-root-relative Ruby module path with extension."""

    return path.relative_to(source_root_for(context, path)).as_posix()
