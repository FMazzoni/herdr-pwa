import json
import stat
import threading
from dataclasses import replace
from pathlib import Path
from typing import TypedDict

import pytest
from pydantic import JsonValue, TypeAdapter

from herdr_pwa import statedir
from herdr_pwa.clock import WallTime
from herdr_pwa.ids import PaneId, SessionId
from herdr_pwa.managed import ManagedHerd, carries_mark
from herdr_pwa.notify import (
    Change,
    HerdSlot,
    Hooks,
    NotifyConfig,
    Planner,
    herd_text,
    notice_for,
    notification_text,
    statuses_of,
    transitions,
)
from herdr_pwa.notify_state import Interrupts, InterruptStore, SeenLedger, StatusLedger
from herdr_pwa.push import EVENT_BLOCKED, EVENT_CLEAR, EVENT_DONE, DeliveryLog
from tests.fakes import Agent, Clock

A = "5e550001-0000-0000-0000-000000000000"
B = "22222222-2222-4222-8222-222222222222"
C = "33333333-3333-4333-8333-333333333333"


class SeenFile(TypedDict):
    version: int
    seen: dict[str, JsonValue]


SEEN_FILE: TypeAdapter[SeenFile] = TypeAdapter(SeenFile)


def agent(session: str = A, status: str = "working", *, seq: int = 0, **fields: str) -> Agent:
    return replace(Agent(session=SessionId(session), status=status, state_change_seq=seq), **fields)


def events(changes: list[Change]) -> list[tuple[str, str]]:
    return [(c.session, c.event) for c in changes]


@pytest.mark.parametrize(
    ("before", "now", "expected"),
    [
        ("working", "blocked", EVENT_BLOCKED),
        ("idle", "blocked", EVENT_BLOCKED),
        ("working", "done", EVENT_DONE),
        ("blocked", "done", EVENT_DONE),
        ("idle", "done", EVENT_DONE),
        ("done", "idle", EVENT_CLEAR),
        ("blocked", "idle", EVENT_CLEAR),
        ("blocked", "working", EVENT_CLEAR),
        ("idle", "working", EVENT_CLEAR),
        ("working", "idle", None),
        ("done", "done", None),
    ],
)
def test_one_transition_at_a_time(before: str, now: str, expected: str | None) -> None:
    found = transitions({A: before}, [agent(status=now)])
    assert [c.event for c in found] == ([] if expected is None else [expected])


def test_a_new_agent_whose_first_state_is_blocked_fires_and_a_quiet_one_does_not() -> None:
    assert events(transitions({}, [agent(status="blocked")])) == [(A, EVENT_BLOCKED)]
    for quiet in ("idle", "working"):
        assert transitions({}, [agent(status=quiet)]) == []


def test_unknown_is_never_recorded_and_never_fired_on() -> None:
    assert transitions({A: "working"}, [agent(status="unknown")]) == []
    assert transitions({A: "working"}, [agent(status="weird")]) == []
    assert statuses_of([agent(status="unknown")]) == {}


def test_an_agent_leaving_the_snapshot_clears_what_it_left_behind() -> None:
    found = transitions({A: "blocked", B: "idle"}, [])
    assert events(found) == [(A, EVENT_CLEAR)]
    assert found[0].agent is None


def test_a_finish_the_desk_saw_fires_when_the_phone_has_not() -> None:
    assert events(transitions({A: "working"}, [agent(status="idle")], unread={A})) == [
        (A, EVENT_DONE)
    ]
    assert transitions({A: "working"}, [agent(status="idle")]) == []


def test_unread_widens_only_a_finish() -> None:
    for was in ("idle", "blocked", "done"):
        found = transitions({A: was}, [agent(status="idle")], unread={A})
        assert EVENT_DONE not in [c.event for c in found]


