from dataclasses import dataclass, field
from typing import Literal, NotRequired

from .base import ContractBase, PayloadBase, RecordBase


@dataclass
class Job:
    name: str
    tags: list[str] = field(default_factory=list)


class Request(PayloadBase):
    operation: str
    timeout: NotRequired[int]


class Response(ContractBase):
    schema_version: Literal["v1"] = "v1"
    accepted: bool
    message: str | None = None


class ArchivedJob(RecordBase):
    archive: str


def make_response() -> Response:
    return Response(accepted=True, message=None, request_id="local")
