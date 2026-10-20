from typing import Literal

from pydantic import BaseModel


class Envelope(BaseModel):
    schema_version: Literal["v1"] = "v1"
    payload: str


def create() -> Envelope:
    return Envelope(payload="synthetic")


def encode(value: Envelope) -> str:
    alias = value
    return alias.model_dump_json()
