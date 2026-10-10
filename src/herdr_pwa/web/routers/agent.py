"""The agent drill-in: a page with Chat and Pane tabs, each a fragment the event stream refreshes."""

from typing import Annotated, Final

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse

from herdr_pwa import panetext
from herdr_pwa.engine.tail import Ask, chat_window
from herdr_pwa.web.chat import chat_view
from herdr_pwa.web.deps import WebState, hidden_tick, viewer_guard
from herdr_pwa.web.herd import find_agent
from herdr_pwa.web.naming import page_title
from herdr_pwa.web.writing import first_note

TABS: Final = ("chat", "pane")


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
    def chat(  # pyright: ignore[reportUnusedFunction]
        session: str,
        start: Annotated[int | None, Query(ge=0)] = None,
        before: Annotated[int | None, Query(ge=0)] = None,
        log: Annotated[str, Query(max_length=200)] = "",
    ) -> HTMLResponse:
        # `start` keeps what the reader already has, earlier pages included; `before` asks for
        # one more page above it; neither is the newest page of messages.
        agent = find_agent(web, session)
        ask = Ask(messages=web.settings.chat_messages, start=start, before=before)
        page = chat_window(
            agent.session,
            agent_kind=agent.agent,
            ask=ask,
            tails=web.tails,
            log=log,
            root=web.settings.transcript_roots or None,
        )
        return web.pages.fragment("chat.html", {"agent": agent, "chat": chat_view(page)})

    @router.get("/pane", dependencies=[Depends(hidden_tick)])
    def pane(session: str) -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        agent = find_agent(web, session)
        text = web.client.mirror_read(agent.target, status=agent.status)
        context = {"agent": agent, "view": panetext.pane_view(text)}
        return web.pages.fragment("pane.html", context)

    return router
