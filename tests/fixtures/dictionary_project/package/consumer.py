def consume(value: dict[str, object]) -> None:
    value.get("trace_id", "missing")
    external(value)
