"""Command-line entry point: ``serve`` the web app, or ask ``doctor`` whether it can."""

import argparse
import logging
import sys
from collections.abc import Callable

import uvicorn

from herdr_pwa import auth, tailscale
from herdr_pwa.engine.client import HerdrClient
from herdr_pwa.engine.transport import EngineError
from herdr_pwa.protocol import EXPECTED_PROTOCOL
from herdr_pwa.settings import Settings
from herdr_pwa.web.app import create_app
from herdr_pwa.web.deps import ClientFactory

log = logging.getLogger(__name__)


def _verdict(found: int | None) -> str:
    if found is None:
        return "FAIL (unknown)"
    return "ok" if found == EXPECTED_PROTOCOL else f"FAIL (pinned {EXPECTED_PROTOCOL})"


def doctor(
    settings: Settings,
    *,
    client_factory: ClientFactory = HerdrClient,
    write: Callable[[str], object] = sys.stdout.write,
) -> int:
    """Compare the live runner's snapshot protocol with the pin; non-zero on drift or silence."""
    engine = settings.engine()
    lines = [f"pinned protocol: {EXPECTED_PROTOCOL}"]
    try:
        snapshot = client_factory(engine).snapshot()
    except EngineError as exc:
        live: int | None = None
        lines.append(f"runner at {engine.socket_path}: unreachable ({exc.message})  FAIL")
    else:
        live = snapshot.protocol
        lines.append(
            f"runner at {engine.socket_path}: version {snapshot.version or 'unknown'}, "
            f"{len(snapshot.agents)} agent(s), protocol {live}  {_verdict(live)}"
        )
    healthy = live == EXPECTED_PROTOCOL
    lines.append("doctor: OK" if healthy else "doctor: FAIL")
    write("\n".join(lines) + "\n")
    return 0 if healthy else 1


def serve(settings: Settings, port: int | None) -> None:
    """Refuse an unsafe assumed login, then serve the app."""
    access = auth.Access(tailnet_login=settings.tailnet_login, assume_login=settings.assume_login)
    auth.assert_assumed_login_safe(access, tailscale.LocalApi(settings.tailscaled_socket))
    warning = auth.unpinned_warning(access)
    if warning:
        log.warning(warning)
    uvicorn.run(create_app(settings), host=settings.host, port=port or settings.port)


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the chosen command."""
    parser = argparse.ArgumentParser(prog="herdr-pwa")
    sub = parser.add_subparsers(dest="command", required=True)
    serve_cmd = sub.add_parser("serve")
    serve_cmd.add_argument("--port", type=int)
    sub.add_parser("doctor")
    args = parser.parse_args(argv)
    settings = Settings()
    command: str = args.command  # pyright: ignore[reportAny]
    if command == "doctor":
        return doctor(settings)
    port: int | None = args.port  # pyright: ignore[reportAny]
    serve(settings, port)
    return 0
