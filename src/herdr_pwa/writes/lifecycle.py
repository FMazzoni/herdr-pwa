"""Starting an agent in a new tab, and ending one: the write with no undo."""

import json
import logging
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from herdr_pwa.audit import CLOSE, START, Write
from herdr_pwa.engine.client import HerdrClient, StartedAgent
from herdr_pwa.engine.model import AgentStatus, PaneId, SessionId, as_pane_id
from herdr_pwa.engine.transport import EngineError
from herdr_pwa.writes.guard import destructive_reason, unsendable
from herdr_pwa.writes.outcome import Pacing
from herdr_pwa.writes.prompt import PromptSent, send_opening_prompt
from herdr_pwa.writes.refusal import Refusal, Refused, failed_result, refusal_for
from herdr_pwa.writes.resolve import Writer, agent_now, attempt, snapshot_now

log = logging.getLogger(__name__)

MAX_AGENT_NAME: Final = 32
_AGENT_NAME: Final = re.compile(r"^[a-z][a-z0-9_-]*$")
_NOT_NAME: Final = re.compile(r"[^a-z0-9_-]+")
# The shape a model id must have before it goes anywhere near an argv.
_MODEL_ID: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,95}$")
SESSION_WAIT: Final = 2.0
SESSION_POLL: Final = 0.5

# opencode auto-approves bash by default, so its agent would never block and never notify.
OPENCODE_ASK: Final = json.dumps({"permission": {"bash": "ask"}}, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class Harness:
    """How one agent kind is launched: its model flag, the models offered, its environment."""

    kind: str
    model_flag: str = "--model"
    # Empty means this box cannot enumerate them, so no model is ever put on the argv.
    models: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict[str, str])

    def args(self, model: str) -> tuple[str, ...]:
        """What follows the bare ``--`` on the agent's command line."""
        return (self.model_flag, model) if model else ()


HARNESSES: Final[Mapping[str, Harness]] = {
    "claude": Harness("claude", models=("fable", "opus", "sonnet", "haiku")),
    # OPENCODE_CONFIG_CONTENT is merged over the project's config: https://opencode.ai/docs/config/
    "opencode": Harness("opencode", env={"OPENCODE_CONFIG_CONTENT": OPENCODE_ASK}),
    "hermes": Harness("hermes"),
}


@dataclass(frozen=True, slots=True)
class StartRequest:
    """What the new-agent form asked for, before any of it is trusted."""

    name: str
    kind: str
    workspace_id: str
    cwd: str
    model: str = ""
    # Typed once the agent listens; blank sends nothing.
    prompt: str = ""
    # The destructive-prompt confirm, given against this form before anything was created.
    confirmed: bool = False


@dataclass(frozen=True, slots=True)
class AgentStarted:
    """An agent the runner started; ``session`` is empty for a harness that names itself late."""

    pane: PaneId
    session: SessionId
    ready: bool
    # What became of the form's first prompt; ``None`` when it carried none.
    first: PromptSent | Refused | None = None


@dataclass(frozen=True, slots=True)
class AgentEnded:
    """A pane the runner closed, and whether a snapshot afterwards agrees the session is gone."""

    pane: PaneId
    verified: bool


def agent_name(typed: str) -> str:
    """The runner's name for an agent, folded out of what a phone typed, or ``""``."""
    folded = _NOT_NAME.sub("-", typed.strip().lower()).strip("-_")
    folded = folded[:MAX_AGENT_NAME].strip("-_")
    return folded if _AGENT_NAME.match(folded) else ""


def on_path(name: str) -> str | None:
    """The executable called ``name`` on this process's PATH, found without spawning anything."""
    for entry in os.environ.get("PATH", os.defpath).split(os.pathsep):
        candidate = Path(entry) / name
        try:
            if entry and candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        except OSError:
            continue
    return None


def offered_kinds(which: Callable[[str], str | None] = on_path) -> tuple[str, ...]:
    """The harnesses this app knows how to launch that are on this machine's PATH."""
    return tuple(kind for kind in HARNESSES if which(kind))


def _unusable_cwd(cwd: str) -> bool:
    if not cwd.startswith("/"):
        return True
    try:
        return not Path(cwd).is_dir()
    except OSError:
        return True


def _form_refusal(request: StartRequest, kinds: tuple[str, ...]) -> Refusal | None:
    harness = HARNESSES.get(request.kind) if request.kind in kinds else None
    checks = (
        (not agent_name(request.name), Refusal.BAD_NAME),
        (harness is None, Refusal.BAD_KIND),
        (
            bool(request.model)
            and (
                harness is None
                or request.model not in harness.models
                or not _MODEL_ID.match(request.model)
            ),
            Refusal.BAD_MODEL,
        ),
        (_unusable_cwd(request.cwd.strip()), Refusal.BAD_CWD),
    )
    return next((refusal for failed, refusal in checks if failed), None)


