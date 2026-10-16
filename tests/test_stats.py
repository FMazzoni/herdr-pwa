"""The stats strip: each harness's own record mapped to fields, and nothing invented."""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from herdr_pwa.engine import HistoryState, ModelRef, SessionUsage, UsageCache
from herdr_pwa.jsontypes import JsonObject, as_object, loads
from herdr_pwa.web.stats import (
    Stats,
    duration,
    duration_said,
    model_label,
    stats_for,
    tokens,
    window_of,
)
from tests import test_hermes
from tests.engine_seam import HERMES_DB, usage
from tests.webfakes import (
    READ,
    SNAPSHOT,
    WORKING_SESSION,
    build_rig,
    read_frame,
    snapshot_frame,
    write_transcript,
)

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC).timestamp()
FOUR_MINUTES_AGO = "2026-10-11T11:56:00.000Z"
OPENCODE = Path(__file__).parent / "fixtures" / "transcript" / "opencode"
OPENCODE_SESSION = "ses_3fd62ba5bffemIxWVhRda5wgS7"


def tally(model: str = "claude-haiku-4-5-20251001", **fields: object) -> SessionUsage:
    """A tally as an adapter leaves it, with one model having answered."""
    base: dict[str, object] = {
        "state": HistoryState.ok,
        "messages": 12,
        "input_tokens": 900,
        "output_tokens": 38_000,
        "cache_read_tokens": 1_200_000,
        "context_tokens": 50_000,
        "models": [ModelRef(model)] if model else [],
        "prompts": 3,
        "turn_started": FOUR_MINUTES_AGO,
        "last_at": FOUR_MINUTES_AGO,
    }
    base.update(fields)
    return SessionUsage.model_validate(base)


def labels(stats: Stats) -> dict[str, str]:
    return {row.label: row.value for row in stats.rows}


# ---- the context window: a table, defaulting to unknown --------------------------------------


def test_an_unknown_model_has_no_window_so_no_percent() -> None:
    assert window_of("moonshotai/Kimi-K2.5") == 0
    assert window_of("claude-opus-5") == 0
    stats = stats_for("claude", "idle", tally("claude-opus-5"), now=NOW)
    assert stats.percent is None
    assert stats.context == "50k ctx"
    assert labels(stats)["Context"] == "50k used; this model's window is not known"
    assert "percent" not in stats.sentence
    assert "context 50 thousand tokens" in stats.sentence


def test_a_known_model_shows_a_percent_and_a_meter() -> None:
    stats = stats_for("claude", "idle", tally(), now=NOW)
    assert stats.percent == 25
    assert stats.meter == 3
    assert stats.context == "25%"
    assert labels(stats)["Context"] == "50k of 200k (25%)"
    assert stats.sentence.startswith("Haiku 4.5, context 25 percent used")


def test_a_prompt_larger_than_the_listed_window_says_tokens_not_a_percent() -> None:
    """The session runs a larger window than the table knows, so a percent would be false."""
    stats = stats_for("claude", "idle", tally(context_tokens=420_000), now=NOW)
    assert stats.percent is None
    assert stats.context == "420k ctx"


# ---- money: only where the harness wrote it down ----------------------------------------------


def test_claude_records_no_money_so_no_cost_is_shown() -> None:
    stats = stats_for("claude", "idle", tally(cost=3.5), now=NOW)
    assert "Cost" not in labels(stats)
    assert stats.spend == "38k out"
    assert "$" not in stats.sentence


def test_opencode_shows_the_money_it_recorded() -> None:
    stats = stats_for("opencode", "idle", tally("x/Kimi-K2.5", cost=0.42), now=NOW)
    assert stats.spend == "$0.42"
    assert labels(stats)["Cost"] == "$0.42"
    assert "spent $0.42" in stats.sentence


def test_a_harness_that_recorded_no_cost_shows_none_never_zero() -> None:
    for kind in ("opencode", "hermes"):
        stats = stats_for(kind, "idle", tally("a-model", cost=0.0), now=NOW)
        assert "Cost" not in labels(stats), kind
        assert "$" not in stats.spend, kind


