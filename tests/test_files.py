"""The file explorer: containment, the secret tap, the caps, the highlight limit, and no caching."""

import os
import threading
import time
from collections.abc import Generator, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Final, cast

import httpx2 as httpx
import pytest
import uvicorn
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from starlette.routing import BaseRoute

from herdr_pwa.files import fuzzy
from herdr_pwa.files.git import Git, _filter_names, parse_status  # pyright: ignore[reportPrivateUsage]
from herdr_pwa.files.reader import Listing, LocalReader, OutsideRoot, Stat
from herdr_pwa.files.roots import Fence, is_secret, root_id
from herdr_pwa.files.view import (
    BUSY_NOTE,
    LINE_CHARS,
    PAGE_LINES,
    Viewer,
    human_size,
)
from herdr_pwa.web.mdnode import MdNode
from herdr_pwa.web.routers import files as files_router
from tests.filefakes import booby_trap, git, make_repo, stand_in, trapped_submodule
from tests.webfakes import (
    SNAPSHOT,
    WORKING_SESSION,
    Rig,
    assistant_says,
    build_rig,
    snapshot_frame,
    write_transcript,
)

SECRET_TEXT: Final = "do-not-show-me"  # noqa: S105 - the fixture's planted value
FROM: Final = f"from={WORKING_SESSION}"


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return make_repo(tmp_path_factory.mktemp("work") / "demo-service")


def _rig(tmp_path: Path, repo: Path, login: str | None = None) -> Rig:
    settings: dict[str, object] = {} if login is None else {"assume_login": login}
    frame = stand_in(snapshot_frame(), repo)
    return build_rig(tmp_path, {SNAPSHOT: frame}, headers=None, paced=False, **settings)


def _holds(page: str, part: str) -> bool:
    # A bare bool: pytest's diff of a `not in` against a page of thousands of lines never ends.
    return part in page


def _base(repo: Path) -> str:
    return f"/files/{root_id(repo)}"


# ---- containment ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "../../../../etc/passwd",
        "/etc/passwd",
        "src/../../demo-outside.txt",
        "%2e%2e%2f%2e%2e%2f%2e%2e%2fetc%2fpasswd",
        "src%2f..%2f..%2f..%2f..%2fetc%2fpasswd",
        "%00",
    ],
)
@pytest.mark.parametrize("view", ["file", "tree", "diff", "raw", "watch"])
def test_a_path_that_leads_out_of_the_root_is_refused(
    tmp_path: Path, repo: Path, path: str, view: str
) -> None:
    rig = _rig(tmp_path, repo)
    response = rig.http.get(f"{_base(repo)}/{view}?path={path}", follow_redirects=False)
    assert response.status_code == 403, response.text[:300]
    assert "root:" not in response.text


def test_a_twice_encoded_traversal_is_a_literal_name_that_is_not_there(
    tmp_path: Path, repo: Path
) -> None:
    rig = _rig(tmp_path, repo)
    response = rig.http.get(f"{_base(repo)}/file?path=%252e%252e%252fetc%252fpasswd")
    assert response.status_code == 404
    assert "root:" not in response.text


def test_a_symlink_out_of_the_root_is_refused_however_it_is_reached(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "loot.txt").write_text("the loot\n")
    repo = make_repo(tmp_path / "linked")
    (repo / "loot.txt").symlink_to(outside / "loot.txt")
    (repo / "elsewhere").symlink_to(outside)
    (repo / "docs" / "up").symlink_to(repo.parent)
    rig = _rig(tmp_path, repo)
    base = _base(repo)
    for url in (
        f"{base}/file?path=loot.txt",
        f"{base}/raw?path=loot.txt",
        f"{base}/diff?path=loot.txt",
        f"{base}/tree?path=elsewhere",
        f"{base}/file?path=elsewhere/loot.txt",
        f"{base}/tree?path=docs/up",
        f"{base}/file?path=docs/up/outside/loot.txt",
    ):
        response = rig.http.get(url, follow_redirects=False)
        assert response.status_code == 403, url
        assert "the loot" not in response.text, url


def test_a_symlink_inside_the_root_is_followed(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "inner")
    (repo / "readme-link.md").symlink_to(repo / "README.md")
    rig = _rig(tmp_path, repo)
    response = rig.http.get(f"{_base(repo)}/file?path=readme-link.md")
    assert response.status_code == 200
    assert "A tiny service" in response.text


