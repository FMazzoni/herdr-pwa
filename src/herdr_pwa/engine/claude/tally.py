"""What a Claude Code session spent, and the background work it started, over its lineage."""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType
from typing import Final

from pydantic import BaseModel, Field

from herdr_pwa.engine.claude.lineage import MAX_HOPS, copied_from
from herdr_pwa.engine.claude.records import (
    Block,
    ClaudeRecord,
    Usage,
    blocks_of,
    is_human,
    record_of,
)
from herdr_pwa.engine.claude.turns import delivered
from herdr_pwa.engine.notices import Injected, PeerMessage, TaskNotice, injected
from herdr_pwa.engine.pages import contained
from herdr_pwa.engine.transcript import (
    ENQUEUE,
    MAX_USAGE_BYTES,
    REMOVE,
    SPAWNED_AGENT,
    SPAWNED_SHELL,
    HistoryState,
    ModelRef,
    SessionUsage,
    SpawnedAgent,
    remember,
    token_count,
)

_SYNTHETIC_MODEL: Final = "<synthetic>"


def _usage_of(record: ClaudeRecord) -> tuple[str, Usage, str] | None:
    """An assistant record's ``(message id, usage, model)``, or ``None``."""
    message = record.message
    if record.type != "assistant" or message is None:
        return None
    if message.usage is None or not message.id:
        return None
    model = message.model or ""
    if model == _SYNTHETIC_MODEL:
        return None
    return message.id, message.usage, model


def _add_usage(tally: SessionUsage, usage: Usage, model: str) -> None:
    """Fold one message's usage into *tally*, in place."""
    details = usage.output_tokens_details
    tally.messages += 1
    tally.input_tokens += token_count(usage.input_tokens)
    tally.output_tokens += token_count(usage.output_tokens)
    tally.cache_creation_tokens += token_count(usage.cache_creation_input_tokens)
    tally.cache_read_tokens += token_count(usage.cache_read_input_tokens)
    if details is not None:
        tally.thinking_tokens += token_count(details.thinking_tokens)
    if model:
        # Claude Code answers from one provider, so the reference carries none.
        remember(tally, ModelRef(model))


def _prompt_size(usage: Usage) -> int:
    """Everything that went UP with one message: fresh, cache-written, cache-read."""
    return (
        token_count(usage.input_tokens)
        + token_count(usage.cache_creation_input_tokens)
        + token_count(usage.cache_read_input_tokens)
    )


class LiveTally(BaseModel):
    """A lineage's tally and what is needed to grow it by the lines appended to its newest log."""

    tally: SessionUsage
    seen: set[str] = Field(default_factory=set[str])
    budget: int = 0
    # Where the newest log's last whole line ends.
    end: int = 0
    # The newest Bash calls by tool_use id, so a result sent to the background is named.
    calls: dict[str, str] = Field(default_factory=dict[str, str])
    # Where each of the session's subagent logs was last read to.
    nested: dict[str, int] = Field(default_factory=dict[str, int])


# Measured: the bytes of a background launch, notice, backgrounded Bash call, resume or stop.
_SPAWN_MARKS: Final = (
    b'"async_launched"',
    b"<task-notification>",
    b'"queued_command"',
    b'"name":"Bash"',
    b'"name": "Bash"',
    b'"backgroundTaskId"',
    b'"resumedAgentId"',
    b'"revivedAgentId"',
    b'"task_type"',
)
# A call is answered within a few records; past this many an unanswered one is forgotten.
_HELD_CALLS: Final = 64
# What TaskStop says it stopped, as the tally's kinds.
_STOPPED: Final = MappingProxyType({"local_bash": SPAWNED_SHELL, "local_agent": SPAWNED_AGENT})
# What a notice calls each kind; measured, a stopped "Task" is a shell a subagent left running.
_NOTICE_KINDS: Final = MappingProxyType(
    {"Agent": SPAWNED_AGENT, "Command": SPAWNED_SHELL, "Task": SPAWNED_SHELL}
)
_BASH: Final = "Bash"


def _spawns(raw: bytes) -> bool:
    return any(mark in raw for mark in _SPAWN_MARKS)


def _worth(raw: bytes) -> bool:
    """Whether a line can carry usage, a prompt or background work; nothing else is parsed."""
    if b'"usage"' in raw or b'"promptSource"' in raw or b'"origin"' in raw:
        return True
    return _spawns(raw)


_ASYNC_LAUNCHED: Final = "async_launched"


def _spawned(tally: SessionUsage, task: str, kind: str = SPAWNED_AGENT) -> SpawnedAgent:
    """*task*'s entry in *tally*, added when this is the first record naming it."""
    for agent in tally.agents:
        if agent.task == task:
            return agent
    agent = SpawnedAgent(task=task, kind=kind)
    tally.agents.append(agent)
    return agent


