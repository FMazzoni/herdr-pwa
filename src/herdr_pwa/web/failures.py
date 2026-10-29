"""Every failure the web layer can meet, mapped once to a status and an honest screen."""

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response

from herdr_pwa.auth import AuthError
from herdr_pwa.decisions import IDLE_CADENCE
from herdr_pwa.engine import (
    EngineError,
    EngineMisconfiguredError,
    EngineTimeoutError,
    EngineUnavailableError,
    InvalidRequestError,
    InvalidResponseError,
    ProtocolMismatchError,
    TargetNotFoundError,
)
from herdr_pwa.files.reader import NotReadableError, OutsideRootError
from herdr_pwa.files.roots import UnknownRootError
from herdr_pwa.patterns import Rx
from herdr_pwa.web.render import Pages
from herdr_pwa.writes.refusal import Refused

log = logging.getLogger(__name__)

# htmx stops an element's polling on 286; the client re-arms it when the page is shown.
STOP_POLLING: Final = 286


class AgentGoneError(Exception):
    """The session asked for is in no snapshot any more."""


class PageHiddenError(Exception):
    """A poll from a hidden page, answered by stopping its clock."""


@dataclass(frozen=True, slots=True)
class Failure:
    """How one kind of failure is answered."""

    status: int
    headline: str
    detail: str
    # A failure that may heal keeps polling; one that cannot, stops.
    retry: bool = field(default=False, kw_only=True)


# First match wins, so a subclass sits above its base.
FAILURES: Final[tuple[tuple[type[Exception], Failure], ...]] = (
    (PageHiddenError, Failure(STOP_POLLING, "", "")),
    (
        AuthError,
        Failure(403, "not allowed", "This request did not prove who is asking."),
    ),
    (
        AgentGoneError,
        Failure(404, "agent gone", "This agent is not in the herd any more."),
    ),
    (
        UnknownRootError,
        Failure(404, "no such repository", "No agent is working in that repository now."),
    ),
    (
        OutsideRootError,
        Failure(403, "outside the repository", "That path leads outside the repository."),
    ),
    (
        NotReadableError,
        Failure(404, "not found", "Nothing readable is at that path."),
    ),
    (
        TargetNotFoundError,
        Failure(
            404, "agent gone", "The agent's pane vanished while it was being read.", retry=True
        ),
    ),
    (
        ProtocolMismatchError,
        Failure(
            409,
            "herdr version drift",
            "herdr is speaking a version this build cannot safely drive.",
            retry=True,
        ),
    ),
    (
        InvalidRequestError,
        Failure(400, "bad address", "That is not an address this app can look up."),
    ),
    (
        EngineTimeoutError,
        Failure(503, "herdr too slow", "herdr did not answer in time.", retry=True),
    ),
    (
        EngineUnavailableError,
        Failure(
            503, "can\u2019t reach herdr", "This app is up, but herdr is not answering.", retry=True
        ),
    ),
    (
        InvalidResponseError,
        Failure(502, "herdr answered nonsense", "herdr's reply did not parse.", retry=True),
    ),
    (
        EngineMisconfiguredError,
        Failure(500, "misconfigured", "The settings for reaching herdr will not load."),
    ),
    (
        EngineError,
        Failure(502, "herdr error", "herdr refused the read.", retry=True),
    ),
)


def failure_for(exc: Exception) -> Failure:
    """The table row for ``exc``; anything unlisted is a bug and is not caught here."""
    for kind, failure in FAILURES:
        if isinstance(exc, kind):
            return failure
    raise exc


def _poll_url(request: Request) -> str:
    query = request.url.query
    return f"{request.url.path}?{query}" if query else request.url.path


# The ids this app gives a region that redraws itself: lower-case words joined by hyphens.
_REGION_ID: Final = re.compile(r"[a-z]+(?:-[a-z]+)*")


def _stand_in(request: Request) -> str:
    # The failure keeps the id htmx names, so the page still finds the region it replaced.
    target = request.headers.get("HX-Target", "")
    return target if _REGION_ID.fullmatch(target) else ""


def _status_and_detail(exc: Exception, failure: Failure) -> tuple[int, str]:
    # The auth library words its refusals for the caller; runner text stays in the log.
    if isinstance(exc, AuthError):
        return exc.status_code, str(exc)
    return failure.status, failure.detail


def respond(pages: Pages, request: Request, exc: Exception) -> Response:
    """The answer for ``exc``: a fragment for htmx, a whole page otherwise."""
    failure = failure_for(exc)
    if failure.status == STOP_POLLING:
        return Response(status_code=STOP_POLLING, headers={"HX-Reswap": "none"})
    _log(request, exc)
    status, detail = _status_and_detail(exc, failure)
    retry = failure.retry and request.method == "GET"
    context = {
        "failure": failure,
        "detail": detail,
        "poll_url": _poll_url(request) if retry else "",
        "cadence": IDLE_CADENCE,
        "region": _stand_in(request),
    }
    if request.headers.get("HX-Request"):
        return pages.fragment("failure.html", context, status=status)
    return pages.page("failure_page.html", context, status=status)


def _log(request: Request, exc: Exception) -> None:
    # Route template and class alone: a URL or an error's text can name a path, session or secret.
    route = getattr(request.scope.get("route"), "path", "?")
    log.warning("%s %s: %s", request.method, route, type(exc).__name__)


def install(app: FastAPI, pages: Pages) -> None:
    """Register one handler for every row of the table."""

    async def handle(request: Request, exc: Exception) -> Response:
        return respond(pages, request, exc)

    for kind, _ in FAILURES:
        app.add_exception_handler(kind, handle)


@dataclass(frozen=True, slots=True)
class Redraw:
    """The template a refused write is drawn back into, what it draws, and whether as a page."""

    name: str
    context: Mapping[str, object]
    as_page: bool = False


def refused(pages: Pages, redraw: Redraw, result: Refused) -> HTMLResponse:
    """A write the library declined, at the status the write library's one table assigns."""
    log.info("write refused: %s %s", result.refusal.value, result.cause)
    shown = {**redraw.context, "refused": result, "says": result.says}
    render = pages.page if redraw.as_page else pages.fragment
    return render(redraw.name, shown, status=result.status)


PATTERNS: Final = (
    Rx(
        _REGION_ID,
        ("answer", "agent-dot"),
        ("Answer", "a-", "-a", "a--b", "a_b"),
        "fullmatch",
        validator=True,
    ),
)
