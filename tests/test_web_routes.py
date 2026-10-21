"""Every route over a fake runner: what it renders, what it refuses, and the headers it sends."""

import re
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import JsonValue

from herdr_pwa.engine import (
    AgentStatus,
    EngineError,
    EngineMisconfiguredError,
    EngineTimeoutError,
    EngineUnavailableError,
    HistoryState,
    InvalidResponseError,
    ProtocolMismatchError,
    Speaker,
    TranscriptPage,
    TranscriptTurn,
)
from herdr_pwa.files.roots import root_id
from herdr_pwa.protocol import WRITE_METHODS
from herdr_pwa.web.chat_status import WORKING_VERBS, Turning
from herdr_pwa.web.chat_view import chat_view
from herdr_pwa.web.security import CSP
from tests.filefakes import make_repo, stand_in
from tests.webfakes import (
    IDLE_SESSION,
    READ,
    SNAPSHOT,
    WORKING_PANE,
    WORKING_SESSION,
    Reply,
    Rig,
    agents_of,
    assistant_says,
    build_rig,
    human_says,
    read_frame,
    routes_of,
    snapshot_frame,
    write_transcript,
)

HOSTILE = '<script>alert("x")</script>'
FRAGMENT = {"HX-Request": "true"}


def _hostile_frame() -> dict[str, JsonValue]:
    frame = snapshot_frame()
    for agent in agents_of(frame):
        if agent.get("pane_id") == "w1:p3":
            agent["name"] = HOSTILE
    return frame


