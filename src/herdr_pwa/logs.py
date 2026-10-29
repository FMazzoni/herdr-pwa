"""Logs scrubbed on write: the agents share the service's uid, so they can read its journal."""

import logging
import re
import traceback
from collections.abc import Mapping
from pathlib import PurePath
from types import TracebackType
from typing import Final, override

from uvicorn.logging import AccessFormatter, DefaultFormatter

type ExcInfo = (
    tuple[type[BaseException], BaseException, TracebackType | None] | tuple[None, None, None]
)

_DEFAULT_FMT: Final = "%(levelprefix)s %(message)s"
_ACCESS_FMT: Final = '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s'
# The installed tree and this package both sit under a home directory; what follows is enough.
_ANCHORS: Final = ("site-packages/", "herdr_pwa/")
# The `File "..."` line traceback.format_stack writes for each frame.
_STACK_FILE: Final = re.compile(r'File "([^"]+)"')
_CAUSE: Final = "The above exception was the direct cause of the following exception:"
_CONTEXT: Final = "During handling of the above exception, another exception occurred:"


class ScrubFilter(logging.Filter):
    """Replaces exception and path arguments with their class name and file name."""

    @override
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, BaseException):
            record.msg = type(record.msg).__name__
        if isinstance(record.args, Mapping):
            record.args = {key: _scrubbed(value) for key, value in record.args.items()}
        elif record.args:
            record.args = tuple(_scrubbed(value) for value in record.args)
        # Another handler may have cached a traceback with messages; this one renders its own.
        record.exc_text = None
        return True


class ScrubFormatter(DefaultFormatter):
    """Uvicorn's default formatter, with tracebacks reduced to frames and class names."""

    @override
    def formatException(self, ei: ExcInfo) -> str:
        return scrubbed_traceback(ei)

    @override
    def formatStack(self, stack_info: str) -> str:
        return scrubbed_stack(stack_info)


class ScrubAccessFormatter(AccessFormatter):
    """Uvicorn's access formatter, with tracebacks reduced to frames and class names."""

    @override
    def formatException(self, ei: ExcInfo) -> str:
        return scrubbed_traceback(ei)

    @override
    def formatStack(self, stack_info: str) -> str:
        return scrubbed_stack(stack_info)


def scrubbed_traceback(ei: ExcInfo) -> str:
    """Each frame's short file, line and function, and each chained exception's class only."""
    _, exc, _ = ei
    if exc is None:
        return ""
    return "\n".join(_chain_lines(traceback.TracebackException.from_exception(exc)))


def scrubbed_stack(stack_info: str) -> str:
    """*stack_info* with every frame's absolute file cut to its short name."""
    return _STACK_FILE.sub(lambda match: f'File "{_short_file(match.group(1))}"', stack_info)


def logging_config() -> dict[str, object]:
    """A dictConfig for the root and uvicorn's loggers, shaped like uvicorn's LOGGING_CONFIG."""
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {"scrub": {"()": ScrubFilter}},
        "formatters": {
            "default": {"()": ScrubFormatter, "fmt": _DEFAULT_FMT, "use_colors": None},
            "access": {"()": ScrubAccessFormatter, "fmt": _ACCESS_FMT},
        },
        "handlers": {
            "default": {
                "formatter": "default",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stderr",
                "filters": ["scrub"],
            },
            "access": {
                "formatter": "access",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "filters": ["scrub"],
            },
        },
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"level": "INFO"},
            "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
        },
        # WARNING is the level the unconfigured root gave the app's own loggers before.
        "root": {"handlers": ["default"], "level": "WARNING"},
    }


def _scrubbed[T](value: T) -> T | str:
    if isinstance(value, BaseException):
        return type(value).__name__
    if isinstance(value, PurePath):
        return value.name
    return value


def _chain_lines(exc: traceback.TracebackException) -> list[str]:
    # Oldest first, as Python prints a chain, so the exception raised last ends the block.
    if exc.__cause__ is not None:
        head = [*_chain_lines(exc.__cause__), "", _CAUSE, ""]
    elif exc.__context__ is not None and not exc.__suppress_context__:
        head = [*_chain_lines(exc.__context__), "", _CONTEXT, ""]
    else:
        head = []
    frames = [
        f'  File "{_short_file(frame.filename)}", line {frame.lineno}, in {frame.name}'
        for frame in exc.stack
    ]
    tail = ["Traceback (most recent call last):", *frames] if frames else []
    return [*head, *tail, exc.exc_type_str]


def _short_file(filename: str) -> str:
    for anchor in _ANCHORS:
        _, found, rest = filename.rpartition(anchor)
        if found:
            return f"{anchor}{rest}" if anchor == "herdr_pwa/" else rest
    return PurePath(filename).name
