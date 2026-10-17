"""The Chat and Pane tabs keep their end in headless Firefox; the walks are scripts/chatend.py's."""

import pytest

from scripts.chatend import WalkCtx, cold, pane_tab

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("tab", [pane_tab("pane cold")], ids=["pane-cold"])
def test_a_tab_loaded_from_its_address_lands_on_its_newest_line(ctx: WalkCtx) -> None:
    assert cold(ctx) == []
