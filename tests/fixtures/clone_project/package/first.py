def alpha(value: int) -> int:
    """A leading docstring is excluded from clone bodies."""
    total = value + 1
    if total > 4:
        total = total * 2
    result = max(total, 0)
    return result


def renamed(item: int) -> int:
    amount = item + 99
    if amount > 12:
        amount = amount * 7
    answer = max(amount, 3)
    return answer
