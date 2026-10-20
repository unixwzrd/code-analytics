import json
import threading

registry: dict[str, int] = {"first": 1}
registry_lock = threading.Lock()
with registry_lock:
    registry["second"] = 2


def build() -> dict[str, object]:
    payload = {"state": "queued"}
    alias = payload
    payload["state"] = "running"
    json.dumps(payload)
    json.dumps(payload, sort_keys=True)
    return alias
