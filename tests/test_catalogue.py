"""The screen catalogue: every named state draws, under a name of its own."""

import re
import threading
from pathlib import Path

import pytest

from tests.catalogue import STATES, State, build, pick
from tests.webfakes import SEND_KEYS, Hold

# The fragments a page pulls in on load, which is where a broken screen usually breaks.
FRAGMENT = re.compile(r'hx-get="(?P<url>[^"]+)"')


def _statuses(state: State, home: Path) -> list[tuple[str, int]]:
    rig = build(state, home)
    page = rig.http.get(state.path, follow_redirects=False)
    urls: list[str] = list(dict.fromkeys(m["url"] for m in FRAGMENT.finditer(page.text)))
    fragments = [(u, rig.http.get(u.replace("&amp;", "&")).status_code) for u in urls]
    return [(state.path, page.status_code), *fragments]


@pytest.mark.parametrize("state", STATES, ids=[s.name for s in STATES])
def test_every_state_draws_without_a_server_error(state: State, tmp_path: Path) -> None:
    statuses = _statuses(state, tmp_path)
    errors = [(url, code) for url, code in statuses if code >= 500]
    if state.fails:
        assert errors, "a failure state that no longer fails shows nothing"
        assert {code for _, code in errors} == {503}
    elif state.login:
        assert [(url, code) for url, code in statuses if code >= 400] == []
    else:
        assert statuses == [(state.path, 401)]


def test_names_are_unique() -> None:
    names = [s.name for s in STATES]
    assert len(names) == len(set(names))


def test_a_tap_state_says_what_to_wait_for_before_tapping() -> None:
    for state in STATES:
        assert bool(state.act) == bool(state.armed), state.name


def test_pick_keeps_catalogue_order_and_refuses_an_unknown_name() -> None:
    assert [s.name for s in pick(["feed-quiet", "feed-blocked"])] == ["feed-blocked", "feed-quiet"]
    assert len(pick([])) == len(STATES)
    with pytest.raises(KeyError, match="nope"):
        pick(["nope"])


INPUT = re.compile(r'<input type="hidden" name="(\w+)" value="([^"]*)">')
OPTION_FORM = re.compile(r"<form[^>]*>(?:(?!</form>).)*button--option", re.DOTALL)


def test_the_sending_state_holds_its_key_until_released(tmp_path: Path) -> None:
    [state] = pick(["agent-answer-sending"])
    rig = build(state, tmp_path)
    hold = rig.wire.after[SEND_KEYS]
    assert isinstance(hold, Hold)
    agent = state.path.split("?")[0]
    found = OPTION_FORM.search(rig.http.get(f"{agent}/answer").text)
    assert found is not None
    fields = dict(INPUT.findall(found.group(0)))
    same_origin = {"Origin": str(rig.http.base_url).rstrip("/")}
    answers: list[int] = []

    def tap_it() -> None:
        answers.append(rig.http.post(f"{agent}/keys", data=fields, headers=same_origin).status_code)

    tap = threading.Thread(target=tap_it)
    tap.start()
    assert hold.taken.wait(10), "the key never reached the runner"
    assert answers == [], "the key answered while it was held"
    hold.release.set()
    tap.join(10)
    assert answers == [200]
    assert hold.released.is_set(), "the key went before it was released"
