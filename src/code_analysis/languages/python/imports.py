from __future__ import annotations

from dataclasses import dataclass

from code_analysis.languages.python.native_ast import ParsedModule, RawImport


@dataclass(frozen=True, order=True, slots=True)
class ResolvedEdge:
    source: str
    target: str
    local: bool
    kind: str


def resolve_edges(
    modules: tuple[ParsedModule, ...], *, only_local: bool
) -> tuple[ResolvedEdge, ...]:
    known = {module.name for module in modules}
    edges: set[ResolvedEdge] = set()
    for module in modules:
        for raw in module.imports:
            for target in _resolve_raw(module.name, raw, known):
                local_target = _match_local(target, known)
                local = local_target is not None
                final_target = local_target or target
                if final_target and (local or not only_local):
                    edges.add(ResolvedEdge(module.name, final_target, local, raw.kind))
    return tuple(sorted(edges))


def _resolve_raw(current: str, raw: RawImport, known: set[str]) -> tuple[str, ...]:
    if raw.level == 0:
        base = raw.module
    else:
        current_is_package = current in known and any(
            name.startswith(current + ".") for name in known
        )
        package_parts = current.split(".") if current_is_package else current.split(".")[:-1]
        trim = raw.level - 1
        base_parts = (
            package_parts[: len(package_parts) - trim] if trim <= len(package_parts) else []
        )
        if raw.module:
            base_parts.extend(raw.module.split("."))
        base = ".".join(base_parts)

    imported_modules = tuple(
        sorted(
            candidate
            for name in raw.names
            if (candidate := f"{base}.{name}" if base else name) in known
        )
    )
    if imported_modules:
        return imported_modules
    return (base,) if base else ()


def _match_local(target: str, known: set[str]) -> str | None:
    if target in known:
        return target
    candidates = [name for name in known if target.startswith(name + ".")]
    if candidates:
        return max(candidates, key=len)
    return None
