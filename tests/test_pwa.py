"""The installable shell: the worker's never-cache rule, read and run, plus manifest and headers."""

import hashlib
import re
import shutil
import struct
import subprocess
from pathlib import Path

import pytest
from pydantic import JsonValue, TypeAdapter

from herdr_pwa.web.assets import SCRIPTS, STATIC_DIR
from herdr_pwa.web.routers.shell import TOUCH_ICONS
from tests.webfakes import build_rig, client_scripts

WORKER = STATIC_DIR / "sw.js"
MANIFEST = STATIC_DIR / "manifest.webmanifest"
# The manifest and the worker harness's report, read whole as JSON documents.
_DOCUMENT = TypeAdapter(dict[str, JsonValue])
HARNESS = Path(__file__).parent / "sw" / "harness.mjs"
SESSION = "11111111-1111-4111-8111-111111111111"
SHELL_PREFIXES = ("/static/",)
SHELL_PATHS = ("/manifest.webmanifest", "/offline")
# Every name the worker's cache has had, with a digest of the PRECACHE it was named for, newest
# last. A new list under an old name leaves phones on the cache they already hold; a new name over
# the same list throws a good cache away. A new PRECACHE takes a new name and a new line here, and
# a line is never edited: phones in the field still hold caches by the old names.
CACHE_NAMES = (
    ("v3", "f633629dc2d9"),
    ("v4", "9fb11146d0a5"),
    ("v5", "31dfbaa52806"),
    ("v6", "32a6cdfbd0a9"),
)


def precache_paths() -> list[str]:
    source = WORKER.read_text()
    block = source[source.index("const PRECACHE = [") : source.index("];")]
    offline = re.search(r'const OFFLINE_URL = "([^"]+)"', source)
    assert offline is not None
    paths = re.findall(r'"(/[^"]*)"', block)
    if "OFFLINE_URL" in block:
        paths.append(offline.group(1))
    return paths


def test_the_worker_precaches_only_the_static_shell() -> None:
    paths = precache_paths()
    assert len(paths) >= 8
    for path in paths:
        assert path.startswith(SHELL_PREFIXES) or path in SHELL_PATHS, path


def test_every_script_a_page_runs_is_precached_and_linked_in_order(tmp_path: Path) -> None:
    precached = precache_paths()
    for name in SCRIPTS:
        assert f"/static/{name}" in precached, name
    page = build_rig(tmp_path).http.get("/").text
    pattern = re.compile(r'<script src="/static/([^"?]+)\?v=[0-9a-f]+" defer>')
    linked = [m.group(1) for m in pattern.finditer(page)]
    assert [name for name in linked if name in SCRIPTS] == list(SCRIPTS)
    assert linked[0] == "htmx.min.js"


# htmx.org@2.0.10's dist/htmx.min.js as published: the vendored copy is upgraded by replacing the
# file whole and both lines here, never by editing it.
HTMX_VERSION = "2.0.10"
HTMX_SHA256 = "71ea67185bfa8c98c39d31717c6fce5d852370fcdfd129db4543774d3145c0de"


def test_the_vendored_htmx_is_the_published_release() -> None:
    vendored = (STATIC_DIR / "htmx.min.js").read_bytes()
    assert f'version:"{HTMX_VERSION}"'.encode() in vendored
    assert hashlib.sha256(vendored).hexdigest() == HTMX_SHA256


def test_the_cache_name_moves_only_when_precache_does() -> None:
    named = re.search(r'const CACHE_NAME = CACHE_PREFIX \+ "([^"]+)";', WORKER.read_text())
    assert named is not None
    listed = hashlib.sha256("\n".join(precache_paths()).encode()).hexdigest()[:12]
    assert (named.group(1), listed) == CACHE_NAMES[-1]
    assert (
        len({name for name, _ in CACHE_NAMES})
        == len({d for _, d in CACHE_NAMES})
        == len(CACHE_NAMES)
    )


def test_the_worker_names_no_route_that_renders_agent_state() -> None:
    source = WORKER.read_text()
    assert "/feed" not in source
    # The one /agent/ in the file builds a tap target; it is never a cache key or a precache entry.
    assert source.count("/agent/") == 1
    assert '"/agent/" + text' in source
    assert "/files" not in source
    assert not [
        path for path in precache_paths() if path.startswith(("/agent", "/feed", "/push", "/files"))
    ]


def test_the_only_cache_write_is_behind_the_allow_list_and_a_200() -> None:
    source = WORKER.read_text()
    assert source.count("cache.put(") == 1
    navigation = source[
        source.index("function navigation(") : source.index('self.addEventListener("fetch"')
    ]
    assert ".put(" not in navigation
    assert "PRECACHE_PATHS.has(url.pathname)" in source
    assert "response.status === 200" in source
    assert 'key.search = ""' in source
    assert 'request.method !== "GET"' in source
    assert "url.origin !== self.location.origin" in source


