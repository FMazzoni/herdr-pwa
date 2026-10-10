"""Command-line entry point: ``serve`` the web app, ``pair`` a phone, or ask ``doctor``."""

import argparse
import logging
import sys
from collections.abc import Callable

import uvicorn

from herdr_pwa import auth, pairing, tailscale
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


def pair(
    settings: Settings,
    *,
    api: tailscale.LocalApi | None = None,
    write: Callable[[str], object] = sys.stdout.write,
    publish: bool = True,
) -> int:
    """Publish this app behind ``tailscale serve`` and print the URL as a QR; never Funnel."""
    local = api or tailscale.LocalApi(settings.tailscaled_socket)
    try:
        result = pairing.pair(local, port=settings.port, bind_host=settings.host, write=publish)
    except pairing.PairingError as exc:
        write(f"pair: {exc}\n")
        return 1
    lines = [result.note]
    if result.served:
        # No URL that resolves, no QR: a code for an unserved name is a dead end on the phone.
        lines += [pairing.qr_text(result.url), result.url, pairing.TAILNET_IS_THE_BOUNDARY]
    if not settings.tailnet_login:
        lines.append(
            "warning: HP_TAILNET_LOGIN is empty, so any tailnet user may drive every agent."
        )
    lines.append(
        "On the phone: open the URL, add it to the Home Screen, open it from there, "
        "then turn alerts on under 'alerts'."
    )
    write("\n".join(lines) + "\n")
    return 0 if result.served else 1


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
    pair_cmd = sub.add_parser("pair", help="publish over tailscale serve and print a QR")
    pair_cmd.add_argument(
        "--check", action="store_true", help="report what serve has, change nothing"
    )
    args = parser.parse_args(argv)
    settings = Settings()
    command: str = args.command  # pyright: ignore[reportAny]
    if command == "doctor":
        return doctor(settings)
    if command == "pair":
        check: bool = args.check  # pyright: ignore[reportAny]
        return pair(settings, publish=not check)
    port: int | None = args.port  # pyright: ignore[reportAny]
    serve(settings, port)
    return 0
