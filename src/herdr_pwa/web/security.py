"""The outermost middleware: one policy assigned on every response, refusals and crashes included."""

import logging
from typing import Final

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger(__name__)

# base-uri, form-action, frame-ancestors and object-src never fall back to default-src.
CSP: Final = "; ".join(
    (
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self'",
        "connect-src 'self'",
        "manifest-src 'self'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "object-src 'none'",
    )
)
NO_STORE: Final = "no-store"
# Safari keeps a no-store page out of the back-forward cache; a page that renders no agent
# state may revalidate instead, so a swipe back to it is instant rather than a blank reload.
ROWLESS: Final = "private, no-cache"
BUILD_HEADER: Final = "HP-Build"


class SecurityHeaders:
    """Assign CSP, nosniff, referrer policy and the build stamp; default to no-store."""

    def __init__(self, app: ASGIApp, *, build: str) -> None:
        self.app = app
        self.build = build

    def _stamp(self, message: Message) -> None:
        headers = MutableHeaders(scope=message)
        headers["Content-Security-Policy"] = CSP
        headers["X-Content-Type-Options"] = "nosniff"
        headers["Referrer-Policy"] = "same-origin"
        headers[BUILD_HEADER] = self.build
        # Agent content is never cached anywhere; only hashed static files and rowless pages opt out.
        if "cache-control" not in headers:
            headers["Cache-Control"] = NO_STORE

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Wrap ``send`` so every response start carries the headers."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def stamped(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                self._stamp(message)
            await send(message)

        try:
            await self.app(scope, receive, stamped)
        except Exception:
            log.exception("unhandled error serving a request")
            if started:
                raise
            await self._crash(stamped)

    @staticmethod
    async def _crash(send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 500,
                "headers": [(b"content-type", b"text/plain; charset=utf-8")],
            }
        )
        await send({"type": "http.response.body", "body": b"Internal error; see the server log."})
