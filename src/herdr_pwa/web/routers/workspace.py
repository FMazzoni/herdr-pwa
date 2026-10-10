"""A read-only listing of the runner's workspaces."""

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from herdr_pwa.web.deps import WebState, viewer_guard


def build(web: WebState) -> APIRouter:
    """The workspace routes; creating, renaming and closing are writes and live elsewhere."""
    router = APIRouter(prefix="/workspaces", dependencies=[Depends(viewer_guard(web))])

    @router.get("")
    def workspaces() -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        snapshot = web.snapshot().value
        counts = {w.workspace_id: 0 for w in snapshot.workspaces}
        for agent in snapshot.agents:
            counts[agent.workspace_id] = counts.get(agent.workspace_id, 0) + 1
        return web.pages.page(
            "workspaces.html", {"workspaces": snapshot.workspaces, "counts": counts}
        )

    return router
