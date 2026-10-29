"""Logs scrubbed on write: the agents share the service's uid, so they can read its journal."""

import errno
import logging
import sys
import threading
import traceback
from collections.abc import Mapping
from pathlib import PurePath
from types import TracebackType
from typing import Final, TextIO, override

from uvicorn.logging import DefaultFormatter

type ExcInfo = (
    tuple[type[BaseException], BaseException, TracebackType | None] | tuple[None, None, None]
)

_DEFAULT_FMT: Final = "%(levelprefix)s %(message)s"
# The installed tree and this package both sit under a home directory; what follows is enough.
_ANCHORS: Final = ("site-packages/", "herdr_pwa/")
# How traceback.format_stack starts and splits each frame's line.
_FRAME_START: Final = '  File "'
_FRAME_REST: Final = '", line '
_STACK_HEAD: Final = "Stack (most recent call last):"
_TRACEBACK_HEAD: Final = "Traceback (most recent call last):"
_CAUSE: Final = "The above exception was the direct cause of the following exception:"
_CONTEXT: Final = "During handling of the above exception, another exception occurred:"
# Asyncio joins the task's repr, with its absolute path and the exception's text, into the message.
_ASYNCIO: Final = "asyncio"
# Each repr asyncio writes starts with one of these; the part before it is its fixed words.
_ASYNCIO_CUTS: Final = (" at ", "<")
_UNFORMATTED: Final = "an unformattable record"
# Captured warnings arrive formatted as warnings.formatwarning writes them, absolute path first.
_WARNINGS: Final = "py.warnings"
_UNCAUGHT: Final = logging.getLogger("herdr_pwa.uncaught")


class ScrubFilter(logging.Filter):
    """Names an exception or path passed as message or argument; nested values are not searched."""

    @override
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, BaseException | PurePath):
            record.msg = _scrubbed(record.msg)
        if isinstance(record.args, Mapping):
            record.args = {key: _scrubbed(value) for key, value in record.args.items()}
        elif record.args:
            record.args = tuple(_scrubbed(value) for value in record.args)
        if record.name == _ASYNCIO:
            record.msg, record.args = _asyncio_words(record), ()
        elif record.name == _WARNINGS:
            record.msg, record.args = _warning_words(record), ()
        # Another handler may have cached a traceback with messages; this one renders its own.
        record.exc_text = None
        return True


class ScrubHandler(logging.StreamHandler[TextIO]):
    """A stream handler whose own errors are said in one line, not a traceback with the args."""

    @override
    def handleError(self, record: logging.LogRecord) -> None:
        # As the stdlib's does: silent unless raiseExceptions, and stderr may be gone at exit.
        if not logging.raiseExceptions or sys.stderr is None:
            return
        kind = sys.exc_info()[0]
        name = kind.__name__ if kind else "an error"
        _ = sys.stderr.write(f"--- Logging error in {record.name}: {name} ---\n")


class ScrubFormatter(DefaultFormatter):
    """Uvicorn's default formatter, with tracebacks reduced to frames and class names."""

    @override
    def formatException(self, ei: ExcInfo) -> str:
        return scrubbed_traceback(ei)

    @override
    def formatStack(self, stack_info: str) -> str:
        return scrubbed_stack(stack_info)


def exception_name(exc: BaseException) -> str:
    """The class name, plus the errno's symbol for an OSError, since both are fixed words."""
    name = type(exc).__name__
    code = exc.errno if isinstance(exc, OSError) else None
    # An OSError takes any first argument as its errno, a list say, which no table holds.
    symbol = errno.errorcode.get(code) if isinstance(code, int) else None
    return f"{name}({symbol})" if symbol else name


def scrubbed_traceback(ei: ExcInfo) -> str:
    """Each frame's short file, line and function, and each chained exception's class only."""
    _, exc, _ = ei
    if exc is None:
        return ""
    return "\n".join(_chain_lines(exc, set()))


def scrubbed_stack(stack_info: str) -> str:
    """*stack_info*'s frame lines alone, each file cut to its short name; source lines can quote."""
    kept = [_STACK_HEAD] if stack_info.startswith(_STACK_HEAD) else []
    for line in stack_info.splitlines():
        filename, found, rest = line.removeprefix(_FRAME_START).rpartition(_FRAME_REST)
        if line.startswith(_FRAME_START) and found:
            kept.append(f"{_FRAME_START}{_short_file(filename)}{_FRAME_REST}{rest}")
    return "\n".join(kept)


