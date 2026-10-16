"""The geometry gate's browser version comes from the install's application.ini, not a child."""

from pathlib import Path

import pytest

from scripts import firefox

INI = """\
; This file is not used.
[App]
Vendor=Mozilla
Name=Firefox
Version=154.0.1
BuildID=20260824154132
"""


def _install(root: Path, ini: str | None) -> None:
    """A Firefox install on PATH as distros lay it out: a symlink into the program directory."""
    program = root / "lib" / "firefox"
    program.mkdir(parents=True)
    binary = program / "firefox"
    # A binary that fails if run, so a version read that starts it cannot pass.
    binary.write_text("#!/bin/false\n", encoding="utf-8")
    binary.chmod(0o755)
    if ini is not None:
        (program / "application.ini").write_text(ini, encoding="utf-8")
    (root / "bin").mkdir()
    (root / "bin" / "firefox").symlink_to(binary)


def test_the_version_line_is_read_beside_the_resolved_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(tmp_path, INI)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    # The same line `firefox --version` prints, so receipts fingerprinted before still match.
    assert firefox.version() == "Mozilla Firefox 154.0.1"


def test_an_install_without_application_ini_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(tmp_path, None)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    with pytest.raises(firefox.BrowserError, match=r"application\.ini"):
        firefox.version()
