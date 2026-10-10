"""Web Push: the VAPID identity, the device store, the hostile-text payload, and delivery."""

import base64
import json
import logging
import re
import time
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol, Self, cast

import pywebpush
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from pywebpush import WebPushException

from herdr_pwa import jsonio, statedir
from herdr_pwa.statedir import StateError

log = logging.getLogger(__name__)

VAPID_FILENAME = "vapid.json"
SUBSCRIPTIONS_FILENAME = "subscriptions.json"
HEALTH_FILENAME = "push-health.json"
EVENT_BLOCKED = "blocked"
EVENT_DONE = "done"
EVENT_CLEAR = "clear"
HERD_TAG = "herd"
MAX_TITLE_CHARS = 60
MAX_BODY_CHARS = 180
MAX_AGENT_CHARS = 64
# Push services reject roughly 4 KiB after encryption.
MAX_PAYLOAD_BYTES = 3072
MAX_ENDPOINT_CHARS = 2048
LINK_PLACEHOLDER = "[link]"
# RFC 2606 names never resolve; Apple answers 403 BadJwtToken to a contact under one.
RESERVED_TLDS = frozenset({"invalid", "example", "test", "localhost", "local"})

_PRIVATE_BYTES = 32
_POINT_BYTES = 65
_AUTH_BYTES = 16
_UNCOMPRESSED = 0x04

Clock = Callable[[], float]


class PushError(Exception):
    """Base for every failure this module raises."""


class VapidKeyError(PushError):
    """The keypair on disk is unreadable, malformed, or its halves disagree."""


class SubjectError(PushError):
    """The VAPID contact is one a push service will refuse."""


class SubscriptionError(PushError):
    """A subscription, or the store file, is not the shape it must be."""


class DeliveryError(PushError):
    """One device, or its push service, refused; carries the HTTP status when known."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status: int | None = status

    @property
    def gone(self) -> bool:
        """True when the push service says this subscription no longer exists."""
        return self.status in {404, 410}


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(value: object, *, label: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty base64url string")
    text = value.strip().replace("+", "-").replace("/", "_")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except ValueError as exc:
        raise ValueError(f"{label} is not valid base64url: {exc}") from exc


def _subject_host(subject: str) -> str:
    if subject.startswith("mailto:"):
        address = subject.removeprefix("mailto:").strip()
        return address.rpartition("@")[2] if address.count("@") == 1 else ""
    host = subject.removeprefix("https://").split("/", 1)[0]
    return host.rpartition("@")[2].partition(":")[0]


def subject_problem(subject: str) -> str:
    """Why a push service would refuse this VAPID ``sub`` claim, or ``""``."""
    value = subject.strip()
    if not value:
        return "is not set; Apple rejects a JWT whose contact nobody could be reached at"
    if not value.startswith(("mailto:", "https://")):
        return f"is {value!r}, which is neither mailto: nor https:// (RFC 8292)"
    host = _subject_host(value)
    if not host:
        return f"is {value!r}, which names no host anybody could be reached at"
    label = host.rstrip(".").rpartition(".")[2] or host.rstrip(".")
    if label.lower() in RESERVED_TLDS:
        return f"is {value!r}, whose .{label} is a reserved name that never resolves (RFC 2606)"
    return ""


def check_subject(subject: str) -> str:
    """The contact to sign as, or SubjectError naming what is wrong with it."""
    problem = subject_problem(subject)
    if problem:
        raise SubjectError(
            f"push is on, but its contact {problem}. Set HP_PUSH_SUBJECT to a mailto: you "
            "read, or HP_PUSH_ENABLED=0."
        )
    return subject.strip()


@dataclass(frozen=True, slots=True)
class VapidKeys:
    """The application server's identity, base64url-encoded as the Web Push APIs spell it."""

    public_key: str
    private_key: str
    path: Path

    def __repr__(self) -> str:
        """Keep the private half out of every log line and traceback."""
        return f"VapidKeys(public_key={self.public_key!r}, private_key=<redacted>)"


def _derive_public(private_raw: bytes) -> bytes:
    key = ec.derive_private_key(int.from_bytes(private_raw, "big"), ec.SECP256R1())
    return key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)


def _validate_pair(private_raw: bytes, public_raw: bytes, *, path: Path) -> None:
    if len(private_raw) != _PRIVATE_BYTES:
        raise VapidKeyError(f"{path}: private key is {len(private_raw)} bytes, expected 32")
    if len(public_raw) != _POINT_BYTES or public_raw[0] != _UNCOMPRESSED:
        raise VapidKeyError(f"{path}: public key is not an uncompressed P-256 point")
    try:
        derived = _derive_public(private_raw)
    except ValueError as exc:
        raise VapidKeyError(f"{path}: private key is not a valid P-256 scalar") from exc
    if derived != public_raw:
        # Every subscribed device is bound to one of the halves; guessing orphans the rest.
        raise VapidKeyError(f"{path}: the public key is not the one the private key derives")


