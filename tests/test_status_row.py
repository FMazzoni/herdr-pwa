"""The status row above the composer: a state dot, what the agent is doing, the counts that are
never cut, and a sheet holding the agents it started, Stop and the key pad."""

import re
from pathlib import Path

from pydantic import JsonValue

from herdr_pwa.engine import (
    INTERRUPT_KEY,
    AgentStatus,
    HistoryState,
    Speaker,
    TranscriptPage,
    TranscriptTurn,
)
from herdr_pwa.web.chat import Turning, chat_view, status_row
from herdr_pwa.web.stats import SpawnedTime, TurnTimes
from tests.catalogue import CROWDED_QUEUED, PERMISSION, capture, crowded_chat, turn_chat
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

FRAGMENT = {"HX-Request": "true"}
PAGE = f"/agent/{WORKING_SESSION}"


def _status(status: str) -> dict[str, JsonValue]:
    frame = snapshot_frame()
    for agent in agents_of(frame):
        if agent.get("pane_id") == WORKING_PANE:
            agent["agent_status"] = status
    return frame


def _page(
    tmp_path: Path, records: list[dict[str, JsonValue]], status: str, screen: str = "> "
) -> str:
    write_transcript(tmp_path / "transcripts", WORKING_SESSION, records)
    rig = build_rig(tmp_path, {SNAPSHOT: _status(status), READ: read_frame(screen)})
    return rig.http.get(f"{PAGE}?tab=chat").text


def _part(body: str, pattern: str) -> str:
    found = re.search(pattern, body, re.DOTALL)
    assert found is not None, body[-3000:]
    return found.group(0)


def _tray(body: str) -> str:
    # The tray is the last thing the page draws before the composer's backdrop.
    return _part(body, r'<div id="agent-tray".*?(?=<div class="pill-rail")')


def _row(body: str) -> str:
    return _part(_tray(body), r'<(summary|div) class="tray__row[^"]*"[^>]*>.*?</\1>')


def _sheet(body: str) -> str:
    return _part(_tray(body), r'<div class="tray__body"[^>]*>.*</details>')


def _head(body: str) -> str:
    return _part(_sheet(body), r'<div class="tray__head".*?(?=<section)')


def _answer(body: str) -> str:
    return _part(body, r'<section id="answer".*?</section>')


def _view(body: str) -> str:
    return _part(body, r'<section id="agent-view".*?</section>')


def _keys(markup: str) -> list[str]:
    return re.findall(r'<input type="hidden" name="key" value="([^"]+)">', markup)


def _drawn(markup: str) -> str:
    """The row's words as drawn, tags and the sentence for VoiceOver dropped."""
    said = r'<span class="sr-only"><span class="tray__said">[^<]*</span>[^<]*</span>'
    seen = re.sub(said, "", markup)
    return " ".join(re.sub(r"<[^>]+>", " ", seen).replace("&middot;", "·").split())


def test_a_working_agent_is_one_row_above_the_composer(tmp_path: Path) -> None:
    body = _page(tmp_path, crowded_chat(), "working")
    # Directly above the composer: the tray follows it in the page, fixed over its top edge.
    assert body.index('id="composer"') < body.index('id="agent-tray"')
    assert 'class="dot dot--working"' in _row(body)


def test_the_counts_are_their_own_tokens_outside_the_words_the_script_redraws(
    tmp_path: Path,
) -> None:
    row = _row(_page(tmp_path, crowded_chat(), "working"))
    words = _part(row, r'<span class="bubble__text"[^>]*>.*?</span>')
    assert ">Waiting<" in words
    assert "agent" not in words
    counts = _part(
        row, r'<span class="tray__counts">.*?</span></span>(?=<span class="tray__time|$)'
    )
    assert '<b class="count__n">2</b> agents</span>' in counts
    assert '<b class="count__n">1</b> queued</span>' in counts
    # Ranked: the dot, the words that take the ellipsis, the counts that never do, then the time.
    order = [row.index(s) for s in ("dot--", "tray__words", "tray__counts", "tray__time")]
    assert order == sorted(order)
    assert _drawn(row).startswith("Waiting · 2 agents · 1 queued · ")


def test_an_open_tool_and_its_running_agents_are_counted_apart() -> None:
    spawned = (
        SpawnedTime("Lint", running=True, seconds=30),
        SpawnedTime("Test", running=True, seconds=20),
    )
    turning = Turning(
        status=AgentStatus.WORKING, stretch="s:1", times=TurnTimes(ran=60, quiet=1), spawned=spawned
    )
    asked = TranscriptTurn(uuid="h", speaker=Speaker.human, text="Go.")
    call = TranscriptTurn(uuid="t", speaker=Speaker.tool, text="just check", tool="Bash", call="c1")
    view = chat_view(TranscriptPage(turns=[asked, call], state=HistoryState.ok), turning=turning)
    row = status_row(view, "working")
    assert (row.state, row.running, row.queued) == ("working", 2, 0)
    assert view.bubble is not None
    assert view.bubble.text == "Running just check"


