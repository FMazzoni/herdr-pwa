"""The event stream: one herd watcher telling each page which of its regions went stale."""

import asyncio
import contextlib
import logging
import re
import threading
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from herdr_pwa.decisions import NO_OVERLAY, Overlay, effective_status, shows_question
from herdr_pwa.engine import (
    CachedSnapshot,
    EngineError,
    EventStream,
    Snapshot,
    has_byte_frontier,
    locate,
)
from herdr_pwa.patterns import Rx
from herdr_pwa.web.deps import WebState
from herdr_pwa.web.herd import overlay_of

log = logging.getLogger(__name__)

TOPIC: Final = re.compile(
    r"""
    feed  # every card
  | (?:agent|chat|pane|stats) : [0-9A-Za-z][0-9A-Za-z_.-]{0,79}  # one agent's, by its id
    """,
    re.VERBOSE,
)
MAX_TOPICS: Final = 8
# A transcript is found again this often, so a fork the conversation moved to is followed.
RELOCATE_SECONDS: Final = 10.0
UPSTREAM_BACKOFF: Final = (1.0, 30.0)

type Marks = dict[str, object]


def topics_of(asked: Iterable[str]) -> frozenset[str]:
    """The well-formed topics in *asked*; anything else is dropped, never echoed."""
    found = {t for t in asked if TOPIC.fullmatch(t)}
    return frozenset(sorted(found)[:MAX_TOPICS])


def herd_marks(snapshot: Snapshot, error: str = "", overlay: Overlay = NO_OVERLAY) -> Marks:
    """What each region shows of the herd, reduced to a value that changes when it should."""
    marks: Marks = {}
    rows: list[tuple[object, ...]] = []
    for agent in snapshot.agents:
        blocked = shows_question(agent, overlay)
        row = (
            agent.target,
            agent.title,
            # The shown status, so a finish being read redraws the feed and the page as idle.
            effective_status(agent, overlay).value,
            agent.agent,
            agent.workspace,
            agent.state_change_seq,
            # A blocked screen's question is read from the screen, so its revision is part of it.
            agent.revision if blocked else 0,
        )
        rows.append((agent.session, *row))
        if not agent.session:
            continue
        marks[f"agent:{agent.session}"] = row
        marks[f"pane:{agent.session}"] = (agent.target, agent.status.value, agent.revision)
        if not has_byte_frontier(agent.agent):
            # No byte frontier to watch: the screen moving is the best sign the store did.
            marks[f"chat:{agent.session}"] = (agent.status.value, agent.revision)
            marks[f"stats:{agent.session}"] = (agent.status.value, agent.revision)
    workspaces = tuple((w.workspace_id, w.label, w.number) for w in snapshot.workspaces)
    marks["feed"] = (error, tuple(rows), workspaces)
    return marks


def changed(before: Mapping[str, object], after: Mapping[str, object]) -> set[str]:
    """Topics whose mark moved or vanished; one seen for the first time is a baseline, not news."""
    moved = {k for k, v in after.items() if k in before and before[k] != v}
    return moved | (set(before) - set(after))


# Mutable: the hub sets its pending and closed flags as events arrive.
@dataclass(eq=False, slots=True)
class Listener:
    """One open page: the topics it shows and the ones it has not been told about yet."""

    topics: frozenset[str]
    joined: float
    pending: set[str] = field(default_factory=set[str])
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    closed: bool = False

    def tell(self, stale: Iterable[str]) -> None:
        """Queue every stale topic this page shows; repeats before it reads them merge."""
        mine = self.topics.intersection(stale)
        if mine:
            self.pending |= mine
            self.wake.set()

    def close(self) -> None:
        """End this page's stream at its next wake."""
        self.closed = True
        self.wake.set()


@dataclass(frozen=True, slots=True)
class HubConfig:
    """How the hub paces itself."""

    interval: float = 1.0
    heartbeat: float = 15.0
    max_clients: int = 16
    lifetime: float = 600.0


# Mutable: a transcript that vanishes drops its path in place.
@dataclass(slots=True)
class _Watch:
    """One transcript a page is reading: where it is and what it looked like."""

    path: Path | None
    found_at: float


