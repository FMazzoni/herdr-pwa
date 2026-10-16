"""Pairing: publish this app behind ``tailscale serve`` and show its https URL as a QR.

There is no token; the URL is safe to screenshot because the tailnet is the boundary.
"""

import io
import json
from dataclasses import dataclass, field, replace
from http import HTTPStatus

import segno
from pydantic import JsonValue, ValidationError

from herdr_pwa import tailscale
from herdr_pwa.tailscale import ServeConfig
from herdr_pwa.validation import is_not_json

HTTPS_PORT = 443
# Narrower than 'tailscale serve reset', which would take every other handler offline too.
TEARDOWN_COMMAND = f"tailscale serve --https={HTTPS_PORT} off"
TAILNET_IS_THE_BOUNDARY = (
    "The URL is not a secret; the tailnet is the boundary: only a device signed into your "
    "tailnet can reach it."
)
_FUNNEL = "funnel"


# tailscaled's own failures stay tailscale.LocalApiError, worded where tailscaled is asked.
class PairingError(RuntimeError):
    """A pairing precondition failed; the message says what to do about it."""


@dataclass(frozen=True, slots=True)
class ServeState:
    """What this node serves, measured against the handler this app wants at ``/``."""

    host: str
    target: str
    published: bool
    configured: bool
    occupied_by: str
    funnel: bool
    changed: bool
    etag: str
    config: ServeConfig = field(default_factory=dict[str, JsonValue])


@dataclass(frozen=True, slots=True)
class Pairing:
    """The outcome of a pairing run: the URL to encode, and what serve now looks like."""

    url: str
    state: ServeState
    served: bool
    note: str


def pair_url(host: str) -> str:
    """The https URL for this node; a secure context is what service workers and push need."""
    hostname = host.strip().rstrip(".")
    if not hostname:
        raise PairingError("Cannot build a pairing URL: the host name is empty.")
    return f"https://{hostname}/"


def qr_text(url: str) -> str:
    """The URL as a terminal QR code; refuses anything that is not https."""
    clean = url.strip()
    if not clean.startswith("https://") or clean == "https://":
        raise PairingError(f"Refusing to render a QR code for a non-https URL: {url!r}.")
    out = io.StringIO()
    segno.make(clean).terminal(out=out, compact=True)
    return out.getvalue()


def serve_state(
    api: tailscale.LocalApi, *, port: int, host: str, bind_host: str = "127.0.0.1"
) -> ServeState:
    """Read the live serve config and say whether this app's handler is already there."""
    reply = api.read(tailscale.SERVE_CONFIG_PATH)
    config = _parse_config(reply.body)
    name = host.strip().rstrip(".")
    target = f"http://{bind_host}:{port}"
    existing = _root_proxy(config, name)
    ours = _same_target(existing, target)
    return ServeState(
        host=name,
        target=target,
        published=ours,
        configured=tailscale.is_serving(config),
        occupied_by="" if ours else existing,
        funnel=_funnel_enabled(config),
        changed=False,
        etag=reply.etag,
        config=config,
    )


def publish(api: tailscale.LocalApi, state: ServeState) -> ServeState:
    """Add this app's handler, or leave a matching config alone; running twice writes once."""
    if state.published:
        return state
    if state.funnel:
        raise PairingError(
            "This node has Funnel switched on, which publishes it to the open internet; "
            "herdr-pwa will not add itself to such a config. Turn Funnel off, then re-run."
        )
    if state.occupied_by:
        raise PairingError(
            f"https://{state.host}/ already proxies to {state.occupied_by}, not {state.target}. "
            f"Refusing to take it over. Use that port, or '{TEARDOWN_COMMAND}' if it is stale."
        )
    _write_serve_config(api, _with_our_handler(state), etag=state.etag)
    return replace(state, published=True, configured=True, changed=True)


def pair(
    api: tailscale.LocalApi, *, port: int, bind_host: str = "127.0.0.1", write: bool = True
) -> Pairing:
    """Discover the name, publish if asked, and only then hand back a URL to encode."""
    host = tailscale.magicdns(api)
    url = pair_url(host)
    state = serve_state(api, port=port, host=host, bind_host=bind_host)
    if state.published:
        return Pairing(url, state, served=True, note=f"Found {state.target} already served.")
    if not write:
        return Pairing(
            url, state, served=False, note=f"Nothing serves {state.target}; nothing changed."
        )
    done = publish(api, state)
    note = f"Published https://{done.host}/ -> {done.target}. Take it down with: {TEARDOWN_COMMAND}"
    return Pairing(url, done, served=True, note=note)


