"""Starting an agent in a new tab, and ending one: the write with no undo."""

import logging
import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from herdr_pwa import statedir
from herdr_pwa.audit import CLOSE, PROMPT, START, Write
from herdr_pwa.engine import (
    AgentStatus,
    EngineError,
    HerdrClient,
    Launch,
    NewTab,
    PaneId,
    Seat,
    SessionId,
    StartedAgent,
    StartRecipe,
    as_pane_id,
    harness_kinds,
    harness_of,
)
from herdr_pwa.writes.guard import destructive_reason, unsendable
from herdr_pwa.writes.outcome import Evidence, Outcome, Pacing, audit_result
from herdr_pwa.writes.prompt import (
    LAUNCH_READS,
    Echo,
    PromptSent,
    read_back,
    send_opening_prompt,
)
from herdr_pwa.writes.refusal import Refusal, Refused, failed_result, refusal_for
from herdr_pwa.writes.resolve import (
    Writer,
    agent_now,
    attempt,
    one_at_a_time,
    snapshot_now,
    workspace_key,
)

log = logging.getLogger(__name__)

MAX_AGENT_NAME: Final = 32
_AGENT_NAME: Final = re.compile(r"^[a-z][a-z0-9_-]*$")
_NOT_NAME: Final = re.compile(r"[^a-z0-9_-]+")
# The shape a model id must have before it goes anywhere near an argv.
_MODEL_ID: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,95}$")
SESSION_WAIT: Final = 2.0
SESSION_POLL: Final = 0.5

# Where a person's own installs land when no package manager puts them on the system PATH. A
# service started with a narrower PATH than a login shell misses these, and offered nothing.
USER_BIN_DIRS: Final = ("~/.local/bin", "~/bin", "~/.bun/bin", "~/.npm-global/bin")
# What `serve` records of its own PATH, so `doctor` run from a shell can judge the service's.
SERVICE_PATH_FILENAME: Final = "service-path"


def launch_of(kind: str) -> StartRecipe | None:
    """How *kind* is started from the phone, or ``None`` for a kind with no harness here."""
    harness = harness_of(kind)
    return harness.launch if harness is not None else None


@dataclass(frozen=True, slots=True)
class StartRequest:
    """What the new-agent form asked for, before any of it is trusted."""

    name: str
    kind: str
    workspace_id: str
    cwd: str
    model: str = ""
    # On the command line for a harness that takes one there, else typed once it listens.
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


def found_in(name: str, dirs: Iterable[str]) -> str | None:
    """The executable called ``name`` in the first of *dirs* holding one, without spawning."""
    for entry in dirs:
        if not entry:
            continue
        candidate = Path(entry).expanduser() / name
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        except OSError:
            continue
    return None


def on_path(name: str) -> str | None:
    """The executable called ``name`` on this process's PATH, found without spawning anything."""
    return found_in(name, os.environ.get("PATH", os.defpath).split(os.pathsep))


def offered_kinds(which: Callable[[str], str | None] = on_path) -> tuple[str, ...]:
    """The harnesses this app knows how to launch that are on this machine's PATH."""
    return tuple(kind for kind in harness_kinds() if which(_binary(kind)))


def _binary(kind: str) -> str:
    launch = launch_of(kind)
    # herdr's --kind names the canonical executable, so a kind with no harness here is its own.
    return launch.binary if launch is not None else kind


@dataclass(frozen=True, slots=True)
class Availability:
    """Whether one kind herdr can run would start from here, and why not when it would not."""

    kind: str
    # This app has a harness for it: its chat, usage and launch are known.
    known: bool
    # Where the PATH asked about finds its executable, or ``""``.
    found: str = ""
    # Where it is installed for the user while that PATH cannot see it, or ``""``.
    stranded: str = ""

    @property
    def offered(self) -> bool:
        """The new-agent form offers it."""
        return self.known and bool(self.found)


def availability(kind: str, path: str) -> Availability:
    """*kind* judged against the PATH string *path*, and against the user's own install dirs."""
    binary = _binary(kind)
    found = found_in(binary, path.split(os.pathsep)) or ""
    stranded = "" if found else found_in(binary, USER_BIN_DIRS) or ""
    return Availability(kind, known=launch_of(kind) is not None, found=found, stranded=stranded)


def stranded_kinds() -> tuple[Availability, ...]:
    """The harnesses here that are installed for the user but not on this process's PATH."""
    search = os.environ.get("PATH", os.defpath)
    return tuple(
        found
        for found in (availability(kind, search) for kind in harness_kinds())
        if found.stranded
    )


def record_service_path(state_dir: Path) -> None:
    """Write this process's PATH where ``doctor`` reads it; a failure is logged, never raised."""
    try:
        _ = statedir.ensure_dir(state_dir)
        statedir.write_secret(
            state_dir / SERVICE_PATH_FILENAME, os.environ.get("PATH", os.defpath) + "\n"
        )
    except OSError as exc:
        log.warning("could not record the service's PATH for doctor: %s", exc)