def test_the_reader_checks_what_it_opened_not_only_what_it_was_asked(tmp_path: Path) -> None:
    # The route resolves first; this is the second check, for a link swapped in between.
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("no")
    reader = LocalReader(root.resolve())
    with pytest.raises(OutsideRoot):
        reader.read_range(tmp_path / "secret.txt", 0, 10)
    with pytest.raises(OutsideRoot):
        reader.list_dir(tmp_path)


def test_a_fifo_is_not_read_and_never_hangs(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "piped")
    os.mkfifo(repo / "pipe.txt")
    rig = _rig(tmp_path, repo)
    response = rig.http.get(f"{_base(repo)}/file?path=pipe.txt")
    assert response.status_code == 404


def test_only_a_known_root_answers(tmp_path: Path, repo: Path) -> None:
    rig = _rig(tmp_path, repo)
    for root in (root_id(Path("/etc")), "../etc", "zzzzzzzzzzzz", root_id(repo.parent)):
        assert rig.http.get(f"/files/{root}/tree").status_code == 404, root
    assert rig.http.get(f"{_base(repo)}/tree").status_code == 200


def test_roots_are_the_repositories_agents_stand_in_and_never_home(tmp_path: Path) -> None:
    home = Path.home().resolve()
    fence = Fence.around(home, tmp_path / "state")
    assert not fence.may_root(home)
    assert not fence.may_root(Path("/"))
    assert not fence.may_root(home.parent)
    assert not fence.allows(home / ".ssh" / "id_ed25519")
    assert not fence.allows(home / ".config" / "herdr-pwa-v2" / "audit.jsonl")
    assert not fence.allows((tmp_path / "state" / "vapid.pem").resolve())
    assert fence.may_root((tmp_path / "work").resolve())


def test_a_dotfiles_repository_at_home_is_not_a_root(tmp_path: Path) -> None:
    home = Path.home()
    make_repo(home)
    rig = _rig(tmp_path, home)
    assert "No agent is working in a git repository" in rig.http.get("/files").text
    assert "not working in a git repository" in rig.http.get(f"/agent/{WORKING_SESSION}/files").text


def test_a_worktree_of_a_root_is_a_root_too(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "main-checkout")
    tree = tmp_path / "feature-tree"
    git(repo, "worktree", "add", "-q", "-b", "feature", str(tree))
    rig = _rig(tmp_path, repo)
    page = rig.http.get("/files").text
    assert "main-checkout" in page
    assert "feature-tree" in page
    assert rig.http.get(f"/files/{root_id(tree.resolve())}/tree").status_code == 200


def test_a_forged_worktree_entry_is_not_a_root(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "main-checkout")
    real = tmp_path / "feature-tree"
    git(repo, "worktree", "add", "-q", "-b", "feature", str(real))
    # Another repository of the user's, and a plain directory, each named by a forged entry.
    other = make_repo(tmp_path / "forged-other")
    plain = tmp_path / "forged-plain"
    plain.mkdir()
    (plain / "data.txt").write_text("not shared\n")
    for name, target in (("evil", plain), ("evil2", other)):
        admin = repo / ".git" / "worktrees" / name
        admin.mkdir(parents=True)
        (admin / "gitdir").write_text(f"{target}/.git\n")
        (admin / "HEAD").write_text("ref: refs/heads/main\n")
        (admin / "commondir").write_text("../..\n")
    page = _rig(tmp_path, repo).http.get("/files").text
    assert "feature-tree" in page
    assert not _holds(page, "forged-plain")
    assert not _holds(page, "forged-other")
    for target in (plain, other):
        assert (
            _rig(tmp_path, repo).http.get(f"/files/{root_id(target.resolve())}/tree").status_code
            == 404
        )


def test_a_work_tree_set_outside_the_agents_cwd_is_not_a_root(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "pointing")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    git(repo, "config", "core.worktree", str(elsewhere))
    rig = _rig(tmp_path, repo)
    assert rig.http.get(f"/files/{root_id(elsewhere.resolve())}/tree").status_code == 404
    assert "No agent is working in a git repository" in rig.http.get("/files").text


@pytest.mark.parametrize(
    "rel",
    [
        ".config/gh/hosts.yml",
        ".local/share/keyrings/login.keyring",
        ".netrc",
        ".git-credentials",
        ".docker/config.json",
        ".kube/config",
    ],
)
def test_credential_stores_under_home_are_fenced(rel: str) -> None:
    home = Path.home().resolve()
    assert not Fence.around(home).allows(home / rel)


# ---- what is shown, and what is held back -------------------------------------------------


