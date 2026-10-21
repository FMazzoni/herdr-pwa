import json
import threading
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pydantic import JsonValue

from herdr_pwa import audit, cli, push, statedir
from herdr_pwa.engine import EngineError, EngineSettings, HerdrClient
from herdr_pwa.notify_state import InterruptStore
from herdr_pwa.push import (
    MAX_BODY_CHARS,
    DeliveryLog,
    SubscriptionStore,
    load_or_create_vapid_keys,
)
from herdr_pwa.settings import Settings
from herdr_pwa.web.ping import (
    DEFAULT_SENDER,
    EXIT_NO_DEVICES,
    EXIT_PARTIAL,
    EXIT_SENT,
    EXIT_UNAVAILABLE,
    EXIT_UNDELIVERED,
    EXIT_USAGE,
    PING_TAG,
    Message,
    ping,
)
from herdr_pwa.web.seams import Seams
from tests.fakes import Clock
from tests.test_client import agent_frame
from tests.test_notifier_loop import Sender
from tests.test_push import SUBJECT, subscribed
from tests.webfakes import FakeWire, Reply

ONE = "https://fcm.googleapis.com/fcm/send/one"
TWO = "https://web.push.apple.com/two"
PANE = "w1:p3"


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "state_dir": tmp_path,
        "socket_path": tmp_path / "no-runner.sock",
        "push_subject": SUBJECT,
    }
    return Settings.model_validate({**base, **overrides})


def _paired(tmp_path: Path, *endpoints: str) -> SubscriptionStore:
    _ = load_or_create_vapid_keys(tmp_path)
    store = SubscriptionStore(tmp_path)
    for endpoint in endpoints:
        _ = store.add(subscribed(endpoint))
    return store


class _Runner:
    """A fake runner answering agent.get, and the sockets it was asked on."""

    def __init__(self, reply: Reply) -> None:
        self.wire: FakeWire = FakeWire({"agent.get": reply})
        self.sockets: list[Path] = []

    def __call__(self, engine: EngineSettings) -> HerdrClient:
        self.sockets.append(engine.socket_path)
        return HerdrClient(engine, transport=self.wire)


@dataclass(frozen=True, slots=True)
class _Rig:
    """What a ping sees outside itself: the sender, the runner and the pane's environment."""

    sender: Sender = field(default_factory=Sender)
    runner: _Runner | None = None
    environ: dict[str, str] = field(default_factory=dict[str, str])


def _ping(
    tmp_path: Path, message: Message, rig: _Rig | None = None, settings: Settings | None = None
) -> tuple[int, list[str]]:
    rig = rig or _Rig()
    out: list[str] = []
    seams = Seams(
        wall=Clock(),
        deliver=rig.sender,
        client_factory=rig.runner,
        write=out.append,
        environ=rig.environ,
    )
    code = ping(settings or _settings(tmp_path), seams, message)
    return code, out


def _records(tmp_path: Path) -> list[dict[str, JsonValue]]:
    text = (tmp_path / audit.AUDIT_FILENAME).read_text()
    return [json.loads(line) for line in text.splitlines()]


def test_a_ping_reaches_every_phone_and_a_tap_opens_the_herd(tmp_path: Path) -> None:
    _ = _paired(tmp_path, ONE, TWO)
    sender = Sender()
    code, out = _ping(tmp_path, Message("waves complete", "waves-pi"), _Rig(sender=sender))
    assert code == EXIT_SENT
    assert out == ["ping: sent to 2 of 2 device(s)\n"]
    assert [endpoint for endpoint, _ in sender.sent] == [ONE, TWO]
    payload = sender.sent[0][1]
    assert payload["event"] == push.EVENT_PING
    assert payload["tag"] == PING_TAG
    assert (payload["title"], payload["body"]) == ("waves-pi", "waves complete")
    # No session, so the service worker opens the dashboard rather than an agent page.
    assert payload["agent"] == ""
    assert payload["quiet"] is False


def test_the_text_is_stripped_of_escapes_and_links_and_capped_as_agent_text(
    tmp_path: Path,
) -> None:
    _ = _paired(tmp_path, ONE)
    sender = Sender()
    hostile = "\x1b[31mdone\x1b[0m\u202e see https://evil.example.com/pay " + "x" * 400
    code, _ = _ping(tmp_path, Message(hostile, "\x1b]0;spoof\x07agent"), _Rig(sender=sender))
    assert code == EXIT_SENT
    payload = sender.sent[0][1]
    body = str(payload["body"])
    assert body.startswith("done see [link] x")
    assert len(body) == MAX_BODY_CHARS
    assert body.endswith("…")
    assert "\x1b" not in body
    assert "\u202e" not in body
    assert payload["title"] == "agent"


def test_text_that_sanitises_to_nothing_is_a_usage_error_and_nothing_is_recorded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _ = _paired(tmp_path, ONE)
    sender = Sender()
    code, _ = _ping(tmp_path, Message("\x1b[2J https://x.com", "me"), _Rig(sender=sender))
    assert code == EXIT_USAGE
    assert sender.sent == []
    assert not (tmp_path / audit.AUDIT_FILENAME).exists()
    assert "nothing left to send" in capsys.readouterr().err


