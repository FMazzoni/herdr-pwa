"""The stats strip: each harness's own record mapped to fields, and nothing invented."""

import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import JsonValue, TypeAdapter

from herdr_pwa.engine import (
    EngineUnavailable,
    HistoryState,
    ModelRef,
    ModelSpend,
    SessionUsage,
    Source,
    UsageCache,
    locate_log,
)
from herdr_pwa.web.stats import (
    Reading,
    Stats,
    model_label,
    stats_for,
    tokens,
    window_of,
)
from tests import test_hermes, test_opencode
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
OPENCODE_SESSION = "ses_ea388ff7fffezqx9eAc8o7s8GI"


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
        "latest": ModelRef(model),
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
    stats = stats_for(Reading("claude", "idle", tally("claude-opus-5"), NOW))
    assert stats.percent is None
    assert stats.context == "50k ctx"
    assert labels(stats)["Context"] == "50k used; this model's window is not known"
    assert "percent" not in stats.sentence
    assert "context 50 thousand tokens" in stats.sentence


def test_a_known_model_shows_a_percent_and_a_meter() -> None:
    stats = stats_for(Reading("claude", "idle", tally(), NOW))
    assert stats.percent == 25
    assert stats.meter == 3
    assert stats.context == "25%"
    assert labels(stats)["Context"] == "50k of 200k (25%)"
    assert stats.sentence.startswith("Haiku 4.5, context 25 percent used")


def test_a_prompt_larger_than_the_listed_window_says_tokens_not_a_percent() -> None:
    """The session runs a larger window than the table knows, so a percent would be false."""
    stats = stats_for(Reading("claude", "idle", tally(context_tokens=420_000), NOW))
    assert stats.percent is None
    assert stats.context == "420k ctx"


# ---- money: only where the harness wrote it down ----------------------------------------------


def test_claude_records_no_money_so_no_cost_is_shown() -> None:
    stats = stats_for(Reading("claude", "idle", tally(cost=3.5), NOW))
    assert "Cost" not in labels(stats)
    assert stats.spend == "38k out"
    assert "$" not in stats.sentence


def test_opencode_shows_the_money_it_recorded() -> None:
    stats = stats_for(Reading("opencode", "idle", tally("x/Kimi-K2.5", cost=0.42), NOW))
    assert stats.spend == "$0.42"
    assert labels(stats)["Cost"] == "$0.42"
    assert "spent $0.42" in stats.sentence


def test_a_harness_that_recorded_no_cost_shows_none_never_zero() -> None:
    for kind in ("opencode", "hermes"):
        stats = stats_for(Reading(kind, "idle", tally("a-model", cost=0.0), NOW))
        assert "Cost" not in labels(stats), kind
        assert "$" not in stats.spend, kind


def test_hermes_stores_no_context_so_none_is_shown() -> None:
    stats = stats_for(Reading("hermes", "idle", tally("a-model", context_tokens=9000), NOW))
    assert stats.context == ""
    assert "Context" not in labels(stats)
    assert "context" not in stats.sentence


# ---- the turn and the rest of the sheet -------------------------------------------------------


def test_a_running_turn_is_timed_from_the_newest_prompt() -> None:
    stats = stats_for(Reading("claude", "working", tally(), NOW))
    assert stats.turn == "4m"
    assert stats.sentence == (
        "Haiku 4.5, context 25 percent used, turn running 4 minutes, 38 thousand output tokens"
    )


def test_a_day_long_turn_is_one_short_unit_on_the_strip_and_whole_in_the_sheet() -> None:
    usage = tally(turn_started="2026-10-09T09:00:00Z")
    stats = stats_for(Reading("claude", "working", usage, NOW))
    assert stats.turn == "2d"
    assert labels(stats)["Turn"] == "2d 3h"
    assert "turn running 2 days 3 hours" in stats.sentence


def test_a_turn_is_timed_only_while_one_is_running() -> None:
    assert stats_for(Reading("claude", "idle", tally(), NOW)).turn == ""
    assert stats_for(Reading("claude", "blocked", tally(), NOW)).turn == ""


