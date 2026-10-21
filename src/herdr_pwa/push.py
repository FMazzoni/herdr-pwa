"""Web Push: the VAPID identity, the device store, the hostile-text payload, and delivery."""

import base64
import json
import logging
import re
import threading
import time
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Final, Literal, Protocol, Self, TypedDict, Unpack, cast
from urllib.parse import urlsplit

import pywebpush
import requests
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    PlainSerializer,
    PlainValidator,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
    model_validator,
)
from pywebpush import WebPushException

from herdr_pwa import statedir
from herdr_pwa.clock import Clock, ago
from herdr_pwa.panetext import without_escapes
from herdr_pwa.validation import is_not_json, or_default, summary

log = logging.getLogger(__name__)

VAPID_FILENAME: Final = "vapid.json"
SUBSCRIPTIONS_FILENAME: Final = "subscriptions.json"
HEALTH_FILENAME: Final = "push-health.json"
# What a push says happened: the two that notify, and the retraction of either.
type PushEvent = Literal["blocked", "done", "clear"]
EVENT_BLOCKED: Final = "blocked"
EVENT_DONE: Final = "done"
EVENT_CLEAR: Final = "clear"
HERD_TAG: Final = "herd"
MAX_TITLE_CHARS: Final = 60
MAX_BODY_CHARS: Final = 180
MAX_AGENT_CHARS: Final = 64
# Push services reject roughly 4 KiB after encryption.
MAX_PAYLOAD_BYTES: Final = 3072
MAX_ENDPOINT_CHARS: Final = 2048
# Each tick sends to every device in turn, so the count bounds how long a fan-out can take.
MAX_DEVICES: Final = 10
# The browsers' push services; anything else is a host the box would be told to post to.
PUSH_SERVICES: Final = (
    "push.apple.com",
    "fcm.googleapis.com",
    "android.googleapis.com",
    "push.services.mozilla.com",
    "notify.windows.com",
)
LINK_PLACEHOLDER: Final = "[link]"
# RFC 2606 names never resolve; Apple answers 403 BadJwtToken to a contact under one.
RESERVED_TLDS: Final = frozenset({"invalid", "example", "test", "localhost", "local"})

_PRIVATE_BYTES: Final = 32
_POINT_BYTES: Final = 65
_AUTH_BYTES: Final = 16
_UNCOMPRESSED: Final = 0x04
# The order n of P-256 (SEC 2, 2.4.2); a private scalar must lie in [1, n-1].
_P256_ORDER: Final = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551


class PushError(Exception):
    """Base for every failure this module raises."""


class VapidKeyError(PushError):
    """The keypair on disk is unreadable, malformed, or its halves disagree."""


class SubjectError(PushError):
    """The VAPID contact is one a push service will refuse."""


class SubscriptionError(PushError):
    """The subscription store file is not the shape it must be."""


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


def _b64u_decode(value: object) -> bytes:
    # binascii.Error is a ValueError, so a bad alphabet or padding fails the field it is in.
    if not isinstance(value, str) or not value:
        raise ValueError("must be a non-empty base64url string")
    text = value.strip().replace("+", "-").replace("/", "_")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# A key as the Web Push APIs spell it: base64url on disk and on the wire, bytes in memory.
type _Key = Annotated[bytes, PlainValidator(_b64u_decode), PlainSerializer(_b64u_encode)]


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


