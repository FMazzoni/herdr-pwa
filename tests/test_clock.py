"""Spans of time: one wording, at a glance, in full and aloud, wherever the app says an age."""

from datetime import UTC, datetime, timedelta

import pytest

from herdr_pwa.clock import ago, brief, duration, duration_said
from herdr_pwa.decisions import freshness
from herdr_pwa.engine import SessionUsage
from herdr_pwa.push import DeliveryRecord, health_line
from herdr_pwa.web.stats import Reading, stats_for

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC).timestamp()
TWO_DAYS_THREE_HOURS = 2 * 86_400 + 3 * 3_600


def test_spans_at_a_glance_in_full_and_aloud() -> None:
    spans = (40, 240, 4_320, 200_000)
    assert [duration(s) for s in spans] == ["40s", "4m", "1h 12m", "2d 7h"]
    assert [brief(s) for s in spans] == ["40s", "4m", "1h", "2d"]
    assert ago(TWO_DAYS_THREE_HOURS) == "2d ago"
    assert duration_said(240) == "4 minutes"
    assert duration_said(3_660) == "1 hour 1 minute"
    assert duration_said(180_000) == "2 days 2 hours"


def test_a_glance_counts_whole_units_and_never_rounds_up_into_the_next() -> None:
    assert [brief(s) for s in (59.5, 3_599, 86_399)] == ["59s", "59m", "23h"]


def test_a_span_from_a_clock_running_ahead_is_none_at_all() -> None:
    assert (duration(-5), brief(-5), ago(-5)) == ("0s", "0s", "0s ago")


def _sheet_ago(seconds: float) -> str:
    stamp = datetime.fromtimestamp(NOW, UTC) - timedelta(seconds=seconds)
    usage = SessionUsage(messages=1, last_at=stamp.isoformat())
    rows = {row.label: row.value for row in stats_for(Reading("claude", "idle", usage, NOW)).rows}
    return rows["Last activity"]


def _doctor_ago(seconds: float) -> str:
    line = health_line(DeliveryRecord(version=1, last_send_at=NOW - seconds), now=NOW)
    return line.removeprefix("push: last sent ").split(" · ")[0]


@pytest.mark.parametrize("seconds", [5, 59.5, 90, 7_200, 90_000, TWO_DAYS_THREE_HOURS])
def test_the_feed_doctor_and_the_sheet_say_an_age_the_same_way(seconds: float) -> None:
    said = {
        "feed": freshness(seconds, 5, None).age_label,
        "doctor": _doctor_ago(seconds),
        "sheet": _sheet_ago(seconds),
    }
    assert set(said.values()) == {ago(seconds)}, said
