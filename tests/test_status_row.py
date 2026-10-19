"""The status row above the composer: what the agent is doing, the agents it started, the prompts
that wait, and a sheet holding them with the key pad; Interrupt at its edge while it works."""

import re
from html.parser import HTMLParser
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
    return _part(_tray(body), r'<summary class="tray__row[^"]*"[^>]*>.*?</summary>')


def _answer(body: str) -> str:
    return _part(body, r'<section id="answer".*?</section>')


def _view(body: str) -> str:
    return _part(body, r'<section id="agent-view".*?</section>')


def _keys(markup: str) -> list[str]:
    return re.findall(r'<input type="hidden" name="key" value="([^"]+)">', markup)


def test_a_working_agent_is_one_row_above_the_composer(tmp_path: Path) -> None:
    body = _page(tmp_path, crowded_chat(), "working")
    row = _row(body)
    assert ">Waiting on 2 agents<" in row
    # The words already count the agents, so the row does not count them twice.
    assert row.count("tray__count") == 1
    assert re.search(r'<span class="tray__count" aria-hidden="true">[^<]*1 queued</span>', row)
    # Directly above the composer: the tray follows it in the page, fixed over its top edge.
    assert body.index('id="composer"') < body.index('id="agent-tray"')


class _Heard(HTMLParser):
    """The text VoiceOver reads in a fragment, and how many separate sentences it is told."""

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.heard: list[str] = []
        self.sentences = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del tag
        found = dict(attrs)
        hidden = found.get("aria-hidden") == "true" or "hidden" in found
        self.stack.append("hidden" if hidden else "sr" if found.get("class") == "sr-only" else "")
        self.sentences += found.get("class") == "sr-only"

    def handle_endtag(self, tag: str) -> None:
        del tag
        self.stack.pop()

    def handle_data(self, data: str) -> None:
        if data.strip() and "hidden" not in self.stack:
            self.heard.append(data)


def test_the_rows_sentence_is_read_once(tmp_path: Path) -> None:
    heard = _Heard()
    heard.feed(_row(_page(tmp_path, crowded_chat(), "working")))
    assert heard.sentences == 1
    # Everything drawn beside the sentence is hidden from VoiceOver, so nothing is read twice.
    assert "".join(heard.heard) == ("Working: Waiting on 2 agents. 1 message queued.")


def test_the_sheet_holds_the_agents_the_queue_and_the_pad(tmp_path: Path) -> None:
    tray = _tray(_page(tmp_path, crowded_chat(), "working"))
    body = _part(tray, r'<div class="tray__body">.*')
    assert body.count('class="spawned__row spawned__row--running"') == 2
    assert body.count('class="spawned__row spawned__row--finished"') == 1
    assert f'<li class="queued__text">{CROWDED_QUEUED}</li>' in body
    assert _keys(_part(body, r'<div class="keys keys--pad.*?</div>')) == [
        "esc",
        "up",
        "down",
        "enter",
    ]


def test_interrupt_sits_at_the_rows_edge_while_the_agent_works(tmp_path: Path) -> None:
    tray = _tray(_page(tmp_path, crowded_chat(), "working"))
    stop = _part(tray, r'<div class="tray__stop">.*?</form>')
    assert _keys(stop) == [INTERRUPT_KEY]
    assert "button--danger" in stop
    # Its own target, outside the row's tap box and outside the sheet.
    assert INTERRUPT_KEY not in _keys(_part(tray, r"<details.*?</details>"))
    # The answer region still decides: a tap's outcome and the armed confirm land there.
    assert 'hx-target="#answer"' in stop
    assert 'hx-sync="#answer:replace"' in stop


def test_the_page_flow_no_longer_carries_the_rows_pieces(tmp_path: Path) -> None:
    body = _page(tmp_path, crowded_chat(), "working")
    view, answer = _view(body), _answer(body)
    for piece in ('class="bubble', 'class="spawned', 'class="queued', "chat__receipt"):
        assert piece not in view, piece
    assert _keys(answer) == []
    assert ">Keys<" not in answer
    assert "data-hp-still" not in body.split('id="agent-tray"')[0]


def test_an_idle_agent_has_no_interrupt_and_its_row_says_how_long_it_worked(
    tmp_path: Path,
) -> None:
    body = _page(tmp_path, turn_chat("done"), "idle")
    tray = _tray(body)
    assert "Worked 4m" in _row(body)
    assert 'class="tray__stop"' not in tray
    assert _keys(tray) == ["esc", "up", "down", "enter"]


def test_a_blocked_agent_keeps_its_answer_region_and_the_row_says_so(tmp_path: Path) -> None:
    body = _page(tmp_path, turn_chat("asking"), "blocked", capture(PERMISSION))
    answer, tray = _answer(body), _tray(body)
    assert "answer__question" in answer
    # The keys stay with the question, Interrupt included, as they always were.
    assert INTERRUPT_KEY in _keys(answer)
    assert _keys(tray) == []
    assert ">Needs you · Do you want to proceed?<" in _row(body)
    assert "tray__row--blocked" in _row(body)


def test_the_tray_route_redraws_the_tray_alone(tmp_path: Path) -> None:
    write_transcript(tmp_path / "transcripts", WORKING_SESSION, crowded_chat())
    rig = build_rig(tmp_path, {SNAPSHOT: _status("working")})
    fragment = rig.http.get(f"{PAGE}/tray", headers=FRAGMENT).text
    assert fragment.lstrip().startswith('<div id="agent-tray"')
    assert f'data-hp-topic="chat:{WORKING_SESSION}"' in fragment
    assert 'hx-trigger="hp:stale, hp:poke from:body"' in fragment
    assert ">Waiting on 2 agents<" in fragment


def test_the_pane_tab_has_the_row_too(tmp_path: Path) -> None:
    write_transcript(tmp_path / "transcripts", WORKING_SESSION, crowded_chat())
    rig = build_rig(tmp_path, {SNAPSHOT: _status("working")})
    pane = rig.http.get(f"{PAGE}?tab=pane").text
    assert ">Waiting on 2 agents<" in _row(pane)
    assert "data-hp-still" in _tray(pane)


def test_the_switch_that_stops_the_animation_is_in_the_sheet(tmp_path: Path) -> None:
    tray = _tray(_page(tmp_path, turn_chat("tool"), "working"))
    switch = 'data-hp-still aria-pressed="false">Stop the animation</button>'
    # Hidden until bubble.js shows it with the choice this device remembers.
    assert re.search(rf'<p class="tray__still" hidden><button [^>]*{switch}</p>', tray)


def test_agents_still_running_beside_an_open_tool_are_counted() -> None:
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
    assert (row.running, row.queued) == (2, 0)
    assert row.sentence == "Working: Running just check. 2 agents running."