def _parse_config(raw: str) -> ServeConfig:
    try:
        return tailscale.serve_config(raw)
    except ValidationError as exc:
        shape = "JSON" if is_not_json(exc) else "a JSON object"
        raise PairingError(f"tailscaled's serve config was not {shape}.") from exc


def _site(host: str) -> str:
    return f"{host}:{HTTPS_PORT}"


def _root_proxy(config: ServeConfig, host: str) -> str:
    root = tailscale.root_handler(config, _site(host))
    if root is None:
        return ""
    proxy = root.get("Proxy")
    if isinstance(proxy, str) and proxy.strip():
        return proxy.strip()
    return f"a non-proxy handler ({', '.join(sorted(root)) or 'empty'})"


def _funnel_enabled(value: JsonValue) -> bool:
    if isinstance(value, dict):
        return any(
            (_FUNNEL in key.casefold() and _carries_truth(item)) or _funnel_enabled(item)
            for key, item in value.items()
        )
    return isinstance(value, list) and any(_funnel_enabled(item) for item in value)


def _carries_truth(value: JsonValue) -> bool:
    # An AllowFunnel map of all-false is Funnel switched off again.
    if isinstance(value, dict):
        return any(_carries_truth(item) for item in value.values())
    if isinstance(value, list):
        return any(_carries_truth(item) for item in value)
    return bool(value)


def _same_target(existing: str, target: str) -> bool:
    return bool(existing) and _normalised(existing) == _normalised(target)


def _normalised(target: str) -> str:
    text = target.strip().casefold().removeprefix("http://").rstrip("/")
    return text.replace("localhost:", "127.0.0.1:")


def _copied(value: JsonValue) -> dict[str, JsonValue]:
    return dict(value) if isinstance(value, dict) else {}


def _with_our_handler(state: ServeState) -> ServeConfig:
    # Copied at every level touched, so the state's own config is never mutated.
    merged = {key: value for key, value in state.config.items() if _FUNNEL not in key.casefold()}
    tcp = _copied(merged.get("TCP"))
    tcp[str(HTTPS_PORT)] = {"HTTPS": True}
    web = _copied(merged.get("Web"))
    site = _copied(web.get(_site(state.host)))
    handlers = _copied(site.get("Handlers"))
    handlers["/"] = {"Proxy": state.target}
    site["Handlers"] = handlers
    web[_site(state.host)] = site
    merged["TCP"] = tcp
    merged["Web"] = web
    return merged


def _write_serve_config(api: tailscale.LocalApi, config: ServeConfig, *, etag: str) -> None:
    # The one write this package makes: serve only, never Funnel, method and path literal.
    body = json.dumps(config)
    if _FUNNEL in body.casefold():
        raise PairingError("Refusing to send a serve config that mentions Funnel.")
    reply = api.call(
        "POST",
        "/localapi/v0/serve-config",
        body=body,
        # Compare-and-swap: a config changed since it was read answers 412, unwritten.
        headers={"If-Match": etag, "Content-Type": "application/json"},
    )
    if reply.status != HTTPStatus.OK:
        raise PairingError(_write_refusal(reply))


def _write_refusal(reply: tailscale.Reply) -> str:
    detail = reply.body.strip()
    hint = f" Details: {detail[:200]}" if detail else ""
    if reply.status == HTTPStatus.PRECONDITION_FAILED:
        return f"The serve config changed while pairing ran, so nothing was written. Re-run.{hint}"
    if reply.status in tailscale.REFUSED:
        return (
            f"tailscaled refused to change the serve config (HTTP {reply.status}). Run "
            "'sudo tailscale set --operator=<your-username>' with a literal name, because an "
            f"unexpanded '$USER' is stored verbatim and never matches.{hint}"
        )
    return f"tailscaled answered HTTP {reply.status} to the serve config write; nothing published.{hint}"