def _parse_vapid(text: str, *, path: Path) -> VapidKeys:
    try:
        document = jsonio.as_dict(jsonio.loads(text))
    except json.JSONDecodeError as exc:
        raise VapidKeyError(
            f"{path} is not valid JSON ({exc}). Refusing to mint a replacement: a new key "
            "orphans every subscribed device. Restore the file, or delete it deliberately."
        ) from exc
    if document is None:
        raise VapidKeyError(f"{path}: expected a JSON object")
    try:
        private_raw = _b64u_decode(document.get("private_key"), label="private_key")
        public_raw = _b64u_decode(document.get("public_key"), label="public_key")
    except ValueError as exc:
        raise VapidKeyError(f"{path}: {exc}") from exc
    _validate_pair(private_raw, public_raw, path=path)
    return VapidKeys(_b64u_encode(public_raw), _b64u_encode(private_raw), path)


def _read(path: Path) -> str | None:
    try:
        return statedir.read_text(path)
    except StateError as exc:
        raise VapidKeyError(str(exc)) from exc


def load_or_create_vapid_keys(state_dir: Path, *, clock: Clock = time.time) -> VapidKeys:
    """The keypair, created once with O_EXCL and reused forever after."""
    try:
        path = statedir.ensure_dir(state_dir) / VAPID_FILENAME
    except StateError as exc:
        raise VapidKeyError(str(exc)) from exc
    existing = _read(path)
    if existing is not None:
        return _parse_vapid(existing, path=path)
    key = ec.generate_private_key(ec.SECP256R1())
    private_raw = key.private_numbers().private_value.to_bytes(32, "big")
    public_raw = key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    keys = VapidKeys(_b64u_encode(public_raw), _b64u_encode(private_raw), path)
    document = {
        "version": 1,
        "created_at": int(clock()),
        "curve": "P-256",
        "public_key": keys.public_key,
        "private_key": keys.private_key,
    }
    try:
        statedir.write_secret(path, json.dumps(document, indent=2) + "\n", exclusive=True)
    except FileExistsError:
        # Another process won the race; its key is as good as ours.
        return _parse_vapid(_read(path) or "", path=path)
    except (OSError, StateError) as exc:
        raise VapidKeyError(f"cannot write {path}: {exc}") from exc
    log.info("generated a new VAPID keypair at %s", path)
    return keys


_ENDPOINT_RE = re.compile(r"^https://[^\s/@]+(?:/\S*)?$")


def _checked_key(value: object, *, label: str, size: int) -> str:
    try:
        raw = _b64u_decode(value, label=f"keys.{label}")
    except ValueError as exc:
        raise SubscriptionError(str(exc)) from exc
    if len(raw) != size:
        raise SubscriptionError(f"keys.{label} decodes to {len(raw)} bytes, expected {size}")
    if size == _POINT_BYTES and raw[0] != _UNCOMPRESSED:
        raise SubscriptionError("keys.p256dh is not an uncompressed P-256 point")
    return _b64u_encode(raw)


def _checked_expiry(value: object) -> float | None:
    if value is None:
        return None
    number = jsonio.as_float(value)
    if number is None:
        raise SubscriptionError("expirationTime must be a number or null")
    return number


def _checked_endpoint(value: object) -> str:
    endpoint = jsonio.as_str(value)
    if endpoint is None or not _ENDPOINT_RE.match(endpoint):
        raise SubscriptionError("endpoint must be an https:// URL")
    if len(endpoint) > MAX_ENDPOINT_CHARS:
        raise SubscriptionError(f"endpoint is over the {MAX_ENDPOINT_CHARS} character cap")
    return endpoint


