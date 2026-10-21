"""The push guard and the commit-message hook, driven by git itself in throwaway repos."""

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[1] / "scripts" / "hooks"
PRE_PUSH, COMMIT_MSG = HOOKS / "pre_push.py", HOOKS / "commit_msg.py"
# Synthetic stand-in for an owner's identifier; the real list lives outside the repo.
MARKER = "planted-marker"
SUBJECT_72 = "S" * 72


@dataclass(frozen=True, slots=True)
class Rig:
    """A work repo with a bare remote that already has main, and the env every git call uses."""

    work: Path
    env: dict[str, str]

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        """Run git in the work repo, failing the test on a non-zero exit when check is set."""
        command = ["git", "-C", str(self.work), *args]
        return subprocess.run(command, env=self.env, capture_output=True, text=True, check=check)  # noqa: S603

    def commit(self, name: str, text: str, message: str = "Add a file") -> None:
        """Write a file and commit it."""
        (self.work / name).write_text(text, encoding="utf-8")
        self.git("add", name)
        self.git("commit", "-q", "-m", message)

    def push(self, *refspecs: str) -> subprocess.CompletedProcess[str]:
        """Push to the bare remote through the installed pre-push hook."""
        return self.git("push", "origin", *refspecs, check=False)


def _hook(path: Path, script: Path) -> None:
    path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    """A repo whose main is on the remote, the guard installed, and a patterns file of one marker."""
    home, work, remote = tmp_path / "home", tmp_path / "work", tmp_path / "remote.git"
    home.mkdir()
    patterns = tmp_path / "deny"
    patterns.write_text(f"# one marker per line\n\n{MARKER}\n", encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env |= {
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Test Author",
        "GIT_AUTHOR_EMAIL": "author@example.com",
        "GIT_COMMITTER_NAME": "Test Committer",
        "GIT_COMMITTER_EMAIL": "committer@example.com",
        "HP_PUSH_DENY_PATTERNS": str(patterns),
    }
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], env=env, check=True)  # noqa: S603, S607
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], env=env, check=True)  # noqa: S603, S607
    made = Rig(work, env)
    made.git("remote", "add", "origin", str(remote))
    made.commit("README", "hello\n", "Start the project")
    made.git("push", "-q", "origin", "main")
    _hook(work / ".git" / "hooks" / "pre-push", PRE_PUSH)
    return made


def test_a_clean_branch_is_pushed(rig: Rig) -> None:
    rig.git("checkout", "-q", "-b", "feature")
    rig.commit("notes.txt", "plain words\n")
    done = rig.push("feature")
    assert done.returncode == 0, done.stderr


def test_a_v_tag_is_pushed_and_a_deleted_branch_passes(rig: Rig) -> None:
    rig.git("tag", "-a", "v1.0", "-m", "First release")
    assert rig.push("v1.0").returncode == 0
    assert rig.push("main:refs/heads/spare").returncode == 0
    done = rig.push("--delete", "spare")
    assert done.returncode == 0, done.stderr


def test_a_foreign_root_is_refused(rig: Rig) -> None:
    rig.git("checkout", "-q", "--orphan", "rewritten")
    rig.commit("other.txt", "unrelated\n", "Start over")
    done = rig.push("rewritten")
    assert done.returncode != 0
    assert "root commit" in done.stderr


def test_the_stash_is_refused(rig: Rig) -> None:
    (rig.work / "README").write_text("edited\n", encoding="utf-8")
    rig.git("stash", "-q")
    # A plain git server refuses refs/stash too, so the guard's own line is what proves the refusal.
    for refspec in ("refs/stash", "refs/stash:refs/heads/parked"):
        done = rig.push(refspec)
        assert done.returncode != 0
        assert "pre-push refused: refs/stash" in done.stderr


def test_a_tag_outside_v_is_refused_on_a_commit_already_pushed(rig: Rig) -> None:
    # pre-commit skips a push with no new commits; git still hands this ref to the hook.
    rig.git("tag", "browser-wave1")
    done = rig.push("refs/tags/browser-wave1")
    assert done.returncode != 0
    assert "refs/tags/browser-wave1" in done.stderr


