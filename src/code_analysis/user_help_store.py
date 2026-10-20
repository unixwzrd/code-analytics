"""Read only validated packaged help; no source paths or renderer imports."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files

from pydantic import ValidationError

from code_analysis.user_help_models import ASSET_NAMES, HelpCatalog


class HelpUnavailableError(ValueError):
    """Help is unavailable; callers must keep analytical navigation usable."""


@dataclass(frozen=True)
class HelpAsset:
    payload: bytes
    content_type: str


def _read(filename: str) -> bytes:
    try:
        limit = 1024 * 1024 if filename == "index.json" else 4 * 1024 * 1024
        with files("code_analysis").joinpath("user_help", filename).open("rb") as stream:
            payload = stream.read(limit + 1)
        if len(payload) > limit:
            raise HelpUnavailableError("Bundled help exceeds the resource size limit")
        return payload
    except (OSError, ModuleNotFoundError) as exc:
        raise HelpUnavailableError("Bundled help is missing or unreadable") from exc


def load_help_catalog() -> HelpCatalog:
    """Strictly validate metadata and every closed asset before returning it."""
    try:
        catalog = HelpCatalog.model_validate_json(_read("index.json"))
    except ValidationError as exc:
        raise HelpUnavailableError("Bundled help catalog is invalid") from exc
    for asset in catalog.assets:
        if sha256(_read(asset.name)).hexdigest() != asset.sha256:
            raise HelpUnavailableError(
                f"Bundled help asset failed integrity validation: {asset.name}"
            )
    return catalog


def read_help_asset(filename: str) -> HelpAsset:
    """Read one exact filename, never an arbitrary path or analyzed-source resource."""
    if filename not in (*ASSET_NAMES, "index.json"):
        raise HelpUnavailableError("Unknown bundled help resource")
    catalog = load_help_catalog()
    payload = _read(filename)
    if filename != "index.json":
        expected = next(asset.sha256 for asset in catalog.assets if asset.name == filename)
        if sha256(payload).hexdigest() != expected:
            raise HelpUnavailableError("Bundled help changed while reading")
    else:
        try:
            if HelpCatalog.model_validate_json(payload) != catalog:
                raise HelpUnavailableError("Bundled help changed while reading")
        except ValidationError as exc:
            raise HelpUnavailableError("Bundled help changed while reading") from exc
    content_type = (
        "application/json; charset=utf-8"
        if filename == "index.json"
        else "text/css; charset=utf-8" if filename == "styles.css" else "text/html; charset=utf-8"
    )
    return HelpAsset(payload=payload, content_type=content_type)