@dataclass(frozen=True, slots=True)
class Subscription:
    """One device's PushSubscription, validated down to its key lengths."""

    endpoint: str
    p256dh: str
    auth: str
    expiration_time: float | None = None
    added_at: int = 0

    @classmethod
    def parse(cls, raw: object, *, added_at: int = 0) -> Self:
        """Validate a browser ``PushSubscription.toJSON()`` object."""
        document = jsonio.as_dict(raw)
        if document is None:
            raise SubscriptionError("expected a PushSubscription object")
        keys = jsonio.as_dict(document.get("keys"))
        endpoint = _checked_endpoint(document.get("endpoint"))
        if keys is None:
            raise SubscriptionError("keys must be an object with p256dh and auth")
        return cls(
            endpoint=endpoint,
            p256dh=_checked_key(keys.get("p256dh"), label="p256dh", size=_POINT_BYTES),
            auth=_checked_key(keys.get("auth"), label="auth", size=_AUTH_BYTES),
            expiration_time=_checked_expiry(document.get("expirationTime")),
            added_at=added_at,
        )

    def push_info(self) -> dict[str, str | dict[str, str]]:
        """The ``subscription_info`` mapping pywebpush expects."""
        return {"endpoint": self.endpoint, "keys": {"p256dh": self.p256dh, "auth": self.auth}}

    def as_json(self) -> dict[str, object]:
        """The on-disk form: the browser's field names plus a stamp."""
        return {
            **self.push_info(),
            "expirationTime": self.expiration_time,
            "added_at": self.added_at,
        }


class SubscriptionStore:
    """Every subscribed device in one 0600 JSON file, keyed by endpoint."""

    def __init__(self, state_dir: Path, *, clock: Clock = time.time) -> None:
        self._state_dir: Path = state_dir
        self._clock: Clock = clock

    @property
    def path(self) -> Path:
        """The store file; its directory is created 0700 on first use."""
        try:
            return statedir.ensure_dir(self._state_dir) / SUBSCRIPTIONS_FILENAME
        except StateError as exc:
            raise SubscriptionError(str(exc)) from exc

    def list_all(self) -> list[Subscription]:
        """Every valid subscription, oldest first; a malformed entry is logged and dropped."""
        out: list[Subscription] = []
        for index, entry in enumerate(self._load()):
            try:
                stamp = jsonio.as_int(jsonio.child(entry, "added_at")) or 0
                out.append(Subscription.parse(entry, added_at=stamp))
            except SubscriptionError as exc:
                log.warning("%s: dropping malformed entry %d: %s", self.path, index, exc)
        return out

    def add(self, raw: object) -> Subscription:
        """Store a device; the same endpoint again updates its keys and keeps its stamp."""
        parsed = Subscription.parse(raw, added_at=int(self._clock()))
        current = self.list_all()
        previous = next((one for one in current if one.endpoint == parsed.endpoint), None)
        if previous is not None:
            parsed = replace(parsed, added_at=previous.added_at)
        self._save([*(one for one in current if one.endpoint != parsed.endpoint), parsed])
        return parsed

    def remove(self, endpoint: str) -> bool:
        """Forget one device; True when it was there."""
        current = self.list_all()
        kept = [one for one in current if one.endpoint != endpoint]
        if len(kept) == len(current):
            return False
        self._save(kept)
        return True

    def _load(self) -> list[object]:
        try:
            text = statedir.read_text(self.path)
        except StateError as exc:
            raise SubscriptionError(str(exc)) from exc
        if text is None or not text.strip():
            return []
        try:
            document = jsonio.loads(text)
        except json.JSONDecodeError as exc:
            raise SubscriptionError(f"{self.path} is not valid JSON: {exc}") from exc
        entries = jsonio.as_list(jsonio.child(document, "subscriptions"))
        if entries is None:
            raise SubscriptionError(f"{self.path}: expected an object with a 'subscriptions' list")
        return entries

    def _save(self, subscriptions: Sequence[Subscription]) -> None:
        document = {"version": 1, "subscriptions": [one.as_json() for one in subscriptions]}
        try:
            statedir.write_secret(self.path, json.dumps(document, indent=2) + "\n")
        except (OSError, StateError) as exc:
            raise SubscriptionError(f"cannot write {self.path}: {exc}") from exc


_ANSI_RE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC, e.g. a title-setting sequence
    r"|\x1b\[[0-?]*[ -/]*[@-~]"  # CSI, e.g. colour
    r"|\x1b[@-Z\\-_]"  # any other two-character escape
)
_LINKY_TLDS = "com|net|org|io|dev|ai|co|me|app|sh|xyz|gg|to|ly"
# Schemes with no // that still navigate or execute.
_BARE_SCHEMES = "data|javascript|vbscript|file|blob|tel|mailto|sms|intent|market"
_URL_RE = re.compile(
    rf"""(?ix)
    (?:
        [a-z][a-z0-9+.\-]*://\S+
      | (?:{_BARE_SCHEMES}):\S*
      | www\.\S+
      | [a-z0-9][a-z0-9\-]*(?:\.[a-z0-9\-]+)*\.(?:{_LINKY_TLDS})\b(?:[/?\#]\S*)?
      | [a-z0-9][a-z0-9\-]*(?:\.[a-z0-9\-]+)+/\S*
    )
    """
)
_INVISIBLE = frozenset({"Cc", "Cf", "Cs", "Co"})


