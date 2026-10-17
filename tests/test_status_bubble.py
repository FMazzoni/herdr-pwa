"""The Chat tab's status bubble: what the agent is doing now, for how long, and when it ended."""

import asyncio
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from fastapi import FastAPI
from pydantic import BaseModel, JsonValue, TypeAdapter

from herdr_pwa.clock import elapsed
from herdr_pwa.decisions import QUIET_AFTER, gone_quiet, needs_you
from herdr_pwa.engine import (
    AgentStatus,
    HistoryState,
    SessionUsage,
    Speaker,
    TranscriptPage,
    TranscriptTurn,
)
from herdr_pwa.web.chat import (
    WORKING_MOTION,
    WORKING_PALETTE,
    WORKING_VERBS,
    ChatView,
    Turning,
    chat_view,
    turn_verb,
)
from herdr_pwa.web.events import Hub
from herdr_pwa.web.stats import TurnTimes, turn_times
from tests.catalogue import PERMISSION, TURN_WORKED_SECONDS, capture, turn_chat
from tests.waiting import until_async
from tests.webfakes import (
    READ,
    SNAPSHOT,
    WORKING_PANE,
    WORKING_SESSION,
    agents_of,
    build_rig,
    read_frame,
    snapshot_frame,
    write_transcript,
)

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "herdr_pwa" / "web" / "static"
FRAGMENT = {"HX-Request": "true"}
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC).timestamp()


class _Spans(BaseModel):
    spans: list[tuple[float, str]]


_SPANS = TypeAdapter(_Spans)


def test_a_span_is_worded_to_the_second_by_the_table_app_js_also_reads() -> None:
    table = _SPANS.validate_json((ROOT / "tests" / "fixtures" / "elapsed.json").read_text())
    for seconds, said in table.spans:
        assert elapsed(seconds) == said, seconds


def test_a_turn_has_gone_quiet_only_past_the_threshold() -> None:
    assert QUIET_AFTER == 90
    assert not gone_quiet(None)
    assert not gone_quiet(QUIET_AFTER - 0.5)
    assert gone_quiet(QUIET_AFTER)


def test_only_a_blocked_agent_needs_you() -> None:
    assert [s for s in AgentStatus if needs_you(s)] == [AgentStatus.BLOCKED]


def _stamp(seconds_ago: float) -> str:
    return datetime.fromtimestamp(NOW - seconds_ago, UTC).isoformat()


def test_the_turns_times_come_from_the_tally_the_strip_reads() -> None:
    usage = SessionUsage(turn_started=_stamp(252), last_at=_stamp(30))
    assert turn_times(usage, NOW) == TurnTimes(ran=252, quiet=30, took=222)
    assert turn_times(SessionUsage(), NOW) == TurnTimes()
    # A stamp from a clock ahead of this one is no span at all.
    assert turn_times(SessionUsage(turn_started=_stamp(-5)), NOW).ran is None


# ---- The view, from a page and what the router knows about the turn ----


def _turn(speaker: Speaker, text: str, **fields: object) -> TranscriptTurn:
    n = len(text) + len(fields)
    return TranscriptTurn.model_validate(
        {"uuid": f"{speaker.value}{n}{text[:4]}", "speaker": speaker, "text": text, **fields}
    )


def _page(*turns: TranscriptTurn) -> TranscriptPage:
    return TranscriptPage(turns=list(turns), state=HistoryState.ok)


ASKED = _turn(Speaker.human, "Run the tests.")
SAID = _turn(Speaker.agent, "Reading first.")


def _tool(name: str, text: str, result: str = "") -> TranscriptTurn:
    return _turn(Speaker.tool, text, tool=name, call=f"c{name}{text}", result=result)


def _working(*turns: TranscriptTurn, quiet: float = 5, ran: float = 102) -> ChatView:
    turning = Turning(
        status=AgentStatus.WORKING, stretch="s:7", times=TurnTimes(ran=ran, quiet=quiet)
    )
    return chat_view(_page(*turns), turning=turning)


def test_while_a_tool_runs_the_bubble_names_it() -> None:
    view = _working(ASKED, SAID, _tool("Bash", "uv run pytest -q tests/test_chat.py"))
    assert view.bubble is not None
    assert view.bubble.state == "working"
    assert view.bubble.text == "Running pytest -q tests/test_chat.py"
    assert view.bubble.time == "1m 42s"
    assert view.bubble.said == "Working"
    assert view.receipt is None


