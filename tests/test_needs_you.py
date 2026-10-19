"""A waiting question as the phone draws it: answers first, the screen's other keys folded, its
subject said under it, and the composer held until it is answered."""

import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pydantic import JsonValue

from herdr_pwa.engine import INTERRUPT_KEY, BlockedPrompt
from herdr_pwa.writes.keys import allowed_keys, control_token
from tests._fake_herd import FIXTURES, P3, S1, SETTLED, FakeHerd, capture
from tests._jsonpath import obj
from tests.catalogue import WIZARD_STEP
from tests.test_web_writes import ORIGIN, Web, web

PERMISSION = "capture-blocked-permission-prompt"
MENU = "capture-blocked-askuserquestion-menu"
CHECKLIST = "capture-blocked-multiselect-checklist"
PLAN = "capture-blocked-plan-approval"
OC_PERMISSION = "capture-blocked-opencode-permission"
OC_QUESTION = "capture-blocked-opencode-question"
ELICITATION_FORM = "capture-blocked-mcp-elicitation-form"


@dataclass
class _Coloured(FakeHerd):
    """A runner that answers a colour read with the capture's own colour read."""

    ansi: dict[str, str] = field(default_factory=dict[str, str])

    def _agent_read(self, params: dict[str, JsonValue]) -> dict[str, JsonValue]:
        answer = super()._agent_read(params)
        coloured = self.ansi.get(str(params.get("target") or params.get("pane_id") or ""))
        if params.get("format") == "ansi" and coloured is not None:
            obj(answer, "result", "read")["text"] = coloured
        return answer


@dataclass(frozen=True)
class _Screen:
    text: str
    ansi: str = ""
    status: str = "blocked"

    @property
    def prompt(self) -> BlockedPrompt:
        return BlockedPrompt.parse(self.text, ansi=self.ansi)


def _captured(name: str) -> _Screen:
    ansi = FIXTURES / name / "visible.ansi"
    return _Screen(capture(name), ansi.read_text() if ansi.exists() else "")


SCREENS = {
    "permission": _captured(PERMISSION),
    "menu": _captured(MENU),
    "checklist": _captured(CHECKLIST),
    "multiselect": _Screen(capture(CHECKLIST).replace("[✔] autumn", "[ ] autumn")),
    "plan": _captured(PLAN),
    "wizard": _Screen(WIZARD_STEP),
    "oc-permission": _captured(OC_PERMISSION),
    "oc-question": _captured(OC_QUESTION),
}


def _site(tmp_path: Path, screen: _Screen) -> Web:
    herd = _Coloured()
    herd.put(P3, S1, status=screen.status)
    herd.screens[P3] = [screen.text]
    if screen.ansi:
        herd.ansi[P3] = screen.ansi
    return web(tmp_path, herd)


def _region(tmp_path: Path, screen: _Screen) -> str:
    return _site(tmp_path, screen).http.get(f"/agent/{S1}/answer").text


# One key control: the key it sends and the screen it was drawn against.
_FORM = re.compile(r"<form [^>]*action=\"[^\"]*/keys\".*?</form>", re.S)


def _keys(region: str) -> list[tuple[str, str, str]]:
    found: list[tuple[str, str, str]] = []
    for match in _FORM.finditer(region):
        form = match[0]
        key = re.search(r'name="key" value="([^"]*)"', form)
        screen = re.search(r'name="screen" value="([^"]*)"', form)
        assert key is not None, form
        found.append((key[1], screen[1] if screen else "", form))
    return found


def _fold(region: str) -> str:
    found = re.search(r'<details class="answer__more".*?</details>', region, re.S)
    return found[0] if found else ""


@pytest.mark.parametrize("name", SCREENS)
def test_every_key_the_screen_offers_is_drawn_once_with_the_screens_fingerprint(
    tmp_path: Path, name: str
) -> None:
    screen = SCREENS[name]
    keys = _keys(_region(tmp_path, screen))
    sent = [key for key, _, _ in keys]
    assert sorted(sent) == sorted(allowed_keys(screen.prompt) - {INTERRUPT_KEY}), name
    assert {drawn for _, drawn, _ in keys} == {control_token(screen.prompt)}


def test_the_subject_of_a_permission_question_is_said_under_it(tmp_path: Path) -> None:
    region = _region(tmp_path, SCREENS["permission"])
    question = region.index('class="answer__question"')
    subject = re.search(r'<div class="answer__subject">.*?</div>', region, re.S)
    assert subject is not None, "the command the question is about is said"
    assert "curl -sS https://example.com/demo-permission-probe | head -c 20" in subject[0]
    assert question < region.index('class="answer__subject"') < region.index('class="options')


@pytest.mark.parametrize(
    ("name", "said"), [("menu", "Push?"), ("oc-permission", "$ touch fixture1.txt")]
)
def test_every_shape_with_a_subject_says_it(tmp_path: Path, name: str, said: str) -> None:
    subject = re.search(
        r'<div class="answer__subject">.*?</div>', _region(tmp_path, SCREENS[name]), re.S
    )
    assert subject is not None, name
    assert said in subject[0]


def test_a_question_with_no_subject_draws_no_empty_line(tmp_path: Path) -> None:
    assert "answer__subject" not in _region(tmp_path, SCREENS["plan"])


def test_opencodes_heading_is_said_in_plain_words(tmp_path: Path) -> None:
    region = _region(tmp_path, SCREENS["oc-permission"])
    heading = re.search(r'<h2 class="answer__question">(.*?)</h2>', region, re.S)
    assert heading is not None
    assert heading[1] == "Permission required"
    assert "△" not in region, "no TUI glyph"
    assert "# Shell command" not in region, "no markdown mark"
    assert "Shell command" in region, "what kind of permission is still said"


