from dataclasses import dataclass
from typing import TypedDict

from pydantic import BaseModel


class ContractBase(BaseModel):
    request_id: str


class PayloadBase(TypedDict):
    source: str


@dataclass
class RecordBase:
    created: int