class _VapidFile(BaseModel):
    """vapid.json. What else it holds (version, created_at, curve) is for a person reading it."""

    model_config = ConfigDict(extra="allow", frozen=True)

    public_key: _Key
    private_key: _Key

    @model_validator(mode="after")
    def _one_pair(self) -> Self:
        if len(self.private_key) != _PRIVATE_BYTES:
            raise ValueError(f"private key is {len(self.private_key)} bytes, expected 32")
        if len(self.public_key) != _POINT_BYTES or self.public_key[0] != _UNCOMPRESSED:
            raise ValueError("public key is not an uncompressed P-256 point")
        if not 0 < int.from_bytes(self.private_key, "big") < _P256_ORDER:
            raise ValueError("private key is not a valid P-256 scalar")
        if _derive_public(self.private_key) != self.public_key:
            # Every subscribed device is bound to one of the halves; guessing orphans the rest.
            raise ValueError("the public key is not the one the private key derives")
        return self

    def keys(self, path: Path) -> VapidKeys:
        """The pair as the Web Push APIs spell it."""
        return VapidKeys(_b64u_encode(self.public_key), _b64u_encode(self.private_key), path)


def _vapid_problem(path: Path, exc: OSError | ValidationError) -> str:
    if isinstance(exc, OSError):
        return f"cannot use {path}: {exc}"
    if is_not_json(exc):
        return (
            f"{path} is not valid JSON ({summary(exc)}). Refusing to mint a replacement: a new "
            "key orphans every subscribed device. Restore the file, or delete it deliberately."
        )
    return f"{path}: {summary(exc)}"


def _minted(path: Path, *, clock: Clock) -> _VapidFile:
    key = ec.generate_private_key(ec.SECP256R1())
    private_raw = key.private_numbers().private_value.to_bytes(32, "big")
    public_raw = key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    document = {
        "public_key": _b64u_encode(public_raw),
        "private_key": _b64u_encode(private_raw),
        "version": 1,
        "created_at": int(clock()),
        "curve": "P-256",
    }
    minted = _VapidFile.model_validate(document)
    try:
        statedir.write_secret(path, minted.model_dump_json(indent=2) + "\n", exclusive=True)
    except FileExistsError:
        # Another process won the race; its key is as good as ours.
        return _VapidFile.model_validate_json(statedir.read_text(path) or "")
    log.info("generated a new VAPID keypair at %s", path)
    return minted


def load_or_create_vapid_keys(state_dir: Path, *, clock: Clock = time.time) -> VapidKeys:
    """The keypair, created once with O_EXCL and reused forever after.

    Raises:
        VapidKeyError: the file cannot be read or written, or is not one valid keypair.
    """
    path = state_dir.expanduser() / VAPID_FILENAME
    try:
        _ = statedir.ensure_dir(state_dir)
        existing = statedir.read_text(path)
        if existing is None:
            document = _minted(path, clock=clock)
        else:
            document = _VapidFile.model_validate_json(existing)
    except (OSError, ValidationError) as exc:
        raise VapidKeyError(_vapid_problem(path, exc)) from exc
    return document.keys(path)


_HTTPS: Final = "https://"


def _is_https_url(endpoint: str) -> bool:
    # A host with no userinfo, then any path: whitespace nowhere, a trailing newline included.
    host = endpoint.removeprefix(_HTTPS).partition("/")[0]
    if not endpoint.startswith(_HTTPS) or not host or "@" in host:
        return False
    return not any(c.isspace() for c in endpoint)


def _is_push_service(host: str) -> bool:
    return any(host == base or host.endswith(f".{base}") for base in PUSH_SERVICES)


def _checked_endpoint(endpoint: str) -> str:
    if len(endpoint) > MAX_ENDPOINT_CHARS:
        raise ValueError(f"endpoint is over the {MAX_ENDPOINT_CHARS} character cap")
    if not _is_https_url(endpoint):
        raise ValueError("endpoint must be an https:// URL")
    parts = urlsplit(endpoint)
    try:
        port = parts.port
    except ValueError:
        port = -1
    # An IP literal, localhost or a LAN name fails this too: the post would reach the box's network.
    if not _is_push_service(parts.hostname or "") or port not in {None, 443}:
        raise ValueError("endpoint is not on a browser push service")
    return endpoint


def _point(raw: bytes) -> bytes:
    if len(raw) != _POINT_BYTES:
        raise ValueError(f"decodes to {len(raw)} bytes, expected {_POINT_BYTES}")
    if raw[0] != _UNCOMPRESSED:
        raise ValueError("is not an uncompressed P-256 point")
    return raw