def test_one_audit_attempt_and_outcome_record_the_sender_and_what_the_phone_shows(
    tmp_path: Path,
) -> None:
    _ = _paired(tmp_path, ONE)
    _ = _ping(tmp_path, Message("  waves   complete ", "waves-pi"))
    attempt, outcome = _records(tmp_path)
    assert attempt["phase"] == audit.ATTEMPT
    assert attempt["endpoint"] == audit.PING
    assert attempt["actor_via"] == "cli"
    assert attempt["target"] == "waves-pi"
    assert attempt["payload"] == "waves complete"
    assert attempt["payload_chars"] == len("waves complete")
    assert outcome["id"] == attempt["id"]
    assert outcome["result"] == "sent:1 failed:0 pruned:0"


def test_an_audit_log_that_cannot_be_written_sends_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _ = _paired(tmp_path, ONE)
    (tmp_path / audit.AUDIT_FILENAME).mkdir()
    sender = Sender()
    code, _ = _ping(tmp_path, Message("hi", "me"), _Rig(sender=sender))
    assert code == EXIT_UNAVAILABLE
    assert sender.sent == []
    assert "nothing was sent" in capsys.readouterr().err


def test_no_paired_phone_is_its_own_exit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _ = _paired(tmp_path)
    code, _ = _ping(tmp_path, Message("hi", "me"))
    assert code == EXIT_NO_DEVICES
    assert "no paired phone" in capsys.readouterr().err


def test_no_key_is_refused_and_none_is_minted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = _ping(tmp_path, Message("hi", "me"))
    assert code == EXIT_UNAVAILABLE
    assert "no VAPID key" in capsys.readouterr().err
    # A key minted here would orphan nothing yet, but the service is the one that mints.
    assert not (tmp_path / push.VAPID_FILENAME).exists()


def test_a_corrupt_key_is_refused_without_being_replaced(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / push.VAPID_FILENAME).write_text("{not json")
    code, _ = _ping(tmp_path, Message("hi", "me"))
    assert code == EXIT_UNAVAILABLE
    assert "not valid JSON" in capsys.readouterr().err
    assert (tmp_path / push.VAPID_FILENAME).read_text() == "{not json"


def test_an_unreadable_device_store_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _paired(tmp_path, ONE)
    _ = store.path.write_text("[]")
    code, _ = _ping(tmp_path, Message("hi", "me"))
    assert code == EXIT_UNAVAILABLE
    assert push.SUBSCRIPTIONS_FILENAME in capsys.readouterr().err


def test_push_switched_off_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _ = _paired(tmp_path, ONE)
    sender = Sender()
    settings = _settings(tmp_path, push_enabled=False)
    code, _ = _ping(tmp_path, Message("hi", "me"), _Rig(sender=sender), settings)
    assert code == EXIT_UNAVAILABLE
    assert sender.sent == []
    assert "HP_PUSH_ENABLED=0" in capsys.readouterr().err