def test_the_worker_swaps_itself_only_when_the_page_asks() -> None:
    source = WORKER.read_text()
    install = source[
        source.index('addEventListener("install"') : source.index('addEventListener("activate"')
    ]
    assert "skipWaiting" not in install
    assert source.count("self.skipWaiting()") == 1


def observed() -> dict[str, JsonValue]:
    result = subprocess.run(  # noqa: S603 - fixed argv, our own files
        ["node", str(HARNESS), str(WORKER)],  # noqa: S607 - node from PATH, skipped when absent
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return _DOCUMENT.validate_json(result.stdout)


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="the harness needs node")


@needs_node
def test_the_running_worker_never_answers_for_agent_state_and_never_stores_a_document() -> None:
    seen = observed()
    intercepted = seen["intercepted"]
    assert isinstance(intercepted, dict)
    for probe in ("feed", "chat", "pane", "pushKey", "postToFeed", "crossOrigin"):
        assert intercepted[probe] is False, probe
    # A file's contents are never the worker's to answer: not a watch, an image or a search.
    for probe in ("fileWatch", "fileImage", "fileFind"):
        assert intercepted[probe] is False, probe
    assert seen["fileNavBody"] == "body-of:/files/0123456789ab/file?path=a.py"
    for probe in ("stampedAsset", "unstampedAsset", "manifest", "document", "agentPage"):
        assert intercepted[probe] is True, probe
    assert seen["cachedDocumentKeys"] == []
    assert seen["offlineFeedResponded"] is False
    assert seen["offlineNavBody"] == "body-of:/offline"
    assert seen["offlineAgentNavBody"] == "body-of:/offline"
    assert seen["onlineSecondBody"] == "body-of:/static/app.css?v=2"
    assert seen["partialCached"] is False
    assert seen["decoyProbeType"] == "error"
    assert seen["installFailsWhenAnAssetIsMissing"] is True
    assert seen["installCacheModes"] == ["reload"]


@needs_node
def test_the_running_worker_swaps_only_on_the_pages_message() -> None:
    seen = observed()
    assert seen["skipWaitingBeforeMessage"] is False
    assert seen["skipWaitingOnAnyMessage"] is False
    assert seen["skipWaitingOnRequest"] is True


@needs_node
def test_a_push_shows_the_servers_text_and_a_tap_opens_that_agent() -> None:
    seen = observed()
    shown = seen["blockedNotification"]
    assert isinstance(shown, dict)
    assert shown["title"] == "needs you: fix the parser"
    assert shown["tag"] == "herd"
    assert shown["data"] == {
        "session": SESSION,
        "event": "blocked",
        "url": f"/agent/{SESSION}",
    }
    assert seen["openedWithNoWindow"] == [f"https://box.tail.ts.net/agent/{SESSION}"]
    assert seen["navigatedExisting"] == [f"https://box.tail.ts.net/agent/{SESSION}"]
    assert seen["openedAsWell"] == []
    assert seen["liveAfterThree"] == 1
    assert (seen["liveBeforeClear"], seen["liveAfterClear"], seen["shownByClear"]) == (1, 0, 0)


@needs_node
def test_a_push_is_quiet_when_on_screen_or_told_so_and_never_dropped() -> None:
    seen = observed()
    assert seen["onScreen"] == {"shown": True, "silent": True, "renotify": False}
    assert seen["elsewhereSilent"] is False
    assert seen["backgroundSilent"] is False
    quiet = seen["quietUpdate"]
    assert isinstance(quiet, dict)
    assert (quiet["silent"], quiet["requireInteraction"]) == (True, True)
    assert seen["unreadableTitle"] == "herdr"


@needs_node
def test_a_tap_url_is_never_built_from_a_string_that_is_not_a_session() -> None:
    seen = observed()
    assert (seen["hostileUrl"], seen["paneUrl"], seen["hostilePaneUrl"]) == ("/", "/", "/")


@needs_node
def test_a_rotated_subscription_re_registers_itself() -> None:
    seen = observed()
    assert seen["resubscribePosts"] == ["/push/key", "/push/subscribe", "/push/unsubscribe"]


