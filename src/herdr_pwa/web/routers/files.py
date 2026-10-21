"""The file explorer: what changed in a root, its tree, go-to-file, one file and one diff. GET only."""

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Final
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from herdr_pwa.engine import Agent, InvalidRequestError, as_session_id
from herdr_pwa.files import diffs, fuzzy
from herdr_pwa.files.git import Git, Status
from herdr_pwa.files.reader import LocalReader, NotReadableError, OutsideRootError, inside
from herdr_pwa.files.roots import (
    ROOT_ID,
    Fence,
    Root,
    UnknownRootError,
    discover,
    is_git_dir,
    is_secret,
    relative,
    resolve,
)
from herdr_pwa.files.view import (
    IMAGE_BYTES,
    PAGE_LINES,
    SNIFF_BYTES,
    FileView,
    Look,
    Viewer,
    human_size,
    image_type,
)
from herdr_pwa.web.deps import WebState, hidden_tick, viewer_guard
from herdr_pwa.web.failures import AgentGoneError
from herdr_pwa.web.herd import agent_status, find_agent
from herdr_pwa.web.routers.agent import read_stats

# An open file is re-checked on disk at the feed's quiet cadence.
WATCH_CADENCE: Final = 10
VIEWS: Final = ("changes", "tree", "find")
NO_STORE: Final = MappingProxyType({"Cache-Control": "no-store"})

type From = Annotated[str, Query(alias="from")]


class _At(BaseModel):
    """A path inside one root, and the agent page the reader came from (``?from=``)."""

    path: str = ""
    from_: str = Field("", alias="from")


class TreeQuery(_At):
    """A folder's listing; ``?all=`` lists what git does not track too."""

    everything: str = Field("", alias="all")


class FileQuery(_At, Look):
    """One file, and how the viewer is asked to show it."""


class DiffQuery(_At):
    """One file's change, past the secret tap when ``reveal`` is set."""

    reveal: str = ""


class RawQuery(BaseModel):
    """One image's bytes, past the secret tap when ``reveal`` is set."""

    path: str = ""
    reveal: str = ""


class WatchQuery(_At):
    """An open file's re-check, against the version ``v`` it was drawn at."""

    v: str = ""


class AgentFilesQuery(BaseModel):
    """Where an agent's Files tab, or a path in its chat, should land in its root."""

    path: str = ""
    line: int = 0
    to: str = ""
    q: str = ""


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


@dataclass(frozen=True, slots=True)
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
        """The root with this id, or UnknownRootError."""
        if ROOT_ID.fullmatch(root_id):
            for root in self.roots():
                if root.id == root_id:
                    return root
        raise UnknownRootError(root_id)

    def page(self, name: str, root: Root, nav: Nav, view: str, **context: object) -> HTMLResponse:
        """One explorer page, with the bar, the root's status and its links."""
        agent = self.agent(nav.back)
        shared = {
            "agent": agent,
            "stats": read_stats(self.web, agent) if agent is not None else None,
            "agent_status": agent_status(self.web, agent) if agent is not None else None,
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

    def at(self, root_id: str, back: str) -> tuple[Root, Nav]:
        """The root with this id, and the links inside it that carry the page ``back`` names."""
        root = self.root(root_id)
        return root, Nav(root.id, _back(back))

    def spot(self, root: Root, path: str) -> "_Spot":
        """``path`` resolved inside ``root``, behind the fence."""
        real = resolve(root, path, self.fence)
        return _Spot(root, real, relative(root, real))

    def agent(self, session: str) -> Agent | None:
        """The agent whose Files tab this is, or None once it has gone."""
        if not session:
            return None
        try:
            return find_agent(self.web, session)
        except (AgentGoneError, InvalidRequestError):
            return None


@dataclass(frozen=True, slots=True)
class _Spot:
    """One path inside a root, resolved: the root, the real path on disk and its relative name."""

    root: Root
    real: Path
    rel: str


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303, headers=NO_STORE)


def _children(explorer: Explorer, spot: _Spot, *, everything: bool) -> list[tuple[str, bool, int]]:
    root, rel = spot.root, spot.rel
    listing = LocalReader(root.path).list_dir(spot.real)
    entries = [(e.name, e.is_dir, e.size) for e in listing.entries if not is_git_dir(e.name)]
    if everything:
        return entries
    prefix = f"{rel}/" if rel else ""
    known = {
        p[len(prefix) :].split("/", 1)[0]
        for p in explorer.git.files(root.path)
        if p.startswith(prefix)
    }
    return [e for e in entries if e[0] in known]


def _diff(explorer: Explorer, spot: _Spot, status: Status) -> diffs.FileDiff:
    root, rel = spot.root, spot.rel
    change = status.of(rel)
    if change is not None and change.kind == "new":
        raw = LocalReader(root.path).read_range(spot.real, 0, IMAGE_BYTES)
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
        raise UnknownRootError(str(cwd))
    # A worktree nested inside its main checkout is the nearer, and the right, root.
    return max(holding, key=lambda r: len(str(r.path)))


def _agent_target(explorer: Explorer, root: Root, cwd: Path, path: str) -> Path:
    if "\0" in path:
        raise OutsideRootError("a NUL in a path")
    # Agents write paths from where they stand; fall back to the top of the repository.
    for base in (cwd, root.path):
        real = (base / path).resolve()
        if inside(real, root.path) and real.exists():
            return resolve(root, relative(root, real), explorer.fence)
    return resolve(root, path, explorer.fence)


