# Guest records and reservations share a process-wide notebook.
"""Shared process state plus repeated validation."""
import rooms

BOOKINGS = []


def create_booking(stays, guest):
    if "name" not in guest:
        raise ValueError("missing name")
    if not isinstance(guest["name"], str):
        raise ValueError("invalid name")
    if not guest["name"].strip():
        raise ValueError("blank name")
    booking = {"id": len(BOOKINGS) + 1, "stays": stays, "guest": guest}
    BOOKINGS.append(booking)
    return booking


def lookup(booking_id):
    for booking in BOOKINGS:
        if booking["id"] == booking_id:
            return booking
    raise KeyError(booking_id)


def preview_guest(guest):
    if "name" not in guest:
        raise ValueError("missing name")
    if not isinstance(guest["name"], str):
        raise ValueError("invalid name")
    if not guest["name"].strip():
        raise ValueError("blank name")
    return guest["name"].strip()


def default_stays():
    return [rooms.standard_room()]