def _auth_secret(raw: bytes) -> bytes:
    if len(raw) != _AUTH_BYTES:
        raise ValueError(f"decodes to {len(raw)} bytes, expected {_AUTH_BYTES}")
    return raw


class SubscriptionKeys(BaseModel):
    """The device's ECDH public key and auth secret, checked down to their lengths."""

    model_config = ConfigDict(frozen=True)

    p256dh: Annotated[_Key, AfterValidator(_point)]
    auth: Annotated[_Key, AfterValidator(_auth_secret)]


class Subscription(BaseModel):
    """One device's ``PushSubscription.toJSON()``, plus when it was first stored."""

    model_config = ConfigDict(frozen=True, validate_by_name=True, serialize_by_alias=True)

    endpoint: Annotated[StrictStr, AfterValidator(_checked_endpoint)]
    keys: SubscriptionKeys
    expiration_time: Annotated[StrictFloat | None, Field(alias="expirationTime")] = None
    added_at: Annotated[StrictInt, or_default(0)] = 0

    def expired(self, now: float) -> bool:
        """Past its ``expirationTime`` (epoch milliseconds), after which the push service drops it."""
        return self.expiration_time is not None and self.expiration_time <= now * 1000

    def push_info(self) -> dict[str, str | dict[str, str]]:
        """The ``subscription_info`` mapping pywebpush expects."""
        keys = {"p256dh": _b64u_encode(self.keys.p256dh), "auth": _b64u_encode(self.keys.auth)}
        return {"endpoint": self.endpoint, "keys": keys}


class _StoreFile(BaseModel):
    """subscriptions.json as read: each entry is checked on its own, so one bad one is dropped."""

    subscriptions: list[JsonValue]


class _StoreDocument(BaseModel):
    """subscriptions.json as written."""

    version: Literal[1] = 1
    subscriptions: Sequence[Subscription]


class SubscriptionStore:
    """Every subscribed device in one 0600 JSON file, keyed by endpoint."""

    def __init__(self, state_dir: Path, *, clock: Clock = time.time) -> None:
        self._state_dir: Path = state_dir
        self._clock: Clock = clock
        # The routes and the notifier's pruning thread both rewrite the file from what they read.
        self._lock: threading.Lock = threading.Lock()

    @property
    def path(self) -> Path:
        """The store file; its directory is created 0700 on first use."""
        return statedir.ensure_dir(self._state_dir) / SUBSCRIPTIONS_FILENAME

    def list_all(self) -> list[Subscription]:
        """Every valid subscription, oldest first; a malformed entry is logged and dropped.

        Raises:
            SubscriptionError: the file is not JSON, or not an object with a subscriptions list.
            OSError: the file cannot be read.
        """
        path = self.path
        text = statedir.read_text(path) or ""
        try:
            entries = _StoreFile.model_validate_json(text).subscriptions if text.strip() else []
        except ValidationError as exc:
            raise SubscriptionError(f"{path}: {summary(exc)}") from exc
        out: list[Subscription] = []
        for index, entry in enumerate(entries):
            try:
                out.append(Subscription.model_validate(entry))
            except ValidationError as exc:
                log.warning("%s: dropping malformed entry %d: %s", path, index, summary(exc))
        return out

    def add(self, subscription: Subscription) -> Subscription:
        """Store a device; the same endpoint again updates its keys and keeps its stamp.

        Raises:
            SubscriptionError: the store already holds ``MAX_DEVICES`` other devices.
        """
        with self._lock:
            current = self.list_all()
            previous = next((one for one in current if one.endpoint == subscription.endpoint), None)
            if previous is None and len(current) >= MAX_DEVICES:
                raise SubscriptionError(
                    f"already {len(current)} devices, the most this server sends to; "
                    "remove one first"
                )
            stamp = int(self._clock()) if previous is None else previous.added_at
            stored = subscription.model_copy(update={"added_at": stamp})
            self._save([*(one for one in current if one.endpoint != stored.endpoint), stored])
        return stored

    def remove(self, endpoint: str) -> bool:
        """Forget one device; True when it was there."""
        with self._lock:
            current = self.list_all()
            kept = [one for one in current if one.endpoint != endpoint]
            if len(kept) == len(current):
                return False
            self._save(kept)
        return True

    def _save(self, subscriptions: Sequence[Subscription]) -> None:
        document = _StoreDocument(subscriptions=subscriptions)
        statedir.write_secret(self.path, document.model_dump_json(indent=2, by_alias=True) + "\n")


