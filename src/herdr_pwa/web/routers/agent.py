"""The agent drill-in: Chat and Pane tabs and the stats strip, each a fragment the stream refreshes.

The page itself carries the first drawing of the view, the answer region and the composer, so the
newest line arrives with the HTML instead of one fetch after the scripts have run.
"""

from dataclasses import dataclass, replace
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from markupsafe import Markup
from pydantic import BaseModel, ConfigDict, Field

from herdr_pwa import panetext
from herdr_pwa.decisions import needs_you
from herdr_pwa.engine import (
    Agent,
    Ask,
    EngineError,
    Located,
    SessionUsage,
    Source,
    chat_window,
    locate_log,
)
from herdr_pwa.web.background import background_times
from herdr_pwa.web.chat import ChatView, Turning, chat_view, status_row
from herdr_pwa.web.deps import WebState, hidden_tick, viewer_guard
from herdr_pwa.web.herd import agent_status, asked_by, find_agent
from herdr_pwa.web.naming import page_title
from herdr_pwa.web.routers.compose import answer_region, composer_region, row_keys
from herdr_pwa.web.stats import (
    Reading,
    Stats,
    stats_for,
    turn_times,
    with_statusline,
)
from herdr_pwa.web.writing import first_note
from herdr_pwa.writes.resolve import Witness, doubt

# Files is a tab too, but its pages are the explorer's, reached through /agent/<session>/files.
TABS: Final = ("chat", "pane")
# A held chat redraws only its note slot; the turns the reader has, and their place, stand.
KEEP_DRAWN: Final = {"HX-Retarget": "#chat-note", "HX-Reswap": "outerHTML"}


@dataclass(frozen=True, slots=True)
class Reads:
    """One agent as this request read it, each source once, for every piece drawn from it."""

    agent: Agent
    log: Located
    # The tally kept from that log, which the strip and the chat's status bubble both read.
    usage: SessionUsage
    # None when the pane could not be read.
    pane: panetext.PaneView | None = None
    # The visible screen as text, for the composer to claim; None when it was not read.
    screen: str | None = None


def _log_reads(web: WebState, agent: Agent, log: str = "") -> Reads:
    roots = web.settings.transcript_roots or None
    found = locate_log(Source.of(agent.session, agent_kind=agent.agent, root=roots, log=log))
    return Reads(agent, found, web.usage.read(found))


def read_stats(web: WebState, agent: Agent) -> Stats:
    """The stats strip for *agent*, from its own reads of the pane and the log."""
    return stats_of(web, replace(_log_reads(web, agent), pane=pane_or_none(web, agent)))


def stats_of(web: WebState, reads: Reads) -> Stats:
    """The strip from this request's *reads*: the statusline the pane shows, else the harness's
    own record when the pane could not be read."""
    agent = reads.agent
    status = agent_status(web, agent).value
    stats = stats_for(Reading(agent.agent or "", status, reads.usage, now=web.wall()))
    # Unreadable, the strip says what the log says, as it did before the pane showed a statusline.
    pane = reads.pane
    return with_statusline(stats, pane.status if pane is not None else [])


class ChatQuery(BaseModel):
    """Which page of the chat the reader asked for, and which of the agent's logs it reads."""

    # Frozen, so the shared _NEWEST default cannot be changed by one reader under another.
    model_config = ConfigDict(frozen=True)

    # `start` keeps what the reader already has, earlier pages included; `before` asks for one
    # more page above it; neither is the newest page of messages.
    start: int | None = Field(default=None, ge=0)
    before: int | None = Field(default=None, ge=0)
    log: str = Field(default="", max_length=200)


_NEWEST: Final = ChatQuery()


def read_chat(web: WebState, agent: Agent, query: ChatQuery = _NEWEST) -> ChatView:
    """The Chat tab's window: the newest page of messages, or the one the reader asked for."""
    return _chat_of(web, _log_reads(web, agent, query.log), query)


def _chat_of(web: WebState, reads: Reads, query: ChatQuery) -> ChatView:
    agent = reads.agent
    ask = Ask(messages=web.settings.chat_messages, start=query.start, before=query.before)
    page = chat_window(reads.log, ask=ask, tails=web.tails)
    said = doubt(agent, Witness(web.client, web.settings.transcript_roots, web.proc))
    drawn = query.start is not None or query.before is not None
    # Every page reaches the newest turn, an earlier one included, so each says what is on its way.
    return chat_view(page, turning=_turning(web, agent, reads.usage), doubt=said, drawn=drawn)


def _turning(web: WebState, agent: Agent, usage: SessionUsage) -> Turning:
    """The turn as the agent's status, its card and its tally tell it, beside the log."""
    status = agent_status(web, agent)
    # The prompt's stamp names the turn, so answering a question mid-turn keeps its verb; with
    # no tally, herdr's sequence, which it bumps on every status change, names the stretch.
    turn = usage.turn_started or str(agent.state_change_seq)
    now = web.wall()
    return Turning(
        status=status,
        stretch=f"{agent.session}:{turn}",
        asked=asked_by(web, agent) if needs_you(status) else "",
        times=turn_times(usage, now),
        spawned=background_times(web, str(agent.target), usage, now),
    )


