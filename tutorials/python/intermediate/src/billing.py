# Basil has put every hotel tariff in one place. It is not a small place.
"""A billing rulebook that grew instead of being designed."""
import bookings


def money(pennies):
    return f"£{pennies // 100}.{pennies % 100:02d}"


def quote_stay(stays, guest, coupon="", statement="email"):
    if not stays:
        return {"status": "empty", "total": 0}
    if guest.get("blocked", False):
        return {"status": "blocked", "total": 0}
    total = 0
    notes = []
    for item in stays:
        if "room_type" in item:
            if "nightly_rate" in item:
                if item["nightly_rate"] >= 0:
                    if item.get("room_count", 1) > 0:
                        amount = item["nightly_rate"] * item.get("room_count", 1)
                        if guest.get("tier") == "gold":
                            if item.get("wing") == "east":
                                if item.get("room_count", 1) >= 3:
                                    amount = amount * 80 // 100
                                    notes.append("gold east bulk")
                                else:
                                    amount = amount * 90 // 100
                                    notes.append("gold east")
                            else:
                                amount = amount * 95 // 100
                                notes.append("gold general")
                        elif guest.get("tier") == "silver":
                            if item.get("room_count", 1) >= 2:
                                amount = amount * 95 // 100
                                notes.append("silver bulk")
                            else:
                                notes.append("silver single")
                        else:
                            if item.get("off_peak", False):
                                amount = amount * 70 // 100
                                notes.append("off_peak")
                            else:
                                notes.append("standard")
                        total += amount
                    else:
                        return {"status": "invalid room count", "total": 0}
                else:
                    return {"status": "invalid room rate", "total": 0}
            else:
                return {"status": "missing room rate", "total": 0}
        elif "code" in item:
            if "annex_rate" in item:
                if item["annex_rate"] >= 0:
                    amount = item["annex_rate"] * item.get("room_count", 1)
                    if item.get("off_peak", False):
                        amount = amount * 70 // 100
                        notes.append("legacy off_peak")
                    total += amount
                else:
                    return {"status": "invalid annex rate", "total": 0}
            else:
                return {"status": "missing annex rate", "total": 0}
        else:
            return {"status": "unknown room", "total": 0}
    if coupon:
        if coupon == "SAVE10":
            if total >= 2000:
                total -= 200
                notes.append("coupon")
            else:
                notes.append("coupon below minimum")
        elif coupon == "STAFF":
            if guest.get("staff", False):
                total = total * 85 // 100
            else:
                notes.append("staff coupon refused")
        else:
            notes.append("unknown coupon")
    if statement == "post":
        if total < 5000:
            if guest.get("tier") == "gold":
                total += 250
            else:
                total += 500
        else:
            notes.append("free shipping")
    elif statement != "email":
        return {"status": "unknown statement", "total": 0}
    return {"status": "ok", "total": total, "notes": notes}


def quote_saved_booking(booking_id):
    booking = bookings.lookup(booking_id)
    return quote_stay(booking["stays"], booking["guest"])