def sanitise(text: str, *, limit: int) -> str:
    """Agent output reduced to what is safe on a lock screen: no escapes, controls or links."""
    cleaned = _ANSI_RE.sub("", text)
    cleaned = "".join(" " if unicodedata.category(ch) in _INVISIBLE else ch for ch in cleaned)
    # An autolinked lock screen is a phishing primitive.
    cleaned = " ".join(_URL_RE.sub(LINK_PLACEHOLDER, cleaned).split())
    if len(cleaned) > limit:
        cleaned = cleaned[: max(limit - 1, 0)].rstrip() + "…"
    return cleaned


@dataclass(frozen=True, slots=True)
class Notice:
    """What one notification says, before sanitising; the agent is the session to deep-link."""

    event: str
    title: str
    body: str
    agent: str = ""
    fallback: str = ""
    tag: str = HERD_TAG
    quiet: bool = False


def build_payload(notice: Notice, *, now: float) -> dict[str, object]:
    """The notification the service worker renders, every text field sanitised and capped."""
    if notice.event not in {EVENT_BLOCKED, EVENT_DONE}:
        raise PushError(f"unknown event {notice.event!r}: only blocked and done notify")
    body = sanitise(notice.body, limit=MAX_BODY_CHARS)
    if not body.replace(LINK_PLACEHOLDER, "").strip():
        body = sanitise(notice.fallback, limit=MAX_BODY_CHARS)
    return {
        "v": 1,
        "event": notice.event,
        "agent": sanitise(notice.agent, limit=MAX_AGENT_CHARS),
        "tag": notice.tag,
        "title": sanitise(notice.title, limit=MAX_TITLE_CHARS),
        "body": body,
        "quiet": notice.quiet,
        "ts": int(now),
    }


def build_clear(*, now: float, tag: str = HERD_TAG) -> dict[str, object]:
    """The retraction: take the slot down and raise nothing."""
    return {"v": 1, "event": EVENT_CLEAR, "agent": "", "tag": tag, "ts": int(now)}


def encode_payload(payload: dict[str, object]) -> str:
    """Compact JSON for the wire, refusing anything over the size ceiling."""
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    size = len(encoded.encode("utf-8"))
    if size > MAX_PAYLOAD_BYTES:
        raise PushError(f"payload is {size} bytes, over the {MAX_PAYLOAD_BYTES} cap")
    return encoded


class Deliver(Protocol):
    """Send one encrypted payload to one device, raising DeliveryError on refusal."""

    def __call__(self, subscription: Subscription, data: str) -> None: ...


class _WebPush(Protocol):
    def __call__(
        self,
        subscription_info: dict[str, str | dict[str, str]],
        data: str,
        vapid_private_key: str,
        vapid_claims: dict[str, str | int],
        ttl: int,
    ) -> object: ...


# pywebpush's own signature leaves the subscription mapping's types unknown.
_webpush = cast("_WebPush", pywebpush.webpush)


@dataclass(frozen=True, slots=True)
class WebPushSender:
    """The real sender: pywebpush signed with this box's VAPID key."""

    keys: VapidKeys
    subject: str
    ttl: int

    def __call__(self, subscription: Subscription, data: str) -> None:
        """Encrypt and post; a refusal becomes DeliveryError with the push service's status."""
        try:
            _ = _webpush(
                subscription_info=subscription.push_info(),
                data=data,
                ttl=self.ttl,
                vapid_private_key=self.keys.private_key,
                vapid_claims={"sub": self.subject},
            )
        except WebPushException as exc:
            raise DeliveryError(str(exc), status=exc.status_code) from exc
        except Exception as exc:
            raise DeliveryError(f"{type(exc).__name__}: {exc}") from exc


def arm_sender(keys: VapidKeys, *, subject: str, ttl: int) -> WebPushSender:
    """The sender, refused here where push is armed when its contact would be rejected."""
    return WebPushSender(keys=keys, subject=check_subject(subject), ttl=ttl)


@dataclass(frozen=True, slots=True)
class DeliveryRecord:
    """Delivery counters as last written, for doctor and the settings sheet."""

    sent: int = 0
    failed: int = 0
    pruned: int = 0
    quieted: int = 0
    last_send_at: float = 0.0
    last_error: str = ""
    last_error_at: float = 0.0
    last_quiet_at: float = 0.0
    updated_at: float = 0.0

    @property
    def never_delivered(self) -> bool:
        """Something tried and nothing has ever got through."""
        return self.sent == 0 and self.failed > 0


