"""Typed shapes the rest of herdr-pwa renders from."""

import hashlib
import re
from collections import Counter
from enum import StrEnum
from typing import Any, NewType, Self

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from herdr_pwa.protocol import EXPECTED_PROTOCOL as _EXPECTED_PROTOCOL

__all__ = [
    "EXPECTED_PROTOCOL",
    "Agent",
    "AgentStatus",
    "BlockedPrompt",
    "KeyHint",
    "MenuShape",
    "PaneId",
    "Presence",
    "PromptOption",
    "SessionId",
    "SessionResolution",
    "Snapshot",
    "Stepper",
    "Workspace",
    "as_pane_id",
    "as_session_id",
    "rank",
    "screen_digest",
]


def screen_digest(screen: str) -> str:
    """A change detector for one rendered terminal screen."""
    return hashlib.sha256(screen.encode("utf-8", "replace")).hexdigest()[:16]


EXPECTED_PROTOCOL = _EXPECTED_PROTOCOL
"""Snapshot protocol this build was written against.
"""


SessionId = NewType("SessionId", str)
"""**Identity.** The provider's session id: which agent this *is*.
"""

PaneId = NewType("PaneId", str)
"""**Address.** The pane id (``w1:p3``): where the agent can be *reached* right now.
"""

_PANE_SHAPE = re.compile(r"^[^\s:]+:[^\s:]+$")
"""A pane id is ``<workspace>:<pane>``; a session id never contains a colon.
"""

_UUID_SHAPE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
"""The canonical 8-4-4-4-12 UUID a claude session is -- one shape among several."""


def as_session_id(value: object) -> SessionId:
    """Tag an untrusted string (a URL path segment, a notification payload) as identity."""
    text = str(value or "").strip()
    if _PANE_SHAPE.match(text):
        raise ValueError(
            f"{text!r} is a pane id (an address), not a session id (an identity); "
            "pane ids are recycled and must never be used to identify an agent"
        )
    return SessionId(text)


def as_pane_id(value: object) -> PaneId:
    """Tag an untrusted string as an address to drive *this tick*."""
    text = str(value or "").strip()
    if _UUID_SHAPE.match(text):
        raise ValueError(
            f"{text!r} is a session id (an identity), not a pane id (an address); "
            "resolve it against a fresh snapshot with Snapshot.resolve_session first"
        )
    return PaneId(text)


class AgentStatus(StrEnum):
    """Lifecycle state of an agent."""

    IDLE = "idle"
    WORKING = "working"
    BLOCKED = "blocked"
    DONE = "done"
    UNKNOWN = "unknown"

    @classmethod
    def coerce(cls, value: object) -> "AgentStatus":
        """Map any value onto the enum, falling back to :attr:`UNKNOWN`."""
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError:
            return cls.UNKNOWN


_RANKS: dict[AgentStatus, int] = {
    AgentStatus.BLOCKED: 0,
    AgentStatus.DONE: 1,
    AgentStatus.WORKING: 2,
    AgentStatus.IDLE: 3,
    AgentStatus.UNKNOWN: 4,
}
"""Urgency ordering — lower sorts to the top of the feed.
"""


def rank(status: object) -> int:
    """Urgency rank: blocked=0, done=1, working=2, idle=3, unknown=4 (lower = more urgent)."""
    # @doc: docs/modules/engine/model.md
    return _RANKS[AgentStatus.coerce(status)]


class Workspace(BaseModel):
    """A runner workspace — the human-named grouping an agent lives in."""

    model_config = ConfigDict(frozen=True)

    workspace_id: str
    label: str = ""
    number: int | None = None
    status: AgentStatus = AgentStatus.UNKNOWN
    focused: bool = False
    pane_count: int = 0
    tab_count: int = 0

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> Self:
        return cls(
            workspace_id=str(raw.get("workspace_id", "")),
            label=str(raw.get("label") or ""),
            number=raw.get("number"),
            status=AgentStatus.coerce(raw.get("agent_status")),
            focused=bool(raw.get("focused", False)),
            pane_count=int(raw.get("pane_count") or 0),
            tab_count=int(raw.get("tab_count") or 0),
        )


def _shorter_title(raw_title: object, stripped: object) -> str:
    """Pick between herdr's two TERMINAL title fields: the SHORTER one trimmed."""
    # @doc: docs/modules/engine/model/_shorter_title.md
    candidates = [
        text
        for text in (str(stripped or "").strip(), str(raw_title or "").strip())
        if text
    ]
    if not candidates:
        return ""
    return min(candidates, key=len)


def _chosen_name(raw: dict[str, Any], pane: dict[str, Any] | None) -> str:
    """What to call this pane: the most deliberate name anyone gave it."""
    # @doc: docs/modules/engine/model/_chosen_name.md
    for text in (
        (pane or {}).get("label"),
        raw.get("title") or (pane or {}).get("title"),
        raw.get("name"),
    ):
        chosen = str(text or "").strip()
        if chosen:
            return chosen
    return _shorter_title(raw.get("terminal_title"), raw.get("terminal_title_stripped"))


def _coerce_session(raw: object) -> tuple[str, str]:
    """Normalize the ``agent_session`` field into ``(session_id, provider_kind)``."""
    if isinstance(raw, str):
        return raw.strip(), ""
    if isinstance(raw, dict):
        return str(raw.get("value") or "").strip(), str(raw.get("agent") or "")
    return "", ""