def test_an_agent_with_no_session_is_filed_under_its_pane_and_moves_quietly() -> None:
    first = transitions({}, [agent(session="", status="blocked", target="w1:p9")])
    assert events(first) == [("pane:w1:p9", EVENT_BLOCKED)]
    moved = transitions({"pane:w1:p9": "blocked"}, [agent(status="blocked", target="w1:p9")])
    assert [(c.session, c.event, c.replaces) for c in moved] == [(A, EVENT_BLOCKED, "pane:w1:p9")]
    slot = HerdSlot(clock=Clock().mono)
    assert slot.update(first) is not None
    after = slot.update(moved)
    assert after is not None
    assert not after.buzz
    assert [a.session for a in after.alerts] == [A]


def test_the_verb_comes_first_and_the_body_carries_the_question() -> None:
    title, body, fallback = notification_text(agent(title="x" * 80), EVENT_BLOCKED, "Proceed?")
    assert title.startswith("needs you: ")
    assert body == "herdr · herdr-pwa — Proceed?"
    assert fallback == "herdr · herdr-pwa"


def test_three_agents_are_one_notification_counted_then_named() -> None:
    slot = HerdSlot(clock=Clock().mono)
    moved = slot.update(
        [
            Change(A, EVENT_DONE, agent(A, title="alpha")),
            Change(B, EVENT_BLOCKED, agent(B, title="beta")),
            Change(C, EVENT_BLOCKED, agent(C, title="gamma")),
        ]
    )
    assert moved is not None
    assert moved.summary is not None
    assert herd_text(moved.summary) == (
        "2 need you, 1 finished",
        "beta, gamma, alpha",
        "open the app to see the herd",
    )
    notice = notice_for(moved)
    assert notice is not None
    assert (notice.tag, notice.event, notice.agent, notice.quiet) == (
        "herd",
        EVENT_BLOCKED,
        "",
        False,
    )


def test_one_outstanding_agent_is_named_with_its_question_and_deep_linked() -> None:
    slot = HerdSlot(clock=Clock().mono)
    moved = slot.update([Change(A, EVENT_BLOCKED, agent(A, title="alpha"))])
    assert moved is not None
    notice = notice_for(moved, lambda _agent: "Allow edit?")
    assert notice is not None
    assert (notice.title, notice.agent) == ("needs you: alpha", A)
    assert notice.body.endswith("Allow edit?")


def test_the_slot_shrinks_silently_and_retracts_on_the_last_one() -> None:
    slot = HerdSlot(clock=Clock().mono)
    _ = slot.update([Change(A, EVENT_BLOCKED, agent(A)), Change(B, EVENT_BLOCKED, agent(B))])
    shrunk = slot.update([Change(A, EVENT_CLEAR)])
    assert shrunk is not None
    assert not shrunk.buzz
    gone = slot.update([Change(B, EVENT_CLEAR)])
    assert gone is not None
    assert gone.summary is None
    assert notice_for(gone) is None
    assert slot.update([Change(B, EVENT_CLEAR)]) is None


def test_only_the_focused_pane_inside_the_desk_window_is_held() -> None:
    clock = Clock()
    slot = HerdSlot(clock=clock.mono, debounce=30, desk_window=60)
    slot.note_desk(A)
    held = slot.update([Change(A, EVENT_BLOCKED, agent(A)), Change(B, EVENT_BLOCKED, agent(B))])
    assert held is not None
    assert [a.session for a in held.alerts] == [B]
    assert slot.next_due() == clock.mono() + 30
    clock.advance(30)
    released = slot.update()
    assert released is not None
    assert released.buzz
    assert {a.session for a in released.alerts} == {A, B}


def test_a_held_alert_resolved_at_the_desk_is_never_sent_and_a_clear_is_never_held() -> None:
    clock = Clock()
    slot = HerdSlot(clock=clock.mono, debounce=30, desk_window=60)
    slot.note_desk(A)
    assert slot.update([Change(A, EVENT_BLOCKED, agent(A))]) is None
    assert slot.update([Change(A, EVENT_CLEAR)]) is None
    clock.advance(31)
    assert slot.update() is None


