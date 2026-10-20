from .dependencies import shared


def first(value: int) -> int:
    result = {"value": shared(value)}["value"]
    if result > 0:
        return result
    return 0


def second(value: int) -> int:
    result = {"value": shared(value)}["value"]
    if result > 0:
        return result
    return 0


def third(value: int) -> int:
    result = {"value": shared(value)}["value"]
    if result > 0:
        return result
    return 0