def test_hermes_stores_no_context_so_none_is_shown() -> None:
    stats = stats_for("hermes", "idle", tally("a-model", context_tokens=9000), now=NOW)
    assert stats.context == ""
    assert "Context" not in labels(stats)
    assert "context" not in stats.sentence


# ---- the turn and the rest of the sheet -------------------------------------------------------


def test_a_running_turn_is_timed_from_the_newest_prompt() -> None:
    stats = stats_for("claude", "working", tally(), now=NOW)
    assert stats.turn == "4m"
    assert stats.sentence == (
        "Haiku 4.5, context 25 percent used, turn running 4 minutes, 38 thousand output tokens"
    )


def test_a_turn_is_timed_only_while_one_is_running() -> None:
    assert stats_for("claude", "idle", tally(), now=NOW).turn == ""
    assert stats_for("claude", "blocked", tally(), now=NOW).turn == ""


def test_a_stamp_from_the_future_or_unreadable_is_not_a_duration() -> None:
    later = tally(turn_started="2026-10-11T13:00:00Z", last_at="garbage")
    stats = stats_for("claude", "working", later, now=NOW)
    assert stats.turn == ""
    assert "Last activity" not in labels(stats)


def test_the_sheet_names_every_figure_and_never_sums_them() -> None:
    rows = labels(stats_for("claude", "idle", tally(), now=NOW))
    assert rows["Input"] == "900"
    assert rows["Output"] == "38k"
    assert rows["Cache read"] == "1.2M"
    assert "Cache written" not in rows
    assert rows["Last activity"] == "4m ago"
    assert rows["Turns"] == "3"


def test_a_harness_with_no_store_says_so_and_shows_no_number() -> None:
    stats = stats_for("cursor", "working", SessionUsage(state=HistoryState.unsupported), now=NOW)
    assert stats.note == "No usage from this harness"
    assert (stats.model, stats.context, stats.turn, stats.spend) == ("", "", "", "")
    assert stats.rows == ()


def test_before_the_first_answer_nothing_is_counted() -> None:
    stats = stats_for("claude", "idle", SessionUsage(state=HistoryState.absent), now=NOW)
    assert stats.note == "No usage recorded yet"


@pytest.mark.parametrize(
    ("model", "said"),
    [
        ("claude-opus-4-5-20251101", "Opus 4.5"),
        ("claude-haiku-4-5-20251001", "Haiku 4.5"),
        ("claude-opus-5", "Opus 5"),
        ("moonshotai/Kimi-K2.5", "Kimi-K2.5"),
        ("gpt-5-20250807", "gpt-5"),
    ],
)
def test_a_model_id_is_said_as_a_person_says_it(model: str, said: str) -> None:
    assert model_label(model) == said


def test_token_counts_at_a_glance() -> None:
    assert [tokens(n) for n in (812, 1_400, 999_999, 2_300_000)] == ["812", "1.4k", "1.0M", "2.3M"]


# ---- each harness, from its own store ---------------------------------------------------------


def said(uuid: str, stamp: str, text: str = "go") -> JsonObject:
    return {
        "type": "user",
        "uuid": uuid,
        "timestamp": stamp,
        "promptSource": "typed",
        "message": {"role": "user", "content": text},
    }


def answered(message: str, stamp: str, *, prompt: int, out: int) -> JsonObject:
    return {
        "type": "assistant",
        "uuid": f"r-{message}",
        "timestamp": stamp,
        "message": {
            "id": message,
            "role": "assistant",
            "model": "claude-haiku-4-5-20251001",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": prompt, "output_tokens": out},
        },
    }


def _log(root: Path) -> Path:
    return root / "-tmp" / f"{WORKING_SESSION}.jsonl"


