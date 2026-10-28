"""Work an agent started that runs on once its turn is over: background shells and agents."""

import os
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pytest

from herdr_pwa.clock import MonoTime, WallTime
from herdr_pwa.decisions import Fetched, Overlay, Pending, decide_herd
from herdr_pwa.engine import (
    Agent,
    AgentStatus,
    HistoryState,
    Located,
    PaneId,
    SessionUsage,
    Source,
    SpawnedAgent,
    TranscriptPage,
    UsageCache,
)
from herdr_pwa.process_start import PanePids, ProcessStarts
from herdr_pwa.web.background import pending_of
from herdr_pwa.web.chat_status import Turning
from herdr_pwa.web.chat_view import chat_view, status_row
from herdr_pwa.web.stats import SpawnedTime, TurnTimes, spawned_times
from tests.catalogue import FIXTURES, LeftRunning, background_chat
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


def test_the_subagent_folder_is_listed_again_only_once_it_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every poll tallies the session; an unchanged folder is not scanned on each one."""
    log = _copy(tmp_path)
    folder = (log.parent / NESTED).parent
    scans: list[Path] = []
    glob = Path.glob

    def counted(self: Path, pattern: str) -> Iterator[Path]:
        if self == folder:
            scans.append(self)
        return glob(self, pattern)

    monkeypatch.setattr(Path, "glob", counted)
    cache = UsageCache()
    found = Located(Source.of(WORKING_SESSION, agent_kind="claude"), log)
    cache.read(found)
    cache.read(found)
    assert len(scans) == 1
    started = (log.parent / NESTED).read_text().replace("bnested04", "bnested05")
    (folder / "agent-alater0001.jsonl").write_text(started)
    later = folder.stat().st_mtime_ns + 1_000_000_000
    os.utime(folder, ns=(later, later))
    assert "bnested05" in _tasks(cache.read(found))
    assert len(scans) == 2


SHELL_ENDS: Final = FIXTURES / "transcript" / "notices" / "shell-ends.jsonl"


def _copy_ends(tmp_path: Path) -> Path:
    shutil.copytree(SHELL_ENDS.parent / "shell-ends", tmp_path / "log" / "shell-ends")
    return Path(shutil.copy(SHELL_ENDS, tmp_path / "log" / SHELL_ENDS.name))


def test_a_subagent_shell_ends_by_the_notice_the_session_log_queued(tmp_path: Path) -> None:
    shell = _tasks(tally_claude(_copy_ends(tmp_path)).tally)["bqueued01"]
    assert (shell.kind, shell.description) == ("shell", "Run the suite")
    assert shell.done
    assert not shell.failed
    # The enqueue and the remove carry one notice; the later stamp is the one kept.
    assert shell.ended == "2026-10-20T10:05:00.100Z"


def test_a_shell_killed_from_outside_the_harness_ends_failed(tmp_path: Path) -> None:
    shell = _tasks(tally_claude(_copy_ends(tmp_path)).tally)["bkilled02"]
    assert shell.done
    assert shell.failed


def test_a_shell_no_notice_names_still_runs(tmp_path: Path) -> None:
    tasks = _tasks(tally_claude(_copy_ends(tmp_path)).tally)
    assert not tasks["brun00003"].done
    assert [t.task for t in tasks.values() if t.kind == "shell" and not t.done] == ["brun00003"]


def test_a_queued_notice_appended_later_ends_its_shell(tmp_path: Path) -> None:
    log = _copy_ends(tmp_path)
    cache = UsageCache()
    found = Located(Source.of(WORKING_SESSION, agent_kind="claude"), log)
    assert not _tasks(cache.read(found))["brun00003"].done
    queued = SHELL_ENDS.read_text().splitlines()[-1].replace("bkilled02", "brun00003")
    with log.open("a") as handle:
        handle.write(queued + "\n")
    assert _tasks(cache.read(found))["brun00003"].done


LATE_ENDS: Final = FIXTURES / "transcript" / "notices" / "shell-late-ends.jsonl"
# The prompt and the six launches; every line after them ends a shell.
LATE_HEAD: Final = 13
# Each shell that ends, and whether it ended any way but completed.
LATE_FAILED: Final = {
    "bwait0001": False,
    "bmoved002": True,
    "bkill0003": True,
    "bstop0004": False,
    "btask0005": True,
}


def _late_lines() -> list[str]:
    return LATE_ENDS.read_text().splitlines(keepends=True)


def _late_budget(budget: str) -> int:
    head = len("".join(_late_lines()[:LATE_HEAD]).encode())
    return {"whole": 1 << 30, "launches": head, "none": 1}[budget]


def _late_shells(tally: SessionUsage) -> dict[str, SpawnedAgent]:
    return {k: v for k, v in _tasks(tally).items() if v.kind == "shell"}


def _assert_late_ends(tally: SessionUsage) -> None:
    shells = _late_shells(tally)
    assert sorted(shells) == sorted([*LATE_FAILED, "brun00006"])
    assert {k: v.failed for k, v in shells.items() if v.done} == LATE_FAILED
    assert shells["bmoved002"].description == "Run the suite"
    assert shells["btask0005"].description == "Poll the queue"
    assert not shells["brun00006"].done


@pytest.mark.parametrize("budget", ["whole", "launches", "none"])
def test_every_way_a_shell_ends_closes_it_wherever_the_byte_budget_ran_out(
    tmp_path: Path, budget: str
) -> None:
    log = Path(shutil.copy(LATE_ENDS, tmp_path / LATE_ENDS.name))
    live = tally_claude(log, max_bytes=_late_budget(budget))
    assert live.tally.partial == (budget != "whole")
    _assert_late_ends(live.tally)


@pytest.mark.parametrize("budget", ["whole", "launches", "none"])
def test_every_way_a_shell_ends_closes_it_when_appended_later(tmp_path: Path, budget: str) -> None:
    lines = _late_lines()
    log = tmp_path / LATE_ENDS.name
    log.write_text("".join(lines[:LATE_HEAD]))
    cache = UsageCache(max_bytes=_late_budget(budget))
    found = Located(Source.of(WORKING_SESSION, agent_kind="claude"), log)
    assert not any(shell.done for shell in _late_shells(cache.read(found)).values())
    with log.open("a") as handle:
        handle.write("".join(lines[LATE_HEAD:]))
    _assert_late_ends(cache.read(found))


def test_a_spent_budget_still_counts_only_the_messages_inside_it(tmp_path: Path) -> None:
    log = Path(shutil.copy(LATE_ENDS, tmp_path / LATE_ENDS.name))
    assert tally_claude(log, max_bytes=_late_budget("none")).tally.messages == 0
    assert tally_claude(log, max_bytes=_late_budget("launches")).tally.messages == 6


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

NOW: Final = WallTime(datetime(2026, 10, 20, 11, 0, tzinfo=UTC).timestamp())


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
        def pane_processes(self, pane_id: str) -> PanePids:
            return PanePids(3, (7,)) if pane_id == "w1:p1" else PanePids(None, ())

    starts = ProcessStarts(Panes(), proc=proc, clock=lambda: MonoTime(0.0))
    assert starts.started(PaneId("w1:p1")) == 1005.0
    assert starts.started(PaneId("w9:p9")) is None


# ---- The row and the sheet ----

SHELLS: Final = (
    SpawnedTime("Serve the docs", running=True, seconds=600, kind="shell"),
    SpawnedTime("npm run build", running=True, seconds=60, kind="shell", fresh=True),
    SpawnedTime("Review", running=True, seconds=300, fresh=True),
)


def _view(status: AgentStatus, spawned: tuple[SpawnedTime, ...]) -> Turning:
    return Turning(
        status=status, stretch="s:1", times=TurnTimes(ran=900, quiet=600), spawned=spawned
    )


def test_an_idle_agent_with_work_still_running_says_so() -> None:
    page = TranscriptPage(turns=[], state=HistoryState.ok)
    view = chat_view(page, turning=_view(AgentStatus.IDLE, SHELLS))
    assert view.bubble is None
    # The shells the last turn started come first, then the older ones.
    assert [s.description for s in view.shells] == ["npm run build", "Serve the docs"]
    assert [a.description for a in view.agents] == ["Review"]
    row = status_row(view, "idle")
    assert (row.state, row.lead, row.running, row.shells) == ("background", "Idle", 1, 2)
    assert row.sentence == "Idle. 1 agent running. 2 shells running."


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


def test_only_pending_work_counts_for_the_feed() -> None:
    """Agents that will report back, and shells the last turn started; never an older server."""
    assert pending_of(SHELLS) == Pending(agents=1, shells=1)
    assert pending_of(SHELLS[:1]) == Pending()
    assert not Pending()
    ended = SpawnedTime("Built", running=False, seconds=60, kind="shell", fresh=True)
    assert pending_of((ended,)) == Pending()


def _idle_rig(tmp_path: Path, left: LeftRunning) -> Rig:
    frame = snapshot_frame()
    next(a for a in agents_of(frame) if a.get("pane_id") == WORKING_PANE)["agent_status"] = "idle"
    rig = build_rig(tmp_path, {SNAPSHOT: frame})
    roots = rig.settings.state_dir.parent / "transcripts"
    write_transcript(roots, WORKING_SESSION, background_chat(left))
    return rig


def _tray(rig: Rig) -> str:
    return rig.http.get(f"/agent/{WORKING_SESSION}/tray", headers={"HX-Request": "true"}).text


def test_the_row_counts_agents_then_shells_and_keeps_its_state_word(tmp_path: Path) -> None:
    body = _tray(_idle_rig(tmp_path, LeftRunning(shells=2, agents=1)))
    assert "tray__row--background" in body
    assert 'class="tray__words tray__words--state" aria-hidden="true">Idle</span>' in body
    agents = body.index('<b class="count__n">1</b> agent</span>')
    shells = body.index('<b class="count__n">2</b> shells</span>')
    assert agents < shells
    assert "running</span>" not in body.split("tray__shelf")[0]


def test_the_row_offers_details_with_work_running_and_keys_without(tmp_path: Path) -> None:
    assert '<span class="tray__tail" aria-hidden="true">Details</span>' in _tray(
        _idle_rig(tmp_path / "a", LeftRunning(shells=1, agents=0))
    )
    assert '<span class="tray__tail" aria-hidden="true">Keys</span>' in _tray(
        _idle_rig(tmp_path / "b", LeftRunning(shells=0, agents=0))
    )


def test_the_sheet_lists_agents_above_shells(tmp_path: Path) -> None:
    body = _tray(_idle_rig(tmp_path, LeftRunning(shells=2, agents=1)))
    assert body.index("Agents <b") < body.index("Shells <b")
    assert body.count('class="spawned__row spawned__row--running"') == 3


def test_a_card_with_pending_work_leads_the_idle_cards_marked_running(tmp_path: Path) -> None:
    body = _idle_rig(tmp_path, LeftRunning(shells=1, agents=2)).http.get("/feed").text
    idle = body[body.index("group--idle") :]
    cards = idle.split('class="card ')[1:]
    assert "dot--running" in cards[0]
    assert '<span class="card__status card__status--running">2 agents · 1 shell</span>' in cards[0]
    assert all("dot--running" not in card for card in cards[1:])


def test_an_old_server_shell_never_moves_or_marks_a_card(tmp_path: Path) -> None:
    body = _idle_rig(tmp_path, LeftRunning(shells=0, agents=0, old=1)).http.get("/feed").text
    assert "dot--running" not in body
    assert "card__status--running" not in body
    # The agent page still lists it.
    tray = _tray(_idle_rig(tmp_path / "again", LeftRunning(shells=0, agents=0, old=1)))
    assert "Shells <b" in tray


def _agent(n: int, status: AgentStatus, seq: int) -> Agent:
    return Agent.model_validate(
        {
            "session": f"00000000-0000-4000-8000-00000000000{n}",
            "target": f"w1:p{n}",
            "status": status,
            "agent": "claude",
            "state_change_seq": seq,
        }
    )


def test_pending_cards_sort_to_the_top_of_idle_and_keep_their_order() -> None:
    herd = [_agent(n, AgentStatus.IDLE, 10 - n) for n in (1, 2, 3, 4)]
    herd.append(_agent(5, AgentStatus.WORKING, 1))
    pending = {str(herd[2].session): Pending(agents=1), str(herd[3].session): Pending(shells=2)}
    overlay = Overlay(pending=pending)
    rows = decide_herd(herd, prompts={}, overlay=overlay, fetched=Fetched(MonoTime(0.0))).rows
    assert [str(r.target) for r in rows] == ["w1:p5", "w1:p3", "w1:p4", "w1:p1", "w1:p2"]
    assert rows[1].pending == Pending(agents=1)
    # Only an idle card is marked; herdr's other states keep their place.
    assert rows[0].pending == Pending()
    # With nothing pending the order is the one the feed always had.
    plain = decide_herd(herd, prompts={}, overlay=Overlay(), fetched=Fetched(MonoTime(0.0))).rows
    assert [str(r.target) for r in plain] == ["w1:p5", "w1:p1", "w1:p2", "w1:p3", "w1:p4"]
