"""The app factory: middleware, static files, the failure table and every router."""

from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Final

from fastapi import Depends, FastAPI
from fastapi.responses import Response
from starlette.middleware.gzip import GZipMiddleware

from herdr_pwa.audit import AuditLog
from herdr_pwa.engine import SnapshotCache
from herdr_pwa.settings import Settings
from herdr_pwa.web import failures
from herdr_pwa.web.assets import Assets
from herdr_pwa.web.deps import WebState, viewer_guard
from herdr_pwa.web.events import Hub, HubConfig
from herdr_pwa.web.gate import IdentityGate
from herdr_pwa.web.notifier import HerdSource, PushState, open_push
from herdr_pwa.web.render import TEMPLATES_DIR, Pages, read_templates
from herdr_pwa.web.routers import (
    agent,
    compose,
    devreport,
    events,
    feed,
    files,
    manage,
    push,
    shell,
    workspace,
)
from herdr_pwa.web.seams import Seams
from herdr_pwa.web.security import SecurityHeaders
from herdr_pwa.web.stalls import Stalls
from herdr_pwa.web.warm import warm_later

IMMUTABLE: Final = "public, max-age=31536000, immutable"
# Below this a gzip frame costs more than it saves.
GZIP_MIN_BYTES: Final = 500


def _static_route(app: FastAPI, web: WebState, assets: Assets) -> None:
    # App source is gated like any page: only /healthz answers a caller with no identity.
    @app.get("/static/{name}", include_in_schema=False, dependencies=[Depends(viewer_guard(web))])
    def static(name: str, v: str = "") -> Response:  # pyright: ignore[reportUnusedFunction]
        asset = assets.files.get(name)
        if asset is None:
            return Response("not found", status_code=404, media_type="text/plain")
        # Only the URL a template wrote, digest and all, may be cached forever.
        cache = IMMUTABLE if v == asset.digest else "no-cache"
        return Response(asset.body, media_type=asset.media_type, headers={"Cache-Control": cache})


@dataclass(frozen=True, slots=True)
class _Background:
    """What runs beside the requests: the event stream's hub and push, when it is on."""

    hub: Hub
    pushing: PushState | None


def _lifespan(
    background: _Background, web: WebState
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        if web.settings.prewarm:
            warm_later(web)
        state = background.pushing
        notifier = state.notifier if state is not None else None
        if notifier is not None:
            notifier.start()
        try:
            yield
        finally:
            await background.hub.close()
            if notifier is not None:
                await notifier.stop()
            if web.seen is not None:
                # What was read survives a restart even when no notifier runs to flush it.
                web.seen.flush()

    return lifespan


def create_app(settings: Settings, seams: Seams) -> FastAPI:
    """Build the web application around one runner client, one snapshot cache and push."""
    assets = Assets.load()
    pages = Pages(
        assets,
        read_templates(TEMPLATES_DIR),
        device_report=settings.device_report,
        fallback=settings.events_fallback,
    )
    web, pushing = _web_state(settings, seams, pages)
    background = _Background(Hub(web, _hub_config(settings)), pushing)
    app = FastAPI(
        docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan(background, web)
    )
    # The gate rigs reach in to drop every stream, to prove a page falls back and recovers.
    app.state.events = background.hub
    # And to its caches, to count what a page read from disk.
    app.state.web = web
    failures.install(app, pages)
    _routes(app, web, assets, background)
    _middleware(app, web, assets.build)
    return app


def _web_state(settings: Settings, seams: Seams, pages: Pages) -> tuple[WebState, PushState | None]:
    # The audit log, the runner client and its cache, and push, on the seams' clocks.
    audit = AuditLog(
        settings.state_dir,
        max_bytes=settings.audit_max_bytes,
        payload_chars=settings.audit_payload_chars,
        clock=seams.wall,
    )
    # A box that cannot record a write does not serve one.
    audit.preflight()
    client = seams.client(settings.engine())
    cache = SnapshotCache(ttl=settings.snapshot_cache_ttl, clock=seams.clock)
    stalls = Stalls(settings.transcript_roots or None)
    pushing = open_push(settings, HerdSource(client, cache, stalls), seams)
    web = WebState(
        settings=settings,
        client=client,
        cache=cache,
        pages=pages,
        audit=audit,
        stalls=stalls,
        clock=seams.clock,
        pacing=seams.pacing,
        wall=seams.wall,
        seen=pushing.stores.seen if pushing is not None else None,
    )
    return web, pushing


def _hub_config(settings: Settings) -> HubConfig:
    return HubConfig(
        interval=settings.events_interval,
        heartbeat=settings.events_heartbeat,
        max_clients=settings.events_max_clients,
        lifetime=settings.events_lifetime,
    )


def _routes(app: FastAPI, web: WebState, assets: Assets, background: _Background) -> None:
    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"status": "ok"}

    _static_route(app, web, assets)
    for build in (
        feed.build,
        agent.build,
        workspace.build,
        compose.build,
        manage.build,
        files.build,
    ):
        app.include_router(build(web))
    app.include_router(push.build(web, background.pushing))
    app.include_router(events.build(web, background.hub))
    app.include_router(shell.build(web, assets))
    # Off means the route does not exist at all, so a stray post is an ordinary 404.
    if web.settings.device_report:
        app.include_router(devreport.build(web))


def _middleware(app: FastAPI, web: WebState, build: str) -> None:
    app.add_middleware(GZipMiddleware, minimum_size=GZIP_MIN_BYTES)
    # Outside routing, so an unknown caller is refused before any route reads its body.
    app.add_middleware(IdentityGate, access=web.access(), pages=web.pages)
    # Added last, so it is outermost and stamps refusals and crashes too.
    app.add_middleware(SecurityHeaders, build=build)
