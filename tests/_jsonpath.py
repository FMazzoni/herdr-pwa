"""Index into parsed JSON in a test, with the same failures plain indexing would raise."""

from pydantic import JsonValue


def at(value: JsonValue, *path: str | int) -> JsonValue:
    """``value[k1][k2]...``, raising ``TypeError``/``KeyError``/``IndexError`` like ``[]``."""
    for key in path:
        if isinstance(key, str):
            if not isinstance(value, dict):
                raise TypeError(f"cannot index {type(value).__name__} with {key!r}")
            value = value[key]
        else:
            if not isinstance(value, list):
                raise TypeError(f"cannot index {type(value).__name__} with {key!r}")
            value = value[key]
    return value


def obj(value: JsonValue, *path: str | int) -> dict[str, JsonValue]:
    """``at(value, *path)``, which must be a JSON object."""
    found = at(value, *path)
    if not isinstance(found, dict):
        raise TypeError(f"expected an object at {path!r}")
    return found
