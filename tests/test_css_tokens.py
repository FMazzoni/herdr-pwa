"""The token checker fails a stylesheet with a literal planted in it; the gate runs it on the real one."""

from pathlib import Path
from typing import NamedTuple, Protocol

import pytest

from scripts import css_tokens

ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "src" / "herdr_pwa" / "web" / "static" / "app.css"
TOKENS = ":root {\n  --ink: #DEE8F3;\n  --space-1: 2px;\n}\n"


class Ran(NamedTuple):
    returncode: int
    stdout: str


class Run(Protocol):
    def __call__(self, css: str, allow: str = "") -> Ran: ...


@pytest.fixture
def run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Run:
    """The checker's main() in this interpreter, on a sheet and an allowlist written to tmp."""

    def _run(css: str, allow: str = "") -> Ran:
        sheet, allowlist = tmp_path / "app.css", tmp_path / "allow"
        sheet.write_text(css, encoding="utf-8")
        allowlist.write_text(allow, encoding="utf-8")
        code = css_tokens.main([str(sheet), "--allow", str(allowlist)])
        return Ran(code, capsys.readouterr().out)

    return _run


def test_a_planted_px_fails_with_its_line(run: Run) -> None:
    css = CSS.read_text(encoding="utf-8") + "\n.planted { padding: 7px; }\n"
    result = run(css)
    assert result.returncode == 1
    line = len(css.splitlines())
    assert f"app.css:{line}: '7px' in `padding: 7px`" in result.stdout


def test_a_planted_colour_fails(run: Run) -> None:
    result = run(TOKENS + ".a { color: #fff; }\n.b { background: rgb(0 0 0); }\n")
    assert result.returncode == 1
    assert "'#fff' in `color: #fff`" in result.stdout
    assert "'rgb(' in `background: rgb(0 0 0)`" in result.stdout


def test_tokens_comments_and_selectors_are_not_literals(run: Run) -> None:
    css = (
        TOKENS
        + "/* 44px is the tap */\n#add .x:hover { margin: var(--space-1) 0; opacity: 0.5; }\n"
    )
    assert run(css).returncode == 0


def test_an_allowed_declaration_passes(run: Run) -> None:
    result = run(TOKENS + ".a { width: 1px; }\n", "width: 1px  # a reason\n")
    assert result.returncode == 0, result.stdout


def test_a_stale_allowlist_entry_fails(run: Run) -> None:
    result = run(TOKENS, "width: 1px  # a reason\n")
    assert result.returncode == 1
    assert "allowlist entry matches nothing, remove it: `width: 1px`" in result.stdout


def test_an_entry_without_a_reason_is_refused(run: Run) -> None:
    with pytest.raises(SystemExit, match="give a reason"):
        run(TOKENS + ".a { width: 1px; }\n", "width: 1px\n")
