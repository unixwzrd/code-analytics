# Sybil asked for a queue. Basil supplied a queue, a cache and a filing dispute.
"""A queue, retry policy, receipt cache and exporter with shared ownership."""
import json
import rooms
import bookings
import billing

PENDING = []
EVENTS = []
RECEIPT_CACHE = {}


class HotelOffice:
    def load(self, jobs):
        # Queue aliases caller-owned jobs; processing changes their state.
        PENDING.extend(jobs)

    def process_queue(self, statement="email"):
        results = []
        total = 0
        while PENDING:
            job = PENDING.pop(0)
            if job.get("cancelled", False):
                job["state"] = "cancelled"
                results.append({"id": job["id"], "reason": "cancelled"})
                EVENTS.append("cancel:" + job["id"])
            else:
                if job["id"] in RECEIPT_CACHE:
                    results.append(RECEIPT_CACHE[job["id"]])
                    total += RECEIPT_CACHE[job["id"]]["total"]
                    EVENTS.append("cache:" + job["id"])
                else:
                    if "guest" in job:
                        if job["guest"].get("blocked", False):
                            job["state"] = "held"
                            results.append({"id": job["id"], "reason": "blocked"})
                        else:
                            if job.get("attempts", 0) < 3:
                                job["attempts"] = job.get("attempts", 0) + 1
                                quote = billing.quote_stay(job["stays"], job["guest"], job.get("coupon", ""), statement)
                                if quote["status"] == "ok":
                                    booking = bookings.create_booking(job["stays"], job["guest"])
                                    receipt = "Fawlty Towers | " + booking["guest"]["name"] + " owes " + billing.money(quote["total"])
                                    result = {"id": job["id"], "receipt": receipt, "total": quote["total"], "state": "billed"}
                                    RECEIPT_CACHE[job["id"]] = result
                                    results.append(result)
                                    total += quote["total"]
                                    job["state"] = "billed"
                                    EVENTS.append("bill:" + job["id"])
                                else:
                                    job["state"] = "invalid"
                                    results.append({"id": job["id"], "reason": quote["status"]})
                            else:
                                job["state"] = "exhausted"
                                results.append({"id": job["id"], "reason": "retry limit"})
                    else:
                        job["state"] = "invalid"
                        results.append({"id": job["id"], "reason": "missing guest"})
        return {"total": total, "results": results, "events": list(EVENTS)}

    def export(self, path, result):
        path.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")


def run_batch_demo():
    PENDING.clear()
    EVENTS.clear()
    RECEIPT_CACHE.clear()
    jobs = [
        {"id": "A", "stays": [dict(rooms.standard_room(), room_count=2)], "guest": {"name": "Ada", "tier": "gold"}},
        {"id": "B", "cancelled": True},
    ]
    office = HotelOffice()
    office.load(jobs)
    return office.process_queue()
