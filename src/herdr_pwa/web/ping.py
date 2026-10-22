"""``herdr-pwa ping``: one message from an agent or a script to every paired phone."""

import os
import pwd
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from herdr_pwa.audit import (
    PING,
    AuditLog,
    AuditUnavailableError,
    Entry,
    Origin,
    Write,
    attempts_between,
)
from herdr_pwa.auth import VIA_CLI
from herdr_pwa.clock import WallTime
from herdr_pwa.engine import EngineError, EngineSettings, as_pane_id, engine_settings
from herdr_pwa.notify_state import InterruptStore
from herdr_pwa.push import (
    EVENT_PING,
    LINK_PLACEHOLDER,
    MAX_BODY_CHARS,
    MAX_TITLE_CHARS,
    Deliver,
    DeliveryLog,
    FanOut,
    Notice,
    Outlet,
    PushError,
    Subscription,
    SubscriptionStore,
    arm_sender,
    build_payload,
    encode_payload,
    fan_out,
    read_vapid_keys,
    sanitise,
)
from herdr_pwa.settings import Settings
from herdr_pwa.web.seams import Seams

# Its own tag: a herd alert or its retraction never replaces a ping, and a newer ping does.
PING_TAG: Final = "ping"
DEFAULT_SENDER: Final = "herdr-pwa"
# Every ping says it is one, so a --title can never pose as an agent's alert on the lock screen.
PING_TITLE: Final = "Ping from {}"
# The pane a harness runs in, and the runner it belongs to, as herdr sets them in its env.
PANE_ENV: Final = "HERDR_PANE_ID"
SOCKET_ENV: Final = "HERDR_SOCKET_PATH"
# Seconds to ask the runner the caller's name; past it the ping goes out under the default.
NAME_TIMEOUT: Final = 2.0
# About this many: a looping agent must not buzz the phone all night; parallel bursts may pass.
PING_BURST: Final = 10
PING_WINDOW: Final = 600.0

EXIT_SENT: Final = 0
EXIT_UNDELIVERED: Final = 1
# What argparse exits with for a bad command line; a message that sanitises to nothing is one.
EXIT_USAGE: Final = 2
EXIT_UNAVAILABLE: Final = 3
EXIT_NO_DEVICES: Final = 4
EXIT_PARTIAL: Final = 5
EXIT_RATE_LIMITED: Final = 6


class _RefusedError(Exception):
    """A ping that cannot go out; the message is what the caller is told."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code: int = code


@dataclass(frozen=True, slots=True)
class Message:
    """What the caller asked to say, and as whom; None asks the runner for the pane's name."""

    text: str
    title: str | None = None


def ping(settings: Settings, seams: Seams, message: Message) -> int:
    """Send one notification to every paired phone, audited first; tapping it opens the herd.

    Returns:
        0 when every device took it, 1 when none did, 2 when the text sanitises to nothing,
        3 when push cannot be used here, 4 with no paired phone, 5 when some devices failed,
        6 when about ``PING_BURST`` pings went out in the last ``PING_WINDOW`` seconds.
    """
    try:
        line, code = _send(settings, seams, message)
    except _RefusedError as exc:
        sys.stderr.write(f"ping: {exc}\n")
        return exc.code
    # The one translation of what push refused (key, contact, store) into words for the caller.
    except PushError as exc:
        sys.stderr.write(f"ping: {exc}\n")
        return EXIT_UNAVAILABLE
    (seams.write if code == EXIT_SENT else sys.stderr.write)(line + "\n")
    return code


def caller_name(settings: Settings, seams: Seams) -> str:
    """The calling pane's agent name, or the default outside a pane or when the runner is silent."""
    environ = seams.environ
    pane = environ.get(PANE_ENV, "").strip()
    if not pane:
        return DEFAULT_SENDER
    socket = environ.get(SOCKET_ENV, "").strip()
    # The pane id is an address on the runner that started it, which may not be HP_SOCKET_PATH's.
    engine = engine_settings(settings)
    if socket:
        engine = EngineSettings(socket_path=Path(socket), command_timeout=NAME_TIMEOUT)
    client = seams.client(engine)
    try:
        title = client.agent(as_pane_id(pane), timeout=NAME_TIMEOUT).title
    except (EngineError, ValueError):
        return DEFAULT_SENDER
    return title or DEFAULT_SENDER


