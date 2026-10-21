"""The typed verbs: what this app is allowed to ask the runner for."""

import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import cache
from typing import Final, Literal, NamedTuple, TypeVar, cast, get_args

from pydantic import BaseModel, ValidationError

from herdr_pwa import panetext
from herdr_pwa.clock import MonoClock, Sleep, mono_now
from herdr_pwa.engine.config import EngineSettings
from herdr_pwa.engine.dialog import BlockedPrompt
from herdr_pwa.engine.generated import request as wire_request
from herdr_pwa.engine.generated import success_response as wire
from herdr_pwa.engine.model import (
    Agent,
    PaneId,
    Seat,
    SessionId,
    Snapshot,
    Workspace,
    WorkspaceId,
    as_pane_id,
)
from herdr_pwa.engine.transport import (
    EngineError,
    EventStream,
    Frame,
    InvalidRequestError,
    InvalidResponseError,
    ProtocolMismatchError,
    SocketTransport,
    TargetNotFoundError,
    Transport,
    WireReply,
)
from herdr_pwa.process_start import PanePids
from herdr_pwa.protocol import WRITE_METHODS

log = logging.getLogger(__name__)


def discovery_subscriptions() -> list[Frame]:
    """The three pane events that take no `pane_id`, so no roster can stale them."""
    return [
        wire_request.PaneCreatedSubscription(type="pane.created").model_dump(),
        wire_request.PaneAgentDetectedSubscription(type="pane.agent_detected").model_dump(),
        wire_request.PaneClosedSubscription(type="pane.closed").model_dump(),
    ]


def status_subscriptions(panes: Sequence[str | PaneId]) -> list[Frame]:
    """One status subscription per pane, because the runner offers no wildcard."""
    return [
        wire_request.PaneAgentStatusChangedSubscription(
            type="pane.agent_status_changed", pane_id=str(pane)
        ).model_dump(exclude_none=True)
        for pane in dict.fromkeys(str(pane) for pane in panes)
    ]


_BLOCKED: Final = "blocked"

NOT_READY_CODE: Final = "agent_not_ready"

INTERRUPT_KEY: Final = "ctrl-c"

SESSION_MOVED_CODE: Final = "session_moved"

PANE_BUSY_CODE: Final = "agent_pane_busy"

_START_RETRIES: Final = 3

_START_BACKOFF: Final = 0.3

_SHELL_POLLS: Final = 20

_SHELL_INTERVAL: Final = 0.1

START_TIMEOUT_MS: Final = 10_000

_START_BUDGET: Final = START_TIMEOUT_MS / 1000 + 3.0

_Arm = TypeVar("_Arm", bound=BaseModel)

_UNSENT: Final = ""


@cache
def method_of(request_type: type[BaseModel]) -> str:
    """The dotted method name herdr's schema pinned onto this request type."""
    annotation = request_type.model_fields["method"].annotation
    args: tuple[object, ...] = get_args(annotation)
    names = [arg for arg in args if isinstance(arg, str)]
    if len(names) != 1:
        raise InvalidResponseError(
            f"{request_type.__name__} does not pin exactly one method name; "
            "the generated models are not the shape this build expects"
        )
    return names[0]


@cache
def payload_field(arm: type[BaseModel]) -> str:
    """The one field of a result arm that is not its ``type`` discriminator."""
    fields = [name for name in arm.model_fields if name != "type"]
    if len(fields) != 1:
        raise InvalidResponseError(
            f"{arm.__name__} does not carry exactly one payload field; "
            "the generated models are not the shape this build expects"
        )
    return fields[0]


_TEXT_FIELD: Final = "text"


# A recent read past the viewport scrolls the operator's own pane, so no caller can ask for one.
ReadBuffer = Literal[wire_request.ReadSource.visible, wire_request.ReadSource.detection]


@dataclass(frozen=True, slots=True)
class ReadOptions:
    """How one pane read is taken: lines, buffer, colour and timeout; no timeout is the default."""

    lines: int = 200
    source: ReadBuffer = wire_request.ReadSource.visible
    colour: bool = False
    timeout: float | None = None