class Agent(BaseModel):
    """One agent, normalized and joined to its workspace label."""

    model_config = ConfigDict(frozen=True)

    session: SessionId = Field(
        description="Provider session id — the stable identity (see SessionId)."
    )
    target: PaneId = Field(
        description="Pane id: the address the runner is driven by (see PaneId)."
    )
    title: str = Field(
        default="",
        description="What to call this pane — the most deliberate name anyone "
        "gave it (see _chosen_name), glyphs stripped.",
    )
    status: AgentStatus = AgentStatus.UNKNOWN
    agent: str = Field(default="", description="Provider kind, e.g. 'claude'.")
    cwd: str = ""
    foreground_cwd: str = ""
    workspace: str = Field(
        default="", description="Workspace label, joined by workspace_id."
    )
    workspace_id: str = ""
    workspace_number: int | None = None
    tab_id: str = ""
    focused: bool = Field(
        default=False,
        description=(
            "Front pane OF ITS TAB, per herdr — not the one globally focused "
            "pane, which is Snapshot.focused_pane_id. Several agents carry it."
        ),
    )
    revision: int = 0
    state_change_seq: int = 0

    @computed_field  # type: ignore
    @property
    def rank(self) -> int:
        """Urgency rank; lower is more urgent. Templates sort and style on this."""
        return rank(self.status)

    def feed_key(self, urgency: int | None = None) -> tuple[int, int, int, str]:
        """The feed's whole order; ``urgency`` replaces the rank and nothing else."""
        return (
            self.rank if urgency is None else urgency,
            -self.state_change_seq,
            self.workspace_number or 0,
            self.target,
        )

    @property
    def sort_key(self) -> tuple[int, int, int, str]:
        """Deterministic feed order: urgency, recency, then workspace, then pane."""
        # @doc: docs/modules/engine/model.md
        return self.feed_key()

    @property
    def project(self) -> str:
        """Basename of ``cwd`` — what a phone-width card can actually show."""
        return self.cwd.rstrip("/").rpartition("/")[2]

    @classmethod
    def from_raw(
        cls,
        raw: dict[str, Any],
        workspaces: dict[str, Workspace] | None = None,
        panes: dict[str, dict[str, Any]] | None = None,
    ) -> Self:
        """Normalize one snapshot ``agents[]`` entry, joining workspace and pane."""
        workspace_id = str(raw.get("workspace_id") or "")
        ws = (workspaces or {}).get(workspace_id)
        pane_id = str(raw.get("pane_id") or "")
        session_id, session_agent = _coerce_session(raw.get("agent_session"))
        return cls(
            session=SessionId(session_id),
            target=PaneId(pane_id),
            title=_chosen_name(raw, (panes or {}).get(pane_id)),
            status=AgentStatus.coerce(raw.get("agent_status")),
            agent=str(raw.get("agent") or session_agent),
            cwd=str(raw.get("cwd") or ""),
            foreground_cwd=str(raw.get("foreground_cwd") or raw.get("cwd") or ""),
            workspace=ws.label if ws else "",
            workspace_id=workspace_id,
            workspace_number=ws.number if ws else None,
            tab_id=str(raw.get("tab_id") or ""),
            focused=bool(raw.get("focused", False)),
            revision=int(raw.get("revision") or 0),
            state_change_seq=int(raw.get("state_change_seq") or 0),
        )


class Presence(StrEnum):
    """What became of a session between when it was captured and now."""

    HERE = "here"
    """Same session, same pane. Safe to render and safe to drive."""
    MOVED = "moved"
    """Same session, different pane — the agent is alive at a new address. A
    """
    GONE = "gone"
    """The session is not in this snapshot at all. Whatever now occupies the old
    """


class SessionResolution(BaseModel):
    """The answer to "is this still the agent I was looking at?"."""

    model_config = ConfigDict(frozen=True)

    session: SessionId
    presence: Presence
    expected_target: PaneId | None = Field(
        default=None, description="The address this session was last seen at, if known."
    )
    agent: Agent | None = Field(
        default=None, description="The agent this session resolves to now, if alive."
    )
    occupant: Agent | None = Field(
        default=None,
        description="Whoever holds ``expected_target`` now — a different agent "
        "whenever this is set and ``presence`` is not HERE.",
    )

    @property
    def ok(self) -> bool:
        """True only when the session is alive *and* still at the expected pane."""
        return self.presence is Presence.HERE

    @property
    def target(self) -> PaneId | None:
        """The address to drive right now, or ``None`` when there is nothing to drive."""
        return self.agent.target if self.agent is not None else None

    @property
    def message(self) -> str:
        """One line a drill-in or a tapped notification can show instead of guessing."""
        if self.presence is Presence.HERE:
            return ""
        if self.presence is Presence.MOVED:
            return (
                f"This agent moved from pane {self.expected_target} to {self.target}."
            )
        if self.occupant is not None:
            return (
                f"This agent is gone. Pane {self.expected_target} now belongs to "
                f"a different agent."
            )
        return "This agent is gone."