def test_a_flapping_agent_away_from_the_desk_buzzes_once() -> None:
    clock = Clock()
    slot = HerdSlot(clock=clock.mono, debounce=30, desk_window=60)
    first = slot.update([Change(A, EVENT_BLOCKED, agent(A))])
    assert first is not None
    assert first.buzz
    _ = slot.update([Change(A, EVENT_CLEAR)])
    clock.advance(5)
    again = slot.update([Change(A, EVENT_BLOCKED, agent(A))])
    assert again is not None
    assert not again.buzz


def planner(
    tmp_path: Path, clock: Clock, hooks: Hooks | None = None, *, quiet_managed: bool = True
) -> Planner:
    ledger = StatusLedger(tmp_path, clock=clock)
    settings = NotifyConfig(debounce=0, desk_window=0, quiet_managed=quiet_managed)
    return Planner(ledger, settings, clock=clock.mono, hooks=hooks)


def test_the_first_tick_primes_silently_even_with_everything_blocked(tmp_path: Path) -> None:
    clock = Clock()
    plan = planner(tmp_path, clock)
    step = plan.step([agent(A, "blocked"), agent(B, "done")])
    assert (step.primed, step.slot) == (True, None)
    assert plan.step([agent(A, "blocked"), agent(B, "done")]).slot is None


def test_a_restart_diffs_against_the_ledger_it_left_and_resends_nothing(tmp_path: Path) -> None:
    clock = Clock()
    first = planner(tmp_path, clock)
    _ = first.step([agent(A, "working")])
    fired = first.step([agent(A, "blocked")])
    assert fired.slot is not None
    second = planner(tmp_path, clock)
    assert second.step([agent(A, "blocked")]).slot is None
    cleared = second.step([agent(A, "idle")])
    assert cleared.slot is not None
    assert cleared.slot.summary is None


@pytest.mark.parametrize("ledger", ["{torn", json.dumps({"version": 9}), None])
def test_a_corrupt_unknown_or_missing_ledger_primes(tmp_path: Path, ledger: str | None) -> None:
    if ledger is not None:
        _ = (tmp_path / "notify.json").write_text(ledger)
    assert planner(tmp_path, Clock()).step([agent(A, "blocked")]).primed


def test_a_ledger_older_than_fifteen_minutes_primes(tmp_path: Path) -> None:
    clock = Clock()
    StatusLedger(tmp_path, clock=clock).save({A: "working"})
    clock.advance(901)
    assert planner(tmp_path, clock).step([agent(A, "blocked")]).primed


def test_the_ledger_is_0600(tmp_path: Path) -> None:
    StatusLedger(tmp_path / "s", clock=Clock()).save({A: "idle"})
    assert stat.S_IMODE((tmp_path / "s" / "notify.json").stat().st_mode) == 0o600


def test_with_nobody_subscribed_no_prior_is_kept(tmp_path: Path) -> None:
    clock = Clock()
    plan = planner(tmp_path, clock)
    _ = plan.step([agent(A, "working")])
    plan.reset()
    (tmp_path / "notify.json").unlink()
    assert plan.step([agent(A, "blocked")]).primed


def test_the_prime_returns_before_unread_is_consulted(tmp_path: Path) -> None:
    class Spy(SeenLedger):
        def unread_sessions(self) -> set[str]:
            raise AssertionError("consulted on the priming tick")

    plan = planner(tmp_path, Clock(), Hooks(seen=Spy(tmp_path)))
    assert plan.step([agent(A, "idle")]).primed


def test_work_finished_at_the_desk_reaches_the_phone_via_the_seen_ledger(tmp_path: Path) -> None:
    clock = Clock()
    seen = SeenLedger(tmp_path, clock=clock)
    plan = planner(tmp_path, clock, Hooks(seen=seen))
    _ = plan.step([agent(A, "working", seq=1)])
    clock.advance(10)
    step = plan.step([agent(A, "idle", seq=2)])
    assert events(list(step.changes)) == [(A, EVENT_DONE)]


