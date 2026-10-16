#!/usr/bin/env python3
"""Stand up, inspect and tear down the disposable hp-test herdr session; never the default one."""

from __future__ import annotations

import argparse
import fcntl
import os
import pty
import socket
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path
from typing import Any

from herdr_pwa.engine.transport import EngineError, Frame, SocketTransport

SESSION = "hp-test"  # anything but this name is a foot-gun

COLS, ROWS = 200, 50  # wide enough that the TUI lays out real panes

INHERITED = (
    "HERDR_ENV",
    "HERDR_SOCKET_PATH",
    "HERDR_PANE_ID",
    "HERDR_TAB_ID",
    "HERDR_WORKSPACE_ID",
)  # the variables that tell a process it is already inside a session

# How long `agent.start` waits for the agent to be ready: the herdr CLI's own default.
START_WAIT_MS = 30_000
# Long enough for that wait and the reply after it.
CALL_SECONDS = START_WAIT_MS / 1000 + 10


def session_dir(session: str) -> Path:
    return Path.home() / ".config" / "herdr" / "sessions" / session


def socket_path(session: str) -> Path:
    return session_dir(session) / "herdr.sock"


def listening(path: Path) -> bool:
    """Is a server actually answering there, rather than a file being present?"""
    if not path.exists():
        return False
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(1.0)
    try:
        probe.connect(str(path))
    except OSError:
        return False
    finally:
        probe.close()
    return True


def clean_env() -> dict[str, str]:
    """The ambient environment minus every trace of the current session."""
    env = {k: v for k, v in os.environ.items() if k not in INHERITED}
    env.pop("HERDR_SESSION", None)
    return env


def api(session: str, method: str, params: Frame | None = None, *, check: bool = True) -> Frame:
    """One request on *this* session's socket, through the app's own transport."""
    link = SocketTransport(socket_path(session), timeout=CALL_SECONDS)
    try:
        return link.call(method, params or {})
    except EngineError:
        if check:
            raise
        return {}
    finally:
        link.close()


def result(payload: Frame) -> dict[str, Any]:
    # An error frame never gets here: the transport raises it as an EngineError.
    return payload.get("result", payload)


# ---------------------------------------------------------------------------
# up / down / status
# ---------------------------------------------------------------------------


def supervise(session: str) -> int:
    """Own a pty, run the TUI on it, and drain it forever. Never returns early."""
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
    child = subprocess.Popen(
        ["herdr", "--session", session],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=clean_env(),
        start_new_session=True,
        close_fds=True,
    )
    os.close(slave)
    try:
        while True:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            # Drained, not kept: what the TUI draws is of no interest, but a
            # pty nobody reads fills up and blocks the server behind it.
    finally:
        os.close(master)
    return child.wait()


def up(session: str, timeout: float = 30.0) -> int:
    path = socket_path(session)
    if listening(path):
        print(f"session {session!r} already listening at {path}")
        return 0
    session_dir(session).mkdir(parents=True, exist_ok=True)
    log = session_dir(session) / "supervisor.log"
    with log.open("ab") as handle:
        subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--session",
                session,
                "supervise",
            ],
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=handle,
            start_new_session=True,
            env=clean_env(),
        )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if listening(path):
            print(f"session {session!r} listening at {path}")
            return 0
        time.sleep(0.25)
    print(
        f"session {session!r} did not come up within {timeout}s; see {log}",
        file=sys.stderr,
    )
    return 1


def down(session: str) -> int:
    path = socket_path(session)
    if not listening(path):
        print(f"session {session!r} is not running")
        return 0
    # The server may hang up instead of answering a stop, which is the stop working.
    api(session, "server.stop", check=False)
    for _ in range(40):
        if not listening(path):
            print(f"session {session!r} stopped")
            return 0
        time.sleep(0.25)
    print(f"session {session!r} did not stop", file=sys.stderr)
    return 1


def agents(session: str) -> list[dict[str, Any]]:
    return result(api(session, "agent.list")).get("agents", [])


