"""Headless Firefox spoken to over Marionette, its own protocol, so no driver is installed."""

import configparser
import contextlib
import json
import shutil
import socket
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pydantic import JsonValue, TypeAdapter

from tests.waiting import eventually

BINARY = "firefox"
# A popup has no toolbar, so it can be narrower than the 500px a normal window stops at.
OPENER_WIDTH = 600
CONNECT_SECONDS = 60.0
# Each look at the port is a fresh connect, so look less often than at a page.
CONNECT_STEP = 0.25
POPUP_SECONDS = 10.0

PREFS = {
    "browser.shell.checkDefaultBrowser": "false",
    "datareporting.policy.dataSubmissionEnabled": "false",
    # Phones draw overlay scrollbars, so a classic one must not eat the measured width.
    "ui.useOverlayScrollbars": "1",
    "dom.disable_open_during_load": "false",
    # Under 5% or 200MB free, Firefox's watcher sends memory-pressure, which empties the bfcache.
    "browser.low_commit_space_threshold_percent": "0",
    "browser.low_commit_space_threshold_mb": "0",
}


# Marionette's frames are its own JSON; nothing here narrows them further than JSON.
_MESSAGE = TypeAdapter(JsonValue)


class BrowserError(RuntimeError):
    """Headless Firefox could not be made to do what it was asked."""


class Marionette:
    """Length-prefixed JSON over loopback: ``len:[0, id, command, params]``."""

    def __init__(self, port: int) -> None:
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=60)
        self.buffer = b""
        self.next_id = 1
        self._receive()
        self.send("WebDriver:NewSession", {})

    def _read_some(self) -> bytes:
        chunk = self.sock.recv(65536)
        if not chunk:
            raise BrowserError("Firefox closed the Marionette connection")
        return chunk

    def _receive(self) -> object:
        while b":" not in self.buffer:
            self.buffer += self._read_some()
        header, _, rest = self.buffer.partition(b":")
        size = int(header)
        while len(rest) < size:
            rest += self._read_some()
        self.buffer = rest[size:]
        return _MESSAGE.validate_json(rest[:size])

    def send(self, command: str, params: dict[str, object]) -> object:
        """Send one command and wait for the reply carrying its id."""
        message_id = self.next_id
        self.next_id += 1
        payload = json.dumps([0, message_id, command, params]).encode()
        self.sock.sendall(str(len(payload)).encode() + b":" + payload)
        while True:
            message = self._receive()
            if isinstance(message, list) and message[:2] == [1, message_id]:
                if message[2]:
                    raise BrowserError(f"{command} failed: {message[2]}")
                return message[3]

    def script(self, body: str, *args: object) -> object:
        """Run JavaScript in the current window and return its value."""
        answer = self.send("WebDriver:ExecuteScript", {"script": body, "args": list(args)})
        return answer.get("value") if isinstance(answer, dict) else answer

    def script_async(self, body: str, *args: object) -> object:
        """Run JavaScript that hands its value to its last argument, and wait for that call."""
        answer = self.send("WebDriver:ExecuteAsyncScript", {"script": body, "args": list(args)})
        return answer.get("value") if isinstance(answer, dict) else answer

    def handles(self) -> list[str]:
        """Every open top-level window."""
        found = self.send("WebDriver:GetWindowHandles", {})
        values = found.get("value", found) if isinstance(found, dict) else found
        return [str(v) for v in values] if isinstance(values, list) else []

    def close(self) -> None:
        """Ask Firefox to quit and drop the socket."""
        with contextlib.suppress(BrowserError, OSError):
            self.send("Marionette:Quit", {})
        self.sock.close()


def free_port() -> int:
    """A loopback port the kernel just handed out, so no fixed port is ever claimed."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def available() -> bool:
    """Whether the browser is on PATH at all."""
    return shutil.which(BINARY) is not None


def version() -> str:
    """The browser's own version line, which changes layout as surely as a CSS edit does."""
    found = shutil.which(BINARY)
    if found is None:
        raise BrowserError(f"{BINARY} is not on PATH")
    # Where `firefox --version` reads it from: the install's application.ini, beside the real binary.
    ini = Path(found).resolve().parent / "application.ini"
    parser = configparser.ConfigParser(interpolation=None)
    if not parser.read(ini, encoding="utf-8") or not parser.has_section("App"):
        raise BrowserError(f"no [App] section in {ini}, so the browser version is unknown")
    app = parser["App"]
    return f"{app.get('Vendor', '')} {app.get('Name', '')} {app.get('Version', '')}".strip()


def _connect(port: int, process: subprocess.Popen[bytes]) -> Marionette:
    driver: Marionette | None = None

    def opened() -> bool:
        nonlocal driver
        with contextlib.suppress(OSError):
            driver = Marionette(port)
        # A browser that has exited will never open the port, so stop looking.
        return driver is not None or process.poll() is not None

    if not eventually(opened, CONNECT_SECONDS, step=CONNECT_STEP) or driver is None:
        raise BrowserError("Firefox never opened its Marionette port")
    return driver


@contextmanager
def browser() -> Iterator[Marionette]:
    """Headless Firefox on a throwaway profile, quit and deleted afterwards."""
    with tempfile.TemporaryDirectory(prefix="hp-geometry-") as directory:
        profile = Path(directory)
        port = free_port()
        prefs = {"marionette.port": str(port), **PREFS}
        lines = (f'user_pref("{key}", {value});' for key, value in prefs.items())
        (profile / "user.js").write_text("\n".join(lines) + "\n", encoding="utf-8")
        process = subprocess.Popen(
            [BINARY, "--headless", "--profile", str(profile), "--marionette", "about:blank"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            driver = _connect(port, process)
            try:
                yield driver
            finally:
                driver.close()
        finally:
            process.terminate()
            process.wait(timeout=30)


def phone_window(driver: Marionette, width: int, height: int) -> str:
    """Open a toolbar-less popup of exactly ``width`` CSS pixels and switch into it."""
    driver.send("WebDriver:SetWindowRect", {"width": OPENER_WIDTH, "height": height + 200})
    before = set(driver.handles())
    driver.script(
        "window.open('about:blank', '_blank', `popup,width=${arguments[0]},height=${arguments[1]}`);",
        width,
        height,
    )
    opened: set[str] = set()

    def appeared() -> bool:
        nonlocal opened
        opened = set(driver.handles()) - before
        return bool(opened)

    if not eventually(appeared, POPUP_SECONDS):
        raise BrowserError("the popup window never opened")
    handle = opened.pop()
    driver.send("WebDriver:SwitchToWindow", {"handle": handle})
    inner = driver.script("return window.innerWidth;")
    if inner != width:
        raise BrowserError(f"asked for a {width}px window and got {inner}px")
    return handle