def read_pane(web: WebState, agent: Agent) -> panetext.PaneView:
    """The Pane tab's mirror of the terminal."""
    return panetext.pane_view(web.client.mirror_read(agent.target, status=agent.status))


def pane_or_none(web: WebState, agent: Agent) -> panetext.PaneView | None:
    """One read of the pane for everything a page draws from it; ``None`` if it cannot be read now."""
    try:
        return read_pane(web, agent)
    except EngineError:
        return None


def _page_reads(web: WebState, agent: Agent) -> Reads:
    # Read once: the strip's statusline, the Pane tab's mirror and the composer's claim are one
    # terminal, and the strip's tally and the Chat tab are one log.
    reads = _log_reads(web, agent)
    try:
        look = web.client.mirror(agent.target, status=agent.status)
    except EngineError:
        return reads
    return replace(reads, pane=panetext.pane_view(look.shown), screen=look.screen)


def first_view(web: WebState, tab: str, reads: Reads, chat: ChatView) -> Markup | None:
    """The view the page opens on, drawn into it from the page's *reads*; ``None`` when the pane
    could not be read, so the region fetches itself and its own failure is drawn in its place."""
    if tab == "chat":
        return web.pages.piece("chat.html", {"agent": reads.agent, "chat": chat})
    if reads.pane is None:
        return None
    return web.pages.piece("pane.html", {"agent": reads.agent, "view": reads.pane})


def tray_region(web: WebState, agent: Agent, chat: ChatView) -> dict[str, object]:
    """The status row and its sheet: the turn as *chat* tells it, and the keys the row offers."""
    status = agent_status(web, agent).value
    return {"agent": agent, "chat": chat, "row": status_row(chat, status), **row_keys(web, agent)}


def _status_word(web: WebState, agent: Agent, chat: ChatView) -> str:
    # herdr still says working for a log still past the threshold; the page says what it shows.
    quiet = chat.bubble is not None and chat.bubble.state == "quiet"
    return "quiet" if quiet else agent_status(web, agent).value


def build(web: WebState) -> APIRouter:
    """The drill-in's routes."""
    router = APIRouter(prefix="/agent/{session}", dependencies=[Depends(viewer_guard(web))])
    _page_route(router, web)
    _view_routes(router, web)
    return router


def _page_context(web: WebState, agent: Agent, tab: str, first: str) -> dict[str, object]:
    """The whole drill-in page: its bar, the chosen tab's first drawing, and the write regions."""
    chosen = tab if tab in TABS else TABS[0]
    reads = _page_reads(web, agent)
    # The status row reads the same page of the log on either tab.
    chat = _chat_of(web, reads, _NEWEST)
    answer = answer_region(web, agent)
    # The composer is held by the question the answer region draws, never by a second read.
    held = answer["holds"] is True
    return {
        "agent": agent,
        "agent_status": agent_status(web, agent),
        "status_word": _status_word(web, agent, chat),
        "tab": chosen,
        "tabs": TABS,
        "heading": page_title(agent.title, agent.agent, agent.target),
        "first": first_note(first),
        "stats": stats_of(web, reads),
        "view": first_view(web, chosen, reads, chat),
        "answer": web.pages.piece("answer.html", answer),
        "tray": web.pages.piece("tray.html", tray_region(web, agent, chat)),
        "composer": web.pages.piece(
            "compose.html", composer_region(agent, reads.screen, held=held)
        ),
    }


def _page_route(router: APIRouter, web: WebState) -> None:
    @router.get("")
    def agent_page(  # pyright: ignore[reportUnusedFunction]
        session: str, tab: str = "chat", first: str = ""
    ) -> Response:
        agent = find_agent(web, session)
        if tab == "files":
            return RedirectResponse(f"/agent/{agent.session}/files", status_code=303)
        return web.pages.page("agent_page.html", _page_context(web, agent, tab, first))


def _view_routes(router: APIRouter, web: WebState) -> None:
    @router.get("/chat", dependencies=[Depends(hidden_tick)])
    def chat(  # pyright: ignore[reportUnusedFunction]
        session: str, query: Annotated[ChatQuery, Query()]
    ) -> HTMLResponse:
        agent = find_agent(web, session)
        chat = read_chat(web, agent, query)
        context = {"agent": agent, "chat": chat}
        if chat.held:
            return web.pages.fragment("chat_note.html", context, headers=KEEP_DRAWN)
        return web.pages.fragment("chat.html", context)

    @router.get("/pane", dependencies=[Depends(hidden_tick)])
    def pane(session: str) -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        agent = find_agent(web, session)
        return web.pages.fragment("pane.html", {"agent": agent, "view": read_pane(web, agent)})

    @router.get("/tray", dependencies=[Depends(hidden_tick)])
    def tray(session: str) -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        agent = find_agent(web, session)
        return web.pages.fragment("tray.html", tray_region(web, agent, read_chat(web, agent)))

    @router.get("/stats", dependencies=[Depends(hidden_tick)])
    def stats(session: str) -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        agent = find_agent(web, session)
        context = {"agent": agent, "stats": read_stats(web, agent)}
        return web.pages.fragment("stats.html", context)
