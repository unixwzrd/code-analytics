"""Exact recorded module ownership for graph grouping and ranking."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from code_analysis.models import SourceLanguage
from code_analysis.report_models import AnalysisReport


@dataclass(frozen=True, slots=True)
class GraphOwnership:
    module_languages: Mapping[str, SourceLanguage]
    node_modules: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "module_languages", MappingProxyType(dict(self.module_languages)))
        object.__setattr__(self, "node_modules", MappingProxyType(dict(self.node_modules)))

    def module_for(self, node_id: str, group_id: str | None = None) -> str | None:
        if node_id in self.module_languages:
            return node_id
        if group_id in self.module_languages:
            return group_id
        module = self.node_modules.get(node_id)
        return module if module in self.module_languages else None


def graph_ownership(report: AnalysisReport) -> GraphOwnership:
    """Build one index; labels and inferred lexical names never establish ownership."""
    modules = {item.name: item.language for item in report.modules}
    subjects = {item.subject_id: item.module for item in report.subjects}
    owners = {item.state_resource_id: item.owner_subject_id for item in report.state_resources}
    owners.update({item.side_effect_id: item.subject_id for item in report.side_effects})
    owners.update({item.structure_id: item.owner_subject_id for item in report.data_structures})
    for shape in report.dictionary_shapes:
        owners[shape.shape_id] = shape.owner_subject_id
        owners.update({state.state_id: shape.owner_subject_id for state in shape.lifecycle_states})
    node_modules = dict(subjects)
    node_modules.update((item.source_file_id, item.module_name) for item in report.source_files)
    node_modules.update(
        (item.occurrence_id, item.module_name) for item in report.static_call_occurrences
    )
    node_modules.update(
        (identity, subjects[owner]) for identity, owner in owners.items() if owner in subjects
    )
    return GraphOwnership(modules, node_modules)
