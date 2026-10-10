"""Web Push: the alerts page, the VAPID key, device subscriptions, preferences and snooze."""

import asyncio
import json
from typing import Final

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from herdr_pwa import jsonio
from herdr_pwa.notify_state import MAX_DND_SECONDS, InterruptStore
from herdr_pwa.push import SubscriptionError, SubscriptionStore
from herdr_pwa.statedir import StateError
from herdr_pwa.web.deps import WebState, viewer_guard
from herdr_pwa.web.notifier import PushState

# A PushSubscription is well under 1 KiB; anything far bigger is not one.
MAX_BODY_BYTES: Final = 8192
PUSH_OFF: Final = "push is switched off on this server (HP_PUSH_ENABLED=0)"


class BadBody(Exception):  # noqa: N818 - named for the answer it becomes
    """The request body is not the JSON object the route needs."""


def _json(body: dict[str, object], status: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status)


def _off() -> JSONResponse:
    # Push off is a fact about this server, not a client error, and must not read as "denied".
    return _json({"enabled": False, "error": PUSH_OFF}, 503)


def _bad(detail: str) -> JSONResponse:
    return _json({"error": detail}, 400)


async def _object(request: Request) -> dict[str, object]:
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise BadBody(f"body is over {MAX_BODY_BYTES} bytes")
    try:
        parsed = jsonio.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise BadBody(f"body is not JSON: {exc}") from exc
    document = jsonio.as_dict(parsed)
    if document is None:
        raise BadBody("expected a JSON object")
    return document


def _devices(store: SubscriptionStore) -> int:
    try:
        return len(store.list_all())
    except SubscriptionError:
        return 0


def _policy(store: InterruptStore) -> dict[str, object]:
    policy, now = store.current()
    return {
        "enabled": True,
        "blocked": policy.blocked,
        "done": policy.done,
        "dndUntil": policy.dnd_until,
        "snoozed": policy.snoozed(now),
        "now": now,
        "maxSnooze": MAX_DND_SECONDS,
    }


def _tristate(value: object) -> bool | None:
    # A key that was not sent leaves that preference alone.
    return value if isinstance(value, bool) else None


def _loop_status(push: PushState) -> dict[str, object]:
    notifier = push.notifier
    if notifier is None:
        return {"armed": False, "running": False, "ticks": 0, "readError": ""}
    loop = notifier.state
    return {
        "armed": True,
        "running": loop.running,
        "ticks": loop.ticks,
        "readError": loop.read_error,
    }


def _status(push: PushState) -> dict[str, object]:
    record = push.stores.health.load()
    delivery: dict[str, object] = {}
    if record is not None:
        delivery = {
            "sent": record.sent,
            "failed": record.failed,
            "pruned": record.pruned,
            "lastError": record.last_error,
            "neverDelivered": record.never_delivered,
        }
    return {
        "enabled": True,
        "reason": push.reason,
        "devices": _devices(push.stores.devices),
        **_loop_status(push),
        "delivery": delivery,
    }


def _page_routes(router: APIRouter, web: WebState, push: PushState | None) -> None:
    @router.get("")
    def alerts_page() -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        if push is None:
            return web.pages.page("push_page.html", {"push": None, "reason": PUSH_OFF})
        policy, now = push.stores.interrupts.current()
        context: dict[str, object] = {
            "push": push,
            "reason": push.reason,
            "policy": policy,
            "snoozed": policy.snoozed(now),
            "devices": _devices(push.stores.devices),
        }
        return web.pages.page("push_page.html", context)

    @router.get("/status")
    def status() -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        return _off() if push is None else _json(_status(push))


async def _subscribe(push: PushState, request: Request) -> JSONResponse:
    try:
        stored = push.stores.devices.add(await _object(request))
    except (BadBody, SubscriptionError) as exc:
        return _bad(str(exc))
    devices = _devices(push.stores.devices)
    # The echoed endpoint is the server's confirmation; only then does the page say "on".
    armed = push.notifier is not None
    return _json({"endpoint": stored.endpoint, "devices": devices, "armed": armed})


async def _unsubscribe(push: PushState, request: Request) -> JSONResponse:
    try:
        endpoint = jsonio.as_str((await _object(request)).get("endpoint"))
        if not endpoint:
            raise BadBody("endpoint must be a non-empty string")  # noqa: TRY301 - one answer
        removed = push.stores.devices.remove(endpoint)
    except (BadBody, SubscriptionError) as exc:
        return _bad(str(exc))
    return _json({"removed": removed, "devices": _devices(push.stores.devices)})


async def _set_prefs(push: PushState, request: Request) -> JSONResponse:
    try:
        body = await _object(request)
        _ = push.stores.interrupts.set_kinds(
            blocked=_tristate(body.get("blocked")), done=_tristate(body.get("done"))
        )
    except (BadBody, StateError, OSError) as exc:
        return _bad(str(exc))
    return _json(_policy(push.stores.interrupts))


async def _snooze(push: PushState, request: Request) -> JSONResponse:
    try:
        seconds = jsonio.as_float((await _object(request)).get("seconds"))
        if seconds is None:
            raise BadBody("'seconds' must be a number")  # noqa: TRY301 - one answer
        policy = push.stores.interrupts.snooze(seconds)
    except (BadBody, StateError, OSError) as exc:
        return _bad(str(exc))
    if policy.dnd_until and push.notifier is not None:
        # The clear goes out over the network, so it runs off the event loop.
        _ = await asyncio.to_thread(push.notifier.retract)
    return _json(_policy(push.stores.interrupts))


def _device_routes(router: APIRouter, push: PushState | None) -> None:
    @router.get("/key")
    def key() -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        if push is None:
            return _off()
        return _json({"enabled": True, "publicKey": push.stores.keys.public_key})

    @router.post("/subscribe")
    async def subscribe(request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        return _off() if push is None else await _subscribe(push, request)

    @router.post("/unsubscribe")
    async def unsubscribe(request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        return _off() if push is None else await _unsubscribe(push, request)


def _preference_routes(router: APIRouter, push: PushState | None) -> None:
    @router.get("/prefs")
    def prefs() -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        return _off() if push is None else _json(_policy(push.stores.interrupts))

    @router.post("/prefs")
    async def set_prefs(request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        return _off() if push is None else await _set_prefs(push, request)

    @router.post("/snooze")
    async def snooze(request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        return _off() if push is None else await _snooze(push, request)

    @router.post("/seen/{session}")
    def seen(session: str) -> Response:  # pyright: ignore[reportUnusedFunction]
        # The loaded page says it was read; the GET that served it does not.
        if push is not None:
            push.stores.seen.mark_seen(session)
        return Response(status_code=204)


def build(web: WebState, push: PushState | None = None) -> APIRouter:
    """The push routes, behind the same identity gate as every other router."""
    router = APIRouter(prefix="/push", dependencies=[Depends(viewer_guard(web))])
    _page_routes(router, web, push)
    _device_routes(router, push)
    _preference_routes(router, push)
    return router
