"""A test is skipped only for want of a resource registered as a marker, and the gate's `skips`
step fails a run that skipped for any other reason."""

import os
import shutil
import subprocess

import pytest

from tests.conftest import REPO

pytest_plugins = ("pytester",)

# Read at import, before the autouse fixture points HOME at a temp dir: uv keeps its cache there.
_HOME = os.environ["HOME"]
_SKIPS = """
import pytest


def test_runs():
    pass


@pytest.mark.xfail(strict=True, reason="a known failure, not a skip")
def test_known_failure():
    assert False


def test_skipped():
    pytest.skip(REASON)
"""


@pytest.mark.parametrize(
    ("reason", "code"),
    [
        pytest.param("needs node: node is not on PATH", 0, id="registered"),
        pytest.param("needs gpu: no card", 1, id="unregistered resource"),
        pytest.param("not a page: a refusal or a redirect", 1, id="no resource"),
    ],
)
def test_the_gate_fails_a_skip_for_an_unregistered_reason(
    pytester: pytest.Pytester, reason: str, code: int
) -> None:
    _ = pytester.makepyfile(test_skips=_SKIPS.replace("REASON", repr(reason)))
    report = pytester.path / "junit.xml"
    pytester.runpytest("-p", "no:cacheprovider", f"--junitxml={report}").assert_outcomes(
        passed=1, skipped=1, xfailed=1
    )
    just = shutil.which("just")
    assert just is not None, "the gate runs under just"
    done = subprocess.run(  # noqa: S603 - the repo's own recipe on a report this test wrote
        [just, "--justfile", str(REPO / "justfile"), "skips", str(report)],
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": _HOME},
        check=False,
    )
    assert done.returncode == code, done.stderr
    if code:
        assert f"test_skips::test_skipped: {reason}" in done.stderr
    else:
        assert "1 needs node" in done.stdout