class Hub:
    """One watcher over the herd and the viewed transcripts, and every page listening to it."""

    def __init__(self, web: WebState, config: HubConfig) -> None:
        self._web = web
        self._config = config
        # The app's own clock, so a test that holds time still holds the streams' ages too.
        self._clock = web.clock
        self._listeners: list[Listener] = []
        self._task: asyncio.Task[None] | None = None
        self._wake: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._marks: Marks = {}
        self._watches: dict[str, _Watch] = {}
        self._upstream = _Upstream(web)
        # Counted so a test can wait for a send or a look instead of a sleep.
        self.sent = 0
        self.looks = 0

    @property
    def listeners(self) -> int:
        """How many pages hold a stream now."""
        return len(self._listeners)

    def join(self, topics: frozenset[str]) -> Listener:
        """Add a page; past the bound the oldest page is let go, and it reconnects or polls."""
        listener = Listener(topics, joined=self._clock())
        while len(self._listeners) >= self._config.max_clients:
            self._listeners.pop(0).close()
        self._listeners.append(listener)
        self._start()
        return listener

    def leave(self, listener: Listener) -> None:
        """Forget a page whose stream ended."""
        listener.closed = True
        if listener in self._listeners:
            self._listeners.remove(listener)

    def publish(self, stale: Iterable[str]) -> None:
        """Tell every page showing one of *stale* that it is."""
        topics = set(stale)
        for listener in self._listeners:
            listener.tell(topics)

    def drop_all(self) -> None:
        """End every stream now, from any thread; the pages fall back to polling and reconnect."""
        loop = self._loop
        if loop is not None and not _on(loop):
            loop.call_soon_threadsafe(self.drop_all)
            return
        for listener in list(self._listeners):
            listener.close()
            self.leave(listener)

    async def stream(self, listener: Listener) -> AsyncIterator[bytes]:
        """The bytes of one page's stream, until it closes, ages out or the page goes."""
        try:
            # A comment first, so the headers and the first byte pass any buffering proxy.
            yield b": open\n\n"
            while not listener.closed:
                if self._clock() - listener.joined > self._config.lifetime:
                    # Said apart from a lost stream, so the page reconnects without redrawing all.
                    yield b"event: rotate\ndata: lifetime\n\n"
                    return
                yield await self._next(listener)
        finally:
            self.leave(listener)

    async def _next(self, listener: Listener) -> bytes:
        try:
            await asyncio.wait_for(listener.wake.wait(), self._config.heartbeat)
        except TimeoutError:
            return b": hb\n\n"
        listener.wake.clear()
        stale, listener.pending = sorted(listener.pending), set()
        self.sent += len(stale)
        return b"".join(f"event: stale\ndata: {topic}\n\n".encode() for topic in stale)

    async def close(self) -> None:
        """Stop watching and end every stream."""
        self.drop_all()
        self._upstream.stop()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # -- the watcher ------------------------------------------------------------

    def _start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._task = self._loop.create_task(self._run())

    def _poke(self) -> None:
        """Wake the watcher early; called from the upstream thread."""
        loop, wake = self._loop, self._wake
        if loop is not None and wake is not None:
            loop.call_soon_threadsafe(wake.set)

    async def _run(self) -> None:
        fresh = False
        try:
            while self._listeners:
                # One bad look never ends the watcher: a live page has turned its own polling off.
                try:
                    marks = await asyncio.to_thread(self._look, self._viewed(), fresh=fresh)
                except Exception:
                    log.exception("the event watcher's look failed")
                else:
                    stale = changed(self._marks, marks) if self._marks else set[str]()
                    self._marks = marks
                    if stale:
                        self.publish(stale)
                self.looks += 1
                fresh = await self._sleep()
        finally:
            self._upstream.stop()
            self._marks = {}

    async def _sleep(self) -> bool:
        """Wait one interval, or less if herdr said something moved; true when it did."""
        wake = self._wake
        if wake is None:
            return False
        try:
            await asyncio.wait_for(wake.wait(), self._config.interval)
        except TimeoutError:
            return False
        wake.clear()
        return True

    def _viewed(self) -> frozenset[str]:
        """The sessions whose chat or stats some page is showing: both are read off the log."""
        return frozenset(
            t.partition(":")[2]
            for listener in self._listeners
            for t in listener.topics
            if t.startswith(("chat:", "stats:"))
        )

    def _look(self, viewed: frozenset[str], *, fresh: bool) -> Marks:
        """Every topic's mark now: the herd from the shared snapshot, chats from their files."""
        entry = self._snapshot(fresh=fresh)
        if entry is None:
            return {"feed": ("unreachable",)}
        snapshot = entry.value
        error = type(entry.error).__name__ if entry.error is not None else ""
        marks = herd_marks(snapshot, error, overlay_of(self._web, snapshot.agents))
        self._upstream.follow(snapshot, self._poke)
        kinds = {str(a.session): (a.agent, a.status.value) for a in snapshot.agents if a.session}
        marks.update(self._chats(viewed, kinds))
        return marks

    def _snapshot(self, *, fresh: bool) -> CachedSnapshot | None:
        try:
            # Woken by herdr, the cached copy is the one thing known to be old.
            return self._web.cache.get(self._web.client.snapshot, max_age=0 if fresh else None)
        except EngineError as exc:
            log.debug("the event watcher could not read the herd: %s", exc)
            return None

    def _chats(self, viewed: frozenset[str], kinds: Mapping[str, tuple[str, str]]) -> Marks:
        """An appended-to log's mark is its identity and size: one stat, no read."""
        marks: Marks = {}
        for session in viewed:
            kind, status = kinds.get(session, ("", ""))
            if has_byte_frontier(kind):
                stat = self._stat(session)
                # Both also say whether a turn is running or waiting, which the log alone cannot.
                marks[f"chat:{session}"] = (status, stat)
                marks[f"stats:{session}"] = (status, stat)
        for gone in set(self._watches) - viewed:
            del self._watches[gone]
        return marks

    def _stat(self, session: str) -> object:
        now = self._clock()
        watch = self._watches.get(session)
        if watch is None or watch.path is None or now - watch.found_at > RELOCATE_SECONDS:
            roots = self._web.settings.transcript_roots or None
            watch = self._watches[session] = _Watch(locate(session, root=roots), now)
        if watch.path is None:
            return None
        try:
            stat = watch.path.stat()
        except OSError:
            watch.path = None
            return None
        # No mtime: a write stamps it before its bytes land, so it would announce a change twice.
        return (str(watch.path), stat.st_ino, stat.st_size)


