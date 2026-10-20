from .dependencies import shared


def transform(value: int) -> int:
    result = {"value": shared(value)}["value"]
    if result > 0:
        return result
    return 0
