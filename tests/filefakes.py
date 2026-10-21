"""A small, realistic git repository with a change in flight, for the file explorer's tests."""

import base64
import os
import subprocess
from pathlib import Path
from typing import Final

from pydantic import JsonValue

from tests.webfakes import WORKING_PANE, agents_of

# A 1x1 PNG: the smallest image whose magic number and type agree.
PNG: Final = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
SERVER: Final = '''"""A tiny HTTP service that answers health checks and echoes a greeting."""

import json
import logging
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer

log = logging.getLogger(__name__)
PORT = 8080


@dataclass(frozen=True)
class Greeting:
    """What the service says, and to whom."""

    name: str
    excited: bool = False

    def render(self) -> str:
        mark = "!" if self.excited else "."
        return f"Hello, {self.name}{mark}"


class Handler(BaseHTTPRequestHandler):
    """Routes GET requests to the two endpoints the service has."""

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._reply(200, {"status": "ok"})
        elif self.path.startswith("/hello/"):
            name = self.path.removeprefix("/hello/") or "world"
            self._reply(200, {"greeting": Greeting(name).render()})
        else:
            self._reply(404, {"error": "no such route"})

    def _reply(self, status: int, body: dict[str, str]) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

'''
FILLER: Final = "".join(
    f'\ndef step_{n}(value: int) -> int:\n    """Step {n} of the pipeline."""\n'
    f"    return value * {n} + {n % 7}\n"
    for n in range(1, 40)
)
MAIN: Final = """

def main() -> None:
    logging.basicConfig(level=logging.INFO)
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    log.info("listening on %s", PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
"""
GUIDE: Final = """# Operator guide

The service answers two routes. Start it with `python -m app.server` and check
`src/app/server.py:24` for the routing.

| Route | Answer |
|---|---|
| `/healthz` | `{"status": "ok"}` |
| `/hello/<name>` | a greeting |

## Deploying

- [x] build the image
- [ ] roll it out to the edge

```python
from app.server import Greeting
print(Greeting("ops", excited=True).render())
```
"""
README: Final = "# demo-service\n\nA tiny service for the explorer's fixtures.\n"
LOG_LINES: Final = 12_000
MINIFIED: Final = (
    "var a=" + ",".join(f"f{i}=function(x){{return x*{i}}}" for i in range(6000)) + ";\n"
)


def git(repo: Path, *args: str) -> bytes:
    """Run one git command in ``repo`` and return its output, failing the test if it fails."""
    command = ["git", "-C", str(repo), *args]
    return subprocess.run(command, check=True, capture_output=True).stdout  # noqa: S603 - a fixed git or python command line, never shell input


def _write(repo: Path, rel: str, body: str | bytes) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(body, bytes):
        path.write_bytes(body)
    else:
        path.write_text(body)


def make_repo(repo: Path) -> Path:
    """A committed repository, then a working tree with a modification, a deletion and a new file."""
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "fixture@example.com")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "commit.gpgsign", "false")
    committed: dict[str, str | bytes] = {
        "README.md": README,
        "src/app/__init__.py": "",
        "src/app/server.py": SERVER + FILLER + MAIN,
        "src/app/legacy.py": "OLD = True\n",
        "docs/guide.md": GUIDE,
        "assets/logo.png": PNG,
        "logs/big.log": "".join(
            f"2026-10-10T10:{n // 60 % 60:02d}:{n % 60:02d} line {n} ok\n"
            for n in range(1, LOG_LINES + 1)
        ),
        "dist/bundle.min.js": MINIFIED,
        "data/blob.bin": b"\x00\x01\x02binary\x00" * 64,
        "config/.env": "API_TOKEN=do-not-show-me\n",
        "config/deploy.pem": "synthetic key material, not-a-real-key\n",
        ".gitignore": "build/\n",
    }
    for rel, body in committed.items():
        _write(repo, rel, body)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "first")
    server = (SERVER + FILLER + MAIN).replace(
        '            self._reply(404, {"error": "no such route"})',
        '            log.warning("no route for %s", self.path)\n'
        '            self._reply(404, {"error": f"no such route: {self.path}"})',
    )
    _write(repo, "src/app/server.py", server.replace("PORT = 8080", "PORT = 8088"))
    (repo / "src/app/legacy.py").unlink()
    _write(
        repo,
        "src/app/metrics.py",
        "COUNTS: dict[str, int] = {}\n\n\ndef count(name: str) -> None:\n    COUNTS[name] = COUNTS.get(name, 0) + 1\n",
    )
    _write(repo, "build/out.txt", "ignored output\n")
    return repo.resolve()