class Snapshot(BaseModel):
    """A whole session at one instant, agents already ranked most-urgent-first."""

    model_config = ConfigDict(frozen=True)

    protocol: int = 0
    version: str = ""
    expected_protocol: int = EXPECTED_PROTOCOL
    """The protocol *this view* was built to expect. Defaults to the module-level
    """
    agents: tuple[Agent, ...] = ()
    workspaces: tuple[Workspace, ...] = ()
    focused_pane_id: str | None = None
    focused_tab_id: str | None = None
    focused_workspace_id: str | None = None

    @classmethod
    def from_raw(
        cls, raw: dict[str, Any], *, expected_protocol: int | None = None
    ) -> Self:
        """Build from the inner ``snapshot`` object of a runner payload."""
        workspaces = tuple(Workspace.from_raw(w) for w in raw.get("workspaces") or ())
        by_id = {w.workspace_id: w for w in workspaces}
        by_pane = {
            str(p.get("pane_id") or ""): p
            for p in raw.get("panes") or ()
            if isinstance(p, dict)
        }
        agents = sorted(
            (Agent.from_raw(a, by_id, by_pane) for a in raw.get("agents") or ()),
            key=lambda a: a.sort_key,
        )
        return cls(
            protocol=int(raw.get("protocol") or 0),
            version=str(raw.get("version") or ""),
            expected_protocol=(
                EXPECTED_PROTOCOL if expected_protocol is None else expected_protocol
            ),
            agents=tuple(agents),
            workspaces=workspaces,
            focused_pane_id=raw.get("focused_pane_id"),
            focused_tab_id=raw.get("focused_tab_id"),
            focused_workspace_id=raw.get("focused_workspace_id"),
        )

    @property
    def protocol_ok(self) -> bool:
        """False when the runner speaks a protocol this *view* was not built for."""
        return self.protocol == self.expected_protocol

    @property
    def blocked(self) -> tuple[Agent, ...]:
        """Agents that need a human. These are the ones a notification is for."""
        return tuple(a for a in self.agents if a.status is AgentStatus.BLOCKED)

    @property
    def counts(self) -> dict[AgentStatus, int]:
        """Per-status tally for the header ("N working · M need you")."""
        tally = Counter(a.status for a in self.agents)
        return {status: tally.get(status, 0) for status in AgentStatus}

    def by_session(self, session: SessionId) -> Agent | None:
        """Look an agent up by its stable identity."""
        text = str(session)
        if not text.strip():
            return None
        as_session_id(text)  # raises if an address was passed as an identity
        return next((a for a in self.agents if a.session == text), None)

    def by_target(self, target: PaneId) -> Agent | None:
        """Look an agent up by pane address. Verify ``session`` before driving it."""
        text = str(target)
        if not text.strip():
            return None
        as_pane_id(text)  # raises if an identity was passed as an address
        return next((a for a in self.agents if a.target == text), None)

    def resolve_session(
        self,
        session: SessionId,
        *,
        expected_target: PaneId | None = None,
    ) -> SessionResolution:
        """Reconcile a remembered identity against this snapshot."""
        agent = self.by_session(session)
        occupant = (
            self.by_target(expected_target) if expected_target is not None else None
        )
        if agent is None:
            presence = Presence.GONE
        elif expected_target is None or agent.target == expected_target:
            presence = Presence.HERE
        else:
            presence = Presence.MOVED
        return SessionResolution(
            session=session,
            presence=presence,
            expected_target=expected_target,
            agent=agent,
            occupant=occupant if occupant is not agent else None,
        )


_OPTION_RE = re.compile(r"^\s*[>❯▸▶‣*\-•]?\s*(\d{1,2})\s*[.):]\s+(?P<label>\S.*?)\s*$")
"""A leading list marker in detection output: "❯ 1.", "2)", "▸ 3." etc.

The cursor glyphs here must be the ones `_MARKER_RE` knows.
@doc: docs/modules/engine/model/_earned.md
"""

_DIVIDER_RE = re.compile(r"^[\s─━╌╍┄┅┈┉═—=_·•\-]{4,}$")
"""A rule/divider drawn between the question and the options — chrome, never prose.
"""

_FOOTER_RE = re.compile(
    r"(?i)(?:\benter\b|\besc\b|\btab\b|\bctrl\b|↑|↓|←|→|arrow)"
    r".*(?:select|cancel|navigate|confirm|choose|toggle|continue|submit|expand|manage)"
)
"""The key-hint footer a real interactive menu prints under its options.
"""

_MAX_CHROME_PARAGRAPHS = 3
"""How many chrome paragraphs may sit between a dialog's question and its options.
"""

_MIN_MENU_OPTIONS = 2
"""Minimum options in a trailing run before it is believed to be a menu.
"""


_TICKED_BOX = "xX*✔✓"
"""What a terminal draws INSIDE a box to mean ticked: ``[x]``, ``[✔]``, ``[*]``."""

_BLANK_BOX = " ·"
"""What it draws inside the same box to mean empty — including a bare ``[]``."""

_TICKED_GLYPH = "☑☒◼"
"""A ticked box drawn as ONE character, with no brackets around it."""

_BLANK_GLYPH = "☐◻"
"""The same, empty. A deliberately short closed set — see the page."""

_CHECKBOX_RE = re.compile(
    rf"^\s*(?:\[(?P<box>[{_TICKED_BOX}{_BLANK_BOX}]?)\]"
    rf"|(?P<glyph>[{_TICKED_GLYPH}{_BLANK_GLYPH}]))"
    r"\s*(?P<rest>\S.*?)\s*$"
)
"""A checkbox row and its two halves: the state in ``box``/``glyph``, the label in ``rest``."""


def _ticked(label: str) -> tuple[bool | None, str]:
    """Split ``"[✔] autumn"`` into the tick state and the label the terminal drew."""
    match = _CHECKBOX_RE.match(label)
    if match is None:
        return None, label
    mark = match.group("box") or match.group("glyph") or ""
    return bool(mark) and mark in _TICKED_BOX + _TICKED_GLYPH, match.group("rest")