def test_a_tool_is_worded_as_what_it_does_to_what() -> None:
    cases = {
        ("Edit", "/repo/src/herdr_pwa/web/chat.py"): "Editing chat.py",
        ("Read", "/repo/src/herdr_pwa/engine/transcript.py"): "Reading transcript.py",
        ("Write", "notes/plan.md"): "Writing plan.md",
        ("Grep", "def chat_view"): "Searching def chat_view",
        ("Bash", "cd /repo && nice -n 19 uv run pytest -q"): "Running pytest -q",
        ("Bash", "FOO=1 git push origin main\necho done"): "Running git push origin main",
        ("bash", "ls"): "Running ls",
        ("mcp__tracker__show", "42"): "Using mcp__tracker__show",
    }
    for (name, text), said in cases.items():
        view = _working(ASKED, _tool(name, text))
        assert view.bubble is not None
        assert view.bubble.text == said, (name, text)


def test_between_tools_it_wears_the_turns_one_verb() -> None:
    view = _working(ASKED, _tool("Bash", "pytest", result="12 passed"))
    assert view.bubble is not None
    assert view.bubble.text == f"{turn_verb('s:7')}…"
    assert turn_verb("s:7") in WORKING_VERBS
    # crc32 of the stretch's name: the same on every poll and after a restart.
    assert turn_verb("s:1") == "Gathering"
    assert {turn_verb(f"s:{n}") for n in range(200)} == set(WORKING_VERBS)


def test_a_failed_call_is_not_still_running() -> None:
    failed = _turn(Speaker.tool, "pytest", tool="Bash", call="c1", failed=True)
    view = _working(ASKED, failed)
    assert view.bubble is not None
    assert view.bubble.text.endswith("…")


def test_past_the_threshold_with_nothing_new_it_says_how_long_it_has_been_quiet() -> None:
    done = _tool("Bash", "pytest", result="ok")
    view = _working(ASKED, done, quiet=QUIET_AFTER + 95)
    assert view.bubble is not None
    assert view.bubble.state == "quiet"
    assert view.bubble.text == "No activity for 3m"
    assert view.bubble.said == "No activity for 3 minutes"
    fresh = _working(ASKED, done, quiet=QUIET_AFTER - 1)
    assert fresh.bubble is not None
    assert fresh.bubble.state == "working"
    assert fresh.bubble.quiet == QUIET_AFTER - 1
    assert fresh.bubble.quiet_after == QUIET_AFTER


def test_with_no_tally_there_is_no_timer_and_no_quiet() -> None:
    turning = Turning(status=AgentStatus.WORKING, stretch="s:7")
    view = chat_view(_page(ASKED), turning=turning)
    assert view.bubble is not None
    assert view.bubble.state == "working"
    assert view.bubble.time == ""
    assert view.bubble.ran is None


def test_a_blocked_agent_needs_you_in_the_cards_words() -> None:
    asked = Turning(status=AgentStatus.BLOCKED, stretch="s:8", asked="Do you want\n to proceed?")
    view = chat_view(_page(ASKED), turning=asked)
    assert view.bubble is not None
    assert view.bubble.state == "blocked"
    assert view.bubble.text == "Needs you · Do you want to proceed?"
    assert view.bubble.said == "Needs you: Do you want to proceed?"
    assert view.bubble.time == ""
    unread = chat_view(_page(ASKED), turning=Turning(status=AgentStatus.BLOCKED))
    assert unread.bubble is not None
    assert unread.bubble.text == "Needs you"


def _ended(*turns: TranscriptTurn, status: AgentStatus = AgentStatus.DONE) -> ChatView:
    times = TurnTimes(ran=900, quiet=648, took=TURN_WORKED_SECONDS)
    return chat_view(_page(*turns), turning=Turning(status=status, times=times))


def test_a_finished_turn_folds_into_how_long_it_worked_and_its_tool_calls() -> None:
    tools = [_tool("Bash", f"step {n}", result="ok") for n in range(3)]
    view = _ended(_turn(Speaker.agent, "Earlier."), ASKED, *tools, SAID)
    assert view.bubble is None
    assert view.receipt is not None
    assert view.receipt.text == "Worked 4m 12s · 3 tool calls"
    assert view.receipt.said == "Worked 4 minutes, 3 tool calls"
    one = _ended(ASKED, _tool("Bash", "x", result="ok"), SAID, status=AgentStatus.IDLE)
    assert one.receipt is not None
    assert one.receipt.text == "Worked 4m 12s · 1 tool call"


