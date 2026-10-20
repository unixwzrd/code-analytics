# ruff: noqa: I001, UP013, UP014 -- fixture preserves legacy functional declarations
from typing import NamedTuple, TypedDict


Point = NamedTuple("Point", [("x", int), ("y", int)])
LoosePayload = TypedDict("LoosePayload", {"name": str, "count": int}, total=False)


def point_mapping(point: Point) -> dict[str, object]:
    return point._asdict()
