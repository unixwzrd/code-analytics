import json


def save(stream) -> None:
    payload = {"item_id": "i1"}
    json.dumps(payload)
    json.dump(payload, stream)
