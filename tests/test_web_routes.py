"""Every route over a fake runner: what it renders, what it refuses, and the headers it sends."""

from pathlib import Path

import pytest

from herdr_pwa.engine.transport import (
    EngineError,
    EngineMisconfigured,
    EngineTimeout,
    EngineUnavailable,
    InvalidResponse,
    ProtocolMismatch,
)
from herdr_pwa.jsontypes import JsonObject
from herdr_pwa.web.security import CSP
from tests.webfakes import (
    IDLE_SESSION,
    READ,
    SNAPSHOT,
    WORKING_SESSION,
    agents_of,
    assistant_says,
    build_rig,
    human_says,
    read_frame,
    snapshot_frame,
    write_transcript,
)

HOSTILE = '<script>alert("x")</script>'
FRAGMENT = {"HX-Request": "true"}


def _hostile_frame() -> JsonObject:
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
    rig = build_rig(tmp_path, {SNAPSHOT: EngineUnavailable("socket gone")})
    response = rig.http.get("/feed", headers=FRAGMENT)
    assert response.status_code == 503
    assert "runner unreachable" in response.text
    assert 'hx-get="/feed"' in response.text
    assert "every 12s" in response.text
    assert "socket gone" not in response.text
    assert response.headers["hp-fragment"] == "1"
    assert response.headers["cache-control"] == "no-store"


def test_a_failed_refresh_over_a_good_snapshot_is_degraded_not_empty(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    assert rig.http.get("/feed").status_code == 200
    rig.wire.replies[SNAPSHOT] = EngineTimeout("slow")
    rig.clock.now += 5
    response = rig.http.get("/feed")
    assert response.status_code == 200
    assert "runner is not answering" in response.text
    assert WORKING_SESSION in response.text


@pytest.mark.parametrize(
    ("failure", "status", "headline", "polls"),
    [
        (EngineUnavailable("x"), 503, "runner unreachable", True),
        (EngineTimeout("x"), 503, "runner too slow", True),
        (InvalidResponse("x"), 502, "runner answered nonsense", True),
        (ProtocolMismatch("x"), 409, "runner version drift", True),
        (EngineMisconfigured("x"), 500, "misconfigured", False),
        (EngineError("x"), 502, "runner error", True),
    ],
)
def test_the_failure_table_maps_each_engine_failure(
    tmp_path: Path, failure: Exception, status: int, headline: str, *, polls: bool
) -> None:
    rig = build_rig(tmp_path, {SNAPSHOT: failure})
    response = rig.http.get("/feed", headers=FRAGMENT)
    assert response.status_code == status
    assert headline in response.text
    assert ("hx-trigger" in response.text) is polls


def test_a_failed_page_load_is_a_whole_document(tmp_path: Path) -> None:
    rig = build_rig(tmp_path, {SNAPSHOT: EngineUnavailable("x")})
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
    rig = build_rig(tmp_path)
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
    assert "immutable" in rig.http.get(url).headers["cache-control"]
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
    assert page.count("data-hp-replace") == 5  # leave, Files, End, two tabs
    assert f'href="/agent/{WORKING_SESSION}?tab=pane" data-hp-replace' in page


def test_the_view_switch_lives_in_the_top_bar_and_the_title_scrolls(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
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