def install_hooks() -> None:
    """Send warnings and uncaught exceptions, any thread's, through logging to be scrubbed too."""
    logging.captureWarnings(True)  # noqa: FBT003 - the stdlib's switch takes no keyword
    sys.excepthook = _log_uncaught
    threading.excepthook = _log_uncaught_in_thread


def logging_config() -> dict[str, object]:
    """A dictConfig for the root and uvicorn's loggers, shaped like uvicorn's LOGGING_CONFIG."""
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {"scrub": {"()": ScrubFilter}},
        "formatters": {
            "default": {"()": ScrubFormatter, "fmt": _DEFAULT_FMT, "use_colors": None},
        },
        "handlers": {
            "default": {
                "formatter": "default",
                "class": ScrubHandler,
                "stream": "ext://sys.stderr",
                "filters": ["scrub"],
            },
        },
        # No uvicorn.access: serve turns access logs off, which empties that logger anyway.
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"level": "INFO"},
        },
        # WARNING is the level the unconfigured root gave the app's own loggers before.
        "root": {"handlers": ["default"], "level": "WARNING"},
    }


# Logging takes any value as an argument (typeshed's _ArgsType), so this takes object.
def _scrubbed(value: object) -> object:
    if isinstance(value, BaseException):
        return exception_name(value)
    if isinstance(value, PurePath):
        return value.name
    return value


def _asyncio_words(record: logging.LogRecord) -> str:
    line = _first_line(record)
    for cut in _ASYNCIO_CUTS:
        line = line.partition(cut)[0]
    return line.rstrip() or _UNFORMATTED


def _first_line(record: logging.LogRecord) -> str:
    # Formatting runs the args' own __str__, which may fail; handleError would print them all.
    try:
        return record.getMessage().split("\n", 1)[0]
    except (TypeError, ValueError):
        return _UNFORMATTED


def _warning_words(record: logging.LogRecord) -> str:
    # The first line reads "<file>:<line>: <Category>: <message>"; the message can quote.
    where, found, rest = _first_line(record).partition(": ")
    filename, _, line = where.rpartition(":")
    if not (found and filename and line.isdigit()):
        return "a warning"
    return f"{rest.partition(':')[0]} at {_short_file(filename)}:{line}"


def _chain_lines(exc: BaseException, seen: set[int]) -> list[str]:
    # Oldest first, as Python prints a chain, so the exception raised last ends the block.
    seen.add(id(exc))
    cause, context = exc.__cause__, exc.__context__
    if cause is not None and id(cause) not in seen:
        head = [*_chain_lines(cause, seen), "", _CAUSE, ""]
    elif context is not None and not exc.__suppress_context__ and id(context) not in seen:
        head = [*_chain_lines(context, seen), "", _CONTEXT, ""]
    else:
        head = []
    frames = [
        f'  File "{_short_file(frame.f_code.co_filename)}", line {line}, in {frame.f_code.co_name}'
        for frame, line in traceback.walk_tb(exc.__traceback__)
    ]
    tail = [_TRACEBACK_HEAD, *frames] if frames else []
    return [*head, *tail, _qualified(exc)]


def _short_file(filename: str) -> str:
    for anchor in _ANCHORS:
        _, found, rest = filename.rpartition(anchor)
        if found:
            return f"{anchor}{rest}" if anchor == "herdr_pwa/" else rest
    return PurePath(filename).name


def _qualified(exc: BaseException) -> str:
    # As traceback names it: the module, unless builtin, then the class, then any errno symbol.
    module = type(exc).__module__
    name = exception_name(exc)
    return name if module in {"builtins", "__main__"} else f"{module}.{name}"


def _log_uncaught(kind: type[BaseException], exc: BaseException, tb: TracebackType | None) -> None:
    _UNCAUGHT.critical("uncaught exception", exc_info=(kind, exc, tb))


def _log_uncaught_in_thread(args: threading.ExceptHookArgs) -> None:
    # As threading's default hook does: a thread that exits by SystemExit is no error.
    if args.exc_type is SystemExit or args.exc_value is None:
        return
    name = args.thread.name if args.thread else "an unknown thread"
    exc_info = (args.exc_type, args.exc_value, args.exc_traceback)
    _UNCAUGHT.error("uncaught exception in %s", name, exc_info=exc_info)
