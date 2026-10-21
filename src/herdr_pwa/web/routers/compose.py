"""Writes to one agent: a prompt, a tapped key, an end; and the by-address door of a starting one."""

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Annotated, Final
from urllib.parse import quote

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from herdr_pwa.decisions import (
    ScreenDecision,
    asks_now,
    decide_screen,
    page_status,
    still_talking,
)
from herdr_pwa.engine import (
    INTERRUPT_KEY,
    Agent,
    AgentStatus,
    BlockedPrompt,
    EngineError,
    PaneId,
    SessionId,
)
from herdr_pwa.web.asking import keys_for, wording
from herdr_pwa.web.deps import WebState, viewer_guard
from herdr_pwa.web.failures import AgentGone, Redraw, refused
from herdr_pwa.web.herd import agent_status, asks, find_agent, overlay_of, prompt_of
from herdr_pwa.web.writing import (
    Posted,
    WriteRoutes,
    address_of,
    agent_url,
    expected,
    first_note,
    identity_of,
    opened,
    raise_if_gone,
    ticked,
    with_first,
)
from herdr_pwa.writes.answer import (
    ESC_KEY,
    KeyAsk,
    KeySent,
    StartingKeyAsk,
    send_key,
    send_starting_key,
    stops_turn,
)
from herdr_pwa.writes.keys import (
    PAD_KEYS,
    allowed_keys,
    answer_digits,
    control_token,
    normalise_key,
    pad_keys,
)
from herdr_pwa.writes.lifecycle import EndAsk, end_agent
from herdr_pwa.writes.outcome import Evidence, Outcome
from herdr_pwa.writes.prompt import (
    FirstPromptAsk,
    PromptAsk,
    PromptSent,
    screen_claim,
    send_first_prompt,
    send_prompt,
)
from herdr_pwa.writes.refusal import Refusal, Refused

ANSWER_CADENCE: Final = 5
DOOR_KEYS: Final = (*PAD_KEYS, INTERRUPT_KEY)


def _screen_now(web: WebState, agent: Agent) -> str | None:
    if agent.status is AgentStatus.WORKING:
        return None
    try:
        return web.client.screen(agent.target)
    except EngineError:
        return None


def _claim(agent: Agent, screen: str | None) -> str:
    # A working agent's screen moves, and a question's is gone once it is answered from the keys
    # (the composer is not redrawn then), so neither is a screen the composer claims.
    if agent.status is AgentStatus.WORKING or screen is None:
        return ""
    return screen_claim(screen)


def _question(web: WebState, agent: Agent) -> BlockedPrompt | None:
    if not asks(web, agent):
        return None
    prompt = prompt_of(web, agent)
    # The same test the key's write makes, so no control is drawn that it would refuse.
    return prompt if prompt is not None and asks_now(agent.status, prompt) else None


def shown_status(web: WebState, agent: Agent, sent: KeySent | None = None) -> str:
    """The status word the agent page shows, with what the phone has read and any key just sent."""
    answered = sent is not None and sent.evidence is Evidence.GONE
    return page_status(agent, overlay_of(web, (agent,)), answered=answered)


def _keyed(agent: Agent, prompt: BlockedPrompt | None) -> bool:
    """Whether the keys belong with a question: one is asked, or the agent is stopped on one."""
    decision = decide_screen(prompt) if prompt is not None else None
    asked = decision is not None and bool(decision.question or decision.options)
    return asked or agent.status is AgentStatus.BLOCKED


def answer_context(agent: Agent, prompt: BlockedPrompt | None, status: str) -> dict[str, object]:
    """What the answer region draws; its digits come from the function the write checks with."""
    decision = decide_screen(prompt) if prompt is not None else None
    return {
        "agent": agent,
        "decision": decision,
        "wording": wording(prompt) if prompt is not None else None,
        "digits": answer_digits(prompt),
        "fingerprint": control_token(prompt) if prompt is not None else "",
        "keys": keys_for(prompt, decision),
        # Otherwise the pad and Interrupt are the status row's, above the composer.
        "keyed": _keyed(agent, prompt),
        "holds": _holds(decision),
        "interrupt": INTERRUPT_KEY,
        "cadence": ANSWER_CADENCE,
        "status": status,
    }