def test_the_count_is_left_out_when_the_turns_prompt_is_off_the_page() -> None:
    view = _ended(_tool("Bash", "x", result="ok"), SAID)
    assert view.receipt is not None
    assert view.receipt.text == "Worked 4m 12s"


def test_no_receipt_before_the_agent_has_answered_or_without_a_tally() -> None:
    assert _ended(SAID, ASKED).receipt is None
    assert _ended().receipt is None
    untimed = chat_view(_page(ASKED, SAID), turning=Turning(status=AgentStatus.DONE))
    assert untimed.receipt is None


def test_the_bubble_wears_the_default_presets() -> None:
    view = _working(ASKED)
    assert view.bubble is not None
    assert (view.bubble.palette, view.bubble.motion) == (WORKING_PALETTE, WORKING_MOTION)
    css = (STATIC / "app.css").read_text()
    assert f'[data-working-palette="{WORKING_PALETTE}"]' in css
    for shipped in ("softer-ember", "ember", "current", "green"):
        assert f'[data-working-palette="{shipped}"]' in css, shipped
    js = (STATIC / "hp.js").read_text()
    table = js[js.index("var MOTIONS = {") :]
    assert re.search(rf'^\s*"?{WORKING_MOTION}"?: \{{', table, re.MULTILINE)


# ---- Through the routes ----


def _status(status: str) -> dict[str, JsonValue]:
    frame = snapshot_frame()
    for agent in agents_of(frame):
        if agent.get("pane_id") == WORKING_PANE:
            agent["agent_status"] = status
    return frame


def _chat(tmp_path: Path, stage: str, status: str, screen: str = "") -> str:
    write_transcript(tmp_path / "transcripts", WORKING_SESSION, turn_chat(stage))
    rig = build_rig(tmp_path, {SNAPSHOT: _status(status), READ: read_frame(screen)})
    return rig.http.get(f"/agent/{WORKING_SESSION}/chat", headers=FRAGMENT).text


def _bubble(body: str) -> str:
    found = re.search(r'<div class="bubble[^"]*"[^>]*>.*?</div>', body, re.DOTALL)
    assert found is not None, body[-2000:]
    return found.group(0)


def test_the_route_draws_the_running_tool_and_its_timer(tmp_path: Path) -> None:
    bubble = _bubble(_chat(tmp_path, "tool", "working"))
    assert 'data-hp-bubble="working"' in bubble
    assert ">Running pytest -q tests/test_chat_fold.py<" in bubble
    assert re.search(r'class="bubble__time" aria-hidden="true">2m 3\ds<', bubble)
    assert re.search(r'data-hp-ran="15\d"', bubble)
    assert f'data-working-palette="{WORKING_PALETTE}"' in bubble
    assert '<span class="bubble__text" aria-hidden="true">' in bubble
    assert '<span class="sr-only">Working: Running pytest' in bubble
    assert 'data-hp-said="Working"' in bubble


def test_the_route_draws_how_long_a_thinking_turn_has_been_still(tmp_path: Path) -> None:
    bubble = _bubble(_chat(tmp_path, "thinking", "working"))
    assert re.search(r'data-hp-quiet="(19|2\d)"', bubble)
    assert f'data-hp-quiet-after="{QUIET_AFTER}"' in bubble


def test_the_route_draws_a_quiet_turn_still(tmp_path: Path) -> None:
    bubble = _bubble(_chat(tmp_path, "quiet", "working"))
    assert 'data-hp-bubble="quiet"' in bubble
    assert ">No activity for 3m<" in bubble


def test_the_route_draws_the_question_the_card_reads(tmp_path: Path) -> None:
    bubble = _bubble(_chat(tmp_path, "asking", "blocked", capture(PERMISSION)))
    assert 'data-hp-bubble="blocked"' in bubble
    assert ">Needs you · Do you want to proceed?<" in bubble


def test_the_route_draws_the_receipt_once_the_turn_is_done(tmp_path: Path) -> None:
    body = _chat(tmp_path, "done", "done")
    assert '<div class="bubble' not in body
    found = re.search(r'<p class="chat__receipt" data-hp-bubble="done"[^>]*>(.*?)</p>', body)
    assert found is not None, body[-1500:]
    assert "Worked 4m 1" in found.group(1)
    assert "3 tool calls" in found.group(1)