def test_the_sheet_has_a_grabber_a_title_and_one_line_agent_rows(tmp_path: Path) -> None:
    sheet = _sheet(_page(tmp_path, crowded_chat(), "working"))
    assert '<button class="button tray__grab" type="button" aria-label="Close"' in sheet
    # The grabber and the header drag the sheet that swipe.js moves; a tap on the grabber closes it.
    assert '<div class="tray__body" data-hp-swipe>' in sheet
    assert re.search(r'class="button tray__grab"[^>]*data-hp-close data-hp-grab', sheet)
    assert re.search(r'<div class="tray__head"[^>]*data-hp-grab', sheet)
    assert '<h2 class="sheet__heading tray__title">Activity</h2>' in sheet
    found = re.finditer(r'<li class="spawned__row spawned__row--(\w+)">(.*?)</li>', sheet)
    rows = [(m.group(1), m.group(2)) for m in found]
    assert [state for state, _ in rows] == ["running", "running", "finished"]
    for state, row in rows:
        dot = "working" if state == "running" else "done"
        assert row.startswith(f'<span class="dot dot--{dot}"'), row
        assert re.search(r'<span class="spawned__time">\d+m \d+s</span></span>$', row), row
        # The dot says the state; only a failure is spelled out.
        assert "spawned__state" not in row


def test_queued_prompts_stay_in_the_chat_and_out_of_the_sheet(tmp_path: Path) -> None:
    body = _page(tmp_path, crowded_chat(), "working")
    assert CROWDED_QUEUED not in _tray(body)
    assert f'<li class="queued__text">{CROWDED_QUEUED}</li>' in _view(body)


def test_the_pad_is_up_down_enter_then_esc_set_apart(tmp_path: Path) -> None:
    pad = _part(
        _sheet(_page(tmp_path, turn_chat("done"), "idle")), r'<div class="keys tray__pad">.*'
    )
    assert _keys(pad) == ["up", "down", "enter", "esc"]
    # Idle, Esc is a plain key: nothing it stops.
    assert "button--danger" not in pad


def test_while_working_esc_asks_first_and_shares_stops_confirm(tmp_path: Path) -> None:
    sheet = _sheet(_page(tmp_path, turn_chat("tool"), "working"))
    pad = _part(sheet, r'<div class="keys tray__pad">.*')
    esc = _part(pad, r'<div class="tray__slot tray__slot--esc">.*?</div>')
    assert _keys(esc) == ["esc"]
    assert "button--danger tray__arm" in esc
    assert ">Esc…</button>" in esc
    # Without script the tap is held by the server for a second one; with it, it arms in place.
    assert 'name="confirm"' not in esc
    assert 'class="button tray__keep" type="button"' in esc
    # The way through takes the title's place on the far side, drawn but not shown until armed.
    armed = _part(sheet, r'<div class="tray__lead">.*?(?=<div class="tray__slot)')
    assert _keys(armed) == [INTERRUPT_KEY, "esc"]
    assert armed.count("data-hp-off>") == 2
    assert armed.count('<input type="hidden" name="confirm" value="1">') == 2
    assert armed.count("button--stop") == 2


def test_stop_is_in_the_sheets_header_and_never_in_the_row(tmp_path: Path) -> None:
    body = _page(tmp_path, crowded_chat(), "working")
    head = _head(body)
    stop = _part(head, r'<div class="tray__slot tray__slot--stop">.*?</div>')
    assert _keys(stop) == [INTERRUPT_KEY]
    assert ">Stop…</button>" in stop
    assert "button--danger tray__arm" in stop
    # The first tap only arms: it carries no confirm, so the server holds it if no script ran.
    assert 'name="confirm"' not in stop
    # The safe option is drawn in the same slot, so the second tap of a double tap lands on it.
    assert 'class="button tray__keep" type="button"' in stop
    assert ">Stop now (Ctrl+C)</button>" in head
    assert ">Stop now (Esc)</button>" in head
    armed = _part(head, r'<p class="tray__armed" hidden>.*?</p>')
    assert "Unsent work in this turn is lost. Disarms in 5s." in armed
    assert INTERRUPT_KEY not in _keys(_row(body))
    assert 'class="tray__stop"' not in _tray(body)
    # Its outcome and the no-script confirm land in the answer region, as every key's do.
    assert 'hx-target="#answer"' in stop
    assert 'hx-sync="#answer:replace"' in stop


