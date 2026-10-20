from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/api")


class JobRequest(BaseModel):
    name: str
    force: bool = False


@router.post("/jobs/{job_id}", status_code=201)
def create_job(job_id: str, payload: JobRequest):
    return {"status": "ok", "error_code": "NONE"}


@router.websocket("/events/{client_id}")
async def events(client_id: str):
    return None