def _settle(agent: SpawnedAgent) -> None:
    # Logs are read newest first, then their ancestors, so the latest stamps decide, not the order.
    agent.done = bool(agent.ended) and agent.ended >= agent.resumed


def _end(agent: SpawnedAgent, at: str, *, failed: bool) -> None:
    if not agent.ended or at > agent.ended:
        agent.ended, agent.failed = at, failed
    _settle(agent)


def _queued_notice(record: ClaudeRecord) -> Injected | None:
    # Measured: a subagent's shell ends only here; enqueue and remove repeat it, which is harmless.
    if record.type != "queue-operation" or record.operation not in (ENQUEUE, REMOVE):
        return None
    return injected(None, record.content or "", human=False)


def _ending(record: ClaudeRecord) -> TaskNotice | PeerMessage | None:
    """What *record* says ended a background agent or shell: its notice, or an agent's report."""
    found = delivered(record) or _queued_notice(record)
    content = record.message.content if record.message is not None else None
    if found is None and record.type == "user" and isinstance(content, str):
        found = injected(record.origin, content, human=is_human(record))
    if isinstance(found, TaskNotice):
        # A summary of no measured shape (a monitor's) names no kind this tally keeps.
        ends = found.named and found.kind in _NOTICE_KINDS and not found.waiting
        return found if ends else None
    return found if isinstance(found, PeerMessage) and found.report else None


def _launched(record: ClaudeRecord, tally: SessionUsage) -> bool:
    launch = record.tool_use_result
    if launch is None or launch.status != _ASYNC_LAUNCHED or not launch.agent_id:
        return False
    agent = _spawned(tally, launch.agent_id)
    agent.description = launch.description or agent.description
    agent.started = record.timestamp or agent.started
    return True


def _shell_name(block: Block) -> str:
    args = block.input
    given = (args.description or args.command or "") if args is not None else ""
    lines = given.strip().splitlines()
    return lines[0] if lines else ""


def _note_calls(record: ClaudeRecord, live: LiveTally) -> None:
    """Hold each Bash call's name until its result says whether it went to the background."""
    if record.type != "assistant":
        return
    for block in blocks_of(record):
        if block.type == "tool_use" and block.name == _BASH and block.id:
            live.calls[block.id] = _shell_name(block)
    while len(live.calls) > _HELD_CALLS:
        live.calls.pop(next(iter(live.calls)))


def _shell_launched(record: ClaudeRecord, live: LiveTally) -> bool:
    launch = record.tool_use_result
    if launch is None or not launch.shell:
        return False
    shell = _spawned(live.tally, launch.shell, SPAWNED_SHELL)
    shell.kind = SPAWNED_SHELL
    answered = [
        b.tool_use_id for b in blocks_of(record) if b.type == "tool_result" and b.tool_use_id
    ]
    named = [live.calls.pop(call, "") for call in answered]
    shell.description = next((name for name in named if name), shell.description)
    shell.started = record.timestamp or shell.started
    return True


def _resumed(record: ClaudeRecord, tally: SessionUsage) -> bool:
    launch = record.tool_use_result
    at = record.timestamp or ""
    if launch is None or not launch.resumed or not at:
        return False
    agent = _spawned(tally, launch.resumed)
    agent.resumed = max(agent.resumed, at)
    _settle(agent)
    return True


def _stopped(record: ClaudeRecord, tally: SessionUsage) -> bool:
    launch = record.tool_use_result
    kind = _STOPPED.get(launch.stopped_kind or "") if launch is not None else None
    if launch is None or kind is None or not launch.stopped:
        return False
    _end(_spawned(tally, launch.stopped, kind), record.timestamp or "", failed=False)
    return True


def _started_or_stopped(record: ClaudeRecord, live: LiveTally) -> bool:
    tally = live.tally
    if _launched(record, tally) or _shell_launched(record, live):
        return True
    return _resumed(record, tally) or _stopped(record, tally)


def _tally_spawned(record: ClaudeRecord, live: LiveTally) -> None:
    """Add a background agent's or shell's launch, resume or ending to *live*, if *record* is one."""
    _note_calls(record, live)
    if _started_or_stopped(record, live):
        return
    found = _ending(record)
    if found is None or not found.task:
        return
    notice = found if isinstance(found, TaskNotice) else None
    kind = _NOTICE_KINDS[notice.kind] if notice is not None else SPAWNED_AGENT
    agent = _spawned(live.tally, found.task, kind)
    if notice is not None and not agent.description:
        agent.description = notice.detail
    _end(agent, record.timestamp or "", failed=notice is not None and notice.failed)