def test_a_contact_apple_would_refuse_stops_the_real_sender_arming(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _ = _paired(tmp_path, ONE)
    seams = Seams(wall=Clock(), write=lambda _text: None)
    code = ping(_settings(tmp_path, push_subject=""), seams, Message("hi", "me"))
    assert code == EXIT_UNAVAILABLE
    assert "HP_PUSH_SUBJECT" in capsys.readouterr().err


def test_some_devices_failing_is_partial(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _ = _paired(tmp_path, ONE, TWO)

    def flaky(subscription: push.Subscription, data: str) -> None:
        del data
        if subscription.endpoint == TWO:
            raise push.DeliveryError("push service answered 500", status=500)

    seams = Seams(wall=Clock(), deliver=flaky, write=lambda _text: None)
    code = ping(_settings(tmp_path), seams, Message("hi", "me"))
    assert code == EXIT_PARTIAL
    assert "sent to 1 of 2 device(s); 1 failed" in capsys.readouterr().err
    record = DeliveryLog(tmp_path).load()
    assert record is not None
    assert (record.sent, record.failed) == (1, 1)


def test_every_device_failing_is_undelivered(tmp_path: Path) -> None:
    _ = _paired(tmp_path, ONE, TWO)
    code, _ = _ping(tmp_path, Message("hi", "me"), _Rig(sender=Sender(refuse=503)))
    assert code == EXIT_UNDELIVERED
    assert _records(tmp_path)[-1]["result"] == "sent:0 failed:2 pruned:0"


def test_a_device_the_push_service_retired_is_forgotten_as_the_service_forgets_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _paired(tmp_path, ONE)
    code, _ = _ping(tmp_path, Message("hi", "me"), _Rig(sender=Sender(refuse=410)))
    assert code == EXIT_UNDELIVERED
    assert store.list_all() == []
    assert "forgot 1 the push service retired" in capsys.readouterr().err
    record = DeliveryLog(tmp_path).load()
    assert record is not None
    assert record.pruned == 1


def test_a_snooze_sends_the_ping_without_a_sound(tmp_path: Path) -> None:
    _ = _paired(tmp_path, ONE)
    _ = InterruptStore(tmp_path, clock=Clock()).snooze(600)
    sender = Sender()
    _ = _ping(tmp_path, Message("hi", "me"), _Rig(sender=sender))
    assert sender.sent[0][1]["quiet"] is True


def test_the_title_defaults_to_the_calling_panes_agent_name(tmp_path: Path) -> None:
    _ = _paired(tmp_path, ONE)
    sender = Sender()
    runner = _Runner(agent_frame(pane_id=PANE, title="", name="waves-pi"))
    socket = tmp_path / "pane-runner.sock"
    environ = {"HERDR_PANE_ID": PANE, "HERDR_SOCKET_PATH": str(socket)}
    code, _ = _ping(tmp_path, Message("hi"), _Rig(sender=sender, runner=runner, environ=environ))
    assert code == EXIT_SENT
    assert sender.sent[0][1]["title"] == "waves-pi"
    # The pane id is an address on the runner that started the pane.
    assert runner.sockets == [socket]


def test_outside_a_pane_the_title_is_the_apps_name(tmp_path: Path) -> None:
    _ = _paired(tmp_path, ONE)
    sender = Sender()
    runner = _Runner(agent_frame(name="never-asked"))
    _ = _ping(tmp_path, Message("hi"), _Rig(sender=sender, runner=runner))
    assert sender.sent[0][1]["title"] == DEFAULT_SENDER
    assert runner.sockets == []


def test_a_silent_runner_leaves_the_apps_name(tmp_path: Path) -> None:
    _ = _paired(tmp_path, ONE)
    sender = Sender()
    runner = _Runner(EngineError("no runner"))
    environ = {"HERDR_PANE_ID": PANE}
    _ = _ping(tmp_path, Message("hi"), _Rig(sender=sender, runner=runner, environ=environ))
    assert sender.sent[0][1]["title"] == DEFAULT_SENDER
    assert runner.sockets == [tmp_path / "no-runner.sock"]


def test_a_pane_id_that_is_not_an_address_leaves_the_apps_name(tmp_path: Path) -> None:
    _ = _paired(tmp_path, ONE)
    sender = Sender()
    runner = _Runner(agent_frame(name="never-asked"))
    environ = {"HERDR_PANE_ID": "0b7e4f3a-1c2d-4e5f-8a9b-0c1d2e3f4a5b"}
    _ = _ping(tmp_path, Message("hi"), _Rig(sender=sender, runner=runner, environ=environ))
    assert sender.sent[0][1]["title"] == DEFAULT_SENDER
    assert runner.wire.calls == []


def test_an_explicit_title_never_asks_the_runner(tmp_path: Path) -> None:
    _ = _paired(tmp_path, ONE)
    runner = _Runner(agent_frame(name="waves-pi"))
    _ = _ping(
        tmp_path, Message("hi", "script"), _Rig(runner=runner, environ={"HERDR_PANE_ID": PANE})
    )
    assert runner.sockets == []


def test_the_command_line_reaches_ping_and_says_why_it_cannot_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # No key in this state dir, so the real sender is never armed and nothing leaves the box.
    monkeypatch.setenv("HP_STATE_DIR", str(tmp_path))
    assert cli.main(["ping", "waves complete", "--title", "waves-pi"]) == EXIT_UNAVAILABLE
    assert capsys.readouterr().err.startswith("ping: no VAPID key")


def test_two_stores_on_one_file_never_lose_a_device_to_each_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone, kept, added = (f"https://fcm.googleapis.com/fcm/send/{n}" for n in ("a", "b", "c"))
    # Two instances stand for the service and a ping's CLI: their thread locks are not shared.
    service, cli_store = SubscriptionStore(tmp_path), SubscriptionStore(tmp_path)
    _ = service.add(subscribed(gone))
    _ = service.add(subscribed(kept))
    write = statedir.write_secret
    pruning, release = threading.Event(), threading.Event()

    def held(path: Path, text: str, *, exclusive: bool = False) -> None:
        if threading.current_thread().name == "pruner":
            pruning.set()
            _ = release.wait(5)
        write(path, text, exclusive=exclusive)

    monkeypatch.setattr(statedir, "write_secret", held)
    pruner = threading.Thread(target=cli_store.remove, args=(gone,), name="pruner")
    adder = threading.Thread(target=service.add, args=(subscribed(added),), name="adder")
    pruner.start()
    assert pruning.wait(5)
    adder.start()
    adder.join(0.3)
    release.set()
    pruner.join(5)
    adder.join(5)
    assert [one.endpoint for one in service.list_all()] == [kept, added]


def test_health_written_by_another_process_is_kept(tmp_path: Path) -> None:
    clock = Clock()
    service, cli_log = DeliveryLog(tmp_path, clock=clock), DeliveryLog(tmp_path, clock=clock)
    service.sent()
    clock.advance(1)
    cli_log.failed("push service answered 500")
    clock.advance(1)
    service.sent()
    record = DeliveryLog(tmp_path).load()
    assert record is not None
    assert (record.sent, record.failed) == (2, 1)
