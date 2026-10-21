"""The web layer's pure parts: the prose tree, card naming, the failure table, doctor."""

import dataclasses
import os
from pathlib import Path
from typing import Final

import pytest
from jinja2 import TemplateNotFound
from pydantic import JsonValue

from herdr_pwa import cli, statedir
from herdr_pwa.cli import doctor
from herdr_pwa.clock import WallTime
from herdr_pwa.decisions import AgentRow, decide_screen
from herdr_pwa.engine import (
    AgentStatus,
    BlockedPrompt,
    EngineSettings,
    EngineUnavailableError,
    HerdrClient,
    PaneId,
    SessionId,
    as_pane_id,
)
from herdr_pwa.protocol import EXPECTED_PROTOCOL
from herdr_pwa.push import DeliveryLog
from herdr_pwa.render.markdown import TAGS, MdNode, render_tree
from herdr_pwa.settings import Settings
from herdr_pwa.web.assets import STATIC_DIR, Assets
from herdr_pwa.web.chat import is_long
from herdr_pwa.web.failures import FAILURES, failure_for
from herdr_pwa.web.naming import card_question, card_title, display_title, page_title
from herdr_pwa.web.render import Pages, read_templates
from herdr_pwa.web.routers.compose import key_name, said
from herdr_pwa.web.seams import Seams
from herdr_pwa.web.started import STARTED_FILENAME, StartedTree, fingerprint, record_started
from herdr_pwa.writes.answer import KeySent
from herdr_pwa.writes.lifecycle import declared_models, record_service_models, record_service_path
from herdr_pwa.writes.outcome import Evidence, Outcome
from tests.fakes import LOGIN, NODE, status_route, tailscaled
from tests.webfakes import (
    FIXTURES,
    SNAPSHOT,
    FakeWire,
    agents_of,
    client_scripts,
    snapshot_frame,
)


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
        ("owner@box:/srv/src", ""),
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
    assert len(_ASKING_CAPTURES) == len(_READS) * len(blocked) + 2 + 3


@pytest.mark.parametrize("path", _SILENT_SCREENS, ids=_named)
def test_card_question_never_promotes_scrollback(path: Path) -> None:
    assert card_question(_asking(path.read_text())) == ""


@pytest.mark.parametrize(
    "path",
    [
        *_reads("capture-idle-*/*.txt"),
        *_reads("capture-working-*/*.txt"),
    ],
    ids=_named,
)
def test_card_question_never_prints_a_status_line_s_git_mark(path: Path) -> None:
    # A git-aware status line marks untracked files with '?': nothing on the screen asks.
    dirty = path.read_text().replace("herdr-pwa [main]", "herdr-pwa on main [!?]")
    assert card_question(_asking(dirty)) == ""


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
    assets = Assets.load(STATIC_DIR)
    names = {p.name for p in STATIC_DIR.iterdir() if p.is_file()}
    assert set(assets.files) == names
    assert {"app.css", "app.js", "htmx.min.js"} <= names
    assert assets.url("app.js").endswith(assets.files["app.js"].digest)


def test_a_template_changed_on_disk_after_start_is_not_what_is_served(tmp_path: Path) -> None:
    # New markup under old view models is a 500 under StrictUndefined; a restart shows it instead.
    _ = (tmp_path / "t.html").write_text("old {{ x }}")
    pages = Pages(Assets.load(STATIC_DIR), read_templates(tmp_path))
    assert pages.render("t.html", {"x": 1}) == "old 1"
    _ = (tmp_path / "t.html").write_text("new {{ y }}")
    _ = (tmp_path / "u.html").write_text("added")
    assert pages.render("t.html", {"x": 1}) == "old 1"
    with pytest.raises(TemplateNotFound):
        _ = pages.render("u.html", {})


def test_static_files_are_what_the_process_started_with(tmp_path: Path) -> None:
    # A merge lands on disk under a running service; what it serves must not move with it.
    (tmp_path / "app.js").write_text("old")
    (tmp_path / "app.css").write_text("old")
    assets = Assets.load(tmp_path)
    started = (assets.build, assets.url("app.js"), assets.files["app.js"].body)
    (tmp_path / "app.js").write_text("new")
    (tmp_path / "app.css").unlink()
    assert (assets.build, assets.url("app.js"), assets.files["app.js"].body) == started
    assert assets.files["app.css"].body == b"old"


def test_no_template_or_script_writes_inline_style_or_markup() -> None:
    templates = Path(STATIC_DIR).parent / "templates"
    for path in templates.iterdir():
        text = path.read_text()
        assert "style=" not in text, path
        assert "|safe" not in text, path
        assert "<script>" not in text, path
    assert "innerHTML" not in client_scripts()


