"""The agent drill-in: Chat and Pane tabs and the stats strip, each a fragment the stream refreshes.

The page itself carries the first drawing of the view, the answer region and the composer, so the
newest line arrives with the HTML instead of one fetch after the scripts have run.
"""

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
    SessionUsage,
    Source,
    chat_window,
)
from herdr_pwa.web.chat import ChatView, Turning, chat_view
from herdr_pwa.web.deps import WebState, hidden_tick, viewer_guard
from herdr_pwa.web.herd import agent_status, asked_by, find_agent
from herdr_pwa.web.naming import page_title
from herdr_pwa.web.routers.compose import answer_region, composer_region
from herdr_pwa.web.stats import Reading, Stats, stats_for, turn_times
from herdr_pwa.web.writing import first_note
from herdr_pwa.writes.resolve import doubt

# Files is a tab too, but its pages are the explorer's, reached through /agent/<session>/files.
TABS: Final = ("chat", "pane")
# A held chat redraws only its note slot; the turns the reader has, and their place, stand.
KEEP_DRAWN: Final = {"HX-Retarget": "#chat-note", "HX-Reswap": "outerHTML"}


def _usage(web: WebState, agent: Agent) -> SessionUsage:
    """*agent*'s tally, which the strip and the chat's status bubble both read."""
    return web.usage.read(
        Source.of(agent.session, agent_kind=agent.agent, root=web.settings.transcript_roots or None)
    )


def read_stats(web: WebState, agent: Agent) -> Stats:
    """The stats strip for *agent*, from its harness's own record."""
    usage = _usage(web, agent)
    status = agent_status(web, agent).value
    return stats_for(Reading(agent.agent or "", status, usage, now=web.wall()))


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
    ask = Ask(messages=web.settings.chat_messages, start=query.start, before=query.before)
    roots = web.settings.transcript_roots
    source = Source.of(agent.session, agent_kind=agent.agent, root=roots or None, log=query.log)
    page = chat_window(source, ask=ask, tails=web.tails)
    said = doubt(agent, roots)
    drawn = query.start is not None or query.before is not None
    # Every page reaches the newest turn, an earlier one included, so each says what is on its way.
    return chat_view(page, turning=_turning(web, agent), doubt=said, drawn=drawn)


def _turning(web: WebState, agent: Agent) -> Turning:
    """The turn as the agent's status, its card and its tally tell it, beside the log."""
    status = agent_status(web, agent)
    usage = _usage(web, agent)
    # The prompt's stamp names the turn, so answering a question mid-turn keeps its verb; with
    # no tally, herdr's sequence, which it bumps on every status change, names the stretch.
    turn = usage.turn_started or str(agent.state_change_seq)
    return Turning(
        status=status,
        stretch=f"{agent.session}:{turn}",
        asked=asked_by(web, agent) if needs_you(status) else "",
        times=turn_times(usage, web.wall()),
    )


def read_pane(web: WebState, agent: Agent) -> panetext.PaneView:
    """The Pane tab's mirror of the terminal."""
    return panetext.pane_view(web.client.mirror_read(agent.target, status=agent.status))


def first_view(web: WebState, agent: Agent, tab: str) -> Markup | None:
    """The view the page opens on, drawn into it; ``None`` if the pane cannot be read now."""
    if tab == "chat":
        return web.pages.piece("chat.html", {"agent": agent, "chat": read_chat(web, agent)})
    try:
        view = read_pane(web, agent)
    except EngineError:
        # The region then fetches itself, and its own failure is drawn where the view would be.
        return None
    return web.pages.piece("pane.html", {"agent": agent, "view": view})


def build(web: WebState) -> APIRouter:
    """The drill-in's routes."""
    router = APIRouter(prefix="/agent/{session}", dependencies=[Depends(viewer_guard(web))])
    _page_route(router, web)
    _view_routes(router, web)
    return router


def _page_context(web: WebState, agent: Agent, tab: str, first: str) -> dict[str, object]:
    """The whole drill-in page: its bar, the chosen tab's first drawing, and the write regions."""
    chosen = tab if tab in TABS else TABS[0]
    return {
        "agent": agent,
        "agent_status": agent_status(web, agent),
        "tab": chosen,
        "tabs": TABS,
        "heading": page_title(agent.title, agent.agent, agent.target),
        "first": first_note(first),
        "stats": read_stats(web, agent),
        "view": first_view(web, agent, chosen),
        "answer": web.pages.piece("answer.html", answer_region(web, agent)),
        "composer": web.pages.piece("compose.html", composer_region(web, agent)),
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

    @router.get("/stats", dependencies=[Depends(hidden_tick)])
    def stats(session: str) -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        agent = find_agent(web, session)
        context = {"agent": agent, "stats": read_stats(web, agent)}
        return web.pages.fragment("stats.html", context)
