import os
import socket
import subprocess
import threading
from pathlib import Path

STATE = {}
CACHE = {}
LOCK = threading.RLock()
SHADOW = {}


class Manager:
    registry = {}


def mutate_state(value: str) -> None:
    global STATE
    alias = STATE
    with LOCK:
        alias.update({"value": value})
    CACHE.clear()
    Manager.registry.update({"value": value})


def perform_effects(source: Path, target: Path) -> None:
    output = Path("result.txt")
    output.write_text("result", encoding="utf-8")
    os.replace(source, target)
    os.environ["PHASE3_TEST"] = os.getenv("PHASE3_SOURCE", "")
    subprocess.run(("true",), check=False)
    threading.Thread(target=lambda: None).start()
    socket.create_connection(("example.invalid", 443))


def misleading(receiver: object) -> None:
    receiver.write_text()
    receiver.run()
    receiver.connect()


def shadows_global() -> dict[str, str]:
    SHADOW = {"local": "only"}
    return SHADOW