def test_a_finish_the_phone_already_opened_is_not_sent(tmp_path: Path) -> None:
    clock = Clock()
    seen = SeenLedger(tmp_path, clock=clock)
    plan = planner(tmp_path, clock, Hooks(seen=seen))
    _ = plan.step([agent(A, "working", seq=1)])
    clock.advance(10)
    seen.observe([agent(A, "idle", seq=2)])
    clock.advance(1)
    seen.mark_seen(SessionId(A))
    assert plan.step([agent(A, "idle", seq=2)]).changes == ()


def test_a_seen_ledger_that_raises_costs_only_the_widening(tmp_path: Path) -> None:
    class Broken(SeenLedger):
        def unread_sessions(self) -> set[str]:
            raise OSError

    plan = planner(tmp_path, Clock(), Hooks(seen=Broken(tmp_path)))
    _ = plan.step([agent(A, "working")])
    assert events(list(plan.step([agent(A, "blocked")]).changes)) == [(A, EVENT_BLOCKED)]


def test_a_managed_finish_is_quiet_but_its_blocked_always_buzzes(tmp_path: Path) -> None:
    clock = Clock()
    health = DeliveryLog(tmp_path, clock=clock)
    plan = planner(tmp_path, clock, Hooks(is_managed=lambda _pane: True, health=health))
    _ = plan.step([agent(A, "working"), agent(B, "working", target="w1:p2")])
    step = plan.step([agent(A, "done"), agent(B, "blocked", target="w1:p2")])
    assert events(list(step.changes)) == [(B, EVENT_BLOCKED)]
    record = health.load()
    assert record is not None
    assert record.quieted == 1


def test_a_managed_reader_that_raises_still_buzzes(tmp_path: Path) -> None:
    def broken(_pane: str) -> bool:
        raise RuntimeError

    plan = planner(tmp_path, Clock(), Hooks(is_managed=broken))
    _ = plan.step([agent(A, "working")])
    assert events(list(plan.step([agent(A, "done")]).changes)) == [(A, EVENT_DONE)]


def test_turning_managed_quieting_off_notifies_every_finish(tmp_path: Path) -> None:
    plan = planner(tmp_path, Clock(), Hooks(is_managed=lambda _pane: True), quiet_managed=False)
    _ = plan.step([agent(A, "working")])
    assert events(list(plan.step([agent(A, "done")]).changes)) == [(A, EVENT_DONE)]


def test_do_not_disturb_never_silences_blocked_and_never_a_clear() -> None:
    snoozed = Interrupts(blocked=True, done=True, dnd_until=2000)
    assert snoozed.allows(EVENT_BLOCKED, now=WallTime(1000))
    assert not snoozed.allows(EVENT_DONE, now=WallTime(1000))
    assert snoozed.allows(EVENT_DONE, now=WallTime(3000))
    assert Interrupts(blocked=False, done=False).allows(EVENT_CLEAR, now=WallTime(0))
    assert not Interrupts(blocked=False).allows(EVENT_BLOCKED, now=WallTime(0))
    assert (Interrupts().blocked, Interrupts().done) == (True, False)


def test_the_planner_applies_the_stored_preferences(tmp_path: Path) -> None:
    clock = Clock()
    store = InterruptStore(tmp_path, clock=clock)
    _ = store.snooze(3600)
    plan = planner(tmp_path, clock, Hooks(policy=store.current))
    _ = plan.step([agent(A, "working"), agent(B, "working", target="w1:p2")])
    step = plan.step([agent(A, "done"), agent(B, "blocked", target="w1:p2")])
    assert events(list(step.changes)) == [(B, EVENT_BLOCKED)]


def test_preferences_live_at_0600_snooze_is_clamped_and_garbage_is_defaults(tmp_path: Path) -> None:
    clock = Clock()
    store = InterruptStore(tmp_path, clock=clock)
    assert store.load() == Interrupts()
    assert store.snooze(10**9).dnd_until == clock() + 12 * 3600
    assert store.set_kinds(done=True).done
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    _ = store.path.write_text("{torn")
    assert store.load() == Interrupts()


