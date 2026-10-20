"""The Chat and Pane tabs keep their end in headless Firefox; the walks are scripts/chatend.py's."""

import pytest

from scripts.chatend import (
    WalkCtx,
    asking_pane_tab,
    cold,
    late_style,
    pane_tab,
    plan_coming_tab,
    question_arrives,
)
from tests.browser.conftest import Served

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("tab", [pane_tab("pane cold")], ids=["pane-cold"])
def test_a_tab_loaded_from_its_address_lands_on_its_newest_line(ctx: WalkCtx) -> None:
    assert cold(ctx) == []


@pytest.mark.parametrize("tab", [asking_pane_tab("pane question late style")], ids=["pane"])
def test_a_question_lands_at_the_end_when_the_stylesheet_applies_after_the_scripts(
    ctx: WalkCtx, served: Served
) -> None:
    assert late_style(served.switch.mount)(ctx) == []


@pytest.mark.parametrize("tab", [plan_coming_tab("tall plan arrives")], ids=["pane"])
def test_a_tall_question_that_arrives_at_the_end_lands_with_its_top_under_the_bar(
    ctx: WalkCtx,
) -> None:
    assert question_arrives(ctx) == []
