"""Opening an agent: the newest line comes with the page, and what it costs is counted, not timed.

The page used to arrive empty and fetch its view, its question and its composer once the scripts
had run, so the first line waited a round trip behind the HTML. These pin the structure (no
fragment fetch stands between the page and its first line) and the reads (the page reads only the
newest page of messages, a refresh reads nothing it already has, and a warmed app reads nothing).
"""

from pathlib import Path
from typing import cast

from fastapi import FastAPI

from herdr_pwa.engine import EngineUnavailableError
from herdr_pwa.web.deps import WebState
from herdr_pwa.web.warm import warm
from tests.catalogue import tool_heavy_chat
from tests.webfakes import (
    READ,
    SNAPSHOT,
    WORKING_SESSION,
    Rig,
    build_rig,
    read_frame,
    snapshot_frame,
    write_transcript,
)

AGENT = f"/agent/{WORKING_SESSION}"
# What makes a region fetch itself as the page loads: a round trip before it can draw.
FETCHED_ON_LOAD = 'hx-trigger="load"'


def _web(rig: Rig) -> WebState:
    app = rig.http.app
    assert isinstance(app, FastAPI)
    # Starlette's state is Any; object makes the isinstance below narrow it.
    web = cast("object", app.state.web)
    assert isinstance(web, WebState)
    return web


def _log(tmp_path: Path) -> Path:
    write_transcript(tmp_path / "transcripts", WORKING_SESSION, tool_heavy_chat())
    return next((tmp_path / "transcripts").rglob(f"{WORKING_SESSION}.jsonl"))


def _view_start(page: str) -> int:
    marker = f'data-hp-poll="{AGENT}/chat?start='
    at = page.index(marker) + len(marker)
    return int(page[at : page.index("&", at)])


def test_the_chat_page_arrives_with_its_newest_line_question_and_composer(tmp_path: Path) -> None:
    _log(tmp_path)
    rig = build_rig(tmp_path)
    page = rig.http.get(AGENT).text
    # The newest message of the fixture, drawn in the page itself.
    assert 'data-hp-key="turn:a59"' in page
    assert 'id="agent-view" class="chat"' in page
    assert 'id="answer"' in page
    assert 'id="composer"' in page
    assert FETCHED_ON_LOAD not in page


def test_the_pane_page_arrives_with_its_screen(tmp_path: Path) -> None:
    rig = build_rig(tmp_path, {SNAPSHOT: snapshot_frame(), READ: read_frame("$ make deploy\n")})
    page = rig.http.get(f"{AGENT}?tab=pane").text
    assert "make deploy" in page
    assert 'id="agent-view" class="mirror"' in page
    assert FETCHED_ON_LOAD not in page


def test_a_screen_that_cannot_be_read_leaves_the_pane_to_fetch_itself(tmp_path: Path) -> None:
    replies = {SNAPSHOT: snapshot_frame(), READ: EngineUnavailableError("the runner went away")}
    rig = build_rig(tmp_path, replies)
    page = rig.http.get(f"{AGENT}?tab=pane")
    assert page.status_code == 200
    # Its own route then draws the failure where the view would be, as it always has.
    assert f'hx-get="{AGENT}/pane" hx-trigger="load"' in page.text


def test_the_page_reads_only_the_newest_messages_and_a_refresh_reads_nothing(
    tmp_path: Path,
) -> None:
    log = _log(tmp_path)
    rig = build_rig(tmp_path)
    tails = _web(rig).tails
    page = rig.http.get(AGENT).text
    start = _view_start(page)
    size = log.stat().st_size
    # From the page's first message to the end, and not a byte before it.
    assert 0 < start < size
    assert tails.bytes_read == size - start
    opened = tails.bytes_read
    rig.http.get(f"{AGENT}/chat?start={start}&log={WORKING_SESSION}")
    rig.http.get(AGENT)
    assert tails.bytes_read == opened


def test_a_warmed_app_opens_a_chat_without_reading_the_transcript(tmp_path: Path) -> None:
    _log(tmp_path)
    rig = build_rig(tmp_path)
    web = _web(rig)
    assert warm(web) >= 1
    warmed = web.tails.bytes_read
    assert warmed > 0
    page = rig.http.get(AGENT).text
    assert 'data-hp-key="turn:a59"' in page
    assert web.tails.bytes_read == warmed
