"""What an entry point reaches outside itself, grouped so a test swaps it in one place."""

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from herdr_pwa.clock import Clock, Sleep
from herdr_pwa.engine import EngineSettings, HerdrClient
from herdr_pwa.push import Deliver
from herdr_pwa.tailscale import LocalApi
from herdr_pwa.writes.outcome import Pacing

type ClientFactory = Callable[[EngineSettings], HerdrClient]


@dataclass(frozen=True, slots=True)
class Seams:
    """Time, the runner, the push service, tailscaled and the terminal, as one app sees them.

    ``cli.main`` builds the real ones; a test builds its own and hands it to the same entry
    point. Nothing below an entry point reads the clock or opens a connection on its own.
    """

    # Intervals, deadlines and cache ages.
    clock: Clock = time.monotonic
    # Stamps written to disk, and how long ago one was.
    wall: Clock = time.time
    # The runner client's retry and poll waits.
    sleep: Sleep = time.sleep
    # What a write's read-back waits run on.
    pacing: Pacing = field(default_factory=Pacing)
    # None is the real runner client on these clocks.
    client_factory: ClientFactory | None = None
    # None is the real sender, armed with this box's key.
    deliver: Deliver | None = None
    write: Callable[[str], object] = sys.stdout.write
    local_api: Callable[[Path], LocalApi] = LocalApi

    def client(self, engine: EngineSettings) -> HerdrClient:
        """The runner client: the injected one, or the real one keeping time on these clocks."""
        if self.client_factory is not None:
            return self.client_factory(engine)
        return HerdrClient(engine, clock=self.clock, sleep=self.sleep)
