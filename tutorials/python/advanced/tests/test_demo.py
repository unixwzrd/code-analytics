import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import rooms
import demo
import reception
import bookings
import billing
import hotel_queue


class HotelTest(unittest.TestCase):
    def setUp(self):
        bookings.BOOKINGS.clear()

    def test_demo_is_repeatable(self):
        expected = {"receipt": "Fawlty Towers | Ada owes £21.60", "saved": 1, "rooms_available": 6, "total": 2160, "batch_total": 2160}
        self.assertEqual(demo.run_demo(), expected)
        self.assertEqual(demo.run_demo(), expected)

    def test_discount_branches_and_legacy_shape(self):
        item = dict(rooms.standard_room(), room_count=3)
        self.assertEqual(billing.quote_stay([item], {"tier": "gold"}, "SAVE10", "post")["total"], 2930)
        self.assertEqual(billing.quote_stay([rooms.annex_room()], {})["total"], 560)
        self.assertEqual(billing.quote_stay([], {})["status"], "empty")
        self.assertEqual(billing.quote_stay([item], {"blocked": True})["status"], "blocked")
        self.assertEqual(billing.quote_stay([item], {}, statement="moon")["status"], "unknown statement")

    def test_clone_behavior_and_validation(self):
        rows = [{"nightly_rate": 500, "room_count": 2}, {"nightly_rate": 1000, "off_peak": True}]
        self.assertEqual(reception.direct_booking_total(rows), 1700)
        self.assertEqual(reception.agency_booking_total(rows), 1700)
        for function in (reception.direct_booking_total, reception.agency_booking_total):
            with self.assertRaises(ValueError):
                function([{"nightly_rate": -1}])

    def test_state_mutation_and_persistence(self):
        room = rooms.standard_room()
        self.assertIs(rooms.reserve(room, 2), room)
        self.assertEqual(room["rooms_available"], 6)
        with self.assertRaises(ValueError):
            rooms.reserve(room, 99)
        desk = reception.ReceptionDesk()
        desk.make_reservation([room], {"name": "Ada"})
        with TemporaryDirectory() as directory:
            path = Path(directory) / "receipts.json"
            self.assertEqual(desk.save(path), 1)
            self.assertEqual(path.read_text(), '["Fawlty Towers | Ada owes £12.00"]\n')


class QueueTest(unittest.TestCase):
    def test_queue_states_cache_aliases_and_export(self):
        first = hotel_queue.run_batch_demo()
        self.assertEqual(first["total"], 2160)
        self.assertEqual(first["events"], ["bill:A", "cancel:B"])
        jobs = [
            {"id": "A"},
            {"id": "C", "guest": {"blocked": True}},
            {"id": "D", "guest": {"name": "Ada"}, "attempts": 3},
            {"id": "E"},
            {"id": "F", "guest": {"name": "Ada"}, "stays": []},
        ]
        office = hotel_queue.HotelOffice()
        office.load(jobs)
        result = office.process_queue()
        self.assertEqual(result["total"], 2160)
        self.assertEqual([job.get("state") for job in jobs], [None, "held", "exhausted", "invalid", "invalid"])
        self.assertEqual(result["results"][0], first["results"][0])
        self.assertEqual(hotel_queue.PENDING, [])
        with TemporaryDirectory() as directory:
            path = Path(directory) / "batch.json"
            office.export(path, result)
            import json
            self.assertEqual(json.loads(path.read_text()), result)


if __name__ == "__main__":
    unittest.main()
