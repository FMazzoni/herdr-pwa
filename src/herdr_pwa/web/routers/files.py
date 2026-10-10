"""The file explorer: what changed in a root, its tree, go-to-file, one file and one diff. GET only."""

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Final
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from herdr_pwa.engine.model import Agent, as_session_id
from herdr_pwa.files import diffs, fuzzy
from herdr_pwa.files.git import Git, Status
from herdr_pwa.files.reader import LocalReader, NotReadable, OutsideRoot, inside
from herdr_pwa.files.roots import (
    ROOT_ID,
    Fence,
    Root,
    UnknownRoot,
    discover,
    is_secret,
    relative,
    resolve,
)
from herdr_pwa.files.view import (
    IMAGE_BYTES,
    PAGE_LINES,
    SNIFF_BYTES,
    FileView,
    Viewer,
    human_size,
    image_type,
)
from herdr_pwa.web.deps import WebState, hidden_tick, viewer_guard
from herdr_pwa.web.herd import find_agent

# An open file is re-checked on disk at the feed's quiet cadence.
WATCH_CADENCE: Final = 10
VIEWS: Final = ("changes", "tree", "find")
NO_STORE: Final = {"Cache-Control": "no-store"}

type From = Annotated[str, Query(alias="from")]


@dataclass(frozen=True, slots=True)
class Nav:
    """Builds every link inside one root, carrying the agent page the reader came from."""

    root: str
    back: str = ""

    def href(self, view: str = "", path: str = "", **extra: str | int) -> str:
        """The URL of ``view`` (changes when empty) at ``path``, with ``extra`` query values."""
        params: dict[str, str | int] = {"path": path} if path else {}
        params.update({k: v for k, v in extra.items() if v})
        if self.back:
            params["from"] = self.back
        tail = f"/{view}" if view else ""
        query = f"?{urlencode(params)}" if params else ""
        return f"/files/{self.root}{tail}{query}"

    def crumbs(self, rel: str) -> list[tuple[str, str]]:
        """Each folder above and including ``rel``, as (name, tree link)."""
        parts = [p for p in rel.split("/") if p]
        return [(p, self.href("tree", "/".join(parts[: i + 1]))) for i, p in enumerate(parts)]


def _back(session: str) -> str:
    try:
        return as_session_id(session) if session else ""
    except ValueError:
        return ""


@dataclass
class Explorer:
    """The explorer's state: one git reader, one viewer, and the fence around every root."""

    web: WebState
    git: Git
    viewer: Viewer
    fence: Fence

    def roots(self) -> tuple[Root, ...]:
        """Every root the herd stands in now."""
        agents = self.web.snapshot().value.agents
        return discover(((a.cwd, a.session) for a in agents if a.cwd), self.git, self.fence)

    def root(self, root_id: str) -> Root:
        """The root with this id, or UnknownRoot."""
        if ROOT_ID.fullmatch(root_id):
            for root in self.roots():
                if root.id == root_id:
                    return root
        raise UnknownRoot(root_id)

    def page(self, name: str, root: Root, nav: Nav, view: str, **context: object) -> HTMLResponse:
        """One explorer page, with the bar, the root's status and its links."""
        shared = {
            "root": root,
            "nav": nav,
            "view": view,
            "views": VIEWS,
            "status": self.git.status(root.path),
            "back": f"/agent/{nav.back}" if nav.back else "/",
            "size": human_size,
            "page_lines": PAGE_LINES,
        }
        return self.web.pages.page(name, {**shared, **context})


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303, headers=NO_STORE)


def _children(
    explorer: Explorer, root: Root, real: Path, rel: str, *, everything: bool
) -> list[tuple[str, bool, int]]:
    listing = LocalReader(root.path).list_dir(real)
    entries = [(e.name, e.is_dir, e.size) for e in listing.entries]
    if everything:
        return entries
    prefix = f"{rel}/" if rel else ""
    known = {
        p[len(prefix) :].split("/", 1)[0]
        for p in explorer.git.files(root.path)
        if p.startswith(prefix)
    }
    return [e for e in entries if e[0] in known]


