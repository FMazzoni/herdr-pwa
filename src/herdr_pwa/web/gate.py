"""The identity gate in front of routing: refuse before any body is read, and bound what is."""

from typing import Final, cast

from fastapi import Request
from fastapi.responses import PlainTextResponse
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from herdr_pwa import auth
from herdr_pwa.web import failures
from herdr_pwa.web.render import Pages

# Answered with no identity at all: a liveness probe has none, and says nothing about the herd.
OPEN_PATHS: Final = frozenset({"/healthz"})
# A 4000-character prompt urlencodes to at most 48 KiB, and a keepalive device report is at
# most 64 KiB; push bodies keep their own tighter cap inside the route.
MAX_BODY_BYTES: Final = 64 * 1024
TOO_LARGE: Final = f"body is over {MAX_BODY_BYTES} bytes"


class BodyTooLarge(HTTPException):
    """A streamed body that outgrew the cap; FastAPI re-raises an HTTPException untouched."""

    def __init__(self) -> None:
        super().__init__(413, TOO_LARGE)


def _declared_too_large(request: Request) -> bool:
    declared = request.headers.get("content-length", "").strip()
    return declared.isdigit() and int(declared) > MAX_BODY_BYTES


def _bounded(receive: Receive) -> Receive:
    seen = 0

    async def bounded() -> Message:
        nonlocal seen
        message = await receive()
        if message["type"] == "http.request":
            # An ASGI http.request message carries its body as bytes.
            seen += len(cast("bytes", message.get("body", b"")))
            # Counted as it arrives, so a missing or lying Content-Length buys nothing.
            if seen > MAX_BODY_BYTES:
                raise BodyTooLarge
        return message

    return bounded


class IdentityGate:
    """Authenticate and origin-check every request but the open paths, then cap its body."""

    def __init__(self, app: ASGIApp, *, access: auth.Access, pages: Pages) -> None:
        self.app = app
        self.access = access
        self.pages = pages

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Answer a refusal here, so no router or body parser runs for an unknown caller."""
        if scope["type"] != "http" or scope["path"] in OPEN_PATHS:
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        try:
            auth.authorize(request.headers, access=self.access, method=request.method)
        except auth.AuthError as exc:
            await failures.respond(self.pages, request, exc)(scope, receive, send)
            return
        if _declared_too_large(request):
            await PlainTextResponse(TOO_LARGE, status_code=413)(scope, receive, send)
            return
        await self.app(scope, _bounded(receive), send)
