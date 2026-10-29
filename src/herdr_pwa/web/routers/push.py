"""Web Push: the alerts page, the VAPID key, device subscriptions, preferences and snooze."""

import asyncio
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import (
    BaseModel,
    StrictBool,
    StrictFloat,
    StrictStr,
    StringConstraints,
    ValidationError,
)

from herdr_pwa.notify_state import MAX_DND_SECONDS, InterruptStore
from herdr_pwa.push import DeviceLimitError, Subscription, SubscriptionError, SubscriptionStore
from herdr_pwa.validation import or_default, summary
from herdr_pwa.web.deps import WebState, viewer_guard
from herdr_pwa.web.herd import mark_read
from herdr_pwa.web.notifier import PushState
from herdr_pwa.web.writing import identity_of

# A PushSubscription is well under 1 KiB; anything far bigger is not one.
MAX_BODY_BYTES: Final = 8192
PUSH_OFF: Final = "push is switched off on this server (HP_PUSH_ENABLED=0)"
DEVICES_UNUSABLE: Final = "this server cannot use its list of devices"
SETTINGS_UNUSABLE: Final = "this server cannot read or write its alert settings"


class BadBodyError(Exception):
    """The request body is too big to be what the route needs."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail: str = detail


class _Endpoint(BaseModel):
    endpoint: Annotated[StrictStr, StringConstraints(min_length=1)]


class _Replaces(BaseModel):
    # The endpoint this page registered last; absent or not a string, nothing is swapped.
    replaces: Annotated[StrictStr | None, or_default(None)] = None


class _Kinds(BaseModel):
    # A key that was not sent, or is not a boolean, leaves that preference alone.
    blocked: Annotated[StrictBool | None, or_default(None)] = None
    done: Annotated[StrictBool | None, or_default(None)] = None


class _Snooze(BaseModel):
    seconds: StrictFloat


def _json(body: dict[str, object], status: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status)


def _off() -> JSONResponse:
    # Push off is a fact about this server, not a client error, and must not read as "denied".
    return _json({"enabled": False, "error": PUSH_OFF}, 503)


def _bad(detail: str) -> JSONResponse:
    return _json({"error": detail}, 400)


def _refused(exc: BadBodyError | SubscriptionError | OSError, unusable: str) -> JSONResponse:
    # Our own fixed words pass; any other error's text can carry the state dir's full path.
    if isinstance(exc, BadBodyError | DeviceLimitError):
        return _bad(exc.detail)
    return _bad(f"{unusable} ({type(exc).__name__})")


async def _body[M: BaseModel](request: Request, model: type[M]) -> M:
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise BadBodyError(f"body is over {MAX_BODY_BYTES} bytes")
    return model.model_validate_json(raw.decode("utf-8", errors="replace"))


def _devices(store: SubscriptionStore) -> int:
    try:
        return len(store.list_all())
    except (SubscriptionError, OSError):
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
    def alerts_page() -> HTMLResponse:
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
    def status() -> JSONResponse:
        return _off() if push is None else _json(_status(push))


# The stores fsync, so their reads and writes run off the event loop that serves every stream.


async def _subscribe(push: PushState, request: Request) -> JSONResponse:
    try:
        subscription = await _body(request, Subscription)
        replaces = (await _body(request, _Replaces)).replaces
        devices = push.stores.devices
        stored = await asyncio.to_thread(devices.add, subscription, replaces=replaces)
    except ValidationError as exc:
        return _bad(summary(exc))
    except (BadBodyError, SubscriptionError, OSError) as exc:
        return _refused(exc, DEVICES_UNUSABLE)
    devices = await asyncio.to_thread(_devices, push.stores.devices)
    # The echoed endpoint is the server's confirmation; only then does the page say "on".
    armed = push.notifier is not None
    return _json({"endpoint": stored.endpoint, "devices": devices, "armed": armed})


async def _unsubscribe(push: PushState, request: Request) -> JSONResponse:
    try:
        endpoint = (await _body(request, _Endpoint)).endpoint
        removed = await asyncio.to_thread(push.stores.devices.remove, endpoint)
    except ValidationError as exc:
        return _bad(summary(exc))
    except (BadBodyError, SubscriptionError, OSError) as exc:
        return _refused(exc, DEVICES_UNUSABLE)
    devices = await asyncio.to_thread(_devices, push.stores.devices)
    return _json({"removed": removed, "devices": devices})


async def _device(push: PushState, request: Request) -> JSONResponse:
    try:
        endpoint = (await _body(request, _Endpoint)).endpoint
        known = await asyncio.to_thread(push.stores.devices.holds, endpoint)
    except ValidationError as exc:
        return _bad(summary(exc))
    except (BadBodyError, SubscriptionError, OSError) as exc:
        return _refused(exc, DEVICES_UNUSABLE)
    devices = await asyncio.to_thread(_devices, push.stores.devices)
    notifier = push.notifier
    # No clear is ever pushed, so the page closes the herd alert itself once nothing is owed.
    owed = notifier is not None and notifier.herd_alert_current()
    # Yes or no for the endpoint the page sent, never the others.
    armed = notifier is not None
    return _json({"known": known, "devices": devices, "armed": armed, "herdAlert": owed})


async def _set_prefs(push: PushState, request: Request) -> JSONResponse:
    try:
        kinds = await _body(request, _Kinds)
        interrupts = push.stores.interrupts
        _ = await asyncio.to_thread(interrupts.set_kinds, blocked=kinds.blocked, done=kinds.done)
    except ValidationError as exc:
        return _bad(summary(exc))
    except (BadBodyError, OSError) as exc:
        return _refused(exc, SETTINGS_UNUSABLE)
    return _json(await asyncio.to_thread(_policy, push.stores.interrupts))


async def _snooze(push: PushState, request: Request) -> JSONResponse:
    try:
        seconds = (await _body(request, _Snooze)).seconds
        policy = await asyncio.to_thread(push.stores.interrupts.snooze, seconds)
    except ValidationError as exc:
        return _bad(summary(exc))
    except (BadBodyError, OSError) as exc:
        return _refused(exc, SETTINGS_UNUSABLE)
    if policy.dnd_until and push.notifier is not None:
        # It waits on the notifier's lock, which a tick holds while it sends, so off the loop.
        await asyncio.to_thread(push.notifier.retract)
    return _json(await asyncio.to_thread(_policy, push.stores.interrupts))


def _device_routes(router: APIRouter, push: PushState | None) -> None:
    @router.get("/key")
    def key() -> JSONResponse:
        if push is None:
            return _off()
        return _json({"enabled": True, "publicKey": push.stores.keys.public_key})

    @router.post("/subscribe")
    async def subscribe(request: Request) -> JSONResponse:
        return _off() if push is None else await _subscribe(push, request)

    @router.post("/unsubscribe")
    async def unsubscribe(request: Request) -> JSONResponse:
        return _off() if push is None else await _unsubscribe(push, request)

    # A POST, so the endpoint, which is itself a send capability, stays out of URLs and logs.
    @router.post("/device")
    async def device(request: Request) -> JSONResponse:
        return _off() if push is None else await _device(push, request)


def _preference_routes(router: APIRouter, web: WebState, push: PushState | None) -> None:
    @router.get("/prefs")
    def prefs() -> JSONResponse:
        return _off() if push is None else _json(_policy(push.stores.interrupts))

    @router.post("/prefs")
    async def set_prefs(request: Request) -> JSONResponse:
        return _off() if push is None else await _set_prefs(push, request)

    @router.post("/snooze")
    async def snooze(request: Request) -> JSONResponse:
        return _off() if push is None else await _snooze(push, request)

    @router.post("/seen/{session}")
    def seen(session: str) -> Response:
        # The loaded page says it was read; the GET that served it does not.
        if push is not None:
            mark_read(web, identity_of(session))
        return Response(status_code=204)


def build(web: WebState, push: PushState | None = None) -> APIRouter:
    """The push routes, behind the same identity gate as every other router."""
    router = APIRouter(prefix="/push", dependencies=[Depends(viewer_guard(web))])
    _page_routes(router, web, push)
    _device_routes(router, push)
    _preference_routes(router, web, push)
    return router