def _diff(explorer: Explorer, root: Root, real: Path, rel: str, status: Status) -> diffs.FileDiff:
    change = status.of(rel)
    if change is not None and change.kind == "new":
        raw = LocalReader(root.path).read_range(real, 0, IMAGE_BYTES)
        if b"\0" in raw[:SNIFF_BYTES]:
            return diffs.FileDiff((), note=diffs.BINARY)
        return diffs.added(raw.decode("utf-8", "replace"))
    found = explorer.git.diff(root.path, rel)
    return (
        diffs.parse(found)
        if found is not None
        else diffs.FileDiff((), note="git could not compare this file.")
    )


def _agent_root(explorer: Explorer, cwd: Path) -> Root:
    holding = [r for r in explorer.roots() if inside(cwd, r.path)]
    if not holding:
        raise UnknownRoot(str(cwd))
    # A worktree nested inside its main checkout is the nearer, and the right, root.
    return max(holding, key=lambda r: len(str(r.path)))


def _agent_target(explorer: Explorer, root: Root, cwd: Path, path: str) -> Path:
    if "\0" in path:
        raise OutsideRoot("a NUL in a path")
    # Agents write paths from where they stand; fall back to the top of the repository.
    for base in (cwd, root.path):
        real = (base / path).resolve()
        if inside(real, root.path) and real.exists():
            return resolve(root, relative(root, real), explorer.fence)
    return resolve(root, path, explorer.fence)


def _register_pages(router: APIRouter, explorer: Explorer) -> None:
    @router.get("/files")
    def roots_page() -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        roots = explorer.roots()
        context = {"roots": roots, "statuses": {r.id: explorer.git.status(r.path) for r in roots}}
        return explorer.web.pages.page("files_roots.html", context)

    @router.get("/files/{root_id}")
    def changes(root_id: str, from_: From = "") -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        root = explorer.root(root_id)
        return explorer.page("files_changes.html", root, Nav(root.id, _back(from_)), "changes")

    @router.get("/files/{root_id}/tree")
    def tree(  # pyright: ignore[reportUnusedFunction]
        root_id: str,
        path: str = "",
        everything: Annotated[str, Query(alias="all")] = "",
        from_: From = "",
    ) -> Response:
        root = explorer.root(root_id)
        nav = Nav(root.id, _back(from_))
        real = resolve(root, path, explorer.fence)
        rel = relative(root, real)
        if real.is_file():
            return _redirect(nav.href("file", rel))
        entries = _children(explorer, root, real, rel, everything=bool(everything))
        context = {"rel": rel, "entries": entries, "everything": bool(everything)}
        return explorer.page("files_tree.html", root, nav, "tree", **context)

    @router.get("/files/{root_id}/find")
    def find(root_id: str, q: str = "", from_: From = "") -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        root = explorer.root(root_id)
        found = fuzzy.find(q, explorer.git.files(root.path))
        context = {"q": q, "found": found}
        return explorer.page("files_find.html", root, Nav(root.id, _back(from_)), "find", **context)


def _register_file(router: APIRouter, explorer: Explorer) -> None:
    @router.get("/files/{root_id}/file")
    def file(  # noqa: PLR0913, PLR0917 - a route's parameters are its query string  # pyright: ignore[reportUnusedFunction]
        root_id: str,
        path: str = "",
        line: int = 0,
        reveal: str = "",
        source: str = "",
        from_: From = "",
    ) -> Response:
        root = explorer.root(root_id)
        nav = Nav(root.id, _back(from_))
        real = resolve(root, path, explorer.fence)
        rel = relative(root, real)
        if real.is_dir():
            return _redirect(nav.href("tree", rel))
        shown: FileView = explorer.viewer.open(
            LocalReader(root.path), real, line=line, reveal=bool(reveal), source=bool(source)
        )
        context = {
            "rel": rel,
            "file": shown,
            "at": line,
            "source": bool(source),
            "cadence": WATCH_CADENCE,
        }
        return explorer.page("files_file.html", root, nav, "tree", **context)

    @router.get("/files/{root_id}/diff")
    def diff(  # pyright: ignore[reportUnusedFunction]
        root_id: str, path: str = "", reveal: str = "", from_: From = ""
    ) -> HTMLResponse:
        root = explorer.root(root_id)
        nav = Nav(root.id, _back(from_))
        real = resolve(root, path, explorer.fence)
        rel = relative(root, real)
        status = explorer.git.status(root.path)
        gated = is_secret(real.name) and not reveal
        found = None if gated else _diff(explorer, root, real, rel, status)
        context = {"rel": rel, "diff": found, "change": status.of(rel), "gated": gated}
        return explorer.page("files_diff.html", root, nav, "changes", **context)