def test_a_secret_is_held_behind_a_tap_everywhere(tmp_path: Path, repo: Path) -> None:
    rig = _rig(tmp_path, repo)
    base = _base(repo)
    held = rig.http.get(f"{base}/file?path=config/.env")
    assert held.status_code == 200
    assert "may hold secrets" in held.text
    assert SECRET_TEXT not in held.text
    assert "reveal=1" in held.text
    assert rig.http.get(f"{base}/raw?path=config/.env").status_code == 403
    assert "PRIVATE KEY" not in rig.http.get(f"{base}/file?path=config/deploy.pem").text
    shown = rig.http.get(f"{base}/file?path=config/.env&reveal=1")
    assert SECRET_TEXT in shown.text


@pytest.mark.parametrize(
    ("name", "secret"),
    [
        (".env", True),
        (".env.production", True),
        ("server.pem", True),
        ("id_ed25519", True),
        ("id_rsa.pub", True),
        ("tls.key", True),
        ("credentials.json", True),
        ("Credentials", True),
        ("environment.py", False),
        ("keys.py", False),
        ("README.md", False),
    ],
)
def test_secret_names(name: str, *, secret: bool) -> None:
    assert is_secret(name) is secret


def test_a_binary_file_says_so_and_shows_no_bytes(tmp_path: Path, repo: Path) -> None:
    rig = _rig(tmp_path, repo)
    response = rig.http.get(f"{_base(repo)}/file?path=data/blob.bin")
    assert response.status_code == 200
    assert "binary file" in response.text
    assert "binary\x00" not in response.text
    assert 'class="src"' not in response.text


def test_an_image_is_drawn_from_its_own_no_store_route(tmp_path: Path, repo: Path) -> None:
    rig = _rig(tmp_path, repo)
    page = rig.http.get(f"{_base(repo)}/file?path=assets/logo.png").text
    assert '<img class="fileimg" src="' in page
    raw = rig.http.get(f"{_base(repo)}/raw?path=assets/logo.png")
    assert raw.headers["content-type"] == "image/png"
    assert raw.headers["cache-control"] == "no-store"
    assert raw.headers["x-content-type-options"] == "nosniff"
    # A text file is never served as bytes, whatever its name claims.
    (tmp_path / "fake").mkdir()
    fake = make_repo(tmp_path / "fake" / "repo")
    (fake / "trap.png").write_text("<script>alert(1)</script>")
    rig = _rig(tmp_path / "fake", fake)
    assert rig.http.get(f"{_base(fake)}/raw?path=trap.png").status_code == 404


def test_code_is_highlighted_and_opens_on_the_line_asked_for(tmp_path: Path, repo: Path) -> None:
    rig = _rig(tmp_path, repo)
    page = rig.http.get(f"{_base(repo)}/file?path=src/app/server.py&line=30").text
    assert 'id="L30" data-hp-at' in page
    assert '<span class="tok-keyword">class</span>' in page
    assert 'class="src__chunk"' in page
    # The Pane's own switch, so the remembered choice carries over to files.
    assert 'data-hp-columns aria-pressed="false">Keep the columns</button>' in page


def test_markdown_renders_through_the_prose_tree(tmp_path: Path, repo: Path) -> None:
    rig = _rig(tmp_path, repo)
    page = rig.http.get(f"{_base(repo)}/file?path=docs/guide.md").text
    assert '<div class="prose filemd">' in page
    assert "<table>" in page
    assert '<span class="tok-keyword">from</span>' in page
    source = rig.http.get(f"{_base(repo)}/file?path=docs/guide.md&source=1").text
    assert "<table>" not in source
    assert "Show it rendered" in source


def test_a_large_file_is_paged_plain_text(tmp_path: Path, repo: Path) -> None:
    rig = _rig(tmp_path, repo)
    page = rig.http.get(f"{_base(repo)}/file?path=logs/big.log&line=1200").text
    assert _holds(page, 'id="L1001"')
    assert _holds(page, 'id="L1200" data-hp-at')
    assert _holds(page, 'id="L1500"')
    assert not _holds(page, 'id="L1501"')
    assert not _holds(page, 'id="L1000"')
    assert not _holds(page, "tok-")
    assert _holds(page, "page 3 of 24")
    assert _holds(page, "line=501")
    assert _holds(page, "line=1501")


def test_too_many_lines_is_paged_even_when_the_bytes_are_few(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "lines")
    # A literal, not HIGHLIGHT_LINES: a test sized from the cap cannot catch the cap moving.
    (repo / "many.py").write_text("x = 1\n" * 5001)
    rig = _rig(tmp_path, repo)
    page = rig.http.get(f"{_base(repo)}/file?path=many.py").text
    assert _holds(page, f'id="L{PAGE_LINES}"')
    assert not _holds(page, f'id="L{PAGE_LINES + 1}"')
    assert not _holds(page, "tok-")


