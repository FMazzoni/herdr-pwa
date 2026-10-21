"""Each request reads each source once: the agent's pane, and the log its conversation is in."""

from pathlib import Path

import pytest
from pydantic import JsonValue

from herdr_pwa.engine import Source
from tests.engine_seam import ClaudeAdapter
from tests.webfakes import (
    IDLE_SESSION,
    READ,
    SNAPSHOT,
    WORKING_SESSION,
    agents_of,
    assistant_says,
    build_rig,
    human_says,
    read_frame,
    snapshot_frame,
    write_transcript,
)

IDLE_PANE = "w652ae557d5b9c1:p2"
DRAWN_DIALOG = (
    Path(__file__).parent / "fixtures" / "ansi" / "claude-blocked-bash-permission.detection.ansi"
).read_text()


def _pane_reads(
    tmp_path: Path, path: str, frame: dict[str, JsonValue] | None = None, detection: str = "> "
) -> list[tuple[str, str]]:
    """The (source, format) of every pane read one GET of *path* made."""
    seen: list[tuple[str, str]] = []

    def answer(params: dict[str, JsonValue]) -> dict[str, JsonValue]:
        source = str(params.get("source"))
        seen.append((source, str(params.get("format"))))
        return read_frame(detection if source == "detection" else "> ", str(params.get("target")))

    rig = build_rig(tmp_path, {SNAPSHOT: frame or snapshot_frame(), READ: answer})
    assert rig.http.get(path.format(session=IDLE_SESSION)).status_code == 200
    return seen


@pytest.mark.parametrize("tab", ["chat", "pane"])
def test_an_idle_agents_page_reads_its_screen_once(tmp_path: Path, tab: str) -> None:
    reads = _pane_reads(tmp_path, f"/agent/{{session}}?tab={tab}")
    # The mirror, the strip and the composer's claim share the visible read; the detection
    # buffer is another read, of what a question would leave there.
    assert sorted(reads) == [("detection", "text"), ("visible", "ansi")], reads


# Prose, the mirror draws the detection buffer; a drawn dialog, it draws the visible screen.
@pytest.mark.parametrize("detection", ["> ", DRAWN_DIALOG], ids=["prose", "dialog"])
def test_a_blocked_agents_page_reads_each_buffer_once(tmp_path: Path, detection: str) -> None:
    frame = snapshot_frame()
    for agent in agents_of(frame):
        if agent["pane_id"] == IDLE_PANE:
            agent["agent_status"] = "blocked"
    reads = _pane_reads(tmp_path, "/agent/{session}?tab=pane", frame, detection)
    assert reads.count(("detection", "text")) == 1, reads
    assert reads.count(("visible", "ansi")) == 1, reads
    assert len(reads) == 3, reads


def test_the_composer_route_reads_the_screen_once_on_its_own(tmp_path: Path) -> None:
    reads = _pane_reads(tmp_path, "/agent/{session}/compose")
    assert reads == [("visible", "ansi")], reads


@pytest.fixture
def lookups(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every time a request looked for a Claude log, by session."""
    found: list[str] = []
    real = ClaudeAdapter.log

    def counted(source: Source) -> Path | None:
        found.append(source.session)
        return real(source)

    monkeypatch.setattr(ClaudeAdapter, "log", counted)
    return found


@pytest.mark.parametrize("tab", ["chat", "pane"])
def test_the_agent_page_finds_the_log_once(tmp_path: Path, tab: str, lookups: list[str]) -> None:
    rig = build_rig(tmp_path, {SNAPSHOT: snapshot_frame(), READ: read_frame("$ ")})
    write_transcript(tmp_path / "transcripts", WORKING_SESSION, [human_says("go")])
    assert rig.http.get(f"/agent/{WORKING_SESSION}?tab={tab}").status_code == 200
    assert lookups == [WORKING_SESSION], len(lookups)


@pytest.mark.parametrize("route", ["chat", "tray"])
def test_a_route_from_the_log_finds_it_once(tmp_path: Path, route: str, lookups: list[str]) -> None:
    rig = build_rig(tmp_path, {SNAPSHOT: snapshot_frame(), READ: read_frame("$ ")})
    write_transcript(
        tmp_path / "transcripts", WORKING_SESSION, [human_says("go"), assistant_says("done")]
    )
    page = rig.http.get(f"/agent/{WORKING_SESSION}/{route}")
    assert page.status_code == 200
    assert route == "tray" or "done" in page.text
    assert lookups == [WORKING_SESSION], len(lookups)
