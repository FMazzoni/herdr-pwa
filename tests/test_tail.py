"""Chat history paged by messages, and the tail cache that reads only what was appended."""

from __future__ import annotations

import json
from pathlib import Path

from herdr_pwa.engine.tail import Ask, TailCache, chat_window
from herdr_pwa.engine.transcript import (
    HistoryState,
    Speaker,
    TranscriptTurn,
    is_message,
    read_page,
)
from herdr_pwa.jsontypes import JsonObject
from tests.webfakes import build_rig

SESSION = "6ae1e031-ddb4-4684-ab07-68073f7fec47"
# Measured on a real session: a screenshot result is a single line of a megabyte or more.
SCREENSHOT = "A" * (1_400_000)


def _record(kind: str, uuid: str, content: object) -> JsonObject:
    message: JsonObject = {"role": kind, "content": content}  # pyright: ignore[reportAssignmentType]
    record: JsonObject = {"type": kind, "uuid": uuid, "timestamp": "t", "message": message}
    if kind == "user" and isinstance(content, str):
        record["promptSource"] = "typed"
    return record


def _call(n: int, *, image: bool = False) -> list[JsonObject]:
    """One tool call and its result; an image result carries a screenshot inline."""
    use = {"type": "tool_use", "id": f"tu{n}", "name": "Bash", "input": {"command": f"run {n}"}}
    body: object = (
        [{"type": "image", "source": {"type": "base64", "data": SCREENSHOT}}]
        if image
        else f"output {n}"
    )
    result = {"type": "tool_result", "tool_use_id": f"tu{n}", "content": body}
    return [_record("assistant", f"c{n}", [use]), _record("user", f"r{n}", [result])]


def tool_heavy(messages: int, *, calls: int = 6, image_every: int = 0) -> list[JsonObject]:
    """Messages each followed by a burst of tool calls, as an agent at work writes them."""
    records: list[JsonObject] = []
    n = 0
    for m in range(messages):
        if m % 5 == 0:
            records.append(_record("user", f"h{m}", f"Prompt {m}"))
        else:
            text = [{"type": "text", "text": f"Message {m}"}]
            records.append(_record("assistant", f"a{m}", text))
        for _ in range(calls):
            n += 1
            records += _call(n, image=bool(image_every) and n % image_every == 0)
    return records


