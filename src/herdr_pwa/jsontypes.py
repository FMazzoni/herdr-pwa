"""Typed JSON: parse once at the boundary, then narrow with isinstance."""

import json
from typing import cast

from pydantic import JsonValue

type JsonObject = dict[str, JsonValue]
type JsonArray = list[JsonValue]


def loads(text: str | bytes) -> JsonValue:
    """Parse JSON text into a typed value; raises ``ValueError`` like ``json.loads``."""
    # json.loads is typed Any; its output is exactly the JsonValue union.
    return cast(JsonValue, json.loads(text))


def as_object(value: object) -> JsonObject | None:
    """The value as a JSON object, or ``None`` when it is not one."""
    if isinstance(value, dict):
        return cast(JsonObject, value)
    return None


def as_array(value: object) -> JsonArray:
    """The value as a JSON array, or an empty one when it is not one."""
    if isinstance(value, list):
        return cast(JsonArray, value)
    return []


def objects(value: object) -> list[JsonObject]:
    """The JSON objects inside an array, skipping anything else."""
    return [item for item in map(as_object, as_array(value)) if item is not None]


def get_object(value: object, key: str) -> JsonObject | None:
    """``value[key]`` when ``value`` is an object and that member is one too."""
    parent = as_object(value)
    return None if parent is None else as_object(parent.get(key))


def get_str(value: object, key: str, default: str = "") -> str:
    """``value[key]`` when it is a string, else ``default``."""
    parent = as_object(value)
    member = None if parent is None else parent.get(key)
    return member if isinstance(member, str) else default


def get_int(value: object, key: str, default: int = 0) -> int:
    """``value[key]`` when it is an integer (and not a bool), else ``default``."""
    parent = as_object(value)
    member = None if parent is None else parent.get(key)
    if isinstance(member, int) and not isinstance(member, bool):
        return member
    return default
