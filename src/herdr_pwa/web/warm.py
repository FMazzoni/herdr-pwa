"""What a restarted app reads and draws once, in the background, before anyone opens an agent."""

import logging
import threading

from herdr_pwa.engine import MAX_TAIL_FILES, EngineError, has_byte_frontier
from herdr_pwa.web.deps import WebState
from herdr_pwa.web.routers.agent import read_chat, read_stats

log = logging.getLogger(__name__)


def warm(web: WebState) -> int:
    """Fill the tail, usage and render caches for the most urgent agents whose logs only append.

    Returns:
        How many agents were warmed; never raises, since a cold cache costs latency, not a page.
    """
    try:
        agents = web.snapshot().value.agents
    except EngineError as exc:
        log.info("nothing warmed, the herd could not be read: %s", exc)
        return 0
    # Most urgent first; past the tail cache's room a warmed file would only push out another.
    held = [a for a in agents if a.session and has_byte_frontier(a.agent)]
    warmed = 0
    for agent in held[:MAX_TAIL_FILES]:
        try:
            read_stats(web, agent)
            chat = read_chat(web, agent)
            web.pages.render("chat.html", {"agent": agent, "chat": chat})
        except Exception:
            log.exception("warming %s failed", agent.session)
            continue
        warmed += 1
    return warmed


def warm_later(web: WebState) -> threading.Thread:
    """Start warming on a thread of its own, so the app answers while it runs."""
    thread = threading.Thread(target=warm, args=(web,), name="herdr-pwa-warm", daemon=True)
    thread.start()
    return thread
