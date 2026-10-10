"""The typed verbs: what this app is allowed to ask the runner for."""

import logging
import threading
import time
from collections.abc import Sequence
from functools import cache
from pathlib import Path
from typing import Any, Final, NamedTuple, Protocol, TypeVar, get_args

from pydantic import BaseModel, ValidationError

from herdr_pwa import panetext
from herdr_pwa import settings as _settings
from herdr_pwa.engine.generated import request as wire_request
from herdr_pwa.engine.generated import success_response as wire
from herdr_pwa.engine.model import (
    Agent,
    BlockedPrompt,
    PaneId,
    SessionId,
    Snapshot,
    Workspace,
    as_pane_id,
)
from herdr_pwa.engine.transport import (
    EngineError,
    EngineMisconfigured,
    EngineTimeout,
    EngineUnavailable,
    EventStream,
    InvalidRequest,
    InvalidResponse,
    ProtocolMismatch,
    RequestTooLarge,
    SocketTransport,
    TargetNotFound,
    Transport,
)
from herdr_pwa.protocol import WRITE_METHODS
from herdr_pwa.settings import SECONDS_PER_RECENT_LINE

__all__ = [
    "INTERRUPT_KEY",
    "NOT_READY_CODE",
    "PANE_BUSY_CODE",
    "SESSION_MOVED_CODE",
    "START_TIMEOUT_MS",
    "EngineError",
    "EngineMisconfigured",
    "EngineSettings",
    "EngineTimeout",
    "EngineUnavailable",
    "EventStream",
    "HerdrClient",
    "InvalidRequest",
    "InvalidResponse",
    "ProtocolMismatch",
    "RequestTooLarge",
    "StartedAgent",
    "TargetNotFound",
    "Transport",
    "agent",
    "agents",
    "blocked_prompt",
    "default_client",
    "discovery_subscriptions",
    "focus",
    "get_client",
    "prompt",
    "read",
    "send_keys",
    "snapshot",
    "status_subscriptions",
]

log = logging.getLogger(__name__)

# @doc: docs/modules/engine/client.md


def discovery_subscriptions() -> list[dict[str, Any]]:
    """The three pane events that take no `pane_id`, so no roster can stale them.

    @doc: docs/modules/engine/client/discovery_subscriptions.md
    """
    return [
        wire_request.PaneCreatedSubscription(type="pane.created").model_dump(),
        wire_request.PaneAgentDetectedSubscription(
            type="pane.agent_detected"
        ).model_dump(),
        wire_request.PaneClosedSubscription(type="pane.closed").model_dump(),
    ]


def status_subscriptions(panes: Sequence[str | PaneId]) -> list[dict[str, Any]]:
    """One status subscription per pane, because the runner offers no wildcard.

    @doc: docs/modules/engine/client/status_subscriptions.md
    """
    return [
        wire_request.PaneAgentStatusChangedSubscription(
            type="pane.agent_status_changed", pane_id=str(pane)
        ).model_dump(exclude_none=True)
        for pane in dict.fromkeys(str(pane) for pane in panes)
    ]


class EngineSettings(Protocol):
    """The settings surface this module reads, and the whole of it."""

    @property
    def socket_path(self) -> Path: ...

    @property
    def command_timeout(self) -> float: ...

    @property
    def expected_protocol(self) -> int: ...

    @property
    def protocol_max_age(self) -> float: ...

    @property
    def protocol_drift_fails_writes(self) -> bool: ...

    @property
    def read_source(self) -> str: ...

    @property
    def read_lines(self) -> int: ...

    @property
    def recent_max_lines(self) -> int: ...


_BLOCKED: Final = "blocked"
"""The one status whose mirror is allowed to prefer the detection source."""

NOT_READY_CODE: Final = "agent_not_ready"
"""Reason code for "that agent is not listening yet"."""

INTERRUPT_KEY: Final = "ctrl-c"
"""The one key that means "stop", and so the one key a busy agent cannot refuse."""

SESSION_MOVED_CODE: Final = "session_moved"
"""Reason code for "that pane belongs to somebody else now"."""

PANE_BUSY_CODE: Final = "agent_pane_busy"
"""Reason code for "that pane is not a shell sitting at a prompt"."""

