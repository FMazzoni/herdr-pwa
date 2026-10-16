"""What every owned model shares: fields that fall back instead of failing, and one error line."""

from typing import Annotated, cast

from pydantic import (
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
    ValidatorFunctionWrapHandler,
    WrapValidator,
)


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


# A field of a format we do not own: its value when it has the shape we read, else None.
type Text = Annotated[StrictStr | None, or_default(None)]
type Count = Annotated[StrictInt | None, or_default(None)]
type Number = Annotated[StrictInt | StrictFloat | None, or_default(None)]
type Flag = Annotated[StrictBool | None, or_default(None)]


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
