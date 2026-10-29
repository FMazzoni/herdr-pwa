"""A secret lands whole at 0600, through a scratch file no one else can predict."""

import os
import stat
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path

import pytest

from herdr_pwa import statedir


def test_a_planted_scratch_name_is_never_written_through(tmp_path: Path) -> None:
    decoy = tmp_path / "decoy"
    _ = decoy.write_text("untouched")
    secret = tmp_path / "secret.json"
    (tmp_path / "secret.json.tmp").symlink_to(decoy)
    statedir.write_secret(secret, "token")
    assert decoy.read_text() == "untouched"
    assert not secret.is_symlink()
    assert secret.read_text() == "token"
    assert stat.S_IMODE(secret.stat().st_mode) == statedir.FILE_MODE


def test_the_bytes_reach_the_disk_before_the_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = tmp_path / "secret.json"
    synced: list[bool] = []
    real = os.fsync

    def fsync(fd: int) -> None:
        synced.append(not secret.exists())
        real(fd)

    monkeypatch.setattr(statedir.os, "fsync", fsync)
    statedir.write_secret(secret, "token")
    assert synced == [True]
    assert [p.name for p in tmp_path.iterdir()] == ["secret.json"]


def test_a_failed_write_leaves_no_scratch_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fsync(_fd: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(statedir.os, "fsync", fsync)
    with pytest.raises(OSError, match="disk full"):
        statedir.write_secret(tmp_path / "secret.json", "token")
    assert list(tmp_path.iterdir()) == []


def test_a_lock_file_whose_mode_drifted_is_put_back_and_still_locks(tmp_path: Path) -> None:
    path = tmp_path / "store.lock"
    path.touch(mode=0o644)
    path.chmod(0o644)
    with statedir.locked(path):
        assert stat.S_IMODE(path.stat().st_mode) == statedir.FILE_MODE


def test_a_lock_held_past_the_limit_raises_rather_than_waiting_forever(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(statedir, "LOCK_TIMEOUT", 0.05)
    path = tmp_path / "store.lock"
    # flock conflicts between descriptors, so one process stands in for two.
    with (
        statedir.locked(path),
        pytest.raises(TimeoutError, match="stayed locked"),
        statedir.locked(path),
    ):
        pass
    with statedir.locked(path):
        pass


def test_two_updates_at_once_both_land(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "store.txt"
    lock = statedir.locked
    first_read, second_ready = threading.Event(), threading.Event()

    @contextmanager
    def waiting(at: Path) -> Generator[None]:
        # The second update has reached the lock: it may now read only what the first wrote.
        if threading.current_thread().name == "second":
            second_ready.set()
        with lock(at):
            yield

    def adding(name: str) -> Callable[[str | None], tuple[str, None]]:
        def change(text: str | None) -> tuple[str, None]:
            if name == "first":
                first_read.set()
                _ = second_ready.wait(5)
            else:
                second_ready.set()
            return (text or "") + f"{name}\n", None

        return change

    monkeypatch.setattr(statedir, "locked", waiting)
    first = threading.Thread(target=statedir.update, args=(path, adding("first")), name="first")
    second = threading.Thread(target=statedir.update, args=(path, adding("second")), name="second")
    first.start()
    assert first_read.wait(5)
    second.start()
    first.join(5)
    second.join(5)
    assert sorted(path.read_text().splitlines()) == ["first", "second"]
    assert stat.S_IMODE(path.stat().st_mode) == statedir.FILE_MODE
