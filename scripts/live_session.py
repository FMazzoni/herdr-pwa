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
import tempfile
import termios
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

# Run as a file, so the repo root (where tests/ lives) is not on the path by itself.
ROOT: Final = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from herdr_pwa.engine import PANE_BUSY_CODE, EngineError, Frame, SocketTransport  # noqa: E402
from tests.waiting import GaveUpError, eventually, until  # noqa: E402

SESSION: Final = "hp-test"  # anything but this name is a foot-gun

COLS, ROWS = 200, 50  # wide enough that the TUI lays out real panes

# herdr's ids, HERDR_PWA_MANAGED (marks an agent watched), CLAUDE_CODE_CHILD_SESSION (no transcript).
INHERITED_PREFIXES: Final = ("HERDR_", "CLAUDE_CODE_")
# "Inside Claude Code" markers, the calling harness's name and the launching session's effort.
INHERITED_NAMES: Final = frozenset({"CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "AI_AGENT"})

# Claude Code's folder-trust menu lists "No, exit" first, under the cursor, so a bare Enter quits.
TRUST_YES: Final = "Yes, I trust this folder"
CURSOR: Final = "\u276f"

# How long `agent.start` waits for the agent to be ready: the herdr CLI's own default.
START_WAIT_MS: Final = 30_000
# Long enough for that wait and the reply after it.
CALL_SECONDS: Final = START_WAIT_MS / 1000 + 10
# How long a freshly split pane's shell may take to finish its startup files.
SHELL_SECONDS: Final = 15.0
# How long a stopped server may take to let go of its socket.
STOP_SECONDS: Final = 10.0
# Each look at the socket or the agents is a herdr call, so look less often than at a page.
LOOK_STEP: Final = 0.25
SETTLE_STEP: Final = 1.0


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


def clean_env(ambient: Mapping[str, str]) -> dict[str, str]:
    """The ambient environment minus every trace of the session it was launched from."""
    return {
        name: value
        for name, value in ambient.items()
        if name not in INHERITED_NAMES and not name.startswith(INHERITED_PREFIXES)
    }


def api(session: str, method: str, params: Frame | None = None, *, check: bool = True) -> Frame:
    """One request on *this* session's socket, through the app's own transport."""
    link = SocketTransport(socket_path(session), timeout=CALL_SECONDS)
    try:
        return link.call(method, params or {}).payload
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
        env=clean_env(os.environ),
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
            env=clean_env(os.environ),
        )
    if eventually(lambda: listening(path), timeout, step=LOOK_STEP):
        print(f"session {session!r} listening at {path}")
        return 0
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
    if eventually(lambda: not listening(path), STOP_SECONDS, step=LOOK_STEP):
        print(f"session {session!r} stopped")
        return 0
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


def split_request(panes: Sequence[Frame], cwd: str) -> Frame:
    """A new pane in ``cwd`` off the session's first, since agent.start takes no folder."""
    if not panes:
        raise SystemExit("the session has no panes at all; is it really up?")
    return {"target_pane_id": str(panes[0]["pane_id"]), "direction": "down", "cwd": cwd}


def fresh_pane(session: str, cwd: str) -> str:
    """A pane of its own at a shell prompt in ``cwd``; an idle one sits in another folder."""
    panes = result(api(session, "pane.list")).get("panes", [])
    fresh = result(api(session, "pane.split", split_request(panes, cwd)))
    return str(fresh["pane"]["pane_id"])


def trust_keys(text: str) -> list[str] | None:
    """The keys that move the trust menu's cursor to "Yes" and confirm it; None with no menu."""
    lines = text.splitlines()
    yes = next((i for i, line in enumerate(lines) if TRUST_YES in line), None)
    cursors = [i for i, line in enumerate(lines) if line.lstrip().startswith(CURSOR)]
    if yes is None or not cursors:
        return None
    # The menu's cursor is the one nearest its options; the input box has one too.
    at = min(cursors, key=lambda i: abs(i - yes))
    step = "Down" if yes > at else "Up"
    return [step] * abs(yes - at) + ["Enter"]


def pane_text(session: str, target: str) -> str:
    params: Frame = {"target": target, "source": "recent", "format": "text", "lines": 40}
    read = result(api(session, "agent.read", params)).get("read", {})
    return str(read.get("text", ""))


def session_uuid(session: str, target: str) -> str | None:
    """The session UUID the agent on ``target`` has registered, if it has one yet."""
    for agent in agents(session):
        if agent["pane_id"] == target:
            uuid = (agent.get("agent_session") or {}).get("value")
            return str(uuid) if uuid else None
    return None


def settle(session: str, target: str, timeout: float = 60.0) -> str | None:
    """Wait for a session UUID, answering the trust prompt once if that is the hold-up."""
    trusted = False

    def registered() -> str | None:
        nonlocal trusted
        uuid = session_uuid(session, target)
        keys = None if uuid or trusted else trust_keys(pane_text(session, target))
        if keys is not None:
            api(session, "agent.send_keys", {"target": target, "keys": keys}, check=False)
            trusted = True
        return uuid

    try:
        return until(registered, timeout, step=SETTLE_STEP)
    except GaveUpError:
        return None


def start(session: str, launch: Frame) -> dict[str, Any]:
    """agent.start, tried again while the new pane's shell is still starting up."""

    def attempt() -> dict[str, Any] | None:
        try:
            return result(api(session, "agent.start", launch))
        except EngineError as exc:
            # herdr refuses a shell that has not reached its prompt yet.
            if exc.code == PANE_BUSY_CODE:
                return None
            raise

    return until(attempt, SHELL_SECONDS, step=SETTLE_STEP)


@dataclass(frozen=True, slots=True)
class Launch:
    """Which agent to start: its name, its harness, the model it runs and the folder it runs in."""

    name: str
    kind: str
    model: str
    cwd: str


def agent(session: str, launch: Launch) -> int:
    name, kind, model, cwd = launch.name, launch.kind, launch.model, launch.cwd
    for existing in agents(session):
        if existing.get("name") == name:
            uuid = (existing.get("agent_session") or {}).get("value")
            print(f"{name} already running on {existing['pane_id']} (session {uuid})")
            return 0
    target = fresh_pane(session, cwd)
    print(f"starting {kind} {name!r} on {target} with --model {model} (cwd {cwd})")
    launch: Frame = {
        "name": name,
        "kind": kind,
        "pane_id": target,
        "args": ["--model", model],
        "timeout_ms": START_WAIT_MS,
    }
    started = start(session, launch)
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
    started.add_argument(
        "--cwd", help="folder the agent runs in (default: a fresh temporary folder, kept)"
    )
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
    cwd = args.cwd or tempfile.mkdtemp(prefix="hp-rig-")
    return agent(args.session, Launch(args.name, args.kind, args.model, str(Path(cwd).resolve())))


if __name__ == "__main__":
    raise SystemExit(main())