def test_a_minified_line_is_cut_and_the_rest_counted(tmp_path: Path, repo: Path) -> None:
    rig = _rig(tmp_path, repo)
    response = rig.http.get(f"{_base(repo)}/file?path=dist/bundle.min.js")
    assert response.status_code == 200
    size = (repo / "dist/bundle.min.js").stat().st_size
    assert _holds(response.text, f"{size - 1 - LINE_CHARS:,} more characters")
    # The page carries the cut line, not the 180KB one.
    assert len(response.content) < 40_000
    assert not _holds(response.text, 'id="L2"')


def test_the_highlight_limit_sends_a_third_file_plain(tmp_path: Path) -> None:
    gate = threading.Event()
    started = threading.Semaphore(0)

    def slow(code: str, language: str, limit: int) -> tuple[MdNode, ...]:
        del code, language, limit
        started.release()
        gate.wait(10)
        return ()

    viewer = Viewer(paint=slow, wait=0.2)
    for n in range(3):
        (tmp_path / f"f{n}.py").write_text(f"x = {n}\n")
    reader = LocalReader(tmp_path.resolve())

    def open_one(n: int) -> None:
        viewer.open(reader, tmp_path / f"f{n}.py", line=0, reveal=False, source=False)

    busy = [threading.Thread(target=open_one, args=(n,)) for n in range(2)]
    for thread in busy:
        thread.start()
    assert started.acquire(timeout=5)
    assert started.acquire(timeout=5)
    third = viewer.open(reader, tmp_path / "f2.py", line=0, reveal=False, source=False)
    assert third.note == BUSY_NOTE
    assert [r.text for r in third.lines[0].runs] == ["x = 2"]
    gate.set()
    for thread in busy:
        thread.join(5)
    assert viewer.open(reader, tmp_path / "f2.py", line=0, reveal=False, source=False).note == ""


def _hold_both_slots(
    viewer: Viewer, tmp_path: Path
) -> tuple[threading.Event, list[threading.Thread]]:
    """Two code highlights parked inside the viewer's slots until the event is set."""
    gate = threading.Event()
    started = threading.Semaphore(0)

    def slow(code: str, language: str, limit: int) -> tuple[MdNode, ...]:
        del code, language, limit
        started.release()
        gate.wait(10)
        return ()

    viewer.paint = slow
    reader = LocalReader(tmp_path.resolve())
    for n in range(2):
        (tmp_path / f"held{n}.py").write_text(f"x = {n}\n")
    busy = [
        threading.Thread(
            target=viewer.open,
            args=(reader, tmp_path / f"held{n}.py"),
            kwargs={"line": 0, "reveal": False, "source": False},
        )
        for n in range(2)
    ]
    for thread in busy:
        thread.start()
    assert started.acquire(timeout=5)
    assert started.acquire(timeout=5)
    return gate, busy


def test_markdown_waits_for_a_highlight_slot_like_code(tmp_path: Path) -> None:
    rendered: list[str] = []

    def render(text: str) -> tuple[MdNode, ...]:
        rendered.append(text)
        return (MdNode(text=text),)

    viewer = Viewer(wait=0.2, render=render)
    gate, busy = _hold_both_slots(viewer, tmp_path)
    (tmp_path / "notes.md").write_text("# Notes\n")
    reader = LocalReader(tmp_path.resolve())
    shown = viewer.open(reader, tmp_path / "notes.md", line=0, reveal=False, source=False)
    assert shown.note == BUSY_NOTE
    assert shown.kind == "code"
    assert rendered == []
    gate.set()
    for thread in busy:
        thread.join(5)
    shown = viewer.open(reader, tmp_path / "notes.md", line=0, reveal=False, source=False)
    assert (shown.kind, shown.note, rendered) == ("markdown", "", ["# Notes\n"])


def test_markdown_past_its_cap_is_shown_as_source(tmp_path: Path) -> None:
    rendered: list[str] = []

    def render(text: str) -> tuple[MdNode, ...]:
        rendered.append(text)
        return ()

    # A literal, not the cap: 100KB of brackets is seconds of CPU in the markdown parser.
    (tmp_path / "big.md").write_text("[" * 100_000 + "\n")
    viewer = Viewer(render=render)
    reader = LocalReader(tmp_path.resolve())
    shown = viewer.open(reader, tmp_path / "big.md", line=0, reveal=False, source=False)
    assert shown.kind == "code"
    assert "too long to render" in shown.note
    assert rendered == []


