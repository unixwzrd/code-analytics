"""Bounded, privacy-preserving Git history facts."""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from code_analysis.report_models import ChangeKind, CommitCategory, GitPathScope


@dataclass(frozen=True, slots=True)
class GitPathFact:
    path: str
    kind: ChangeKind
    previous_path: str | None
    additions: int | None
    deletions: int | None


@dataclass(frozen=True, slots=True)
class GitCommitFact:
    commit_id: str
    authored_date: str
    contributor_pseudonym: str
    category: CommitCategory
    paths: tuple[GitPathFact, ...]
    bulk_change: bool


@dataclass(frozen=True, slots=True)
class GitHistoryFacts:
    git_version: str
    head_commit_id: str
    path_scope: GitPathScope
    tracked_files: int
    shallow: bool
    commits: tuple[GitCommitFact, ...]
    truncated: bool


class GitUnavailableError(RuntimeError):
    pass


class GitHistoryError(RuntimeError):
    pass


def collect_git_history(
    project_root: Path, *, maximum_commits: int, path_scope: GitPathScope
) -> GitHistoryFacts:
    if not (project_root / ".git").exists():
        raise GitUnavailableError("target root is not a Git worktree root")
    version = _git(project_root, "--version").strip()
    top = _git(project_root, "rev-parse", "--show-toplevel").strip()
    if not top:
        raise GitUnavailableError("Git repository was not found")
    repository_root = Path(top).resolve()
    try:
        prefix = project_root.resolve().relative_to(repository_root).as_posix()
    except ValueError as exc:
        raise GitUnavailableError("project root is outside the Git repository") from exc
    prefix = "" if prefix == "." else prefix.rstrip("/") + "/"
    head = _git(repository_root, "rev-parse", "HEAD").strip()
    tracked_raw = _git_bytes(repository_root, "ls-files", "-z")
    tracked_paths = tuple(item.decode("utf-8") for item in tracked_raw.split(b"\0") if item)
    relevant_tracked = tuple(
        item
        for item in tracked_paths
        if (not prefix or item.startswith(prefix)) and _path_selected(item, path_scope)
    )
    shallow = _git(repository_root, "rev-parse", "--is-shallow-repository").strip() == "true"
    pathspecs = ((f":(glob){prefix}**/*.py",) if path_scope is GitPathScope.PYTHON else ()) or (
        (prefix.rstrip("/"),) if prefix else ()
    )
    raw = _git_bytes(
        repository_root,
        "log",
        "--no-merges",
        "-z",
        f"--max-count={maximum_commits + 1}",
        "--format=%x1e%H%x1f%aI%x1f%ae%x1f%s",
        "--name-status",
        "-M",
        "--",
        *pathspecs,
    )
    parsed = _parse_log(raw, repository_root, prefix, len(relevant_tracked), path_scope)
    return GitHistoryFacts(
        git_version=version,
        head_commit_id=head,
        path_scope=path_scope,
        tracked_files=len(relevant_tracked),
        shallow=shallow,
        commits=parsed[:maximum_commits],
        truncated=len(parsed) > maximum_commits,
    )


def _parse_log(
    raw: bytes,
    repository_root: Path,
    prefix: str,
    tracked_count: int,
    path_scope: GitPathScope,
) -> tuple[GitCommitFact, ...]:
    commits: list[GitCommitFact] = []
    for chunk in raw.split(b"\x1e"):
        if not chunk:
            continue
        tokens = chunk.split(b"\0")
        header = tokens[0].decode("utf-8").strip("\n")
        fields = header.split("\x1f", 3)
        if len(fields) != 4:
            raise ValueError("malformed Git log header")
        commit_id, authored, email, subject = fields
        statuses = tokens[1:]
        path_rows: list[tuple[str, str | None, ChangeKind]] = []
        index = 0
        while index < len(statuses):
            status = statuses[index].decode("utf-8").strip()
            index += 1
            if not status:
                continue
            if index >= len(statuses):
                raise ValueError("malformed Git name-status entry")
            old_or_path = statuses[index].decode("utf-8")
            index += 1
            if status.startswith("R"):
                if index >= len(statuses):
                    raise ValueError("malformed Git rename entry")
                new_path = statuses[index].decode("utf-8")
                index += 1
                path_rows.append((new_path, old_or_path, ChangeKind.RENAMED))
            else:
                kind = {
                    "A": ChangeKind.ADDED,
                    "D": ChangeKind.REMOVED,
                    "M": ChangeKind.MODIFIED,
                }.get(status[:1], ChangeKind.MODIFIED)
                path_rows.append((old_or_path, None, kind))
        number_stats = _numstat(repository_root, commit_id)
        paths: list[GitPathFact] = []
        for path, previous, kind in path_rows:
            scoped = _scoped_change(path, previous, kind, path_scope)
            if scoped is None:
                continue
            path, previous, kind = scoped
            if prefix and not path.startswith(prefix):
                continue
            portable = _portable_path(path[len(prefix) :] if prefix else path)
            portable_previous = (
                None
                if previous is None
                else (
                    _portable_path(previous[len(prefix) :] if prefix else previous)
                    if not prefix or previous.startswith(prefix)
                    else "repository-external"
                )
            )
            additions, deletions = number_stats.get(path, (None, None))
            paths.append(
                GitPathFact(
                    path=portable,
                    kind=kind,
                    previous_path=portable_previous,
                    additions=additions,
                    deletions=deletions,
                )
            )
        unique_paths = {item.path for item in paths}
        bulk = len(unique_paths) > 100 or (
            tracked_count > 0 and len(unique_paths) / tracked_count > 0.20
        )
        category = _category(subject)
        if category is CommitCategory.FORMATTING and not _formatting_only(
            repository_root, commit_id, paths, prefix
        ):
            category = CommitCategory.UNKNOWN
        commits.append(
            GitCommitFact(
                commit_id=commit_id,
                authored_date=authored[:10],
                contributor_pseudonym=hashlib.sha256(email.strip().lower().encode()).hexdigest(),
                category=category,
                paths=tuple(sorted(paths, key=lambda item: (item.path, item.previous_path or ""))),
                bulk_change=bulk,
            )
        )
    return tuple(commits)