def _holds(decision: ScreenDecision | None) -> bool:
    # A question drawn above the composer: the prompt write refuses while the screen shows one.
    return decision is not None and bool(decision.question or decision.options)


def _held_over(agent: Agent, screen: str | None) -> bool:
    # The composer's own route has only its screen read: the question is parsed from that.
    if screen is None:
        return False
    prompt = BlockedPrompt.parse(screen)
    return asks_now(agent.status, prompt) and _holds(decide_screen(prompt))


def answer_region(web: WebState, agent: Agent) -> dict[str, object]:
    """The answer region as it stands now, for its own route and for the page that opens with it."""
    return answer_context(agent, _question(web, agent), shown_status(web, agent))


# The sheet's pad: the arrows and Enter first, then Esc set apart, since while the agent works
# Esc stops its turn.
_ROW_PAD: Final = ("up", "down", "enter", ESC_KEY)


def row_keys(web: WebState, agent: Agent) -> dict[str, object]:
    """The keys the status row offers: the pad while no question holds the keys, and Stop while
    the agent works; the same allowed set, and the same confirm, the write checks with."""
    prompt = _question(web, agent)
    keyed = _keyed(agent, prompt)
    offered = set(pad_keys(prompt)) & allowed_keys(prompt)
    return {
        "pad": [] if keyed else [k for k in _ROW_PAD if k in offered],
        "fingerprint": control_token(prompt) if prompt is not None else "",
        "interrupt": INTERRUPT_KEY,
        "stop": not keyed and still_talking(agent_status(web, agent)),
        "esc_arms": stops_turn(ESC_KEY, agent.status, asked=prompt is not None),
    }


def composer_region(agent: Agent, screen: str | None, *, held: bool) -> dict[str, object]:
    """An empty composer that claims *screen*, held while a question waits above it."""
    return {"agent": agent, "screen": _claim(agent, screen), "text": "", "held": held}


@dataclass(frozen=True, slots=True)
class Said:
    """The banner a sent key earns: its colour, the claim, the reason, and whether to go look."""

    kind: str
    what: str
    why: str
    unsure: bool = field(default=False, kw_only=True)


_LOOK: Final = "Look at the Pane before tapping again; nothing is sent twice."

# What watching the pane established, in words; {key} is the key as the user saw it.
_SAID: Final[MappingProxyType[tuple[Outcome, Evidence], Said]] = MappingProxyType(
    {
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
            unsure=True,
        ),
        (Outcome.UNCONFIRMED, Evidence.UNMOVED): Said(
            "unsure", "Sent {key}, but the question is still showing.", _LOOK, unsure=True
        ),
        (Outcome.ACCEPTED, Evidence.UNMOVED): Said(
            "sent", "Sent {key}.", "The question is still up."
        ),
        (Outcome.ACCEPTED, Evidence.DELIVERED): Said(
            "sent", "Sent {key}.", "Nothing was waiting to watch."
        ),
    }
)
_UNCERTAIN: Final = Said("unsure", "Sent {key}, unconfirmed.", _LOOK, unsure=True)


def key_name(key: str) -> str:
    """A key as the screen's own hints name it: Enter, Esc, Ctrl+C; a digit is its option."""
    if key.isdigit():
        return f"option {key}"
    return "+".join(part.capitalize() for part in key.split("-"))


def said(sent: KeySent) -> Said:
    """The words for what a sent key is known to have done, never more than was seen."""
    shape = _SAID.get((sent.outcome, sent.evidence), _UNCERTAIN)
    name = key_name(sent.key)
    return Said(
        shape.kind, shape.what.format(key=name), shape.why.format(key=name), unsure=shape.unsure
    )


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


