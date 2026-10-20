import json
from pathlib import Path
from tempfile import TemporaryDirectory
import rooms
import reception
import billing


def run_demo():
    billing.QUOTES_SEEN.clear()
    room = rooms.standard_room()
    rooms.reserve(room, 2)
    desk = reception.ReceptionDesk()
    with TemporaryDirectory() as directory:
        receipt = desk.make_reservation([dict(room, room_count=2)], {"name": "Ada", "tier": "gold"}, Path(directory) / "receipts.json")
        saved = len(desk.receipts)
    return {"receipt": receipt, "saved": saved, "rooms_available": room["rooms_available"]}


if __name__ == "__main__":
    print(json.dumps(run_demo(), sort_keys=True, ensure_ascii=False))
