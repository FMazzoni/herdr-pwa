"""The installable shell at root scope: worker, manifest, home-screen icons, offline page."""

from typing import Final

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from herdr_pwa.web.assets import Assets
from herdr_pwa.web.deps import WebState, viewer_guard

# Fetched by a fixed URL with no stamp, so a long cache is a worker that can never be replaced.
SHELL_CACHE: Final = "no-cache"
WORKER: Final = "sw.js"
MANIFEST: Final = "manifest.webmanifest"
# The root names iOS probes for a home-screen icon, and the file that answers each.
TOUCH_ICONS: Final = {
    "/apple-touch-icon.png": "apple-touch-icon.png",
    "/apple-touch-icon-precomposed.png": "apple-touch-icon.png",
    "/apple-touch-icon-120x120.png": "apple-touch-icon-120x120.png",
    "/apple-touch-icon-120x120-precomposed.png": "apple-touch-icon-120x120.png",
}


def _root_scoped(assets: Assets, name: str, media_type: str) -> Response:
    asset = assets.files[name]
    headers = {"Cache-Control": SHELL_CACHE}
    if name == WORKER:
        # Makes the root scope explicit rather than implied by the path.
        headers["Service-Worker-Allowed"] = "/"
    return Response(asset.body, media_type=media_type, headers=headers)


def _touch_icon_route(router: APIRouter, assets: Assets, path: str, name: str) -> None:
    def icon() -> Response:
        return _root_scoped(assets, name, "image/png")

    router.add_api_route(path, icon, methods=["GET"], include_in_schema=False)


def build(web: WebState, assets: Assets) -> APIRouter:
    """The shell's routes, gated like everything else."""
    router = APIRouter(dependencies=[Depends(viewer_guard(web))])

    @router.get("/sw.js", include_in_schema=False)
    def worker() -> Response:  # pyright: ignore[reportUnusedFunction]
        return _root_scoped(assets, WORKER, "text/javascript")

    @router.get("/manifest.webmanifest", include_in_schema=False)
    def manifest() -> Response:  # pyright: ignore[reportUnusedFunction]
        return _root_scoped(assets, MANIFEST, "application/manifest+json")

    @router.get("/offline", include_in_schema=False)
    def offline() -> Response:  # pyright: ignore[reportUnusedFunction]
        # Rendered from the build stamp alone: no snapshot is in scope to hand it a row.
        page = web.pages.page("offline.html", {})
        page.headers["Cache-Control"] = SHELL_CACHE
        return page

    for path, name in TOUCH_ICONS.items():
        _touch_icon_route(router, assets, path, name)
    return router