def _key(session: SessionId, post: Posted) -> HTMLResponse:
    web = post.web
    prior = _lookup(web, session)
    values = post.values
    ask = KeyAsk(
        session,
        values.get("key", ""),
        expected_target=expected(values),
        drawn_from=values.get("screen", "").strip(),
        confirmed=ticked(values, "confirm"),
    )
    result = send_key(post.writer, ask)
    if isinstance(result, Refused):
        raise_if_gone(result)
        agent = _after(web, session, prior)
        shown = shown_status(web, agent)
        context = {**answer_context(agent, result.prompt, shown), "key": normalise_key(ask.key)}
        return refused(web.pages, Redraw("answer.html", context), result)
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
        **answer_context(agent, result.prompt, shown_status(web, agent, result)),
        "sent": result,
        "said": said(result),
    }
    return web.pages.fragment("answer.html", context, headers={"HX-Trigger": "hp:poke"})


def _prompt(session: SessionId, post: Posted) -> HTMLResponse:
    web = post.web
    prior = _lookup(web, session)
    values = post.values
    ask = PromptAsk(
        session,
        values.get("text", ""),
        expected_target=expected(values),
        drawn_against=values.get("screen", "").strip(),
        again=ticked(values, "again"),
    )
    result = send_prompt(post.writer, ask)
    if isinstance(result, Refused):
        raise_if_gone(result)
        context = {
            "agent": _after(web, session, prior),
            "text": ask.text,
            "screen": result.screen,
            # Kept through a later refusal, or the retry after it would lose the word "again".
            "again": result.asks_again or ask.again,
            "held": result.refusal is Refusal.QUESTION_SHOWING,
        }
        return refused(web.pages, Redraw("compose.html", context), result)
    return _composed(web, _after(web, session, prior), result)


def _composed(web: WebState, agent: Agent, result: PromptSent) -> HTMLResponse:
    context = {"agent": agent, "text": "", "screen": "", "sent": result}
    return web.pages.fragment("compose.html", context, headers={"HX-Trigger": "hp:poke"})


def _close(session: SessionId, post: Posted) -> Response:
    web, values = post.web, post.values
    ask = EndAsk(
        session,
        expected_target=expected(values),
        confirmed=ticked(values, "confirm"),
        acknowledged=ticked(values, "acknowledge"),
    )
    result = end_agent(post.writer, ask)
    if isinstance(result, Refused):
        raise_if_gone(result)
        context = {"agent": find_agent(web, session)}
        return refused(web.pages, Redraw("close.html", context, as_page=True), result)
    return opened(post.request, "/")


def build(web: WebState) -> APIRouter:
    """The per-agent write routes; every one sits behind the identity and origin guard."""
    guard = viewer_guard(web)
    router = APIRouter(dependencies=[Depends(guard)])
    writes = WriteRoutes.guarded_by(web, guard)
    _composer_routes(router, writes)
    _answer_routes(router, writes)
    _close_routes(router, writes)
    _door_page(router, web)
    _door_writes(router, writes)
    return router


def _composer_routes(router: APIRouter, writes: WriteRoutes) -> None:
    web = writes.web

    # Each reportUnusedFunction ignore marks a route: FastAPI registers it by decorator.
    @router.get("/agent/{session}/compose")
    def composer(session: str) -> HTMLResponse:  # pyright: ignore[reportUnusedFunction] - FastAPI registers the route by its decorator
        agent = find_agent(web, identity_of(session))
        screen = _screen_now(web, agent)
        held = _held_over(agent, screen)
        return web.pages.fragment("compose.html", composer_region(agent, screen, held=held))

    @router.post("/agent/{session}/prompt")
    def prompt(session: str, post: Annotated[Posted, Depends(writes.post)]) -> HTMLResponse:  # pyright: ignore[reportUnusedFunction] - FastAPI registers the route by its decorator
        return _prompt(identity_of(session), post)