class _Counting:
    """A reader that remembers the longest read it was asked for."""

    def __init__(self, root: Path) -> None:
        self.inner = LocalReader(root)
        self.longest = 0

    def list_dir(self, path: Path) -> Listing:
        return self.inner.list_dir(path)

    def stat(self, path: Path) -> Stat:
        return self.inner.stat(path)

    def read_range(self, path: Path, offset: int, length: int) -> bytes:
        self.longest = max(self.longest, length)
        return self.inner.read_range(path, offset, length)


def test_a_page_with_no_newline_is_read_only_up_to_its_byte_cap(tmp_path: Path) -> None:
    # A literal: a few MB with no newline is one page, which must not be read whole.
    (tmp_path / "one.log").write_bytes(b"y" * (6 * 1024 * 1024))
    reader = _Counting(tmp_path.resolve())
    shown = Viewer().open(reader, tmp_path / "one.log", line=0, reveal=False, source=False)
    assert shown.kind == "paged"
    assert reader.longest <= 2 * 1024 * 1024
    assert [line.n for line in shown.lines] == [1]
    assert shown.lines[0].cut > 0
    assert "cut" in shown.note


def test_a_diff_is_read_from_git_only_up_to_a_cap(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "huge-diff")
    # A literal: 12MB of changed lines, past any page a phone reads.
    (repo / "logs/big.log").write_text("changed line of the log\n" * 500_000)
    found = Git().diff(repo, "logs/big.log")
    assert found is not None
    assert 0 < len(found) <= 8 * 1024 * 1024
    assert found.startswith("diff --git")


# ---- one huge file cannot stall the feed --------------------------------------------------


@contextmanager
def _serving(rig: Rig) -> Generator[str]:
    """The rig's app on an ephemeral loopback port, never a port a real bridge uses."""
    config = uvicorn.Config(
        rig.http.app, host="127.0.0.1", port=0, log_level="error", lifespan="off"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(10)


@pytest.fixture
def served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[str, threading.Event]]:
    """The real app on an ephemeral port, its highlighter stuck until the test lets it go."""
    gate = threading.Event()

    def stuck(code: str, language: str, limit: int) -> tuple[MdNode, ...]:
        del code, language, limit
        gate.wait(20)
        return ()

    monkeypatch.setattr(files_router, "Viewer", lambda: Viewer(paint=stuck, wait=30))
    repo = make_repo(tmp_path / "huge")
    (repo / "huge.py").write_text("value = 1  # a line\n" * 4000)
    rig = _rig(tmp_path, repo, login="me@example.com")
    with _serving(rig) as origin:
        try:
            yield f"{origin}{_base(repo)}", gate
        finally:
            gate.set()


FEED_BUDGET: Final = 1.0


def test_a_feed_poll_completes_while_two_huge_highlights_hold_their_slots(
    served: tuple[str, threading.Event],
) -> None:
    base, gate = served
    origin = base.split("/files/")[0]
    held: list[httpx.Response] = []

    def open_huge() -> None:
        held.append(httpx.get(f"{base}/file?path=huge.py", timeout=30))

    jammed = [threading.Thread(target=open_huge) for _ in range(2)]
    for thread in jammed:
        thread.start()
    time.sleep(0.3)
    assert not held, "the stuck highlights should still be running"
    started = time.monotonic()
    feed = httpx.get(f"{origin}/feed", timeout=5)
    elapsed = time.monotonic() - started
    assert feed.status_code == 200
    assert elapsed < FEED_BUDGET, f"the feed took {elapsed:.2f}s behind a highlight"
    assert not held
    gate.set()
    for thread in jammed:
        thread.join(10)
    assert [r.status_code for r in held] == [200, 200]


def test_a_real_highlight_of_a_capped_file_leaves_the_feed_within_budget(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "capped")
    line = "def f(x: int) -> int:  # comment\n    return x * 2 + len('text')\n"
    (repo / "big.py").write_text(line * 2400)
    rig = _rig(tmp_path, repo, login="me@example.com")
    with _serving(rig) as origin:
        worker = threading.Thread(
            target=httpx.get,
            args=(f"{origin}{_base(repo)}/file?path=big.py",),
            kwargs={"timeout": 30},
        )
        worker.start()
        time.sleep(0.05)
        started = time.monotonic()
        assert httpx.get(f"{origin}/feed", timeout=5).status_code == 200
        assert time.monotonic() - started < FEED_BUDGET
        worker.join(30)


# ---- changes, tree, find, diff --------------------------------------------------------------