_SELECT_VERBS = (
    "select",
    "choose",
    "pick",
    "confirm",
    "proceed",
    "approve",
    "lock",
)
"""The verbs that mean "Enter takes the row the cursor is on, AND NOTHING ELSE".

@doc: docs/modules/engine/model/_earned.md
"""

_TOGGLE_VERBS = ("toggle", "check", "uncheck", "deselect", "select all")
"""The verbs that mean "this screen keeps state you toggle".
"""

_MARKER_RE = re.compile(r"^\s*[>❯▸▶‣]\s")
"""A selection marker at the head of a menu row.
"""

_STEP_TOKEN_RE = re.compile(
    r"(?P<box>[\u2610\u2612\u2611\u2714\u2713])\s*(?P<label>[^\u2610\u2612\u2611\u2714\u2713\u2190\u2192]*)"
)
"""One ``\u2610 Scope`` token of a stepper strip: the box, then everything up to the next box."""

_STEP_TICKED = "\u2612\u2611"
"""What the strip draws on a question step that ALREADY has an answer."""

_STEP_SUBMIT = "\u2714\u2713"
"""What it draws on its own Submit tab, which is a step but not a question."""

_STEPPER_RE = re.compile(
    r"^\u2190[^\u2190\u2192]*[\u2610\u2612\u2611\u2714\u2713][^\u2190\u2192]*\u2192$"
)
"""The stepper strip Claude draws above a MULTI-STEP question.

@doc: docs/modules/engine/model/_STEPPER_RE.md
"""


_HINT_RE = re.compile(
    r"^\s*(?P<keys>[^\s].{0,29}?)\s+to\s+(?P<verb>[A-Za-z][A-Za-z0-9 /+-]{0,28}?)\s*$"
)
"""One ``<key> to <verb>`` hint out of a footer.
"""

_HINT_SPLIT = re.compile(r"\s*[·•|｜]\s*|\s{3,}")
"""What a terminal separates footer hints with."""

_TRAILING_PAREN_RE = re.compile(r"(?<=\S)\s*\([^()]*\)\s*$")
"""A parenthetical AFTER a hint -- a countdown, never the verb.

The lookbehind is what keeps `(esc to cancel)`, where the parentheses wrap the
whole hint rather than trail it.
@doc: docs/modules/engine/model.md
"""


def _hint_text(segment: str) -> str:
    """One footer piece with its wrapping and any trailing countdown removed."""
    return _TRAILING_PAREN_RE.sub("", segment.strip()).strip(" ()[]")


def _whole_hint(text: str) -> bool:
    """Does *text* parse as ONE hint whose keys are all really keys?

    `_HINT_RE` alone is not enough to answer it. "↑/↓ to select, Enter to
    confirm" matches whole, with `keys` swallowing "↑/↓ to select, Enter" --
    a parse that reads the first verb as a key and loses a hint. Asking whether
    every key piece is a key token is what tells that apart from "esc, q to
    cancel", where the comma really is inside one hint's keys.
    @doc: docs/modules/engine/model.md
    """
    match = _HINT_RE.match(text)
    if match is None:
        return False
    pieces = _key_pieces(match.group("keys"))
    return bool(pieces) and all(_KEY_TOKEN_RE.match(piece) for piece in pieces)


_KEY_PIECE = re.compile(r"\s*/\s*|\s+or\s+|\s*,\s*")
"""What a hint separates the keys of one affordance with: ``↑/↓``, ``esc or q``."""

_KEY_TOKEN_RE = re.compile(
    r"(?i)^(?:(?:ctrl|shift|alt|opt|option|cmd|meta|super)[+-])*"
    r"(?:f\d{1,2}|enter|return|esc|escape|tab|space|spacebar|backspace|del|delete"
    r"|up|down|left|right|arrows?(?:\s+keys?)?|pgup|pgdn|home|end"
    r"|[a-z0-9]|[\u2191\u2193\u2190\u2192\u21b5\u23ce])$"
)
"""A token that NAMES a key, whether or not this app is willing to send it.

@doc: docs/modules/engine/model/_KEY_TOKEN_RE.md
"""

_KEY_ALIASES: dict[str, tuple[str, ...]] = {
    "enter": ("enter",),
    "return": ("enter",),
    "↵": ("enter",),
    "⏎": ("enter",),
    "esc": ("esc",),
    "escape": ("esc",),
    "tab": ("tab",),
    "space": ("space",),
    "spacebar": ("space",),
    "up": ("up",),
    "down": ("down",),
    "left": ("left",),
    "right": ("right",),
    "↑": ("up",),
    "↓": ("down",),
    "←": ("left",),
    "→": ("right",),
    # A generic "arrow keys" advertises the VERTICAL pair only. Left/right are
    # A different affordance -- a row of choices laid out sideways -- and the parser
    # enables them only where the screen advertises ← or → itself.
    "arrow": ("up", "down"),
    "arrows": ("up", "down"),
    "arrow keys": ("up", "down"),
    "arrow key": ("up", "down"),
}
"""The key names this app is willing to send, spelled the way herdr's ``send_keys`` spells them.
"""

_CTRL_RE = re.compile(r"^ctrl[+-]([a-z])$")
"""``ctrl+e`` / ``ctrl-e`` -> ``ctrl-e``. One ASCII letter only."""

_MAX_HINTS = 4
"""How many hints a footer may contribute.
"""

_MAX_FOOTER_LINES = 3
"""How many trailing lines may be read as the footer."""


