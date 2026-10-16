"""The app factory: middleware, static files, the failure table and every router."""

import time
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Final

from fastapi import Depends, FastAPI
from fastapi.responses import Response
from starlette.middleware.gzip import GZipMiddleware

from herdr_pwa.audit import AuditLog
from herdr_pwa.engine import HerdrClient, SnapshotCache
from herdr_pwa.push import Deliver
from herdr_pwa.settings import Settings
from herdr_pwa.web import failures
from herdr_pwa.web.assets import Assets
from herdr_pwa.web.deps import ClientFactory, WebState, viewer_guard
from herdr_pwa.web.events import Hub, HubConfig
from herdr_pwa.web.gate import IdentityGate
from herdr_pwa.web.notifier import PushState, open_push
from herdr_pwa.web.render import Pages
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
from herdr_pwa.web.security import SecurityHeaders
from herdr_pwa.web.warm import warm_later
from herdr_pwa.writes.outcome import Pacing

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


def _lifespan(
    state: PushState | None, hub: Hub, web: WebState
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        if web.settings.prewarm:
            warm_later(web)
        notifier = state.notifier if state is not None else None
        if notifier is not None:
            notifier.start()
        try:
            yield
        finally:
            await hub.close()
            if notifier is not None:
                await notifier.stop()

    return lifespan


def create_app(
    settings: Settings,
    client_factory: ClientFactory = HerdrClient,
    *,
    clock: Callable[[], float] = time.monotonic,
    deliver: Deliver | None = None,
    pacing: Pacing | None = None,
) -> FastAPI:
    """Build the web application around one runner client, one snapshot cache and push."""
    assets = Assets.load()
    pages = Pages(assets, device_report=settings.device_report, fallback=settings.events_fallback)
    audit = AuditLog(
        settings.state_dir,
        max_bytes=settings.audit_max_bytes,
        payload_chars=settings.audit_payload_chars,
    )
    # A box that cannot record a write does not serve one.
    audit.preflight()
    web = WebState(
        settings=settings,
        client=client_factory(settings.engine()),
        cache=SnapshotCache(ttl=settings.snapshot_cache_ttl, clock=clock),
        pages=pages,
        audit=audit,
        clock=clock,
        pacing=pacing or Pacing(),
    )
    pushing = open_push(
        settings, reader=web.client, cache=web.cache, deliver=deliver, clock=time.time
    )
    hub = Hub(
        web,
        HubConfig(
            interval=settings.events_interval,
            heartbeat=settings.events_heartbeat,
            max_clients=settings.events_max_clients,
            lifetime=settings.events_lifetime,
        ),
    )
    app = FastAPI(
        docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan(pushing, hub, web)
    )
    # The gate rigs reach in to drop every stream, to prove a page falls back and recovers.
    app.state.events = hub
    # And to its caches, to count what a page read from disk.
    app.state.web = web
    failures.install(app, pages)

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
    app.include_router(push.build(web, pushing))
    app.include_router(events.build(web, hub))
    app.include_router(shell.build(web, assets))
    # Off means the route does not exist at all, so a stray post is an ordinary 404.
    if settings.device_report:
        app.include_router(devreport.build(web))
    app.add_middleware(GZipMiddleware, minimum_size=GZIP_MIN_BYTES)
    # Outside routing, so an unknown caller is refused before any route reads its body.
    app.add_middleware(IdentityGate, access=web.access(), pages=pages)
    # Added last, so it is outermost and stamps refusals and crashes too.
    app.add_middleware(SecurityHeaders, build=assets.build)
    return app