def test_what_changed_comes_first_and_links_each_diff(tmp_path: Path, repo: Path) -> None:
    rig = _rig(tmp_path, repo)
    page = rig.http.get(_base(repo)).text
    assert page.index("Changes") < page.index("Files") < page.index("Find")
    for path in ("src%2Fapp%2Fserver.py", "src%2Fapp%2Fmetrics.py", "src%2Fapp%2Flegacy.py"):
        assert f"/diff?path={path}" in page
    assert "build%2Fout.txt" not in page


def test_a_diff_shows_its_rows_and_an_untracked_file_is_all_new(tmp_path: Path, repo: Path) -> None:
    rig = _rig(tmp_path, repo)
    base = _base(repo)
    changed = rig.http.get(f"{base}/diff?path=src/app/server.py").text
    assert 'src__code--add"><span class="sr-only">added: </span>PORT = 8088' in changed
    assert 'src__code--del"><span class="sr-only">removed: </span>PORT = 8080' in changed
    assert "line=" in changed
    new = rig.http.get(f"{base}/diff?path=src/app/metrics.py").text
    assert new.count("src__code--add") == 5
    gone = rig.http.get(f"{base}/diff?path=src/app/legacy.py").text
    assert "removed: </span>OLD = True" in gone
    assert "open the file" not in gone
    secret = rig.http.get(f"{base}/diff?path=config/.env").text
    assert "may hold secrets" in secret


def test_the_tree_lists_one_folder_and_hides_ignored_files_until_asked(
    tmp_path: Path, repo: Path
) -> None:
    rig = _rig(tmp_path, repo)
    base = _base(repo)
    top = rig.http.get(f"{base}/tree").text
    assert ">src/<" in top
    assert ">build/<" not in top
    assert "server.py" not in top
    everything = rig.http.get(f"{base}/tree?all=1").text
    assert ">build/<" in everything
    inner = rig.http.get(f"{base}/tree?path=src/app").text
    assert "server.py" in inner
    assert "fbadge--modified" in inner
    assert rig.http.get(f"{base}/tree?path=README.md", follow_redirects=False).status_code == 303


def test_go_to_file_ranks_and_caps(tmp_path: Path, repo: Path) -> None:
    rig = _rig(tmp_path, repo)
    page = rig.http.get(f"{_base(repo)}/find?q=srvpy").text
    assert page.index("server.py") < page.index("</ul>")
    paths = [f"pkg/m{n}/server_{n}.py" for n in range(400)]
    found = fuzzy.find("serv", paths)
    assert len(found.paths) == fuzzy.MAX_RESULTS
    assert found.total == 400
    assert fuzzy.find("guide", ["docs/guide.md", "src/g/u/i/d/e.py"]).paths[0] == "docs/guide.md"
    assert fuzzy.find("", paths).paths == ()


def test_an_open_file_says_when_it_changed_on_disk(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "moving")
    rig = _rig(tmp_path, repo)
    base = _base(repo)
    page = rig.http.get(f"{base}/file?path=README.md").text
    marker = "/watch?path=README.md&amp;v="
    version = page[page.index(marker) + len(marker) :].split('"', 1)[0]
    quiet = rig.http.get(f"{base}/watch?path=README.md&v={version}", headers={"HX-Request": "1"})
    assert "Changed on disk" not in quiet.text
    (repo / "README.md").write_text("# moved\n")
    moved = rig.http.get(f"{base}/watch?path=README.md&v={version}")
    assert "Changed on disk" in moved.text
    hidden = rig.http.get(f"{base}/watch?path=README.md&v={version}", headers={"HP-Hidden": "1"})
    assert hidden.status_code == 286


# ---- from an agent, and from chat -----------------------------------------------------------


def test_an_agent_opens_its_own_repository_and_its_paths(tmp_path: Path, repo: Path) -> None:
    rig = _rig(tmp_path, repo)
    agent = f"/agent/{WORKING_SESSION}/files"
    home = rig.http.get(agent, follow_redirects=False)
    assert home.status_code == 303
    assert home.headers["location"] == f"{_base(repo)}?{FROM}"
    line = rig.http.get(f"{agent}?path=src/app/server.py&line=42", follow_redirects=False)
    assert line.headers["location"] == (
        f"{_base(repo)}/file?path=src%2Fapp%2Fserver.py&line=42&{FROM}#L42"
    )
    absolute = rig.http.get(f"{agent}?path={repo}/README.md", follow_redirects=False)
    assert absolute.headers["location"].startswith(f"{_base(repo)}/file?path=README.md")
    assert rig.http.get(f"{agent}?path=/etc/passwd", follow_redirects=False).status_code == 403
    page = rig.http.get(f"{agent}?path=README.md").text
    bar = page[page.index('<header class="topbar">') : page.index("</header>")]
    # A file opened from the Files tab keeps the agent's bar, its Files tab lit.
    assert '<nav class="tabs tabs--3" aria-label="View">' in bar
    assert 'aria-current="page"><span class="tab__label">Files</span>' in bar
    assert 'aria-label="Repository"' not in bar
    # The same stats strip as on Chat and Pane, under the same bar.
    assert f'data-hp-topic="stats:{WORKING_SESSION}"' in bar
    # Changes | Files | Find is a row in the page, and the way back to the tab's lists.
    main = page[page.index("<main ") :]
    assert '<nav class="tabs tabs--3 subtabs" aria-label="Repository">' in main
    assert f'href="{_base(repo)}?{FROM}" data-hp-replace' in main
    assert rig.http.get("/agent/not-a-session/files").status_code == 404


