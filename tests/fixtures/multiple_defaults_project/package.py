def normalize(body: dict[str, object]) -> dict[str, object]:
    return {
        "tools": body.get("tools") if isinstance(body.get("tools"), list) else None,
    }