def stand_in(
    frame: dict[str, JsonValue], repo: Path, pane: str = WORKING_PANE
) -> dict[str, JsonValue]:
    """The snapshot frame with the agent at ``pane`` working inside ``repo``."""
    for agent in agents_of(frame):
        if agent.get("pane_id") == pane:
            agent["cwd"] = str(repo)
            agent["foreground_cwd"] = str(repo)
    return frame


def booby_trap(repo: Path, marks: Path) -> None:
    """Point every command a repository's config can make git run at a script leaving a mark."""
    marks.mkdir(parents=True, exist_ok=True)
    script = repo.parent / f"{repo.name}-trap.sh"
    # It leaves a file named for what ran it, then copies stdin through, as a filter must.
    script.write_text(f'#!/bin/sh\ntouch "{marks}/$1"\ncat\n')
    script.chmod(0o755)
    hooks = repo / ".git" / "trap-hooks"
    hooks.mkdir()
    for hook in ("post-index-change", "reference-transaction", "post-checkout"):
        (hooks / hook).write_text(f'#!/bin/sh\ntouch "{marks}/hook-{hook}"\n')
        (hooks / hook).chmod(0o755)
    for key, value in (
        ("core.fsmonitor", f"{script} fsmonitor"),
        ("core.hooksPath", str(hooks)),
        ("core.pager", f"{script} pager"),
        ("diff.external", f"{script} external"),
        ("diff.trap.textconv", f"{script} textconv"),
        ("diff.trap.command", f"{script} diffcommand"),
        ("filter.trap.clean", f"{script} clean"),
        ("filter.trap.process", f"{script} process"),
        ("filter.trap.required", "true"),
    ):
        git(repo, "config", key, value)
    (repo / ".gitattributes").write_text("*.py diff=trap filter=trap\n*.md diff=trap\n")
    # Where an agent could also name a driver, outside the work tree.
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text("*.md filter=trap\n")


def trapped_submodule(repo: Path, marks: Path) -> str:
    """Add a submodule whose own config names a filter, with a same-size edit for it to clean."""
    marks.mkdir(parents=True, exist_ok=True)
    inner = repo.parent / f"{repo.name}-inner"
    inner.mkdir()
    git(inner, "init", "-q", "-b", "main")
    _write(inner, "notes.txt", "hello\n")
    _write(inner, ".gitattributes", "* filter=evil\n")
    git(inner, "add", "-A")
    git(inner, "-c", "user.email=f@example.com", "-c", "user.name=F", "commit", "-q", "-m", "x")
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(inner), "vendor")
    git(repo, "commit", "-q", "-m", "vendor")
    # The submodule's own config, which the superproject's reads never see.
    git(repo / "vendor", "config", "filter.evil.clean", f'touch "{marks}/submodule-clean"; cat')
    git(repo / "vendor", "config", "filter.evil.process", f'touch "{marks}/submodule-process"')
    # Same size, so only hashing the content (through the filter) can tell it changed.
    path = repo / "vendor" / "notes.txt"
    stamp = path.stat().st_mtime_ns
    path.write_text("jello\n")
    os.utime(path, ns=(stamp + 2_000_000_000, stamp + 2_000_000_000))
    return "vendor/notes.txt"
