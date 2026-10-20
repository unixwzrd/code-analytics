

from . import b
from . import b


class Outer:
    class Inner:
        pass

    def method(self, value: int) -> int:
        def nested(item: int) -> int:
            if item:
                return item
            return 0

        return nested(value)


def calculate(left: int, right: int = 1) -> int:
    if left and right:
        return left + right
    return 0