@pytest.mark.parametrize(
    ("name", "text", "message", "email"),
    [
        pytest.param("notes.txt", f"a line with {MARKER.upper()}\n", "Add notes", "", id="diff"),
        pytest.param(f"{MARKER}.txt", "plain\n", "Add notes", "", id="file-name"),
        pytest.param("notes.txt", "plain\n", f"Add notes\n\nSeen in {MARKER}.", "", id="message"),
        pytest.param("notes.txt", "plain\n", "Add notes", f"{MARKER}@example.com", id="email"),
    ],
)
def test_a_denied_identifier_is_refused(  # noqa: PLR0913, PLR0917 - pytest passes each parametrize column as its own argument
    rig: Rig, name: str, text: str, message: str, email: str
) -> None:
    if email:
        rig.env["GIT_AUTHOR_EMAIL"] = email
    rig.git("checkout", "-q", "-b", "feature")
    rig.commit(name, text, message)
    done = rig.push("feature")
    assert done.returncode != 0
    assert "deny pattern on line 3" in done.stderr
    assert MARKER not in done.stderr.lower()


def test_a_missing_patterns_file_refuses_unless_switched_off(rig: Rig) -> None:
    del rig.env["HP_PUSH_DENY_PATTERNS"]
    rig.git("checkout", "-q", "-b", "feature")
    rig.commit("notes.txt", "plain\n")
    done = rig.push("feature")
    assert done.returncode != 0
    assert "push-deny-patterns" in done.stderr
    rig.env["HP_PUSH_DENY_PATTERNS"] = "none"
    assert rig.push("feature").returncode == 0


def _commit_msg(tmp_path: Path, message: str) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "COMMIT_EDITMSG"
    path.write_text(message, encoding="utf-8")
    command = [sys.executable, str(COMMIT_MSG), str(path)]
    return subprocess.run(command, capture_output=True, text=True, check=False)  # noqa: S603


SIX = "One. Two. Three. Four. Five. Six."


@pytest.mark.parametrize(
    "message",
    [
        pytest.param(f"{SUBJECT_72}\n", id="subject-72"),
        pytest.param("Keep the subject short\n\nWhy one. Why two? Why three!\n", id="short-body"),
        pytest.param(
            "Drop a stale cache\n\nOne. Two. Three. Four. Five.\nRejected: a. b. c.\n\n"
            "Signed-off-by: A. N. Other <a@example.com>\n",
            id="rejected-and-trailers-uncounted",
        ),
        pytest.param(f"Merge branch 'x' into main\n\n{SIX}\n", id="merge"),
        pytest.param(f"{SUBJECT_72}\n# {SIX}\n# {'x' * 80}\n", id="comments-ignored"),
        pytest.param("Wipe the cache on logout\n", id="wip-only-as-a-word"),
    ],
)
def test_a_good_message_passes(tmp_path: Path, message: str) -> None:
    done = _commit_msg(tmp_path, message)
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize(
    ("message", "reason"),
    [
        pytest.param(f"{SUBJECT_72}x\n", "73 characters", id="subject-73"),
        pytest.param(f"Explain it\n\n{SIX}\n", "6 sentences", id="six-sentences"),
        pytest.param("WIP: half a fix\n", "unfinished", id="wip"),
        pytest.param("fixup! Add a file\n", "unfinished", id="fixup"),
        pytest.param("squash! Add a file\n", "unfinished", id="squash"),
    ],
)
def test_a_bad_message_is_refused(tmp_path: Path, message: str, reason: str) -> None:
    done = _commit_msg(tmp_path, message)
    assert done.returncode != 0
    assert reason in done.stderr


def test_git_runs_the_commit_msg_hook(rig: Rig) -> None:
    _hook(rig.work / ".git" / "hooks" / "commit-msg", COMMIT_MSG)
    (rig.work / "notes.txt").write_text("plain\n", encoding="utf-8")
    rig.git("add", "notes.txt")
    refused = rig.git("commit", "-q", "-m", f"{SUBJECT_72}x", check=False)
    assert refused.returncode != 0
    assert "73 characters" in refused.stderr
    assert rig.git("commit", "-q", "-m", SUBJECT_72, check=False).returncode == 0
