from pathlib import Path


def write_again() -> None:
    destination = Path("other.txt")
    destination.write_text("other", encoding="utf-8")
