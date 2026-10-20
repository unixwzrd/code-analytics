def beta(value: int) -> int:
    """This different docstring is also excluded."""
    total = value + 1
    if total > 4:
        total = total * 2
    result = max(total, 0)
    return result