def _register_pages(router: APIRouter, explorer: Explorer) -> None:
    @router.get("/files")
    def roots_page() -> HTMLResponse:
        roots = explorer.roots()
        context = {"roots": roots, "statuses": {r.id: explorer.git.status(r.path) for r in roots}}
        return explorer.web.pages.page("files_roots.html", context)

    @router.get("/files/{root_id}")
    def changes(root_id: str, from_: From = "") -> HTMLResponse:
        root, nav = explorer.at(root_id, from_)
        return explorer.page("files_changes.html", root, nav, "changes")

    @router.get("/files/{root_id}/tree")
    def tree(root_id: str, query: Annotated[TreeQuery, Query()]) -> Response:
        root, nav = explorer.at(root_id, query.from_)
        spot = explorer.spot(root, query.path)
        if spot.real.is_file():
            return _redirect(nav.href("file", spot.rel))
        everything = bool(query.everything)
        entries = _children(explorer, spot, everything=everything)
        context = {"rel": spot.rel, "entries": entries, "everything": everything}
        return explorer.page("files_tree.html", root, nav, "tree", **context)

    @router.get("/files/{root_id}/find")
    def find(root_id: str, q: str = "", from_: From = "") -> HTMLResponse:
        root, nav = explorer.at(root_id, from_)
        found = fuzzy.find(q, explorer.git.files(root.path))
        return explorer.page("files_find.html", root, nav, "find", q=q, found=found)


def _register_file(router: APIRouter, explorer: Explorer) -> None:
    @router.get("/files/{root_id}/file")
    def file(root_id: str, query: Annotated[FileQuery, Query()]) -> Response:
        return _file_page(explorer, root_id, query)

    @router.get("/files/{root_id}/diff")
    def diff(root_id: str, query: Annotated[DiffQuery, Query()]) -> HTMLResponse:
        return _diff_page(explorer, root_id, query)


def _file_page(explorer: Explorer, root_id: str, query: FileQuery) -> Response:
    root, nav = explorer.at(root_id, query.from_)
    spot = explorer.spot(root, query.path)
    if spot.real.is_dir():
        return _redirect(nav.href("tree", spot.rel))
    shown: FileView = explorer.viewer.open(LocalReader(root.path), spot.real, query)
    context = {
        "rel": spot.rel,
        "file": shown,
        "at": query.line,
        "source": query.source,
        "cadence": WATCH_CADENCE,
    }
    return explorer.page("files_file.html", root, nav, "tree", **context)


def _diff_page(explorer: Explorer, root_id: str, query: DiffQuery) -> HTMLResponse:
    root, nav = explorer.at(root_id, query.from_)
    spot = explorer.spot(root, query.path)
    status = explorer.git.status(root.path)
    gated = is_secret(spot.real.name) and not query.reveal
    found = None if gated else _diff(explorer, spot, status)
    context = {"rel": spot.rel, "diff": found, "change": status.of(spot.rel), "gated": gated}
    return explorer.page("files_diff.html", root, nav, "changes", **context)


def _image(root: Root, real: Path, path: str) -> Response:
    reader = LocalReader(root.path)
    if reader.stat(real).size > IMAGE_BYTES:
        raise NotReadableError(f"{path} is over the image cap")
    body = reader.read_range(real, 0, IMAGE_BYTES)
    media = image_type(real.name, body[:SNIFF_BYTES])
    if not media:
        raise NotReadableError(f"{path} is not an image this viewer shows")
    return Response(body, media_type=media, headers=NO_STORE)


def _version(root: Root, real: Path) -> str:
    try:
        return LocalReader(root.path).stat(real).version
    except NotReadableError:
        return ""


def _register_bytes(router: APIRouter, explorer: Explorer) -> None:
    @router.get("/files/{root_id}/raw")
    def raw(root_id: str, query: Annotated[RawQuery, Query()]) -> Response:
        root = explorer.root(root_id)
        real = resolve(root, query.path, explorer.fence)
        if is_secret(real.name) and not query.reveal:
            raise OutsideRootError(query.path)
        return _image(root, real, query.path)

    @router.get("/files/{root_id}/watch", dependencies=[Depends(hidden_tick)])
    def watch(root_id: str, query: Annotated[WatchQuery, Query()]) -> HTMLResponse:
        root, nav = explorer.at(root_id, query.from_)
        spot = explorer.spot(root, query.path)
        context = {
            "nav": nav,
            "rel": spot.rel,
            "version": query.v,
            "now": _version(root, spot.real),
            "cadence": WATCH_CADENCE,
        }
        return explorer.web.pages.fragment("files_watch.html", context)


def _register_agent(router: APIRouter, explorer: Explorer) -> None:
    @router.get("/agent/{session}/files")
    def from_agent(session: str, query: Annotated[AgentFilesQuery, Query()]) -> Response:
        agent = find_agent(explorer.web, session)
        try:
            return _redirect(_agent_url(explorer, agent, query))
        except UnknownRootError:
            # The tab stays, so the bar keeps its shape; the page says why it is empty.
            context = {
                "agent": agent,
                "agent_status": agent_status(explorer.web, agent),
                "stats": read_stats(explorer.web, agent),
            }
            return explorer.web.pages.page("files_none.html", context)


def _agent_url(explorer: Explorer, agent: Agent, ask: AgentFilesQuery) -> str:
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