def _doctor(
    tmp_path: Path, wire: FakeWire, now: float = 0.0, *, opencode_models: tuple[str, ...] = ()
) -> tuple[int, str]:
    settings = Settings(
        socket_path=tmp_path / "x.sock",
        state_dir=tmp_path,
        opencode_models=opencode_models,
        tailscaled_socket=tmp_path / "no-tailscaled.sock",
        public_host=NODE,
        tailnet_login=LOGIN,
    )
    out: list[str] = []

    def factory(engine: EngineSettings) -> HerdrClient:
        return HerdrClient(engine, transport=wire)

    code = doctor(
        settings, Seams(wall=lambda: WallTime(now), client_factory=factory, write=out.append)
    )
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


def _doctor_on(settings: Settings) -> tuple[int, str]:
    out: list[str] = []
    wire = FakeWire({SNAPSHOT: _frame(EXPECTED_PROTOCOL)})

    def factory(engine: EngineSettings) -> HerdrClient:
        return HerdrClient(engine, transport=wire)

    code = doctor(
        settings, Seams(wall=lambda: WallTime(0.0), client_factory=factory, write=out.append)
    )
    return code, "".join(out)


def test_doctor_names_the_served_host_and_fails_without_one(tmp_path: Path) -> None:
    base = {"socket_path": tmp_path / "x.sock", "state_dir": tmp_path, "tailnet_login": LOGIN}
    code, out = _doctor_on(Settings.model_validate({**base, "public_host": f"{NODE.upper()}."}))
    assert code == 0
    assert f"served host: {NODE} (from HP_PUBLIC_HOST)" in out
    with tailscaled(tmp_path, canned=status_route()) as fake:
        settings = Settings.model_validate({**base, "tailscaled_socket": fake.socket_path})
        code, out = _doctor_on(settings)
    assert code == 0
    assert f"served host: {NODE} (from tailscaled's MagicDNS name)" in out
    missing = {**base, "tailscaled_socket": tmp_path / "none.sock"}
    code, out = _doctor_on(Settings.model_validate(missing))
    assert code == 1
    assert "served host: unknown, so every Tailscale identity is refused" in out
    assert out.rstrip().endswith("doctor: FAIL")


def test_doctor_says_who_may_drive_agents_and_fails_when_nobody_chose(tmp_path: Path) -> None:
    base = {"socket_path": tmp_path / "x.sock", "state_dir": tmp_path, "public_host": NODE}
    code, out = _doctor_on(Settings.model_validate({**base, "tailnet_login": LOGIN}))
    assert code == 0
    assert f"who may drive agents: only {LOGIN} (HP_TAILNET_LOGIN)\n" in out
    code, out = _doctor_on(Settings.model_validate({**base, "allow_any_tailnet_user": True}))
    assert code == 0
    assert "any user your tailnet lets reach this node (HP_ALLOW_ANY_TAILNET_USER)  WARN" in out
    code, out = _doctor_on(Settings.model_validate(base))
    assert code == 1
    assert "nobody: HP_TAILNET_LOGIN is not set, so serve refuses to start" in out
    code, out = _doctor_on(Settings.model_validate({**base, "assume_login": LOGIN}))
    assert code == 0
    assert f"scripts on this machine as {LOGIN} (HP_ASSUME_LOGIN)\n" in out


def test_doctor_fails_on_a_raw_tcp_forward_and_says_when_it_cannot_tell(tmp_path: Path) -> None:
    base = {"socket_path": tmp_path / "x.sock", "state_dir": tmp_path, "tailnet_login": LOGIN}
    forward = {"TCP": {"8443": {"TCPForward": "127.0.0.1:8772"}}}
    with tailscaled(tmp_path, canned=status_route(), serve_config=forward) as fake:
        code, out = _doctor_on(
            Settings.model_validate({**base, "tailscaled_socket": fake.socket_path})
        )
    assert code == 1
    assert "raw TCP forward: 'tailscale serve' forwards raw TCP from port 8443" in out
    (tmp_path / "plain").mkdir()
    with tailscaled(tmp_path / "plain", canned=status_route()) as fake:
        code, out = _doctor_on(
            Settings.model_validate({**base, "tailscaled_socket": fake.socket_path})
        )
    assert code == 0
    assert "raw TCP forward: none to port 8772  ok" in out
    unreadable = {**base, "public_host": NODE, "tailscaled_socket": tmp_path / "none.sock"}
    code, out = _doctor_on(Settings.model_validate(unreadable))
    assert code == 0
    assert "raw TCP forward: unknown, the serve config is unreadable (No tailscaled socket" in out
    assert "  WARN\n" in out.split("raw TCP forward:")[1]