def _image(root: Root, real: Path, path: str) -> Response:
    reader = LocalReader(root.path)
    if reader.stat(real).size > IMAGE_BYTES:
        raise NotReadable(f"{path} is over the image cap")
    body = reader.read_range(real, 0, IMAGE_BYTES)
    media = image_type(real.name, body[:SNIFF_BYTES])
    if not media:
        raise NotReadable(f"{path} is not an image this viewer shows")
    return Response(body, media_type=media, headers=NO_STORE)


def _version(root: Root, real: Path) -> str:
    try:
        return LocalReader(root.path).stat(real).version
    except NotReadable:
        return ""


def _register_bytes(router: APIRouter, explorer: Explorer) -> None:
    @router.get("/files/{root_id}/raw")
    def raw(root_id: str, path: str = "", reveal: str = "") -> Response:  # pyright: ignore[reportUnusedFunction]
        root = explorer.root(root_id)
        real = resolve(root, path, explorer.fence)
        if is_secret(real.name) and not reveal:
            raise OutsideRoot(path)
        return _image(root, real, path)

    @router.get("/files/{root_id}/watch", dependencies=[Depends(hidden_tick)])
    def watch(root_id: str, path: str = "", v: str = "", from_: From = "") -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        root = explorer.root(root_id)
        nav = Nav(root.id, _back(from_))
        real = resolve(root, path, explorer.fence)
        now = _version(root, real)
        context = {
            "nav": nav,
            "rel": relative(root, real),
            "version": v,
            "now": now,
            "cadence": WATCH_CADENCE,
        }
        return explorer.web.pages.fragment("files_watch.html", context)


def _register_agent(router: APIRouter, explorer: Explorer) -> None:
    @router.get("/agent/{session}/files")
    def from_agent(  # pyright: ignore[reportUnusedFunction]
        session: str, path: str = "", line: int = 0, to: str = "", q: str = ""
    ) -> RedirectResponse:
        agent = find_agent(explorer.web, session)
        return _redirect(_agent_url(explorer, agent, _Ask(path, line, to, q)))


@dataclass(frozen=True, slots=True)
class _Ask:
    path: str
    line: int
    to: str
    q: str


def _agent_url(explorer: Explorer, agent: Agent, ask: _Ask) -> str:
    path, line, to = ask.path, ask.line, ask.to
    cwd = Path(agent.cwd or "/").resolve()
    root = _agent_root(explorer, cwd)
    nav = Nav(root.id, agent.session)
    if to == "find":
        return nav.href("find", q=ask.q)
    if not path:
        return nav.href("tree" if to == "tree" else "")
    rel = relative(root, _agent_target(explorer, root, cwd, path))
    if to == "diff":
        return nav.href("diff", rel)
    anchor = f"#L{line}" if line > 0 else ""
    return nav.href("file", rel, line=line) + anchor


def build(web: WebState) -> APIRouter:
    """The explorer's routes: read-only, every one behind the same identity guard."""
    router = APIRouter(dependencies=[Depends(viewer_guard(web))])
    fence = Fence.around(Path.home(), web.settings.state_dir)
    explorer = Explorer(web, Git(clock=web.clock), Viewer(), fence)
    _register_pages(router, explorer)
    _register_file(router, explorer)
    _register_bytes(router, explorer)
    _register_agent(router, explorer)
    return router
