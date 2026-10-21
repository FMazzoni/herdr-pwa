"""The wire seam the engine and write tests share: a fake transport and a clock that moves."""

import json
from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import JsonValue

from herdr_pwa.clock import MonoTime
from herdr_pwa.engine import WireReply
from tests.engine_seam import decode_frame

# A canned frame, a failure to raise, or a function of the request params.
type Reply = (
    dict[str, JsonValue] | Exception | Callable[[dict[str, JsonValue]], dict[str, JsonValue]]
)


def as_reply(frame: dict[str, JsonValue]) -> WireReply:
    """A canned frame read the way the real wire reads the bytes it arrives as."""
    return decode_frame(json.dumps(frame).encode())


class TickingClock:
    """A monotonic clock that moves a millisecond each time it is read."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> MonoTime:
        self.now += 0.001
        return MonoTime(self.now)


@dataclass(frozen=True, slots=True)
class Call:
    method: str
    params: dict[str, JsonValue]
    timeout: float | None
    idempotent: bool


# Mutable: a fake that records being closed.
@dataclass(slots=True)
class FakeTransport:
    """The wire seam, recording every request and answering canned frames."""

    replies: dict[str, Reply] = field(default_factory=dict[str, Reply])
    calls: list[Call] = field(default_factory=list)
    closed: int = 0

    def call(
        self,
        method: str,
        params: dict[str, JsonValue],
        *,
        timeout: float | None = None,
        idempotent: bool = True,
    ) -> WireReply:
        self.calls.append(Call(method, dict(params), timeout, idempotent))
        reply = self.replies.get(method, {"id": "x", "result": {"type": "ok"}})
        if isinstance(reply, Exception):
            raise reply
        return as_reply(reply(params) if callable(reply) else reply)

    def close(self) -> None:
        self.closed += 1

    @property
    def methods(self) -> list[str]:
        return [c.method for c in self.calls]

    @property
    def last(self) -> Call:
        return self.calls[-1]


def no_sleep(_seconds: float) -> None:
    """The injected sleep: a retry or a poll interval costs no wall time."""
