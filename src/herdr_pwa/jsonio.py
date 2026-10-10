"""Typed views over parsed JSON, so nothing downstream handles ``Any``."""

import json
from typing import cast


def loads(text: str) -> object:
    """Parse JSON text into an untyped value; raises ``json.JSONDecodeError``."""
    return cast("object", json.loads(text))


def as_dict(value: object) -> dict[str, object] | None:
    """The value as a JSON object, or None when it is anything else."""
    if isinstance(value, dict):
        # JSON object keys are always strings.
        return cast("dict[str, object]", value)
    return None


def as_list(value: object) -> list[object] | None:
    """The value as a JSON array, or None when it is anything else."""
    if isinstance(value, list):
        return cast("list[object]", value)
    return None


def child(value: object, key: str) -> object:
    """``value[key]`` when value is an object, else None."""
    mapping = as_dict(value)
    return None if mapping is None else mapping.get(key)


def as_str(value: object) -> str | None:
    """The value when it is a string, else None."""
    return value if isinstance(value, str) else None


def as_int(value: object) -> int | None:
    """The value when it is an integer and not a bool, else None."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def as_float(value: object) -> float | None:
    """The value as a float when it is a number and not a bool, else None."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)
