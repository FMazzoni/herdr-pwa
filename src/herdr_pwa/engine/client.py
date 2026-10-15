"""The typed verbs: what this app is allowed to ask the runner for."""

import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from functools import cache
from typing import Final, NamedTuple, TypeVar, cast, get_args

from pydantic import BaseModel, ValidationError

from herdr_pwa import panetext
from herdr_pwa.engine.config import EngineSettings
from herdr_pwa.engine.dialog import BlockedPrompt
from herdr_pwa.engine.generated import request as wire_request
from herdr_pwa.engine.generated import success_response as wire
from herdr_pwa.engine.model import (
    Agent,
    PaneId,
    SessionId,
    Snapshot,
    Workspace,
    as_pane_id,
)
from herdr_pwa.engine.transport import (
    EngineError,
    EventStream,
    InvalidRequest,
    InvalidResponse,
    ProtocolMismatch,
    SocketTransport,
    TargetNotFound,
    Transport,
)
from herdr_pwa.jsontypes import JsonObject, as_object, objects
from herdr_pwa.protocol import WRITE_METHODS

log = logging.getLogger(__name__)


def discovery_subscriptions() -> list[JsonObject]:
    """The three pane events that take no `pane_id`, so no roster can stale them."""
    return [
        wire_request.PaneCreatedSubscription(type="pane.created").model_dump(),
        wire_request.PaneAgentDetectedSubscription(type="pane.agent_detected").model_dump(),
        wire_request.PaneClosedSubscription(type="pane.closed").model_dump(),
    ]


def status_subscriptions(panes: Sequence[str | PaneId]) -> list[JsonObject]:
    """One status subscription per pane, because the runner offers no wildcard."""
    return [
        wire_request.PaneAgentStatusChangedSubscription(
            type="pane.agent_status_changed", pane_id=str(pane)
        ).model_dump(exclude_none=True)
        for pane in dict.fromkeys(str(pane) for pane in panes)
    ]


# Measured cost of one line of a recent read on herdr 0.8.0.
SECONDS_PER_RECENT_LINE: Final = 0.035

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
        raise InvalidResponse(
            f"{request_type.__name__} does not pin exactly one method name; "
            "the generated models are not the shape this build expects"
        )
    return names[0]


@cache
def payload_field(arm: type[BaseModel]) -> str:
    """The one field of a result arm that is not its ``type`` discriminator."""
    fields = [name for name in arm.model_fields if name != "type"]
    if len(fields) != 1:
        raise InvalidResponse(
            f"{arm.__name__} does not carry exactly one payload field; "
            "the generated models are not the shape this build expects"
        )
    return fields[0]


_TEXT_FIELD: Final = "text"

_EXPLAIN_FIELD: Final = "explain"