def test_every_precached_url_is_served_by_this_app(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    for path in precache_paths():
        assert rig.http.get(path).status_code == 200, path


def test_the_worker_is_served_at_root_scope_and_never_cached_long(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    answer = rig.http.get("/sw.js")
    assert answer.status_code == 200
    assert answer.headers["content-type"].startswith("text/javascript")
    assert answer.headers["cache-control"] == "no-cache"
    assert answer.headers["service-worker-allowed"] == "/"
    assert answer.text == WORKER.read_text()


def test_the_manifest_is_served_at_root_with_its_media_type(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    answer = rig.http.get("/manifest.webmanifest")
    assert answer.headers["content-type"].startswith("application/manifest+json")
    assert answer.headers["cache-control"] == "no-cache"
    assert "service-worker-allowed" not in answer.headers


@pytest.mark.parametrize("route", ["/sw.js", "/manifest.webmanifest", "/offline", *TOUCH_ICONS])
def test_the_shell_is_gated_like_everything_else(tmp_path: Path, route: str) -> None:
    rig = build_rig(tmp_path, headers={})
    assert rig.http.get(route).status_code == 401


def png_size(body: bytes) -> tuple[int, int]:
    assert body[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", body[16:24])
    return int(width), int(height)  # pyright: ignore[reportAny]


@pytest.mark.parametrize("route", list(TOUCH_ICONS))
def test_ios_finds_a_home_screen_icon_at_each_root_name(tmp_path: Path, route: str) -> None:
    rig = build_rig(tmp_path)
    answer = rig.http.get(route)
    assert answer.headers["content-type"] == "image/png"
    expected = 120 if "120x120" in route else 180
    assert png_size(answer.content) == (expected, expected)


def test_the_manifest_is_a_standalone_app_rooted_at_slash() -> None:
    manifest = _DOCUMENT.validate_json(MANIFEST.read_text())
    assert (manifest["start_url"], manifest["scope"], manifest["id"]) == ("/", "/", "/")
    assert manifest["display"] == "standalone"
    assert manifest["name"]
    assert manifest["short_name"]


def test_every_manifest_icon_exists_at_its_declared_size() -> None:
    manifest = _DOCUMENT.validate_json(MANIFEST.read_text())
    icons = manifest["icons"]
    assert isinstance(icons, list)
    sizes: set[str] = set()
    for raw in icons:
        icon = raw
        assert isinstance(icon, dict)
        src, size = str(icon["src"]), str(icon["sizes"])
        assert src.startswith("/static/")
        body = (STATIC_DIR / src.removeprefix("/static/")).read_bytes()
        if size != "any":
            width, height = (int(n) for n in size.split("x"))
            assert png_size(body) == (width, height)
        sizes.add(size)
    assert {"192x192", "512x512"} <= sizes


def test_the_offline_page_is_rowless_and_never_stored_for_long(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    answer = rig.http.get("/offline")
    assert answer.headers["cache-control"] == "no-cache"
    assert "offline" in answer.text
    assert "card" not in answer.text
    assert "hx-get" not in answer.text
    assert rig.wire.calls == []


def test_every_page_links_the_root_manifest_and_carries_its_build(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    page = rig.http.get("/")
    assert '<link rel="manifest" href="/manifest.webmanifest">' in page.text
    assert '<link rel="apple-touch-icon" href="/apple-touch-icon.png">' in page.text
    assert f'<meta name="hp-build" content="{page.headers["hp-build"]}">' in page.text


def test_the_update_offer_is_hidden_and_outside_every_polled_region(tmp_path: Path) -> None:
    page = build_rig(tmp_path).http.get("/").text
    assert '<div class="updatebar" id="hp-update" hidden>' in page
    assert page.index('id="hp-update"') < page.index("data-hp-poll")
    assert "updatebar" not in page.partition("data-hp-poll")[2]


def test_the_client_script_builds_no_markup_and_registers_the_root_worker() -> None:
    source = client_scripts()
    for banned in ("innerHTML", "outerHTML =", "insertAdjacentHTML", "eval(", "new Function"):
        assert banned not in source, banned
    assert 'register("/sw.js", { scope: "/" })' in source
    # Only a second, different stamp moves the build; a reload follows only the user's tap.
    assert 'getResponseHeader("HP-Build")' in source
    assert "if (swapping)" in source


def test_the_worker_and_app_post_to_the_same_push_routes() -> None:
    worker, app = WORKER.read_text(), client_scripts()
    for route in ("/push/key", "/push/subscribe", "/push/unsubscribe"):
        assert route in worker
        assert route in app


def test_the_status_bar_is_the_top_bars_colour() -> None:
    # Standalone, iOS paints the status bar in theme-color right above the sticky top bar.
    surface = re.search(r"--surface: (#[0-9A-Fa-f]{6});", (STATIC_DIR / "app.css").read_text())
    assert surface is not None
    base = (STATIC_DIR.parent / "templates" / "base.html").read_text()
    assert f'<meta name="theme-color" content="{surface.group(1)}">' in base
    manifest = _DOCUMENT.validate_json(MANIFEST.read_text())
    assert manifest["theme_color"] == surface.group(1)
