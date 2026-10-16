"""Writes to one agent: a prompt, a tapped key, an end; and the by-address door of a starting one."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from herdr_pwa import auth
from herdr_pwa.decisions import decide_screen
from herdr_pwa.engine import (
    INTERRUPT_KEY,
    Agent,
    AgentStatus,
    BlockedPrompt,
    EngineError,
    MenuShape,
    PaneId,
    SessionId,
    screen_digest,
)
from herdr_pwa.web.deps import WebState, viewer_guard
from herdr_pwa.web.failures import AgentGone, refused
from herdr_pwa.web.herd import find_agent
from herdr_pwa.web.writing import (
    FormBody,
    address_of,
    agent_url,
    expected,
    first_note,
    form,
    identity_of,
    opened,
    raise_if_gone,
    ticked,
    writer_for,
)
from herdr_pwa.writes.answer import KeyAsk, KeySent, send_key, send_starting_key
from herdr_pwa.writes.guard import asks_a_question
from herdr_pwa.writes.keys import PAD_KEYS, allowed_keys, answer_digits, control_token, pad_keys
from herdr_pwa.writes.lifecycle import EndAsk, end_agent
from herdr_pwa.writes.outcome import Evidence, Outcome
from herdr_pwa.writes.prompt import PromptAsk, PromptSent, send_first_prompt, send_prompt
from herdr_pwa.writes.refusal import Refused
from herdr_pwa.writes.resolve import Writer

ANSWER_CADENCE: Final = 5
DOOR_KEYS: Final = (*PAD_KEYS, INTERRUPT_KEY)


def _screen_now(web: WebState, agent: Agent) -> str:
    # A working agent's screen moves, and a question's is gone once it is answered from the keys
    # (the composer is not redrawn then), so neither is a screen the composer claims.
    if agent.status is AgentStatus.WORKING:
        return ""
    try:
        screen = web.client.read(agent.target, source="visible")
    except EngineError:
        return ""
    return "" if asks_a_question(screen) else screen_digest(screen)


def _question(web: WebState, agent: Agent) -> BlockedPrompt | None:
    if agent.status is not AgentStatus.BLOCKED:
        return None
    try:
        return web.client.blocked_prompt(agent.target, session=agent.session)
    except EngineError:
        return None


# Said, not drawn as a herdr state: the question went, and herdr has not yet said what replaced it.
UPDATING: Final = "updating"


def shown_status(agent: Agent, sent: KeySent | None = None) -> str:
    """The status word the agent page shows: herdr's, or "updating" over a question just answered."""
    answered = sent is not None and sent.evidence is Evidence.GONE
    return UPDATING if answered and agent.status is AgentStatus.BLOCKED else agent.status.value


def answer_context(agent: Agent, prompt: BlockedPrompt | None) -> dict[str, object]:
    """What the answer region draws; its digits come from the function the write checks with."""
    allowed = allowed_keys(prompt)
    decision = decide_screen(prompt) if prompt is not None else None
    hints = [h for h in decision.answer_hints if h.key in allowed] if decision else []
    hinted = {h.key for h in hints}
    return {
        "agent": agent,
        "decision": decision,
        "digits": answer_digits(prompt),
        "fingerprint": control_token(prompt) if prompt is not None else "",
        "hints": hints,
        "pad": [k for k in pad_keys(prompt) if k not in hinted],
        "interrupt": INTERRUPT_KEY,
        "cadence": ANSWER_CADENCE,
        "status": shown_status(agent),
        "about": _asked_about(prompt),
    }


def _asked_about(prompt: BlockedPrompt | None) -> tuple[str, ...]:
    # A buttons dialog asks about a command its heading does not name.
    if prompt is None or prompt.shape is not MenuShape.BUTTONS:
        return ()
    return prompt.context


def answer_region(web: WebState, agent: Agent) -> dict[str, object]:
    """The answer region as it stands now, for its own route and for the page that opens with it."""
    return answer_context(agent, _question(web, agent))


def composer_region(web: WebState, agent: Agent) -> dict[str, object]:
    """An empty composer that claims the screen it was drawn over."""
    return {"agent": agent, "screen": _screen_now(web, agent), "text": ""}


@dataclass(frozen=True, slots=True)
class Said:
    """The banner a sent key earns: its colour, the claim, the reason, and whether to go look."""

    kind: str
    what: str
    why: str
    unsure: bool = False


_LOOK: Final = "Look at the Pane before tapping again; nothing is sent twice."

