"""Basil owns the desk; Manuel supplied the suspiciously familiar calculators."""
import json
import billing


def direct_booking_total(rows):
    total = 0
    for row in rows:
        if "nightly_rate" not in row:
            raise ValueError("missing nightly_rate")
        if row["nightly_rate"] < 0:
            raise ValueError("negative nightly_rate")
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
            raise ValueError("missing nightly_rate")
        if entry["nightly_rate"] < 0:
            raise ValueError("negative nightly_rate")
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

    def make_reservation(self, stays, guest, receipt_path=None, channel="desk", breakfast=False, quiet=False, porter=False):
        # Basil: "One method means one person to blame. Excellent management."
        if "name" not in guest:
            raise ValueError("missing guest name")
        if not isinstance(guest["name"], str):
            raise ValueError("invalid guest name")
        if not guest["name"].strip():
            raise ValueError("blank guest name")
        if not stays:
            raise ValueError("empty reservation")
        count = 0
        instructions = []
        for stay in stays:
            if "nightly_rate" in stay:
                if stay["nightly_rate"] < 0:
                    raise ValueError("negative nightly rate")
                if stay.get("room_count", 1) < 1:
                    raise ValueError("invalid room count")
                count += stay.get("room_count", 1)
            elif "annex_rate" in stay:
                if stay["annex_rate"] < 0:
                    raise ValueError("negative annex rate")
                count += stay.get("room_count", 1)
            else:
                raise ValueError("missing room rate")
        if channel == "desk":
            if quiet:
                instructions.append("Polly: use the garden corridor")
            else:
                instructions.append("Basil: the desk is already quiet")
        elif channel == "telephone":
            instructions.append("Manuel: telephone reservation copied once")
        else:
            raise ValueError("unknown reservation channel")
        if breakfast:
            if count > 1:
                instructions.append("Polly: two breakfast places, one argument")
            else:
                instructions.append("Polly: one breakfast place")
        if porter:
            instructions.append("Manuel: luggage noted; no extra charge")
        result = billing.quote_stay(stays, guest)
        if result["status"] != "ok":
            raise ValueError(result["status"])
        receipt = "Fawlty Towers | " + guest["name"] + " owes " + billing.money(result["total"])
        self.receipts.append(receipt)
        self.last_instructions = instructions
        if receipt_path is not None:
            self.save(receipt_path)
        return receipt

    def save(self, path):
        path.write_text(json.dumps(self.receipts, ensure_ascii=False) + "\n", encoding="utf-8")
        return len(self.receipts)
