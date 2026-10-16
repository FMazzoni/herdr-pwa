import logging
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import JsonValue, TypeAdapter

from herdr_pwa import audit
from herdr_pwa.audit import (
    CLOSE,
    KEYS,
    OMITTED_NO_ECHO,
    PROMPT,
    START,
    AuditLog,
    AuditUnavailableError,
    Origin,
    Recorder,
    Write,
    describe,
    records,
)
from herdr_pwa.auth import Identity

ME = Origin(
    actor="you@example.com", via="tailscale", client_ip="127.0.0.1", forwarded_for="100.64.0.7"
)


def fixed_clock() -> datetime:
    return datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def make_log(
    state: Path, *, max_bytes: int = 5 * 1024 * 1024, payload_chars: int = 512
) -> AuditLog:
    return AuditLog(state, max_bytes=max_bytes, payload_chars=payload_chars, clock=fixed_clock)


LINE: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


def lines(state: Path, name: str = audit.AUDIT_FILENAME) -> list[dict[str, JsonValue]]:
    return [LINE.validate_json(line) for line in (state / name).read_text().splitlines()]


def test_the_record_is_jsonl_at_0600_in_a_0700_directory(tmp_path: Path) -> None:
    state = tmp_path / "state"
    with make_log(state).record(ME, Write(PROMPT, target="w1:p1", payload="hi")) as entry:
        entry.outcome("sent")
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE((state / audit.AUDIT_FILENAME).stat().st_mode) == 0o600
    attempt, outcome = lines(state)
    assert attempt["ts"] == "2026-10-10T12:00:00+00:00"
    assert (attempt["phase"], outcome["phase"], outcome["result"]) == ("attempt", "outcome", "sent")