def write(path: Path, records: list[JsonObject]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


def append(path: Path, records: list[JsonObject]) -> int:
    data = "".join(json.dumps(r) + "\n" for r in records).encode()
    with path.open("ab") as handle:
        handle.write(data)
    return len(data)


def said(turns: list[TranscriptTurn]) -> list[str]:
    return [t.text for t in turns if is_message(t)]


# Paging by messages


def test_a_page_counts_messages_and_carries_the_tool_calls_between_them(tmp_path: Path) -> None:
    log = write(tmp_path / "log.jsonl", tool_heavy(60))
    page = read_page(log, limit=400, messages=20)
    assert said(page.turns) == [
        f"{'Prompt' if m % 5 == 0 else 'Message'} {m}" for m in range(40, 60)
    ]
    # Six calls and six results after each message: the page did not stop at a turn count.
    assert len(page.turns) == 20 * 13
    assert page.turns[0].text == "Prompt 40"
    assert page.has_more
    assert page.cursor == page.turns[0].at


def test_counting_turns_alone_shows_a_couple_of_messages(tmp_path: Path) -> None:
    """What the old limit did on a tool-heavy log: forty turns, three messages."""
    log = write(tmp_path / "log.jsonl", tool_heavy(60))
    assert len(said(read_page(log, limit=40).turns)) == 3


def test_a_screenshot_longer_than_the_read_window_does_not_end_the_page(tmp_path: Path) -> None:
    log = write(tmp_path / "log.jsonl", tool_heavy(30, image_every=3))
    assert log.stat().st_size > 20 * len(SCREENSHOT)
    page = read_page(log, limit=400, messages=20, max_bytes=64 * 1024 * 1024)
    assert len(said(page.turns)) == 20


def test_an_inline_image_is_named_not_dumped(tmp_path: Path) -> None:
    log = write(tmp_path / "log.jsonl", tool_heavy(1, calls=3, image_every=3))
    page = read_page(log, limit=400)
    results = [t for t in page.turns if t.tool == "result"]
    assert [r.text for r in results][-1] == "[image]"
    assert all("AAAA" not in r.text for r in results)


# The tail cache


def test_a_refresh_reads_only_what_was_appended(tmp_path: Path) -> None:
    log = write(tmp_path / "log.jsonl", tool_heavy(40, image_every=4))
    tails = TailCache()
    first = tails.window(log, Ask(messages=20))
    built = tails.bytes_read
    assert built > 0
    again = tails.window(log, Ask(messages=20, start=first.cursor))
    assert tails.bytes_read == built
    assert [t.uuid for t in again.turns] == [t.uuid for t in first.turns]
    grown = append(log, [_record("assistant", "new", [{"type": "text", "text": "Newest"}])])
    after = tails.window(log, Ask(messages=20, start=first.cursor))
    assert tails.bytes_read - built == grown
    assert said(after.turns)[-1] == "Newest"
    # Held at the reader's start, the window grew rather than slid.
    assert after.turns[0].uuid == first.turns[0].uuid


def test_a_partial_line_is_not_taken_until_it_is_whole(tmp_path: Path) -> None:
    log = write(tmp_path / "log.jsonl", tool_heavy(3))
    tails = TailCache()
    tails.window(log, Ask(messages=20))
    line = json.dumps(_record("assistant", "late", [{"type": "text", "text": "Late"}])) + "\n"
    with log.open("a") as handle:
        handle.write(line[:20])
    assert "Late" not in said(tails.window(log, Ask(messages=20)).turns)
    with log.open("a") as handle:
        handle.write(line[20:])
    assert said(tails.window(log, Ask(messages=20)).turns)[-1] == "Late"


def test_a_rewritten_file_is_read_again(tmp_path: Path) -> None:
    log = write(tmp_path / "log.jsonl", tool_heavy(10))
    tails = TailCache()
    tails.window(log, Ask(messages=20))
    replacement = write(tmp_path / "other.jsonl", tool_heavy(2))
    replacement.replace(log)
    assert said(tails.window(log, Ask(messages=20)).turns) == ["Prompt 0", "Message 1"]


def test_load_earlier_is_the_page_just_above_with_nothing_lost_or_doubled(tmp_path: Path) -> None:
    log = write(tmp_path / "log.jsonl", tool_heavy(50))
    whole = read_page(log, limit=10_000)
    tails = TailCache()
    newest = tails.window(log, Ask(messages=20))
    both = tails.window(log, Ask(messages=20, before=newest.cursor))
    assert len(said(both.turns)) == 40
    assert both.cursor < newest.cursor
    assert [t.uuid for t in both.turns] == [t.uuid for t in whole.turns][-len(both.turns) :]
    third = tails.window(log, Ask(messages=20, before=both.cursor))
    assert said(third.turns)[:2] == ["Prompt 0", "Message 1"]
    assert not third.has_more


def test_a_page_held_at_its_start_survives_a_fresh_cache(tmp_path: Path) -> None:
    """After a restart the reader's start is read again from disk, earlier pages included."""
    log = write(tmp_path / "log.jsonl", tool_heavy(50))
    earlier = TailCache().window(
        log, Ask(messages=20, before=read_page(log, limit=400, messages=20).cursor)
    )
    fresh = TailCache().window(log, Ask(messages=20, start=earlier.cursor))
    assert [t.uuid for t in fresh.turns] == [t.uuid for t in earlier.turns]


def test_the_cache_is_bounded_in_files_and_turns(tmp_path: Path) -> None:
    tails = TailCache(files=2, held=50)
    logs = [write(tmp_path / f"log{n}.jsonl", tool_heavy(10)) for n in range(3)]
    for log in logs:
        tails.window(log, Ask(messages=5))
    assert len(tails._files) == 2  # pyright: ignore[reportPrivateUsage]
    append(logs[2], tool_heavy(30))
    tails.window(logs[2], Ask(messages=5))
    held = tails._files[str(logs[2])]  # pyright: ignore[reportPrivateUsage]
    assert len(held.turns) <= 100


def test_chat_window_starts_over_when_the_conversation_moved(tmp_path: Path) -> None:
    root = tmp_path / "transcripts"
    write(root / "-tmp" / f"{SESSION}.jsonl", tool_heavy(30))
    page = chat_window(
        SESSION,
        agent_kind="claude",
        ask=Ask(messages=20, start=5),
        tails=TailCache(),
        log="another-file",
        root=str(root),
    )
    assert page.moved
    assert len(said(page.turns)) == 20


def test_chat_window_folds_results_and_names_a_missing_log(tmp_path: Path) -> None:
    root = tmp_path / "transcripts"
    write(root / "-tmp" / f"{SESSION}.jsonl", tool_heavy(3))
    page = chat_window(
        SESSION, agent_kind="claude", ask=Ask(messages=20), tails=TailCache(), root=str(root)
    )
    calls = [t for t in page.turns if t.speaker is Speaker.tool]
    assert calls
    assert all(c.result.startswith("output") for c in calls)
    gone = chat_window(
        "5b609dba-d756-493a-a3fc-b4e09bf0a183",
        agent_kind="claude",
        ask=Ask(messages=20),
        tails=TailCache(),
        root=str(root),
    )
    assert gone.state is HistoryState.absent


def test_a_file_rewritten_in_place_is_not_spliced(tmp_path: Path) -> None:
    """Same inode, longer: the old tail no longer lines up, so the window is read afresh."""
    records = tool_heavy(10)
    log = write(tmp_path / "log.jsonl", records)
    tails = TailCache()
    tails.window(log, Ask(messages=20))
    inserted = [*records[:5], _record("assistant", "mid", [{"type": "text", "text": "Mid"}])]
    log.write_text("".join(json.dumps(r) + "\n" for r in [*inserted, *records[5:]]))
    assert said(tails.window(log, Ask(messages=20)).turns) == said(read_page(log, limit=400).turns)


# The route


def _articles(html: str) -> int:
    return html.count('<article class="turn turn--')


def test_the_chat_route_draws_twenty_messages_and_asks_for_more_by_its_start(
    tmp_path: Path,
) -> None:
    write(tmp_path / "transcripts" / "-tmp" / f"{SESSION}.jsonl", tool_heavy(50))
    rig = build_rig(tmp_path)
    first = rig.http.get(f"/agent/{SESSION}/chat", headers={"HX-Request": "true"}).text
    assert _articles(first) == 20
    start = first.split('data-hp-poll="', 1)[1].split('"', 1)[0]
    assert "?start=" in start
    assert "Load earlier messages" in first
    earlier = first.split('data-hp-earlier hx-get="', 1)[1].split('"', 1)[0]
    assert "?before=" in earlier
    both = rig.http.get(earlier.replace("&amp;", "&"), headers={"HX-Request": "true"}).text
    assert _articles(both) == 40
    # Redrawn by its own start, the region keeps the page the reader loaded.
    kept = both.split('data-hp-poll="', 1)[1].split('"', 1)[0].replace("&amp;", "&")
    assert _articles(rig.http.get(kept, headers={"HX-Request": "true"}).text) == 40
    last = rig.http.get(
        both.split('data-hp-earlier hx-get="', 1)[1].split('"', 1)[0].replace("&amp;", "&"),
        headers={"HX-Request": "true"},
    ).text
    assert _articles(last) == 50
    assert "Load earlier messages" not in last