_START_RETRIES: Final = 3
"""How many times a start is retried after `PANE_BUSY_CODE`."""

_START_BACKOFF: Final = 0.3
"""How long a retried start waits between attempts."""

_SHELL_POLLS: Final = 20
"""How many times the readiness gate polls a new pane before refusing the start."""

_SHELL_INTERVAL: Final = 0.1
"""How long the readiness gate waits between polls."""

START_TIMEOUT_MS: Final = 10_000
"""`agent.start`'s own timeout, in milliseconds."""

_START_BUDGET: Final = START_TIMEOUT_MS / 1000 + 3.0
"""The socket budget for a start, longer than the launch happening at the other end."""

_Arm = TypeVar("_Arm", bound=BaseModel)

_UNSENT: Final = ""
"""Placeholder for the `id` of a request being validated."""


@cache
def _method_of(request_type: type[BaseModel]) -> str:
    """The dotted method name herdr's schema pinned onto this request type."""
    annotation = request_type.model_fields["method"].annotation
    names = [arg for arg in get_args(annotation) if isinstance(arg, str)]
    if len(names) != 1:
        raise InvalidResponse(
            f"{request_type.__name__} does not pin exactly one method name; "
            "the generated models are not the shape this build expects"
        )
    return names[0]


@cache
def _payload_field(arm: type[BaseModel]) -> str:
    """The one field of a result arm that is not its ``type`` discriminator."""
    fields = [name for name in arm.model_fields if name != "type"]
    if len(fields) != 1:
        raise InvalidResponse(
            f"{arm.__name__} does not carry exactly one payload field; "
            "the generated models are not the shape this build expects"
        )
    return fields[0]


_TEXT_FIELD: Final = "text"
"""The field of `PaneReadResult` holding terminal content."""

_EXPLAIN_FIELD: Final = "explain"
"""The field of `AgentExplainResult` holding the detection report."""


class StartedAgent(NamedTuple):
    """What starting an agent actually told us, in this app's own words."""

    pane: str
    """The pane the agent was started in. Always known; a start that failed
    """
    session: str
    """The provider's session UUID, or `""` when it has not registered one."""
    status: str
    """herdr's agent status at the instant of the start. `blocked` here is
    """
    ready: bool = False
    """Whether herdr says the agent is reading its terminal *now*.

    @doc: docs/modules/engine/client/StartedAgent.md
    """


NO_SESSION: Final[SessionId] = SessionId("")
"""The sentinel meaning "no session asserted"."""


