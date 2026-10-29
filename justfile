# One command, non-zero on any problem. CI runs exactly this recipe.

default: check

# ESLint comes from package-lock.json: run `npm ci --ignore-scripts` once, then this runs offline.
# Cheapest first, so a sub-second guard fails before minutes of pytest and the browser, not after.
check: && skips
    uv run ruff format --check .
    uv run ruff check .
    uv run python scripts/css_tokens.py
    npx --no-install eslint src/herdr_pwa/web/static
    node --test tests/js/*.test.mjs
    uv run basedpyright
    uv run complexipy --quiet src
    uv run lint-imports --no-cache --no-logo
    HP_FIXTURE_USE=1 uv run pytest -q -n auto --cov=herdr_pwa --cov-branch --cov-report= --cov-fail-under=94.7 --junitxml=.pytest_cache/junit.xml
    uv run python scripts/geometry_gate.py

# Fail on a skip whose reason is not "needs <resource>:" for a resource registered as a pytest marker
# in pyproject.toml, and on a pytest.xfail() call under TESTS, which decides from inside a test as a
# skip would; prints how many tests each resource kept from running. REPORT is pytest's JUnit XML,
# which records an expected failure, marked or called, as a skip of its own type.
skips REPORT=".pytest_cache/junit.xml" TESTS="tests":
    #!/usr/bin/env -S uv run --quiet python
    import ast
    import sys
    import tomllib
    import xml.etree.ElementTree as ET
    from pathlib import Path
    with open("pyproject.toml", "rb") as file:
        markers = tomllib.load(file)["tool"]["pytest"]["ini_options"]["markers"]
    registered = {marker.partition(":")[0] for marker in markers}
    kinds: dict[str, int] = {}
    stray: list[str] = []
    for case in ET.parse("{{REPORT}}").iter("testcase"):
        for skip in case.iter("skipped"):
            if skip.get("type") == "pytest.xfail":
                continue
            reason = skip.get("message", "")
            kind = reason.removeprefix("needs ").partition(":")[0]
            if reason.startswith("needs ") and kind in registered:
                kinds[kind] = kinds.get(kind, 0) + 1
            else:
                stray.append(f"{case.get('classname')}::{case.get('name')}: {reason}")
    for path in sorted(Path("{{TESTS}}").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_bytes())):
            call = node.func if isinstance(node, ast.Call) else None
            if isinstance(call, ast.Attribute) and isinstance(call.value, ast.Name):
                if (call.value.id, call.attr) == ("pytest", "xfail"):
                    stray.append(f"{path}:{node.lineno}: pytest.xfail(); mark the test instead")
    print("skipped:", ", ".join(f"{n} needs {k}" for k, n in sorted(kinds.items())) or "none")
    for line in stray:
        print(f"unregistered skip: {line}", file=sys.stderr)
    sys.exit(1 if stray else 0)

# Headless Firefox over the real templates at 390px, 430px and 320px: no sideways scroll, 44px tap
# targets, content inside the viewport. Passes without running when a receipt for this exact tree
# exists in ~/.cache/herdr-pwa/geometry, which every worktree shares. The walks already moved into
# tests/browser are reused the same way, one receipt per test, and counted as reused.
geometry:
    uv run python scripts/geometry_gate.py
    uv run pytest -q -m browser tests/browser

# The same checks with no receipt consulted, for when the fingerprint is not trusted.
geometry-force:
    uv run python scripts/geometry_gate.py --force
    uv run pytest -q -m browser tests/browser --browser-force

# WebKit as an iPhone (390x844 at 3x, touch, Safari's UA), at 430x932 and at 320px: a first-viewport
# and a full-page PNG per catalogue state under .agents/shots/. For eyes, not a gate; skips without
# WebKit.
shots *KEYS:
    uv run python scripts/shots.py {{KEYS}}

# A before | after HTML contact sheet per state key under .agents/sheets/: --left DIR is a folder
# saved earlier with `just shots --out DIR`; --embed --out PATH writes one self-contained file.
sheet *ARGS:
    uv run python scripts/sheet.py {{ARGS}}

# A decision page from a TOML data file: each option shot on catalogue states (this tree, this tree
# under a CSS overlay, or a saved shots folder) beside its pros, cons and effort. One self-contained
# HTML file with no script or outside file, written beside the data file. --wire for wireframes.
compare *ARGS:
    uv run python scripts/compare.py {{ARGS}}

# WebKit as an iPhone: reload a Chat and a Pane, walk forward into one, and reload a page that replaced
# one; each lands where it lands when opened. For eyes, not a gate (Firefox restores before landing).
reload *ARGS:
    uv run python scripts/reload.py {{ARGS}}

# The browser the shots use; a dev tool, fetched into ~/.cache/ms-playwright, never committed.
webkit:
    uv run playwright install webkit

# Print the latest phone device report from the state dir (HP_STATE_DIR, default ~/.config/herdr-pwa).
device-report *args:
    uv run herdr-pwa device-report {{args}}

# Serve on the build port against the throwaway hp-test session, never the default one.
live-serve: live-session
    HP_SOCKET_PATH="$HOME/.config/herdr/sessions/hp-test/herdr.sock" uv run herdr-pwa serve

# A disposable hp-test herdr session with its own server and socket; never the default one.
live-session:
    uv run python scripts/live_session.py up

# Start (or reuse) a cheap claude-on-haiku agent in a pane of its own; --cwd picks its folder.
live-agent *args: live-session
    uv run python scripts/live_session.py agent {{args}}

# The tests marked live, against the hp-test session; the gate skips them as needing it.
live-tests *ARGS: live-session
    HP_LIVE=1 uv run pytest -q -m live {{ARGS}}

# Show the hp-test socket and every agent in it.
live-status:
    uv run python scripts/live_session.py status

# Stop the hp-test session; its workspace returns on the next live-session.
live-down:
    uv run python scripts/live_session.py down
