"""The web layer's pure parts: the prose tree, card naming, the failure table, doctor."""

import dataclasses
import time
from pathlib import Path
from typing import Final

import pytest
from pydantic import JsonValue

from herdr_pwa.cli import doctor
from herdr_pwa.decisions import AgentRow, decide_screen
from herdr_pwa.engine import (
    AgentStatus,
    BlockedPrompt,
    EngineSettings,
    EngineUnavailable,
    HerdrClient,
    PaneId,
    SessionId,
    as_pane_id,
)
from herdr_pwa.protocol import EXPECTED_PROTOCOL
from herdr_pwa.push import DeliveryLog
from herdr_pwa.settings import Settings
from herdr_pwa.web.assets import STATIC_DIR, Assets
from herdr_pwa.web.chat import is_long
from herdr_pwa.web.failures import FAILURES, failure_for
from herdr_pwa.web.markdown import TAGS, MdNode, render_tree
from herdr_pwa.web.naming import card_question, card_title, display_title, page_title
from herdr_pwa.web.routers.compose import key_name, said
from herdr_pwa.writes.answer import KeySent
from herdr_pwa.writes.outcome import Evidence, Outcome
from tests.webfakes import FIXTURES, SNAPSHOT, FakeWire, agents_of, snapshot_frame


def _tags(nodes: tuple[MdNode, ...]) -> set[str]:
    found: set[str] = set()
    for node in nodes:
        if node.tag:
            found.add(node.tag)
        found |= _tags(node.children)
    return found


def _texts(nodes: tuple[MdNode, ...]) -> str:
    return "".join(n.text + _texts(n.children) for n in nodes)


def test_markdown_emits_only_the_closed_tag_set() -> None:
    text = (
        "# h\n\n<div onclick=x>raw</div>\n\n- a\n\n> q\n\n```\ncode\n```\n\n| a |\n|---|\n| 1 |\n"
    )
    nodes = render_tree(text)
    assert _tags(nodes) <= TAGS
    assert "<div onclick=x>" in _texts(nodes)


def test_markdown_drops_unsafe_links_and_never_fetches_images() -> None:
    nodes = render_tree("[a](javascript:alert(1)) [b](data:x) ![pic](https://e.x/p.png)")
    assert "a" not in _tags(nodes)
    assert "img" not in _tags(nodes)
    assert "pic" in _texts(nodes)


def test_markdown_keeps_http_links() -> None:
    (paragraph,) = render_tree("[site](https://e.x/a)")
    (link,) = paragraph.children
    assert (link.tag, link.href) == ("a", "https://e.x/a")


def _row(title: str, harness: str = "claude", workspace: str = "proj") -> AgentRow:
    return AgentRow(
        session=SessionId("s"),
        target=PaneId("w1:p1"),
        title=title,
        harness=harness,
        workspace=workspace,
        status=AgentStatus.IDLE,
        effective_status=AgentStatus.IDLE,
        feed_key=(3, 0, 0, "w1:p1"),
        answer=None,
    )


@pytest.mark.parametrize(
    ("title", "shown"),
    [
        ("✳ Fix the parser", "Fix the parser"),
        ("⠂⠐", ""),
        ("claude", ""),
        ("Proj", ""),
        ("owner@box:~/src", ""),
        ("New session - 2026-10-10", ""),
        ("hermes-probe", "hermes-probe"),
    ],
)
def test_card_title_shows_only_a_title_that_says_something(title: str, shown: str) -> None:
    assert card_title(_row(title)) == shown


def _asking(screen: str) -> AgentRow:
    answer = decide_screen(BlockedPrompt.parse(screen))
    return dataclasses.replace(_row("t"), answer=answer)


def test_card_question_prints_a_question_the_screen_gave() -> None:
    screen = (FIXTURES / "capture-blocked-permission-prompt" / "detection.txt").read_text()
    assert card_question(_asking(screen)) == "Do you want to proceed?"


