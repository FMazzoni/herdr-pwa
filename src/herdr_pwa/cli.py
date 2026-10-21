"""Command-line entry point: ``serve`` the web app, ``pair`` a phone, or ask ``doctor``."""

import argparse
import ipaddress
import logging
import os
import sys
from dataclasses import dataclass, field
from importlib.metadata import version
from typing import Final

import uvicorn

from herdr_pwa import auth, devreport, pairing, tailscale
from herdr_pwa.audit import AuditUnavailableError
from herdr_pwa.engine import HERDR_KINDS, EngineError, engine_settings, harness_kinds
from herdr_pwa.protocol import EXPECTED_PROTOCOL
from herdr_pwa.push import DeliveryLog, VapidKeyError, health_line
from herdr_pwa.settings import Settings
from herdr_pwa.web.app import create_app
from herdr_pwa.web.seams import Seams
from herdr_pwa.web.started import check_tree, record_started
from herdr_pwa.writes.lifecycle import (
    Availability,
    Declared,
    availability,
    declared_models,
    models_env,
    record_service_models,
    record_service_path,
    service_models,
    service_path,
)

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


def model_lines(recorded: Declared | None, settings: Settings) -> list[str]:
    """Each kind's declared models as the service has them (*recorded*) or, without, this shell's."""
    if recorded is None:
        head = "models (the service never recorded its own, so this shell's settings):"
        declared = declared_models(settings)
    else:
        head, declared = "models, as the service declared them:", recorded
    return [head, *(_model_line(kind, models) for kind, models in declared.items())]


def _model_line(kind: str, models: tuple[str, ...]) -> str:
    if models:
        return f"  {kind}: {', '.join(models)}"
    return f"  {kind}: none declared ({models_env(kind)}), so the form offers no model"


@dataclass(frozen=True, slots=True)
class HostLine:
    """What doctor says of the served name, and whether one is known."""

    line: str
    known: bool = field(kw_only=True)


def served_host_line(settings: Settings, api: tailscale.LocalApi) -> HostLine:
    """The Host a header identity must carry, resolved as ``serve`` resolves it."""
    source = "HP_PUBLIC_HOST" if settings.public_host else "tailscaled's MagicDNS name"
    try:
        host = auth.served_host(settings.public_host, api)
    except tailscale.LocalApiError as exc:
        return HostLine(
            f"served host: unknown, so every Tailscale identity is refused ({exc})  FAIL",
            known=False,
        )
    return HostLine(f"served host: {host} (from {source}); any other Host is refused", known=True)


def doctor(settings: Settings, seams: Seams) -> int:
    """Check the runner's protocol against the pin and the service's code against the disk.

    Non-zero on protocol drift, a silent runner, a tree that moved since the service started,
    or no served name to accept identities on.
    """
    engine = engine_settings(settings)
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
    # A declared list goes stale silently on the phone, so it is said where it can be checked.
    lines.extend(model_lines(service_models(settings.state_dir.expanduser()), settings))
    tree = check_tree(settings.state_dir.expanduser())
    lines.append(tree.line)
    host = served_host_line(settings, seams.local_api(settings.tailscaled_socket))
    lines.append(host.line)
    healthy = live == EXPECTED_PROTOCOL and not tree.stale and host.known
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


def _learned_host(settings: Settings, api: tailscale.LocalApi) -> str:
    try:
        return auth.served_host(settings.public_host, api)
    except tailscale.LocalApiError as exc:
        reason = str(exc)
    # Fail closed: an unknown name would otherwise mean accepting any Host, rebinding included.
    log.error(
        "Refusing every Tailscale identity: HP_PUBLIC_HOST is not set and this node's "
        "MagicDNS name could not be learned (%s)",
        reason,
    )
    return ""


def serve(settings: Settings, port: int | None, seams: Seams) -> None:
    """Refuse a reachable bind or an unsafe assumed login, then serve the app."""
    assert_loopback_bind(settings.host)
    access = auth.Access(tailnet_login=settings.tailnet_login, assume_login=settings.assume_login)
    api = seams.local_api(settings.tailscaled_socket)
    auth.assert_assumed_login_safe(access, api)
    settings = settings.model_copy(update={"public_host": _learned_host(settings, api)})
    if settings.public_host:
        log.info("Tailscale identities are accepted on Host %r only", settings.public_host)
    warning = auth.unpinned_warning(access)
    if warning:
        log.warning(warning)
    record_service_path(settings.state_dir.expanduser())
    record_service_models(settings.state_dir.expanduser(), declared_models(settings))
    record_started(settings.state_dir.expanduser(), seams.wall())
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


class _Args(argparse.Namespace):
    # argparse.Namespace is untyped; these declare what the subcommands below can set.
    command: str = ""
    port: int | None = None
    session: str = ""
    check: bool = False


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the chosen command."""
    parser = argparse.ArgumentParser(prog="herdr-pwa")
    parser.add_argument("--version", action="version", version=f"%(prog)s {version('herdr-pwa')}")
    sub = parser.add_subparsers(dest="command", required=True)
    serve_cmd = sub.add_parser("serve", help="serve the dashboard on the loopback port")
    serve_cmd.add_argument("--port", type=int, help="a port instead of HP_PORT")
    sub.add_parser("doctor", help="check the runner and the running service")
    pair_cmd = sub.add_parser("pair", help="publish over tailscale serve and print a QR")
    pair_cmd.add_argument(
        "--check", action="store_true", help="report what serve has, change nothing"
    )
    report_cmd = sub.add_parser("device-report", help="print the latest phone device report")
    report_cmd.add_argument("--session", default="", help="a session id instead of the latest")
    args = parser.parse_args(argv, namespace=_Args())
    settings = Settings()
    seams = Seams()
    if args.command == "doctor":
        return doctor(settings, seams)
    if args.command == "device-report":
        return device_report(settings, seams, args.session)
    if args.command == "pair":
        return pair(settings, seams, publish=not args.check)
    try:
        serve(settings, args.port, seams)
    # A key or audit log that cannot be used stops the box serving; say why, not a traceback.
    except (auth.AuthError, NonLoopbackBindError, VapidKeyError, AuditUnavailableError) as exc:
        sys.stderr.write(f"serve: {exc}\n")
        return 1
    return 0
