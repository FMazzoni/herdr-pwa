"""The token checker fails a stylesheet with a literal planted in it; the gate runs it on the real one."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "css_tokens.py"
CSS = ROOT / "src" / "herdr_pwa" / "web" / "static" / "app.css"
TOKENS = ":root {\n  --ink: #DEE8F3;\n  --space-1: 2px;\n}\n"


def _run(tmp_path: Path, css: str, allow: str = "") -> subprocess.CompletedProcess[str]:
    sheet, allowlist = tmp_path / "app.css", tmp_path / "allow"
    sheet.write_text(css, encoding="utf-8")
    allowlist.write_text(allow, encoding="utf-8")
    command = [sys.executable, str(SCRIPT), str(sheet), "--allow", str(allowlist)]
    return subprocess.run(command, capture_output=True, text=True, check=False)  # noqa: S603


def test_a_planted_px_fails_with_its_line(tmp_path: Path) -> None:
    css = CSS.read_text(encoding="utf-8") + "\n.planted { padding: 7px; }\n"
    result = _run(tmp_path, css)
    assert result.returncode == 1
    line = len(css.splitlines())
    assert f"app.css:{line}: '7px' in `padding: 7px`" in result.stdout


def test_a_planted_colour_fails(tmp_path: Path) -> None:
    result = _run(tmp_path, TOKENS + ".a { color: #fff; }\n.b { background: rgb(0 0 0); }\n")
    assert result.returncode == 1
    assert "'#fff' in `color: #fff`" in result.stdout
    assert "'rgb(' in `background: rgb(0 0 0)`" in result.stdout


def test_tokens_comments_and_selectors_are_not_literals(tmp_path: Path) -> None:
    css = (
        TOKENS
        + "/* 44px is the tap */\n#add .x:hover { margin: var(--space-1) 0; opacity: 0.5; }\n"
    )
    assert _run(tmp_path, css).returncode == 0


def test_an_allowed_declaration_passes(tmp_path: Path) -> None:
    result = _run(tmp_path, TOKENS + ".a { width: 1px; }\n", "width: 1px  # a reason\n")
    assert result.returncode == 0, result.stdout


def test_a_stale_allowlist_entry_fails(tmp_path: Path) -> None:
    result = _run(tmp_path, TOKENS, "width: 1px  # a reason\n")
    assert result.returncode == 1
    assert "allowlist entry matches nothing, remove it: `width: 1px`" in result.stdout


def test_an_entry_without_a_reason_is_refused(tmp_path: Path) -> None:
    result = _run(tmp_path, TOKENS + ".a { width: 1px; }\n", "width: 1px\n")
    assert result.returncode != 0
    assert "give a reason" in result.stderr
