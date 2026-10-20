"""Four synthetic callbacks; two workers plus their main-thread coordinator."""

from __future__ import annotations

import cProfile
import json
import os
import profile
import pstats
import subprocess
import sys
import threading
from collections import Counter
from pathlib import Path

events = []
event_lock = threading.Lock()
active = threading.Event()
noise_done = threading.Event()
cancelled = threading.Event()
begin = threading.Event()
phase = threading.Barrier(3, timeout=3)
modes = ("normal", "close", "cancel", "error")


def record(name):
    with event_lock:
        events.append((name, threading.current_thread().name))


def main_leaf():
    record("main_leaf")


def noise_leaf():
    record("noise_leaf")


def scoped_leaf():
    record("scoped_leaf")


def generator_leaf():
    record("generator_leaf")


def cleanup_leaf():
    record("cleanup_leaf")


def cancel_leaf():
    record("cancel_leaf")


def error_leaf():
    record("error_leaf")


def failure_leaf():
    record("failure_leaf")
    raise ValueError("synthetic failure")


class Cancelled(Exception):
    pass


def values():
    try:
        generator_leaf()
        yield 1
        generator_leaf()
        yield 2
    finally:
        cleanup_leaf()


def callback(mode):
    scoped_leaf()
    active.set()
    if not noise_done.wait(3):
        raise TimeoutError("missing overlapping worker")
    stream = values()
    try:
        assert next(stream) == 1
        if mode == "normal":
            assert next(stream) == 2
        elif mode == "cancel":
            assert cancelled.is_set()
            stream.throw(Cancelled())
        elif mode == "error":
            failure_leaf()
    except Cancelled:
        cancel_leaf()
    except ValueError:
        error_leaf()
    finally:
        stream.close()


def child_leaf():
    return 1


def run(mode, output):
    captures, failures = [], []

    def existing_worker():
        try:
            assert begin.wait(3)
            for index, case in enumerate(modes):
                phase.wait()
                if mode.startswith("scoped-"):
                    kind = profile.Profile if mode == "scoped-profile" else cProfile.Profile
                    capture = kind()
                    capture.runcall(callback, case)
                    path = output / f"callback-{index}.prof"
                    capture.dump_stats(str(path))
                    captures.append(str(path))
                else:
                    callback(case)
                phase.wait()
        except BaseException as exc:
            failures.append(type(exc).__name__)
            phase.abort()

    def new_worker():
        try:
            for _ in modes:
                phase.wait()
                assert active.wait(3)
                noise_leaf()
                noise_done.set()
                phase.wait()
        except BaseException as exc:
            failures.append(type(exc).__name__)
            phase.abort()

    existing = threading.Thread(target=existing_worker, name="existing-worker")
    existing.start()  # Existing before main-thread profiler activation.

    def measured():
        late = threading.Thread(target=new_worker, name="new-worker")
        late.start()
        begin.set()
        for case in modes:
            active.clear()
            noise_done.clear()
            if case == "cancel":
                cancelled.set()
            phase.wait()
            main_leaf()
            phase.wait()
        existing.join(3)
        late.join(3)
        assert not existing.is_alive() and not late.is_alive() and not failures
        child = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "child"],
            capture_output=True, text=True, check=True, timeout=3,
        )
        assert json.loads(child.stdout) == {"child_calls": 3}

    if mode == "main-cprofile":
        capture = cProfile.Profile()
        capture.runcall(measured)
        capture.dump_stats(str(output / "main.prof"))
    else:
        measured()
    if captures:
        pstats.Stats(*captures).dump_stats(str(output / "scoped.prof"))
    counts = Counter(name for name, _ in events)
    return {
        "counts": dict(sorted(counts.items())),
        "owners": {name: sorted({owner for event, owner in events if event == name}) for name in sorted(counts)},
        "child_calls": 3,
        "callback_cases": list(modes),
        "pid": os.getpid(),
    }


if __name__ == "__main__":
    if sys.argv[1] == "child":
        print(json.dumps({"child_calls": sum(child_leaf() for _ in range(3))}))
    else:
        print(json.dumps(run(sys.argv[1], Path(sys.argv[2])), sort_keys=True))
