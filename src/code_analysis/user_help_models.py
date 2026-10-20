"""Closed, immutable contracts for compiled, source-free user documentation."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DOCUMENT_IDS = ("getting-started", "user-guide", "cli-reference", "examples", "reports")
ASSET_NAMES = (*[f"{name}.html" for name in DOCUMENT_IDS], "styles.css")


class HelpModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, str_max_length=512)


class HelpHeading(HelpModel):
    anchor: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    title: str = Field(min_length=1)
    level: int = Field(ge=1, le=6)


class HelpDocument(HelpModel):
    document_id: Literal["getting-started", "user-guide", "cli-reference", "examples", "reports"]
    title: str = Field(min_length=1)
    category: str = Field(min_length=1)
    headings: tuple[HelpHeading, ...] = Field(max_length=512)

    @model_validator(mode="after")
    def unique_headings(self) -> Self:
        anchors = [heading.anchor for heading in self.headings]
        if len(anchors) != len(set(anchors)):
            raise ValueError("duplicate help heading")
        return self


class HelpHash(HelpModel):
    name: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class HelpCatalog(HelpModel):
    schema_version: Literal[1]
    renderer: Literal["markdown-it-py==4.0.0"]
    configuration: Literal["commonmark;html=false;tables=true;highlight=false;links=closed-v1"]
    generator_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    sources: tuple[HelpHash, ...] = Field(min_length=5, max_length=5)
    assets: tuple[HelpHash, ...] = Field(min_length=6, max_length=6)
    documents: tuple[HelpDocument, ...] = Field(min_length=5, max_length=5)

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_schema_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema version must be an integer")
        return value

    @model_validator(mode="after")
    def closed_catalog(self) -> Self:
        if tuple(document.document_id for document in self.documents) != DOCUMENT_IDS:
            raise ValueError("help document catalog is not the closed ordered set")
        if tuple(asset.name for asset in self.assets) != tuple(sorted(ASSET_NAMES)):
            raise ValueError("help assets are not the closed ordered set")
        expected_sources = (
            "README.md",
            "docs/cli-examples.md",
            "docs/cli.md",
            "docs/report-and-diagram-catalog.md",
            "docs/ui-guide.md",
        )
        if tuple(source.name for source in self.sources) != expected_sources:
            raise ValueError("help sources are not the closed ordered set")
        return self
