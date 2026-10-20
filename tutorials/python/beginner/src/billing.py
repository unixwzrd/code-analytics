"""A small billing loop with process-wide memory and a safe lazy-use cycle."""
import reception

QUOTES_SEEN = []


def money(pennies):
    return f"£{pennies // 100}.{pennies % 100:02d}"


def quote_stay(stays, guest):
    if not stays:
        return {"status": "empty", "total": 0}
    total = 0
    for item in stays:
        nightly_rate = item.get("nightly_rate", item.get("annex_rate", 0))
        if nightly_rate < 0:
            return {"status": "invalid nightly_rate", "total": 0}
        amount = nightly_rate * item.get("room_count", 1)
        if guest.get("tier") == "gold":
            amount = amount * 90 // 100
        total += amount
    QUOTES_SEEN.append(total)
    return {"status": "ok", "total": total}


def preview_reservation(stays, guest):
    return reception.ReceptionDesk().make_reservation(stays, guest)
