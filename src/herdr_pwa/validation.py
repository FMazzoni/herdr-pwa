"""What every owned model shares: fields that fall back instead of failing, and one error line."""

import re
from typing import Annotated, Final, cast

from pydantic import (
    GetCoreSchemaHandler,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
    ValidatorFunctionWrapHandler,
    WrapValidator,
)
from pydantic_core import CoreSchema, core_schema

from herdr_pwa.patterns import Rx


def or_default(default: object) -> WrapValidator:
    """A field validator that answers ``default`` for a value of the wrong shape.

    Use as ``Annotated[float, or_default(0.0)] = 0.0``: the plain default covers a missing
    key, this covers a present one that does not validate.
    """

    def validate(value: object, handler: ValidatorFunctionWrapHandler) -> object:
        try:
            # The handler is typed Any; what it returns is the field's own validated value.
            validated = cast("object", handler(value))
        except ValidationError:
            return default
        return validated

    return WrapValidator(validate)


class _OrNone:
    """`or_default(None)` decided inside pydantic-core, with no Python call per field."""

    def __get_pydantic_core_schema__(
        self, source: object, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        # Measured on a 67MB transcript: the Python wrap per field cost a page read ~10%.
        return core_schema.with_default_schema(handler(source), default=None, on_error="default")


# Use as ``Annotated[Model | None, OR_NONE] = None``: a value of the wrong shape reads as absent.
OR_NONE: Final = _OrNone()

# A field of a format we do not own: its value when it has the shape we read, else None.
type Text = Annotated[StrictStr | None, OR_NONE]
type Count = Annotated[StrictInt | None, OR_NONE]
type Number = Annotated[StrictInt | StrictFloat | None, OR_NONE]
type Flag = Annotated[StrictBool | None, OR_NONE]


# The shape a model id must have before it goes near an argv; fullmatch it, as ``$`` admits a final \n.
MODEL_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,95}")


def summary(exc: ValidationError) -> str:
    """Every problem pydantic found, as one line a person can act on."""
    problems: list[str] = []
    for error in exc.errors(include_url=False, include_input=False):
        where = ".".join(str(part) for part in error["loc"])
        problems.append(f"{where}: {error['msg']}" if where else error["msg"])
    return "; ".join(problems)


def is_not_json(exc: ValidationError) -> bool:
    """The text was not JSON at all, as opposed to JSON of the wrong shape."""
    return any(error["type"] == "json_invalid" for error in exc.errors())


PATTERNS: Final = (
    Rx(
        MODEL_ID,
        ("claude-opus-4-5", "openai/gpt-5.1", "moonshotai/Kimi-K2.5"),
        ("-x", "a b", "a" * 97, ""),
        "fullmatch",
        validator=True,
    ),
)