class HerdrClient:
    """One configured view of one agent runner, with its own protocol memory."""

    def __init__(
        self,
        settings: EngineSettings | None = None,
        *,
        transport: Transport | None = None,
    ) -> None:
        self.settings: EngineSettings = (
            settings if settings is not None else _settings_from_environment()
        )
        """Injected, never imported from a module-level singleton: that is what
        """
        self.transport: Transport = (
            transport
            if transport is not None
            else SocketTransport(
                self.settings.socket_path, timeout=self.settings.command_timeout
            )
        )
        """The wire. Injected for the same reason as the settings, and the seam
        """
        self.last_protocol: int | None = None
        """Last protocol actually observed, used to fail writes closed.
        """
        self.last_protocol_at: float = 0.0
        """`time.monotonic()` when `last_protocol` was observed. Pairs
        """
        self.warned_protocol: int | None = None
        """Protocol the drift warning has already been logged for, so a poll
        """
        self._refreshing = threading.local()
        """Per-thread "this thread is already inside the protocol refresh" flag.
        """

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
    ) -> dict[str, Any]:
        """Issue one request, with the write guard applied first."""
        method = _method_of(request_type)
        writes = method in WRITE_METHODS
        if writes and not guarded:
            self._require_writable(timeout)
        frame = request_type.model_validate(
            {"id": _UNSENT, "method": method, "params": params}
        )
        return self.transport.call(
            method,
            frame.model_dump(mode="json", exclude_none=True)["params"],
            timeout=self._resolve_timeout(timeout),
            idempotent=not writes,
        )

    def _resolve_timeout(self, timeout: float | None) -> float:
        return self.settings.command_timeout if timeout is None else timeout

    def _result(
        self,
        payload: dict[str, Any],
        arm: type[_Arm],
        *,
        method: str,
        degrade: bool,
    ) -> tuple[_Arm | None, dict[str, Any]]:
        """Validate a reply against herdr's schema; return its arm and its payload."""
        field = _payload_field(arm)
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
                f"{method} answered {type(root).__name__} where "
                f"{arm.__name__} was expected"
            )
        return root, getattr(root, field).model_dump(mode="json", exclude_none=True)

    def _arm(
        self,
        payload: dict[str, Any],
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
                f"{method} answered {type(root).__name__} where "
                f"{arm.__name__} was expected"
            )
        return root

    @staticmethod
    def _sub_object(
        payload: dict[str, Any], key: str, *, method: str
    ) -> dict[str, Any]:
        """The raw ``result.<key>`` object, for a frame that would not validate."""
        result = payload.get("result")
        raw = result.get(key) if isinstance(result, dict) else None
        if not isinstance(raw, dict):
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
            method=_method_of(wire_request.SessionSnapshotRequest),
            degrade=True,
        )
        snap = Snapshot.from_raw(raw, expected_protocol=self.settings.expected_protocol)
        self._remember_protocol(snap.protocol)
        return snap

    def watch_statuses(self, panes: Sequence[str | PaneId]) -> EventStream:
        """A stream of the status transitions on `panes`, on a connection of its own.

        @doc: docs/modules/engine/client/HerdrClient.watch_statuses.md
        """
        return EventStream(
            self.settings.socket_path,
            subscriptions=status_subscriptions(panes) + discovery_subscriptions(),
            timeout=self.settings.command_timeout,
        )

    def _remember_protocol(self, protocol: int) -> None:
        """Record what the runner just proved it speaks, and warn once on drift."""
        self.last_protocol = protocol
        self.last_protocol_at = time.monotonic()
        if protocol != self.settings.expected_protocol and (
            self.warned_protocol != protocol
        ):
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
    ) -> tuple[wire.AgentInfoResponseResult | None, dict[str, Any]]:
        payload = self._call(
            wire_request.AgentGetRequest,
            wire_request.AgentTarget(target=target),
            timeout=timeout,
        )
        return self._result(
            payload,
            wire.AgentInfoResponseResult,
            method=_method_of(wire_request.AgentGetRequest),
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
        where = self._read_source(
            self.settings.read_source if source is None else source
        )
        if where is wire_request.ReadSource.recent and lines > (
            self.settings.recent_max_lines
        ):
            # Not only cost: past the viewport this scrolls the operator's own
            # pane. @doc: docs/modules/engine/client/HerdrClient.read.md
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
                format=(
                    wire_request.ReadFormat.ansi
                    if colour
                    else wire_request.ReadFormat.text
                ),
                # Belt and braces: the format says what to render, this says
                # not to sanitise it afterwards. Passed explicitly rather than
                # left to the schema default, which is True.
                strip_ansi=not colour,
            ),
            timeout=timeout,
        )
        _, raw = self._result(
            payload,
            wire.PaneReadResponseResult,
            method=_method_of(wire_request.AgentReadRequest),
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
            raise InvalidRequest(
                f"unknown read source {source!r}; expected {allowed}"
            ) from exc

    def mirror_read(
        self,
        target: PaneId,
        *,
        status: object = "",
        lines: int | None = None,
        timeout: float | None = None,
    ) -> str:
        """The pane read the drill-in mirror renders.

        Args:
            status: the agent's status, in any spelling
                `herdr_pwa.engine.model.AgentStatus` accepts. Anything that
                is not `blocked` takes the visible path directly.

        """
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

    def explain(
        self, target: PaneId, *, timeout: float | None = None
    ) -> dict[str, Any]:
        """Which detection rule decided this agent's state, on what evidence.

        @doc: docs/modules/engine/client/HerdrClient.explain.md
        """
        method = _method_of(wire_request.AgentExplainRequest)
        payload = self._call(
            wire_request.AgentExplainRequest,
            wire_request.AgentTarget(target=as_pane_id(target)),
            timeout=timeout,
        )
        return self._sub_object(payload, _EXPLAIN_FIELD, method=method)

    def agent_frame(
        self, target: PaneId, *, timeout: float | None = None
    ) -> dict[str, Any]:
        """The whole `agent.get` reply as the runner sent it, normalized by nothing.

        @doc: docs/modules/engine/client/HerdrClient.agent_frame.md
        """
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
            and (time.monotonic() - self.last_protocol_at)
            <= self.settings.protocol_max_age
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
        # Launching AND blocked is not "not reading yet" -- it is an
        # agent that stopped during startup to ask something, and a keystroke
        # is the whole point. @doc: docs/modules/engine/client/HerdrClient._require_ready.md
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
    def _require_session(
        target: PaneId, info: wire.AgentInfo, session: SessionId | None
    ) -> None:
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
    ) -> dict[str, Any]:
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
    ) -> dict[str, Any]:
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

    def focus(self, target: PaneId, *, timeout: float | None = None) -> dict[str, Any]:
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
        running = payload.get("foreground_processes") or []
        return bool(shell) and [p.get("pid") for p in running] == [shell]

    def pane_pids(
        self, pane_id: str, *, timeout: float | None = None
    ) -> tuple[int, ...]:
        """Every process in this pane, shell first. A runner that will not say answers `()`.

        @doc: docs/modules/engine/client/HerdrClient.pane_pids.md
        """
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
        running = payload.get("foreground_processes") or []
        pids = [shell] if isinstance(shell, int) else []
        pids += [
            p.get("pid")
            for p in running
            if isinstance(p, dict) and isinstance(p.get("pid"), int)
        ]
        return tuple(dict.fromkeys(pid for pid in pids if isinstance(pid, int)))

    def create_tab(
        self,
        *,
        workspace_id: str,
        cwd: str,
        label: str,
        timeout: float | None = None,
    ) -> str:
        """Make a new tab with one shell pane in it, and return its root pane."""
        payload = self._call(
            wire_request.TabCreateRequest,
            wire_request.TabCreateParams(
                workspace_id=workspace_id, cwd=cwd, label=label, focus=False
            ),
            timeout=timeout,
        )
        arm = self._arm(payload, wire.TabCreatedResponseResult, method="tab.create")
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
        payload = self._start_with_retries(
            wire_request.AgentStartRequest, params, timeout
        )
        arm = self._arm(payload, wire.AgentStartedResponseResult, method="agent.start")
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

    def started_agent(
        self, pane_id: str, *, timeout: float | None = None
    ) -> StartedAgent:
        """Re-read a just-started agent, to see whether it has a session UUID yet."""
        root, _ = self._agent_result(
            as_pane_id(pane_id), timeout=timeout, degrade=False
        )
        if root is None:  # pragma: no cover - degrade=False cannot return None
            return StartedAgent(pane=pane_id, session="", status="unknown")
        return self._started(root.agent)

    def close_pane(
        self,
        target: PaneId,
        *,
        session: SessionId | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
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
    # @doc: docs/modules/engine/client/workspaces.md
    # ----------------------------------------------------------------------

    def create_workspace(
        self, *, label: str = "", cwd: str = "", timeout: float | None = None
    ) -> Workspace:
        """Make an empty workspace and hand it back -- no tab is filled, no agent starts."""
        payload = self._call(
            wire_request.WorkspaceCreateRequest,
            wire_request.WorkspaceCreateParams(
                label=label or None, cwd=cwd or None, focus=False
            ),
            timeout=timeout,
        )
        arm = self._arm(
            payload, wire.WorkspaceCreatedResponseResult, method="workspace.create"
        )
        return Workspace.from_raw(arm.workspace.model_dump(mode="json"))

    def rename_workspace(
        self, workspace_id: str, label: str, *, timeout: float | None = None
    ) -> dict[str, Any]:
        """Relabel a workspace. Nothing in it is touched; only what it is called."""
        return self._call(
            wire_request.WorkspaceRenameRequest,
            wire_request.WorkspaceRenameParams(workspace_id=workspace_id, label=label),
            timeout=timeout,
        )

    def close_workspace(
        self, workspace_id: str, *, timeout: float | None = None
    ) -> dict[str, Any]:
        """End every agent in a workspace at once -- the largest write this app makes."""
        return self._call(
            wire_request.WorkspaceCloseRequest,
            wire_request.WorkspaceTarget(workspace_id=workspace_id),
            timeout=timeout,
        )

    # -- the two halves of start_agent, kept out of it for readability ------

    def _wait_for_shell(self, pane_id: str, timeout: float | None) -> None:
        """Block until the new pane is a settled shell, or refuse the start."""
        for attempt in range(_SHELL_POLLS):
            if self.shell_pane(pane_id, timeout=timeout):
                return
            if attempt + 1 < _SHELL_POLLS:
                time.sleep(_SHELL_INTERVAL)
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
    ) -> dict[str, Any]:
        """``agent.start``, retried on ``agent_pane_busy`` and on nothing else."""
        budget = _START_BUDGET if timeout is None else timeout
        last: EngineError | None = None
        for attempt in range(_START_RETRIES):
            try:
                return self._call(request_type, params, timeout=budget, guarded=True)
            except EngineError as exc:
                if getattr(exc, "code", "") != PANE_BUSY_CODE:
                    raise
                last = exc
                if attempt + 1 < _START_RETRIES:
                    time.sleep(_START_BACKOFF)
        raise last if last is not None else EngineError("agent.start never ran")