# What watching the pane established, in words; {key} is the key as the user saw it.
_SAID: Final[dict[tuple[Outcome, Evidence], Said]] = {
    (Outcome.CONFIRMED, Evidence.GONE): Said(
        "sent", "Answered.", "You sent {key} and the question went away."
    ),
    (Outcome.CONFIRMED, Evidence.MOVED): Said(
        "sent", "Sent {key}.", "The screen moved on; what it shows now is below."
    ),
    (Outcome.UNCONFIRMED, Evidence.MOVED): Said(
        "unsure",
        "Sent {key}, and a different question is up.",
        "Read it before you answer it.",
        True,
    ),
    (Outcome.UNCONFIRMED, Evidence.UNMOVED): Said(
        "unsure", "Sent {key}, but the question is still showing.", _LOOK, True
    ),
    (Outcome.ACCEPTED, Evidence.UNMOVED): Said("sent", "Sent {key}.", "The question is still up."),
    (Outcome.ACCEPTED, Evidence.DELIVERED): Said(
        "sent", "Sent {key}.", "Nothing was waiting to watch."
    ),
}
_UNCERTAIN: Final = Said("unsure", "Sent {key}, unconfirmed.", _LOOK, True)


def key_name(key: str) -> str:
    """A key as the screen's own hints name it: Enter, Esc, Ctrl+C; a digit is its option."""
    if key.isdigit():
        return f"option {key}"
    return "+".join(part.capitalize() for part in key.split("-"))


def said(sent: KeySent) -> Said:
    """The words for what a sent key is known to have done, never more than was seen."""
    shape = _SAID.get((sent.outcome, sent.evidence), _UNCERTAIN)
    name = key_name(sent.key)
    return Said(shape.kind, shape.what.format(key=name), shape.why.format(key=name), shape.unsure)


def _lookup(web: WebState, session: SessionId) -> Agent | None:
    try:
        return find_agent(web, session)
    except AgentGone:
        return None


def _after(web: WebState, session: SessionId, prior: Agent | None) -> Agent:
    # A write that ended its agent (Ctrl+C, /exit) still says what it did; only nothing to draw is 404.
    agent = _lookup(web, session) or prior
    if agent is None:
        raise AgentGone(session)
    return agent


def _key(web: WebState, writer: Writer, session: SessionId, values: dict[str, str]) -> HTMLResponse:
    prior = _lookup(web, session)
    ask = KeyAsk(
        session,
        values.get("key", ""),
        expected_target=expected(values),
        drawn_from=values.get("screen", "").strip(),
        confirmed=ticked(values, "confirm"),
    )
    result = send_key(writer, ask)
    if isinstance(result, Refused):
        raise_if_gone(result)
        agent = _after(web, session, prior)
        context = {**answer_context(agent, result.prompt), "key": ask.key.strip().lower()}
        return refused(web.pages, "answer.html", context, result)
    return _answered(web, _after(web, session, prior), result)


def _fresh(web: WebState, agent: Agent) -> Agent:
    # Refreshes the shared snapshot too, so the poll this answer pokes reads the same herd.
    try:
        found = web.cache.get(web.client.snapshot, max_age=0).value.by_session(agent.session)
    except EngineError:
        return agent
    return found or agent


def _answered(web: WebState, agent: Agent, result: KeySent) -> HTMLResponse:
    # Controls come back drawn from the screen the key made, never the one it was aimed at.
    agent = _fresh(web, agent)
    context = {
        **answer_context(agent, result.prompt),
        "sent": result,
        "said": said(result),
        "status": shown_status(agent, result),
    }
    return web.pages.fragment("answer.html", context, headers={"HX-Trigger": "hp:poke"})


def _prompt(
    web: WebState, writer: Writer, session: SessionId, values: dict[str, str]
) -> HTMLResponse:
    prior = _lookup(web, session)
    ask = PromptAsk(
        session,
        values.get("text", ""),
        expected_target=expected(values),
        drawn_against=values.get("screen", "").strip(),
        confirmed=ticked(values, "confirm"),
        again=ticked(values, "again"),
    )
    result = send_prompt(writer, ask)
    if isinstance(result, Refused):
        raise_if_gone(result)
        context = {
            "agent": _after(web, session, prior),
            "text": ask.text,
            "screen": result.screen,
            # Kept through a later refusal, or a confirm after it would lose the word "again".
            "again": result.asks_again or ask.again,
        }
        return refused(web.pages, "compose.html", context, result)
    return _composed(web, _after(web, session, prior), result)


def _composed(web: WebState, agent: Agent, result: PromptSent) -> HTMLResponse:
    context = {"agent": agent, "text": "", "screen": "", "sent": result}
    return web.pages.fragment("compose.html", context, headers={"HX-Trigger": "hp:poke"})