# The default read: the visible screen as text, at the command timeout.
PLAIN_READ: Final = ReadOptions()
# What is on the pane's screen right now, as text.
VISIBLE_READ: Final = ReadOptions(source=wire_request.ReadSource.visible)
# The same screen in colour: the mirror draws it, and its text is what a prompt is checked against.
SCREEN_READ: Final = ReadOptions(source=wire_request.ReadSource.visible, colour=True)


class Mirror(NamedTuple):
    """One look at a pane for the drill-in: what its mirror draws, and the visible screen."""

    shown: str
    # None when the mirror drew the detection buffer and the screen beside it could not be read.
    screen: str | None


@dataclass(frozen=True, slots=True)
class NewTab:
    """A tab to make in a workspace: where its shell starts, its label, and its environment."""

    workspace_id: WorkspaceId
    cwd: str
    label: str
    env: Mapping[str, str] = field(default_factory=dict[str, str])


@dataclass(frozen=True, slots=True)
class Launch:
    """An agent to start: its name, its harness kind, the settled shell pane, and its arguments."""

    name: str
    kind: str
    pane_id: PaneId
    args: tuple[str, ...] = ()


class _Reply(NamedTuple):
    """A frame the runner answered, and the method it answered, for the error that names it."""

    method: str
    payload: Frame
    result: wire.ResponseResult | ValidationError


class _SalvagedProcessInfo(wire.PaneProcessInfo):
    """The process info of a reply whose envelope the schema refused, read on its own."""

    @classmethod
    def salvage(cls, raw: Frame) -> wire.PaneProcessInfo | None:
        try:
            return cls.model_validate(raw)
        except ValidationError:
            return None


class StartedAgent(NamedTuple):
    """What starting an agent actually told us, in this app's own words."""

    pane: PaneId
    session: SessionId
    status: str
    ready: bool = False


NO_SESSION: Final[SessionId] = SessionId("")