_LINKY_TLDS: Final = "com|net|org|io|dev|ai|co|me|app|sh|xyz|gg|to|ly"
# Schemes with no // that still navigate or execute.
_BARE_SCHEMES: Final = "data|javascript|vbscript|file|blob|tel|mailto|sms|intent|market"
# Each run is entered only at its start, so a scan is linear: one from every letter took 18 s on 20k.
_URL_RE: Final = re.compile(
    rf"""(?ix)
    (?:
        # A scheme, entered where its run starts; digits before the first letter go with it.
        (?<![a-z0-9+.\-]) (?P<scheme_lead>[+.\-]*+) (?:[0-9][0-9+.\-]*+)? [a-z][a-z0-9+.\-]*+ ://\S+
      | (?:{_BARE_SCHEMES}):\S*
      | www\.\S+
        # A host, entered where its run starts or after a "..", which no host crosses.
      | (?: (?<![a-z0-9.\-]) | (?<=\.\.) ) (?P<host_lead>(?: - | \.(?!\.) )*+)
        (?:
            www\.\S+
          | (?:{_BARE_SCHEMES}):\S*
          | [a-z0-9][a-z0-9\-]*+
            (?:
                (?:\.[a-z0-9\-]++)++ /\S*
              | (?:\.[a-z0-9\-]++)* \.(?:{_LINKY_TLDS})\b (?:[/?\#]\S*)?
            )
        )
    )
    """
)
# Text past this is never scanned: a lock screen shows a few hundred characters at most.
_MAX_SCANNED: Final = 4096
_INVISIBLE: Final = frozenset({"Cc", "Cf", "Cs", "Co"})


def _unlinked(found: re.Match[str]) -> str:
    # A run's leading punctuation was matched only so the scan could enter there.
    return (found["scheme_lead"] or found["host_lead"] or "") + LINK_PLACEHOLDER


def sanitise(text: str, *, limit: int) -> str:
    """Agent output reduced to what is safe on a lock screen: no escapes, controls or links."""
    cleaned = without_escapes(text)
    cleaned = "".join(" " if unicodedata.category(ch) in _INVISIBLE else ch for ch in cleaned)
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > _MAX_SCANNED:
        # A word cut at the cap may be half a link that no longer looks like one, so it goes.
        cleaned = cleaned[:_MAX_SCANNED].rsplit(" ", 1)[0] + " …"
    # An autolinked lock screen is a phishing primitive.
    cleaned = " ".join(_URL_RE.sub(_unlinked, cleaned).split())
    if len(cleaned) > limit:
        cleaned = cleaned[: max(limit - 1, 0)].rstrip() + "…"
    return cleaned


@dataclass(frozen=True, slots=True)
class Notice:
    """What one notification says, before sanitising; the agent is the session to deep-link."""

    event: PushEvent
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


class _SendOptions(TypedDict):
    vapid_private_key: str
    vapid_claims: dict[str, str | int]
    ttl: int
    timeout: float
    requests_session: requests.Session


class _WebPush(Protocol):
    def __call__(
        self,
        subscription_info: dict[str, str | dict[str, str]],
        data: str,
        **options: Unpack[_SendOptions],
    ) -> object: ...