def _path_selected(path: str, path_scope: GitPathScope) -> bool:
    return path_scope is GitPathScope.REPOSITORY or path.endswith(".py")


def _scoped_change(
    path: str,
    previous: str | None,
    kind: ChangeKind,
    path_scope: GitPathScope,
) -> tuple[str, str | None, ChangeKind] | None:
    if path_scope is GitPathScope.REPOSITORY:
        return path, previous, kind
    current_python = path.endswith(".py")
    previous_python = previous is not None and previous.endswith(".py")
    if current_python and previous_python:
        return path, previous, kind
    if current_python:
        return path, None, ChangeKind.ADDED if previous is not None else kind
    if previous_python and previous is not None:
        return previous, None, ChangeKind.REMOVED
    return None


def _numstat(repository_root: Path, commit_id: str) -> dict[str, tuple[int | None, int | None]]:
    raw = _git_bytes(repository_root, "show", "--format=", "--numstat", "-z", commit_id)
    result: dict[str, tuple[int | None, int | None]] = {}
    tokens = [item for item in raw.split(b"\0") if item]
    index = 0
    while index < len(tokens):
        token = tokens[index].decode("utf-8").lstrip("\n")
        index += 1
        parts = token.split("\t", 2)
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        if not path and index + 1 < len(tokens):
            index += 1
            path = tokens[index].decode("utf-8")
            index += 1
        result[path] = (
            int(added) if added.isdigit() else None,
            int(deleted) if deleted.isdigit() else None,
        )
    return result


def _formatting_only(
    repository_root: Path, commit_id: str, paths: list[GitPathFact], prefix: str
) -> bool:
    if not paths or any(item.kind is not ChangeKind.MODIFIED for item in paths):
        return False
    python_paths = [item.path for item in paths if item.path.endswith(".py")]
    if len(python_paths) != len(paths):
        return False
    for path in python_paths:
        repository_path = prefix + path
        try:
            before = _git_bytes(repository_root, "show", f"{commit_id}^:{repository_path}")
            after = _git_bytes(repository_root, "show", f"{commit_id}:{repository_path}")
        except subprocess.CalledProcessError:
            return False
        if _significant_python(before) != _significant_python(after):
            return False
    return True


def _significant_python(source: bytes) -> tuple[tuple[int, str], ...]:
    import io
    import tokenize

    try:
        tokens = tokenize.tokenize(io.BytesIO(source).readline)
        return tuple(
            (token.type, token.string)
            for token in tokens
            if token.type
            not in {
                tokenize.ENCODING,
                tokenize.COMMENT,
                tokenize.INDENT,
                tokenize.DEDENT,
                tokenize.NL,
                tokenize.NEWLINE,
                tokenize.ENDMARKER,
            }
        )
    except (SyntaxError, tokenize.TokenError, UnicodeError):
        return ()


def _category(subject: str) -> CommitCategory:
    lowered = subject.strip().lower()
    if lowered.startswith(("revert", "rollback")):
        return CommitCategory.REVERT
    if lowered.startswith(("fix", "bug", "repair", "hotfix")):
        return CommitCategory.FIX
    if lowered.startswith(("feat", "feature", "add")):
        return CommitCategory.FEATURE
    if lowered.startswith(("refactor", "cleanup")):
        return CommitCategory.REFACTOR
    if lowered.startswith(("format", "style", "black", "ruff")):
        return CommitCategory.FORMATTING
    if lowered.startswith(("doc", "readme")):
        return CommitCategory.DOCUMENTATION
    return CommitCategory.UNKNOWN


def _portable_path(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not value:
        raise ValueError("Git path is not portable")
    return path.as_posix()


def _git(cwd: Path, *arguments: str) -> str:
    return _git_bytes(cwd, *arguments).decode("utf-8")


def _git_bytes(cwd: Path, *arguments: str) -> bytes:
    try:
        result = subprocess.run(
            ("git", "-C", str(cwd), *arguments),
            check=True,
            capture_output=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise GitUnavailableError("Git command is unavailable for this target") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitHistoryError("Git history command timed out") from exc
    return result.stdout