def test_a_stamp_from_the_future_or_unreadable_is_not_a_duration() -> None:
    later = tally(turn_started="2026-10-11T13:00:00Z", last_at="garbage")
    stats = stats_for(Reading("claude", "working", later, NOW))
    assert stats.turn == ""
    assert "Last activity" not in labels(stats)


def test_the_sheet_names_every_figure_and_never_sums_them() -> None:
    rows = labels(stats_for(Reading("claude", "idle", tally(), NOW)))
    assert rows["Input"] == "900"
    assert rows["Output"] == "38k"
    assert rows["Cache read"] == "1.2M"
    assert "Cache written" not in rows
    assert rows["Last activity"] == "4m ago"
    assert rows["Turns"] == "3"


def test_a_harness_with_no_store_says_so_and_shows_no_number() -> None:
    stats = stats_for(
        Reading("cursor", "working", SessionUsage(state=HistoryState.unsupported), NOW)
    )
    assert stats.note == "No usage from this harness"
    assert (stats.model, stats.context, stats.turn, stats.spend) == ("", "", "", "")
    assert stats.rows == ()


def test_before_the_first_answer_nothing_is_counted() -> None:
    stats = stats_for(Reading("claude", "idle", SessionUsage(state=HistoryState.absent), NOW))
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


def said(uuid: str, stamp: str, text: str = "go") -> dict[str, JsonValue]:
    return {
        "type": "user",
        "uuid": uuid,
        "timestamp": stamp,
        "promptSource": "typed",
        "message": {"role": "user", "content": text},
    }


def answered(message: str, stamp: str, *, prompt: int, out: int) -> dict[str, JsonValue]:
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
    first = cache.read(locate_log(Source.of(WORKING_SESSION, agent_kind="claude", root=str(root))))
    assert (first.messages, first.prompts, first.context_tokens) == (1, 2, 40_000)
    assert first.turn_started == FOUR_MINUTES_AGO
    with _log(root).open("a") as log:
        log.write(json.dumps(answered("m2", "2026-10-11T11:58:00Z", prompt=50_000, out=2_000)))
        # Half a line is still being written: it is not read until it is whole.
        log.write("\n" + json.dumps(answered("m3", "2026-10-11T11:59:00Z", prompt=1, out=1))[:40])
    grown = cache.read(locate_log(Source.of(WORKING_SESSION, agent_kind="claude", root=str(root))))
    assert (grown.messages, grown.output_tokens, grown.context_tokens) == (2, 3_000, 50_000)
    assert grown.last_at == "2026-10-11T11:58:00Z"
    stats = stats_for(Reading("claude", "working", grown, NOW))
    assert stats.sentence == (
        "Haiku 4.5, context 25 percent used, turn running 4 minutes, 3 thousand output tokens"
    )


def test_claude_reads_a_rewritten_log_afresh_rather_than_splicing(tmp_path: Path) -> None:
    root = tmp_path / "transcripts"
    write_transcript(root, WORKING_SESSION, [answered("m1", FOUR_MINUTES_AGO, prompt=9, out=500)])
    cache = UsageCache()
    assert (
        cache.read(
            locate_log(Source.of(WORKING_SESSION, agent_kind="claude", root=str(root)))
        ).output_tokens
        == 500
    )
    write_transcript(root, WORKING_SESSION, [answered("m9", FOUR_MINUTES_AGO, prompt=9, out=70)])
    assert (
        cache.read(
            locate_log(Source.of(WORKING_SESSION, agent_kind="claude", root=str(root)))
        ).output_tokens
        == 70
    )


def test_a_prompt_copied_into_a_fork_is_counted_once(tmp_path: Path) -> None:
    root = tmp_path / "transcripts"
    write_transcript(root, WORKING_SESSION, [said("u1", FOUR_MINUTES_AGO)] * 3)
    found = UsageCache().read(
        locate_log(Source.of(WORKING_SESSION, agent_kind="claude", root=str(root)))
    )
    assert found.prompts == 1


