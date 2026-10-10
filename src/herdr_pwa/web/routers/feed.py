"""The urgency-ranked feed: the shell page and its self-polling fragment."""

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from herdr_pwa.decisions import feed_view
from herdr_pwa.web.deps import WebState, hidden_tick, viewer_guard
from herdr_pwa.web.herd import load_herd
from herdr_pwa.web.naming import card_question, card_title
from herdr_pwa.web.security import ROWLESS


def build(web: WebState) -> APIRouter:
    """The feed's routes."""
    router = APIRouter(dependencies=[Depends(viewer_guard(web))])

    @router.get("/")
    def feed_page(ws: str = "") -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        # The filter is the address the user tapped, not agent state, so the shell stays rowless.
        page = web.pages.page("feed_page.html", {"ws": ws})
        # The shell holds no row (the fragment does), so it may sit in the back-forward cache.
        page.headers["Cache-Control"] = ROWLESS
        return page

    @router.get("/feed", dependencies=[Depends(hidden_tick)])
    def feed_fragment(ws: str = "") -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        herd = load_herd(web)
        view = feed_view(herd.decision, ws)
        context = {
            "herd": herd.decision,
            "view": view,
            "card_title": card_title,
            "card_question": card_question,
        }
        return web.pages.fragment("feed.html", context)

    return router
