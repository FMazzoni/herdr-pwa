"""A finish the phone has shown is idle on every surface, and stays so across a restart."""

import re
from pathlib import Path

from herdr_pwa.decisions import read_status
from herdr_pwa.engine import AgentStatus, Snapshot
from herdr_pwa.jsontypes import JsonObject
from herdr_pwa.notify import Hooks
from herdr_pwa.notify_state import SeenLedger
from herdr_pwa.push import EVENT_CLEAR, EVENT_DONE
from herdr_pwa.web.events import changed, herd_marks
from tests.fakes import Clock
from tests.test_notify import agent, events, planner
from tests.webfakes import (
    IDENTITY,
    IDLE_SESSION,
    READ,
    SNAPSHOT,
    Rig,
    agents_of,
    build_rig,
    read_frame,
    snapshot_frame,
)

SAME_ORIGIN = {**IDENTITY, "Origin": "http://testserver"}
FRAGMENT = {"HX-Request": "true"}
S = IDLE_SESSION


def _frame(status: str, seq: int) -> JsonObject:
    frame = snapshot_frame()
    for raw in agents_of(frame):
        if S in str(raw.get("agent_session")):
            raw["agent_status"] = status
            raw["state_change_seq"] = seq
    return frame


def _herd(rig: Rig, status: str, seq: int) -> None:
    """herdr now reports the agent so; the cached snapshot is let go."""
    rig.wire.replies[SNAPSHOT] = _frame(status, seq)
    rig.clock.sleep(60.0)


def _rig(tmp_path: Path) -> Rig:
    return build_rig(tmp_path, {SNAPSHOT: snapshot_frame(), READ: read_frame("$ ")})


def _card(rig: Rig) -> str:
    """The status class on the agent's feed card."""
    feed = rig.http.get("/feed", headers=FRAGMENT).text
    found = re.search(r'card card--(\w+)"[^>]*>\s*<a[^>]*href="/agent/' + S, feed)
    assert found is not None, feed
    return found.group(1)


def _read(rig: Rig) -> None:
    assert rig.http.post(f"/push/seen/{S}", headers=SAME_ORIGIN).status_code == 204


def _surfaces(rig: Rig) -> dict[str, str]:
    """What each drawn surface says the agent is."""
    page = rig.http.get(f"/agent/{S}?tab=chat").text
    answer = rig.http.get(f"/agent/{S}/answer").text
    feed = rig.http.get("/feed", headers=FRAGMENT).text

    def one(pattern: str, text: str) -> str:
        found = re.search(pattern, text)
        assert found is not None, pattern
        return found.group(1)

    return {
        "card": _card(rig),
        "meta": one(r'id="agent-status">(\w+)<', page),
        "dot": one(r'dot dot--(\w+)" id="agent-dot"', page),
        # The answer region is what the stream redraws; the page takes its status from it.
        "answer": one(r'<section id="answer"[^>]*data-hp-status="(\w+)"', answer),
        "unseen": "unseen" if "unseen" in feed else "none",
    }


IDLE_EVERYWHERE = {
    "card": "idle",
    "meta": "idle",
    "dot": "idle",
    "answer": "idle",
    "unseen": "none",
}


def test_the_rule_is_one_function_and_only_a_read_finish_moves() -> None:
    read = {S: False}
    assert read_status(AgentStatus.DONE, S, read) is AgentStatus.IDLE
    assert read_status(AgentStatus.DONE, S, {S: True}) is AgentStatus.DONE
    # Never read, or nothing keeping track: still news.
    assert read_status(AgentStatus.DONE, S, {}) is AgentStatus.DONE
    assert read_status(AgentStatus.DONE, S, None) is AgentStatus.DONE
    for other in (AgentStatus.BLOCKED, AgentStatus.WORKING, AgentStatus.IDLE):
        assert read_status(other, S, read) is other


def test_finish_then_read_is_idle_on_every_surface(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    _herd(rig, "working", 20)
    assert _card(rig) == "working"
    _herd(rig, "done", 21)
    assert _surfaces(rig)["card"] == "done"
    _read(rig)
    assert _surfaces(rig) == IDLE_EVERYWHERE


def test_read_then_finish_is_news_until_it_is_read_again(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    _herd(rig, "working", 20)
    assert _card(rig) == "working"
    # The page was opened while it worked; the finish came after that look.
    _read(rig)
    _herd(rig, "done", 21)
    assert _surfaces(rig)["meta"] == "done"
    assert _card(rig) == "done"
    # The open page saw it finish and says so once; that look covers the finish.
    _read(rig)
    assert _surfaces(rig) == IDLE_EVERYWHERE


def test_a_read_before_the_ledger_ever_saw_the_agent_still_counts(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    _herd(rig, "done", 21)
    _read(rig)
    assert _card(rig) == "idle"


def test_a_restart_keeps_what_was_read(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    with rig.http:
        _herd(rig, "working", 20)
        _ = _card(rig)
        _herd(rig, "done", 21)
        assert _card(rig) == "done"
        _read(rig)
    again = _rig(tmp_path)
    again.wire.replies[SNAPSHOT] = _frame("done", 21)
    assert _surfaces(again) == IDLE_EVERYWHERE


def _snapshot(status: str, seq: int) -> Snapshot:
    result = _frame(status, seq)["result"]
    assert isinstance(result, dict)
    raw = result["snapshot"]
    assert isinstance(raw, dict)
    return Snapshot.from_raw(raw)


def test_reading_a_finish_is_stale_for_the_feed_and_the_agent_page() -> None:
    herd = _snapshot("done", 21)
    before = herd_marks(herd, unread={S: True})
    after = herd_marks(herd, unread={S: False})
    assert {"feed", f"agent:{S}"} <= changed(before, after)


def test_a_finish_read_on_the_phone_takes_its_notification_down(tmp_path: Path) -> None:
    clock = Clock()
    seen = SeenLedger(tmp_path, clock=clock)
    plan = planner(tmp_path, clock, Hooks(seen=seen))
    a = agent(status="working", seq=1)
    _ = plan.step([a])
    clock.advance(10)
    done = agent(status="done", seq=2)
    fired = plan.step([done])
    assert events(list(fired.changes)) == [(a.session, EVENT_DONE)]
    clock.advance(5)
    seen.mark_seen(a.session)
    taken = plan.step([done])
    assert events(list(taken.changes)) == [(a.session, EVENT_CLEAR)]
    assert taken.slot is not None
    assert taken.slot.summary is None
    # Read stays read: nothing more is said about it.
    assert plan.step([done]).changes == ()