def _answer_routes(router: APIRouter, writes: WriteRoutes) -> None:
    web = writes.web

    @router.get("/agent/{session}/answer")
    def answer(session: str) -> HTMLResponse:  # pyright: ignore[reportUnusedFunction] - FastAPI registers the route by its decorator
        agent = find_agent(web, identity_of(session))
        return web.pages.fragment("answer.html", answer_region(web, agent))

    @router.post("/agent/{session}/keys")
    def keys(session: str, post: Annotated[Posted, Depends(writes.post)]) -> HTMLResponse:  # pyright: ignore[reportUnusedFunction] - FastAPI registers the route by its decorator
        return _key(identity_of(session), post)


def _close_routes(router: APIRouter, writes: WriteRoutes) -> None:
    web = writes.web

    @router.get("/agent/{session}/close")
    def close_form(session: str) -> HTMLResponse:  # pyright: ignore[reportUnusedFunction] - FastAPI registers the route by its decorator
        agent = find_agent(web, identity_of(session))
        return web.pages.page("close.html", {"agent": agent})

    @router.post("/agent/{session}/close", response_model=None)
    def close(session: str, post: Annotated[Posted, Depends(writes.post)]) -> Response:  # pyright: ignore[reportUnusedFunction] - FastAPI registers the route by its decorator
        return _close(identity_of(session), post)


def _door_page(router: APIRouter, web: WebState) -> None:
    @router.get("/starting/{pane}", response_model=None)
    def starting(pane: str, first: str = "") -> Response:  # pyright: ignore[reportUnusedFunction] - FastAPI registers the route by its decorator
        target = address_of(pane)
        agent = web.client.snapshot().by_target(target)
        if agent is not None and agent.session:
            return RedirectResponse(with_first(agent_url(agent.session), first), status_code=303)
        context = {
            **_door_context(target, agent, first=first),
            "first": first_note(first, pane_tab=False),
        }
        return web.pages.page("starting.html", context)


def _door_writes(router: APIRouter, writes: WriteRoutes) -> None:
    web = writes.web

    @router.post("/starting/{pane}/prompt")
    def first_prompt(pane: str, post: Annotated[Posted, Depends(writes.post)]) -> HTMLResponse:  # pyright: ignore[reportUnusedFunction] - FastAPI registers the route by its decorator
        values = post.values
        target = address_of(pane)
        text = values.get("text", "")
        result = send_first_prompt(post.writer, FirstPromptAsk(target, text))
        return _door(web, target, _Knock(result, text=text))

    @router.post("/starting/{pane}/keys")
    def first_key(pane: str, post: Annotated[Posted, Depends(writes.post)]) -> HTMLResponse:  # pyright: ignore[reportUnusedFunction] - FastAPI registers the route by its decorator
        target = address_of(pane)
        key = post.values.get("key", "")
        ask = StartingKeyAsk(target, key, confirmed=ticked(post.values, "confirm"))
        result = send_starting_key(post.writer, ask)
        return _door(web, target, _Knock(result, key=normalise_key(key)))


def _door_context(
    pane: PaneId, agent: Agent | None, text: str = "", *, first: str = ""
) -> dict[str, object]:
    # Checking again keeps the first prompt's note, which the redirect then carries on.
    again = with_first(f"/starting/{quote(pane, safe='')}", first)
    return {"pane": pane, "agent": agent, "pad": DOOR_KEYS, "text": text, "again": again}


@dataclass(frozen=True, slots=True)
class _Knock:
    """What a write at the door came to, with the prompt or key it carried, kept for a refusal."""

    result: PromptSent | KeySent | Refused
    text: str = ""
    key: str = ""


def _door(web: WebState, pane: PaneId, knock: _Knock) -> HTMLResponse:
    agent = web.client.snapshot().by_target(pane)
    result = knock.result
    if isinstance(result, Refused):
        # Which key the refusal answers, so a held interrupt arms only that key's button.
        context = {**_door_context(pane, agent, knock.text), "key": knock.key}
        return refused(web.pages, Redraw("starting.html", context, as_page=True), result)
    return web.pages.page("starting.html", {**_door_context(pane, agent), "sent": result})