def test_an_agent_outside_any_repository_has_no_files(tmp_path: Path) -> None:
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    rig = _rig(tmp_path, plain)
    page = rig.http.get(f"/agent/{WORKING_SESSION}/files")
    # The tab stays, so the bar never changes shape; the page says why it is empty.
    assert page.status_code == 200
    assert "not working in a git repository" in page.text
    assert 'aria-current="page"><span class="tab__label">Files</span>' in page.text


def test_a_forged_from_is_dropped_not_linked(tmp_path: Path, repo: Path) -> None:
    rig = _rig(tmp_path, repo)
    page = rig.http.get(f"{_base(repo)}/tree?from=javascript:alert(1)").text
    assert "javascript" not in page
    assert 'aria-label="Back to herd"' in page


# ---- the gate, the cache and the verbs ------------------------------------------------------


def _all_routes(routes: Sequence[BaseRoute]) -> list[APIRoute]:
    found: list[APIRoute] = []
    for route in routes:
        if isinstance(route, APIRoute):
            found.append(route)
        # An included router keeps its own table behind it.
        inner = getattr(route, "original_router", None)
        if isinstance(inner, APIRouter):
            found += _all_routes(inner.routes)
    return found


def _explorer_routes(rig: Rig) -> list[APIRoute]:
    routes = _all_routes(cast(FastAPI, rig.http.app).routes)
    return [r for r in routes if r.path.startswith("/files") or r.path.endswith("/files")]


def test_the_explorer_is_read_only(tmp_path: Path, repo: Path) -> None:
    rig = _rig(tmp_path, repo)
    routes = _explorer_routes(rig)
    assert len(routes) == 9
    assert {m for r in routes for m in r.methods or ()} == {"GET"}
    for verb in ("post", "put", "patch", "delete"):
        # Same-origin, so the gate passes it and the router is what answers.
        response = rig.http.request(
            verb, f"{_base(repo)}/file?path=README.md", headers={"Origin": "http://testserver"}
        )
        assert response.status_code == 405, verb


def test_every_explorer_route_sits_behind_the_identity_gate(tmp_path: Path, repo: Path) -> None:
    rig = _rig(tmp_path, repo, login="")
    anonymous = build_rig(
        tmp_path / "anon", {SNAPSHOT: stand_in(snapshot_frame(), repo)}, headers={}
    )
    base = _base(repo)
    urls = (
        "/files",
        base,
        f"{base}/tree",
        f"{base}/find?q=a",
        f"{base}/file?path=README.md",
        f"{base}/diff?path=src/app/server.py",
        f"{base}/raw?path=assets/logo.png",
        f"{base}/watch?path=README.md&v=1",
        f"/agent/{WORKING_SESSION}/files",
    )
    assert len(urls) == len(_explorer_routes(rig))
    for url in urls:
        response = anonymous.http.get(url, follow_redirects=False)
        assert response.status_code == 401, url
        assert "demo-service" not in response.text


def test_nothing_the_explorer_answers_may_be_stored(tmp_path: Path, repo: Path) -> None:
    rig = _rig(tmp_path, repo)
    base = _base(repo)
    for url in (
        "/files",
        base,
        f"{base}/tree",
        f"{base}/find?q=srv",
        f"{base}/file?path=src/app/server.py",
        f"{base}/file?path=config/.env&reveal=1",
        f"{base}/diff?path=src/app/server.py",
        f"{base}/raw?path=assets/logo.png",
        f"{base}/watch?path=README.md&v=1",
        f"/agent/{WORKING_SESSION}/files?path=README.md",
        f"{base}/file?path=../x",
        f"{base}/file?path=nope.txt",
    ):
        response = rig.http.get(url, follow_redirects=False)
        assert response.headers["cache-control"] == "no-store", url
        assert "etag" not in response.headers, url


