"""What VoiceOver, Text Size and Reduce Motion get from the stylesheet and the markup."""

import colorsys
import re
from pathlib import Path

import pytest

from herdr_pwa.engine import EngineUnavailableError
from tests.catalogue import BLOCKED, STATES, State, build, herd
from tests.webfakes import (
    READ,
    SNAPSHOT,
    WORKING_SESSION,
    assistant_says,
    build_rig,
    client_scripts,
    human_says,
    write_transcript,
)

STATIC = Path(__file__).resolve().parents[1] / "src" / "herdr_pwa" / "web" / "static"
CSS = (STATIC / "app.css").read_text(encoding="utf-8")
JS = client_scripts()
ROOT_BLOCK = CSS[CSS.index(":root {") : CSS.index("}", CSS.index(":root {"))]
TOKENS: dict[str, str] = {
    m["name"]: m["value"]
    for m in re.finditer(r"(?P<name>--[\w-]+):\s*(?P<value>[^;]+);", ROOT_BLOCK)
}
FRAGMENT = {"HX-Request": "true"}

# The system body size at iOS's default Text Size, which the browser fallback also gives.
DEFAULT_ROOT_PX = 17.0
SMALLEST_TEXT_PX = 13.0
# WCAG 2.1 AA for body text.
AA = 4.5
TEXT_COLOURS = (
    "--ink",
    "--ink-2",
    "--ink-3",
    "--accent",
    "--blocked",
    "--working",
    "--running",
    "--idle",
    "--done",
    "--unknown",
)
GROUNDS = ("--ground", "--surface", "--surface-2")


