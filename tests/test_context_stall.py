"""An agent stopped on a full context needs the person: its card asks, and it pushes once."""

import json
import re
from pathlib import Path

import pytest
from pydantic import JsonValue

from herdr_pwa.decisions import Overlay, context_stalled, read_status, reads_stall
from herdr_pwa.engine import AgentStatus, SessionId, Snapshot, TranscriptTurn
from herdr_pwa.engine.notices import CONTEXT_FULL
from herdr_pwa.web.events import changed, herd_marks
from tests._jsonpath import obj
from tests.catalogue import api_error_records, slash_command_records
from tests.engine_seam import parse_claude_line
from tests.test_notifier_loop import PAST_THE_CACHE, build_loop
from tests.test_opencode import errored_page
from tests.webfakes import (
    IDLE_SESSION,
    READ,
    SNAPSHOT,
    WORKING_SESSION,
    Rig,
    agents_of,
    build_rig,
    human_says,
    read_frame,
    snapshot_frame,
    write_transcript,
)

FRAGMENT = {"HX-Request": "true"}
S = SessionId(IDLE_SESSION)
# The fixture, by index: 0 a prompt, 1 the answer, 2 the first refusal, 3-8 retries each woken by
# a task notice or a peer message, 9 a typed prompt, 10 its refusal, 11 /compact, 12 on limits.
FIRST_REFUSAL = 3
RETRIED = 9
TYPED = 10
REFUSED_AGAIN = 11
COMPACTED = 12


def _turns(records: list[dict[str, JsonValue]]) -> list[TranscriptTurn]:
    parsed = (parse_claude_line(json.dumps(r).encode()) for r in records)
    return [turn for turn in parsed if turn is not None]


def _upto(count: int) -> list[TranscriptTurn]:
    return _turns(api_error_records()[:count])


# The conversation


@pytest.mark.parametrize(
    ("count", "stalled"),
    [
        (2, False),
        (FIRST_REFUSAL, True),
        (FIRST_REFUSAL + 1, True),
        (RETRIED, True),
        (TYPED, False),
        (REFUSED_AGAIN, True),
        (COMPACTED, False),
        (COMPACTED + 1, False),
    ],
)
def test_a_conversation_stalls_on_its_last_refusal_and_retries_do_not_end_it(
    count: int, stalled: bool
) -> None:
    # A notice or a peer message only woke it to be refused again; a prompt or /compact moved on.
    assert context_stalled(_upto(count)) is stalled


def test_compact_run_after_a_full_context_ends_the_stall() -> None:
    refused = next(r for r in api_error_records() if r.get("error") == "invalid_request")
    slash = slash_command_records()
    start = next(i for i, r in enumerate(slash) if "/compact" in json.dumps(r))
    # The caveat, the command and what it printed, as Claude Code writes them after a /compact.
    compacted = _turns([refused, *slash[start - 1 : start + 2]])
    assert [t.tool for t in compacted] == ["Context", "Ran", "Output"]
    assert context_stalled(compacted[:1]) is True
    assert context_stalled(compacted[:2]) is False
    assert context_stalled(compacted) is False


def test_another_refusal_is_not_a_full_context() -> None:
    records = api_error_records()
    limit = next(i for i, t in enumerate(_turns(records)) if t.tool == "Limit")
    assert context_stalled(_turns(records)[: limit + 1]) is False


def test_an_opencode_overflow_stalls_by_the_same_rule(tmp_path: Path) -> None:
    turns = errored_page(tmp_path).turns
    full = next(i for i, t in enumerate(turns) if t.tool == "Context")
    assert context_stalled(turns[: full + 1]) is True
    assert context_stalled(turns) is False


# The status every surface shows


@pytest.mark.parametrize(
    ("herdr", "shown"),
    [
        (AgentStatus.IDLE, AgentStatus.BLOCKED),
        (AgentStatus.DONE, AgentStatus.BLOCKED),
        (AgentStatus.WORKING, AgentStatus.BLOCKED),
        (AgentStatus.BLOCKED, AgentStatus.BLOCKED),
        (AgentStatus.UNKNOWN, AgentStatus.UNKNOWN),
    ],
)
def test_a_stalled_agent_needs_you_whatever_herdr_calls_its_stop(
    herdr: AgentStatus, shown: AgentStatus
) -> None:
    assert read_status(herdr, S, Overlay(stalled=frozenset({S}))) is shown
    assert read_status(herdr, SessionId("another"), Overlay(stalled=frozenset({S}))) is herdr


@pytest.mark.parametrize(
    ("status", "held", "reads"),
    [
        (AgentStatus.IDLE, False, True),
        (AgentStatus.DONE, False, True),
        (AgentStatus.WORKING, False, False),
        # A retry is working for a moment; only the log says whether it moved on.
        (AgentStatus.WORKING, True, True),
        (AgentStatus.BLOCKED, True, False),
        (AgentStatus.UNKNOWN, True, False),
    ],
)
def test_only_a_stop_or_a_stalled_agent_retrying_is_read_for_a_stall(
    status: AgentStatus, held: bool, reads: bool
) -> None:
    assert reads_stall(status, held=held) is reads


# The card


def _frame(status: str, revision: int, session: str = S) -> dict[str, JsonValue]:
    frame = snapshot_frame()
    for raw in agents_of(frame):
        if session in str(raw.get("agent_session")):
            raw["agent_status"] = status
            raw["revision"] = revision
    return frame


def _rig(tmp_path: Path, records: list[dict[str, JsonValue]]) -> Rig:
    rig = build_rig(tmp_path, {SNAPSHOT: _frame("idle", 7), READ: read_frame("> ")})
    write_transcript(tmp_path / "transcripts", S, records)
    return rig