def test_claude_maps_its_transcript_and_grows_by_what_is_appended(tmp_path: Path) -> None:
    root = tmp_path / "transcripts"
    write_transcript(
        root,
        WORKING_SESSION,
        [
            said("u1", "2026-10-11T11:50:00Z"),
            answered("m1", "2026-10-11T11:51:00Z", prompt=40_000, out=1_000),
            said("u2", FOUR_MINUTES_AGO),
        ],
    )
    cache = UsageCache()
    first = cache.read(WORKING_SESSION, agent_kind="claude", root=str(root))
    assert (first.messages, first.prompts, first.context_tokens) == (1, 2, 40_000)
    assert first.turn_started == FOUR_MINUTES_AGO
    with _log(root).open("a") as log:
        log.write(json.dumps(answered("m2", "2026-10-11T11:58:00Z", prompt=50_000, out=2_000)))
        # Half a line is still being written: it is not read until it is whole.
        log.write("\n" + json.dumps(answered("m3", "2026-10-11T11:59:00Z", prompt=1, out=1))[:40])
    grown = cache.read(WORKING_SESSION, agent_kind="claude", root=str(root))
    assert (grown.messages, grown.output_tokens, grown.context_tokens) == (2, 3_000, 50_000)
    assert grown.last_at == "2026-10-11T11:58:00Z"
    stats = stats_for("claude", "working", grown, now=NOW)
    assert stats.sentence == (
        "Haiku 4.5, context 25 percent used, turn running 4 minutes, 3 thousand output tokens"
    )


def test_claude_reads_a_rewritten_log_afresh_rather_than_splicing(tmp_path: Path) -> None:
    root = tmp_path / "transcripts"
    write_transcript(root, WORKING_SESSION, [answered("m1", FOUR_MINUTES_AGO, prompt=9, out=500)])
    cache = UsageCache()
    assert cache.read(WORKING_SESSION, agent_kind="claude", root=str(root)).output_tokens == 500
    write_transcript(root, WORKING_SESSION, [answered("m9", FOUR_MINUTES_AGO, prompt=9, out=70)])
    assert cache.read(WORKING_SESSION, agent_kind="claude", root=str(root)).output_tokens == 70


def test_a_prompt_copied_into_a_fork_is_counted_once(tmp_path: Path) -> None:
    root = tmp_path / "transcripts"
    write_transcript(root, WORKING_SESSION, [said("u1", FOUR_MINUTES_AGO)] * 3)
    found = UsageCache().read(WORKING_SESSION, agent_kind="claude", root=str(root))
    assert found.prompts == 1


def test_opencode_maps_its_store_with_no_cost_when_it_recorded_none() -> None:
    """The captured session's messages all say cost 0: no source, so no cost and no zero."""
    found = usage(OPENCODE_SESSION, agent_kind="opencode", root=OPENCODE)
    assert found.prompts > 0
    assert found.turn_started
    stats = stats_for("opencode", "working", found, now=found_now(found.turn_started, 90))
    assert stats.model == "Kimi-K2.5"
    assert stats.percent is None
    assert stats.context.endswith(" ctx")
    assert stats.turn == "1m"
    assert "Cost" not in labels(stats)


def test_opencode_shows_the_cost_its_messages_record(tmp_path: Path) -> None:
    store = tmp_path / "storage"
    for source in OPENCODE.rglob("*.json"):
        target = store / source.relative_to(OPENCODE)
        target.parent.mkdir(parents=True, exist_ok=True)
        record = as_object(loads(source.read_text()))
        assert record is not None
        if source.parent.parent.name == "message" and record.get("role") == "assistant":
            record["cost"] = 0.21
        target.write_text(json.dumps(record))
    found = usage(OPENCODE_SESSION, agent_kind="opencode", root=store)
    stats = stats_for("opencode", "idle", found, now=NOW)
    assert stats.spend.startswith("$")
    assert labels(stats)["Cost"] == stats.spend


def found_now(stamp: str, seconds: float) -> float:
    return datetime.fromisoformat(stamp).timestamp() + seconds


def test_hermes_maps_its_rollup_and_its_messages(tmp_path: Path) -> None:
    path = test_hermes.build(tmp_path / HERMES_DB)
    test_hermes.spend(path, cost_status="actual", actual_cost_usd=2.5)
    found = usage(test_hermes.SESSION, agent_kind="hermes", root=tmp_path)
    assert found.prompts == 1
    assert found.turn_started
    stats = stats_for("hermes", "working", found, now=found_now(found.turn_started, 30))
    assert stats.model == "a-model"
    assert stats.context == ""
    assert stats.spend == "$2.50"
    assert stats.turn == "30s"