def test_a_mode_that_drifted_is_put_back_before_anything_is_written(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    log.preflight()
    (tmp_path / audit.AUDIT_FILENAME).chmod(0o644)
    _ = log.record(ME, Write(KEYS, payload="Enter"))
    assert stat.S_IMODE((tmp_path / audit.AUDIT_FILENAME).stat().st_mode) == 0o600


def test_the_vocabulary_is_the_four_writes_the_runner_is_asked_to_make() -> None:
    assert {PROMPT, KEYS, START, CLOSE} == audit.ENDPOINTS


def test_anything_outside_the_vocabulary_is_refused_and_nothing_written(tmp_path: Path) -> None:
    for endpoint in ("read", "push_subscribe", "push_unsubscribe", "refused"):
        with pytest.raises(ValueError, match="not one of"):
            _ = make_log(tmp_path).record(ME, Write(endpoint))
    assert not (tmp_path / audit.AUDIT_FILENAME).exists()


def test_the_attempt_is_on_disk_before_the_write_is_made(tmp_path: Path) -> None:
    entry = make_log(tmp_path).record(ME, Write(PROMPT, payload="go"))
    (attempt,) = lines(tmp_path)
    assert (attempt["id"], attempt["phase"]) == (entry.id, "attempt")


def test_both_records_share_an_id_and_an_exception_is_recorded_then_left_alone(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError), make_log(tmp_path).record(ME, Write(CLOSE, target="w1:p1")):
        raise RuntimeError
    attempt, outcome = lines(tmp_path)
    assert attempt["id"] == outcome["id"]
    assert outcome["result"] == "error:RuntimeError"


def test_a_write_that_names_no_outcome_says_so(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR), make_log(tmp_path).record(ME, Write(START)):
        pass
    assert lines(tmp_path)[1]["result"] == audit.UNRECORDED
    assert "named no outcome" in caplog.text


def test_an_attempt_that_cannot_be_written_refuses_the_write(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-dir"
    _ = blocker.write_text("")
    log = make_log(blocker)
    with pytest.raises(AuditUnavailableError):
        log.preflight()
    with pytest.raises(AuditUnavailableError):
        _ = log.record(ME, Write(PROMPT, payload="rm -rf"))


def test_an_outcome_that_cannot_be_written_logs_loudly_and_never_raises(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    log = make_log(tmp_path)
    entry = log.record(ME, Write(PROMPT, payload="x"))
    (tmp_path / audit.AUDIT_FILENAME).unlink()
    (tmp_path / audit.AUDIT_FILENAME).mkdir()
    with caplog.at_level(logging.ERROR), entry:
        entry.outcome("sent")
    assert "HAPPENED" in caplog.text
    assert "this may have run" in caplog.text


def test_a_long_payload_is_truncated_visibly_in_band(tmp_path: Path) -> None:
    _ = make_log(tmp_path, payload_chars=10).record(ME, Write(PROMPT, payload="a" * 25))
    (attempt,) = lines(tmp_path)
    assert attempt["payload"] == "a" * 10 + "…[truncated: 25 chars sent]"
    assert (attempt["payload_truncated"], attempt["payload_chars"]) == (True, 25)


def test_a_payload_within_the_cap_is_stored_verbatim_on_one_line(tmp_path: Path) -> None:
    _ = make_log(tmp_path).record(ME, Write(PROMPT, payload="two\nlines"))
    assert len((tmp_path / audit.AUDIT_FILENAME).read_text().splitlines()) == 1
    (attempt,) = lines(tmp_path)
    assert (attempt["payload"], attempt["payload_truncated"]) == ("two\nlines", False)


def test_a_no_echo_prompt_keeps_the_length_and_never_the_text(tmp_path: Path) -> None:
    _ = make_log(tmp_path).record(ME, Write(PROMPT, payload="hunter2", omitted=OMITTED_NO_ECHO))
    assert "hunter2" not in (tmp_path / audit.AUDIT_FILENAME).read_text()
    (attempt,) = lines(tmp_path)
    assert (attempt["payload"], attempt["payload_omitted"], attempt["payload_chars"]) == (
        None,
        OMITTED_NO_ECHO,
        7,
    )


def test_actor_and_both_addresses_are_kept_on_both_auth_paths(tmp_path: Path) -> None:
    for identity in (Identity("you@example.com"), Identity("you@example.com", via="config")):
        recorder = Recorder(
            make_log(tmp_path),
            Origin.of(identity, client_ip="127.0.0.1", forwarded_for="100.64.0.7"),
        )
        _ = recorder.record(Write(KEYS, payload="1"))
    first, second = lines(tmp_path)
    assert (first["actor"], first["actor_via"], second["actor_via"]) == (
        "you@example.com",
        "tailscale",
        "config",
    )
    assert (first["client_ip"], first["forwarded_for"]) == ("127.0.0.1", "100.64.0.7")


def test_two_generations_bound_the_disk(tmp_path: Path) -> None:
    log = make_log(tmp_path, max_bytes=4096)
    for n in range(40):
        _ = log.record(ME, Write(PROMPT, payload=f"{n}" * 100))
    assert (tmp_path / audit.ROTATED_FILENAME).exists()
    assert (tmp_path / audit.AUDIT_FILENAME).stat().st_size <= 4096
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        audit.AUDIT_FILENAME,
        audit.ROTATED_FILENAME,
    ]


def test_reading_back_crosses_the_rotation_in_order(tmp_path: Path) -> None:
    log = make_log(tmp_path, max_bytes=4096)
    for n in range(40):
        _ = log.record(ME, Write(PROMPT, payload=f"{n:02d}" + "x" * 100))
    found = records(tmp_path, limit=0)
    numbers = [str(one["payload"])[:2] for one in found if one["phase"] == "attempt"]
    assert numbers == sorted(numbers)
    assert numbers[-1] == "39"


def test_a_damaged_line_is_reported_not_skipped(tmp_path: Path) -> None:
    _ = make_log(tmp_path).record(ME, Write(PROMPT, payload="ok"))
    with (tmp_path / audit.AUDIT_FILENAME).open("a") as handle:
        _ = handle.write("{torn\n")
    found = records(tmp_path)
    assert found[-1]["phase"] == audit.UNREADABLE
    assert "UNREADABLE RECORD at audit.log:2" in describe(found[-1])


def test_describe_shows_who_what_and_a_withheld_payload(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    with log.record(
        ME, Write(PROMPT, target="w1:p1", payload="pw", omitted=OMITTED_NO_ECHO)
    ) as entry:
        entry.outcome("sent")
    attempt, outcome = (describe(one) for one in records(tmp_path))
    assert "you@example.com via tailscale from 100.64.0.7" in attempt
    assert "<payload withheld: no_echo_prompt, 2 chars>" in attempt
    assert outcome.endswith("-> sent")
