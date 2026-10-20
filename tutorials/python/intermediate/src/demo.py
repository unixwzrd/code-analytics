"""Small legitimate orchestration: coordination is its job."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import rooms
import reception
import bookings
import billing


def run_demo():
    bookings.BOOKINGS.clear()
    room = rooms.standard_room()
    rooms.reserve(room, 2)
    item = dict(room, room_count=2)
    guest = {"name": "Ada", "tier": "gold"}
    booking = bookings.create_booking([item], guest)
    desk = reception.ReceptionDesk()
    receipt = desk.make_reservation(booking["stays"], guest)
    with TemporaryDirectory() as directory:
        saved = desk.save(Path(directory) / "receipts.json")
    return {"receipt": receipt, "saved": saved, "rooms_available": room["rooms_available"],
            "total": billing.quote_saved_booking(booking["id"])["total"]}


if __name__ == "__main__":
    print(json.dumps(run_demo(), sort_keys=True, ensure_ascii=False))
