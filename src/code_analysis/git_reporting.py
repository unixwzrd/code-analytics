"""Build explicit, bounded Git change evidence without scanning source."""

from __future__ import annotations

import hashlib
import itertools
import json

from code_analysis import __version__
from code_analysis.analysis.git_history import (
    GitHistoryError,
    GitUnavailableError,
    collect_git_history,
)
from code_analysis.change_models import ChangeEvidenceSource, ChangeReport
from code_analysis.context import AnalysisContext
from code_analysis.models import (
    Completeness,
    Diagnostic,
    DiagnosticLevel,
    ProviderResult,
    ProviderState,
)
from code_analysis.report_models import (
    ChangeCouplingRecord,
    CommitCategory,
    GitCommitObservation,
    GitPathChangeRecord,
    GitRepositoryIdentity,
)

GIT_LIMITATIONS = (
    "Change coupling is bounded historical co-change evidence, not proof of semantic ownership.",
    "Fix categories are subject-prefix heuristics and cannot prove defect ownership.",
    "Portable Git evidence omits author names, email addresses, and raw commit subjects.",
)


def build_git_history(context: AnalysisContext) -> ChangeReport:
    """Collect Git evidence only for an explicit history command."""
    target_identity = _target_identity(context)
    try:
        facts = collect_git_history(
            context.source.physical_root,
            maximum_commits=context.config.phase_four_limits.max_git_commits,
            path_scope=context.config.git_path_scope,
        )
    except GitUnavailableError:
        provider = ProviderResult(
            provider_id="native-git-history",
            state=ProviderState.UNAVAILABLE,
            duration_ms=0,
            observations=(),
            diagnostics=(),
            completeness=Completeness.PARTIAL,
        )
        return _empty_report(
            context,
            target_identity,
            provider,
            Completeness.PARTIAL,
            "Git history is unavailable for the selected target.",
        )
    except (GitHistoryError, OSError, UnicodeError, ValueError) as exc:
        diagnostic = Diagnostic(
            code="git-history-malformed",
            level=DiagnosticLevel.ERROR,
            message=f"Git history could not be normalized: {type(exc).__name__}",
            provider="native-git-history",
        )
        provider = ProviderResult(
            provider_id="native-git-history",
            state=ProviderState.FAILED,
            duration_ms=0,
            diagnostics=(diagnostic,),
            completeness=Completeness.FAILED,
        )
        return _empty_report(
            context,
            target_identity,
            provider,
            Completeness.FAILED,
            "Git history collection failed.",
            diagnostics=(diagnostic,),
        )

    commits = tuple(
        sorted(
            (
                GitCommitObservation(
                    commit_id=item.commit_id,
                    authored_date=item.authored_date,
                    category=item.category,
                    contributor_pseudonym=item.contributor_pseudonym,
                    changed_paths=tuple(sorted({path.path for path in item.paths})),
                    bulk_change=item.bulk_change,
                    limitations=(
                        ("Bulk change is excluded from change coupling.",)
                        if item.bulk_change
                        else ()
                    ),
                )
                for item in facts.commits
            ),
            key=lambda item: item.commit_id,
        )
    )
    path_changes = tuple(
        sorted(
            (
                GitPathChangeRecord(
                    path_change_id=_stable_id(
                        "path-change", commit.commit_id, path.path, path.kind.value
                    ),
                    commit_id=commit.commit_id,
                    path=path.path,
                    kind=path.kind,
                    previous_path=path.previous_path,
                    additions=path.additions,
                    deletions=path.deletions,
                    formatting_only=commit.category is CommitCategory.FORMATTING,
                    limitations=(
                        ("Binary or unsupported numstat counts are unavailable.",)
                        if path.additions is None or path.deletions is None
                        else ()
                    ),
                )
                for commit in facts.commits
                for path in commit.paths
            ),
            key=lambda item: item.path_change_id,
        )
    )
    coupling, coupling_truncated, low_signal_pairs = _coupling(
        commits,
        context.config.phase_four_limits.max_git_coupling_pairs,
        context.config.phase_four_limits.min_git_shared_changes,
    )
    truncated = int(facts.truncated) + coupling_truncated
    diagnostics = (
        (
            Diagnostic(
                code="git-history-bound-truncated",
                level=DiagnosticLevel.WARNING,
                message=(
                    f"Git bounds omitted {int(facts.truncated)} commit range and "
                    f"{coupling_truncated} coupling pairs"
                ),
                provider="native-git-history",
            ),
        )
        if truncated
        else ()
    )
    completeness = Completeness.PARTIAL if truncated else Completeness.COMPLETE
    repository = GitRepositoryIdentity(
        repository_id=_stable_id(
            "git-repository",
            facts.head_commit_id,
            facts.path_scope.value,
            str(facts.tracked_files),
        ),
        head_commit_id=facts.head_commit_id,
        path_scope=facts.path_scope,
        tracked_files=facts.tracked_files,
        shallow=facts.shallow,
        observed_non_merge_commits=len(commits),
        truncated=facts.truncated,
    )
    provider = ProviderResult(
        provider_id="native-git-history",
        state=ProviderState.PARTIAL if truncated else ProviderState.SUCCESS,
        executable="git",
        version=facts.git_version,
        duration_ms=0,
        observations=(
            "bounded-non-merge-commits",
            "change-coupling",
            f"{facts.path_scope.value}-path-scope",
            "path-churn",
            "pseudonymous-contributors",
        ),
        diagnostics=diagnostics,
        completeness=completeness,
    )
    scope_limitation = (
        "Git history and change coupling are limited to Python paths."
        if facts.path_scope.value == "python"
        else (
            "Repository-wide Git history includes non-Python paths and must not be interpreted "
            "as code-only coupling."
        )
    )
    threshold_limitation = (
        f"{low_signal_pairs} coupling pairs below "
        f"{context.config.phase_four_limits.min_git_shared_changes} shared changes were omitted."
    )
    payload = {
        "configuration": context.config.phase_four_identity(),
        "target": target_identity,
        "repository": repository.model_dump(mode="json"),
        "commits": [item.model_dump(mode="json") for item in commits],
        "paths": [item.model_dump(mode="json") for item in path_changes],
        "coupling": [item.model_dump(mode="json") for item in coupling],
    }
    return ChangeReport(
        tool_version=__version__,
        change_identity_sha256=_digest(payload),
        target_identity_sha256=target_identity,
        source=ChangeEvidenceSource.GIT,
        git_repository=repository,
        git_commits=commits,
        git_path_changes=path_changes,
        coupling=coupling,
        providers=(provider,),
        diagnostics=diagnostics,
        completeness=completeness,
        limitations=tuple(sorted({*GIT_LIMITATIONS, scope_limitation, threshold_limitation})),
    )


