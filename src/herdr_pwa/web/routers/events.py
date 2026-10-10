"""``GET /events``: a page's server-sent stream of which of its regions went stale."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse, Response, StreamingResponse

from herdr_pwa.web.deps import WebState, viewer_guard
from herdr_pwa.web.events import Hub, topics_of


def build(web: WebState, hub: Hub) -> APIRouter:
    """The event stream's one route, behind the same gate as every page."""
    router = APIRouter(dependencies=[Depends(viewer_guard(web))])

    @router.get("/events")
    async def events(  # pyright: ignore[reportUnusedFunction]
        topic: Annotated[list[str] | None, Query()] = None,
    ) -> Response:
        topics = topics_of(topic or ())
        if not topics:
            return PlainTextResponse("name at least one region to watch", status_code=400)
        listener = hub.join(topics)
        # No buffering anywhere on the way: gzip skips this type, and the header asks proxies.
        headers = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
        return StreamingResponse(
            hub.stream(listener), media_type="text/event-stream", headers=headers
        )

    return router
