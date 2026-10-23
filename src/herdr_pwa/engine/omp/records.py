"""omp's session log as records: where a session's log is, and what one line of it holds."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Final

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError

from herdr_pwa.engine.omp.ask import AskQuestion
from herdr_pwa.engine.pages import contained
from herdr_pwa.engine.transcript import Source, ToolInput
from herdr_pwa.validation import OR_NONE, Count, Flag, Number, Text

# herdr's name for omp: the registry key, and the only source kind whose log is read here.
KIND: Final = "omp"
OMP_ROOT: Final = "~/.omp/agent/sessions"


class _Cost(BaseModel):
    """What one answer cost, as omp splits it by token kind; only the total is read."""

    model_config = ConfigDict(extra="ignore")

    total: Number = None


class Usage(BaseModel):
    """One answer's tokens and money, as the provider reported them."""

    model_config = ConfigDict(extra="ignore")

    input: Count = None
    output: Count = None
    cache_read: Count = Field(default=None, validation_alias=AliasChoices("cacheRead"))
    cache_write: Count = Field(default=None, validation_alias=AliasChoices("cacheWrite"))
    reasoning: Count = Field(
        default=None, validation_alias=AliasChoices("reasoningTokens", "reasoning")
    )
    cost: Annotated[_Cost | None, OR_NONE] = None


class _AskArgs(BaseModel):
    """The arguments of an ``ask`` tool call: the questions the dialog is about to draw."""

    model_config = ConfigDict(extra="ignore")

    questions: tuple[AskQuestion, ...] = ()


class Block(BaseModel):
    """One content block of an omp message, as far as the chat draws it."""

    model_config = ConfigDict(extra="ignore")

    type: Text = None
    text: Text = None
    id: Text = None
    name: Text = None
    intent: Text = Field(default=None, validation_alias=AliasChoices("i", "intent"))
    arguments: Annotated[ToolInput | None, OR_NONE] = None
    # The same arguments read for an ``ask`` call's questions, which ToolInput does not keep.
    asked: Annotated[_AskArgs | None, OR_NONE] = Field(
        default=None, validation_alias=AliasChoices("arguments")
    )


type _OneBlock = Annotated[Block | None, OR_NONE]


class Message(BaseModel):
    """The ``message`` of a user, assistant or toolResult record."""

    model_config = ConfigDict(extra="ignore")

    role: Text = None
    content: Annotated[list[_OneBlock] | str | None, OR_NONE] = None
    tool_call_id: Text = Field(
        default=None, validation_alias=AliasChoices("toolCallId", "tool_call_id")
    )
    is_error: Flag = Field(default=None, validation_alias=AliasChoices("isError", "is_error"))
    model: Text = None
    provider: Text = None
    usage: Annotated[Usage | None, OR_NONE] = None
    stop_reason: Text = Field(
        default=None, validation_alias=AliasChoices("stopReason", "stop_reason")
    )
    error_message: Text = Field(
        default=None, validation_alias=AliasChoices("errorMessage", "error_message")
    )


class Record(BaseModel):
    """One line of an omp session log: its kind, its id, and what the kind carries."""

    model_config = ConfigDict(extra="ignore")

    type: Text = None
    id: Text = None
    timestamp: Text = None
    cwd: Text = None
    message: Annotated[Message | None, OR_NONE] = None


def session_log(source: Source) -> Path | None:
    """The session's log in the first root holding it, contained to that root."""
    if not source.session:
        return None
    for base in source.roots:
        found = _in_root(base, source.session)
        if found is not None:
            return found
    return None


def trimmed(value: Text) -> str:
    """One foreign string, trimmed, or ``""`` for anything that is not one."""
    return value.strip() if value else ""


def parse_record(raw: bytes) -> Record | None:
    """One line as a record, or ``None`` for a torn or malformed write."""
    try:
        return Record.model_validate_json(raw)
    except ValidationError:
        return None


def blocks_of(message: Message) -> list[Block]:
    """A message's content blocks; a bare string is the one text block older stores write."""
    content = message.content
    if isinstance(content, str):
        return [Block(type="text", text=content)]
    return [block for block in content or () if block is not None]


def _in_root(base: Path, session: str) -> Path | None:
    """*session*'s log inside ONE root, contained to THAT root and no other."""
    # A session is a file name: a separator would walk the tree, and no glob is built from it.
    if not session or "/" in session or "\\" in session:
        return None
    try:
        base = base.resolve()
        projects = sorted(entry for entry in base.iterdir() if entry.is_dir())
    except OSError:
        return None
    for project in projects:
        path = project / f"{session}.jsonl"
        if not path.is_file():
            continue
        # One containment rule, shared with the Claude reader: a symlink may point anywhere.
        resolved = contained(path, base)
        if resolved is not None:
            return resolved
    return None
