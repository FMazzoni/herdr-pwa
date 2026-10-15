"""Writes that shape the herd: starting an agent, and making, renaming or closing a workspace."""

from collections.abc import Callable
from typing import Annotated
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from herdr_pwa import auth
from herdr_pwa.engine.model import Snapshot, Workspace
from herdr_pwa.web.deps import WebState, viewer_guard
from herdr_pwa.web.failures import refused
from herdr_pwa.web.writing import (
    FormBody,
    agent_url,
    first_code,
    form,
    opened,
    ticked,
    writer_for,
)
from herdr_pwa.writes.lifecycle import (
    HARNESSES,
    AgentStarted,
    StartRequest,
    offered_kinds,
    start_agent,
)
from herdr_pwa.writes.refusal import Refusal, Refused
from herdr_pwa.writes.workspaces import (
    WorkspaceCloseAsk,
    close_workspace,
    cost_of,
    create_workspace,
    rename_workspace,
)


def _new_context(snapshot: Snapshot, values: dict[str, str]) -> dict[str, object]:
    kinds = offered_kinds()
    focused = next((w.workspace_id for w in snapshot.workspaces if w.focused), "")
    return {
        "kinds": kinds,
        "models": {kind: HARNESSES[kind].models for kind in kinds},
        "workspaces": snapshot.workspaces,
        "cwds": sorted({a.cwd for a in snapshot.agents if a.cwd}),
        "values": {"workspace": focused, "kind": kinds[0] if kinds else "", **values},
    }


def _started(request: Request, result: AgentStarted) -> Response:
    # A late-identity harness has no session yet, so its page is the door at its address.
    where = agent_url(result.session) if result.session else f"/starting/{quote(result.pane)}"
    code = first_code(result.first)
    return opened(request, f"{where}?{urlencode({'first': code})}" if code else where)


def _workspace_back(workspace_id: str) -> str:
    return f"/workspaces#{quote(workspace_id)}"


def build(web: WebState) -> APIRouter:
    """The herd-shaping routes; every one sits behind the identity and origin guard."""
    guard = viewer_guard(web)
    router = APIRouter(dependencies=[Depends(guard)])

    @router.get("/new")
    def new_form() -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        return web.pages.page("new.html", _new_context(web.client.snapshot(), {}))

    @router.post("/new", response_model=None)
    def new(  # pyright: ignore[reportUnusedFunction]
        request: Request, identity: Annotated[auth.Identity, Depends(guard)], body: FormBody = b""
    ) -> Response:
        values = form(body)
        ask = StartRequest(
            name=values.get("name", ""),
            kind=values.get("kind", ""),
            workspace_id=values.get("workspace", ""),
            cwd=values.get("cwd", ""),
            model=values.get("model", "").strip(),
            prompt=values.get("prompt", ""),
            confirmed=ticked(values, "confirm"),
        )
        result = start_agent(writer_for(web, request, identity), ask)
        if isinstance(result, Refused):
            context = _new_context(web.client.snapshot(), values)
            return refused(web.pages, "new.html", context, result, whole=True)
        return _started(request, result)

    _workspace_routes(router, web)
    _close_routes(router, web, guard)
    return router


def _find(snapshot: Snapshot, workspace_id: str) -> Workspace | None:
    return next((w for w in snapshot.workspaces if w.workspace_id == workspace_id), None)


def _no_workspace(web: WebState, name: str, verb: str) -> HTMLResponse:
    context: dict[str, object] = {"workspace": None, "label": "", "cost": None, "armed": False}
    return refused(web.pages, name, context, Refused(Refusal.NO_WORKSPACE, verb), whole=True)


def _workspace_routes(router: APIRouter, web: WebState) -> None:
    # Create and rename are reversible and not in the audit log's endpoints, so they carry no trail.
    @router.get("/workspaces/new")
    def workspace_form() -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        return web.pages.page("workspace_new.html", {"label": ""})

    @router.post("/workspaces/new", response_model=None)
    def workspace_new(request: Request, body: FormBody = b"") -> Response:  # pyright: ignore[reportUnusedFunction]
        label = form(body).get("label", "")
        result = create_workspace(web.client, label)
        if isinstance(result, Refused):
            return refused(web.pages, "workspace_new.html", {"label": label}, result, whole=True)
        return opened(request, _workspace_back(result.workspace_id))

    @router.get("/workspaces/{workspace_id}/rename")
    def rename_form(workspace_id: str) -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        found = _find(web.client.snapshot(), workspace_id)
        if found is None:
            return _no_workspace(web, "workspace_rename.html", "renamed")
        return web.pages.page("workspace_rename.html", {"workspace": found, "label": found.label})

    @router.post("/workspaces/{workspace_id}/rename", response_model=None)
    def rename(workspace_id: str, request: Request, body: FormBody = b"") -> Response:  # pyright: ignore[reportUnusedFunction]
        label = form(body).get("label", "")
        result = rename_workspace(web.client, workspace_id, label)
        if isinstance(result, Refused):
            found = _find(web.client.snapshot(), workspace_id)
            context = {"workspace": found, "label": label}
            return refused(web.pages, "workspace_rename.html", context, result, whole=True)
        return opened(request, _workspace_back(workspace_id))


def _close_context(web: WebState, workspace_id: str, *, armed: bool) -> dict[str, object]:
    snapshot = web.client.snapshot()
    found = _find(snapshot, workspace_id)
    return {"cost": cost_of(found, snapshot) if found is not None else None, "armed": armed}


def _close_routes(
    router: APIRouter, web: WebState, guard: Callable[[Request], auth.Identity]
) -> None:
    @router.get("/workspaces/{workspace_id}/close")
    def close_form(workspace_id: str) -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        context = _close_context(web, workspace_id, armed=False)
        if context["cost"] is None:
            return _no_workspace(web, "workspace_close.html", "closed")
        return web.pages.page("workspace_close.html", context)

    @router.post("/workspaces/{workspace_id}/close", response_model=None)
    def close(  # pyright: ignore[reportUnusedFunction]
        workspace_id: str,
        request: Request,
        identity: Annotated[auth.Identity, Depends(guard)],
        body: FormBody = b"",
    ) -> Response:
        values = form(body)
        ask = WorkspaceCloseAsk(
            workspace_id,
            confirmed=ticked(values, "confirm"),
            armed=ticked(values, "armed"),
            saw=values.get("saw", "").strip(),
        )
        result = close_workspace(writer_for(web, request, identity), ask)
        if isinstance(result, Refused):
            # The second step is armed once the first confirm has been given against a cost.
            armed = ask.confirmed and result.refusal in (Refusal.CONFIRM, Refusal.COUNT_CHANGED)
            context = _close_context(web, workspace_id, armed=armed)
            return refused(web.pages, "workspace_close.html", context, result, whole=True)
        return opened(request, "/workspaces")