def test_opencode_maps_its_store_with_no_cost_when_it_recorded_none() -> None:
    """The captured session's messages all say cost 0: no source, so no cost and no zero."""
    found = usage(Source.of(OPENCODE_SESSION, agent_kind="opencode", root=OPENCODE))
    assert found.prompts > 0
    assert found.turn_started
    stats = stats_for(Reading("opencode", "working", found, found_now(found.turn_started, 90)))
    assert stats.model == "glm-5.3-flash-free"
    assert stats.percent is None
    assert stats.context.endswith(" ctx")
    assert stats.turn == "1m"
    assert "Cost" not in labels(stats)


def test_opencode_shows_the_cost_its_messages_record(tmp_path: Path) -> None:
    store = tmp_path / "storage"
    for source in OPENCODE.rglob("*.json"):
        target = store / source.relative_to(OPENCODE)
        target.parent.mkdir(parents=True, exist_ok=True)
        record = TypeAdapter(dict[str, JsonValue]).validate_json(source.read_text())
        if source.parent.parent.name == "message" and record.get("role") == "assistant":
            record["cost"] = 0.21
        target.write_text(json.dumps(record))
    found = usage(Source.of(OPENCODE_SESSION, agent_kind="opencode", root=store))
    stats = stats_for(Reading("opencode", "idle", found, NOW))
    assert stats.spend.startswith("$")
    assert labels(stats)["Cost"] == stats.spend


def found_now(stamp: str, seconds: float) -> float:
    return datetime.fromisoformat(stamp).timestamp() + seconds


def test_hermes_maps_its_rollup_and_its_messages(tmp_path: Path) -> None:
    path = test_hermes.build(tmp_path / HERMES_DB)
    test_hermes.spend(path, cost_status="actual", actual_cost_usd=2.5)
    found = usage(Source.of(test_hermes.SESSION, agent_kind="hermes", root=tmp_path))
    assert found.prompts == 1
    assert found.turn_started
    stats = stats_for(Reading("hermes", "working", found, found_now(found.turn_started, 30)))
    assert stats.model == "a-model"
    assert stats.context == ""
    assert stats.spend == "$2.50"
    assert stats.turn == "30s"


def test_hermes_with_no_priced_row_shows_no_cost(tmp_path: Path) -> None:
    path = test_hermes.build(tmp_path / HERMES_DB)
    test_hermes.spend(path, cost_status="unknown", estimated_cost_usd=1.5)
    found = usage(Source.of(test_hermes.SESSION, agent_kind="hermes", root=tmp_path))
    stats = stats_for(Reading("hermes", "idle", found, NOW))
    assert "Cost" not in labels(stats)
    assert stats.spend == "20 out"


def test_hermes_counts_only_live_prompts(tmp_path: Path) -> None:
    """A compaction leaves rows behind with ``active = 0``; they are not turns."""
    path = test_hermes.build(tmp_path / HERMES_DB)
    with sqlite3.connect(path) as db:
        db.execute("update messages set active = 0 where session_id = ?", (test_hermes.SESSION,))
    found = usage(Source.of(test_hermes.SESSION, agent_kind="hermes", root=tmp_path))
    assert found.prompts == 0
    assert found.turn_started == ""


MODEL_SWITCH = Path(__file__).parent / "fixtures" / "transcript" / "notices" / "model-switch.jsonl"


def test_a_session_that_switched_models_and_back_names_the_one_answering_now(
    tmp_path: Path,
) -> None:
    """Opus, then Haiku, then Opus again: the strip says Opus, and both keep their totals."""
    root = tmp_path / "transcripts"
    records = TypeAdapter(list[dict[str, JsonValue]]).validate_python(
        [json.loads(line) for line in MODEL_SWITCH.read_text().splitlines()]
    )
    write_transcript(root, WORKING_SESSION, records)
    found = UsageCache().read(
        locate_log(Source.of(WORKING_SESSION, agent_kind="claude", root=str(root)))
    )
    assert [ref.model for ref in found.models] == [
        "claude-opus-4-5-20251101",
        "claude-haiku-4-5-20251001",
    ]
    assert stats_for(Reading("claude", "idle", found, NOW)).model == "Opus 4.5"