class MenuShape(StrEnum):
    """What KIND of screen this is -- a claim kept separate from "is it live"."""

    NONE = "none"
    """No menu was recognised at all: free text, or finished transcript."""
    SINGLE_CHOICE = "single-choice"
    """A numbered ``1..n`` run, corroborated, with no toggle evidence anywhere."""
    MULTI_SELECT = "multi-select"
    """Rows that are independently toggled, then confirmed. Stateful, and
    """
    UNKNOWN = "unknown"
    """A live menu whose shape is not one of the above. Recognised as "there is
    """


class KeyHint(BaseModel):
    """One ``<key> to <verb>`` the screen printed under its options."""

    model_config = ConfigDict(frozen=True)

    key: str
    """The key as herdr's ``send_keys`` spells it: ``enter``, ``esc``, ``tab``,
    """
    verb: str
    """The verb the screen printed, lowercased and trimmed. Shown to the user
    """

    @property
    def name(self) -> str:
        """``Enter`` -- the KEY alone, for a button that prints its verb beneath it.

        @doc: docs/modules/engine/model/KeyHint.name.md
        """
        return _key_label(self.key)

    @property
    def label(self) -> str:
        """``Enter to select`` -- the control's accessible name."""
        return f"{self.name} to {self.verb}"


def _key_label(key: str) -> str:
    """A key name as a human reads it on a button: ``ctrl-e`` -> ``Ctrl+E``."""
    if (m := _CTRL_RE.match(key)) is not None:
        return f"Ctrl+{m.group(1).upper()}"
    return key.capitalize()


def _key_pieces(token: str) -> list[str]:
    """A hint's key-side split into the individual keys it names."""
    pieces = (raw.strip().strip(".") for raw in _KEY_PIECE.split(token.strip().lower()))
    return [piece for piece in pieces if piece]


def _keys_in(token: str) -> tuple[str, ...]:
    """Every key name a hint's key-side names, or ``()`` if none is recognised."""
    found: list[str] = []
    for raw in _KEY_PIECE.split(token.strip().lower()):
        piece = raw.strip().strip(".")
        if not piece:
            continue
        if (ctrl := _CTRL_RE.match(piece)) is not None:
            found.append(f"ctrl-{ctrl.group(1)}")
            continue
        found.extend(_KEY_ALIASES.get(piece, ()))
    return tuple(dict.fromkeys(found))


def _is_key_hint(line: str) -> bool:
    """True when a line is the terminal advertising a KEY, not prose.

    @doc: docs/modules/engine/model/_is_key_hint.md
    """
    for segment in _HINT_SPLIT.split(line):
        match = _HINT_RE.match(segment.strip(" ()[]"))
        if match is None:
            continue
        pieces = _key_pieces(match.group("keys"))
        if pieces and all(_KEY_TOKEN_RE.match(piece) for piece in pieces):
            return True
    return False


def _footer_lines(lines: list[str]) -> list[str]:
    """The key-hint footer at the very END of the viewport, and only there."""
    found: list[str] = []
    for line in reversed(lines):
        if not line.strip():
            if found:
                break
            continue
        if _DIVIDER_RE.match(line) or not _is_chrome(line):
            break
        found.append(line)
        if len(found) >= _MAX_FOOTER_LINES:
            break
    return list(reversed(found))


def _segments(line: str) -> list[str]:
    """One footer line as hint-sized pieces, commas used only where they must be.

    A comma is only a separator in a piece that does not parse without one.
    @doc: docs/modules/engine/model.md
    """
    pieces: list[str] = []
    for segment in _HINT_SPLIT.split(line):
        whole = _hint_text(segment)
        if _whole_hint(whole) or "," not in whole:
            pieces.append(segment)
            continue
        pieces.extend(part for part in whole.split(",") if part.strip())
    return pieces


def _hints(footer: list[str]) -> tuple[KeyHint, ...]:
    """Parse ``<key> to <verb>`` out of the footer lines, in reading order."""
    found: list[KeyHint] = []
    seen: set[tuple[str, str]] = set()
    for line in footer:
        for segment in _segments(line):
            if (m := _HINT_RE.match(_hint_text(segment))) is None:
                continue
            verb = " ".join(m.group("verb").lower().split())
            for key in _keys_in(m.group("keys")):
                if (key, verb) in seen:
                    continue
                seen.add((key, verb))
                found.append(KeyHint(key=key, verb=verb))
    return tuple(found[:_MAX_HINTS])


def _is_multi_select(
    lines: list[str],
    run: list[tuple[int, int, str]] | None,
    hints: tuple[KeyHint, ...],
) -> bool:
    """Positive evidence that this screen TOGGLES rather than chooses."""
    if any(v in hint.verb for hint in hints for v in _TOGGLE_VERBS):
        return True
    if run is not None:
        return any(_CHECKBOX_RE.match(label) for _, _, label in run)
    start = _dialog_start(lines, len(lines))
    return sum(bool(_CHECKBOX_RE.match(line)) for line in lines[start:]) >= 2


def _corroborated(
    lines: list[str],
    run: list[tuple[int, int, str]],
    hints: tuple[KeyHint, ...],
) -> bool:
    """Is the trailing numbered run a LIVE menu?"""
    return bool(hints) or any(_MARKER_RE.match(lines[i]) for i, _, _ in run)


class Stepper(BaseModel):
    """The strip that says this question has more steps behind it."""

    model_config = ConfigDict(frozen=True)

    steps: tuple[str, ...] = ()
    """The question steps the strip named, in the order it drew them. The final
    """
    answered: int = 0
    """How many of :attr:`steps` the strip drew ALREADY TICKED.
    """

    @property
    def total(self) -> int:
        """How many question steps this one belongs to."""
        return len(self.steps)

    @property
    def remaining(self) -> int:
        """How many steps the strip has not drawn ticked yet."""
        return max(0, self.total - self.answered)


