"""The app factory: middleware, static files, the failure table and every router."""

import time
from collections.abc import Callable
from typing import Final

from fastapi import FastAPI
from fastapi.responses import Response
from starlette.middleware.gzip import GZipMiddleware

from herdr_pwa.engine.cache import SnapshotCache
from herdr_pwa.engine.client import HerdrClient
from herdr_pwa.settings import Settings
from herdr_pwa.web import failures
from herdr_pwa.web.assets import Assets
from herdr_pwa.web.deps import ClientFactory, WebState
from herdr_pwa.web.render import Pages
from herdr_pwa.web.routers import agent, compose, feed, push, workspace
from herdr_pwa.web.security import SecurityHeaders

IMMUTABLE: Final = "public, max-age=31536000, immutable"
# Below this a gzip frame costs more than it saves.
GZIP_MIN_BYTES: Final = 500


def _static_route(app: FastAPI, assets: Assets) -> None:
    @app.get("/static/{name}", include_in_schema=False)
    def static(name: str, v: str = "") -> Response:  # pyright: ignore[reportUnusedFunction]
        asset = assets.files.get(name)
        if asset is None:
            return Response("not found", status_code=404, media_type="text/plain")
        # Only the URL a template wrote, digest and all, may be cached forever.
        cache = IMMUTABLE if v == asset.digest else "no-cache"
        return Response(asset.body, media_type=asset.media_type, headers={"Cache-Control": cache})


def create_app(
    settings: Settings,
    client_factory: ClientFactory = HerdrClient,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> FastAPI:
    """Build the web application around one runner client and one snapshot cache."""
    assets = Assets.load()
    pages = Pages(assets)
    web = WebState(
        settings=settings,
        client=client_factory(settings.engine()),
        cache=SnapshotCache(ttl=settings.snapshot_cache_ttl, clock=clock),
        pages=pages,
        clock=clock,
    )
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    failures.install(app, pages)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"status": "ok"}

    _static_route(app, assets)
    for build in (feed.build, agent.build, workspace.build, compose.build, push.build):
        app.include_router(build(web))
    app.add_middleware(GZipMiddleware, minimum_size=GZIP_MIN_BYTES)
    # Added last, so it is outermost and stamps refusals and crashes too.
    app.add_middleware(SecurityHeaders, build=assets.build)
    return app
