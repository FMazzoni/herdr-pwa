"""Agents a session started in the background: which still run, which ended, and where it is said."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from pydantic import JsonValue, TypeAdapter

from herdr_pwa.engine import (
    AgentStatus,
    HistoryState,
    SessionUsage,
    SpawnedAgent,
    Speaker,
    TranscriptPage,
    TranscriptTurn,
)
from herdr_pwa.web.chat import ChatView, Turning, chat_view, joined
from herdr_pwa.web.stats import SpawnedTime, TurnTimes, spawned_times
from tests.catalogue import FIXTURES, spawned_chat
from tests.engine_seam import TaskNotice, Window, injected, read_page, tally_claude
from tests.webfakes import WORKING_SESSION, build_rig, write_transcript

# Trimmed from a live orchestrator's log: a typed prompt, two background launches, one agent told
# "waiting" and then finished, the other handing back and finishing mid-turn (attachments only),
# a second prompt, and a third launch still running.
SPAWNED: Final = FIXTURES / "transcript" / "notices" / "spawned.jsonl"
FIRST: Final = "a1610e2cacba7147a"
WAITED: Final = "a58ed79d3e8bfb4f3"
RUNNING: Final = "afd10ee41107eb4a2"
_RECORD: Final = TypeAdapter(dict[str, JsonValue])
NOW: Final = datetime(2026, 10, 17, 20, 0, tzinfo=UTC).timestamp()


def _records() -> list[dict[str, JsonValue]]:
    return [_RECORD.validate_json(line) for line in SPAWNED.read_text().splitlines()]


def _log(tmp_path: Path, records: list[dict[str, JsonValue]]) -> Path:
    path = tmp_path / "log.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


def _agents(tmp_path: Path, records: list[dict[str, JsonValue]]) -> dict[str, SpawnedAgent]:
    tally = tally_claude(_log(tmp_path, records)).tally
    return {agent.task: agent for agent in tally.agents}


def _turns(tmp_path: Path, records: list[dict[str, JsonValue]]) -> list[TranscriptTurn]:
    return read_page(_log(tmp_path, records), Window(messages=50)).turns


# ---- The notice that an agent waits on its own work ----

WAITING: Final = (
    "<task-notification>\n<task-id>a1</task-id>\n<status>completed</status>\n"
    '<summary>Agent "Seams" is waiting on its own background work</summary>\n'
    "</task-notification>"
)


def test_an_agent_waiting_on_its_own_work_reads_as_an_agent_that_is_not_over() -> None:
    found = injected(None, WAITING, human=False)
    assert isinstance(found, TaskNotice)
    assert (found.kind, found.detail, found.ended) == ("Agent", "Seams", "waiting")
    assert found.waiting
    assert not found.failed
    assert found.outcome == "Waiting on its own background work"
    finished = injected(
        None, WAITING.replace("is waiting on its own background work", "finished"), human=False
    )
    assert isinstance(finished, TaskNotice)
    assert not finished.waiting


# ---- The chat reads what the harness delivered mid-turn ----


def test_a_notice_and_a_hand_back_delivered_mid_turn_are_in_the_chat(tmp_path: Path) -> None:
    turns = _turns(tmp_path, _records())
    notices = [t for t in turns if t.speaker is Speaker.notice and t.task == FIRST]
    assert [(t.tool, t.text, t.status) for t in notices] == [
        ("Agent", "Name the prompt group", "finished")
    ]
    # The report was only an attachment too, and joins the notice it belongs to.
    carried = [t for t in joined(tuple(turns)) if t.task == FIRST]
    assert len(carried) == 1
    assert "Done; the gate passed." in carried[0].result


def test_a_prompt_the_person_queued_is_theirs_not_a_notice(tmp_path: Path) -> None:
    record: dict[str, JsonValue] = {
        "type": "attachment",
        "uuid": "q1",
        "timestamp": "2026-10-17T15:00:00.000Z",
        "attachment": {
            "type": "queued_command",
            "prompt": "also fix the docs",
            "commandMode": "prompt",
            "origin": {"kind": "human"},
        },
    }
    turns = _turns(tmp_path, [record])
    assert [(t.speaker, t.text) for t in turns] == [(Speaker.human, "also fix the docs")]


# ---- The tally joins launches to endings ----


def test_the_tally_knows_which_background_agents_ended_and_which_still_run(tmp_path: Path) -> None:
    agents = _agents(tmp_path, _records())
    assert list(agents) == [FIRST, WAITED, RUNNING]
    assert agents[FIRST].description == "Name the prompt group"
    assert agents[FIRST].done
    assert agents[WAITED].done
    assert not agents[RUNNING].done
    assert agents[RUNNING].started == "2026-10-17T19:42:47.411Z"


def test_waiting_on_its_own_work_does_not_end_an_agent(tmp_path: Path) -> None:
    records = _records()
    upto = next(i for i, r in enumerate(records) if r.get("type") == "attachment")
    agents = _agents(tmp_path, records[:upto])
    assert not agents[WAITED].done


def test_a_hand_back_ends_an_agent_even_with_no_finished_notice(tmp_path: Path) -> None:
    """Measured: handed back while its own work runs, it is told "waiting" and never "finished"."""
    records = _records()
    hand_back = next(i for i, r in enumerate(records) if r.get("type") == "attachment")
    agents = _agents(tmp_path, records[: hand_back + 1])
    assert agents[FIRST].done
    assert agents[FIRST].ended == "2026-10-17T14:59:07.245Z"


# ---- Which of them the page lists ----


def test_the_page_lists_running_agents_then_those_ended_this_turn() -> None:
    usage = SessionUsage(
        turn_started="2026-10-17T19:00:00Z",
        agents=[
            SpawnedAgent(
                task="old",
                description="Old",
                started="2026-10-17T18:00:00Z",
                done=True,
                ended="2026-10-17T18:30:00Z",
            ),
            SpawnedAgent(
                task="ended",
                description="Ended",
                started="2026-10-17T19:10:00Z",
                done=True,
                ended="2026-10-17T19:20:00Z",
                failed=True,
            ),
            SpawnedAgent(task="runs", description="Runs", started="2026-10-17T19:30:00Z"),
            # A hand-back with no launch seen is not a background agent of this session.
            SpawnedAgent(task="unseen", done=True, ended="2026-10-17T19:40:00Z"),
        ],
    )
    assert spawned_times(usage, NOW) == (
        SpawnedTime("Runs", running=True, seconds=1800),
        SpawnedTime("Ended", running=False, failed=True, seconds=600),
    )


def test_with_no_prompt_in_the_log_only_running_agents_are_listed() -> None:
    usage = SessionUsage(
        agents=[
            SpawnedAgent(
                task="a",
                description="A",
                started="2026-10-17T19:00:00Z",
                done=True,
                ended="2026-10-17T19:10:00Z",
            ),
            SpawnedAgent(task="b", description="B", started="2026-10-17T19:30:00Z"),
        ]
    )
    assert [s.description for s in spawned_times(usage, NOW)] == ["B"]


# ---- The bubble and the list under it ----

SAID: Final = TranscriptTurn(uuid="s", speaker=Speaker.agent, text="Waiting on the agents.")
TWO_RUN: Final = (
    SpawnedTime("Seams", running=True, seconds=300),
    SpawnedTime("Corpus", running=True, seconds=290),
    SpawnedTime("Clump", running=False, seconds=120),
)


def _view(status: AgentStatus, spawned: tuple[SpawnedTime, ...], quiet: float = 600) -> ChatView:
    page = TranscriptPage(turns=[SAID], state=HistoryState.ok)
    turning = Turning(
        status=status, stretch="s:1", times=TurnTimes(ran=900, quiet=quiet), spawned=spawned
    )
    return chat_view(page, turning=turning)


def test_a_turn_waiting_on_its_agents_says_so_and_never_reads_as_gone_quiet() -> None:
    view = _view(AgentStatus.WORKING, TWO_RUN)
    assert view.bubble is not None
    # The row counts the agents as a token of its own, so the words do not.
    assert (view.bubble.state, view.bubble.text) == ("working", "Waiting")
    assert view.bubble.quiet is None
    assert [(a.description, a.state, a.time) for a in view.agents] == [
        ("Seams", "running", "5m"),
        ("Corpus", "running", "4m 50s"),
        ("Clump", "finished", "2m"),
    ]
    one = _view(AgentStatus.WORKING, TWO_RUN[:1])
    assert one.bubble is not None
    assert one.bubble.text == "Waiting"


def test_with_every_agent_ended_the_bubble_goes_quiet_as_before() -> None:
    view = _view(AgentStatus.WORKING, TWO_RUN[2:])
    assert view.bubble is not None
    assert view.bubble.state == "quiet"
    assert [a.state for a in view.agents] == ["finished"]


def test_the_list_is_drawn_only_beside_a_live_bubble() -> None:
    """A session that dies never says its agents ended, so an idle agent lists none."""
    view = _view(AgentStatus.DONE, TWO_RUN)
    assert view.bubble is None
    assert view.agents == ()


def test_the_row_lists_the_agents_an_orchestrator_waits_on(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    write_transcript(rig.settings.state_dir.parent / "transcripts", WORKING_SESSION, spawned_chat())
    body = rig.http.get(f"/agent/{WORKING_SESSION}/tray", headers={"HX-Request": "true"}).text
    assert 'aria-hidden="true">Waiting</span>' in body
    assert '<b class="count__n">2</b> agents</span>' in body
    assert body.count('class="spawned__row spawned__row--running"') == 2
    assert body.count('class="spawned__row spawned__row--finished"') == 1
    assert "Remove the foreign capture corpus" in body