class StartedAgent(NamedTuple):
    """What starting an agent actually told us, in this app's own words."""

    pane: str
    session: str
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
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self._clock = clock
        self._sleep = sleep
        self.transport: Transport = (
            transport
            if transport is not None
            else SocketTransport(self.settings.socket_path, timeout=self.settings.command_timeout)
        )
        self.last_protocol: int | None = None
        self.last_protocol_at: float = 0.0
        self.warned_protocol: int | None = None
        self._refreshing = threading.local()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"{type(self).__name__}(socket={str(self.settings.socket_path)!r}, "
            f"timeout={self.settings.command_timeout!r}, "
            f"expected_protocol={self.settings.expected_protocol!r})"
        )

    def close(self) -> None:
        """Release the connection. The client stays usable and will redial."""
        self.transport.close()

    def forget_protocol(self) -> None:
        """Drop the protocol memory, as if this client had just been built."""
        self.last_protocol = None
        self.last_protocol_at = 0.0
        self.warned_protocol = None

    # ----------------------------------------------------------------------
    # The one choke point: guard, issue, validate
    # ----------------------------------------------------------------------

    def _call(
        self,
        request_type: type[BaseModel],
        params: BaseModel,
        *,
        timeout: float | None = None,
        guarded: bool = False,
    ) -> JsonObject:
        """Issue one request, with the write guard applied first."""
        method = method_of(request_type)
        writes = method in WRITE_METHODS
        if writes and not guarded:
            self._require_writable(timeout)
        frame = request_type.model_validate({"id": _UNSENT, "method": method, "params": params})
        dumped = frame.model_dump(mode="json", exclude_none=True)
        return self.transport.call(
            method,
            # A json-mode dump is JSON by construction, and the schema pins params to an object.
            cast(JsonObject, dumped["params"]),
            timeout=self._resolve_timeout(timeout),
            idempotent=not writes,
        )

    def _resolve_timeout(self, timeout: float | None) -> float:
        return self.settings.command_timeout if timeout is None else timeout

    def _result(
        self,
        payload: JsonObject,
        arm: type[_Arm],
        *,
        method: str,
        degrade: bool,
    ) -> tuple[_Arm | None, JsonObject]:
        """Validate a reply against herdr's schema; return its arm and its payload."""
        field = payload_field(arm)
        try:
            root = wire.SuccessResponse.model_validate(payload).result.root
        except ValidationError as exc:
            if not degrade:
                raise InvalidResponse(
                    f"{method} answered a frame that does not match the "
                    f"runner's own published schema: {exc}"
                ) from exc
            log.warning(
                "%s answered a frame that does not match the runner's own "
                "published schema; serving it anyway, writes will fail closed: %s",
                method,
                exc,
            )
            return None, self._sub_object(payload, field, method=method)
        if not isinstance(root, arm):
            raise InvalidResponse(
                f"{method} answered {type(root).__name__} where {arm.__name__} was expected"
            )
        member = cast(BaseModel, getattr(root, field))
        return root, member.model_dump(mode="json", exclude_none=True)

    def _arm(
        self,
        payload: JsonObject,
        arm: type[_Arm],
        *,
        method: str,
    ) -> _Arm:
        """Validate a reply and hand back its **typed arm**, never a payload field."""
        del self
        try:
            root = wire.SuccessResponse.model_validate(payload).result.root
        except ValidationError as exc:
            raise InvalidResponse(
                f"{method} answered a frame that does not match the runner's "
                f"own published schema: {exc}"
            ) from exc
        if not isinstance(root, arm):
            raise InvalidResponse(
                f"{method} answered {type(root).__name__} where {arm.__name__} was expected"
            )
        return root

    def _written(self, payload: JsonObject, arm: type[_Arm], *, method: str) -> _Arm:
        """A write's reply as its typed arm; a reply that will not parse came after the write ran."""
        try:
            return self._arm(payload, arm, method=method)
        except InvalidResponse as exc:
            exc.after_send = True
            raise

    @staticmethod
    def _sub_object(payload: JsonObject, key: str, *, method: str) -> JsonObject:
        """The raw ``result.<key>`` object, for a frame that would not validate."""
        result = as_object(payload.get("result"))
        raw = as_object(result.get(key)) if result is not None else None
        if raw is None:
            raise InvalidResponse(f"{method} answered no {key!r} object")
        return raw

    # ----------------------------------------------------------------------
    # Reads
    # ----------------------------------------------------------------------

    def snapshot(self, *, timeout: float | None = None) -> Snapshot:
        """Fetch the whole session state, agents already ranked most-urgent-first."""
        payload = self._call(
            wire_request.SessionSnapshotRequest,
            wire_request.EmptyParams(),
            timeout=timeout,
        )
        _, raw = self._result(
            payload,
            wire.SessionSnapshotResponseResult,
            method=method_of(wire_request.SessionSnapshotRequest),
            degrade=True,
        )
        snap = Snapshot.from_raw(raw, expected_protocol=self.settings.expected_protocol)
        self._remember_protocol(snap.protocol)
        return snap

    def watch_statuses(self, panes: Sequence[str | PaneId]) -> EventStream:
        """A stream of the status transitions on `panes`, on a connection of its own."""
        return EventStream(
            self.settings.socket_path,
            subscriptions=status_subscriptions(panes) + discovery_subscriptions(),
            timeout=self.settings.command_timeout,
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
        _, raw = self._agent_result(as_pane_id(target), timeout=timeout, degrade=True)
        return Agent.from_raw(raw)

    def _agent_result(
        self, target: PaneId, *, timeout: float | None, degrade: bool
    ) -> tuple[wire.AgentInfoResponseResult | None, JsonObject]:
        payload = self._call(
            wire_request.AgentGetRequest,
            wire_request.AgentTarget(target=target),
            timeout=timeout,
        )
        return self._result(
            payload,
            wire.AgentInfoResponseResult,
            method=method_of(wire_request.AgentGetRequest),
            degrade=degrade,
        )

    def read(
        self,
        target: PaneId,
        *,
        lines: int | None = None,
        source: str | None = None,
        colour: bool = False,
        timeout: float | None = None,
    ) -> str:
        """Read an agent's terminal output."""
        target = as_pane_id(target)
        lines = self.settings.read_lines if lines is None else lines
        where = self._read_source(self.settings.read_source if source is None else source)
        if where is wire_request.ReadSource.recent and lines > (self.settings.recent_max_lines):
            # Not only cost: past the viewport this scrolls the operator's own pane.
            log.warning(
                "read(%s, source=%s, lines=%d) would scroll the agent's pane for "
                "~%.1fs; capping to %d lines -- use the visible source",
                target,
                where,
                lines,
                lines * SECONDS_PER_RECENT_LINE,
                self.settings.recent_max_lines,
            )
            lines = self.settings.recent_max_lines
        payload = self._call(
            wire_request.AgentReadRequest,
            wire_request.AgentReadParams(
                target=target,
                lines=lines,
                source=where,
                format=(wire_request.ReadFormat.ansi if colour else wire_request.ReadFormat.text),
                # Explicit because the schema default is True, which would sanitise the colour away.
                strip_ansi=not colour,
            ),
            timeout=timeout,
        )
        _, raw = self._result(
            payload,
            wire.PaneReadResponseResult,
            method=method_of(wire_request.AgentReadRequest),
            degrade=True,
        )
        text = raw.get(_TEXT_FIELD)
        if not isinstance(text, str):
            raise InvalidResponse("the runner answered a read with no terminal text")
        return text

    @staticmethod
    def _read_source(source: str) -> "wire_request.ReadSource":
        """Turn a caller's string into the enum herdr's schema declares."""
        if isinstance(source, wire_request.ReadSource):
            return source
        try:
            return wire_request.ReadSource(source)
        except ValueError as exc:
            allowed = ", ".join(s.value for s in wire_request.ReadSource)
            raise InvalidRequest(f"unknown read source {source!r}; expected {allowed}") from exc

    def mirror_read(
        self,
        target: PaneId,
        *,
        status: object = "",
        lines: int | None = None,
        timeout: float | None = None,
    ) -> str:
        """The pane read the drill-in mirror renders: detection when blocked and clean, else visible."""
        target = as_pane_id(target)
        if str(getattr(status, "value", status)).strip().lower() == _BLOCKED:
            try:
                detected = self.read(
                    target,
                    lines=lines,
                    source=wire_request.ReadSource.detection,
                    colour=True,
                    timeout=timeout,
                )
            except EngineError as exc:
                log.debug("detection read failed for %s: %s", target, exc)
            else:
                if panetext.is_clean_prose(detected):
                    return detected
        return self.read(target, lines=lines, colour=True, timeout=timeout)

    def explain(self, target: PaneId, *, timeout: float | None = None) -> JsonObject:
        """Which detection rule decided this agent's state, on what evidence."""
        method = method_of(wire_request.AgentExplainRequest)
        payload = self._call(
            wire_request.AgentExplainRequest,
            wire_request.AgentTarget(target=as_pane_id(target)),
            timeout=timeout,
        )
        return self._sub_object(payload, _EXPLAIN_FIELD, method=method)

    def agent_frame(self, target: PaneId, *, timeout: float | None = None) -> JsonObject:
        """The whole `agent.get` reply as the runner sent it, normalized by nothing."""
        return self._call(
            wire_request.AgentGetRequest,
            wire_request.AgentTarget(target=as_pane_id(target)),
            timeout=timeout,
        )

    def blocked_prompt(
        self,
        target: PaneId,
        *,
        session: SessionId = NO_SESSION,
        lines: int | None = None,
        timeout: float | None = None,
    ) -> BlockedPrompt:
        """Read what a blocked agent is waiting on, parsed into question + options."""
        target = as_pane_id(target)
        text = self.read(
            target,
            lines=lines,
            source=wire_request.ReadSource.detection,
            timeout=timeout,
        )
        return BlockedPrompt.parse(text, target=target, session=session)

    # ----------------------------------------------------------------------
    # Writes -- every one of these fails closed on an unexpected protocol
    # ----------------------------------------------------------------------

    def _require_writable(self, timeout: float | None = None) -> None:
        """Refuse to drive an agent when the snapshot shape can no longer be trusted."""
        if not self.settings.protocol_drift_fails_writes:
            return
        fresh = (
            self.last_protocol is not None
            and (self._clock() - self.last_protocol_at) <= self.settings.protocol_max_age
        )
        if not fresh:
            self._refresh_protocol(timeout)
        if self.last_protocol != self.settings.expected_protocol:
            raise ProtocolMismatch(
                f"runner speaks snapshot protocol {self.last_protocol}, expected "
                f"{self.settings.expected_protocol}; refusing to drive an agent"
            )

    def _refresh_protocol(self, timeout: float | None) -> None:
        """Take a snapshot for the write guard, refusing to do it recursively."""
        if getattr(self._refreshing, "active", False):
            raise EngineError(
                "the write guard re-entered its own protocol refresh; the "
                "snapshot request must not be registered as a write",
                code="engine_misconfigured",
            )
        self._refreshing.active = True
        try:
            self.snapshot(timeout=timeout)
        finally:
            self._refreshing.active = False

    def _require_ready(
        self,
        target: PaneId,
        timeout: float | None = None,
        *,
        session: SessionId | None = None,
        interrupting: bool = False,
    ) -> None:
        """Refuse to type at an agent whose terminal is not reading yet."""
        root, _ = self._agent_result(target, timeout=timeout, degrade=False)
        info = root.agent if root is not None else None
        if info is None:  # pragma: no cover - degrade=False cannot return None
            return
        self._require_session(target, info, session)
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
    def _require_session(target: PaneId, info: wire.AgentInfo, session: SessionId | None) -> None:
        """Refuse to type at a pane that a *different* agent now holds."""
        if session is None:
            return
        holder = info.agent_session.value if info.agent_session else ""
        if holder.strip() != str(session).strip():
            raise TargetNotFound(
                f"pane {target} is held by session {holder or '(none)'}, not "
                f"{session}; pane ids are recycled, so this keystroke would go "
                "to a different agent -- re-resolve the session first",
                code=SESSION_MOVED_CODE,
            )

    def prompt(
        self,
        target: PaneId,
        text: str,
        *,
        session: SessionId | None = None,
        timeout: float | None = None,
    ) -> JsonObject:
        """Submit a prompt to an agent."""
        target = as_pane_id(target)
        self._require_writable(timeout)
        self._require_ready(target, timeout, session=session)
        return self._call(
            wire_request.AgentPromptRequest,
            wire_request.AgentPromptParams(target=target, text=text),
            timeout=timeout,
            guarded=True,
        )

    def send_keys(
        self,
        target: PaneId,
        *keys: str,
        session: SessionId | None = None,
        timeout: float | None = None,
    ) -> JsonObject:
        """Send key presses to an agent (e.g. ``"1"``, ``"enter"``, ``"esc"``, ``"ctrl-c"``)."""
        if not keys:
            raise InvalidRequest("send_keys requires at least one key")
        target = as_pane_id(target)
        self._require_writable(timeout)
        self._require_ready(
            target,
            timeout,
            session=session,
            interrupting=all(key == INTERRUPT_KEY for key in keys),
        )
        return self._call(
            wire_request.AgentSendKeysRequest,
            wire_request.AgentSendKeysParams(target=target, keys=list(keys)),
            timeout=timeout,
            guarded=True,
        )

    def focus(self, target: PaneId, *, timeout: float | None = None) -> JsonObject:
        """Focus an agent's pane in the running session."""
        return self._call(
            wire_request.AgentFocusRequest,
            wire_request.AgentTarget(target=as_pane_id(target)),
            timeout=timeout,
        )

    # ----------------------------------------------------------------------
    # Lifecycle: the two writes that make a herd able to shrink as well as grow
    # ----------------------------------------------------------------------

    def shell_pane(self, pane_id: str, *, timeout: float | None = None) -> bool:
        """Is this pane a settled shell, ready for an agent to be started in it?"""
        _, payload = self._result(
            self._call(
                wire_request.PaneProcessInfoRequest,
                wire_request.PaneProcessInfoParams(pane_id=pane_id),
                timeout=timeout,
            ),
            wire.PaneProcessInfoResponseResult,
            method="pane.process_info",
            degrade=False,
        )
        shell = payload.get("shell_pid")
        running = objects(payload.get("foreground_processes"))
        return bool(shell) and [p.get("pid") for p in running] == [shell]

    def pane_pids(self, pane_id: str, *, timeout: float | None = None) -> tuple[int, ...]:
        """Every process in this pane, shell first. A runner that will not say answers `()`."""
        _, payload = self._result(
            self._call(
                wire_request.PaneProcessInfoRequest,
                wire_request.PaneProcessInfoParams(pane_id=pane_id),
                timeout=timeout,
            ),
            wire.PaneProcessInfoResponseResult,
            method="pane.process_info",
            degrade=True,
        )
        shell = payload.get("shell_pid")
        pids = [shell] if isinstance(shell, int) else []
        for process in objects(payload.get("foreground_processes")):
            pid = process.get("pid")
            if isinstance(pid, int):
                pids.append(pid)
        return tuple(dict.fromkeys(pids))

    def create_tab(
        self,
        *,
        workspace_id: str,
        cwd: str,
        label: str,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> str:
        """Make a new tab with one shell pane in it, and return its root pane."""
        payload = self._call(
            wire_request.TabCreateRequest,
            wire_request.TabCreateParams(
                workspace_id=workspace_id,
                cwd=cwd,
                label=label,
                focus=False,
                env=dict(env) if env else None,
            ),
            timeout=timeout,
        )
        arm = self._written(payload, wire.TabCreatedResponseResult, method="tab.create")
        return arm.root_pane.pane_id

    def start_agent(
        self,
        *,
        name: str,
        kind: str,
        pane_id: str,
        args: tuple[str, ...] = (),
        timeout: float | None = None,
    ) -> StartedAgent:
        """Start an agent in a pane that already holds a settled shell."""
        self._require_writable(timeout)
        self._wait_for_shell(pane_id, timeout)
        params = wire_request.AgentStartParams(
            name=name,
            kind=kind,
            pane_id=pane_id,
            args=list(args) or None,
            timeout_ms=START_TIMEOUT_MS,
        )
        payload = self._start_with_retries(wire_request.AgentStartRequest, params, timeout)
        arm = self._written(payload, wire.AgentStartedResponseResult, method="agent.start")
        return self._started(arm.agent)

    @staticmethod
    def _started(info: "wire.AgentInfo") -> StartedAgent:
        """One ``AgentInfo`` as the three facts the create flow acts on."""
        holder = info.agent_session.value if info.agent_session else ""
        return StartedAgent(
            pane=info.pane_id,
            session=str(holder or "").strip(),
            status=str(info.agent_status),
            ready=not info.launch_pending and info.interactive_ready is not False,
        )

    def started_agent(self, pane_id: str, *, timeout: float | None = None) -> StartedAgent:
        """Re-read a just-started agent, to see whether it has a session UUID yet."""
        root, _ = self._agent_result(as_pane_id(pane_id), timeout=timeout, degrade=False)
        if root is None:  # pragma: no cover - degrade=False cannot return None
            return StartedAgent(pane=pane_id, session="", status="unknown")
        return self._started(root.agent)

    def close_pane(
        self,
        target: PaneId,
        *,
        session: SessionId | None = None,
        timeout: float | None = None,
    ) -> JsonObject:
        """End an agent by closing the pane it runs in -- the app's one no-undo write."""
        target = as_pane_id(target)
        self._require_writable(timeout)
        if session is not None:
            root, _ = self._agent_result(target, timeout=timeout, degrade=False)
            if root is not None:
                self._require_session(target, root.agent, session)
        return self._call(
            wire_request.PaneCloseRequest,
            wire_request.PaneTarget(pane_id=str(target)),
            timeout=timeout,
            guarded=True,
        )

    # ----------------------------------------------------------------------
    # Workspaces: the container the herd is grouped by
    # ----------------------------------------------------------------------

    def create_workspace(
        self, *, label: str = "", cwd: str = "", timeout: float | None = None
    ) -> Workspace:
        """Make an empty workspace and hand it back -- no tab is filled, no agent starts."""
        payload = self._call(
            wire_request.WorkspaceCreateRequest,
            wire_request.WorkspaceCreateParams(label=label or None, cwd=cwd or None, focus=False),
            timeout=timeout,
        )
        arm = self._written(payload, wire.WorkspaceCreatedResponseResult, method="workspace.create")
        return Workspace.from_raw(arm.workspace.model_dump(mode="json"))

    def rename_workspace(
        self, workspace_id: str, label: str, *, timeout: float | None = None
    ) -> JsonObject:
        """Relabel a workspace. Nothing in it is touched; only what it is called."""
        return self._call(
            wire_request.WorkspaceRenameRequest,
            wire_request.WorkspaceRenameParams(workspace_id=workspace_id, label=label),
            timeout=timeout,
        )

    def close_workspace(self, workspace_id: str, *, timeout: float | None = None) -> JsonObject:
        """End every agent in a workspace at once -- the largest write this app makes."""
        return self._call(
            wire_request.WorkspaceCloseRequest,
            wire_request.WorkspaceCloseParams(workspace_id=workspace_id),
            timeout=timeout,
        )

    # -- the two halves of start_agent, kept out of it for readability ------

    def _wait_for_shell(self, pane_id: str, timeout: float | None) -> None:
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
    ) -> JsonObject:
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