def test_hermes_with_no_priced_row_shows_no_cost(tmp_path: Path) -> None:
    path = test_hermes.build(tmp_path / HERMES_DB)
    test_hermes.spend(path, cost_status="unknown", estimated_cost_usd=1.5)
    found = usage(test_hermes.SESSION, agent_kind="hermes", root=tmp_path)
    stats = stats_for("hermes", "idle", found, now=NOW)
    assert "Cost" not in labels(stats)
    assert stats.spend == "20 out"


def test_hermes_counts_only_live_prompts(tmp_path: Path) -> None:
    """A compaction leaves rows behind with ``active = 0``; they are not turns."""
    path = test_hermes.build(tmp_path / HERMES_DB)
    with sqlite3.connect(path) as db:
        db.execute("update messages set active = 0 where session_id = ?", (test_hermes.SESSION,))
    found = usage(test_hermes.SESSION, agent_kind="hermes", root=tmp_path)
    assert found.prompts == 0
    assert found.turn_started == ""


# ---- the strip on the page --------------------------------------------------------------------


def _strip(page: str) -> str:
    bar = page[page.index('<header class="topbar">') : page.index("</header>")]
    return bar[bar.index('<div id="agent-stats"') :]


def _transcript(tmp_path: Path) -> None:
    records = [said("u1", FOUR_MINUTES_AGO), answered("m1", FOUR_MINUTES_AGO, prompt=50_000, out=7)]
    write_transcript(tmp_path / "transcripts", WORKING_SESSION, records)


@pytest.mark.parametrize("tab", ["chat", "pane"])
def test_the_strip_sits_in_the_bar_on_chat_and_pane(tmp_path: Path, tab: str) -> None:
    _transcript(tmp_path)
    rig = build_rig(tmp_path, {SNAPSHOT: snapshot_frame(), READ: read_frame("$ ")})
    strip = _strip(rig.http.get(f"/agent/{WORKING_SESSION}?tab={tab}").text)
    assert f'data-hp-topic="stats:{WORKING_SESSION}"' in strip
    assert f'hx-get="/agent/{WORKING_SESSION}/stats"' in strip
    # One sentence for VoiceOver; the drawn facts are hidden from it.
    assert '<span class="sr-only">Haiku 4.5, context 25 percent used' in strip
    assert '<span class="stats__facts" aria-hidden="true">' in strip
    assert "meter__fill--3" in strip


def test_the_pane_no_longer_draws_its_own_status_strip(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    pane = rig.http.get(f"/agent/{WORKING_SESSION}/pane").text
    assert "pane--status" not in pane


def test_the_strip_refreshes_through_its_own_route(tmp_path: Path) -> None:
    _transcript(tmp_path)
    rig = build_rig(tmp_path)
    answer = rig.http.get(f"/agent/{WORKING_SESSION}/stats")
    assert answer.status_code == 200
    assert answer.text.lstrip().startswith('<div id="agent-stats"')
    assert "<dt>Turns</dt><dd>1</dd>" in answer.text
    hidden = rig.http.get(f"/agent/{WORKING_SESSION}/stats", headers={"HP-Hidden": "1"})
    assert hidden.status_code == 286


def test_the_files_tab_is_a_redirect_into_the_agents_files(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    found = rig.http.get(f"/agent/{WORKING_SESSION}?tab=files", follow_redirects=False)
    assert found.status_code == 303
    assert found.headers["location"] == f"/agent/{WORKING_SESSION}/files"


def test_spans_at_a_glance_and_aloud() -> None:
    assert [duration(s) for s in (40, 240, 4_320, 200_000)] == ["40s", "4m", "1h 12m", "2d 7h"]
    assert duration_said(240) == "4 minutes"
    assert duration_said(3_660) == "1 hour 1 minute"
    assert duration_said(180_000) == "2 days 2 hours"