def test_doctor_says_when_push_last_sent_failed_and_held_back(tmp_path: Path) -> None:
    frame = FakeWire({SNAPSHOT: _frame(EXPECTED_PROTOCOL)})
    assert "push: nothing delivered or attempted yet" in _doctor(tmp_path, frame)[1]
    now = 1_800_000_000.0
    DeliveryLog(tmp_path, clock=lambda: WallTime(now - 3 * 3600)).failed("BadJwtToken")
    DeliveryLog(tmp_path, clock=lambda: WallTime(now - 200)).sent()
    code, out = _doctor(tmp_path, frame, now)
    assert code == 0
    assert "push: last sent 3m ago · last error 3h ago (BadJwtToken) · last quieted never" in out


def test_doctor_judges_each_harness_on_the_services_path_not_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home_bin = tmp_path / "home" / ".local" / "bin"
    service_bin = tmp_path / "service-bin"
    for where, name in ((home_bin, "hermes"), (service_bin, "claude")):
        where.mkdir(parents=True, exist_ok=True)
        _ = (where / name).write_text("#!/bin/sh\n")
        (where / name).chmod(0o700)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", str(service_bin))
    record_service_path(tmp_path)
    # doctor runs from a shell that CAN see hermes; the verdict is still the service's.
    monkeypatch.setenv("PATH", f"{service_bin}:{home_bin}")
    code, out = _doctor(tmp_path, FakeWire({SNAPSHOT: _frame(EXPECTED_PROTOCOL)}))
    assert code == 0
    assert "harnesses, on the service's PATH:" in out
    assert f"  claude: offered, at {service_bin / 'claude'}" in out
    assert f"  hermes: installed at {home_bin / 'hermes'}, not on the service's PATH  WARN" in out
    assert "  codex: no harness here" in out
    assert out.rstrip().endswith("doctor: OK")


def test_doctor_says_when_the_service_never_recorded_its_path(tmp_path: Path) -> None:
    _, out = _doctor(tmp_path, FakeWire({SNAPSHOT: _frame(EXPECTED_PROTOCOL)}))
    assert "the service never recorded its PATH, so judged on this shell's" in out


def test_doctor_passes_a_service_whose_tree_has_not_moved(tmp_path: Path) -> None:
    record_started(tmp_path, at=1_800_000_000.0)
    code, out = _doctor(tmp_path, FakeWire({SNAPSHOT: _frame(EXPECTED_PROTOCOL)}))
    assert code == 0
    assert ": unchanged  ok" in out
    assert out.rstrip().endswith("doctor: OK")


def _started_from(state_dir: Path, tree: Path) -> None:
    started = StartedTree(directory=str(tree), fingerprint=fingerprint(tree), at=1_800_000_000.0)
    statedir.write_secret(state_dir / STARTED_FILENAME, started.model_dump_json())


def test_doctor_fails_when_the_tree_moved_since_the_service_started(tmp_path: Path) -> None:
    tree = tmp_path / "src"
    tree.mkdir()
    _ = (tree / "page.html").write_text("{{ x }}")
    _started_from(tmp_path, tree)
    _ = (tree / "page.html").write_text("{{ y }}")
    code, out = _doctor(tmp_path, FakeWire({SNAPSHOT: _frame(EXPECTED_PROTOCOL)}))
    assert code == 1
    assert f"service tree at {tree}" in out
    assert f"now {fingerprint(tree)}, so it serves old code; restart it  FAIL" in out
    assert out.rstrip().endswith("doctor: FAIL")


def test_doctor_fails_when_the_tree_the_service_started_from_is_gone(tmp_path: Path) -> None:
    tree = tmp_path / "src"
    tree.mkdir()
    _started_from(tmp_path, tree)
    tree.rmdir()
    code, out = _doctor(tmp_path, FakeWire({SNAPSHOT: _frame(EXPECTED_PROTOCOL)}))
    assert code == 1
    assert "unreadable now (no such directory)  FAIL" in out


def test_doctor_says_when_the_service_never_recorded_its_tree(tmp_path: Path) -> None:
    code, out = _doctor(tmp_path, FakeWire({SNAPSHOT: _frame(EXPECTED_PROTOCOL)}))
    assert code == 0
    assert "service tree: never recorded, so a stale service is not ruled out" in out


