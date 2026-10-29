"""Logs scrubbed on write: the agents share the service's uid, so they can read its journal."""

import errno
import logging
import sys
import threading
import traceback
from collections.abc import Iterable, Mapping
from pathlib import PurePath
from types import TracebackType
from typing import Final, cast, override

from uvicorn.logging import AccessFormatter, DefaultFormatter

type ExcInfo = (
    tuple[type[BaseException], BaseException, TracebackType | None] | tuple[None, None, None]
)

_DEFAULT_FMT: Final = "%(levelprefix)s %(message)s"
_ACCESS_FMT: Final = '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s'
# The installed tree and this package both sit under a home directory; what follows is enough.
_ANCHORS: Final = ("site-packages/", "herdr_pwa/")
# How traceback.format_stack starts and splits each frame's line.
_FRAME_START: Final = '  File "'
_FRAME_REST: Final = '", line '
_STACK_HEAD: Final = "Stack (most recent call last):"
_TRACEBACK_HEAD: Final = "Traceback (most recent call last):"
_CAUSE: Final = "The above exception was the direct cause of the following exception:"
_CONTEXT: Final = "During handling of the above exception, another exception occurred:"
# How deep into list, tuple, set and dict arguments an exception or path is still found.
_NESTING: Final = 2
# Asyncio joins the task's repr, with its absolute path and the exception's text, into the message.
_ASYNCIO: Final = "asyncio"
_UNCAUGHT: Final = logging.getLogger("herdr_pwa.uncaught")


class ScrubFilter(logging.Filter):
    """Names exceptions and paths in arguments, two containers deep; not inside other objects."""

    @override
    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == _ASYNCIO and record.exc_info:
            record.msg, record.args = record.getMessage().split("\n", 1)[0], ()
        if isinstance(record.msg, BaseException):
            record.msg = exception_name(record.msg)
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


def exception_name(exc: BaseException) -> str:
    """The class name, plus the errno's symbol for an OSError, since both are fixed words."""
    name = type(exc).__name__
    code = exc.errno if isinstance(exc, OSError) else None
    symbol = errno.errorcode.get(code) if code is not None else None
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


def install_excepthooks() -> None:
    """Send uncaught exceptions, main thread or not, through logging, so they are scrubbed too."""
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


# Logging takes any value as an argument (typeshed's _ArgsType), so these take object.
def _scrubbed(value: object, depth: int = _NESTING) -> object:
    if isinstance(value, BaseException):
        return exception_name(value)
    if isinstance(value, PurePath):
        return value.name
    if depth <= 0:
        return value
    return _scrubbed_items(value, depth - 1)


def _scrubbed_items(value: object, depth: int) -> object:
    if isinstance(value, Mapping):
        # Isinstance cannot say a mapping's types; logging lets them be anything.
        pairs = cast("Mapping[object, object]", value).items()
        return {key: _scrubbed(item, depth) for key, item in pairs}
    if isinstance(value, list | tuple | set | frozenset):
        # A list, since a scrubbed member, a list itself, could not go back into a set.
        return [_scrubbed(item, depth) for item in cast("Iterable[object]", value)]
    return value


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
    return [*head, *tail, _qualified(exc), *_member_lines(exc, seen)]


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


def _member_lines(exc: BaseException, seen: set[int]) -> list[str]:
    if not isinstance(exc, BaseExceptionGroup):
        return []
    # Isinstance leaves the group's member type unknown; every member is a BaseException.
    members = cast("BaseExceptionGroup[BaseException]", exc).exceptions
    lines: list[str] = []
    for number, member in enumerate(members, start=1):
        lines.append(f"  +-- {number} of {len(members)}:")
        lines.extend(f"    {line}" for line in _chain_lines(member, seen))
    return lines


def _log_uncaught(kind: type[BaseException], exc: BaseException, tb: TracebackType | None) -> None:
    _UNCAUGHT.critical("uncaught exception", exc_info=(kind, exc, tb))


def _log_uncaught_in_thread(args: threading.ExceptHookArgs) -> None:
    # As threading's default hook does: a thread that exits by SystemExit is no error.
    if args.exc_type is SystemExit or args.exc_value is None:
        return
    name = args.thread.name if args.thread else "an unknown thread"
    exc_info = (args.exc_type, args.exc_value, args.exc_traceback)
    _UNCAUGHT.error("uncaught exception in %s", name, exc_info=exc_info)
