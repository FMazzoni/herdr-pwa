"""Writes to one agent: a prompt, a tapped key, an end; and the by-address door of a starting one."""

from dataclasses import dataclass
from typing import Annotated, Final

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from herdr_pwa.decisions import decide_screen, page_status
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
from herdr_pwa.web.failures import AgentGone, Redraw, refused
from herdr_pwa.web.herd import find_agent, unread_of
from herdr_pwa.web.writing import (
    Posted,
    Posting,
    address_of,
    agent_url,
    expected,
    first_note,
    identity_of,
    opened,
    posted,
    raise_if_gone,
    ticked,
)
from herdr_pwa.writes.answer import KeyAsk, KeySent, send_key, send_starting_key
from herdr_pwa.writes.guard import asks_a_question
from herdr_pwa.writes.keys import PAD_KEYS, allowed_keys, answer_digits, control_token, pad_keys
from herdr_pwa.writes.lifecycle import EndAsk, end_agent
from herdr_pwa.writes.outcome import Evidence, Outcome
from herdr_pwa.writes.prompt import PromptAsk, PromptSent, send_first_prompt, send_prompt
from herdr_pwa.writes.refusal import Refused

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


def shown_status(web: WebState, agent: Agent, sent: KeySent | None = None) -> str:
    """The status word the agent page shows, with what the phone has read and any key just sent."""
    answered = sent is not None and sent.evidence is Evidence.GONE
    return page_status(agent, unread_of(web, (agent,)), answered=answered)


def answer_context(agent: Agent, prompt: BlockedPrompt | None, status: str) -> dict[str, object]:
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
        "status": status,
        "about": _asked_about(prompt),
    }


def _asked_about(prompt: BlockedPrompt | None) -> tuple[str, ...]:
    # A buttons dialog asks about a command its heading does not name.
    if prompt is None or prompt.shape is not MenuShape.BUTTONS:
        return ()
    return prompt.context


def answer_region(web: WebState, agent: Agent) -> dict[str, object]:
    """The answer region as it stands now, for its own route and for the page that opens with it."""
    return answer_context(agent, _question(web, agent), shown_status(web, agent))


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


def _key(web: WebState, session: SessionId, post: Posted) -> HTMLResponse:
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
        context = {**answer_context(agent, result.prompt, shown), "key": ask.key.strip().lower()}
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


def _prompt(web: WebState, session: SessionId, post: Posted) -> HTMLResponse:
    prior = _lookup(web, session)
    values = post.values
    ask = PromptAsk(
        session,
        values.get("text", ""),
        expected_target=expected(values),
        drawn_against=values.get("screen", "").strip(),
        confirmed=ticked(values, "confirm"),
        again=ticked(values, "again"),
    )
    result = send_prompt(post.writer, ask)
    if isinstance(result, Refused):
        raise_if_gone(result)
        context = {
            "agent": _after(web, session, prior),
            "text": ask.text,
            "screen": result.screen,
            # Kept through a later refusal, or a confirm after it would lose the word "again".
            "again": result.asks_again or ask.again,
        }
        return refused(web.pages, Redraw("compose.html", context), result)
    return _composed(web, _after(web, session, prior), result)


def _composed(web: WebState, agent: Agent, result: PromptSent) -> HTMLResponse:
    context = {"agent": agent, "text": "", "screen": "", "sent": result}
    return web.pages.fragment("compose.html", context, headers={"HX-Trigger": "hp:poke"})


def _close(web: WebState, session: SessionId, post: Posted) -> Response:
    values = post.values
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
        return refused(web.pages, Redraw("close.html", context, whole=True), result)
    return opened(post.request, "/")


def build(web: WebState) -> APIRouter:
    """The per-agent write routes; every one sits behind the identity and origin guard."""
    guard = viewer_guard(web)
    router = APIRouter(dependencies=[Depends(guard)])
    posting = posted(web, guard)
    _composer_routes(router, web, posting)
    _answer_routes(router, web, posting)
    _close_routes(router, web, posting)
    _door_page(router, web)
    _door_writes(router, web, posting)
    return router


def _composer_routes(router: APIRouter, web: WebState, posting: Posting) -> None:
    @router.get("/agent/{session}/compose")
    def composer(session: str) -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        agent = find_agent(web, identity_of(session))
        return web.pages.fragment("compose.html", composer_region(web, agent))

    @router.post("/agent/{session}/prompt")
    def prompt(session: str, post: Annotated[Posted, Depends(posting)]) -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        return _prompt(web, identity_of(session), post)


def _answer_routes(router: APIRouter, web: WebState, posting: Posting) -> None:
    @router.get("/agent/{session}/answer")
    def answer(session: str) -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        agent = find_agent(web, identity_of(session))
        return web.pages.fragment("answer.html", answer_region(web, agent))

    @router.post("/agent/{session}/keys")
    def keys(session: str, post: Annotated[Posted, Depends(posting)]) -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        return _key(web, identity_of(session), post)


def _close_routes(router: APIRouter, web: WebState, posting: Posting) -> None:
    @router.get("/agent/{session}/close")
    def close_form(session: str) -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        agent = find_agent(web, identity_of(session))
        return web.pages.page("close.html", {"agent": agent})

    @router.post("/agent/{session}/close", response_model=None)
    def close(session: str, post: Annotated[Posted, Depends(posting)]) -> Response:  # pyright: ignore[reportUnusedFunction]
        return _close(web, identity_of(session), post)


def _door_page(router: APIRouter, web: WebState) -> None:
    @router.get("/starting/{pane}", response_model=None)
    def starting(pane: str, first: str = "") -> Response:  # pyright: ignore[reportUnusedFunction]
        target = address_of(pane)
        agent = web.client.snapshot().by_target(target)
        if agent is not None and agent.session:
            return RedirectResponse(agent_url(agent.session), status_code=303)
        context = {**_door_context(target, agent), "first": first_note(first)}
        return web.pages.page("starting.html", context)


def _door_writes(router: APIRouter, web: WebState, posting: Posting) -> None:
    @router.post("/starting/{pane}/prompt")
    def first_prompt(pane: str, post: Annotated[Posted, Depends(posting)]) -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        values = post.values
        target = address_of(pane)
        text = values.get("text", "")
        result = send_first_prompt(
            post.writer,
            target,
            text,
            drawn_against=values.get("screen", "").strip(),
            confirmed=ticked(values, "confirm"),
        )
        return _door(web, target, _Knock(result, text=text))

    @router.post("/starting/{pane}/keys")
    def first_key(pane: str, post: Annotated[Posted, Depends(posting)]) -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        target = address_of(pane)
        key = post.values.get("key", "")
        confirmed = ticked(post.values, "confirm")
        result = send_starting_key(post.writer, target, key, confirmed=confirmed)
        return _door(web, target, _Knock(result, key=key.strip().lower()))


def _door_context(pane: PaneId, agent: Agent | None, text: str = "") -> dict[str, object]:
    return {"pane": pane, "agent": agent, "pad": DOOR_KEYS, "text": text}


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
        # Which form the refusal answers, so a held interrupt never arms the prompt's confirm.
        context = {**_door_context(pane, agent, knock.text), "key": knock.key}
        return refused(web.pages, Redraw("starting.html", context, whole=True), result)
    return web.pages.page("starting.html", {**_door_context(pane, agent), "sent": result})