def test_the_chat_tab_alone_carries_the_switch_that_stops_the_animation(tmp_path: Path) -> None:
    write_transcript(tmp_path / "transcripts", WORKING_SESSION, turn_chat("tool"))
    rig = build_rig(tmp_path, {SNAPSHOT: _status("working"), READ: read_frame("")})
    chat = rig.http.get(f"/agent/{WORKING_SESSION}?tab=chat").text
    switch = 'data-hp-still aria-pressed="false">Stop the animation</button>'
    # Hidden until bubble.js shows it with the choice this device remembers.
    assert re.search(rf'<p class="agent__columns" hidden><button [^>]*{switch}</p>', chat)
    pane = rig.http.get(f"/agent/{WORKING_SESSION}?tab=pane").text
    assert "data-hp-still" not in pane
    assert "data-hp-columns" in pane


def test_the_old_working_line_is_gone(tmp_path: Path) -> None:
    body = _chat(tmp_path, "thinking", "working")
    assert "chat__working" not in body
    assert "data-hp-verbs" not in body


def test_a_status_change_with_no_new_line_is_stale_for_the_chat(tmp_path: Path) -> None:
    """The bubble says blocked and done, which the log alone does not, so the chat hears them."""
    write_transcript(tmp_path / "transcripts", WORKING_SESSION, turn_chat("tool"))
    rig = build_rig(tmp_path, {SNAPSHOT: _status("working")}, events_interval=0.05)
    app = rig.http.app
    assert isinstance(app, FastAPI)
    hub = cast(object, app.state.events)
    assert isinstance(hub, Hub)
    topic = f"chat:{WORKING_SESSION}"

    async def scenario() -> set[str]:
        listener = hub.join(frozenset({topic}))
        hub.stream(listener)
        await until_async(lambda: hub.looks >= 1, 5.0)
        rig.wire.replies[SNAPSHOT] = _status("blocked")
        rig.clock.sleep(60.0)
        await until_async(lambda: bool(listener.pending), 5.0)
        heard = set(listener.pending)
        await hub.close()
        return heard

    assert asyncio.run(scenario()) == {topic}


def test_a_tool_still_open_is_working_however_long_it_runs() -> None:
    """A ten-minute test run writes nothing until it ends; that is not the agent being stuck."""
    view = _working(ASKED, _tool("Bash", "just check"), quiet=300)
    assert view.bubble is not None
    assert view.bubble.state == "working"
    assert view.bubble.text == "Running just check"
    # No quiet clock, so the page never flips it to quiet either.
    assert view.bubble.quiet is None


def test_the_route_keeps_an_open_tool_working_past_the_threshold(tmp_path: Path) -> None:
    records = turn_chat("tool")
    for record in records:
        stamp = datetime.fromisoformat(str(record["timestamp"]).replace("Z", "+00:00"))
        record["timestamp"] = (stamp - timedelta(minutes=5)).isoformat()
    write_transcript(tmp_path / "transcripts", WORKING_SESSION, records)
    rig = build_rig(tmp_path, {SNAPSHOT: _status("working")})
    bubble = _bubble(rig.http.get(f"/agent/{WORKING_SESSION}/chat", headers=FRAGMENT).text)
    assert 'data-hp-bubble="working"' in bubble
    assert "data-hp-quiet=" not in bubble.replace("data-hp-quiet-after", "")


def _verb_at_seq(tmp_path: Path, seq: int) -> str:
    frame = _status("working")
    for agent in agents_of(frame):
        agent["state_change_seq"] = seq
    rig = build_rig(tmp_path, {SNAPSHOT: frame})
    body = rig.http.get(f"/agent/{WORKING_SESSION}/chat", headers=FRAGMENT).text
    found = re.search(r'class="bubble__text" aria-hidden="true">(\w+)…<', body)
    assert found is not None, body[-1500:]
    return found.group(1)


def test_the_verb_is_the_turns_and_survives_a_question_answered_mid_turn(tmp_path: Path) -> None:
    """Answering a question bumps herdr's sequence; the turn, and so its verb, is the same."""
    records = turn_chat("thinking")
    for name in ("a", *(f"s{seq}" for seq in range(8, 20))):
        write_transcript(tmp_path / name / "transcripts", WORKING_SESSION, records)
    first = _verb_at_seq(tmp_path / "a", 7)
    assert {_verb_at_seq(tmp_path / f"s{seq}", seq) for seq in range(8, 20)} == {first}
