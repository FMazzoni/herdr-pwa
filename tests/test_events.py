"""The event stream: which topics a change makes stale, the fan-out, and the route."""

import asyncio
import json
import os
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI

from herdr_pwa.engine import Snapshot
from herdr_pwa.web.events import Hub, Listener, Marks, changed, herd_marks, topics_of
from tests.catalogue import long_chat
from tests.waiting import until_async
from tests.webfakes import (
    IDENTITY,
    READ,
    SNAPSHOT,
    WORKING_SESSION,
    Rig,
    agents_of,
    assistant_says,
    build_rig,
    read_frame,
    snapshot_frame,
    write_transcript,
)

IDLE = "5b609dba-d756-493a-a3fc-b4e09bf0a183"
WAIT = 5.0


def _snapshot(**edits: Mapping[str, str | int]) -> Snapshot:
    """The recorded herd, with the named session's raw agent fields edited."""
    frame = snapshot_frame()
    for agent in agents_of(frame):
        session = agent["agent_session"]
        value = session.get("value") if isinstance(session, dict) else None
        if isinstance(value, str) and value in edits:
            for key, field in edits[value].items():
                agent[key] = field
    result = frame["result"]
    assert isinstance(result, dict)
    raw = result["snapshot"]
    assert isinstance(raw, dict)
    return Snapshot.from_raw(raw)


# What is stale


def test_only_well_formed_topics_are_kept() -> None:
    asked = ["feed", f"chat:{WORKING_SESSION}", "chat:../../etc", "agent:", "nope", "pane:a b"]
    assert topics_of(asked) == {"feed", f"chat:{WORKING_SESSION}"}
    assert topics_of([f"stats:{WORKING_SESSION}"]) == {f"stats:{WORKING_SESSION}"}
    assert len(topics_of([f"agent:s{n}" for n in range(40)])) == 8


def test_a_status_change_is_stale_for_the_feed_and_the_agent_not_its_pane_alone() -> None:
    before = herd_marks(_snapshot())
    after = herd_marks(_snapshot(**{IDLE: {"agent_status": "blocked", "state_change_seq": 99}}))
    stale = changed(before, after)
    assert {"feed", f"agent:{IDLE}", f"pane:{IDLE}"} <= stale
    assert f"agent:{WORKING_SESSION}" not in stale


def test_a_working_screen_moving_is_stale_for_its_pane_only() -> None:
    before = herd_marks(_snapshot())
    after = herd_marks(_snapshot(**{WORKING_SESSION: {"revision": 320}}))
    assert changed(before, after) == {f"pane:{WORKING_SESSION}"}


def test_a_blocked_screen_moving_is_stale_for_its_question() -> None:
    blocked = {"agent_status": "blocked"}
    before = herd_marks(_snapshot(**{IDLE: blocked}))
    after = herd_marks(_snapshot(**{IDLE: {**blocked, "revision": 3}}))
    assert changed(before, after) == {"feed", f"agent:{IDLE}", f"pane:{IDLE}"}


def test_an_agent_that_left_is_stale_everywhere_it_was_shown() -> None:
    before = herd_marks(_snapshot())
    frame = snapshot_frame()
    kept = [a for a in agents_of(frame) if IDLE not in json.dumps(a)]
    result = frame["result"]
    assert isinstance(result, dict)
    raw = result["snapshot"]
    assert isinstance(raw, dict)
    raw["agents"] = kept  # pyright: ignore[reportArgumentType]
    stale = changed(before, herd_marks(Snapshot.from_raw(raw)))
    assert {"feed", f"agent:{IDLE}", f"pane:{IDLE}"} <= stale


def test_nothing_moving_is_nothing_stale_and_a_first_look_is_a_baseline() -> None:
    marks = herd_marks(_snapshot())
    assert changed(marks, herd_marks(_snapshot())) == set()
    assert changed({}, marks) == set()


# The fan-out


def _hub(tmp_path: Path, *, lifetime: float = 600.0, clients: int = 16) -> tuple[Hub, Rig]:
    rig = build_rig(
        tmp_path,
        events_interval=0.05,
        events_heartbeat=0.2,
        events_lifetime=lifetime,
        events_max_clients=clients,
    )
    app = rig.http.app
    assert isinstance(app, FastAPI)
    hub = cast(object, app.state.events)
    assert isinstance(hub, Hub)
    return hub, rig


async def _said(stream: AsyncIterator[bytes], want: str, seconds: float = WAIT) -> list[str]:
    """Every stale topic the stream says until it says *want*."""
    heard: list[str] = []

    async def listen() -> None:
        async for chunk in stream:
            heard.extend(
                line[6:] for line in chunk.decode().splitlines() if line.startswith("data: ")
            )
            if want in heard:
                return

    await asyncio.wait_for(listen(), seconds)
    return heard


