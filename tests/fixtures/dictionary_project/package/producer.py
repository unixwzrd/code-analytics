from .consumer import consume


def build(queued: bool) -> dict[str, object]:
    if queued:
        payload = {"kind": "queued", "job_id": "j1"}
    else:
        payload = {"kind": "done", "result": 1}
    consume(payload)
    return payload


def advance() -> dict[str, object]:
    payload = {"state": "queued"}
    payload["state"] = "running"
    return payload
