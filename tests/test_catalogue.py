"""The screen catalogue: every named state draws, and the old app's names are all accounted for."""

import re
from pathlib import Path

import pytest

from tests.catalogue import (
    LEGACY_KEYS,
    NOT_YET,
    STATES,
    V2_ONLY,
    State,
    build,
    pick,
    ported,
)

# The fragments a page pulls in on load, which is where a broken screen usually breaks.
FRAGMENT = re.compile(r'hx-get="([^"]+)"')


def _statuses(state: State, home: Path) -> list[tuple[str, int]]:
    rig = build(state, home)
    page = rig.http.get(state.path, follow_redirects=False)
    urls: list[str] = list(dict.fromkeys(m.group(1) for m in FRAGMENT.finditer(page.text)))
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


def test_every_old_key_is_either_ported_or_listed_as_not_yet() -> None:
    assert set(ported()).isdisjoint(NOT_YET)
    assert sorted([*ported(), *NOT_YET]) == sorted(LEGACY_KEYS)
    assert len(LEGACY_KEYS) == len(set(LEGACY_KEYS)) == 89


def test_names_are_unique_and_every_new_name_is_declared() -> None:
    names = [s.name for s in STATES]
    assert len(names) == len(set(names))
    assert sorted(set(names) - set(LEGACY_KEYS)) == sorted(V2_ONLY)


def test_a_tap_state_says_what_to_wait_for_before_tapping() -> None:
    for state in STATES:
        assert bool(state.act) == bool(state.armed), state.name


def test_pick_keeps_catalogue_order_and_refuses_an_unknown_name() -> None:
    assert [s.name for s in pick(["feed-quiet", "feed-blocked"])] == ["feed-blocked", "feed-quiet"]
    assert len(pick([])) == len(STATES)
    with pytest.raises(KeyError, match="nope"):
        pick(["nope"])