class HerdrClient:
    """One configured view of one agent runner, with its own protocol memory."""

    def __init__(
        self,
        settings: EngineSettings,
        *,
        transport: Transport | None = None,
        clock: MonoClock = mono_now,
        sleep: Sleep = time.sleep,
    ) -> None:
        self.settings = settings
        self._clock = clock
        self._sleep = sleep
        self.transport: Transport = (
            transport
            if transport is not None
            else SocketTransport(
                self.settings.socket_path,
                timeout=self.settings.command_timeout,
                clock=clock,
                limits=self.settings.wire,
            )
        )
        self.last_protocol: int | None = None
        self.last_protocol_at: float = 0.0
        self.warned_protocol: int | None = None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"{type(self).__name__}(socket={str(self.settings.socket_path)!r}, "
            f"timeout={self.settings.command_timeout!r}, "
            f"expected_protocol={self.settings.expected_protocol!r})"
        )

    # --- The one choke point: guard, issue, validate ---

    def _call(
        self,
        request_type: type[BaseModel],
        params: BaseModel,
        *,
        timeout: float | None = None,
        guarded: bool = False,
    ) -> _Reply:
        """Issue one request, with the write guard applied first; the reply names its method."""
        method = method_of(request_type)
        writes = method in WRITE_METHODS
        if writes and not guarded:
            self._require_writable(timeout)
        frame = request_type(id=_UNSENT, method=method, params=params)
        dumped = frame.model_dump(mode="json", exclude_none=True)
        reply: WireReply = self.transport.call(
            method,
            # A json-mode dump is JSON by construction, and the schema pins params to an object.
            cast("Frame", dumped["params"]),
            timeout=self._resolve_timeout(timeout),
            idempotent=not writes,
        )
        return _Reply(method, reply.payload, reply.result)

    def _resolve_timeout(self, timeout: float | None) -> float:
        return self.settings.command_timeout if timeout is None else timeout

    def _result(
        self, reply: _Reply, arm: type[_Arm], *, degrade: bool
    ) -> tuple[_Arm | None, Frame]:
        """Validate a reply against herdr's schema; return its arm and its payload."""
        method, payload, result = reply
        key = payload_field(arm)
        if isinstance(result, ValidationError):
            if not degrade:
                raise InvalidResponseError(
                    f"{method} answered a frame that does not match the "
                    f"runner's own published schema: {result}"
                ) from result
            log.warning(
                "%s answered a frame that does not match the runner's own published schema; "
                "serving it anyway, since a write to an agent first re-reads it with a strict "
                "agent.get: %s",
                method,
                result,
            )
            return None, self._sub_object(payload, key, method=method)
        root = result.root
        if not isinstance(root, arm):
            raise InvalidResponseError(
                f"{method} answered {type(root).__name__} where {arm.__name__} was expected"
            )
        # Payload_field names the arm's field that holds its BaseModel payload.
        member = cast("BaseModel", getattr(root, key))
        return root, member.model_dump(mode="json", exclude_none=True)

    def _arm(self, reply: _Reply, arm: type[_Arm]) -> _Arm:
        """Validate a reply and hand back its **typed arm**, never a payload field."""
        del self
        method, _, result = reply
        if isinstance(result, ValidationError):
            raise InvalidResponseError(
                f"{method} answered a frame that does not match the runner's "
                f"own published schema: {result}"
            ) from result
        root = result.root
        if not isinstance(root, arm):
            raise InvalidResponseError(
                f"{method} answered {type(root).__name__} where {arm.__name__} was expected"
            )
        return root

    def _written(self, reply: _Reply, arm: type[_Arm]) -> _Arm:
        """A write's reply as its typed arm; a reply that will not parse came after the write ran."""
        try:
            return self._arm(reply, arm)
        except InvalidResponseError as exc:
            exc.after_send = True
            raise

    @staticmethod
    def _sub_object(payload: Frame, key: str, *, method: str) -> Frame:
        """The raw ``result.<key>`` object, for a frame that would not validate."""
        result = payload.get("result")
        raw = result.get(key) if isinstance(result, dict) else None
        if not isinstance(raw, dict):
            raise InvalidResponseError(f"{method} answered no {key!r} object")
        return raw

    # --- Reads ---

    def snapshot(self, *, timeout: float | None = None) -> Snapshot:
        """Fetch the whole session state, agents already ranked most-urgent-first."""
        reply = self._call(
            wire_request.SessionSnapshotRequest, wire_request.EmptyParams(), timeout=timeout
        )
        _, raw = self._result(reply, wire.SessionSnapshotResponseResult, degrade=True)
        snap = Snapshot.from_raw(raw)
        self._remember_protocol(snap.protocol)
        return snap

    def watch_statuses(self, panes: Sequence[str | PaneId]) -> EventStream:
        """A stream of the status transitions on `panes`, on a connection of its own."""
        return EventStream(
            self.settings.socket_path,
            subscriptions=status_subscriptions(panes) + discovery_subscriptions(),
            timeout=self.settings.command_timeout,
            limits=self.settings.wire,
        )

    def _remember_protocol(self, protocol: int) -> None:
        """Record what the runner just proved it speaks, and warn once on drift."""
        self.last_protocol = protocol
        self.last_protocol_at = self._clock()
        if protocol not in (self.settings.expected_protocol, self.warned_protocol):
            self.warned_protocol = protocol
            log.warning(
                "runner speaks snapshot protocol %s, expected %s: "
                "serving reads anyway, writes will fail closed",
                protocol,
                self.settings.expected_protocol,
            )

    def agents(self, *, timeout: float | None = None) -> list[Agent]:
        """Every live agent, normalized, most urgent first. Zero agents is not an error."""
        return list(self.snapshot(timeout=timeout).agents)

    def agent(self, target: PaneId, *, timeout: float | None = None) -> Agent:
        """One agent by pane address -- cheaper than a whole snapshot for a detail poll."""
        _, raw = self._result(
            self._agent_get(as_pane_id(target), timeout=timeout),
            wire.AgentInfoResponseResult,
            degrade=True,
        )
        return Agent.from_raw(raw)

    def _agent_get(self, target: PaneId, *, timeout: float | None) -> _Reply:
        """One ``agent.get``, left for the caller to validate strictly or leniently."""
        return self._call(
            wire_request.AgentGetRequest, wire_request.AgentTarget(target=target), timeout=timeout
        )

    def _agent_info(self, target: PaneId, *, timeout: float | None) -> "wire.AgentInfo":
        """The agent at ``target``, validated strictly, since a write acts on what it says."""
        return self._arm(
            self._agent_get(target, timeout=timeout), wire.AgentInfoResponseResult
        ).agent

    def read(self, target: PaneId, options: ReadOptions = PLAIN_READ) -> str:
        """Read an agent's terminal output."""
        target = as_pane_id(target)
        reply = self._call(
            wire_request.AgentReadRequest,
            self._read_params(target, options),
            timeout=options.timeout,
        )
        _, raw = self._result(reply, wire.PaneReadResponseResult, degrade=True)
        text = raw.get(_TEXT_FIELD)
        if not isinstance(text, str):
            raise InvalidResponseError("the runner answered a read with no terminal text")
        return text

    @staticmethod
    def _read_params(target: PaneId, options: ReadOptions) -> "wire_request.AgentReadParams":
        """The read's parameters, as herdr's schema declares them."""
        colour = options.colour
        return wire_request.AgentReadParams(
            target=target,
            lines=options.lines,
            source=options.source,
            format=(wire_request.ReadFormat.ansi if colour else wire_request.ReadFormat.text),
            # Explicit because the schema default is True, which would sanitise the colour away.
            strip_ansi=not colour,
        )

    def mirror_read(
        self, target: PaneId, *, status: object = "", options: ReadOptions = PLAIN_READ
    ) -> str:
        """The pane read the drill-in mirror renders: detection when blocked and clean, else visible.

        Args:
            target: The pane to read.
            status: The agent's reported status, which decides whether detection is tried.
            options: The lines and the timeout; the mirror chooses the source, in colour.
        """
        target = as_pane_id(target)
        detected = self._detected(target, status, options)
        if detected is not None:
            return detected
        return self.read(
            target, replace(options, source=wire_request.ReadSource.visible, colour=True)
        )

    def mirror(self, target: PaneId, *, status: object = "") -> Mirror:
        """The mirror's read with the visible screen as text beside it, one read when they are one."""
        target = as_pane_id(target)
        detected = self._detected(target, status, PLAIN_READ)
        if detected is None:
            shown = self.read(target, SCREEN_READ)
            return Mirror(shown, panetext.as_text_read(shown))
        try:
            return Mirror(detected, self.screen(target))
        except EngineError as exc:
            log.debug("visible read failed for %s: %s", target, exc)
            return Mirror(detected, None)

    def screen(self, target: PaneId) -> str:
        """The visible screen as text, from the colour read the mirror takes of it."""
        # Text from the colour read, so the screen a page claims is the one a write checks.
        return panetext.as_text_read(self.read(target, SCREEN_READ))

    def _detected(self, target: PaneId, status: object, options: ReadOptions) -> str | None:
        """A blocked agent's detection buffer in colour when it is clean prose, else ``None``."""
        if str(getattr(status, "value", status)).strip().lower() != _BLOCKED:
            return None
        try:
            detected = self.read(
                target, replace(options, source=wire_request.ReadSource.detection, colour=True)
            )
        except EngineError as exc:
            log.debug("detection read failed for %s: %s", target, exc)
            return None
        return detected if panetext.is_clean_prose(detected) else None

    def blocked_prompt(
        self,
        target: PaneId,
        *,
        session: SessionId = NO_SESSION,
        options: ReadOptions = PLAIN_READ,
    ) -> BlockedPrompt:
        """Read what a blocked agent is waiting on, parsed into question + options.

        Args:
            target: The pane to read.
            session: The session the parsed prompt names as its holder.
            options: The lines and the timeout; the parse chooses the buffers.
        """
        target = as_pane_id(target)
        text = self.read(
            target, replace(options, source=wire_request.ReadSource.detection, colour=False)
        )
        return BlockedPrompt.from_reads(
            text, lambda: self._colour_read(target, options), target=target, session=session
        )

    def _colour_read(self, target: PaneId, options: ReadOptions) -> str | None:
        """The visible viewport in colour, or ``None`` when the read fails."""
        try:
            # Measured on the rig: detection strips colour, and only the visible viewport keeps SGR.
            return self.read(
                target, replace(options, source=wire_request.ReadSource.visible, colour=True)
            )
        except EngineError:
            return None

    # --- Writes: every one fails closed on an unexpected protocol ---

    def _require_writable(self, timeout: float | None = None) -> None:
        """Refuse to drive an agent when the snapshot shape can no longer be trusted."""
        fresh = (
            self.last_protocol is not None
            and (self._clock() - self.last_protocol_at) <= self.settings.protocol_max_age
        )
        if not fresh:
            self.snapshot(timeout=timeout)
        if self.last_protocol != self.settings.expected_protocol:
            raise ProtocolMismatchError(
                f"runner speaks snapshot protocol {self.last_protocol}, expected "
                f"{self.settings.expected_protocol}; refusing to drive an agent"
            )

    def _require_ready(
        self, seat: Seat, timeout: float | None = None, *, interrupting: bool = False
    ) -> None:
        """Refuse to type at an agent whose terminal is not reading yet."""
        target = seat.target
        info = self._agent_info(target, timeout=timeout)
        self._require_session(seat, info)
        # Launching AND blocked is an agent that stopped during startup to ask; keys are the point.
        if info.launch_pending and info.agent_status is not wire.AgentStatus.blocked:
            raise EngineError(
                f"agent {target} is still launching; keys sent now would be typed "
                "into a terminal that is not reading yet",
                code=NOT_READY_CODE,
            )
        if info.interactive_ready is False and not interrupting:
            raise EngineError(
                f"agent {target} is not accepting input right now",
                code=NOT_READY_CODE,
            )

    @staticmethod
    def _require_session(seat: Seat, info: wire.AgentInfo) -> None:
        """Refuse to type at a pane that a *different* agent now holds."""
        target, session = seat.target, seat.session
        if session is None:
            return
        holder = info.agent_session.value if info.agent_session else ""
        if holder.strip() != str(session).strip():
            raise TargetNotFoundError(
                f"pane {target} is held by session {holder or '(none)'}, not "
                f"{session}; pane ids are recycled, so this keystroke would go "
                "to a different agent -- re-resolve the session first",
                code=SESSION_MOVED_CODE,
            )

    def prompt(self, seat: Seat, text: str, *, timeout: float | None = None) -> Frame:
        """Submit a prompt to the agent in ``seat``."""
        target = as_pane_id(seat.target)
        seat = Seat(target, seat.session)
        self._require_writable(timeout)
        self._require_ready(seat, timeout)
        return self._call(
            wire_request.AgentPromptRequest,
            wire_request.AgentPromptParams(target=target, text=text),
            timeout=timeout,
            guarded=True,
        ).payload

    def send_keys(self, seat: Seat, *keys: str, timeout: float | None = None) -> Frame:
        """Send key presses to the agent in ``seat`` (``"1"``, ``"enter"``, ``"ctrl-c"``...)."""
        if not keys:
            raise InvalidRequestError("send_keys requires at least one key")
        target = as_pane_id(seat.target)
        seat = Seat(target, seat.session)
        self._require_writable(timeout)
        self._require_ready(seat, timeout, interrupting=all(key == INTERRUPT_KEY for key in keys))
        return self._call(
            wire_request.AgentSendKeysRequest,
            wire_request.AgentSendKeysParams(target=target, keys=list(keys)),
            timeout=timeout,
            guarded=True,
        ).payload

    def focus(self, target: PaneId, *, timeout: float | None = None) -> Frame:
        """Focus an agent's pane in the running session."""
        return self._call(
            wire_request.AgentFocusRequest,
            wire_request.AgentTarget(target=as_pane_id(target)),
            timeout=timeout,
        ).payload

    # --- Lifecycle: the two writes that let a herd shrink as well as grow ---

    def shell_pane(self, pane_id: PaneId, *, timeout: float | None = None) -> bool:
        """Is this pane a settled shell, ready for an agent to be started in it?"""
        info = self._arm(
            self._call(
                wire_request.PaneProcessInfoRequest,
                wire_request.PaneProcessInfoParams(pane_id=pane_id),
                timeout=timeout,
            ),
            wire.PaneProcessInfoResponseResult,
        ).process_info
        running = info.foreground_processes or []
        return bool(info.shell_pid) and [p.pid for p in running] == [info.shell_pid]

    def pane_pids(self, pane_id: PaneId, *, timeout: float | None = None) -> tuple[int, ...]:
        """Every process in this pane, shell first. A runner that will not say answers `()`."""
        found = self.pane_processes(pane_id, timeout=timeout)
        return found.others if found.shell is None else (found.shell, *found.others)

    def pane_processes(self, pane_id: PaneId, *, timeout: float | None = None) -> PanePids:
        """This pane's shell, when herdr names one, apart from its other processes."""
        info = self._process_info(pane_id, timeout)
        if info is None:
            return PanePids(shell=None, others=())
        running = (process.pid for process in info.foreground_processes or [])
        others = tuple(dict.fromkeys(pid for pid in running if pid != info.shell_pid))
        return PanePids(shell=info.shell_pid, others=others)

    def foreground_leader(self, pane_id: PaneId, *, timeout: float | None = None) -> int | None:
        """The process leading the pane's terminal, the one its keys reach, or ``None``."""
        info = self._process_info(pane_id, timeout)
        return None if info is None else info.foreground_process_group_id

    def _process_info(self, pane_id: PaneId, timeout: float | None) -> wire.PaneProcessInfo | None:
        arm, raw = self._result(
            self._call(
                wire_request.PaneProcessInfoRequest,
                wire_request.PaneProcessInfoParams(pane_id=pane_id),
                timeout=timeout,
            ),
            wire.PaneProcessInfoResponseResult,
            degrade=True,
        )
        return arm.process_info if arm else _SalvagedProcessInfo.salvage(raw)

    def create_tab(self, tab: NewTab, *, timeout: float | None = None) -> PaneId:
        """Make a new tab with one shell pane in it, and return its root pane."""
        reply = self._call(
            wire_request.TabCreateRequest,
            wire_request.TabCreateParams(
                workspace_id=tab.workspace_id,
                cwd=tab.cwd,
                label=tab.label,
                focus=False,
                env=dict(tab.env) if tab.env else None,
            ),
            timeout=timeout,
        )
        arm = self._written(reply, wire.TabCreatedResponseResult)
        return PaneId(arm.root_pane.pane_id)

    def start_agent(self, launch: Launch, *, timeout: float | None = None) -> StartedAgent:
        """Start an agent in a pane that already holds a settled shell."""
        self._require_writable(timeout)
        self._wait_for_shell(launch.pane_id, timeout)
        params = wire_request.AgentStartParams(
            name=launch.name,
            kind=launch.kind,
            pane_id=launch.pane_id,
            args=list(launch.args) or None,
            timeout_ms=START_TIMEOUT_MS,
        )
        reply = self._start_with_retries(wire_request.AgentStartRequest, params, timeout)
        arm = self._written(reply, wire.AgentStartedResponseResult)
        return self._started(arm.agent)

    @staticmethod
    def _started(info: "wire.AgentInfo") -> StartedAgent:
        """One ``AgentInfo`` as the three facts the create flow acts on."""
        holder = info.agent_session.value if info.agent_session else ""
        return StartedAgent(
            pane=PaneId(info.pane_id),
            session=SessionId(str(holder or "").strip()),
            status=str(info.agent_status),
            ready=not info.launch_pending and info.interactive_ready is not False,
        )

    def started_agent(self, pane_id: PaneId, *, timeout: float | None = None) -> StartedAgent:
        """Re-read a just-started agent, to see whether it has a session UUID yet."""
        return self._started(self._agent_info(as_pane_id(pane_id), timeout=timeout))

    def close_pane(self, seat: Seat, *, timeout: float | None = None) -> Frame:
        """End an agent by closing the pane it runs in -- the app's one no-undo write."""
        target = as_pane_id(seat.target)
        self._require_writable(timeout)
        if seat.session is not None:
            info = self._agent_info(target, timeout=timeout)
            self._require_session(Seat(target, seat.session), info)
        return self._call(
            wire_request.PaneCloseRequest,
            wire_request.PaneTarget(pane_id=str(target)),
            timeout=timeout,
            guarded=True,
        ).payload

    # --- Workspaces: the container the herd is grouped by ---

    def create_workspace(
        self, *, label: str = "", cwd: str = "", timeout: float | None = None
    ) -> Workspace:
        """Make an empty workspace and hand it back -- no tab is filled, no agent starts."""
        reply = self._call(
            wire_request.WorkspaceCreateRequest,
            wire_request.WorkspaceCreateParams(label=label or None, cwd=cwd or None, focus=False),
            timeout=timeout,
        )
        arm = self._written(reply, wire.WorkspaceCreatedResponseResult)
        return Workspace.from_raw(arm.workspace.model_dump(mode="json"))

    def rename_workspace(
        self, workspace_id: WorkspaceId, label: str, *, timeout: float | None = None
    ) -> Frame:
        """Relabel a workspace. Nothing in it is touched; only what it is called."""
        return self._call(
            wire_request.WorkspaceRenameRequest,
            wire_request.WorkspaceRenameParams(workspace_id=workspace_id, label=label),
            timeout=timeout,
        ).payload

    def close_workspace(self, workspace_id: WorkspaceId, *, timeout: float | None = None) -> Frame:
        """End every agent in a workspace at once -- the largest write this app makes."""
        return self._call(
            wire_request.WorkspaceCloseRequest,
            wire_request.WorkspaceCloseParams(workspace_id=workspace_id),
            timeout=timeout,
        ).payload

    # -- the two halves of start_agent, kept out of it for readability ------

    def _wait_for_shell(self, pane_id: PaneId, timeout: float | None) -> None:
        """Block until the new pane is a settled shell, or refuse the start."""
        for attempt in range(_SHELL_POLLS):
            if self.shell_pane(pane_id, timeout=timeout):
                return
            if attempt + 1 < _SHELL_POLLS:
                self._sleep(_SHELL_INTERVAL)
        raise EngineError(
            f"pane {pane_id} never settled to a shell prompt, so starting an "
            "agent in it would fail on a precondition rather than launch",
            code=PANE_BUSY_CODE,
        )

    def _start_with_retries(
        self,
        request_type: type[BaseModel],
        params: "wire_request.AgentStartParams",
        timeout: float | None,
    ) -> _Reply:
        """``agent.start``, retried on ``agent_pane_busy`` and on nothing else."""
        budget = _START_BUDGET if timeout is None else timeout
        last: EngineError | None = None
        for attempt in range(_START_RETRIES):
            if attempt:
                self._sleep(_START_BACKOFF)
            try:
                return self._call(request_type, params, timeout=budget, guarded=True)
            except EngineError as exc:
                if exc.code != PANE_BUSY_CODE:
                    raise
                last = exc
        raise last if last is not None else EngineError("agent.start never ran")