# --------------------------------------------------------------------------
# The default instance, and the verbs that delegate to it
# --------------------------------------------------------------------------


def _settings_from_environment() -> EngineSettings:
    """Build the settings nobody injected, turning a bad box into a typed failure."""
    try:
        return _settings.EngineSettings()
    except ValidationError as exc:
        raise EngineMisconfigured(
            "this box's configuration does not load, so no engine client can be "
            f"built: {exc}. Fix the HP_ variable or the .env file it names -- "
            "only the engine's own fields are read here (the runner socket, its "
            "timeout and the protocol fields), so a bad bind address or a bad "
            "push cadence cannot be what stopped you"
        ) from exc


@cache
def default_client() -> HerdrClient:
    """The process-wide client the module-level verbs use."""
    return HerdrClient()


def get_client() -> HerdrClient:
    """FastAPI dependency: ``client: HerdrClient = Depends(get_client)``."""
    return default_client()


def snapshot(*, timeout: float | None = None) -> Snapshot:
    """Fetch the whole session state -- see :meth:`HerdrClient.snapshot`."""
    return default_client().snapshot(timeout=timeout)


def agents(*, timeout: float | None = None) -> list[Agent]:
    """Every live agent, most urgent first -- see :meth:`HerdrClient.agents`."""
    return default_client().agents(timeout=timeout)