def _steps(line: str) -> Stepper | None:
    """The question steps one stepper strip named, or ``None`` if it named none."""
    steps: list[str] = []
    answered = 0
    for token in _STEP_TOKEN_RE.finditer(line):
        box = token.group("box")
        # The strip's own Submit tab is not a question, so it is not a step.
        if box in _STEP_SUBMIT:
            continue
        steps.append(" ".join(token.group("label").split()))
        answered += box in _STEP_TICKED
    return Stepper(steps=tuple(steps), answered=answered) if steps else None


def _stepper(lines: list[str], stop: int) -> Stepper | None:
    """The stepper strip above the dialog in ``lines[:stop]``, or ``None``.

    @doc: docs/modules/engine/model/_stepper.md
    """
    for index in range(stop - 1, _dialog_start(lines, stop) - 1, -1):
        if _STEPPER_RE.match(lines[index].strip()):
            return _steps(lines[index].strip())
    return None


def _asks(lines: list[str], stop: int) -> str:
    """The question the DIALOG BOX asks, or ``""`` when it asks none.

    @doc: docs/modules/engine/model/_asks.md
    """
    start = _dialog_start(lines, stop)
    # With no rule anywhere above it there is no BOX, and "the last few
    # paragraphs of the viewport" is transcript. Only the paragraph the run sits
    # directly under can be its question then. Measured on a live /model picker.
    window = _MAX_CHROME_PARAGRAPHS + 1 if start else 1
    for paragraph in reversed(_askable(lines, start, stop)[-window:]):
        if "?" in paragraph:
            return paragraph
    return ""


def _focused(lines: list[str], run: list[tuple[int, int, str]]) -> bool:
    """Is the terminal cursor parked on one of THESE rows?

    @doc: docs/modules/engine/model/_earned.md
    """
    return any(_MARKER_RE.match(lines[i]) for i, _, _ in run)


def _rows_are_the_answer(
    lines: list[str], run: list[tuple[int, int, str]], hints: tuple[KeyHint, ...]
) -> bool:
    """Does the screen SAY that taking a row answers it, and nothing more?

    @doc: docs/modules/engine/model/_earned.md
    """
    if _asks(lines, run[0][0]):
        return True
    return any(
        hint.key == "enter" and any(verb in hint.verb for verb in _SELECT_VERBS)
        for hint in hints
    )


def _shape(
    lines: list[str],
    run: list[tuple[int, int, str]] | None,
    hints: tuple[KeyHint, ...],
    *,
    corroborated: bool,
) -> MenuShape:
    """Classify the screen, in the order that fails safe."""
    if _is_multi_select(lines, run, hints):
        return MenuShape.MULTI_SELECT
    # SINGLE_CHOICE IS EARNED, NOT DEFAULTED TO. Failing the
    # test means UNKNOWN -- refused because it failed, not because it was odd.
    # @doc: docs/modules/engine/model/_earned.md
    if run is not None and corroborated and _rows_are_the_answer(lines, run, hints):
        return MenuShape.SINGLE_CHOICE
    if hints:
        return MenuShape.UNKNOWN
    return MenuShape.NONE


class PromptOption(BaseModel):
    """One numbered choice offered by a blocked agent."""

    model_config = ConfigDict(frozen=True)

    number: int
    label: str
    checked: bool | None = None
    """Whether the terminal drew this row TICKED, or ``None`` if it is not a toggle."""

    # There is deliberately no `key` property here any more.
    # @doc: docs/modules/engine/model.md

    @property
    def toggles(self) -> bool:
        """True when this row keeps state a keystroke flips rather than answering."""
        return self.checked is not None


