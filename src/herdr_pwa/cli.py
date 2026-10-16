"""Command-line entry point: ``serve`` the web app, ``pair`` a phone, or ask ``doctor``."""

import argparse
import ipaddress
import logging
import os
import sys
from typing import Final

import uvicorn

from herdr_pwa import auth, devreport, pairing, tailscale
from herdr_pwa.audit import AuditUnavailableError
from herdr_pwa.engine import HERDR_KINDS, EngineError, harness_kinds
from herdr_pwa.protocol import EXPECTED_PROTOCOL
from herdr_pwa.push import DeliveryLog, VapidKeyError, health_line
from herdr_pwa.settings import Settings
from herdr_pwa.web.app import create_app
from herdr_pwa.web.seams import Seams
from herdr_pwa.writes.lifecycle import Availability, availability, record_service_path, service_path

log = logging.getLogger(__name__)


def _verdict(found: int | None) -> str:
    if found is None:
        return "FAIL (unknown)"
    return "ok" if found == EXPECTED_PROTOCOL else f"FAIL (pinned {EXPECTED_PROTOCOL})"


def _harness_line(found: Availability) -> str:
    if found.offered:
        return f"  {found.kind}: offered, at {found.found}"
    if found.known and found.stranded:
        return f"  {found.kind}: installed at {found.stranded}, not on the service's PATH  WARN"
    if found.known:
        return f"  {found.kind}: not installed"
    where = found.found or found.stranded
    return f"  {found.kind}: no harness here" + (f" (installed at {where})" if where else "")


def harness_lines(recorded: str | None) -> list[str]:
    """Every kind herdr runs, judged on the service's PATH (*recorded*) or, without it, this one."""
    if recorded is None:
        head = "harnesses (the service never recorded its PATH, so judged on this shell's):"
        path = os.environ.get("PATH", os.defpath)
    else:
        head, path = "harnesses, on the service's PATH:", recorded
    kinds = (*harness_kinds(), *(k for k in HERDR_KINDS if k not in harness_kinds()))
    return [head, *(_harness_line(availability(kind, path)) for kind in kinds)]


def doctor(settings: Settings, seams: Seams) -> int:
    """Compare the live runner's snapshot protocol with the pin; non-zero on drift or silence."""
    engine = settings.engine()
    lines = [f"pinned protocol: {EXPECTED_PROTOCOL}"]
    try:
        snapshot = seams.client(engine).snapshot()
    except EngineError as exc:
        live: int | None = None
        lines.append(f"runner at {engine.socket_path}: unreachable ({exc.message})  FAIL")
    else:
        live = snapshot.protocol
        lines.append(
            f"runner at {engine.socket_path}: version {snapshot.version or 'unknown'}, "
            f"{len(snapshot.agents)} agent(s), protocol {live}  {_verdict(live)}"
        )
    # Informational: a box that never delivered is worth seeing, but the runner decides the verdict.
    health = DeliveryLog(settings.state_dir.expanduser()).load()
    lines.append(health_line(health, now=seams.wall()))
    # Informational too: a harness the service cannot see is one the form silently cannot offer.
    lines.extend(harness_lines(service_path(settings.state_dir.expanduser())))
    healthy = live == EXPECTED_PROTOCOL
    lines.append("doctor: OK" if healthy else "doctor: FAIL")
    seams.write("\n".join(lines) + "\n")
    return 0 if healthy else 1


def pair(settings: Settings, seams: Seams, *, publish: bool = True) -> int:
    """Publish this app behind ``tailscale serve`` and print the URL as a QR; never Funnel."""
    local = seams.local_api(settings.tailscaled_socket)
    write = seams.write
    try:
        result = pairing.pair(local, port=settings.port, bind_host=settings.host, write=publish)
    except (pairing.PairingError, tailscale.LocalApiError) as exc:
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


class NonLoopbackBindError(Exception):
    """A listen address other peers can reach, where they could forge the identity header."""


def is_loopback_bind(host: str) -> bool:
    """True for a listen address only this machine can reach."""
    name = host.strip().removeprefix("[").removesuffix("]").lower()
    if name in auth.LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def assert_loopback_bind(host: str) -> None:
    """At startup: refuse a bind any other peer can reach, since the header is all it checks."""
    if is_loopback_bind(host):
        return
    raise NonLoopbackBindError(
        f"Refusing to start: HP_HOST={host!r} is not a loopback address, so any peer that "
        "reaches it can send its own Tailscale-User-Login header and drive every agent. "
        "Bind 127.0.0.1 and publish with 'herdr-pwa pair' (tailscale serve) instead."
    )


# Under the unit's TimeoutStopSec, so open streams are cut rather than the process killed.
SHUTDOWN_GRACE: Final = 3


def serve(settings: Settings, port: int | None, seams: Seams) -> None:
    """Refuse a reachable bind or an unsafe assumed login, then serve the app."""
    assert_loopback_bind(settings.host)
    access = auth.Access(tailnet_login=settings.tailnet_login, assume_login=settings.assume_login)
    auth.assert_assumed_login_safe(access, seams.local_api(settings.tailscaled_socket))
    warning = auth.unpinned_warning(access)
    if warning:
        log.warning(warning)
    record_service_path(settings.state_dir.expanduser())
    uvicorn.run(
        create_app(settings, seams),
        host=settings.host,
        port=port or settings.port,
        # An event stream never ends on its own; without this a restart waits on every phone.
        timeout_graceful_shutdown=SHUTDOWN_GRACE,
    )


def device_report(settings: Settings, seams: Seams, session: str = "") -> int:
    """Print the latest (or the named) device-report session from the state dir."""
    seams.write(devreport.render(settings.state_dir.expanduser(), session))
    return 0


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
    report_cmd = sub.add_parser("device-report", help="print the latest phone device report")
    report_cmd.add_argument("--session", default="", help="a session id instead of the latest")
    args = parser.parse_args(argv)
    settings = Settings()
    seams = Seams()
    command: str = args.command  # pyright: ignore[reportAny]
    if command == "doctor":
        return doctor(settings, seams)
    if command == "device-report":
        session: str = args.session  # pyright: ignore[reportAny]
        return device_report(settings, seams, session)
    if command == "pair":
        check: bool = args.check  # pyright: ignore[reportAny]
        return pair(settings, seams, publish=not check)
    port: int | None = args.port  # pyright: ignore[reportAny]
    try:
        serve(settings, port, seams)
    # A key or audit log that cannot be used stops the box serving; say why, not a traceback.
    except (auth.AuthError, NonLoopbackBindError, VapidKeyError, AuditUnavailableError) as exc:
        sys.stderr.write(f"serve: {exc}\n")
        return 1
    return 0
