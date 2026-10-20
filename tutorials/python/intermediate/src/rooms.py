# Polly keeps the rooms available; Basil keeps changing their field names.
"""Deliberately mixed room shapes. All prices are integer pennies."""
from typing import TypedDict
import billing


class RoomOffer(TypedDict):
    room_type: str
    nightly_rate: int
    rooms_available: int
    wing: str


def standard_room() -> RoomOffer:
    return {"room_type": "DOUBLE", "nightly_rate": 1200, "rooms_available": 8, "wing": "east"}


def annex_room():
    # A second shape for the same business concept.
    return {"code": "ANNEX", "annex_rate": 800, "vacancies": 4, "off_peak": True}


def list_rooms():
    return [standard_room(), annex_room()]


def display_rate(room):
    return billing.money(room.get("nightly_rate", room.get("annex_rate", 0)))


def reserve(room, room_count):
    # Hidden input mutation; caller's object is the inventory record.
    if room_count <= 0:
        raise ValueError("room count must be positive")
    field = "rooms_available" if "rooms_available" in room else "vacancies"
    if room[field] < room_count:
        raise ValueError("insufficient rooms")
    room[field] -= room_count
    return room