def _empty_report(
    context: AnalysisContext,
    target_identity: str,
    provider: ProviderResult,
    completeness: Completeness,
    limitation: str,
    *,
    diagnostics: tuple[Diagnostic, ...] = (),
) -> ChangeReport:
    payload = {
        "configuration": context.config.phase_four_identity(),
        "target": target_identity,
        "provider": provider.model_dump(mode="json"),
    }
    return ChangeReport(
        tool_version=__version__,
        change_identity_sha256=_digest(payload),
        target_identity_sha256=target_identity,
        source=ChangeEvidenceSource.GIT,
        providers=(provider,),
        diagnostics=diagnostics,
        completeness=completeness,
        limitations=(limitation,),
    )


def _coupling(
    commits: tuple[GitCommitObservation, ...],
    maximum_pairs: int,
    minimum_shared_changes: int,
) -> tuple[tuple[ChangeCouplingRecord, ...], int, int]:
    path_commits: dict[str, set[str]] = {}
    shared: dict[tuple[str, str], set[str]] = {}
    for commit in commits:
        if commit.bulk_change:
            continue
        for path in commit.changed_paths:
            path_commits.setdefault(path, set()).add(commit.commit_id)
        for left, right in itertools.combinations(commit.changed_paths, 2):
            shared.setdefault((left, right), set()).add(commit.commit_id)
    eligible = sorted(
        (pair, commit_ids)
        for pair, commit_ids in shared.items()
        if len(commit_ids) >= minimum_shared_changes
    )
    retained = eligible[:maximum_pairs]
    records = tuple(
        ChangeCouplingRecord(
            coupling_id=_stable_id("change-coupling", left, right),
            left_path=left,
            right_path=right,
            shared_changes=len(commit_ids),
            left_total=len(path_commits[left]),
            right_total=len(path_commits[right]),
            jaccard=len(commit_ids) / len(path_commits[left] | path_commits[right]),
            commit_ids=tuple(sorted(commit_ids)),
        )
        for (left, right), commit_ids in retained
    )
    return (
        tuple(sorted(records, key=lambda item: item.coupling_id)),
        max(0, len(eligible) - maximum_pairs),
        len(shared) - len(eligible),
    )


def _target_identity(context: AnalysisContext) -> str:
    return _digest(
        {
            "logical_root": context.source.logical_root,
            "source_roots": context.source.logical_source_roots,
            "target_kind": "file" if context.target.is_file() else "directory",
        }
    )


def _digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _stable_id(prefix: str, *values: str) -> str:
    return f"{prefix}-{hashlib.sha256(chr(31).join(values).encode()).hexdigest()[:20]}"