def status(session: str) -> int:
    path = socket_path(session)
    if not listening(path):
        print(f"session {session!r}: not running ({path})")
        return 1
    print(f"session {session!r}: listening at {path}")
    for agent in agents(session):
        uuid = (agent.get("agent_session") or {}).get("value", "-- none yet --")
        print(
            f"  {agent['pane_id']}  {agent.get('agent')}  {agent.get('name')}  "
            f"status={agent.get('agent_status')}  session={uuid}"
        )
    if not agents(session):
        print("  (no agents)")
    return 0


# ---------------------------------------------------------------------------
# the agent
# ---------------------------------------------------------------------------


def free_pane(session: str, cwd: str) -> str:
    """A pane at a shell prompt, splitting one off if every pane is taken."""
    panes = result(api(session, "pane.list")).get("panes", [])
    for pane in panes:
        if not pane.get("agent"):
            return str(pane["pane_id"])
    if not panes:
        raise SystemExit("the session has no panes at all; is it really up?")
    split = {"target_pane_id": str(panes[0]["pane_id"]), "direction": "down", "cwd": cwd}
    fresh = result(api(session, "pane.split", split))
    return str(fresh["pane"]["pane_id"])


def pane_text(session: str, target: str) -> str:
    params: Frame = {"target": target, "source": "recent", "format": "text", "lines": 40}
    read = result(api(session, "agent.read", params)).get("read", {})
    return str(read.get("text", ""))


def settle(session: str, target: str, timeout: float = 60.0) -> str | None:
    """Wait for a session UUID, answering the trust prompt if that is the hold-up."""
    deadline = time.monotonic() + timeout
    trusted = False
    while time.monotonic() < deadline:
        for agent in agents(session):
            if agent["pane_id"] == target:
                uuid = (agent.get("agent_session") or {}).get("value")
                if uuid:
                    return str(uuid)
        if not trusted and "trust this folder" in pane_text(session, target):
            api(session, "agent.send_keys", {"target": target, "keys": ["Enter"]}, check=False)
            trusted = True
        time.sleep(1.0)
    return None


def agent(session: str, name: str, kind: str, model: str, cwd: str) -> int:
    for existing in agents(session):
        if existing.get("name") == name:
            uuid = (existing.get("agent_session") or {}).get("value")
            print(f"{name} already running on {existing['pane_id']} (session {uuid})")
            return 0
    target = free_pane(session, cwd)
    print(f"starting {kind} {name!r} on {target} with --model {model} (cwd {cwd})")
    launch: Frame = {
        "name": name,
        "kind": kind,
        "pane_id": target,
        "args": ["--model", model],
        "timeout_ms": START_WAIT_MS,
    }
    started = result(api(session, "agent.start", launch))
    # herdr echoes the argv it actually ran, which is the only honest proof
    # that the model argument reached the agent binary.
    print(f"argv: {started.get('argv')}")
    uuid = settle(session, target)
    if uuid is None:
        print(
            f"{name} started but never registered a session UUID; it will not be "
            "drillable in the app. Read the pane and see what it is waiting on.",
            file=sys.stderr,
        )
        return 1
    print(f"{name} ready on {target}, session {uuid}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--session", default=SESSION, help=f"session name (default {SESSION})")
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("up", help="start the session's server if it is not already up")
    subs.add_parser("down", help="stop the session's server")
    subs.add_parser("status", help="print the socket and every agent in the session")
    subs.add_parser("supervise", help=argparse.SUPPRESS)
    started = subs.add_parser("agent", help="start a cheap claude agent in the session")
    started.add_argument("--name", default="probe-claude")
    started.add_argument("--kind", default="claude")
    started.add_argument("--model", default="haiku")
    started.add_argument("--cwd", default=str(Path.cwd()))
    args = parser.parse_args(argv)

    if args.session in {"", "default"}:
        raise SystemExit("refusing to touch the default session")

    if args.command == "up":
        return up(args.session)
    if args.command == "down":
        return down(args.session)
    if args.command == "status":
        return status(args.session)
    if args.command == "supervise":
        return supervise(args.session)
    return agent(args.session, args.name, args.kind, args.model, args.cwd)


if __name__ == "__main__":
    raise SystemExit(main())