def test_a_fork_with_no_answer_yet_names_the_model_its_parent_answered_with_last(
    tmp_path: Path,
) -> None:
    root = tmp_path / "transcripts"
    records = TypeAdapter(list[dict[str, JsonValue]]).validate_python(
        [json.loads(line) for line in MODEL_SWITCH.read_text().splitlines()]
    )
    write_transcript(root, "parent-session", records)
    child = said("u-fork", FOUR_MINUTES_AGO)
    child["sessionId"] = WORKING_SESSION
    child["forkedFrom"] = {"sessionId": "parent-session", "messageUuid": "x"}
    write_transcript(root, WORKING_SESSION, [child])
    found = UsageCache().read(
        locate_log(Source.of(WORKING_SESSION, agent_kind="claude", root=str(root)))
    )
    assert found.messages == 3
    assert stats_for(Reading("claude", "idle", found, NOW)).model == "Opus 4.5"


def test_opencode_names_the_model_that_answered_last_not_the_last_one_met(
    tmp_path: Path,
) -> None:
    """The captured session's last answer, moved back onto the model it started on."""
    rows = test_opencode.edited(
        9,
        '"deepseek-v4.1-flash-free","providerID":"opencode"',
        '"zai-org/GLM-5.2","providerID":"togetherai"',
    )
    test_opencode.build(tmp_path / "opencode.db", rows)
    found = usage(Source.of(MIXED, agent_kind="opencode", root=tmp_path))
    assert found.models[-1].model == "zai-org/GLM-5.3-Flash"
    assert stats_for(Reading("opencode", "idle", found, NOW)).model == "GLM-5.2"


def test_hermes_names_the_model_its_rollup_saw_last(tmp_path: Path) -> None:
    """Hermes keeps one row per model; its ``last_seen`` says which answered most recently."""
    path = test_hermes.build(tmp_path / HERMES_DB)
    test_hermes.spend(path, model="now-model", first_seen=1.0, last_seen=30.0)
    test_hermes.spend(path, model="then-model", first_seen=10.0, last_seen=20.0)
    found = usage(Source.of(test_hermes.SESSION, agent_kind="hermes", root=tmp_path))
    assert stats_for(Reading("hermes", "idle", found, NOW)).model == "now-model"


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


@pytest.mark.parametrize("tab", ["chat", "pane"])
def test_the_page_reads_the_pane_once_for_the_strip_and_the_tab_together(
    tmp_path: Path, tab: str
) -> None:
    rig = build_rig(tmp_path, {SNAPSHOT: snapshot_frame(), READ: read_frame("$ ")})
    assert rig.http.get(f"/agent/{WORKING_SESSION}?tab={tab}").status_code == 200
    assert rig.wire.calls.count(READ) == 1, rig.wire.calls


def test_the_strip_route_reads_the_pane_once_on_its_own(tmp_path: Path) -> None:
    rig = build_rig(tmp_path, {SNAPSHOT: snapshot_frame(), READ: read_frame("$ ")})
    assert rig.http.get(f"/agent/{WORKING_SESSION}/stats").status_code == 200
    assert rig.wire.calls.count(READ) == 1, rig.wire.calls