def test_a_page_hears_only_its_own_topics_and_repeats_merge(tmp_path: Path) -> None:
    hub, _rig = _hub(tmp_path)

    async def scenario() -> tuple[set[str], set[str]]:
        chat = hub.join(frozenset({f"chat:{WORKING_SESSION}"}))
        feed = hub.join(frozenset({"feed"}))
        for _ in range(3):
            hub.publish({f"chat:{WORKING_SESSION}", "pane:other"})
        mine, theirs = set(chat.pending), set(feed.pending)
        await hub.close()
        return mine, theirs

    mine, theirs = asyncio.run(scenario())
    assert mine == {f"chat:{WORKING_SESSION}"}
    assert theirs == set()


def test_past_the_bound_the_oldest_page_is_let_go(tmp_path: Path) -> None:
    hub, _rig = _hub(tmp_path, clients=2)

    async def scenario() -> list[Listener]:
        pages = [hub.join(frozenset({"feed"})) for _ in range(3)]
        count = hub.listeners
        await hub.close()
        assert count == 2
        return pages

    first, second, third = asyncio.run(scenario())
    assert first.closed
    assert not second.pending
    assert third.topics == {"feed"}


def test_a_stream_opens_with_a_comment_beats_while_quiet_and_ends_with_its_lifetime(
    tmp_path: Path,
) -> None:
    hub, rig = _hub(tmp_path, lifetime=0.5)

    async def scenario() -> list[bytes]:
        listener = hub.join(frozenset({"feed"}))
        chunks: list[bytes] = []
        async for chunk in hub.stream(listener):
            chunks.append(chunk)
            if chunk == b": hb\n\n":
                # Quiet for a beat: now age the stream past its lifetime on the app's clock.
                rig.clock.now += 1.0
        await hub.close()
        return chunks

    chunks = asyncio.run(asyncio.wait_for(scenario(), WAIT))
    assert chunks[0].startswith(b": open\n")
    assert b": hb\n\n" in chunks
    assert hub.listeners == 0


def test_an_appended_transcript_is_one_stale_chat(tmp_path: Path) -> None:
    write_transcript(tmp_path / "transcripts", WORKING_SESSION, long_chat(6))
    hub, _rig = _hub(tmp_path)
    log = next((tmp_path / "transcripts").rglob(f"{WORKING_SESSION}.jsonl"))
    topic = f"chat:{WORKING_SESSION}"

    async def scenario() -> tuple[list[str], set[str]]:
        listener = hub.join(frozenset({topic, "feed"}))
        stream = hub.stream(listener)
        await _looked(hub, 1)
        with log.open("a") as handle:
            handle.write(json.dumps(assistant_says("appended", uuid="z9")) + "\n")
        heard = await _said(stream, topic)
        # Two more looks at the unchanged log: a second stale would be queued by now.
        await _looked(hub, hub.looks + 2)
        again = set(listener.pending)
        await hub.close()
        return heard, again

    heard, again = asyncio.run(scenario())
    assert heard == [topic]
    assert again == set()


def test_an_append_whose_time_lands_before_its_bytes_is_still_one_stale_chat(
    tmp_path: Path,
) -> None:
    """Linux stamps a write's mtime before it copies the bytes and grows the size, so a look
    can fall between the two. The chat read then is the one before the write, and the look
    after it would tell the page again: two refreshes for one change. Here the time moves
    alone first, as a touch, and then the bytes arrive."""
    write_transcript(tmp_path / "transcripts", WORKING_SESSION, long_chat(6))
    hub, _rig = _hub(tmp_path)
    log = next((tmp_path / "transcripts").rglob(f"{WORKING_SESSION}.jsonl"))
    topic = f"chat:{WORKING_SESSION}"

    async def scenario() -> tuple[set[str], list[str]]:
        listener = hub.join(frozenset({topic}))
        stream = hub.stream(listener)
        await _looked(hub, 1)
        stamped = log.stat().st_mtime_ns + 1_000_000_000
        os.utime(log, ns=(stamped, stamped))
        await _looked(hub, hub.looks + 2)
        early = set(listener.pending)
        with log.open("a") as handle:
            handle.write(json.dumps(assistant_says("appended", uuid="z9")) + "\n")
        heard = await _said(stream, topic)
        await hub.close()
        return early, heard

    early, heard = asyncio.run(scenario())
    assert early == set()
    assert heard == [topic]


async def _looked(hub: Hub, looks: int) -> None:
    """Wait until the watcher has looked ``looks`` times: the first look is the baseline."""
    await until_async(lambda: hub.looks >= looks, WAIT)