def _prompt_refusal(request: StartRequest) -> Refused | None:
    # Checked before anything is created, so a prompt that cannot go never leaves a half-start.
    if not request.prompt.strip():
        return None
    if unsendable(request.prompt) is False:
        return Refused(Refusal.TOO_LONG, "created")
    reason = "" if request.confirmed else destructive_reason(request.prompt)
    return Refused(Refusal.CONFIRM, "created", reason=reason) if reason else None


def _abandon(client: HerdrClient, pane: str) -> None:
    # The one close by bare pane id: a pane this same request created a moment ago.
    try:
        _ = client.close_pane(as_pane_id(pane))
    except (EngineError, ValueError):
        log.exception("the pane %s could not be closed and has leaked", pane)


def _await_session(client: HerdrClient, started: StartedAgent, pacing: Pacing) -> StartedAgent:
    deadline = pacing.clock() + SESSION_WAIT
    while not started.session and pacing.clock() < deadline:
        pacing.sleep(SESSION_POLL)
        try:
            started = client.started_agent(started.pane)
        except EngineError as exc:
            log.warning("could not re-read the new agent at %s: %s", started.pane, exc)
            break
    return started


def _launch(client: HerdrClient, request: StartRequest, name: str) -> StartedAgent | Refused:
    harness = HARNESSES[request.kind]
    try:
        pane = client.create_tab(
            workspace_id=request.workspace_id, cwd=request.cwd.strip(), label=name, env=harness.env
        )
    except EngineError as exc:
        return Refused(refusal_for(exc), "created", detail=str(exc))
    try:
        return client.start_agent(
            name=name, kind=harness.kind, pane_id=pane, args=harness.args(request.model)
        )
    except EngineError as exc:
        _abandon(client, pane)
        return Refused(refusal_for(exc), "created", detail=str(exc))


def start_agent(
    writer: Writer,
    request: StartRequest,
    *,
    which: Callable[[str], str | None] = on_path,
) -> AgentStarted | Refused:
    """Start one agent in a new tab of a workspace that exists now, or say why not."""
    refusal = _form_refusal(request, offered_kinds(which))
    held = Refused(refusal, "created") if refusal is not None else _prompt_refusal(request)
    if held is not None:
        return held
    snap = snapshot_now(writer.client, verb="created")
    if isinstance(snap, Refused):
        return snap
    if not any(w.workspace_id == request.workspace_id for w in snap.workspaces):
        return Refused(Refusal.NO_WORKSPACE, "created")
    name = agent_name(request.name)
    payload = (
        f"kind={request.kind!r} name={name!r} model={request.model!r} cwd={request.cwd.strip()!r}"
    )
    entry = attempt(writer.audit, Write(START, payload=payload), verb="created")
    if isinstance(entry, Refused):
        return entry
    with entry:
        started = _launch(writer.client, request, name)
        if isinstance(started, Refused):
            entry.outcome(f"{'unknown' if started.unsure else 'refused'}:{started.refusal.value}")
            return started
        entry.outcome(f"started:{started.pane}")
    return _opened(writer, started, request.prompt)


def _opened(writer: Writer, started: StartedAgent, prompt: str) -> AgentStarted:
    settled = _await_session(writer.client, started, writer.pacing)
    first = None
    if prompt.strip():
        first = send_opening_prompt(writer, as_pane_id(settled.pane), prompt)
        # A late-identity harness opens its session on its first prompt, so look for it again.
        settled = _await_session(writer.client, settled, writer.pacing)
    return AgentStarted(
        as_pane_id(settled.pane), SessionId(settled.session), settled.ready, first=first
    )


def _still_there(client: HerdrClient, session: SessionId) -> bool:
    try:
        return client.snapshot().by_session(session) is not None
    except EngineError:
        return True


@dataclass(frozen=True, slots=True)
class EndAsk:
    """A close: who it ends, and the two acknowledgements it may need."""

    session: SessionId
    expected_target: PaneId | None = None
    confirmed: bool = False
    # The second, different acknowledgement a working agent needs.
    acknowledged: bool = False


def end_agent(writer: Writer, ask: EndAsk) -> AgentEnded | Refused:
    """Close the pane of the agent holding the session now, after both confirms it needs."""
    agent = agent_now(
        writer.client, ask.session, expected_target=ask.expected_target, verb="closed"
    )
    if isinstance(agent, Refused):
        return agent
    if not ask.confirmed:
        return Refused(Refusal.CONFIRM, "closed")
    # Judged on the status read in this request, never the one the confirm screen showed.
    if agent.status is AgentStatus.WORKING and not ask.acknowledged:
        return Refused(Refusal.ACKNOWLEDGE, "closed")
    entry = attempt(writer.audit, Write(CLOSE, agent.target, ask.session), verb="closed")
    if isinstance(entry, Refused):
        return entry
    with entry:
        try:
            _ = writer.client.close_pane(agent.target, session=ask.session)
        except EngineError as exc:
            entry.outcome(failed_result(exc))
            return Refused(refusal_for(exc), "closed", detail=str(exc))
        entry.outcome("closed")
    return AgentEnded(agent.target, verified=not _still_there(writer.client, ask.session))
