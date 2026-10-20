# Manuel copied the quotation calculator so the agency desk would feel included.
"""Billing, presentation and persistence all have the same owner."""
import json
import billing


def direct_booking_total(rows):
    total = 0
    for row in rows:
        if "nightly_rate" not in row:
            raise ValueError("missing room rate")
        if row["nightly_rate"] < 0:
            raise ValueError("negative room rate")
        room_count = row.get("room_count", 1)
        if room_count < 1:
            raise ValueError("invalid room count")
        subtotal = row["nightly_rate"] * room_count
        if row.get("off_peak", False):
            subtotal = subtotal * 70 // 100
        total += subtotal
    return total


def agency_booking_total(entries):
    total = 0
    for entry in entries:
        if "nightly_rate" not in entry:
            raise ValueError("missing room rate")
        if entry["nightly_rate"] < 0:
            raise ValueError("negative room rate")
        room_count = entry.get("room_count", 1)
        if room_count < 1:
            raise ValueError("invalid room count")
        subtotal = entry["nightly_rate"] * room_count
        if entry.get("off_peak", False):
            subtotal = subtotal * 70 // 100
        total += subtotal
    return total


class ReceptionDesk:
    def __init__(self):
        self.receipts = []

    def make_reservation(self, stays, guest):
        result = billing.quote_stay(stays, guest)
        if result["status"] != "ok":
            raise ValueError(result["status"])
        receipt = "Fawlty Towers | " + guest["name"] + " owes " + billing.money(result["total"])
        self.receipts.append(receipt)
        return receipt

    def save(self, path):
        path.write_text(json.dumps(self.receipts, ensure_ascii=False) + "\n", encoding="utf-8")
        return len(self.receipts)