def _card(feed: str) -> str:
    cards = (m.group(0) for m in re.finditer(r'<li class="card card--.*?</li>', feed, re.DOTALL))
    found = [card for card in cards if f"/agent/{S}" in card]
    assert len(found) == 1, feed
    return found[0]


def test_the_card_says_it_needs_you_and_what_unblocks_it(tmp_path: Path) -> None:
    rig = _rig(tmp_path, api_error_records()[:RETRIED])
    feed = rig.http.get("/feed", headers=FRAGMENT).text
    card = _card(feed)
    assert "card--blocked" in card
    assert CONTEXT_FULL in card
    needs_you = feed.split('class="group group--blocked"', 1)[1].split("</section>", 1)[0]
    assert f"/agent/{S}" in needs_you


def test_every_surface_agrees_with_the_card(tmp_path: Path) -> None:
    rig = _rig(tmp_path, api_error_records()[:FIRST_REFUSAL])
    page = rig.http.get(f"/agent/{S}?tab=chat").text
    answer = rig.http.get(f"/agent/{S}/answer").text
    assert 'id="agent-status">blocked<' in page
    assert 'data-hp-status="blocked"' in answer


def test_compact_sent_clears_it_at_once(tmp_path: Path) -> None:
    records = api_error_records()[:FIRST_REFUSAL]
    rig = _rig(tmp_path, records)
    assert "card--blocked" in _card(rig.http.get("/feed", headers=FRAGMENT).text)
    # Measured: the typed /compact is written the moment it is sent, long before it finishes.
    write_transcript(tmp_path / "transcripts", S, [*records, human_says("/compact", "c1")])
    rig.wire.replies[SNAPSHOT] = _frame("working", 8)
    rig.clock.sleep(60.0)
    card = _card(rig.http.get("/feed", headers=FRAGMENT).text)
    assert "card--working" in card
    assert CONTEXT_FULL not in card


def test_a_finish_that_is_not_a_refusal_stays_a_finish(tmp_path: Path) -> None:
    rig = _rig(tmp_path, api_error_records()[:2])
    card = _card(rig.http.get("/feed", headers=FRAGMENT).text)
    assert "card--idle" in card
    assert CONTEXT_FULL not in card


# The push


def _stall(tmp_path: Path, records: list[dict[str, JsonValue]]) -> None:
    write_transcript(tmp_path / "transcripts", WORKING_SESSION, records)


def test_a_stall_pushes_once_through_its_retries_and_not_again_on_compact(tmp_path: Path) -> None:
    loop = build_loop(tmp_path)
    loop.subscribe()
    records = api_error_records()
    _stall(tmp_path, records[:2])
    _ = loop.notifier.tick()
    # The turn is refused: herdr calls it a finish, the log says the context is full.
    _stall(tmp_path, records[:FIRST_REFUSAL])
    loop.set_status("idle")
    _ = loop.notifier.tick()
    # Each notice wakes it to be refused again: a moment of working, then the same stop.
    for end in range(FIRST_REFUSAL + 1, RETRIED + 1):
        _stall(tmp_path, records[:end])
        loop.set_status("working" if end % 2 == 0 else "idle")
        _ = loop.notifier.tick()
    _stall(tmp_path, [*records[:RETRIED], human_says("/compact", "c1")])
    loop.set_status("working")
    _ = loop.notifier.tick()
    sent = [payload for _, payload in loop.sender.sent]
    # The compact retracts the alert on the box only: a clear push would show nothing.
    assert [p["event"] for p in sent] == ["blocked"]
    assert str(sent[0]["title"]).startswith("needs you")
    assert CONTEXT_FULL in str(sent[0]["body"])


def test_a_refusal_after_the_person_answered_is_a_new_stop(tmp_path: Path) -> None:
    loop = build_loop(tmp_path)
    loop.subscribe()
    records = api_error_records()
    _stall(tmp_path, records[:2])
    _ = loop.notifier.tick()
    for end, status in ((FIRST_REFUSAL, "idle"), (TYPED, "working"), (REFUSED_AGAIN, "idle")):
        _stall(tmp_path, records[:end])
        loop.set_status(status)
        _ = loop.notifier.tick()
    assert [p["event"] for _, p in loop.sender.sent] == ["blocked", "blocked"]


def test_an_agent_already_stalled_when_watching_began_is_not_news(tmp_path: Path) -> None:
    loop = build_loop(tmp_path)
    loop.subscribe()
    _stall(tmp_path, api_error_records()[:FIRST_REFUSAL])
    loop.set_status("idle")
    _ = loop.notifier.tick()
    loop.clock.now += PAST_THE_CACHE
    _ = loop.notifier.tick()
    assert loop.sender.sent == []


def test_a_working_agent_whose_log_lags_is_not_called_stalled(tmp_path: Path) -> None:
    # Read only at a stop: a working agent's newest turn may not be written yet.
    loop = build_loop(tmp_path)
    loop.subscribe()
    _ = loop.notifier.tick()
    _stall(tmp_path, api_error_records()[:FIRST_REFUSAL])
    loop.set_status("working")
    _ = loop.notifier.tick()
    assert loop.sender.sent == []


def test_a_stall_alone_redraws_the_feed_and_the_page() -> None:
    # A stop can settle in herdr a moment before its refusal is written; the stall still redraws.
    snapshot = Snapshot.from_raw(obj(_frame("idle", 7), "result", "snapshot"))
    stalled = Overlay(stalled=frozenset({S}))
    stale = changed(herd_marks(snapshot), herd_marks(snapshot, overlay=stalled))
    assert {"feed", f"agent:{S}"} <= stale