def test_the_page_flow_no_longer_carries_the_rows_pieces(tmp_path: Path) -> None:
    body = _page(tmp_path, crowded_chat(), "working")
    view, answer = _view(body), _answer(body)
    for piece in ('class="bubble', 'class="spawned', "chat__receipt"):
        assert piece not in view, piece
    assert _keys(answer) == []
    assert "data-hp-still" not in body


def test_an_idle_agent_has_no_stop_and_its_row_says_how_long_it_worked_and_keys(
    tmp_path: Path,
) -> None:
    body = _page(tmp_path, turn_chat("done"), "idle")
    row = _row(body)
    assert "Worked 4m" in row
    assert '<span class="tray__tail" aria-hidden="true">Keys</span>' in row
    assert "tray__slot--stop" not in _tray(body)
    assert "tray__armed" not in _tray(body)
    assert "button--stop" not in _tray(body)


def test_a_quiet_agent_says_quiet_once_in_neutral_ink_and_keeps_stop_in_the_sheet(
    tmp_path: Path,
) -> None:
    body = _page(tmp_path, turn_chat("quiet"), "working")
    row = _row(body)
    assert "tray__row--quiet" in row
    assert 'class="dot dot--updating"' in row
    assert ">Quiet 3m<" in row
    # One duration: the turn's own clock is not drawn beside the quiet one.
    assert "tray__time" not in row
    assert "tray__slot--stop" in _head(body)
    assert "herdr still reports it working" in _sheet(body)


def test_a_quiet_agents_page_does_not_say_working(tmp_path: Path) -> None:
    body = _page(tmp_path, turn_chat("quiet"), "working")
    assert '<span class="agent__status" id="agent-status">quiet</span>' in body


def test_a_blocked_agent_keeps_its_question_and_the_row_only_says_needs_you(
    tmp_path: Path,
) -> None:
    body = _page(tmp_path, turn_chat("asking"), "blocked", capture(PERMISSION))
    answer, tray = _answer(body), _tray(body)
    assert "answer__question" in answer
    # The question's own No and Esc decline it; Ctrl+C is not drawn beside the options.
    assert INTERRUPT_KEY not in _keys(answer)
    assert "keys__stop" not in answer
    row = _row(body)
    assert row.startswith('<div class="tray__row tray__row--blocked"')
    assert _drawn(row) == "Needs you"
    # Not a way into a sheet: there is nothing in one while the keys are with the question.
    assert "<details" not in tray
    assert _keys(tray) == []


def test_a_stop_held_by_the_server_is_confirmed_in_the_answer_region(tmp_path: Path) -> None:
    write_transcript(tmp_path / "transcripts", WORKING_SESSION, turn_chat("tool"))
    rig = build_rig(tmp_path, {SNAPSHOT: _status("working")})
    for key, label in ((INTERRUPT_KEY, "Really interrupt"), ("esc", "Really send Esc")):
        posted = {**FRAGMENT, "Origin": "http://testserver"}
        held = rig.http.post(f"{PAGE}/keys", data={"key": key}, headers=posted).text
        armed = _part(held, r'<div class="keys__stop keys__stop--armed".*?</button>\s*</div>')
        assert "Keep going" in armed
        assert label in armed
        assert _keys(armed) == [key]
        assert '<input type="hidden" name="confirm" value="1">' in armed


def test_the_tray_route_redraws_the_tray_alone(tmp_path: Path) -> None:
    write_transcript(tmp_path / "transcripts", WORKING_SESSION, crowded_chat())
    rig = build_rig(tmp_path, {SNAPSHOT: _status("working")})
    fragment = rig.http.get(f"{PAGE}/tray", headers=FRAGMENT).text
    assert fragment.lstrip().startswith('<div id="agent-tray"')
    assert f'data-hp-topic="chat:{WORKING_SESSION}"' in fragment
    assert 'hx-trigger="hp:stale, hp:poke from:body"' in fragment
    assert ">Waiting<" in fragment


def test_the_pane_tab_has_the_row_too(tmp_path: Path) -> None:
    write_transcript(tmp_path / "transcripts", WORKING_SESSION, crowded_chat())
    rig = build_rig(tmp_path, {SNAPSHOT: _status("working")})
    pane = rig.http.get(f"{PAGE}?tab=pane").text
    assert ">Waiting<" in _row(pane)
    assert 'data-hp-tab="pane"' in pane


def test_the_switch_that_stops_the_animation_is_on_the_alerts_page(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    alerts = rig.http.get("/push").text
    switch = 'data-hp-still aria-pressed="false">Stop the animation</button>'
    # Hidden until bubble.js shows it with the choice this device remembers.
    assert re.search(rf"<button [^>]*{switch}", alerts)