def test_feed_page_is_a_shell_that_polls_the_fragment(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    response = rig.http.get("/")
    assert response.status_code == 200
    assert 'hx-get="/feed"' in response.text
    assert rig.wire.calls == []


def test_every_response_carries_the_one_policy(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    for path in ("/", "/feed", "/static/app.css", "/agent/nobody-here", "/healthz"):
        response = rig.http.get(path)
        assert response.headers["content-security-policy"] == CSP, path
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "same-origin"
        assert response.headers["hp-build"]


def test_policy_allows_no_inline_script_or_style() -> None:
    assert "unsafe-inline" not in CSP
    assert "unsafe-eval" not in CSP
    for directive in ("base-uri", "form-action", "frame-ancestors", "object-src"):
        assert directive in CSP


def test_feed_fragment_ranks_sections_and_names_agents(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    response = rig.http.get("/feed", headers=FRAGMENT)
    assert response.status_code == 200
    body = response.text
    assert body.index("Needs you") < body.index("Working") < body.index("Idle")
    assert f'href="/agent/{WORKING_SESSION}"' in body
    assert "nobody is waiting on you" in body
    # A live herd is redrawn when the event stream says it changed, not on a timer.
    assert "every 5s" not in body
    assert 'data-hp-topic="feed"' in body
    assert 'hx-trigger="hp:stale, hp:poke from:body"' in body
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["hp-fragment"] == "1"


def test_a_workspace_filter_narrows_all_but_needs_you(tmp_path: Path) -> None:
    frame = snapshot_frame()
    for agent in agents_of(frame):
        if agent.get("pane_id") == "w1:p3":
            agent["agent_status"] = "blocked"
    rig = build_rig(tmp_path, {SNAPSHOT: frame, READ: read_frame("")})
    shell = rig.http.get("/?ws=w4").text
    assert 'hx-get="/feed?ws=w4"' in shell
    body = rig.http.get("/feed?ws=w4", headers=FRAGMENT).text
    assert 'hx-get="/feed?ws=w4"' in body
    assert f'href="/agent/{WORKING_SESSION}"' in body
    assert "w4:p1" in body
    assert "w2:p1" not in body
    chips = body[body.index('class="chips"') :]
    assert chips.index('href="/?ws=w4" aria-current="true" data-hp-replace') < chips.index(
        'href="/"'
    )
    assert body.index('class="chips"') < body.index("Needs you") < body.index("always shown")
    assert "in notes:" in body


def test_a_blocked_card_with_no_readable_question_says_where_it_is(tmp_path: Path) -> None:
    frame = snapshot_frame()
    for agent in agents_of(frame):
        if agent.get("pane_id") == "w1:p3":
            agent["agent_status"] = "blocked"
    rule = "\u2500" * 40
    rig = build_rig(tmp_path, {SNAPSHOT: frame, READ: read_frame(f"{rule}\n\u276f \n{rule}\n")})
    body = rig.http.get("/feed", headers=FRAGMENT).text
    assert "Open to see what it&rsquo;s asking" in body
    assert "\u276f" not in body
    # The feed's cards carry their pane, so a poll that re-sorts them can hold one in place.
    assert 'data-hp-key="w1:p3"' in body


def test_an_unknown_workspace_filter_shows_the_whole_herd(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    body = rig.http.get("/feed?ws=gone", headers=FRAGMENT).text
    assert 'hx-get="/feed"' in body
    assert "w4:p1" in body
    assert "w2:p1" in body


def test_a_hostile_agent_name_stays_text(tmp_path: Path) -> None:
    rig = build_rig(tmp_path, {SNAPSHOT: _hostile_frame()})
    feed = rig.http.get("/feed").text
    page = rig.http.get(f"/agent/{WORKING_SESSION}").text
    for body in (feed, page):
        assert HOSTILE not in body
        assert "&lt;script&gt;" in body


def test_a_hidden_page_poll_stops_its_clock(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    response = rig.http.get("/feed", headers={"HP-Hidden": "1"})
    assert response.status_code == 286
    assert response.headers["hx-reswap"] == "none"
    assert rig.wire.calls == []


def test_an_unreachable_runner_renders_honestly_and_keeps_polling(tmp_path: Path) -> None:
    rig = build_rig(tmp_path, {SNAPSHOT: EngineUnavailableError("socket gone")})
    response = rig.http.get("/feed", headers=FRAGMENT)
    assert response.status_code == 503
    assert "can\u2019t reach herdr" in response.text
    assert 'hx-get="/feed"' in response.text
    assert "every 12s" in response.text
    assert "socket gone" not in response.text
    assert response.headers["hp-fragment"] == "1"
    assert response.headers["cache-control"] == "no-store"


def test_a_failed_refresh_over_a_good_snapshot_is_degraded_not_empty(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    assert rig.http.get("/feed").status_code == 200
    rig.wire.replies[SNAPSHOT] = EngineTimeoutError("slow")
    rig.clock.now += 5
    response = rig.http.get("/feed")
    assert response.status_code == 200
    assert "herdr is not answering" in response.text
    assert WORKING_SESSION in response.text


@dataclass(frozen=True, slots=True)
class _Row:
    """One row of the failure table: the status, headline and polling an engine failure gets."""

    failure: Exception
    status: int
    headline: str
    polls: bool


def _failure_name(row: _Row) -> str:
    return type(row.failure).__name__


@pytest.mark.parametrize(
    "row",
    [
        _Row(EngineUnavailableError("x"), 503, "can\u2019t reach herdr", polls=True),
        _Row(EngineTimeoutError("x"), 503, "herdr too slow", polls=True),
        _Row(InvalidResponseError("x"), 502, "herdr answered nonsense", polls=True),
        _Row(ProtocolMismatchError("x"), 409, "herdr version drift", polls=True),
        _Row(EngineMisconfiguredError("x"), 500, "misconfigured", polls=False),
        _Row(EngineError("x"), 502, "herdr error", polls=True),
    ],
    ids=_failure_name,
)
def test_the_failure_table_maps_each_engine_failure(tmp_path: Path, row: _Row) -> None:
    rig = build_rig(tmp_path, {SNAPSHOT: row.failure})
    response = rig.http.get("/feed", headers=FRAGMENT)
    assert response.status_code == row.status
    assert row.headline in response.text
    assert ("hx-trigger" in response.text) is row.polls


def test_a_failure_fragment_stands_in_for_its_region_under_the_region_id(tmp_path: Path) -> None:
    # The page holds a region's redraw back under a finger by its id; a failure with none is
    # swapped out from under the reader the moment it heals.
    rig = build_rig(tmp_path, {SNAPSHOT: EngineUnavailableError("x")})
    url = f"/agent/{WORKING_SESSION}/answer"
    response = rig.http.get(url, headers={**FRAGMENT, "HX-Target": "answer"})
    assert response.status_code == 503
    assert 'id="answer"' in response.text
    assert f'data-hp-poll="{url}"' in response.text


@pytest.mark.parametrize("target", ['x" onclick="alert(1)', "", "Answer", "a b"])
def test_a_failure_takes_only_a_plain_region_id_from_the_request(
    tmp_path: Path, target: str
) -> None:
    rig = build_rig(tmp_path, {SNAPSHOT: EngineUnavailableError("x")})
    response = rig.http.get("/feed", headers={**FRAGMENT, "HX-Target": target})
    assert response.status_code == 503
    assert " id=" not in response.text.split(">", 1)[0]


TEMPLATES = Path(__file__).parents[1] / "src" / "herdr_pwa" / "web" / "templates"
TAG = re.compile(r"<[a-z][^>]*>", re.DOTALL)
TARGET = re.compile(r'hx-target="(?P<target>[^"]*)"')
ID = re.compile(r'\sid="(?P<id>[^"]*)"')


def region_ids() -> set[str]:
    """Every element a fragment replaces, by id: a ``this`` target's own, or a ``#id`` target."""
    found: set[str] = set()
    for template in TEMPLATES.glob("*.html"):
        for tag in (m[0] for m in TAG.finditer(template.read_text())):
            target = TARGET.search(tag)
            own = ID.search(tag)
            if target and target["target"].startswith("#"):
                found.add(target["target"][1:])
            elif target and target["target"] == "this" and own and own["id"] != "{{ region }}":
                found.add(own["id"])
    return found


def test_every_region_id_the_templates_draw_is_echoed_by_a_failure(tmp_path: Path) -> None:
    # The echo takes only plain ids; a region outside that shape would lose its id on failure.
    ids = region_ids()
    assert {"answer", "agent-view", "composer", "find-results"} <= ids
    rig = build_rig(tmp_path, {SNAPSHOT: EngineUnavailableError("x")})
    for region in sorted(ids):
        response = rig.http.get("/feed", headers={**FRAGMENT, "HX-Target": region})
        assert f'id="{region}"' in response.text.split(">", 1)[0], region


def test_a_failed_page_load_is_a_whole_document(tmp_path: Path) -> None:
    rig = build_rig(tmp_path, {SNAPSHOT: EngineUnavailableError("x")})
    response = rig.http.get(f"/agent/{WORKING_SESSION}")
    assert response.status_code == 503
    assert response.text.startswith("<!doctype html>")
    assert 'http-equiv="refresh"' in response.text


def test_a_gone_agent_is_404_and_does_not_poll(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    response = rig.http.get("/agent/no-such-session/chat", headers=FRAGMENT)
    assert response.status_code == 404
    assert "agent gone" in response.text
    assert "hx-trigger" not in response.text


def test_a_pane_address_is_not_an_identity(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    assert rig.http.get("/agent/w1:p3").status_code == 400


def test_the_agent_page_has_both_tabs(tmp_path: Path) -> None:
    rig = build_rig(tmp_path, {SNAPSHOT: snapshot_frame(), READ: read_frame("$ ")})
    body = rig.http.get(f"/agent/{WORKING_SESSION}?tab=pane").text
    assert f'hx-get="/agent/{WORKING_SESSION}/pane"' in body
    assert "?tab=chat" in body


def test_chat_renders_prose_as_escaped_nodes(tmp_path: Path) -> None:
    write_transcript(
        tmp_path / "transcripts",
        WORKING_SESSION,
        [
            human_says(f"please {HOSTILE}"),
            assistant_says(f"**done** and `x` {HOSTILE} [ok](https://e.x) [no](javascript:1)"),
        ],
    )
    rig = build_rig(tmp_path)
    response = rig.http.get(f"/agent/{WORKING_SESSION}/chat", headers=FRAGMENT)
    assert response.status_code == 200
    body = response.text
    assert HOSTILE not in body
    assert body.count("&lt;script&gt;") == 2
    assert "<strong>done</strong>" in body
    assert "<code>x</code>" in body
    assert 'href="https://e.x" rel="noopener noreferrer"' in body
    assert "javascript:" not in body.replace("[no](javascript:1)", "")
    assert response.headers["cache-control"] == "no-store"


def test_chat_without_a_transcript_says_so(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    body = rig.http.get(f"/agent/{IDLE_SESSION}/chat").text
    assert "No transcript was found" in body


def test_chat_for_a_harness_with_no_transcript_offers_the_pane_door(tmp_path: Path) -> None:
    frame = snapshot_frame()
    working = next(a for a in agents_of(frame) if a.get("pane_id") == WORKING_PANE)
    working["agent"] = "cursor"
    rig = build_rig(tmp_path, {SNAPSHOT: frame, READ: read_frame("")})
    body = rig.http.get(f"/agent/{WORKING_SESSION}/chat", headers=FRAGMENT).text
    note = re.search(r'<p class="empty">.*?</p>', body, re.DOTALL)
    assert note is not None, "an empty log says why"
    assert "keeps no transcript this app can read" in note[0]
    door = f'href="/agent/{WORKING_SESSION}?tab=pane" data-hp-replace>'
    assert f'<a class="chat__go" {door}Look at the Pane</a>' in note[0]
    assert "see Pane" not in body, "a link, not a direction"


def test_pane_mirror_keeps_colour_as_classes_and_text_escaped(tmp_path: Path) -> None:
    replies = {SNAPSHOT: snapshot_frame(), READ: read_frame(f"\x1b[31mred {HOSTILE}\x1b[0m\n")}
    rig = build_rig(tmp_path, replies)
    response = rig.http.get(f"/agent/{WORKING_SESSION}/pane", headers=FRAGMENT)
    assert response.status_code == 200
    assert '<span class="ansi-red">red &lt;script&gt;' in response.text
    assert HOSTILE not in response.text
    assert "\x1b" not in response.text


def test_workspaces_lists_each_one(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    body = rig.http.get("/workspaces").text
    assert "herdr-pwa" in body
    assert "notes" in body


def test_large_answers_are_compressed(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    response = rig.http.get("/feed", headers={"Accept-Encoding": "gzip"})
    assert response.headers["content-encoding"] == "gzip"
    assert response.headers["content-security-policy"] == CSP
    raw = rig.http.get("/static/app.css", headers={"Accept-Encoding": "gzip"})
    assert raw.headers["content-encoding"] == "gzip"


def test_static_is_cached_forever_only_at_its_hashed_url(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    page = rig.http.get("/").text
    start = page.index("/static/app.css?v=")
    url = page[start : page.index('"', start)]
    # Private, since the file is behind the identity gate.
    assert rig.http.get(url).headers["cache-control"] == "private, max-age=31536000, immutable"
    assert rig.http.get("/static/app.css?v=stale").headers["cache-control"] == "no-cache"
    assert rig.http.get("/static/../settings.py").status_code == 404
    assert rig.http.get("/static/nope.js").status_code == 404


def test_no_identity_is_refused_with_the_policy_still_on(tmp_path: Path) -> None:
    rig = build_rig(tmp_path, headers={})
    response = rig.http.get("/feed")
    assert response.status_code == 401
    assert response.headers["content-security-policy"] == CSP
    assert rig.wire.calls == []


def test_an_assumed_login_works_only_on_a_loopback_host(tmp_path: Path) -> None:
    rig = build_rig(tmp_path, headers={}, assume_login="me@example.com")
    assert rig.http.get("/feed", headers={"Host": "127.0.0.1:8772"}).status_code == 200
    assert rig.http.get("/feed", headers={"Host": "evil.example"}).status_code == 403


def test_a_crash_is_a_plain_500_with_the_policy(tmp_path: Path) -> None:
    rig = build_rig(tmp_path, {SNAPSHOT: RuntimeError("bug")})
    response = rig.http.get("/feed")
    assert response.status_code == 500
    assert response.headers["content-security-policy"] == CSP
    assert "bug" not in response.text


def test_only_the_rowless_feed_shell_may_enter_the_back_forward_cache(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    assert rig.http.get("/").headers["cache-control"] == "private, no-cache"
    for path in (f"/agent/{WORKING_SESSION}", "/workspaces", "/feed"):
        assert rig.http.get(path).headers["cache-control"] == "no-store", path


def test_back_belongs_to_the_herd_in_the_markup(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    feed = rig.http.get("/").text
    assert 'class="topbar__link" href="/workspaces">' in feed
    assert 'class="topbar__link" href="/push">' in feed
    assert 'class="topbar__link" href="/files">' in feed
    cards = rig.http.get("/feed", headers=FRAGMENT).text
    assert f'href="/agent/{WORKING_SESSION}">' in cards
    page = rig.http.get(f"/agent/{WORKING_SESSION}").text
    # Leave, Files, End, two tabs, and the answer region's way to the Pane should a tap be lost.
    assert page.count("data-hp-replace") == 6
    assert f'href="/agent/{WORKING_SESSION}?tab=pane" data-hp-replace' in page


def test_the_view_switch_lives_in_the_top_bar_and_the_title_scrolls(tmp_path: Path) -> None:
    rig = build_rig(tmp_path, {SNAPSHOT: snapshot_frame(), READ: read_frame("$ ")})
    page = rig.http.get(f"/agent/{WORKING_SESSION}?tab=pane").text
    bar = page[page.index('<header class="topbar">') : page.index("</header>")]
    assert '<nav class="tabs tabs--3" aria-label="View">' in bar
    assert f'href="/agent/{WORKING_SESSION}?tab=chat" data-hp-replace' in bar
    assert f'href="/agent/{WORKING_SESSION}/files" data-hp-replace' in bar
    assert 'aria-current="page"><span class="tab__label">Pane</span>' in bar
    # The tab replaces the link that sat beside End; End keeps the far edge alone.
    assert "agent__files" not in page
    main = page[page.index("<main ") :]
    assert main.index('<h1 class="agent__title">') < main.index('class="agent__meta')
    assert "<h1" not in bar


# The status row's bubble: the agent's status decides whether there is one, so it cannot
# flicker between polls, and a locked store never reads as "nothing said".

NOTE = '<span class="bubble bubble--'


def test_the_row_says_it_is_working_while_the_agent_works(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    body = rig.http.get(f"/agent/{WORKING_SESSION}/tray").text
    assert NOTE in body
    assert 'data-hp-said="Working"' in body


def _verb_in(body: str) -> str:
    found = re.search(
        r'<span class="bubble__text" aria-hidden="true">(?P<verb>\w+)\u2026</span>', body
    )
    assert found is not None, body
    return found["verb"]


def _working_at(tmp_path: Path, seq: int, revision: int = 1) -> str:
    frame = snapshot_frame()
    for agent in agents_of(frame):
        agent["state_change_seq"] = seq
        agent["revision"] = revision
    rig = build_rig(tmp_path, {SNAPSHOT: frame})
    return rig.http.get(f"/agent/{WORKING_SESSION}/tray", headers=FRAGMENT).text


def test_the_working_verb_holds_for_a_turn_and_changes_with_the_next(tmp_path: Path) -> None:
    """Every poll of one working stretch redraws the same verb; a new stretch may pick another."""
    first = _verb_in(_working_at(tmp_path / "a", 7))
    assert first in WORKING_VERBS
    # A poll redraws on every new screen (revision); the stretch, and so the verb, is the same.
    assert _verb_in(_working_at(tmp_path / "b", 7, revision=40)) == first
    verbs = {_verb_in(_working_at(tmp_path / f"s{seq}", seq)) for seq in range(8, 20)}
    assert len(verbs) > 1


def test_the_bubble_is_gone_when_the_agent_stops(tmp_path: Path) -> None:
    frame = snapshot_frame()
    for agent in agents_of(frame):
        agent["agent_status"] = "done"
    rig = build_rig(tmp_path, {SNAPSHOT: frame})
    body = rig.http.get(f"/agent/{WORKING_SESSION}/tray").text
    assert NOTE not in body


HELD = "The transcript could not be read just now"


def _talking(tmp_path: Path) -> Path:
    """A working agent's log of a few exchanges; the file itself, to lock."""
    records = [
        record
        for n in range(4)
        for record in (
            human_says(f"ask {n}", uuid=f"h{n}"),
            assistant_says(f"Reply {n}.", uuid=f"a{n}"),
        )
    ]
    write_transcript(tmp_path / "transcripts", WORKING_SESSION, records)
    return next((tmp_path / "transcripts").rglob(f"{WORKING_SESSION}.jsonl"))


def test_a_refresh_that_cannot_read_the_log_swaps_only_the_note(tmp_path: Path) -> None:
    """A locked store must not blank the turns already drawn: only the note slot is redrawn."""
    log = _talking(tmp_path)
    rig = build_rig(tmp_path, chat_messages=2)
    drawn = rig.http.get(f"/agent/{WORKING_SESSION}/chat", headers=FRAGMENT).text
    assert "Reply 3." in drawn
    assert HELD not in drawn
    log.chmod(0)
    try:
        for ask in ("start=0", "before=999999"):
            held = rig.http.get(f"/agent/{WORKING_SESSION}/chat?{ask}", headers=FRAGMENT)
            assert held.status_code == 200, ask
            assert held.headers.get("hx-retarget") == "#chat-note", ask
            assert held.headers.get("hx-reswap") == "outerHTML", ask
            assert 'id="agent-view"' not in held.text, ask
            assert HELD in held.text, ask
    finally:
        log.chmod(0o600)
    healed = rig.http.get(f"/agent/{WORKING_SESSION}/chat?start=0", headers=FRAGMENT)
    assert "hx-retarget" not in healed.headers
    assert 'id="agent-view"' in healed.text
    assert HELD not in healed.text


@pytest.mark.parametrize("ask", ["start=-1", "before=-1", f"log={'x' * 201}"])
def test_a_chat_ask_out_of_bounds_is_refused(tmp_path: Path, ask: str) -> None:
    """The chat's query holds its bounds: no negative cursor, no log name past 200 characters."""
    _talking(tmp_path)
    rig = build_rig(tmp_path)
    refused = rig.http.get(f"/agent/{WORKING_SESSION}/chat?{ask}", headers=FRAGMENT)
    assert refused.status_code == 422


def test_an_unreadable_log_on_opening_says_so_in_the_whole_view(tmp_path: Path) -> None:
    """Nothing is drawn yet, so there is nothing to keep: the view is drawn with its note."""
    _talking(tmp_path).chmod(0)
    rig = build_rig(tmp_path)
    response = rig.http.get(f"/agent/{WORKING_SESSION}/chat", headers=FRAGMENT)
    assert "hx-retarget" not in response.headers
    assert 'id="agent-view"' in response.text
    assert "could not be read" in response.text


def test_an_earlier_page_leaves_the_row_alone(tmp_path: Path) -> None:
    """Load earlier redraws the log; the row above the composer is its own region and stays."""
    _talking(tmp_path)
    rig = build_rig(tmp_path, chat_messages=2)
    earlier = rig.http.get(f"/agent/{WORKING_SESSION}/chat?before=999999", headers=FRAGMENT)
    assert "Reply 3." in earlier.text
    assert NOTE not in earlier.text
    assert NOTE in rig.http.get(f"/agent/{WORKING_SESSION}/tray", headers=FRAGMENT).text


def test_whether_there_is_a_bubble_is_a_function_of_the_status_alone() -> None:
    """No pane read decides it: the same page draws a bubble for a working or blocked agent."""
    page = TranscriptPage(
        turns=[TranscriptTurn(uuid="t1", speaker=Speaker.human, text="hi")],
        state=HistoryState.ok,
    )
    for status in AgentStatus:
        view = chat_view(page, turning=Turning(status=status, stretch="s:1"))
        shown = status in (AgentStatus.WORKING, AgentStatus.BLOCKED)
        assert (view.bubble is not None) is shown, status


# ---- A GET never writes anything that matters ---------------------------------------------


def _get_walk(repo: Path) -> dict[str, tuple[str, ...]]:
    """Each GET route, by its template, and the URLs that walk it."""
    base = f"/files/{root_id(repo)}"
    agent = f"/agent/{IDLE_SESSION}"
    return {
        "/healthz": ("/healthz",),
        "/static/{name}": ("/static/app.js",),
        "/": ("/",),
        "/feed": ("/feed",),
        "/agent/{session}": (agent, f"{agent}?tab=chat", f"{agent}?tab=pane"),
        "/agent/{session}/chat": (f"{agent}/chat",),
        "/agent/{session}/pane": (f"{agent}/pane",),
        "/agent/{session}/stats": (f"{agent}/stats",),
        "/agent/{session}/tray": (f"{agent}/tray",),
        "/agent/{session}/compose": (f"{agent}/compose",),
        "/agent/{session}/answer": (f"{agent}/answer",),
        "/agent/{session}/close": (f"{agent}/close",),
        "/agent/{session}/files": (
            f"/agent/{WORKING_SESSION}/files",
            f"/agent/{WORKING_SESSION}/files?path=README.md",
        ),
        "/workspaces": ("/workspaces",),
        "/starting/{pane}": ("/starting/w1:p3", "/starting/w9:p9"),
        "/new": ("/new",),
        "/workspaces/new": ("/workspaces/new",),
        "/workspaces/{workspace_id}/rename": ("/workspaces/w1/rename",),
        "/workspaces/{workspace_id}/close": ("/workspaces/w1/close",),
        "/files": ("/files",),
        "/files/{root_id}": (base,),
        "/files/{root_id}/tree": (f"{base}/tree",),
        "/files/{root_id}/find": (f"{base}/find?q=a",),
        "/files/{root_id}/file": (f"{base}/file?path=README.md",),
        "/files/{root_id}/diff": (f"{base}/diff?path=src/app/server.py",),
        "/files/{root_id}/raw": (f"{base}/raw?path=assets/logo.png",),
        "/files/{root_id}/watch": (f"{base}/watch?path=README.md&v=1",),
        "/push": ("/push",),
        "/push/status": ("/push/status",),
        "/push/key": ("/push/key",),
        "/push/prefs": ("/push/prefs",),
        "/events": ("/events?topic=feed", f"/events?topic=agent:{IDLE_SESSION}"),
        "/sw.js": ("/sw.js",),
        "/manifest.webmanifest": ("/manifest.webmanifest",),
        "/offline": ("/offline",),
        "/apple-touch-icon.png": ("/apple-touch-icon.png",),
        "/apple-touch-icon-precomposed.png": ("/apple-touch-icon-precomposed.png",),
        "/apple-touch-icon-120x120.png": ("/apple-touch-icon-120x120.png",),
        "/apple-touch-icon-120x120-precomposed.png": ("/apple-touch-icon-120x120-precomposed.png",),
    }


def _finished(status: str, seq: int, repo: Path) -> dict[str, JsonValue]:
    frame = stand_in(snapshot_frame(), repo)
    for raw in agents_of(frame):
        if IDLE_SESSION in str(raw.get("agent_session")):
            raw["agent_status"] = status
            raw["state_change_seq"] = seq
    return frame


def _state(directory: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob("*") if p.is_file()
    }


def _idle_card(rig: Rig) -> str:
    feed = rig.http.get("/feed", headers=FRAGMENT).text
    found = re.search(
        r'card card--(?P<status>\w+)"[^>]*>\s*<a[^>]*href="/agent/' + IDLE_SESSION, feed
    )
    assert found is not None, feed
    return found["status"]


def test_no_get_writes_anything_that_matters(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "work" / "demo-service")
    ok: dict[str, JsonValue] = {"id": "x", "result": {"type": "ok"}}
    replies: dict[str, Reply] = {SNAPSHOT: _finished("working", 20, repo), READ: read_frame("$ ")}
    # A write the runner would take, so one sent from a GET is counted, not refused.
    replies.update(dict.fromkeys(WRITE_METHODS, ok))
    rig = build_rig(tmp_path, replies, events_lifetime=0.3)
    walk = _get_walk(repo)
    gets = {r.path for r in routes_of(rig) if "GET" in (r.methods or ())}
    # A new GET route fails here until it is walked.
    assert set(walk) == gets
    # The agent finished after the phone's ledger saw it work, so it is news until read.
    assert _idle_card(rig) == "working"
    rig.wire.replies[SNAPSHOT] = _finished("done", 21, repo)
    rig.clock.sleep(60.0)
    assert _idle_card(rig) == "done"
    before = _state(rig.settings.state_dir)
    # Each stream answers once and ends: every look at the clock ages it a second.
    rig.clock.step = 1.0
    for urls in walk.values():
        for url in urls:
            response = rig.http.get(url, follow_redirects=False)
            assert response.status_code < 500, (url, response.status_code)
    rig.clock.step = 0.0
    assert not set(rig.wire.calls) & WRITE_METHODS, rig.wire.calls
    # Opening the agent's page and every region on it did not mark the finish read.
    assert _idle_card(rig) == "done"
    assert _state(rig.settings.state_dir) == before
