def build(queued: bool) -> dict[str, object]:
    if queued:
        payload = {"kind": "queued", "job_id": "j1"}
    else:
        payload = {"kind": "done", "result": 1}
    if not payload.get("kind"):
        raise ValueError("kind is required")
    return payload
