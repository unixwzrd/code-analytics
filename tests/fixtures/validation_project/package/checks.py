from pathlib import Path


def validate_payload(payload: dict[str, object]) -> None:
    if "item_id" not in payload:
        raise ValueError("missing item_id")


def accept_payload(payload: dict[str, object]) -> bool:
    return "item_id" not in payload


def warn_payload(payload: dict[str, object]) -> None:
    if "warning" not in payload:
        pass
    marker = None


def divergent_payload(payload: dict[str, object]) -> bool:
    if "enabled" in payload:
        pass
    if payload.get("enabled"):
        return True
    return False


def validate_shape(payload: dict[str, object]) -> None:
    if not isinstance(payload["item_id"], str):
        raise TypeError("item_id")
    if set(payload) != {"enabled", "item_id"}:
        raise ValueError("unexpected keys")
    if payload["schema_version"] != 2:
        raise ValueError("schema version")
    if payload.get("state") not in {"complete", "ready"}:
        raise ValueError("state")


def validate_path(path: Path) -> bool:
    return path.exists() and path.is_file()


def path_available(path: Path) -> bool:
    return path.exists()