# pywebpush's own signature leaves the subscription mapping's types unknown.
_webpush = cast("_WebPush", pywebpush.webpush)


class _NoRedirects(requests.Session):
    """A session that hands back a redirect as the answer instead of following it."""

    def get_redirect_target(self, resp: requests.Response) -> str | None:
        """Never a target: a push service has no reason to send the box elsewhere."""
        _ = resp
        return None


def _refusal(exc: Exception) -> DeliveryError:
    if not isinstance(exc, WebPushException):
        return DeliveryError(f"{type(exc).__name__}: {exc}")
    response = cast("requests.Response | None", exc.response)
    if response is None:
        return DeliveryError(str(exc), status=exc.status_code)
    # The body is the service's (or a redirect target's) text, so it never reaches the record.
    return DeliveryError(
        f"push service answered {response.status_code} {response.reason}",
        status=response.status_code,
    )


@dataclass(frozen=True, slots=True)
class PushLimits:
    """How long one send, and one whole fan-out, may hold the notifier."""

    # Seconds one send may wait to connect, and again to read; a stalled service must not hold it.
    send_timeout: float = 10.0
    # A fan-out that has spent this long skips its remaining devices, bounding the notifier's lock.
    fan_out_budget: float = 30.0


PUSH_LIMITS: Final = PushLimits()


@dataclass(frozen=True, slots=True)
class WebPushSender:
    """The real sender: pywebpush signed with this box's VAPID key."""

    keys: VapidKeys
    subject: str
    ttl: int
    timeout: float

    def __call__(self, subscription: Subscription, data: str) -> None:
        """Encrypt and post; a refusal becomes DeliveryError with the push service's status."""
        with _NoRedirects() as session:
            try:
                _ = _webpush(
                    subscription_info=subscription.push_info(),
                    data=data,
                    ttl=self.ttl,
                    vapid_private_key=self.keys.private_key,
                    vapid_claims={"sub": self.subject},
                    timeout=self.timeout,
                    requests_session=session,
                )
            except Exception as exc:
                raise _refusal(exc) from exc


def arm_sender(
    keys: VapidKeys, *, subject: str, ttl: int, limits: PushLimits = PUSH_LIMITS
) -> WebPushSender:
    """The sender, refused here where push is armed when its contact would be rejected."""
    return WebPushSender(
        keys=keys, subject=check_subject(subject), ttl=ttl, timeout=limits.send_timeout
    )


# Counters and stamps read back leniently: one damaged field costs that field, not the record.
type _Count = Annotated[StrictInt, or_default(0)]


def _rounded(at: float) -> float:
    return round(at, 3)


type _Stamp = Annotated[StrictFloat, or_default(0.0), PlainSerializer(_rounded)]


class DeliveryRecord(BaseModel):
    """Delivery counters as last written, for doctor and the settings sheet."""

    model_config = ConfigDict(frozen=True)

    version: Literal[1]
    sent: _Count = 0
    failed: _Count = 0
    pruned: _Count = 0
    quieted: _Count = 0
    last_send_at: _Stamp = 0.0
    last_error: Annotated[StrictStr, or_default("")] = ""
    last_error_at: _Stamp = 0.0
    last_quiet_at: _Stamp = 0.0
    updated_at: _Stamp = 0.0

    @property
    def never_delivered(self) -> bool:
        """Something tried and nothing has ever got through."""
        return self.sent == 0 and self.failed > 0


def _ago(at: float, now: float) -> str:
    # A zero stamp is the record's word for "this never happened".
    return ago(now - at) if at else "never"