def _nested_logs(path: Path) -> list[Path]:
    """The logs of the agents *path*'s session started, which Claude Code keeps beside it."""
    directory = path.with_suffix("") / "subagents"
    try:
        return sorted(directory.glob("agent-*.jsonl"))
    except OSError:
        return []


def _tally_nested_log(path: Path, live: LiveTally) -> None:
    """Add the background work in one subagent log's whole lines appended since it was read."""
    key = path.name
    start = live.nested.get(key, 0)
    with path.open("rb") as handle:
        if handle.seek(0, 2) < start:
            # Rewritten, not appended to: read it again whole.
            start = 0
        handle.seek(start)
        for raw in handle:
            if not raw.endswith(b"\n"):
                break
            start += len(raw)
            if _spawns(raw):
                _tally_spawned(record_of(raw), live)
    live.nested[key] = start


def _tally_nested(path: Path, live: LiveTally) -> None:
    """Add what the session's own agents started; their usage is not the session's, so only that."""
    for log in _nested_logs(path):
        try:
            _tally_nested_log(log, live)
        except OSError:
            continue


def _tally_log(path: Path, live: LiveTally, *, newest: bool, start: int = 0) -> int:
    """Add *path*'s whole lines from *start* to *live*; return where the last of them ends."""
    end = start
    with path.open("rb") as handle:
        handle.seek(start)
        for raw in handle:
            if not raw.endswith(b"\n"):
                # A line still being written is read once it is whole.
                break
            end += len(raw)
            _tally_raw(raw, live, newest=newest)
    return end


def _tally_raw(raw: bytes, live: LiveTally, *, newest: bool) -> None:
    """Add one whole line to *live*: all of it inside the byte budget, its background work past it."""
    live.budget -= len(raw)
    if live.budget >= 0:
        if _worth(raw):
            _tally_line(raw, live, newest=newest)
        return
    live.tally.partial = True
    live.budget = 0
    # Measured: a shell's end lands anywhere in a long log, so a spent budget may never hide it.
    if _spawns(raw):
        _tally_spawned(record_of(raw), live)


def _tally_prompt(
    record: ClaudeRecord, tally: SessionUsage, seen: set[str], *, newest: bool
) -> None:
    """Count one prompt the person sent; the newest log's last one starts the current turn."""
    if newest:
        tally.turn_started = record.timestamp or ""
    key = f"prompt:{record.uuid}"
    if key not in seen:
        seen.add(key)
        tally.prompts += 1


def _tally_line(raw: bytes, live: LiveTally, *, newest: bool) -> None:
    """Add one record to *live*: its message unless already counted, or the prompt it is."""
    record = record_of(raw)
    tally, seen = live.tally, live.seen
    if newest and record.timestamp:
        tally.last_at = record.timestamp
    _tally_spawned(record, live)
    if record.type == "user" and not record.is_meta and is_human(record):
        _tally_prompt(record, tally, seen, newest=newest)
        return
    found = _usage_of(record)
    if found is None:
        return
    identifier, usage, model = found
    if newest:
        # Read forward, so the last message to land here is the live window.
        tally.context_tokens = _prompt_size(usage)
    # One message is several records carrying identical usage; summing records nearly doubles it.
    if identifier in seen:
        return
    seen.add(identifier)
    _add_usage(tally, usage, model)


def _ancestors(path: Path) -> list[Path]:
    """Every log *path*'s thread was copied out of, newest first."""
    directory = path.parent
    chain: list[Path] = []
    current = path
    for _ in range(MAX_HOPS):
        parent = copied_from(current)
        if not parent:
            return chain
        found = contained(directory / f"{parent}.jsonl", directory)
        if found is None or found == path or found in chain:
            return chain
        chain.append(found)
        current = found
    return chain


def tally_claude(path: Path, *, max_bytes: int = MAX_USAGE_BYTES) -> LiveTally:
    """Sum *path* and every log its thread was copied out of, ready to grow."""
    live = LiveTally(tally=SessionUsage(), budget=max_bytes)
    for index, log in enumerate([path, *_ancestors(path)]):
        latest = live.tally.latest
        end = _tally_log(log, live, newest=index == 0)
        if latest.model:
            # Logs are read newest first: an older one's answers never name the current model.
            live.tally.latest = latest
        if index == 0:
            live.end = end
    _tally_nested(path, live)
    live.tally.state = HistoryState.ok if live.tally.messages else HistoryState.empty
    return live


def grow_claude(live: LiveTally, path: Path) -> None:
    """Add what was appended to the newest log, and to its agents' logs, since *live* read them."""
    live.end = _tally_log(path, live, newest=True, start=live.end)
    live.tally.state = HistoryState.ok if live.tally.messages else HistoryState.empty
    # An agent's shell ends in that agent's log while the session's own is still.
    _tally_nested(path, live)