def test_a_snooze_during_a_preference_change_keeps_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    store = InterruptStore(tmp_path, clock=clock)
    write = statedir.write_secret
    changing, release, wrote = threading.Event(), threading.Event(), threading.Event()

    def held(path: Path, text: str, *, exclusive: bool = False) -> None:
        # The change stops inside its write, after reading the store, until the snooze has had its go.
        if threading.current_thread().name == "kinds":
            changing.set()
            _ = release.wait(5)
        write(path, text, exclusive=exclusive)
        if threading.current_thread().name == "snooze":
            wrote.set()

    monkeypatch.setattr(statedir, "write_secret", held)
    kinds = threading.Thread(target=lambda: store.set_kinds(done=True), name="kinds")
    snooze = threading.Thread(target=lambda: store.snooze(60), name="snooze")
    kinds.start()
    assert changing.wait(5)
    snooze.start()
    _ = wrote.wait(0.5)
    release.set()
    kinds.join(5)
    snooze.join(5)
    kept = store.load()
    assert kept.done
    assert kept.dnd_until == clock() + 60


def test_the_seen_ledger_persists_at_0600_and_caps_its_size(tmp_path: Path) -> None:
    clock = Clock()
    seen = SeenLedger(tmp_path, clock=clock)
    seen.observe([agent(f"s{n}", "idle") for n in range(600)])
    seen.flush()
    reloaded = SeenLedger(tmp_path, clock=clock)
    clock.advance(1)
    reloaded.observe([agent("s599", "idle", seq=3)])
    assert reloaded.unread_sessions() == {"s599"}
    stored = SEEN_FILE.validate_json(seen.path.read_text())["seen"]
    assert len(stored) == 512
    assert stat.S_IMODE(seen.path.stat().st_mode) == 0o600


class Panes:
    def __init__(self, pids: dict[str, tuple[int, ...]]) -> None:
        self.pids: dict[str, tuple[int, ...]] = pids

    def pane_pids(self, pane_id: str) -> tuple[int, ...]:
        return self.pids[pane_id]


def fake_proc(root: Path, pid: int, environ: bytes) -> None:
    (root / str(pid)).mkdir(parents=True)
    _ = (root / str(pid) / "environ").write_bytes(environ)


@pytest.mark.parametrize(
    ("environ", "managed"),
    [
        (b"PATH=/bin\0HERDR_PWA_MANAGED=1\0", True),
        (b"HERDR_PWA_MANAGED=yes\0", True),
        (b"HERDR_PWA_MANAGED=0\0", False),
        (b"PATH=/bin\0", False),
        (b"NOT_HERDR_PWA_MANAGED=1\0", False),
    ],
)
def test_the_mark_is_read_from_the_process_environment(
    tmp_path: Path, environ: bytes, managed: bool
) -> None:
    fake_proc(tmp_path, 42, environ)
    assert carries_mark(42, proc=tmp_path) is managed


def test_every_failure_reading_the_mark_counts_as_unmanaged(tmp_path: Path) -> None:
    assert not carries_mark(7, proc=tmp_path)
    herd = ManagedHerd(Panes({}), proc=tmp_path)
    assert not herd.is_managed(PaneId("w1:p1"))


def test_a_recycled_pid_set_is_read_again(tmp_path: Path) -> None:
    fake_proc(tmp_path, 1, b"HERDR_PWA_MANAGED=1\0")
    fake_proc(tmp_path, 2, b"PATH=/bin\0")
    panes = Panes({"p": (1,)})
    herd = ManagedHerd(panes, proc=tmp_path)
    assert herd.is_managed(PaneId("p"))
    panes.pids["p"] = (2,)
    assert not herd.is_managed(PaneId("p"))
    herd.forget([])
    del panes.pids["p"]
    assert not herd.is_managed(PaneId("p"))