def health_line(record: DeliveryRecord | None, *, now: float) -> str:
    """Delivery as doctor prints it: when push last sent, last failed, and last held one back."""
    if record is None:
        return "push: nothing delivered or attempted yet"
    error = _ago(record.last_error_at, now)
    if record.last_error_at:
        error += f" ({record.last_error})"
    return (
        f"push: last sent {_ago(record.last_send_at, now)} · last error {error} · "
        f"last quieted {_ago(record.last_quiet_at, now)}"
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
            return None if text is None else DeliveryRecord.model_validate_json(text)
        except (OSError, ValidationError):
            return None

    def sent(self) -> None:
        """One device accepted a push."""
        self._bump(lambda r, now: {"sent": r.sent + 1, "last_send_at": now})

    def failed(self, detail: str) -> None:
        """One push failed for a reason other than the device being gone."""
        self._bump(
            lambda r, now: {"failed": r.failed + 1, "last_error": detail, "last_error_at": now}
        )

    def pruned(self) -> None:
        """A device the push service retired was forgotten."""
        self._bump(lambda r, _now: {"pruned": r.pruned + 1})

    def quieted(self, count: int) -> None:
        """Changes a preference or the managed mark held back."""
        self._bump(lambda r, now: {"quieted": r.quieted + count, "last_quiet_at": now})

    def _bump(self, change: Callable[[DeliveryRecord, float], dict[str, object]]) -> None:
        now = self._clock()
        base = self._current or self.load() or DeliveryRecord(version=1)
        self._current = base.model_copy(update={**change(base, now), "updated_at": now})
        try:
            _ = statedir.ensure_dir(self.path.parent)
            statedir.write_secret(self.path, self._current.model_dump_json(indent=2) + "\n")
        except OSError as exc:
            # Health is for the reader; failing to write it must never stop a push.
            log.warning("cannot write %s: %s; doctor will see nothing", self.path, exc)


def _prune(store: SubscriptionStore, subscription: Subscription) -> None:
    try:
        _ = store.remove(subscription.endpoint)
    except (SubscriptionError, OSError) as exc:
        log.warning("could not prune a retired subscription: %s", exc)


@dataclass(frozen=True, slots=True)
class FanOut:
    """How one payload fared across every device."""

    sent: int = 0
    failed: int = 0
    pruned: int = 0


@dataclass(frozen=True, slots=True)
class Outlet:
    """Where a fan-out sends, and what it keeps: the sender, the devices, the health log."""

    deliver: Deliver
    store: SubscriptionStore
    health: DeliveryLog
    limits: PushLimits = PUSH_LIMITS
    # What the budget is measured on.
    clock: Clock = time.monotonic


def _one(data: str, subscription: Subscription, outlet: Outlet, *, now: float) -> str:
    if subscription.expired(now):
        # Dead by its own expirationTime, as a 404 or 410 would say after a wasted send.
        _prune(outlet.store, subscription)
        outlet.health.pruned()
        return "pruned"
    try:
        outlet.deliver(subscription, data)
    except DeliveryError as exc:
        if exc.gone:
            _prune(outlet.store, subscription)
            outlet.health.pruned()
            return "pruned"
        log.warning("push failed: %s", exc)
        outlet.health.failed(str(exc))
        return "failed"
    outlet.health.sent()
    return "sent"


def fan_out(
    data: str, subscriptions: Sequence[Subscription], outlet: Outlet, *, now: float
) -> FanOut:
    """Send to every device: prune the gone, record each outcome, never let one stop the rest.

    *now* is wall time from the caller's clock, for subscriptions that expire by their own date.
    Past the outlet's ``fan_out_budget`` seconds the devices not yet tried are skipped and counted failed.
    """
    outcomes: list[str] = []
    budget = outlet.limits.fan_out_budget
    started = outlet.clock()
    for index, subscription in enumerate(subscriptions):
        if outlet.clock() - started > budget:
            skipped = len(subscriptions) - index
            outlet.health.failed(f"fan-out over {budget:g}s: skipped {skipped} devices")
            outcomes += ["failed"] * skipped
            break
        outcomes.append(_one(data, subscription, outlet, now=now))
    return FanOut(
        sent=outcomes.count("sent"),
        failed=outcomes.count("failed"),
        pruned=outcomes.count("pruned"),
    )