def _on(loop: asyncio.AbstractEventLoop) -> bool:
    """Whether the caller is running on *loop*."""
    try:
        return asyncio.get_running_loop() is loop
    except RuntimeError:
        return False


class _Upstream:
    """The one herdr subscription every page shares; it only wakes the watcher early."""

    def __init__(self, web: WebState) -> None:
        self._web = web
        self._lock = threading.Lock()
        self._panes: tuple[str, ...] = ()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._stream: EventStream | None = None

    def follow(self, snapshot: Snapshot, poke: Callable[[], None]) -> None:
        """Run, subscribed to every agent's pane in *snapshot*; resubscribe when they change."""
        panes = tuple(sorted({str(a.target) for a in snapshot.agents}))
        with self._lock:
            moved = panes != self._panes
            self._panes = panes
            running = self._thread is not None and self._thread.is_alive()
            if not running:
                self._stop = threading.Event()
                self._thread = threading.Thread(
                    target=self._listen, args=(poke, self._stop), name="hp-events", daemon=True
                )
                self._thread.start()
            elif moved:
                self._close_stream()

    def stop(self) -> None:
        """End the subscription and its thread."""
        with self._lock:
            self._stop.set()
            self._close_stream()
            self._thread = None

    def _close_stream(self) -> None:
        if self._stream is not None:
            self._stream.close()

    def _listen(self, poke: Callable[[], None], stop: threading.Event) -> None:
        delay = UPSTREAM_BACKOFF[0]
        while not stop.is_set():
            with self._lock:
                stream = self._web.client.watch_statuses(self._panes)
                self._stream = stream
            try:
                for _ in stream:
                    delay = UPSTREAM_BACKOFF[0]
                    poke()
            except EngineError as exc:
                log.debug("the herdr subscription ended: %s", exc)
            if stream.closed:
                # Closed on purpose: the pane set moved, so subscribe again at once.
                continue
            stop.wait(delay)
            delay = min(delay * 2, UPSTREAM_BACKOFF[1])


PATTERNS: Final = (
    Rx(
        TOPIC,
        ("feed", "agent:abc", "stats:a.b-c_d"),
        ("feeds", "agent:", "agent:-x", "chat:" + "a" * 81, "pane:a b"),
        "fullmatch",
        validator=True,
    ),
)
