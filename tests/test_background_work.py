"""Work an agent started that runs on once its turn is over: background shells and agents."""

import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from herdr_pwa.engine import (
    AgentStatus,
    HistoryState,
    Located,
    SessionUsage,
    Source,
    SpawnedAgent,
    TranscriptPage,
    UsageCache,
)
from herdr_pwa.process_start import ProcessStarts
from herdr_pwa.web.chat import Turning, chat_view, status_row
from herdr_pwa.web.stats import SpawnedTime, TurnTimes, spawned_times
from tests.catalogue import FIXTURES, background_chat
from tests.engine_seam import TaskNotice, injected, tally_claude
from tests.webfakes import (
    SNAPSHOT,
    WORKING_PANE,
    WORKING_SESSION,
    Rig,
    agents_of,
    build_rig,
    snapshot_frame,
    write_transcript,
)

BACKGROUND: Final = FIXTURES / "transcript" / "notices" / "background.jsonl"
NESTED: Final = Path("background") / "subagents" / "agent-aresume001.jsonl"


def _copy(tmp_path: Path) -> Path:
    shutil.copytree(BACKGROUND.parent, tmp_path / "log", dirs_exist_ok=True)
    return tmp_path / "log" / BACKGROUND.name


def _tasks(tally: SessionUsage) -> dict[str, SpawnedAgent]:
    return {task.task: task for task in tally.agents}


# ---- The tally reads background shells beside the agents ----


def test_the_tally_knows_which_background_shells_still_run(tmp_path: Path) -> None:
    tasks = _tasks(tally_claude(_copy(tmp_path)).tally)
    shells = {k: v for k, v in tasks.items() if v.kind == "shell"}
    assert sorted(shells) == ["bnested04", "bserve001", "bsuite002", "btail0003"]
    assert shells["bserve001"].description == "Serve the docs"
    assert not shells["bserve001"].done
    assert shells["bserve001"].started == "2026-10-20T10:00:06.000Z"
    # Moved to the background by its timeout, then a notice said it ended.
    assert shells["bsuite002"].done
    # Stopped by the agent, and named by its command since it carried no description.
    assert shells["btail0003"].done
    assert shells["btail0003"].description == "tail -f build.log"
    # Started by a background agent, in that agent's own log.
    assert shells["bnested04"].description == "Watch the shots"
    assert not shells["bnested04"].done


def test_an_agent_resumed_after_it_ended_runs_again(tmp_path: Path) -> None:
    agent = _tasks(tally_claude(_copy(tmp_path)).tally)["aresume001"]
    assert agent.kind == "agent"
    assert not agent.done
    assert agent.resumed == "2026-10-20T10:30:01.000Z"


def test_a_notice_whose_quotes_were_escaped_still_ends_its_agent(tmp_path: Path) -> None:
    assert _tasks(tally_claude(_copy(tmp_path)).tally)["aescaped02"].done
    escaped = (
        "<task-notification>\n<task-id>a1</task-id>\n<status>completed</status>\n"
        '<summary>Agent \\"Seams\\" finished</summary>\n</task-notification>'
    )
    found = injected(None, escaped, human=False)
    assert isinstance(found, TaskNotice)
    assert (found.kind, found.detail) == ("Agent", "Seams")


def test_a_shell_a_subagent_stops_is_read_though_the_main_log_is_still(tmp_path: Path) -> None:
    log = _copy(tmp_path)
    cache = UsageCache()
    found = Located(Source.of(WORKING_SESSION, agent_kind="claude"), log)
    assert not _tasks(cache.read(found))["bnested04"].done
    stopped = (
        '{"type": "user", "timestamp": "2026-10-20T10:40:00.000Z", "toolUseResult": '
        '{"message": "stopped", "task_id": "bnested04", "task_type": "local_bash"}}\n'
    )
    with (log.parent / NESTED).open("a") as handle:
        handle.write(stopped)
    assert _tasks(cache.read(found))["bnested04"].done


def test_the_cache_holds_as_many_logs_as_the_herd_has_agents(tmp_path: Path) -> None:
    cache = UsageCache(files=2)
    cache.fit(4)
    logs: list[Located] = []
    for n in range(4):
        log = tmp_path / f"{n}.jsonl"
        log.write_text(BACKGROUND.read_text())
        logs.append(Located(Source.of(f"s{n}", agent_kind="claude"), log))
        _ = cache.read(logs[-1])
    assert cache.held == 4


# ---- What the page lists ----

NOW: Final = datetime(2026, 10, 20, 11, 0, tzinfo=UTC).timestamp()