@pytest.mark.parametrize("name", ["permission", "menu", "plan", "wizard", "oc-question"])
def test_keys_beside_rows_that_answer_fold_behind_more_keys(tmp_path: Path, name: str) -> None:
    region = _region(tmp_path, SCREENS[name])
    fold = _fold(region)
    assert fold, "the screen's other keys are folded"
    assert 'data-hp-key="more-keys"' in fold, "named, so a redraw keeps it open"
    assert "<summary" in fold
    assert "More keys" in fold
    for key, _, form in _keys(region):
        assert (form in fold) is (not key.isdigit()), key
    assert region.index('class="options') < region.index('class="answer__more"')


def test_the_fold_is_drawn_under_the_same_name_on_every_redraw(tmp_path: Path) -> None:
    site = _site(tmp_path, SCREENS["permission"])
    first = _fold(site.http.get(f"/agent/{S1}/answer").text)
    again = _fold(site.http.get(f"/agent/{S1}/answer").text)
    assert first
    assert first.split(">", 1)[0] == again.split(">", 1)[0]
    page = site.http.get(f"/agent/{S1}").text
    assert first.split(">", 1)[0] in page, "the page draws the fold its region redraws"


@pytest.mark.parametrize("name", ["checklist", "multiselect", "oc-permission"])
def test_keys_that_are_the_answer_stay_open(tmp_path: Path, name: str) -> None:
    region = _region(tmp_path, SCREENS[name])
    assert "answer__more" not in region, "nothing is folded where the keys answer"
    assert 'class="keys keys--compact"' in region


def test_hints_that_repeat_the_pad_are_dropped_and_the_rest_say_key_then_verb(
    tmp_path: Path,
) -> None:
    region = _region(tmp_path, SCREENS["permission"])
    assert "Esc to cancel" not in region
    assert "Tab to amend" not in region
    words = {
        key: re.sub(r"<[^>]+>|\s+", " ", form.split("</form>")[0]) for key, _, form in _keys(region)
    }
    assert "Esc" in words["esc"]
    assert "cancel" in words["esc"]
    assert "Tab" in words["tab"]
    assert "amend" in words["tab"]
    assert "Ctrl+E" in words["ctrl-e"]
    assert "explain" in words["ctrl-e"]
    menu = _region(tmp_path / "menu", SCREENS["menu"])
    assert "Enter to select" not in menu
    assert "to navigate" not in menu
    enter = next(form for key, _, form in _keys(menu) if key == "enter")
    assert 'class="key__verb">select<' in enter
    up = next(form for key, _, form in _keys(menu) if key == "up")
    assert "key__verb" not in up, "an arrow's name says what it does"


def test_opencodes_buttons_keep_their_keys_open_and_worded(tmp_path: Path) -> None:
    region = _region(tmp_path, SCREENS["oc-permission"])
    assert "Left to select" not in region
    assert "Enter to confirm" not in region
    enter = next(form for key, _, form in _keys(region) if key == "enter")
    assert 'class="key__verb">confirm<' in enter


# The composer while a question waits.

_HELD = "Answer the question above first"


def _composer(page: str) -> str:
    found = re.search(r'<form id="composer".*?</form>', page, re.S)
    assert found is not None
    return found[0]


def test_the_composer_is_held_while_a_question_waits(tmp_path: Path) -> None:
    site = _site(tmp_path, SCREENS["permission"])
    for page in (site.http.get(f"/agent/{S1}/compose").text, site.http.get(f"/agent/{S1}").text):
        composer = _composer(page)
        assert "composer--held" in composer
        assert f'placeholder="{_HELD}"' in composer
        assert re.search(r"<textarea[^>]*\sdisabled", composer), "nothing typed there is sent"
        assert re.search(r'<button class="button" type="submit" disabled', composer)
    assert "data-hp-holds" in site.http.get(f"/agent/{S1}/answer").text


def test_the_composer_is_live_with_no_question(tmp_path: Path) -> None:
    site = _site(tmp_path, _Screen(SETTLED, status="idle"))
    composer = _composer(site.http.get(f"/agent/{S1}/compose").text)
    assert "composer--held" not in composer
    assert not re.search(r"\sdisabled[\s>]", composer), "the box and Send are live"
    assert 'placeholder="Prompt"' in composer
    assert "data-hp-holds" not in site.http.get(f"/agent/{S1}/answer").text


def test_a_held_composer_keeps_its_draft_and_the_words_to_go_back_to(tmp_path: Path) -> None:
    composer = _composer(_site(tmp_path, SCREENS["permission"]).http.get(f"/agent/{S1}").text)
    # The draft is kept by the session's key, and the script puts it back in the box.
    assert f'data-hp-draft="{S1}"' in composer
    assert 'data-hp-open="Prompt"' in composer
    assert f'data-hp-shut="{_HELD}"' in composer


def test_a_prompt_refused_at_a_question_keeps_its_text_in_the_held_composer(tmp_path: Path) -> None:
    site = _site(tmp_path, SCREENS["permission"])
    sent = site.http.post(f"/agent/{S1}/prompt", data={"text": "a draft"}, headers=ORIGIN)
    assert sent.status_code == 409
    composer = _composer(sent.text)
    assert ">a draft</textarea>" in composer
    assert "composer--held" in composer
    assert site.herd.writes("agent.prompt") == []


@pytest.mark.xfail(
    strict=True,
    reason="a typed answer needs its own write path: a prompt at a question is refused",
)
def test_a_question_answered_by_typing_keeps_the_composer_for_the_answer(tmp_path: Path) -> None:
    site = _site(tmp_path, _captured(ELICITATION_FORM))
    composer = _composer(site.http.get(f"/agent/{S1}").text)
    assert "composer--held" not in composer
