"""The agent drill-in: a page with Chat and Pane tabs, each a self-polling fragment."""

from typing import Final

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from herdr_pwa import panetext
from herdr_pwa.engine import transcript
from herdr_pwa.engine.model import Agent, AgentStatus
from herdr_pwa.web.chat import chat_view
from herdr_pwa.web.deps import WebState, hidden_tick, viewer_guard
from herdr_pwa.web.herd import find_agent
from herdr_pwa.web.naming import page_title
from herdr_pwa.web.writing import first_note

TABS: Final = ("chat", "pane")
LIVE_CADENCE: Final = 3
QUIET_CADENCE: Final = 10


def _cadence(agent: Agent) -> int:
    # A screen that is changing or waiting on a person is worth watching closely.
    busy = agent.status in (AgentStatus.WORKING, AgentStatus.BLOCKED)
    return LIVE_CADENCE if busy else QUIET_CADENCE


def build(web: WebState) -> APIRouter:
    """The drill-in's routes."""
    router = APIRouter(prefix="/agent/{session}", dependencies=[Depends(viewer_guard(web))])

    @router.get("")
    def agent_page(  # pyright: ignore[reportUnusedFunction]
        session: str, tab: str = "chat", first: str = ""
    ) -> HTMLResponse:
        agent = find_agent(web, session)
        chosen = tab if tab in TABS else TABS[0]
        heading = page_title(agent.title, agent.agent, agent.target)
        context = {
            "agent": agent,
            "tab": chosen,
            "tabs": TABS,
            "heading": heading,
            "first": first_note(first),
        }
        return web.pages.page("agent_page.html", context)

    @router.get("/chat", dependencies=[Depends(hidden_tick)])
    def chat(session: str) -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        agent = find_agent(web, session)
        roots = web.settings.transcript_roots or None
        page = transcript.history(
            agent.session, agent_kind=agent.agent, limit=web.settings.chat_turns, root=roots
        )
        context = {"agent": agent, "chat": chat_view(page), "cadence": _cadence(agent)}
        return web.pages.fragment("chat.html", context)

    @router.get("/pane", dependencies=[Depends(hidden_tick)])
    def pane(session: str) -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        agent = find_agent(web, session)
        text = web.client.mirror_read(agent.target, status=agent.status)
        context = {"agent": agent, "view": panetext.pane_view(text), "cadence": _cadence(agent)}
        return web.pages.fragment("pane.html", context)

    return router
