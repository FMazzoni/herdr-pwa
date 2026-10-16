"""The wire seam the engine and write tests share: a fake transport and a clock that moves."""

from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import JsonValue

# A canned frame, a failure to raise, or a function of the request params.
type Reply = (
    dict[str, JsonValue] | Exception | Callable[[dict[str, JsonValue]], dict[str, JsonValue]]
)


class TickingClock:
    """A monotonic clock that moves a millisecond each time it is read."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        self.now += 0.001
        return self.now


@dataclass
class Call:
    method: str
    params: dict[str, JsonValue]
    timeout: float | None
    idempotent: bool


@dataclass
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
    ) -> dict[str, JsonValue]:
        self.calls.append(Call(method, dict(params), timeout, idempotent))
        reply = self.replies.get(method, {"id": "x", "result": {"type": "ok"}})
        if isinstance(reply, Exception):
            raise reply
        return reply(params) if callable(reply) else reply

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