def test_card_question_never_prints_a_bare_prompt_glyph() -> None:
    # No captured blocked screen reads as this; an unreadable one still must not print it.
    rule = "\u2500" * 40
    assert card_question(_asking(f"{rule}\n\u276f \n{rule}\n")) == ""
    assert card_question(_row("t")) == ""


# The four sources each capture was read from; explain.txt is herdr's reasoning, not a screen.
_READS: Final = ("detection.txt", "visible.txt", "recent.txt", "recent-unwrapped.txt")


def _reads(pattern: str) -> list[Path]:
    return sorted(p for p in FIXTURES.glob(pattern) if p.name in _READS)


def _named(path: Path) -> str:
    return f"{path.parent.name}/{path.name}"


# Every real blocked screen in the corpus, in every source it was read from.
_ASKING_CAPTURES: Final = (
    *_reads("capture-blocked-*/*.txt"),
    *sorted(FIXTURES.glob("ansi/claude-blocked-*.ansi")),
    *sorted(FIXTURES.glob("hermes/blocked-*.txt")),
)
# Screens that ask nothing: the parser's fallback on them is scrollback or the status line.
_SILENT_SCREENS: Final = (
    FIXTURES / "rig" / "blocked-build-scrollback.ansi",
    *_reads("capture-idle-*/*.txt"),
    *_reads("capture-working-*/*.txt"),
    FIXTURES / "ansi" / "claude-idle-welcome.ansi",
)


@pytest.mark.parametrize("path", _ASKING_CAPTURES, ids=_named)
def test_card_question_keeps_every_real_question(path: Path) -> None:
    question = card_question(_asking(path.read_text()))
    assert question
    assert not any(ord(c) < 0x20 and c != "\n" for c in question), question


def test_the_corpus_holds_every_blocked_capture() -> None:
    blocked = list(FIXTURES.glob("capture-blocked-*/"))
    assert len(_ASKING_CAPTURES) == len(_READS) * len(blocked) + 2 + 2


@pytest.mark.parametrize("path", _SILENT_SCREENS, ids=_named)
def test_card_question_never_promotes_scrollback(path: Path) -> None:
    assert card_question(_asking(path.read_text())) == ""


def test_card_question_drops_escapes_from_a_question() -> None:
    screen = "\x1b]0;title\x07\x1b[1mDo you want to \x1b[32mdeploy\x1b[0m?\x07\n"
    assert card_question(_asking(screen)) == "Do you want to deploy?"


@pytest.mark.parametrize("asked", ["Enter passphrase for key:", "Allow edits to app.py", "Proceed"])
def test_card_question_keeps_a_prompt_with_no_question_mark(asked: str) -> None:
    assert card_question(_asking(f"build ok\n\n{asked}\n")) == asked


def test_card_title_drops_control_bytes() -> None:
    assert card_title(_row("\x1b[1mFix\x1b[0m the parser\x07")) == "Fix the parser"


def test_display_title_strips_only_leading_glyphs() -> None:
    assert display_title("✳ ✳ a ✳") == "a ✳"


@pytest.mark.parametrize(
    ("title", "shown"),
    [
        ("◐ Estimate Claude model costs", "Estimate Claude model costs"),
        ("⠋ Fix the parser", "Fix the parser"),
        ("✳ Fix the parser", "Fix the parser"),
        ("· Review tickets", "Review tickets"),
        ("● done · 3 left", "done · 3 left"),
        ("#12 fix the parser", "#12 fix the parser"),
        ("", "claude"),
    ],
)
def test_page_title_drops_the_status_glyph_the_dot_already_says(title: str, shown: str) -> None:
    assert page_title(title, "claude", "w1:p1") == shown


def test_failure_rows_never_shadow_a_later_subclass() -> None:
    kinds = [kind for kind, _ in FAILURES]
    for i, kind in enumerate(kinds):
        for earlier in kinds[:i]:
            assert not issubclass(kind, earlier), (kind, earlier)


def test_an_unlisted_exception_is_not_swallowed_by_the_table() -> None:
    with pytest.raises(KeyError):
        failure_for(KeyError("bug"))


