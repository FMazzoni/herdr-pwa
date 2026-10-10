"""Index into parsed JSON in a test, with the same failures plain indexing would raise."""

from pydantic import JsonValue

from herdr_pwa.jsontypes import JsonObject, as_object


def at(value: JsonValue, *path: str | int) -> JsonValue:
    """``value[k1][k2]...``, raising ``TypeError``/``KeyError``/``IndexError`` like ``[]``."""
    for key in path:
        if isinstance(key, str):
            found = as_object(value)
            if found is None:
                raise TypeError(f"cannot index {type(value).__name__} with {key!r}")
            value = found[key]
        else:
            if not isinstance(value, list):
                raise TypeError(f"cannot index {type(value).__name__} with {key!r}")
            value = value[key]
    return value


def obj(value: JsonValue, *path: str | int) -> JsonObject:
    """``at(value, *path)``, which must be a JSON object."""
    found = as_object(at(value, *path))
    if found is None:
        raise TypeError(f"expected an object at {path!r}")
    return found