def agent(target: PaneId, *, timeout: float | None = None) -> Agent:
    """One agent by pane address -- see :meth:`HerdrClient.agent`."""
    return default_client().agent(target, timeout=timeout)


def read(
    target: PaneId,
    *,
    lines: int | None = None,
    source: str | None = None,
    colour: bool = False,
    timeout: float | None = None,
) -> str:
    """Read an agent's terminal output -- see :meth:`HerdrClient.read`."""
    return default_client().read(
        target, lines=lines, source=source, colour=colour, timeout=timeout
    )


def blocked_prompt(
    target: PaneId,
    *,
    session: SessionId = NO_SESSION,
    lines: int | None = None,
    timeout: float | None = None,
) -> BlockedPrompt:
    """What a blocked agent is waiting on -- see :meth:`HerdrClient.blocked_prompt`."""
    return default_client().blocked_prompt(
        target, session=session, lines=lines, timeout=timeout
    )


def prompt(
    target: PaneId,
    text: str,
    *,
    session: SessionId | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Submit a prompt to an agent -- see :meth:`HerdrClient.prompt`."""
    return default_client().prompt(target, text, session=session, timeout=timeout)


def send_keys(
    target: PaneId,
    *keys: str,
    session: SessionId | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Send key presses to an agent -- see :meth:`HerdrClient.send_keys`."""
    return default_client().send_keys(target, *keys, session=session, timeout=timeout)


def focus(target: PaneId, *, timeout: float | None = None) -> dict[str, Any]:
    """Focus an agent's pane -- see :meth:`HerdrClient.focus`."""
    return default_client().focus(target, timeout=timeout)