def test_assets_hash_every_static_file() -> None:
    assets = Assets.load()
    names = {p.name for p in STATIC_DIR.iterdir() if p.is_file()}
    assert set(assets.files) == names
    assert {"app.css", "app.js", "htmx.min.js"} <= names
    assert assets.url("app.js").endswith(assets.files["app.js"].digest)


def test_no_template_or_script_writes_inline_style_or_markup() -> None:
    templates = Path(STATIC_DIR).parent / "templates"
    for path in templates.iterdir():
        text = path.read_text()
        assert "style=" not in text, path
        assert "|safe" not in text, path
        assert "<script>" not in text, path
    assert "innerHTML" not in (STATIC_DIR / "app.js").read_text()


def _doctor(tmp_path: Path, wire: FakeWire) -> tuple[int, str]:
    settings = Settings(socket_path=tmp_path / "x.sock", state_dir=tmp_path)
    out: list[str] = []

    def factory(engine: EngineSettings) -> HerdrClient:
        return HerdrClient(engine, transport=wire)

    code = doctor(settings, client_factory=factory, write=out.append)
    return code, "".join(out)


def _frame(protocol: int) -> dict[str, JsonValue]:
    frame = snapshot_frame()
    snapshot = frame["result"]
    assert isinstance(snapshot, dict)
    inner = snapshot["snapshot"]
    assert isinstance(inner, dict)
    inner["protocol"] = protocol
    assert agents_of(frame)
    return frame


def test_doctor_passes_when_both_sides_speak_the_pin(tmp_path: Path) -> None:
    code, out = _doctor(tmp_path, FakeWire({SNAPSHOT: _frame(EXPECTED_PROTOCOL)}))
    assert code == 0
    assert out.rstrip().endswith("doctor: OK")


def test_doctor_says_when_push_last_sent_failed_and_held_back(tmp_path: Path) -> None:
    frame = FakeWire({SNAPSHOT: _frame(EXPECTED_PROTOCOL)})
    assert "push: nothing delivered or attempted yet" in _doctor(tmp_path, frame)[1]
    now = time.time()
    health = DeliveryLog(tmp_path, clock=lambda: now - 3 * 3600)
    health.failed("BadJwtToken")
    health.clock = lambda: now - 200
    health.sent()
    code, out = _doctor(tmp_path, frame)
    assert code == 0
    assert "push: last sent 3m ago · last error 3h ago (BadJwtToken) · last quieted never" in out


def test_doctor_fails_on_runner_drift(tmp_path: Path) -> None:
    drifted = EXPECTED_PROTOCOL + 1
    code, out = _doctor(tmp_path, FakeWire({SNAPSHOT: _frame(drifted)}))
    assert code == 1
    assert f"protocol {drifted}  FAIL" in out
    assert out.rstrip().endswith("doctor: FAIL")


def test_doctor_fails_on_an_unreachable_runner(tmp_path: Path) -> None:
    code, out = _doctor(tmp_path, FakeWire({SNAPSHOT: EngineUnavailable("down")}))
    assert code == 1
    assert "unreachable" in out


def test_a_turn_is_long_past_eight_lines_or_about_eight_lines_of_characters() -> None:
    assert not is_long("short")
    assert is_long("x" * 321)
    assert is_long("line\n" * 8)
    assert not is_long("line\n" * 7)


def test_a_sent_key_is_said_in_the_screens_own_words() -> None:

    assert [key_name(k) for k in ("1", "enter", "ctrl-c")] == ["option 1", "Enter", "Ctrl+C"]
    pane = as_pane_id("w1:p3")
    gone = said(KeySent(pane, "2", Outcome.CONFIRMED, Evidence.GONE, True, None))
    assert (gone.kind, gone.what, gone.unsure) == ("sent", "Answered.", False)
    flicker = said(KeySent(pane, "enter", Outcome.ACCEPTED, Evidence.FLICKER, True, None))
    assert (flicker.kind, flicker.what, flicker.unsure) == (
        "unsure",
        "Sent Enter, unconfirmed.",
        True,
    )