def test_a_shell_started_before_the_running_process_belongs_to_a_dead_one() -> None:
    usage = SessionUsage(
        agents=[
            SpawnedAgent(
                task="old", kind="shell", description="Old", started="2026-10-20T09:00:00Z"
            ),
            SpawnedAgent(
                task="new", kind="shell", description="New", started="2026-10-20T10:30:00Z"
            ),
        ]
    )
    since = datetime(2026, 10, 20, 10, 0, tzinfo=UTC).timestamp()
    assert [s.description for s in spawned_times(usage, NOW, since=since)] == ["New"]
    # With no process start known, every unended shell is counted, as the harness's footer does.
    assert [s.description for s in spawned_times(usage, NOW)] == ["Old", "New"]


def test_a_resumed_agent_is_timed_from_its_resume() -> None:
    usage = SessionUsage(
        agents=[
            SpawnedAgent(
                task="a",
                description="A",
                started="2026-10-20T09:00:00Z",
                ended="2026-10-20T09:30:00Z",
                resumed="2026-10-20T10:50:00Z",
            )
        ]
    )
    assert spawned_times(usage, NOW) == (SpawnedTime("A", running=True, seconds=600),)


def test_the_process_start_is_read_from_the_panes_claude(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    (proc / "7").mkdir(parents=True)
    (proc / "stat").write_text("cpu 1 2 3\nbtime 1000\n")
    # Field 22, starttime in clock ticks since boot; the name holds a space and a parenthesis.
    fields = ["S", *["0"] * 18, str(5 * os.sysconf("SC_CLK_TCK"))]
    (proc / "7" / "stat").write_text(f"7 (claude (x)) {' '.join(fields)} 0 0\n")

    class Panes:
        def pane_pids(self, pane_id: str) -> tuple[int, ...]:
            return (3, 7) if pane_id == "w1:p1" else ()

    starts = ProcessStarts(Panes(), proc=proc, clock=lambda: 0.0)
    assert starts.started("w1:p1") == 1005.0
    assert starts.started("w9:p9") is None


# ---- The row and the sheet ----

SHELLS: Final = (
    SpawnedTime("Serve the docs", running=True, seconds=600, kind="shell"),
    SpawnedTime("npm run build", running=True, seconds=60, kind="shell"),
    SpawnedTime("Review", running=True, seconds=300),
)


def _view(status: AgentStatus, spawned: tuple[SpawnedTime, ...]) -> Turning:
    return Turning(
        status=status, stretch="s:1", times=TurnTimes(ran=900, quiet=600), spawned=spawned
    )


def test_an_idle_agent_with_work_still_running_says_so() -> None:
    page = TranscriptPage(turns=[], state=HistoryState.ok)
    view = chat_view(page, turning=_view(AgentStatus.IDLE, SHELLS))
    assert view.bubble is None
    assert [s.description for s in view.shells] == ["Serve the docs", "npm run build"]
    assert [a.description for a in view.agents] == ["Review"]
    row = status_row(view, "idle")
    assert (row.state, row.lead, row.shells, row.running) == ("background", "Idle", 2, 1)
    assert row.sentence == "Idle. 2 shells running. 1 agent running."


def test_an_idle_agent_with_nothing_running_is_plain_idle() -> None:
    ended = (SpawnedTime("Old", running=False, seconds=60, kind="shell"),)
    page = TranscriptPage(turns=[], state=HistoryState.ok)
    view = chat_view(page, turning=_view(AgentStatus.IDLE, ended))
    assert (view.shells, view.agents) == ((), ())
    assert status_row(view, "idle").state == "still"


def test_a_working_turn_is_not_waiting_on_a_shell() -> None:
    page = TranscriptPage(turns=[], state=HistoryState.ok)
    view = chat_view(page, turning=_view(AgentStatus.WORKING, SHELLS[:1]))
    assert view.bubble is not None
    assert view.bubble.text != "Waiting"
    assert status_row(view, "working").shells == 1


def _idle_rig(tmp_path: Path, shells: int, agents: int) -> Rig:
    frame = snapshot_frame()
    next(a for a in agents_of(frame) if a.get("pane_id") == WORKING_PANE)["agent_status"] = "idle"
    rig = build_rig(tmp_path, {SNAPSHOT: frame})
    roots = rig.settings.state_dir.parent / "transcripts"
    write_transcript(roots, WORKING_SESSION, background_chat(shells, agents))
    return rig


def test_the_row_counts_the_shells_and_the_sheet_lists_them(tmp_path: Path) -> None:
    rig = _idle_rig(tmp_path, shells=2, agents=1)
    body = rig.http.get(f"/agent/{WORKING_SESSION}/tray", headers={"HX-Request": "true"}).text
    assert "tray__row--background" in body
    assert '<b class="count__n">2</b> shells</span>' in body
    assert '<b class="count__n">1</b> agent running</span>' in body
    assert "Shells <b" in body
    assert body.count('class="spawned__row spawned__row--running"') == 3


def test_the_feed_card_says_background_instead_of_idle(tmp_path: Path) -> None:
    rig = _idle_rig(tmp_path, shells=1, agents=0)
    body = rig.http.get("/feed").text
    assert body.count('class="card__status card__status--background">background</span>') == 1
