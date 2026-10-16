"""A pane's reported session is checked against where the pane runs before it is trusted.

The case these reproduce: a bare nested opencode, spawned from inside an opencode pane, inherited
the pane's id and reported its own throwaway session over the real one, last writer wins. The
real agent and its store were untouched; only herdr's record of who holds the pane was wrong.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from herdr_pwa.decisions import identity_doubt
from herdr_pwa.engine import Agent, as_pane_id, as_session_id, session_origin
from herdr_pwa.jsontypes import JsonObject
from herdr_pwa.writes.lifecycle import EndAsk, end_agent
from herdr_pwa.writes.prompt import PromptAsk, PromptSent, send_prompt
from herdr_pwa.writes.refusal import Refusal, Refused
from tests._fake_herd import P3, SETTLED, FakeHerd, echoed, rig
from tests.webfakes import (
    READ,
    SNAPSHOT,
    WORKING_PANE,
    agents_of,
    build_rig,
    read_frame,
    snapshot_frame,
)

FIXTURES = Path(__file__).parent / "fixtures" / "transcript"
OPENCODE_SCHEMA = (FIXTURES / "opencode" / "schema.sql").read_text()
HERMES_SCHEMA = (FIXTURES / "hermes" / "schema.sql").read_text()

REPO = "/home/owner/projects/herdr-pwa"
THROWAWAY = "/tmp/opencode/perm-test"  # noqa: S108 - a recorded path, never opened
REAL = as_session_id("ses_ebd52da9ffferyXfceutz4VMZx")
STRAY = as_session_id("ses_ebd4bb9b3ffefe2V3uubh2DqkC")


def held(session: str, kind: str) -> Agent:
    """An agent of *kind* whose pane reports *session*."""
    return Agent(session=as_session_id(session), target=as_pane_id("w1:p1"), agent=kind)


def _session_row(session: str, directory: str) -> JsonObject:
    return {
        "id": session,
        "project_id": "p1",
        "slug": session,
        "directory": directory,
        "title": session,
        "version": "1.0.0",
        "time_created": 1,
        "time_updated": 1,
    }


def _turns(session: str, count: int) -> list[tuple[str, JsonObject, JsonObject]]:
    """``count`` user messages of one text part each."""
    found: list[tuple[str, JsonObject, JsonObject]] = []
    for n in range(count):
        message = f"msg_{session[4:12]}{n:06d}"
        found.append(
            (
                message,
                {"role": "user", "time": {"created": 1000 + n}},
                {"type": "text", "text": f"turn {n} of {session}"},
            )
        )
    return found


def opencode_store(data: Path, sessions: dict[str, tuple[str, int]]) -> Path:
    """An opencode.db under *data* holding each session at its directory with that many turns."""
    data.mkdir(parents=True, exist_ok=True)
    path = data / "opencode.db"
    with sqlite3.connect(path) as db:
        db.executescript(OPENCODE_SCHEMA)
        for session, (directory, count) in sessions.items():
            row = _session_row(session, directory)
            columns = ", ".join(f'"{k}"' for k in row)
            marks = ", ".join("?" for _ in row)
            db.execute(f"insert into session ({columns}) values ({marks})", tuple(row.values()))  # noqa: S608
            for n, (message, record, part) in enumerate(_turns(session, count)):
                db.execute(
                    "insert into message values (?, ?, ?, ?, ?)",
                    (message, session, n, n, json.dumps(record)),
                )
                db.execute(
                    "insert into part values (?, ?, ?, ?, ?, ?)",
                    (f"prt_{message[4:]}", message, session, n, n, json.dumps(part)),
                )
    return path


def the_hijack(data: Path) -> None:
    """The real session, long and in the repo; the stray one, four turns old and in /tmp."""
    opencode_store(data, {REAL: (REPO, 40), STRAY: (THROWAWAY, 4)})


# Where each harness says its session started


def test_opencode_says_where_a_session_started(tmp_path: Path) -> None:
    the_hijack(tmp_path)
    assert session_origin(held(REAL, "opencode"), root=tmp_path) == REPO
    assert session_origin(held(STRAY, "opencode"), root=tmp_path) == THROWAWAY


def test_claude_says_where_a_session_started(tmp_path: Path) -> None:
    session = "00000000-0000-4000-8000-000000000015"
    project = tmp_path / "-home-owner-projects-herdr-pwa"
    project.mkdir()
    records = [
        {"type": "summary", "summary": "no cwd on this one"},
        {"type": "user", "cwd": REPO, "uuid": "u1"},
        {"type": "user", "cwd": f"{REPO}/src", "uuid": "u2"},
    ]
    (project / f"{session}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    assert session_origin(held(session, "claude"), root=tmp_path) == REPO


def test_hermes_says_where_a_session_started(tmp_path: Path) -> None:
    with sqlite3.connect(tmp_path / "state.db") as db:
        db.executescript(HERMES_SCHEMA)
        db.execute(
            "insert into sessions (id, source, started_at, cwd) values (?, ?, ?, ?)",
            ("h1", "cli", 1.0, REPO),
        )
    assert session_origin(held("h1", "hermes"), root=tmp_path) == REPO


def test_a_session_no_store_holds_has_no_origin(tmp_path: Path) -> None:
    for kind in ("claude", "opencode", "hermes", "nobody"):
        assert session_origin(held(STRAY, kind), root=tmp_path) == "", kind


# Whether that fits the pane


@pytest.mark.parametrize(
    ("origin", "cwds"),
    [
        ("", (REPO,)),  # nothing recorded yet: no evidence either way
        (REPO, ()),  # herdr said nowhere: no evidence either way
        (REPO, (REPO,)),
        (REPO, (f"{REPO}/.worktrees/x",)),  # started at the root, now in a worktree under it
        (f"{REPO}/src", (REPO,)),  # started below where the pane is
        (f"{REPO}/", (REPO,)),
        (REPO, ("", REPO)),  # an empty cwd is skipped, not compared
        (THROWAWAY, (REPO, THROWAWAY)),  # either of herdr's two cwds will do
    ],
)
def test_a_session_that_started_where_the_pane_runs_is_not_doubted(
    origin: str, cwds: tuple[str, ...]
) -> None:
    assert identity_doubt(origin, cwds) == ""


def test_a_session_that_started_somewhere_unrelated_is_doubted() -> None:
    said = identity_doubt(THROWAWAY, (REPO, REPO))
    assert THROWAWAY in said
    assert REPO in said


def test_a_sibling_whose_name_merely_starts_the_same_is_unrelated() -> None:
    assert identity_doubt(f"{REPO}-v2", (REPO,)) != ""


# The hijack, through the write path


def _opencode_pane(herd: FakeHerd, session: str) -> None:
    herd.put(P3, session, agent="opencode", cwd=REPO, foreground_cwd=REPO)


def test_a_prompt_goes_to_the_pane_while_it_reports_its_own_session(tmp_path: Path) -> None:
    the_hijack(Path.home() / ".local" / "share" / "opencode")
    herd = FakeHerd()
    _opencode_pane(herd, REAL)
    herd.screens[P3] = [SETTLED, echoed("\u276f hello")]
    r = rig(tmp_path, herd)
    assert isinstance(send_prompt(r.writer, PromptAsk(REAL, "hello")), PromptSent)
    assert len(herd.writes("agent.prompt")) == 1


def test_a_prompt_is_refused_once_a_stray_session_claims_the_pane(tmp_path: Path) -> None:
    the_hijack(Path.home() / ".local" / "share" / "opencode")
    herd = FakeHerd()
    _opencode_pane(herd, REAL)
    herd.screens[P3] = [SETTLED]
    r = rig(tmp_path, herd)
    # The second report on the same pane: the nested process's own session, last writer wins.
    _opencode_pane(herd, STRAY)

    got = send_prompt(r.writer, PromptAsk(STRAY, "hello"))

    assert isinstance(got, Refused), got
    assert got.refusal is Refusal.DOUBTED_IDENTITY
    assert THROWAWAY in got.detail
    assert herd.writes("agent.prompt") == [], "the keystrokes would have reached the REAL agent"
    assert r.audit_records == [], "refused before the attempt, so nothing was recorded as tried"


def test_the_real_session_is_still_gone_rather_than_typed_at(tmp_path: Path) -> None:
    the_hijack(Path.home() / ".local" / "share" / "opencode")
    herd = FakeHerd()
    _opencode_pane(herd, STRAY)
    r = rig(tmp_path, herd)
    got = send_prompt(r.writer, PromptAsk(REAL, "hello"))
    assert isinstance(got, Refused)
    assert got.refusal is Refusal.GONE


def test_closing_a_doubted_pane_is_refused_too(tmp_path: Path) -> None:
    """Closing it would end the real agent while the phone believes it ends the stray one."""
    the_hijack(Path.home() / ".local" / "share" / "opencode")
    herd = FakeHerd()
    _opencode_pane(herd, STRAY)
    r = rig(tmp_path, herd)
    got = end_agent(r.writer, EndAsk(STRAY, confirmed=True, acknowledged=True))
    assert isinstance(got, Refused)
    assert got.refusal is Refusal.DOUBTED_IDENTITY
    assert herd.writes("pane.close") == []


# The hijack, through the Chat tab


def _frame(session: str) -> JsonObject:
    frame = snapshot_frame()
    agent = next(a for a in agents_of(frame) if a.get("pane_id") == WORKING_PANE)
    agent.update(agent="opencode", cwd=REPO, foreground_cwd=REPO, agent_status="idle")
    agent["agent_session"] = {
        "agent": "opencode",
        "kind": "id",
        "source": "herdr:opencode",
        "value": session,
    }
    return frame


DOUBT = "may not be this agent"


def test_the_chat_says_so_when_the_pane_reports_a_session_from_elsewhere(tmp_path: Path) -> None:
    the_hijack(tmp_path / "transcripts")
    rig_ = build_rig(tmp_path, {SNAPSHOT: _frame(REAL)})
    before = rig_.http.get(f"/agent/{REAL}/chat").text
    assert DOUBT not in before
    assert "turn 39 of" in before

    rig_.wire.replies[SNAPSHOT] = _frame(STRAY)
    rig_.clock.now += 3600  # past any snapshot cache
    after = rig_.http.get(f"/agent/{STRAY}/chat").text

    assert DOUBT in after
    assert THROWAWAY in after
    assert REPO in after


def test_a_prompt_from_the_phone_to_a_doubted_pane_is_refused_in_words(tmp_path: Path) -> None:
    the_hijack(tmp_path / "transcripts")
    rig_ = build_rig(tmp_path, {SNAPSHOT: _frame(STRAY), READ: read_frame(SETTLED)})
    page = rig_.http.get(f"/agent/{STRAY}").text
    assert DOUBT in page
    response = rig_.http.post(
        f"/agent/{STRAY}/prompt",
        data={"text": "hello"},
        headers={"Origin": "http://testserver"},
    )
    assert response.status_code == 409
    assert "not sure which agent" in response.text
