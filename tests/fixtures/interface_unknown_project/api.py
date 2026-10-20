from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/api")


class UnknownRequest(BaseModel):
    payload: Any


@router.post("/unknown")
def accept_unknown(payload: UnknownRequest):
    return {"status": "ok"}
