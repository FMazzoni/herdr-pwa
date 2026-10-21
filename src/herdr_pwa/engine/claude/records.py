"""What one line of a Claude Code log holds, as far as this reader looks."""

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    RootModel,
    StrictStr,
    ValidationError,
)

from herdr_pwa.engine.notices import RecordOrigin
from herdr_pwa.engine.transcript import ToolInput
from herdr_pwa.validation import OR_NONE, Count, Flag, Text


class _ResultPart(BaseModel):
    """One part of a tool result's content list that the chat can show as text."""

    model_config = ConfigDict(extra="ignore")

    type: Literal["text", "image"]
    text: Text = None


class ResultParts(RootModel[Annotated[list[_ResultPart], Field(min_length=1)]]):
    """A tool result's content when it is text and images only; an empty list is not one."""


# Left to right, so only text and images read as parts; any other result stays whole JSON.
type ResultBody = Annotated[StrictStr | ResultParts | JsonValue, Field(union_mode="left_to_right")]


class Block(BaseModel):
    """One content block of a message: prose, a tool call, or a tool's result."""

    model_config = ConfigDict(extra="ignore")

    type: Text = None
    text: Text = None
    id: Text = None
    name: Text = None
    input: Annotated[ToolInput | None, OR_NONE] = None
    tool_use_id: Text = None
    is_error: Flag = None
    content: ResultBody = None

    @property
    def said(self) -> str:
        """The block's text, empty when it has none."""
        return self.text or ""

    @property
    def called(self) -> str:
        """The tool a ``tool_use`` block calls, empty when it names none."""
        return self.name or ""


class _Details(BaseModel):
    model_config = ConfigDict(extra="ignore")

    thinking_tokens: Count = None


class Usage(BaseModel):
    """What one assistant message cost, in tokens."""

    model_config = ConfigDict(extra="ignore")

    input_tokens: Count = None
    output_tokens: Count = None
    cache_creation_input_tokens: Count = None
    cache_read_input_tokens: Count = None
    output_tokens_details: Annotated[_Details | None, OR_NONE] = None


class _Message(BaseModel):
    """A record's ``message``: its content, and on an answer, what it cost."""

    model_config = ConfigDict(extra="ignore")

    content: Annotated[str | list[Annotated[Block | None, OR_NONE]] | None, OR_NONE] = None
    id: Text = None
    model: Text = None
    usage: Annotated[Usage | None, OR_NONE] = None


class _Kind(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kind: Text = None


class _Fork(BaseModel):
    model_config = ConfigDict(extra="ignore")

    session_id: Text = Field(default=None, validation_alias="sessionId")


class _Opaque(BaseModel):
    """An object this reader only needs to know is there."""

    model_config = ConfigDict(extra="ignore")


class Queued(BaseModel):
    """An ``attachment`` record's body: a ``queued_command`` is what the harness delivered mid-turn."""

    model_config = ConfigDict(extra="ignore")

    type: Text = None
    prompt: Text = None
    origin: Annotated[RecordOrigin | None, OR_NONE] = None


class _Launch(BaseModel):
    """A tool result's ``toolUseResult``, as far as it says background work started or stopped."""

    model_config = ConfigDict(extra="ignore")

    status: Text = None
    agent_id: Text = Field(default=None, validation_alias="agentId")
    description: Text = None
    # A Bash call sent to the background, by its flag, its timeout or the person.
    shell: Text = Field(default=None, validation_alias="backgroundTaskId")
    # A message that woke an agent which had ended.
    resumed: Text = Field(
        default=None, validation_alias=AliasChoices("resumedAgentId", "revivedAgentId")
    )
    # TaskStop's result: the task it stopped and whether that was a shell or an agent.
    stopped: Text = Field(default=None, validation_alias="task_id")
    stopped_kind: Text = Field(default=None, validation_alias="task_type")


class ClaudeRecord(BaseModel):
    """One line of a Claude Code log, holding only the fields this reader looks at."""

    # Claude Code adds fields every release; one it renames reads as absent, never as an error.
    model_config = ConfigDict(extra="ignore")

    type: Text = None
    uuid: Text = None
    timestamp: Text = None
    subtype: Text = None
    # A system record's text, which is where a `local_command` one keeps its tags.
    content: Text = None
    cwd: Text = None
    is_meta: Flag = Field(default=None, validation_alias="isMeta")
    message: Annotated[_Message | None, OR_NONE] = None
    origin: Annotated[RecordOrigin | None, OR_NONE] = None
    prompt_source: Annotated[str | _Kind | None, OR_NONE] = Field(
        default=None, validation_alias="promptSource"
    )
    session: Text = Field(default=None, validation_alias="sessionId")
    # A background session's stamp is spelled the other way.
    stamp: Text = Field(default=None, validation_alias="session_id")
    session_kind: Text = Field(default=None, validation_alias="sessionKind")
    forked_from: Annotated[_Fork | None, OR_NONE] = Field(
        default=None, validation_alias="forkedFrom"
    )
    compact_metadata: Annotated[_Opaque | None, OR_NONE] = Field(
        default=None, validation_alias="compactMetadata"
    )
    is_compact_summary: Flag = Field(default=None, validation_alias="isCompactSummary")
    # Measured: the harness writes a refused turn as an assistant record from model "<synthetic>".
    is_api_error: Flag = Field(default=None, validation_alias="isApiErrorMessage")
    error: Text = None
    # A queue-operation record's verb; what it queued rides in `content`.
    operation: Text = None
    attachment: Annotated[Queued | None, OR_NONE] = None
    tool_use_result: Annotated[_Launch | None, OR_NONE] = Field(
        default=None, validation_alias="toolUseResult"
    )


def claude_record(raw: bytes) -> ClaudeRecord | None:
    """One JSONL line as a record, or ``None`` for a line that is not one."""
    try:
        return ClaudeRecord.model_validate_json(raw)
    except ValidationError:
        return None


def record_of(raw: bytes) -> ClaudeRecord:
    """One JSONL line as a record, or an empty one for anything that is not."""
    if not raw.strip():
        return ClaudeRecord()
    return claude_record(raw) or ClaudeRecord()


_HUMAN_SOURCES: Final = frozenset({"typed", "queued", "pasted"})


def source_of(record: ClaudeRecord) -> str | None:
    """Where *record*'s prompt came from, as the harness names it, or ``None``."""
    source = record.prompt_source
    return source.kind if isinstance(source, _Kind) else source


def is_human(record: ClaudeRecord) -> bool:
    """Whether *record* is a prompt the person sent, typed, queued or pasted."""
    if source_of(record) in _HUMAN_SOURCES:
        return True
    return record.origin is not None and record.origin.kind == "human"


def blocks_of(record: ClaudeRecord) -> list[Block]:
    """The blocks of *record*'s content, when it is a list of them."""
    content = record.message.content if record.message is not None else None
    if not isinstance(content, list):
        return []
    return [block for block in content if block is not None]