def service_path(state_dir: Path) -> str | None:
    """The PATH the service last started with, or ``None`` when it never said."""
    try:
        text = statedir.read_text(state_dir / SERVICE_PATH_FILENAME)
    except OSError:
        return None
    return None if text is None else text.strip()


def _unusable_cwd(cwd: str) -> bool:
    if not cwd.startswith("/"):
        return True
    try:
        return not Path(cwd).is_dir()
    except OSError:
        return True


def _form_refusal(request: StartRequest, kinds: tuple[str, ...]) -> Refusal | None:
    harness = launch_of(request.kind) if request.kind in kinds else None
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
        _ = client.close_pane(Seat(as_pane_id(pane)))
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


@dataclass(frozen=True, slots=True)
class _Launched:
    agent: StartedAgent
    # The first prompt, when it went on the command line rather than being typed.
    first: PromptSent | None = None


def _launch(writer: Writer, request: StartRequest, name: str) -> _Launched | Refused:
    harness = launch_of(request.kind)
    if harness is None:
        return Refused(Refusal.BAD_KIND, "created")
    prompt = request.prompt if harness.takes_prompt and request.prompt.strip() else ""
    # herdr types the launch into the tab's shell, which would keep the prompt in its history file;
    # a null HISTFILE saves none (bash(1), HISTFILE). Only this tab's shell, and only then.
    env = {**harness.env, "HISTFILE": ""} if prompt else harness.env
    try:
        pane = writer.client.create_tab(
            NewTab(request.workspace_id, request.cwd.strip(), name, env=env)
        )
    except EngineError as exc:
        return Refused(refusal_for(exc), "created", detail=str(exc))
    launch = Launch(name, request.kind, pane, harness.args(request.model, prompt))
    launched = _spawn(writer, launch, prompt)
    if isinstance(launched, Refused):
        _abandon(writer.client, pane)
    return launched


def _spawn(writer: Writer, launch: Launch, prompt: str) -> _Launched | Refused:
    if not prompt:
        try:
            return _Launched(writer.client.start_agent(launch))
        except EngineError as exc:
            return Refused(refusal_for(exc), "created", detail=str(exc))
    # The pane's key, as a first prompt typed by the form or the starting page would take it.
    key = f"pane:{launch.pane_id}"
    return one_at_a_time(key, "created", lambda: _spawn_prompted(writer, launch, prompt))


def _spawn_prompted(writer: Writer, launch: Launch, prompt: str) -> _Launched | Refused:
    # Recorded before the process that carries it exists: an unrecordable prompt spawns nothing.
    pane = as_pane_id(launch.pane_id)
    entry = attempt(writer.audit, Write(PROMPT, pane, payload=prompt), verb="created")
    if isinstance(entry, Refused):
        return entry
    with entry:
        try:
            started = writer.client.start_agent(launch)
        except EngineError as exc:
            entry.outcome(failed_result(exc))
            return Refused(refusal_for(exc), "created", detail=str(exc))
        shown = read_back(writer.client, Echo(pane, prompt), writer.pacing, reads=LAUNCH_READS)
        outcome, evidence = (
            (Outcome.CONFIRMED, Evidence.ECHO) if shown else (Outcome.ACCEPTED, Evidence.ARGV)
        )
        entry.outcome(audit_result(outcome, evidence))
    return _Launched(started, PromptSent(pane, outcome, evidence))


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
    # Keyed on the workspace: the new pane does not exist yet, and a close of this workspace
    # mid-start would end the agent it is starting.
    return one_at_a_time(
        workspace_key(request.workspace_id), "created", lambda: _start_agent(writer, request)
    )


def _start_agent(writer: Writer, request: StartRequest) -> AgentStarted | Refused:
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
        launched = _launch(writer, request, name)
        if isinstance(launched, Refused):
            entry.outcome(f"{'unknown' if launched.unsure else 'refused'}:{launched.refusal.value}")
            return launched
        entry.outcome(f"started:{launched.agent.pane}")
    return _opened(writer, launched, request.prompt)


def _opened(writer: Writer, launched: _Launched, prompt: str) -> AgentStarted:
    settled = _await_session(writer.client, launched.agent, writer.pacing)
    first: PromptSent | Refused | None = launched.first
    if first is None and prompt.strip():
        first = send_opening_prompt(writer, as_pane_id(settled.pane), prompt)
    if first is not None:
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
    # The prompt and key writes' key, so nothing is typed at an agent while it is being ended.
    return one_at_a_time(f"session:{ask.session}", "closed", lambda: _end_agent(writer, ask))


def _end_agent(writer: Writer, ask: EndAsk) -> AgentEnded | Refused:
    agent = agent_now(writer, ask.session, expected_target=ask.expected_target, verb="closed")
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
            _ = writer.client.close_pane(Seat(agent.target, ask.session))
        except EngineError as exc:
            entry.outcome(failed_result(exc))
            return Refused(refusal_for(exc), "closed", detail=str(exc))
        entry.outcome("closed")
    return AgentEnded(agent.target, verified=not _still_there(writer.client, ask.session))