def test_the_files_tab_is_a_redirect_into_the_agents_files(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    found = rig.http.get(f"/agent/{WORKING_SESSION}?tab=files", follow_redirects=False)
    assert found.status_code == 303
    assert found.headers["location"] == f"/agent/{WORKING_SESSION}/files"


# ---- spend per model ----------------------------------------------------------------------------

MIXED = "ses_ea397b0caffeA2IKEmeHbtj4hG"


def mixed_store(tmp_path: Path, keep: tuple[str, ...] = ()) -> SessionUsage:
    """The captured mixed-model rows read through the adapter; *keep* narrows them to those models."""
    rows = test_opencode.MIXED_ROWS
    if keep:
        rows = {
            **rows,
            "message": [
                row
                for row in rows["message"]
                if '"role":"user"' in str(row["data"])
                or any(f'"modelID":"{model}"' in str(row["data"]) for model in keep)
            ],
        }
    test_opencode.build(tmp_path / "opencode.db", rows)
    return usage(Source.of(MIXED, agent_kind="opencode", root=tmp_path))


def models(stats: Stats) -> dict[str, str]:
    return {row.label: row.value for row in stats.models}


def test_each_model_shows_its_tokens_cache_apart_and_its_own_money(tmp_path: Path) -> None:
    stats = stats_for(Reading("opencode", "idle", mixed_store(tmp_path), NOW))
    rows = models(stats)
    assert list(rows) == [
        "GLM-5.2 via togetherai",
        "MiniMax-M3 via togetherai",
        "GLM-5.3-Flash via togetherai",
        "deepseek-v4.1-flash-free via opencode",
    ]
    assert rows["GLM-5.2 via togetherai"] == (
        "2 answers: 12k in, 69 out, 36 thinking, 11k cache read; $0.02"
    )
    assert rows["MiniMax-M3 via togetherai"] == (
        "2 answers: 10k in, 44 out, 14k cache read; under $0.01"
    )


def test_a_model_whose_provider_records_nothing_reads_not_recorded(tmp_path: Path) -> None:
    """Zeros from a provider that writes none are neither free nor nothing used."""
    stats = stats_for(Reading("opencode", "idle", mixed_store(tmp_path), NOW))
    flash = models(stats)["GLM-5.3-Flash via togetherai"]
    assert flash == "1 answer; no tokens or cost recorded"
    assert "0" not in flash.replace("1 answer", "")
    assert "$" not in flash


def test_a_model_with_tokens_and_no_money_says_no_cost_not_zero(tmp_path: Path) -> None:
    stats = stats_for(Reading("opencode", "idle", mixed_store(tmp_path), NOW))
    assert models(stats)["deepseek-v4.1-flash-free via opencode"] == (
        "1 answer: 11k in, 2 out, 6 thinking, 1.7k cache read; no cost recorded"
    )


def test_the_totals_say_which_models_they_leave_out(tmp_path: Path) -> None:
    stats = stats_for(Reading("opencode", "idle", mixed_store(tmp_path), NOW))
    rows = labels(stats)
    assert rows["Not recorded"] == "GLM-5.3-Flash via togetherai, so the totals leave it out"
    assert rows["Cost"] == "$0.02"


def test_one_model_needs_no_split(tmp_path: Path) -> None:
    stats = stats_for(Reading("opencode", "idle", mixed_store(tmp_path, ("zai-org/GLM-5.2",)), NOW))
    assert stats.models == ()
    assert "Not recorded" not in labels(stats)


def test_a_session_whose_only_provider_records_nothing_shows_no_zero(tmp_path: Path) -> None:
    """The strip and the sheet say "not recorded", never 0 tokens and never $0."""
    found = mixed_store(tmp_path, ("zai-org/GLM-5.3-Flash",))
    stats = stats_for(Reading("opencode", "idle", found, NOW))
    assert stats.model == "GLM-5.3-Flash"
    assert stats.spend == ""
    rows = labels(stats)
    assert rows["Tokens"] == "not recorded by togetherai"
    assert "Input" not in rows
    assert "Output" not in rows
    assert "Cost" not in rows
    assert stats.models == ()


def test_money_under_a_cent_is_not_rounded_to_nothing() -> None:
    stats = stats_for(Reading("opencode", "idle", tally("x/Kimi-K2.5", cost=0.004), NOW))
    assert stats.spend == "under $0.01"
    assert labels(stats)["Cost"] == "under $0.01"


def test_a_harness_that_does_not_price_itself_shows_no_money_per_model() -> None:
    split = tally(
        cost=1.5,
        by_model=[
            ModelSpend(ref=ModelRef("claude-haiku-4-5"), messages=2, output_tokens=9, cost=1.0),
            ModelSpend(ref=ModelRef("claude-opus-4-5"), messages=1, output_tokens=3, cost=0.5),
        ],
    )
    rows = models(stats_for(Reading("claude", "idle", split, NOW)))
    assert rows == {
        "Haiku 4.5": "2 answers: 0 in, 9 out; no cost recorded",
        "Opus 4.5": "1 answer: 0 in, 3 out; no cost recorded",
    }


# ---- the statusline the terminal shows ---------------------------------------------------------

RIG = Path(__file__).parent / "fixtures" / "rig"
STATUSLINE_SCREEN = (RIG / "claude-statusline.ansi").read_text()
NARROW_SCREEN = (RIG / "claude-statusline-narrow.ansi").read_text()


def _with_screen(tmp_path: Path, screen: str) -> str:
    _transcript(tmp_path)
    rig = build_rig(tmp_path, {SNAPSHOT: snapshot_frame(), READ: read_frame(screen)})
    return _strip(rig.http.get(f"/agent/{WORKING_SESSION}?tab=chat").text)


def test_a_pane_with_a_statusline_draws_it_in_the_strip_in_place_of_the_summed_facts(
    tmp_path: Path,
) -> None:
    strip = _with_screen(tmp_path, STATUSLINE_SCREEN)
    assert 'class="stats__status"' in strip
    # The numbers are the terminal's, in the colour it drew them, through the palette's tokens.
    assert '<span class="ansi-green">22%</span>' in strip
    assert "stats__model" not in strip
    assert "meter__fill" not in strip
    # VoiceOver hears the rows as printed, one sentence.
    assert '<span class="sr-only">Statusline: herdr-pwa [main] | ctx: 22% | 5h: 46%(now)' in strip
    # The sheet under the tap keeps the figures the log records.
    assert "<dt>Turns</dt><dd>1</dd>" in strip


def test_every_row_of_the_statusline_is_drawn_in_order(tmp_path: Path) -> None:
    strip = _with_screen(tmp_path, STATUSLINE_SCREEN)
    # The line has the first row; the sheet lists the one after it.
    rows = re.findall(r'<span class="stats__status-row">', strip)
    assert len(rows) == 1
    assert strip.index("Opus 5.5") < strip.index("auto mode on")


def test_every_colour_the_terminal_drew_reaches_the_strip_as_a_token_class(tmp_path: Path) -> None:
    strip = _with_screen(tmp_path, STATUSLINE_SCREEN)
    for cls in ("ansi-d", "ansi-green", "ansi-yellow", "ansi-red", "ansi-cyan"):
        assert f'<span class="{cls}">' in strip, cls
    assert '<span class="ansi-magenta ansi-b">Opus 5.5</span>' in strip


def test_the_statusline_is_drawn_in_segments_the_phone_wraps_between(tmp_path: Path) -> None:
    strip = _with_screen(tmp_path, STATUSLINE_SCREEN)
    segments = re.findall(r'<span class="stats__seg">(.*?)</span></span>', strip)
    assert len(segments) >= 15
    assert '<span class="stats__seg"><span class="ansi-d">|</span></span>' in strip


def test_a_pane_without_a_statusline_leaves_the_strip_as_it_was(tmp_path: Path) -> None:
    strip = _with_screen(tmp_path, "$ ")
    assert "stats__status" not in strip
    assert "stats__model" in strip
    assert "meter__fill--3" in strip


def test_an_unreadable_pane_still_serves_the_strip_from_the_log(tmp_path: Path) -> None:
    _transcript(tmp_path)
    rig = build_rig(
        tmp_path, {SNAPSHOT: snapshot_frame(), READ: EngineUnavailable("the runner is away")}
    )
    answer = rig.http.get(f"/agent/{WORKING_SESSION}/stats")
    assert answer.status_code == 200
    assert "stats__status" not in answer.text
    assert "stats__model" in answer.text


def test_a_read_that_is_not_a_pane_still_serves_the_strip_from_the_log(tmp_path: Path) -> None:
    _transcript(tmp_path)
    broken: dict[str, JsonValue] = {"id": "x", "result": {"type": "pane_read", "read": "no"}}
    rig = build_rig(tmp_path, {SNAPSHOT: snapshot_frame(), READ: broken})
    answer = rig.http.get(f"/agent/{WORKING_SESSION}/stats")
    assert answer.status_code == 200
    assert "stats__model" in answer.text


def test_the_strips_own_route_carries_the_statusline(tmp_path: Path) -> None:
    _transcript(tmp_path)
    rig = build_rig(tmp_path, {SNAPSHOT: snapshot_frame(), READ: read_frame(STATUSLINE_SCREEN)})
    answer = rig.http.get(f"/agent/{WORKING_SESSION}/stats")
    assert 'class="stats__status"' in answer.text


def test_a_narrow_pane_cuts_the_row_and_the_strip_shows_what_the_screen_shows(
    tmp_path: Path,
) -> None:
    strip = _with_screen(tmp_path, NARROW_SCREEN)
    assert '<span class="ansi-red">91</span>' in strip
    assert "Opus 5.5" not in strip


# ---- the shapes people run: powerline, bars and icons, several rows -------------------------

POWERLINE_SCREEN = (RIG / "claude-statusline-powerline.ansi").read_text()
BARS_SCREEN = (RIG / "claude-statusline-bars.ansi").read_text()
ROWS_SCREEN = (RIG / "claude-statusline-rows.ansi").read_text()
PRIVATE_USE = re.compile(r"[-]")


def _summary_and_sheet(strip: str) -> tuple[str, str]:
    line = strip[strip.index('<span class="stats__status"') : strip.index("</summary>")]
    sheet = strip[strip.index('<div class="stats__status-rows">') : strip.index("</div>")]
    return line, sheet


def test_the_line_is_the_first_row_only_and_the_sheet_has_the_rows_after_it(
    tmp_path: Path,
) -> None:
    strip = _with_screen(tmp_path, ROWS_SCREEN)
    line, sheet = _summary_and_sheet(strip)
    assert "Opus 5.5" in line
    assert "ctx" not in line
    assert "stats__status-row" not in line
    assert len(re.findall(r'<span class="stats__status-row">', sheet)) == 3
    assert sheet.index("22%") < sheet.index("6h34m") < sheet.index("auto mode")
    assert "Statusline: Opus 5.5 | herdr-pwa [main]; ctx 22% | 5h 46%(now) | 7d 91%(1d5h);" in strip


def test_a_powerline_statusline_keeps_its_words_and_colours_and_draws_no_private_use_glyph(
    tmp_path: Path,
) -> None:
    strip = _with_screen(tmp_path, POWERLINE_SCREEN)
    assert not PRIVATE_USE.search(strip)
    line, sheet = _summary_and_sheet(strip)
    for word in ("main", "Opus 5.5", "ctx 22%", "5h 46% · 7d 91%"):
        assert word in line, word
    assert "auto mode on" in sheet
    # Truecolour foregrounds land on the nearest of the sixteen tokens; backgrounds are not drawn.
    assert 'class="ansi-bright-white"' in line
    assert "ansi-bg" not in strip
    assert "Statusline: main Opus 5.5 ctx 22% 5h 46% · 7d 91%;" in strip


def test_a_bar_statusline_splits_at_its_bars_and_bullets_and_keeps_its_blocks_and_emoji(
    tmp_path: Path,
) -> None:
    strip = _with_screen(tmp_path, BARS_SCREEN)
    line, _ = _summary_and_sheet(strip)
    pieces: list[str] = re.findall(r'<span class="stats__seg">(.*?)</span></span>', line)
    texts = [re.sub(r"<[^>]+>", "", piece) for piece in pieces]
    assert texts == [
        "█████░░░░░ 52%", "│", "⏱ 12m", "│", "💰 $1.20", "•", "🧠 Opus 5.5", "•", "\u276f main",
    ]  # fmt: skip
    assert '<span class="ansi-green">█████</span>' in line


def test_the_first_row_is_drawn_once_in_the_line_and_the_sheet_lists_the_rows_after_it(
    tmp_path: Path,
) -> None:
    """Open, the line's row is shown whole by CSS, so the sheet repeating it would draw it twice."""
    strip = _with_screen(tmp_path, ROWS_SCREEN)
    line, sheet = _summary_and_sheet(strip)
    assert "Opus 5.5" in line
    assert "Opus 5.5" not in sheet
    assert len(re.findall(r'<span class="stats__status-row">', sheet)) == 3
    assert sheet.index("22%") < sheet.index("6h34m") < sheet.index("auto mode")
