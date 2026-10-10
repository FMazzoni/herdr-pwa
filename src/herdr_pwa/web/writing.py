"""What every write route shares: the form, the ids in the URL, and a writer that audits who asks."""

import json
from typing import Annotated, Final
from urllib.parse import parse_qs, quote

from fastapi import Body, Request
from fastapi.responses import RedirectResponse, Response

from herdr_pwa import auth
from herdr_pwa.audit import Origin, Recorder
from herdr_pwa.engine.model import PaneId, SessionId, as_pane_id, as_session_id
from herdr_pwa.engine.transport import InvalidRequest
from herdr_pwa.web.deps import WebState
from herdr_pwa.web.failures import AgentGone
from herdr_pwa.writes.refusal import Refusal, Refused
from herdr_pwa.writes.resolve import Writer

FORM: Final = "application/x-www-form-urlencoded"
# Names the page a finished write opens; app.js answers it with location.replace.
OPENED_EVENT: Final = "hp:opened"

type FormBody = Annotated[bytes, Body(media_type=FORM)]


def form(body: bytes) -> dict[str, str]:
    """An urlencoded body as ``{field: first value}``."""
    fields = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    return {name: values[0] if values else "" for name, values in fields.items()}


def ticked(values: dict[str, str], name: str) -> bool:
    """Was this confirm or flag posted as set?"""
    return values.get(name, "").strip() == "1"


def identity_of(session: str) -> SessionId:
    """A URL's session segment as an identity; an address posing as one is a bad request."""
    try:
        return as_session_id(session)
    except ValueError as exc:
        raise InvalidRequest(str(exc)) from exc


def address_of(pane: str) -> PaneId:
    """A URL's pane segment as an address; an identity posing as one is a bad request."""
    try:
        return as_pane_id(pane)
    except ValueError as exc:
        raise InvalidRequest(str(exc)) from exc


def expected(values: dict[str, str]) -> PaneId | None:
    """The pane the form was drawn against, if it said one."""
    target = values.get("target", "").strip()
    return address_of(target) if target else None


def writer_for(web: WebState, request: Request, identity: auth.Identity) -> Writer:
    """This request's writer: the shared client and an audit trail naming who asks."""
    origin = Origin.of(
        identity,
        client_ip=request.client.host if request.client else "",
        forwarded_for=request.headers.get("x-forwarded-for", ""),
    )
    return Writer(web.client, Recorder(web.audit, origin), web.pacing)


def agent_url(session: str) -> str:
    """Where one agent's page lives."""
    return f"/agent/{quote(session, safe='')}"


def raise_if_gone(result: Refused) -> None:
    """A write refused because its agent is gone is answered by the read table's 404."""
    if result.refusal is Refusal.GONE:
        raise AgentGone(result.detail)


def opened(request: Request, where: str) -> Response:
    """Send a finished write on to ``where`` without leaving the spent form in history."""
    if request.headers.get("HX-Request"):
        # htmx's HX-Redirect is location.href, a push; only the page can replace its own entry.
        trigger = json.dumps({OPENED_EVENT: {"where": where}})
        return Response(status_code=200, headers={"HX-Trigger": trigger, "HX-Reswap": "none"})
    # Without script the form's POST already pushed; the 303 at least lands on a GET.
    return RedirectResponse(where, status_code=303)