class BlockedPrompt(BaseModel):
    """The question a blocked agent is waiting on, as far as it can be recovered."""

    model_config = ConfigDict(frozen=True)

    target: PaneId = PaneId("")
    """The address this question was read from, and the address an answer would
    """
    session: SessionId = SessionId("")
    """The identity that was asking. Kept alongside the address so a caller can
    """
    question: str = ""
    options: tuple[PromptOption, ...] = ()
    raw: str = ""
    shape: MenuShape = MenuShape.NONE
    """What kind of screen this is. See :class:`MenuShape` -- and note that it
    """
    corroborated: bool = False
    """Whether something the terminal drew says this is a live, waiting menu:
    """
    hints: tuple[KeyHint, ...] = ()
    """The keys the screen printed under its options, with the verbs it printed
    """
    stepper: Stepper | None = None
    """The strip that says this question has MORE STEPS behind it.
    """
    body: str = ""
    """The dialog BOX's own text above the options -- what is being asked about.

    @doc: docs/modules/engine/model/BlockedPrompt.body.md
    """
    drew_rows: bool = False
    """Whether the terminal drew a numbered run at all, trusted or not.

    @doc: docs/modules/engine/model/BlockedPrompt.drew_rows.md
    """
    asked: bool = False
    """Whether the DIALOG-BOX resolver found a question, as opposed to a fallback.

    @doc: docs/modules/engine/model/BlockedPrompt.asked.md
    """

    @field_validator("target", mode="before")
    @classmethod
    def _check_target(cls, value: object) -> PaneId:
        """Refuse an identity where an address belongs (and normalize whitespace)."""
        return as_pane_id(value)

    @field_validator("session", mode="before")
    @classmethod
    def _check_session(cls, value: object) -> SessionId:
        """Refuse an address where an identity belongs. The mirror of the above."""
        return as_session_id(value)

    @property
    def context(self) -> tuple[str, ...]:
        """The dialog's own lines minus the question — what is being asked ABOUT.

        @doc: docs/modules/engine/model/BlockedPrompt.context.md
        """
        asked = set(self.question.splitlines())
        return tuple(
            line
            for line in self.body.splitlines()
            if line not in asked and not _is_chrome(line)
        )

    @property
    def is_choice(self) -> bool:
        """True when this is a screen we positively recognise as single-choice."""
        return bool(self.options)

    @property
    def answerable(self) -> bool:
        """True when there is anything to answer with beyond the generic keypad."""
        return bool(self.hints)

    @property
    def fingerprint(self) -> str:
        """A digest of the exact screen this question was read off."""
        return screen_digest(self.raw)

    @property
    def identity(self) -> str:
        """A digest of the QUESTION AS POSED, or ``""`` if nothing was read.

        @doc: docs/modules/engine/model/BlockedPrompt.identity.md
        """
        # NO DIALOG, NO IDENTITY -- `question` falls back to
        # the status line on a screen with no buttons.
        # @doc: docs/modules/engine/model/BlockedPrompt.identity.md
        if not (self.drew_rows or self.shape is not MenuShape.NONE or self.asked):
            return ""
        if not (self.question or self.options or self.shape is MenuShape.MULTI_SELECT):
            return ""
        rows = (f"{o.number}\x1f{o.label}\x1f{o.checked}" for o in self.options)
        step = self.stepper
        return screen_digest(
            "\x1e".join(
                (
                    self.shape.value,
                    # WHAT IS BEING ASKED ABOUT, not just what is being asked.
                    # Without it every claude permission prompt is one question.
                    # @doc: docs/modules/engine/model/BlockedPrompt.identity.md
                    self.body,
                    self.question,
                    ""
                    if step is None
                    else f"{step.answered}/{'\x1d'.join(step.steps)}",
                    *rows,
                )
            )
        )

    @classmethod
    def parse(
        cls,
        text: str,
        *,
        target: PaneId = PaneId(""),
        session: SessionId = SessionId(""),
    ) -> Self:
        """Split detection output into a question and any numbered options."""
        lines = text.splitlines()
        # A DRAWN PANEL IS THE SCREEN, and `text` follows it so that `raw` is
        # still the screen this was read from.
        # @doc: docs/modules/engine/model.md
        if (panel := _panel_screen(lines)) is not None:
            lines, text = panel, "\n".join(panel)
        # `found` is the run as READ; `run` is the run as TRUSTED.
        # @doc: docs/modules/engine/model/BlockedPrompt.drew_rows.md
        found = _trailing_menu(lines)
        run = found
        hints = _hints(_footer_lines(lines))
        # NO CURSOR, NO MENU, whatever the footer looks like.
        # @doc: docs/modules/engine/model/_earned.md
        if run is not None and not _focused(lines, run):
            run = None
        corroborated = run is not None and _corroborated(lines, run, hints)
        shape = _shape(lines, run, hints, corroborated=corroborated)
        return cls(
            target=target,
            session=session,
            question=_question(lines, len(lines) if run is None else run[0][0]),
            options=_options(run, shape),
            stepper=_stepper(lines, len(lines) if run is None else run[0][0]),
            body=_body(lines, len(lines) if run is None else run[0][0]),
            drew_rows=found is not None,
            asked=bool(_asks(lines, len(lines) if run is None else run[0][0])),
            raw=text,
            shape=shape,
            corroborated=corroborated,
            hints=hints,
        )


def _options(
    run: list[tuple[int, int, str]] | None, shape: MenuShape
) -> tuple[PromptOption, ...]:
    """The rows of a menu this app is willing to name, with their tick state."""
    if run is None or shape not in (MenuShape.SINGLE_CHOICE, MenuShape.MULTI_SELECT):
        return ()
    rows = []
    for _, number, label in run:
        checked, text = _ticked(label)
        rows.append(PromptOption(number=number, label=text, checked=checked))
    return tuple(rows)


def _is_inert(line: str) -> bool:
    """True for lines that may sit *after* a menu's last option: blanks, rules, hints."""
    return not line.strip() or _is_chrome(line)


def _is_interleave(line: str) -> bool:
    """True for lines that may sit *between* two options of the same menu."""
    return _is_inert(line) or line[:1].isspace()


_PANEL_TOP_RE = re.compile(r"^\s*[╭┌╔]")
"""The top-left corner of a drawn dialog panel."""

_PANEL_BOTTOM_RE = re.compile(r"^\s*[╰└╚]")
"""The bottom-left corner of one."""

_PANEL_SIDE_RE = re.compile(r"^(\s*)[│┃║](.*?)[│┃║]?\s*$")
"""One row of a panel's interior, with the drawn sides around it."""

_PANEL_TAIL = 6
"""How far below a panel to look for the footer it was drawn with."""


def _unboxed(line: str) -> str:
    """One panel row with its sides removed and its own indentation kept."""
    match = _PANEL_SIDE_RE.match(line)
    return f"{match.group(1)}{match.group(2)}" if match else line


def _panel_footer(lines: list[str]) -> list[str]:
    """The key hints drawn BELOW a panel, which is where hermes puts them."""
    found: list[str] = []
    for line in lines[:_PANEL_TAIL]:
        if _DIVIDER_RE.match(line):
            break
        if _is_chrome(line) and line.strip():
            found.append(line)
    return found