def build(web: WebState) -> APIRouter:
    """The per-agent write routes; every one sits behind the identity and origin guard."""
    guard = viewer_guard(web)
    router = APIRouter(dependencies=[Depends(guard)])

    @router.get("/agent/{session}/compose")
    def composer(session: str) -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        agent = find_agent(web, identity_of(session))
        return web.pages.fragment("compose.html", composer_region(web, agent))

    @router.post("/agent/{session}/prompt")
    def prompt(  # pyright: ignore[reportUnusedFunction]
        session: str,
        request: Request,
        identity: Annotated[auth.Identity, Depends(guard)],
        body: FormBody = b"",
    ) -> HTMLResponse:
        writer = writer_for(web, request, identity)
        return _prompt(web, writer, identity_of(session), form(body))

    @router.get("/agent/{session}/answer")
    def answer(session: str) -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        agent = find_agent(web, identity_of(session))
        return web.pages.fragment("answer.html", answer_region(web, agent))

    @router.post("/agent/{session}/keys")
    def keys(  # pyright: ignore[reportUnusedFunction]
        session: str,
        request: Request,
        identity: Annotated[auth.Identity, Depends(guard)],
        body: FormBody = b"",
    ) -> HTMLResponse:
        writer = writer_for(web, request, identity)
        return _key(web, writer, identity_of(session), form(body))

    @router.get("/agent/{session}/close")
    def close_form(session: str) -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        agent = find_agent(web, identity_of(session))
        return web.pages.page("close.html", {"agent": agent})

    @router.post("/agent/{session}/close", response_model=None)
    def close(  # pyright: ignore[reportUnusedFunction]
        session: str,
        request: Request,
        identity: Annotated[auth.Identity, Depends(guard)],
        body: FormBody = b"",
    ) -> Response:
        writer = writer_for(web, request, identity)
        return _close(web, request, writer, identity_of(session), form(body))

    _starting_routes(router, web, guard)
    return router


def _close(
    web: WebState, request: Request, writer: Writer, session: SessionId, values: dict[str, str]
) -> Response:
    ask = EndAsk(
        session,
        expected_target=expected(values),
        confirmed=ticked(values, "confirm"),
        acknowledged=ticked(values, "acknowledge"),
    )
    result = end_agent(writer, ask)
    if isinstance(result, Refused):
        raise_if_gone(result)
        context = {"agent": find_agent(web, session)}
        return refused(web.pages, "close.html", context, result, whole=True)
    return opened(request, "/")


def _starting_routes(
    router: APIRouter, web: WebState, guard: Callable[[Request], auth.Identity]
) -> None:
    @router.get("/starting/{pane}", response_model=None)
    def starting(pane: str, first: str = "") -> Response:  # pyright: ignore[reportUnusedFunction]
        target = address_of(pane)
        agent = web.client.snapshot().by_target(target)
        if agent is not None and agent.session:
            return RedirectResponse(agent_url(agent.session), status_code=303)
        context = {**_door_context(target, agent), "first": first_note(first)}
        return web.pages.page("starting.html", context)

    @router.post("/starting/{pane}/prompt")
    def first_prompt(  # pyright: ignore[reportUnusedFunction]
        pane: str,
        request: Request,
        identity: Annotated[auth.Identity, Depends(guard)],
        body: FormBody = b"",
    ) -> HTMLResponse:
        values = form(body)
        target = address_of(pane)
        text = values.get("text", "")
        writer = writer_for(web, request, identity)
        result = send_first_prompt(
            writer,
            target,
            text,
            drawn_against=values.get("screen", "").strip(),
            confirmed=ticked(values, "confirm"),
        )
        return _door(web, target, text, result)

    @router.post("/starting/{pane}/keys")
    def first_key(  # pyright: ignore[reportUnusedFunction]
        pane: str,
        request: Request,
        identity: Annotated[auth.Identity, Depends(guard)],
        body: FormBody = b"",
    ) -> HTMLResponse:
        values = form(body)
        target = address_of(pane)
        key = values.get("key", "")
        writer = writer_for(web, request, identity)
        result = send_starting_key(writer, target, key, confirmed=ticked(values, "confirm"))
        return _door(web, target, "", result, key=key.strip().lower())


def _door_context(pane: PaneId, agent: Agent | None, text: str = "") -> dict[str, object]:
    return {"pane": pane, "agent": agent, "pad": DOOR_KEYS, "text": text}


def _door(
    web: WebState, pane: PaneId, text: str, result: PromptSent | KeySent | Refused, key: str = ""
) -> HTMLResponse:
    agent = web.client.snapshot().by_target(pane)
    if isinstance(result, Refused):
        # Which form the refusal answers, so a held interrupt never arms the prompt's confirm.
        context = {**_door_context(pane, agent, text), "key": key}
        return refused(web.pages, "starting.html", context, result, whole=True)
    return web.pages.page("starting.html", {**_door_context(pane, agent), "sent": result})
