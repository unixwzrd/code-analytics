import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import rooms
import demo
import reception
import billing


class BeginnerTest(unittest.TestCase):
    def test_repeatable_demo_and_lazy_cycle(self):
        expected = {"receipt": "Fawlty Towers | Ada owes £21.60", "saved": 1, "rooms_available": 6}
        self.assertEqual(demo.run_demo(), expected)
        self.assertEqual(demo.run_demo(), expected)
        self.assertEqual(billing.QUOTES_SEEN, [2160])

    def test_price_shapes_and_mutation(self):
        self.assertEqual(billing.quote_stay([rooms.annex_room()], {})["total"], 800)
        self.assertEqual(billing.quote_stay([], {})["status"], "empty")
        room = rooms.standard_room()
        self.assertIs(rooms.reserve(room, 2), room)
        self.assertEqual(room["rooms_available"], 6)
        with self.assertRaises(ValueError):
            rooms.reserve(room, 99)

    def test_clone_pair(self):
        rows = [{"nightly_rate": 1000, "room_count": 2, "off_peak": True}]
        for total in [reception.direct_booking_total, reception.agency_booking_total]:
            self.assertEqual(total(rows), 1400)
            with self.assertRaises(ValueError):
                total([{"nightly_rate": -1}])

    def test_front_desk_options_and_persistence(self):
        from tempfile import TemporaryDirectory
        import json
        desk = reception.ReceptionDesk()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "reservation.json"
            receipt = desk.make_reservation([rooms.standard_room()], {"name": "Ada"}, path, "desk", True, True, True)
            self.assertEqual(json.loads(path.read_text()), [receipt])
            self.assertEqual(len(desk.last_instructions), 3)
        with self.assertRaises(ValueError):
            desk.make_reservation([rooms.standard_room()], {"name": " "})
        with self.assertRaises(ValueError):
            desk.make_reservation([rooms.standard_room()], {"name": "Ada"}, channel="roof")


if __name__ == "__main__":
    unittest.main()