def _panel_screen(lines: list[str]) -> list[str] | None:
    """A drawn dialog panel and its footer, as a screen of its own.

    @doc: docs/modules/engine/model.md
    """
    top = next(
        (i for i in range(len(lines) - 1, -1, -1) if _PANEL_TOP_RE.match(lines[i])),
        None,
    )
    if top is None:
        return None
    bottom = next(
        (i for i in range(top + 1, len(lines)) if _PANEL_BOTTOM_RE.match(lines[i])),
        None,
    )
    if bottom is None:
        return None
    inside = [_unboxed(line) for line in lines[top + 1 : bottom]]
    if not any(_OPTION_RE.match(line) for line in inside):
        return None
    return _upto_last_option(inside) + _panel_footer(lines[bottom + 1 :])


def _upto_last_option(inside: list[str]) -> list[str]:
    """A panel's rows, cut after its last option row.

    @doc: docs/modules/engine/model.md
    """
    last = max(i for i, line in enumerate(inside) if _OPTION_RE.match(line))
    kept = inside[: last + 1]
    return kept + [line for line in inside[last + 1 :] if _is_inert(line)]


def _trailing_menu(lines: list[str]) -> list[tuple[int, int, str]] | None:
    """The ``1..n`` option run anchored at the end of the viewport, or ``None``."""
    matches = [
        (i, int(m.group(1)), m.group("label"))
        for i, line in enumerate(lines)
        if (m := _OPTION_RE.match(line))
    ]
    if not matches:
        return None

    # Nothing but blanks, rules and key hints may follow the last option: a menu
    # is what the agent is waiting on *now*, so it sits at the end of the view.
    if any(not _is_inert(line) for line in lines[matches[-1][0] + 1 :]):
        return None

    run: list[tuple[int, int, str]] = []
    expected = matches[-1][1]
    for entry in reversed(matches):
        if entry[1] != expected:
            return None
        if run and any(
            not _is_interleave(line) for line in lines[entry[0] + 1 : run[-1][0]]
        ):
            return None
        run.append(entry)
        if expected == 1:
            break
        expected -= 1
    else:
        return None  # ran out of matches before reaching option 1

    if len(run) < _MIN_MENU_OPTIONS:
        return None
    return list(reversed(run))


def _paragraphs(lines: list[str], start: int, stop: int) -> list[str]:
    """The non-empty paragraphs of ``lines[start:stop]``, in reading order."""
    found: list[str] = []
    current: list[str] = []
    for raw_line in lines[start:stop]:
        line = "" if _is_chrome(raw_line) else raw_line.strip()
        if line:
            current.append(line)
        elif current:
            found.append("\n".join(current))
            current = []
    if current:
        found.append("\n".join(current))
    return found


def _is_chrome(line: str) -> bool:
    """True for a rule or a key-hint footer: furniture the terminal drew, not text.

    Two readings of "key hint", and the second is why plan approval used to
    parse as nothing. :data:`_FOOTER_RE` recognises a footer by the
    VERBS it prints, from a closed list, and both lines Claude Code draws under
    a plan -- ``shift+tab to approve with this feedback`` and ``ctrl+g to edit
    in nano`` -- print a verb that is not on it. Neither counted as furniture,
    so each ON ITS OWN put non-inert content after the last option and made
    :func:`_trailing_menu` drop a textbook four-row run. :func:`_is_key_hint`
    reads the same lines by their KEY instead, which is a terminal fact rather
    than a guess at Claude Code's vocabulary.

    @doc: docs/modules/engine/model/_is_chrome.md
    """
    return (
        bool(_DIVIDER_RE.match(line))
        or bool(_FOOTER_RE.search(line))
        or _is_key_hint(line)
    )


_MARKER_LEAD = re.compile(r"^[\s\u25cf\u25cb\u2022\u00b7]+")
"""A line's leading marker and indent -- the part of it that ANIMATES.

@doc: docs/modules/engine/model/_MARKER_LEAD.md
"""


def _body(lines: list[str], stop: int) -> str:
    """What is being asked ABOUT: the dialog's text above the options.

    @doc: docs/modules/engine/model/_body.md
    """
    start = _dialog_start(lines, stop)
    kept = (_MARKER_LEAD.sub("", line).rstrip() for line in lines[start:stop])
    return "\n".join(line for line in kept if line)


def _dialog_start(lines: list[str], stop: int) -> int:
    """Index just after the last rule before ``stop`` — where the current box begins."""
    for index in range(stop - 1, -1, -1):
        if _DIVIDER_RE.match(lines[index]):
            return index + 1
    return 0


def _is_option_block(paragraph: str) -> bool:
    """True for a paragraph that is nothing but numbered option rows."""
    rows = paragraph.split("\n")
    return all(_OPTION_RE.match(row) for row in rows)


def _askable(lines: list[str], start: int, stop: int) -> list[str]:
    """The paragraphs of ``lines[start:stop]`` that could be a question at all."""
    return [
        paragraph
        for paragraph in _paragraphs(lines, start, stop)
        if not _is_option_block(paragraph)
    ]


def _question(lines: list[str], stop: int) -> str:
    """The paragraph of ``lines[:stop]`` that is being asked."""
    # One resolver: what `_earned` tests for is the SAME paragraph this returns,
    # so a screen can never be called single-choice on a question we then drop.
    # @doc: docs/modules/engine/model/_asks.md
    if asked := _asks(lines, stop):
        return asked
    fallback = _askable(lines, 0, stop)
    return fallback[-1] if fallback else ""