def _luminance(hex_colour: str) -> float:
    channels = [int(hex_colour.lstrip("#")[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(front: str, back: str) -> float:
    light, dark = sorted((_luminance(front), _luminance(back)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


@pytest.mark.parametrize("ground", GROUNDS)
@pytest.mark.parametrize("ink", TEXT_COLOURS)
def test_every_text_colour_reads_at_aa_on_every_ground(ink: str, ground: str) -> None:
    ratio = _contrast(TOKENS[ink], TOKENS[ground])
    assert ratio >= AA, f"{ink} on {ground} is {ratio:.2f}:1"


def _hue(hex_colour: str) -> float:
    red, green, blue = (int(hex_colour.lstrip("#")[i : i + 2], 16) / 255 for i in (0, 2, 4))
    return colorsys.rgb_to_hsv(red, green, blue)[0] * 360


def test_the_running_blue_is_not_the_accent_blue() -> None:
    """A card still at work is told by its blue, which no link or edge in the accent shares."""
    assert abs(_hue(TOKENS["--running"]) - _hue(TOKENS["--accent"])) >= 15


# Ink drawn on a filled colour rather than on a ground: Stop now's words on its danger fill.
FILLED = (("--ground", "--blocked"),)


@pytest.mark.parametrize(("ink", "fill"), FILLED)
def test_ink_on_a_filled_button_reads_at_aa(ink: str, fill: str) -> None:
    ratio = _contrast(TOKENS[ink], TOKENS[fill])
    assert ratio >= AA, f"{ink} on {fill} is {ratio:.2f}:1"
    assert f"background: var({fill}); border-color: var({fill}); color: var({ink});" in CSS


def _type_tokens() -> dict[str, str]:
    return {
        k: v.strip() for k, v in TOKENS.items() if k.startswith("--text-") and k != "--text-root"
    }


def test_every_type_size_follows_the_root() -> None:
    for name, value in _type_tokens().items():
        assert "rem" in value, f"{name} is {value}: a fixed size ignores iPhone Text Size"


def test_nothing_is_set_under_13px_at_the_default_text_size() -> None:
    for name, value in _type_tokens().items():
        sizes = [
            float(m["rem"]) * DEFAULT_ROOT_PX for m in re.finditer(r"(?P<rem>[\d.]+)rem", value)
        ]
        assert min(sizes) >= SMALLEST_TEXT_PX, f"{name} is {value}, {min(sizes):.1f}px"


def test_the_root_takes_the_system_body_size_on_ios() -> None:
    supports = re.search(
        r"@supports \(font: -apple-system-body\)[^{]*\{\s*html \{(?P<rule>[^}]*)\}", CSS
    )
    assert supports is not None
    assert "font: -apple-system-body" in supports["rule"]
    # The family and leading are put back after the shorthand resets them.
    assert "font-family: var(--sans)" in supports["rule"]


def test_reduce_motion_stills_every_transition_and_animation() -> None:
    block = re.search(
        r"@media \(prefers-reduced-motion: reduce\) \{(?P<rules>.*?)\n\}", CSS, re.DOTALL
    )
    assert block is not None
    assert "transition: none !important" in block["rules"]
    assert "animation: none !important" in block["rules"]
    assert "scroll-behavior: auto" in block["rules"]
    assert "smooth" not in JS


def test_the_agent_page_has_landmarks_a_heading_and_a_named_way_back(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    page = rig.http.get(f"/agent/{WORKING_SESSION}").text
    assert '<header class="topbar">' in page
    assert "<main " in page
    assert '<nav class="tabs tabs--3" aria-label="View">' in page
    assert re.search(r"<h1 [^>]*>", page)
    assert 'aria-label="Back to herd"' in page
    assert 'id="hp-live" aria-live="polite"' in page


def test_the_feed_has_a_page_heading(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    assert '<h1 class="sr-only">Herd</h1>' in rig.http.get("/").text


def test_with_scripts_off_the_feed_links_the_servers_own_list(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    page = rig.http.get("/?ws=w2").text
    noscript = page.partition("<noscript>")[2].partition("</noscript>")[0]
    assert '<a href="/feed?ws=w2">' in noscript
    assert "reload" not in noscript
    assert rig.http.get("/feed?ws=w2").status_code == 200


H1 = re.compile(r"<h1[\s>]")


@pytest.mark.parametrize("state", STATES, ids=[s.name for s in STATES])
def test_every_page_has_exactly_one_page_heading(state: State, tmp_path: Path) -> None:
    page = build(state, tmp_path).http.get(state.path, follow_redirects=False)
    if not page.text.lower().startswith("<!doctype html"):
        pytest.skip("not a page: a refusal or a redirect")
    assert len(H1.findall(page.text)) == 1


def test_who_spoke_is_said_in_words(tmp_path: Path) -> None:
    write_transcript(
        tmp_path / "transcripts",
        WORKING_SESSION,
        [human_says("run it"), assistant_says("ran it")],
    )
    rig = build_rig(tmp_path)
    chat = rig.http.get(f"/agent/{WORKING_SESSION}/chat", headers=FRAGMENT).text
    assert '<span class="sr-only">You:</span>' in chat
    assert '<span class="sr-only">Agent:</span>' in chat


LIVE = re.compile(r'role="(?:status|alert)"|aria-live')


@pytest.mark.parametrize(
    "path", ["/feed", "/agent/{s}/chat", "/agent/{s}/answer", "/agent/{s}/pane"]
)
def test_a_polled_region_carries_no_live_role(tmp_path: Path, path: str) -> None:
    """A poll redraws these whole; a live role in them is read out again on every redraw."""
    # Blocked, and its screen unreadable: the answer region's loudest drawing.
    replies = {SNAPSHOT: herd(**BLOCKED), READ: EngineUnavailableError("the read failed")}
    write_transcript(tmp_path / "transcripts", WORKING_SESSION, [assistant_says("ran it")])
    rig = build_rig(tmp_path, replies)
    body = rig.http.get(path.format(s=WORKING_SESSION), headers=FRAGMENT).text
    # A hidden notice is never read; app.js shows it only after a tap got no reply.
    loud = [
        m
        for m in LIVE.finditer(body)
        if "data-hp-lost hidden" not in body[m.start() : m.start() + 80]
    ]
    assert not loud, body[loud[0].start() - 200 : loud[0].end() + 200]


def test_a_stale_feed_keeps_its_text_contrast() -> None:
    stale = [m["rule"] for m in re.finditer(r"\.feed\.is-stale[^{]*\{(?P<rule>[^}]*)\}", CSS)]
    assert stale, "the stale feed has no rule of its own"
    assert not any("opacity" in rule for rule in stale), "a dimmed card takes its text below AA"