# ---- small parts ----------------------------------------------------------------------------


def test_status_reads_renames_untracked_and_the_branch() -> None:
    raw = b"## feature...origin/feature\0 M a.py\0R  new.py\0old.py\0?? notes.txt\0D  gone.py\0"
    status = parse_status(raw)
    assert status.branch == "feature"
    assert [(c.path, c.kind) for c in status.changes] == [
        ("a.py", "modified"),
        ("gone.py", "deleted"),
        ("new.py", "renamed"),
        ("notes.txt", "new"),
    ]
    assert status.under("") == 4
    assert status.of("old.py") is None


def test_sizes_read_as_a_phone_reads_them() -> None:
    assert [human_size(n) for n in (0, 812, 14_000, 3_300_000)] == [
        "0 B",
        "812 B",
        "14 KB",
        "3.1 MB",
    ]


def test_a_path_and_line_in_chat_links_into_the_agents_files(tmp_path: Path, repo: Path) -> None:
    rig = _rig(tmp_path, repo)
    says = "The route is at `src/app/server.py:24`; see docs/guide.md:3 too."
    write_transcript(tmp_path / "transcripts", WORKING_SESSION, [assistant_says(says)])
    chat = rig.http.get(f"/agent/{WORKING_SESSION}/chat", headers={"HX-Request": "1"}).text
    route = f"/agent/{WORKING_SESSION}/files"
    assert (
        f'<a class="file-ref" href="{route}?path=src%2Fapp%2Fserver.py&amp;line=24" data-hp-replace>'
        "<code>src/app/server.py:24</code></a>"
    ) in chat
    assert f'href="{route}?path=docs%2Fguide.md&amp;line=3"' in chat
    landed = rig.http.get(f"{route}?path=src%2Fapp%2Fserver.py&line=24")
    assert 'id="L24" data-hp-at' in landed.text


def test_too_many_bytes_is_paged_even_when_the_lines_are_few(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "wide")
    wide = "x = '" + "y" * 150 + "'\n"
    (repo / "wide.py").write_text(wide * 2000)
    assert (repo / "wide.py").stat().st_size > 256 * 1024
    rig = _rig(tmp_path, repo)
    page = rig.http.get(f"{_base(repo)}/file?path=wide.py").text
    assert not _holds(page, f'id="L{PAGE_LINES + 1}"')
    assert not _holds(page, "tok-")
    assert _holds(page, "page 1 of 4")


# ---- git never runs a command the repository names ----------------------------------------


def test_no_command_a_repository_configures_is_ever_run(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "trapped")
    marks = tmp_path / "marks"
    booby_trap(repo, marks)
    # Dirty again after the trap is set, so status and diff have content to compare.
    (repo / "src/app/server.py").write_text("PORT = 9\n")
    (repo / "README.md").write_text("# moved\n")
    rig = _rig(tmp_path, repo)
    base = _base(repo)
    for url in (
        "/files",
        base,
        f"{base}/tree",
        f"{base}/tree?path=src/app",
        f"{base}/find?q=serv",
        f"{base}/diff?path=src/app/server.py",
        f"{base}/diff?path=README.md",
        f"{base}/diff?path=src/app/metrics.py",
        f"{base}/file?path=src/app/server.py",
        f"{base}/file?path=README.md",
        f"/agent/{WORKING_SESSION}/files?path=README.md&line=1",
    ):
        assert rig.http.get(url).status_code == 200, url
        assert sorted(p.name for p in marks.iterdir()) == [], url
    assert "PORT = 9" in rig.http.get(f"{base}/diff?path=src/app/server.py").text


def test_a_submodule_never_runs_a_command_its_own_config_names(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "super")
    marks = tmp_path / "marks"
    inside = trapped_submodule(repo, marks)
    rig = _rig(tmp_path, repo)
    base = _base(repo)
    for url in (
        "/files",
        base,
        f"{base}/tree?path=vendor",
        f"{base}/find?q=notes",
        f"{base}/diff?path=vendor",
        f"{base}/diff?path={inside}",
        f"{base}/file?path={inside}",
    ):
        assert rig.http.get(url).status_code == 200, url
        assert sorted(p.name for p in marks.iterdir()) == [], url


def test_a_filter_name_that_cannot_be_blanked_stops_git() -> None:
    raw = b"filter.lfs.clean\ngit-lfs clean\0filter.a.b.process\nx\0"
    assert _filter_names(raw) == ["lfs", "a.b"]
    assert _filter_names(b"filter.x=y.clean\nrm\0") is None
    assert _filter_names(b"") == []