def _send(settings: Settings, seams: Seams, message: Message) -> tuple[str, int]:
    # Nothing to say is refused before any push config, device or runner is looked at.
    body = _body(message)
    state_dir = settings.state_dir.expanduser()
    deliver = _deliver(settings, seams, state_dir)
    store, devices = _devices(seams, state_dir)
    _check_rate(seams, state_dir)
    # Asked only once there is somewhere to send, so a refusal never waits on the runner.
    sender = message.title
    if sender is None:
        sender = caller_name(settings, seams)
    # A snooze holds finishes back; a ping still shows, but without a sound.
    policy, now = InterruptStore(state_dir, clock=seams.wall).current()
    payload = _payload(body, sender, quiet=policy.snoozed(now), now=now)
    data = encode_payload(payload)
    outlet = Outlet(
        deliver, store, DeliveryLog(state_dir, clock=seams.wall), seams.push_limits, seams.clock
    )
    with _audit(settings, seams, state_dir, payload) as entry:
        result = fan_out(data, devices, outlet, now=seams.wall())
        entry.outcome(f"sent:{result.sent} failed:{result.failed} pruned:{result.pruned}")
    return _verdict(result, len(devices))


def _body(message: Message) -> str:
    body = sanitise(message.text, limit=MAX_BODY_CHARS)
    if not body.replace(LINK_PLACEHOLDER, "").strip():
        raise _RefusedError(
            "nothing left to send: the text is empty once controls and links go", EXIT_USAGE
        )
    return body


def _deliver(settings: Settings, seams: Seams, state_dir: Path) -> Deliver:
    if not settings.push_enabled:
        raise _RefusedError(
            "push is switched off on this box (HP_PUSH_ENABLED=0)", EXIT_UNAVAILABLE
        )
    keys = read_vapid_keys(state_dir)
    if keys is None:
        raise _RefusedError(
            f"no VAPID key in {state_dir}: start the service and turn alerts on from the phone",
            EXIT_UNAVAILABLE,
        )
    if seams.deliver is not None:
        return seams.deliver
    return arm_sender(
        keys, subject=settings.push_subject, ttl=settings.push_ttl, limits=seams.push_limits
    )


def _devices(seams: Seams, state_dir: Path) -> tuple[SubscriptionStore, list[Subscription]]:
    store = SubscriptionStore(state_dir, clock=seams.wall, limits=seams.push_limits)
    try:
        devices = store.list_all()
    except OSError as exc:
        raise _RefusedError(f"cannot read the paired devices: {exc}", EXIT_UNAVAILABLE) from exc
    if not devices:
        raise _RefusedError(
            "no paired phone: open the app on the phone and turn alerts on", EXIT_NO_DEVICES
        )
    return store, devices


def _check_rate(seams: Seams, state_dir: Path) -> None:
    try:
        now = seams.wall()
        sent = attempts_between(state_dir, PING, now - PING_WINDOW, now)
    except OSError as exc:
        raise _RefusedError(f"cannot read the audit log: {exc}", EXIT_UNAVAILABLE) from exc
    if sent >= PING_BURST:
        raise _RefusedError(
            f"{sent} pings in the last {PING_WINDOW / 60:g} minutes, the most it sends; "
            "try again later",
            EXIT_RATE_LIMITED,
        )


def _payload(body: str, sender: str, *, quiet: bool, now: WallTime) -> dict[str, object]:
    name = sanitise(sender, limit=MAX_TITLE_CHARS - len(PING_TITLE.format(""))) or DEFAULT_SENDER
    title = PING_TITLE.format(name)
    notice = Notice(EVENT_PING, title=title, body=body, tag=PING_TAG, quiet=quiet)
    return build_payload(notice, now=now)


def _audit(settings: Settings, seams: Seams, state_dir: Path, payload: dict[str, object]) -> Entry:
    audit = AuditLog(
        state_dir,
        max_bytes=settings.audit_max_bytes,
        payload_chars=settings.audit_payload_chars,
        clock=seams.wall,
    )
    # The text recorded is what the lock screen shows, so the log answers who said what there.
    write = Write(PING, target=str(payload["title"]), payload=str(payload["body"]))
    try:
        return audit.record(_origin(), write)
    except AuditUnavailableError as exc:
        raise _RefusedError(f"{exc}; nothing was sent", EXIT_UNAVAILABLE) from exc


def _origin() -> Origin:
    # The uid is what admits a CLI caller: anyone running as it can already read the state dir.
    uid = os.getuid()
    try:
        name = pwd.getpwuid(uid).pw_name
    except KeyError:
        # A uid with no passwd entry, as in some containers, is still who ran it.
        name = str(uid)
    return Origin(name, VIA_CLI)


def _verdict(result: FanOut, devices: int) -> tuple[str, int]:
    line = f"ping: sent to {result.sent} of {devices} device(s)"
    if result.pruned:
        line += f"; forgot {result.pruned} the push service retired"
    if result.failed:
        line += f"; {result.failed} failed (herdr-pwa doctor shows the last error)"
    if result.sent and result.failed:
        return line, EXIT_PARTIAL
    return line, EXIT_SENT if result.sent else EXIT_UNDELIVERED
