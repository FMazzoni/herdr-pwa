"""Command-line entry point: ``serve`` the app, ``pair`` a phone and ``ping`` it, or ``doctor``."""

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
from herdr_pwa.web import ping
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
class CheckLine:
    """What doctor says of one safeguard, and whether ``serve`` would run with it."""

    line: str
    ok: bool = field(kw_only=True)


def served_host_line(settings: Settings, api: tailscale.LocalApi) -> CheckLine:
    """The Host a header identity must carry, resolved as ``serve`` resolves it."""
    source = "HP_PUBLIC_HOST" if settings.public_host else "tailscaled's MagicDNS name"
    try:
        host = auth.served_host(settings.public_host, api)
    except tailscale.LocalApiError as exc:
        return CheckLine(
            f"served host: unknown, so every Tailscale identity is refused ({exc})  FAIL",
            ok=False,
        )
    return CheckLine(f"served host: {host} (from {source}); any other Host is refused", ok=True)


def login_line(settings: Settings) -> CheckLine:
    """Who the settings let in; with no login pinned and no opt-in, ``serve`` refuses to start."""
    access = access_of(settings)
    policy = f"who may drive agents: {auth.login_policy(access)}"
    if access.tailnet_login or access.assume_login:
        return CheckLine(policy, ok=True)
    if access.any_login:
        return CheckLine(f"{policy}  WARN", ok=True)
    return CheckLine(
        f"{policy}, so serve refuses to start; set it, or HP_ALLOW_ANY_TAILNET_USER=1  FAIL",
        ok=False,
    )


def raw_forward_line(settings: Settings, api: tailscale.LocalApi) -> CheckLine:
    """Whether serve forwards raw TCP to the app, which would let a forged header through."""
    try:
        found = tailscale.raw_forwards(api, port=settings.port)
    except tailscale.LocalApiError as exc:
        return CheckLine(
            f"raw TCP forward: unknown, the serve config is unreadable ({exc})  WARN", ok=True
        )
    if found:
        return CheckLine(
            f"raw TCP forward: {_raw_forward_problem(found, settings.port)}  FAIL", ok=False
        )
    return CheckLine(f"raw TCP forward: none to port {settings.port}  ok", ok=True)


def access_of(settings: Settings) -> auth.Access:
    """Who *settings* let in, before the served name is learned."""
    return auth.Access(
        tailnet_login=settings.tailnet_login,
        assume_login=settings.assume_login,
        any_login=settings.allow_any_tailnet_user,
    )


def doctor(settings: Settings, seams: Seams) -> int:
    """Check the runner's protocol against the pin and the service's code against the disk.

    Returns:
        Non-zero on protocol drift, a silent runner, a moved tree, no served name, no login
        chosen, or a raw TCP forward to the app.
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
    api = seams.local_api(settings.tailscaled_socket)
    checks = (
        served_host_line(settings, api),
        login_line(settings),
        raw_forward_line(settings, api),
    )
    lines.extend(check.line for check in checks)
    healthy = live == EXPECTED_PROTOCOL and not tree.stale and all(check.ok for check in checks)
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
        lines.append(f"warning: {login_line(settings).line}")
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


class RawForwardError(Exception):
    """Serve passes raw TCP to the app, so a client's own identity header reaches it unchanged."""


# Each stops the box serving, and is said in a line rather than a traceback.
SERVE_REFUSALS: Final = (
    auth.AuthError,
    NonLoopbackBindError,
    RawForwardError,
    VapidKeyError,
    AuditUnavailableError,
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


def _assert_no_raw_forward(api: tailscale.LocalApi, port: int) -> None:
    try:
        found = tailscale.raw_forwards(api, port=port)
    except tailscale.LocalApiError as exc:
        # Unreadable is not a forward; doctor reports it, and refusing would break a box without tailscaled.
        log.warning("Could not check 'tailscale serve' for a raw TCP forward: %s", exc)
        return
    if found:
        raise RawForwardError(f"Refusing to start: {_raw_forward_problem(found, port)}")


def _raw_forward_problem(found: tuple[str, ...], port: int) -> str:
    return (
        f"'tailscale serve' forwards raw TCP from port {', '.join(found)} to this app on {port}, "
        "which passes a client's own Tailscale-User-Login header through, so any tailnet device "
        "could act as you. Remove it with 'tailscale serve --tcp=<port> off' and publish with "
        "'herdr-pwa pair' instead."
    )


def serve(settings: Settings, port: int | None, seams: Seams) -> None:
    """Refuse a reachable bind, no chosen login, an unsafe assumed login or a raw TCP forward."""
    assert_loopback_bind(settings.host)
    access = access_of(settings)
    auth.assert_login_chosen(access)
    api = seams.local_api(settings.tailscaled_socket)
    auth.assert_assumed_login_safe(access, api)
    _assert_no_raw_forward(api, port or settings.port)
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
    text: str = ""
    title: str | None = None


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
    ping_cmd = sub.add_parser(
        "ping", help="send one notification to every paired phone (about 10 in 10 minutes at most)"
    )
    ping_cmd.add_argument("text", help="plain text, capped as an agent's is")
    ping_cmd.add_argument("--title", help="who it is from (default: this pane's agent name)")
    args = parser.parse_args(argv, namespace=_Args())
    settings = Settings()
    seams = Seams()
    if args.command == "doctor":
        return doctor(settings, seams)
    if args.command == "device-report":
        return device_report(settings, seams, args.session)
    if args.command == "pair":
        return pair(settings, seams, publish=not args.check)
    if args.command == "ping":
        return ping.ping(settings, seams, ping.Message(args.text, args.title))
    try:
        serve(settings, args.port, seams)
    except SERVE_REFUSALS as exc:
        sys.stderr.write(f"serve: {exc}\n")
        return 1
    return 0
