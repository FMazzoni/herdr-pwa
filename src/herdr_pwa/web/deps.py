"""What every router is handed: the app's shared state and its request guards."""

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from fastapi import Request

from herdr_pwa import auth
from herdr_pwa.audit import AuditLog
from herdr_pwa.engine import (
    CachedValue,
    EngineSettings,
    HerdrClient,
    Snapshot,
    SnapshotCache,
    TailCache,
    UsageCache,
)
from herdr_pwa.notify_state import SeenLedger
from herdr_pwa.settings import Settings
from herdr_pwa.web.failures import PageHidden
from herdr_pwa.web.render import Pages
from herdr_pwa.writes.outcome import Pacing

type ClientFactory = Callable[[EngineSettings], HerdrClient]

HIDDEN_HEADER = "HP-Hidden"


@dataclass(frozen=True, slots=True)
class WebState:
    """One app's settings, runner client, snapshot cache and renderer."""

    settings: Settings
    client: HerdrClient
    cache: SnapshotCache
    pages: Pages
    audit: AuditLog
    clock: Callable[[], float] = field(default=time.monotonic)
    # What a write's read-back waits run on; injected so route tests never sleep.
    pacing: Pacing = field(default_factory=Pacing)
    # The viewed transcripts, held parsed so a refresh reads only what was appended.
    tails: TailCache = field(default_factory=TailCache)
    # The viewed agents' usage tallies, grown by what their logs appended.
    usage: UsageCache = field(default_factory=UsageCache)
    # Wall time, for how long ago a stamp in a transcript was; injected so tests hold it still.
    wall: Callable[[], float] = field(default=time.time)
    # What the phone has shown; absent with push off, and then nothing is ever read.
    seen: SeenLedger | None = None

    def snapshot(self) -> CachedValue[Snapshot]:
        """The shared snapshot, fetched only if nobody else is fetching it."""
        return self.cache.get(self.client.snapshot)

    def access(self) -> auth.Access:
        """Who the settings let in."""
        return auth.Access(
            tailnet_login=self.settings.tailnet_login, assume_login=self.settings.assume_login
        )


def viewer_guard(web: WebState) -> Callable[[Request], auth.Identity]:
    """The dependency every router hangs off: authenticate, and origin-check writes."""

    def require_viewer(request: Request) -> auth.Identity:
        return auth.authorize(request.headers, access=web.access(), method=request.method)

    return require_viewer


def hidden_tick(request: Request) -> None:
    """Stop a hidden page's poll on the server, since htmx trigger filters need eval."""
    if request.headers.get(HIDDEN_HEADER) == "1":
        raise PageHidden
