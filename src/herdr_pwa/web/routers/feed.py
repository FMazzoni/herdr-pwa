"""The urgency-ranked feed: the shell page and its self-polling fragment."""

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from herdr_pwa.web.deps import WebState, hidden_tick, viewer_guard
from herdr_pwa.web.herd import load_herd
from herdr_pwa.web.naming import card_title


def build(web: WebState) -> APIRouter:
    """The feed's routes."""
    router = APIRouter(dependencies=[Depends(viewer_guard(web))])

    @router.get("/")
    def feed_page() -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        return web.pages.page("feed_page.html", {})

    @router.get("/feed", dependencies=[Depends(hidden_tick)])
    def feed_fragment() -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        herd = load_herd(web)
        return web.pages.fragment("feed.html", {"herd": herd.decision, "card_title": card_title})

    return router