_RECORD_INTS = ("sent", "failed", "pruned", "quieted")
_RECORD_FLOATS = ("last_send_at", "last_error_at", "last_quiet_at", "updated_at")


def _record_from(document: dict[str, object]) -> DeliveryRecord:
    ints = {name: jsonio.as_int(document.get(name)) or 0 for name in _RECORD_INTS}
    floats = {name: jsonio.as_float(document.get(name)) or 0.0 for name in _RECORD_FLOATS}
    error = jsonio.as_str(document.get("last_error")) or ""
    return DeliveryRecord(
        sent=ints["sent"],
        failed=ints["failed"],
        pruned=ints["pruned"],
        quieted=ints["quieted"],
        last_send_at=floats["last_send_at"],
        last_error=error,
        last_error_at=floats["last_error_at"],
        last_quiet_at=floats["last_quiet_at"],
        updated_at=floats["updated_at"],
    )


class DeliveryLog:
    """Every delivery outcome, written to disk as it happens so another process can read it."""

    def __init__(self, state_dir: Path, *, clock: Clock = time.time) -> None:
        self.path: Path = state_dir / HEALTH_FILENAME
        self._clock: Clock = clock
        self._current: DeliveryRecord | None = None

    def load(self) -> DeliveryRecord | None:
        """The last outcomes on disk, or None when there are none this build reads."""
        try:
            text = statedir.read_text(self.path)
            document = jsonio.as_dict(jsonio.loads(text or ""))
        except (StateError, json.JSONDecodeError):
            return None
        if document is None or document.get("version") != 1:
            return None
        return _record_from(document)

    def sent(self) -> None:
        """One device accepted a push."""
        self._bump(lambda r, now: replace(r, sent=r.sent + 1, last_send_at=now))

    def failed(self, detail: str) -> None:
        """One push failed for a reason other than the device being gone."""
        self._bump(
            lambda r, now: replace(r, failed=r.failed + 1, last_error=detail, last_error_at=now)
        )

    def pruned(self) -> None:
        """A device the push service retired was forgotten."""
        self._bump(lambda r, _now: replace(r, pruned=r.pruned + 1))

    def quieted(self, count: int) -> None:
        """Changes a preference or the managed mark held back."""
        self._bump(lambda r, now: replace(r, quieted=r.quieted + count, last_quiet_at=now))

    def _bump(self, change: Callable[[DeliveryRecord, float], DeliveryRecord]) -> None:
        now = self._clock()
        base = self._current or self.load() or DeliveryRecord()
        self._current = replace(change(base, now), updated_at=now)
        document = {"version": 1, **_record_json(self._current)}
        try:
            _ = statedir.ensure_dir(self.path.parent)
            statedir.write_secret(self.path, json.dumps(document, indent=2) + "\n")
        except (OSError, StateError) as exc:
            # Health is for the reader; failing to write it must never stop a push.
            log.warning("cannot write %s: %s; doctor will see nothing", self.path, exc)


def _record_json(record: DeliveryRecord) -> dict[str, object]:
    return {
        "sent": record.sent,
        "failed": record.failed,
        "pruned": record.pruned,
        "quieted": record.quieted,
        "last_send_at": round(record.last_send_at, 3),
        "last_error": record.last_error,
        "last_error_at": round(record.last_error_at, 3),
        "last_quiet_at": round(record.last_quiet_at, 3),
        "updated_at": round(record.updated_at, 3),
    }


def _prune(store: SubscriptionStore, subscription: Subscription) -> None:
    try:
        _ = store.remove(subscription.endpoint)
    except SubscriptionError as exc:
        log.warning("could not prune a retired subscription: %s", exc)


@dataclass(frozen=True, slots=True)
class FanOut:
    """How one payload fared across every device."""

    sent: int = 0
    failed: int = 0
    pruned: int = 0


def fan_out(
    data: str,
    subscriptions: Sequence[Subscription],
    *,
    deliver: Deliver,
    store: SubscriptionStore,
    health: DeliveryLog,
) -> FanOut:
    """Send to every device: prune the gone, record each outcome, never let one stop the rest."""
    sent = failed = pruned = 0
    for subscription in subscriptions:
        try:
            deliver(subscription, data)
        except DeliveryError as exc:
            if exc.gone:
                _prune(store, subscription)
                health.pruned()
                pruned += 1
                continue
            log.warning("push failed: %s", exc)
            health.failed(str(exc))
            failed += 1
            continue
        health.sent()
        sent += 1
    return FanOut(sent=sent, failed=failed, pruned=pruned)