def test_the_tree_fingerprint_follows_content_not_mtimes_or_bytecode(tmp_path: Path) -> None:
    # just check rewrites mtimes, and importing writes __pycache__: neither is a code change.
    _ = (tmp_path / "a.py").write_text("x = 1\n")
    before = fingerprint(tmp_path)
    os.utime(tmp_path / "a.py", (0, 0))
    (tmp_path / "__pycache__").mkdir()
    _ = (tmp_path / "__pycache__" / "a.cpython-313.pyc").write_bytes(b"\0")
    assert fingerprint(tmp_path) == before
    _ = (tmp_path / "a.py").write_text("x = 2\n")
    assert fingerprint(tmp_path) != before
    _ = (tmp_path / "a.py").write_text("x = 1\n")
    _ = (tmp_path / "b.py").write_text("")
    assert fingerprint(tmp_path) != before


def test_doctor_fails_on_runner_drift(tmp_path: Path) -> None:
    drifted = EXPECTED_PROTOCOL + 1
    code, out = _doctor(tmp_path, FakeWire({SNAPSHOT: _frame(drifted)}))
    assert code == 1
    assert f"protocol {drifted}  FAIL" in out
    assert out.rstrip().endswith("doctor: FAIL")


def test_doctor_fails_on_an_unreachable_runner(tmp_path: Path) -> None:
    code, out = _doctor(tmp_path, FakeWire({SNAPSHOT: EngineUnavailableError("down")}))
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
    gone = said(KeySent(pane, "2", Outcome.CONFIRMED, Evidence.GONE, ended=True, prompt=None))
    assert (gone.kind, gone.what, gone.unsure) == ("sent", "Answered.", False)
    flicker = said(
        KeySent(pane, "enter", Outcome.ACCEPTED, Evidence.FLICKER, ended=True, prompt=None)
    )
    assert (flicker.kind, flicker.what, flicker.unsure) == (
        "unsure",
        "Sent Enter, unconfirmed.",
        True,
    )


PICKLE = "opencode/big-pickle"
SONNET = "anthropic/claude-sonnet-4-5"


def test_doctor_says_when_no_opencode_models_are_declared(tmp_path: Path) -> None:
    code, out = _doctor(tmp_path, FakeWire({SNAPSHOT: _frame(EXPECTED_PROTOCOL)}))
    assert code == 0
    assert "models (the service never recorded its own, so this shell's settings):" in out
    assert "  opencode: none declared (HP_OPENCODE_MODELS), so the form offers no model" in out


def test_doctor_lists_the_declared_opencode_models(tmp_path: Path) -> None:
    wire = FakeWire({SNAPSHOT: _frame(EXPECTED_PROTOCOL)})
    _, out = _doctor(tmp_path, wire, opencode_models=(PICKLE, SONNET))
    assert f"  opencode: {PICKLE}, {SONNET}" in out


def test_doctor_lists_the_models_the_service_declared_not_its_own_shell(tmp_path: Path) -> None:
    record_service_models(tmp_path, declared_models(Settings(opencode_models=(SONNET, PICKLE))))
    # doctor's shell never sees the unit's environment file, so it declares nothing itself.
    code, out = _doctor(tmp_path, FakeWire({SNAPSHOT: _frame(EXPECTED_PROTOCOL)}))
    assert code == 0
    assert "models, as the service declared them:" in out
    assert f"  opencode: {SONNET}, {PICKLE}" in out
    assert out.rstrip().endswith("doctor: OK")


def test_doctor_says_the_service_declared_none(tmp_path: Path) -> None:
    record_service_models(tmp_path, declared_models(Settings()))
    wire = FakeWire({SNAPSHOT: _frame(EXPECTED_PROTOCOL)})
    _, out = _doctor(tmp_path, wire, opencode_models=(PICKLE,))
    assert "models, as the service declared them:" in out
    assert "  opencode: none declared (HP_OPENCODE_MODELS), so the form offers no model" in out
    assert PICKLE not in out


def _never_serves(*_args: object, **_kwargs: object) -> None:
    return None


def test_doctor_from_a_bare_shell_says_what_serve_was_declared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HP_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("HP_TAILSCALED_SOCKET", str(tmp_path / "none.sock"))
    monkeypatch.setenv("HP_TAILNET_LOGIN", LOGIN)
    monkeypatch.setattr(cli.uvicorn, "run", _never_serves)
    # The unit's environment file declares them; the shell doctor is run from does not.
    monkeypatch.setenv("HP_OPENCODE_MODELS", f"{PICKLE},{SONNET}")
    assert cli.main(["serve", "--port", "0"]) == 0
    _, out = _doctor(tmp_path, FakeWire({SNAPSHOT: _frame(EXPECTED_PROTOCOL)}))
    assert "models, as the service declared them:" in out
    assert f"  opencode: {PICKLE}, {SONNET}" in out