def test_an_appended_transcript_is_stale_for_the_stats_strip_too(tmp_path: Path) -> None:
    """The strip sums the log, so a page showing only the strip, as on Pane, is still told."""
    write_transcript(tmp_path / "transcripts", WORKING_SESSION, long_chat(6))
    hub, _rig = _hub(tmp_path)
    log = next((tmp_path / "transcripts").rglob(f"{WORKING_SESSION}.jsonl"))
    topic = f"stats:{WORKING_SESSION}"

    async def scenario() -> list[str]:
        listener = hub.join(frozenset({topic, f"pane:{WORKING_SESSION}"}))
        stream = hub.stream(listener)
        await _looked(hub, 1)
        with log.open("a") as handle:
            handle.write(json.dumps(assistant_says("appended", uuid="z9")) + "\n")
        heard = await _said(stream, topic)
        await hub.close()
        return heard

    assert asyncio.run(scenario()) == [topic]


def test_a_status_change_is_stale_for_the_strip_of_a_harness_with_no_log() -> None:
    """A turn starting or ending moves the strip's turn time; opencode's store moves with it."""
    kind = {"agent": "opencode"}
    before = herd_marks(_snapshot(**{IDLE: kind}))
    after = herd_marks(_snapshot(**{IDLE: {**kind, "agent_status": "working"}}))
    assert f"stats:{IDLE}" in changed(before, after)


def test_a_herd_change_reaches_the_feed_and_the_agent(tmp_path: Path) -> None:
    hub, rig = _hub(tmp_path)

    async def scenario() -> list[str]:
        listener = hub.join(frozenset({"feed", f"agent:{IDLE}"}))
        stream = hub.stream(listener)
        await _looked(hub, 1)
        frame = snapshot_frame()
        for agent in agents_of(frame):
            if IDLE in json.dumps(agent):
                agent["agent_status"] = "blocked"
        rig.wire.replies[SNAPSHOT] = frame
        rig.clock.sleep(60.0)
        heard = await _said(stream, f"agent:{IDLE}")
        await hub.close()
        return heard

    assert set(asyncio.run(scenario())) == {"feed", f"agent:{IDLE}"}


def test_a_look_that_raises_is_logged_and_a_later_change_still_reaches_the_page(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    hub, rig = _hub(tmp_path)
    looked = hub._look  # pyright: ignore[reportPrivateUsage]
    calls: list[int] = []

    def flaky(viewed: frozenset[str], *, fresh: bool) -> Marks:
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("a store the look reads broke")
        return looked(viewed, fresh=fresh)

    hub._look = flaky  # pyright: ignore[reportPrivateUsage]

    async def scenario() -> list[str]:
        listener = hub.join(frozenset({"feed", f"agent:{IDLE}"}))
        stream = hub.stream(listener)
        await _looked(hub, 2)
        frame = snapshot_frame()
        for agent in agents_of(frame):
            if IDLE in json.dumps(agent):
                agent["agent_status"] = "blocked"
        rig.wire.replies[SNAPSHOT] = frame
        rig.clock.sleep(60.0)
        heard = await _said(stream, f"agent:{IDLE}")
        await hub.close()
        return heard

    assert set(asyncio.run(scenario())) == {"feed", f"agent:{IDLE}"}
    assert "the event watcher's look failed" in caplog.text


# The route


def test_the_route_is_behind_the_gate_and_streams_unbuffered(tmp_path: Path) -> None:
    rig = _hub(tmp_path, lifetime=0.3)[1]
    # Every look at the clock ages the stream a second, so it ends at its first check.
    rig.clock.step = 1.0
    refused = rig.http.get("/events?topic=feed", headers={"Tailscale-User-Login": ""})
    assert refused.status_code == 401
    assert rig.http.get("/events?topic=nope", headers=IDENTITY).status_code == 400
    answer = rig.http.get("/events?topic=feed", headers={**IDENTITY, "Accept-Encoding": "gzip"})
    assert answer.status_code == 200
    assert answer.headers["content-type"].startswith("text/event-stream")
    assert "content-encoding" not in answer.headers
    assert answer.headers["cache-control"] == "no-store"
    assert "connect-src 'self'" in answer.headers["content-security-policy"]
    assert answer.text.startswith(": open\n")


@pytest.mark.parametrize("tab", ["chat", "pane"])
def test_the_agent_page_names_its_topics(tmp_path: Path, tab: str) -> None:
    # The page draws its view, so the Pane tab reads the screen as it is served.
    rig = build_rig(tmp_path, {SNAPSHOT: snapshot_frame(), READ: read_frame("$ ")})
    page = rig.http.get(f"/agent/{WORKING_SESSION}?tab={tab}").text
    assert f'data-hp-topic="agent:{WORKING_SESSION}"' in page
    assert f'data-hp-topic="{tab}:{WORKING_SESSION}"' in page
    assert f'data-hp-topic="stats:{WORKING_SESSION}"' in page
    assert 'data-hp-fallback="20"' in page
