"""One file as the phone shows it: highlighted, rendered, paged, or refused, under fixed caps."""

import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Final

from herdr_pwa.files.reader import Reader, Stat
from herdr_pwa.files.roots import is_secret
from herdr_pwa.web.highlight import highlight, language_for, split_lines
from herdr_pwa.web.markdown import render_tree
from herdr_pwa.web.mdnode import MdNode

# Past either of these a file is paged plain text: lexing runs in Python, on this box, per miss.
HIGHLIGHT_BYTES: Final = 256 * 1024
HIGHLIGHT_LINES: Final = 5000
PAGE_LINES: Final = 500
# A page of long lines is read only this far: a newline-free file is one page however large.
PAGE_BYTES: Final = 1024 * 1024
# The markdown parser costs far more per byte than lexing; brackets or math run to seconds.
MARKDOWN_BYTES: Final = 64 * 1024
# A chunk the phone may skip laying out until it scrolls near (content-visibility: auto).
CHUNK_LINES: Final = 400
# A minified line is cut here; the rest is counted, not drawn.
LINE_CHARS: Final = 2000
# A NUL in the first 8KB is how git decides a file is binary, and so does this.
SNIFF_BYTES: Final = 8192
IMAGE_BYTES: Final = 5 * 1024 * 1024
# Beyond this the paging index alone is too much work for a glance from a phone.
SHOWN_BYTES: Final = 64 * 1024 * 1024
# At most this many highlights run at once; a request waits this long for a turn, then goes plain.
HIGHLIGHT_JOBS: Final = 2
HIGHLIGHT_WAIT: Final = 2.0
CACHE_ENTRIES: Final = 24
_SCAN: Final = 1024 * 1024
_MARKDOWN: Final = frozenset({".md", ".markdown", ".mdown"})
# Magic numbers, so an image is only served as the type its bytes say it is.
IMAGES: Final = {
    ".png": (b"\x89PNG\r\n\x1a\n", "image/png"),
    ".jpg": (b"\xff\xd8\xff", "image/jpeg"),
    ".jpeg": (b"\xff\xd8\xff", "image/jpeg"),
    ".gif": (b"GIF8", "image/gif"),
    ".webp": (b"RIFF", "image/webp"),
}
BUSY_NOTE: Final = "Highlighting is busy with another file, so this one is shown plain."
LONG_PROSE_NOTE: Final = "This file is too long to render as prose, so it is shown as source."
PAGE_CUT_NOTE: Final = "This page is cut at 1 MB; the rest of its lines are not shown."


@dataclass(frozen=True, slots=True)
class Line:
    """One numbered line of a file, as runs of text; ``cut`` counts characters not drawn."""

    n: int
    runs: tuple[MdNode, ...]
    cut: int = 0


@dataclass(frozen=True, slots=True)
class FileView:
    """Everything the file page draws. ``kind`` picks the template's branch."""

    kind: str
    name: str
    size: int
    version: str
    language: str = ""
    lines: tuple[Line, ...] = ()
    prose: tuple[MdNode, ...] = ()
    page: int = 0
    pages: int = 1
    note: str = ""

    @property
    def chunks(self) -> list[tuple[Line, ...]]:
        """The lines in runs of ``CHUNK_LINES``, each laid out only when scrolled near."""
        return [self.lines[i : i + CHUNK_LINES] for i in range(0, len(self.lines), CHUNK_LINES)]

    @property
    def first(self) -> int:
        """The number of the first line shown."""
        return self.lines[0].n if self.lines else 0


def image_type(name: str, head: bytes) -> str:
    """The media type of an image whose name and first bytes agree, else ""."""
    known = IMAGES.get(PurePosixPath(name).suffix.lower())
    if known is None or not head.startswith(known[0]):
        return ""
    if known[1] == "image/webp" and head[8:12] != b"WEBP":
        return ""
    return known[1]


def _cut(text: str) -> tuple[str, int]:
    return (text[:LINE_CHARS], len(text) - LINE_CHARS) if len(text) > LINE_CHARS else (text, 0)


def _plain(texts: list[str], first: int) -> tuple[Line, ...]:
    cuts = [_cut(t) for t in texts]
    return tuple(
        Line(first + i, (MdNode(text=t),) if t else (), c) for i, (t, c) in enumerate(cuts)
    )


def _lines_of(text: str) -> list[str]:
    lines = text.split("\n")
    # A final newline ends the last line rather than starting an empty one.
    return lines[:-1] if lines and not lines[-1] else lines


@dataclass
class _Lru[K, V]:
    size: int
    items: OrderedDict[K, V] = field(default_factory=OrderedDict[K, V])
    lock: threading.Lock = field(default_factory=threading.Lock)

    def get(self, key: K) -> V | None:
        with self.lock:
            if key not in self.items:
                return None
            self.items.move_to_end(key)
            return self.items[key]

    def put(self, key: K, value: V) -> None:
        with self.lock:
            self.items[key] = value
            self.items.move_to_end(key)
            while len(self.items) > self.size:
                self.items.popitem(last=False)


type Key = tuple[str, int, int]


@dataclass(frozen=True, slots=True)
class _Source:
    reader: Reader
    real: Path
    stat: Stat

    @property
    def key(self) -> Key:
        return (str(self.real), self.stat.mtime_ns, self.stat.size)


def _textless(name: str, st: Stat, head: bytes, *, held: bool) -> str:
    """The kind of a file shown without its text, or "" for one whose text is shown."""
    if held:
        return "secret"
    if not st.size:
        return "empty"
    if image_type(name, head) and st.size <= IMAGE_BYTES:
        return "image"
    if b"\0" in head:
        return "binary"
    return "too_large" if st.size > SHOWN_BYTES else ""


@dataclass(frozen=True, slots=True)
class Look:
    """How a file is asked to be shown: the line to open at, past the secret tap, as source."""

    line: int = 0
    reveal: bool = False
    source: bool = False


# A file opened at its top, rendered, with a secret held behind its tap.
FIRST_LOOK: Final = Look()


@dataclass
class Viewer:
    """Builds file views, holding the highlight cache and the limit on highlights in flight."""

    jobs: threading.BoundedSemaphore = field(
        default_factory=lambda: threading.BoundedSemaphore(HIGHLIGHT_JOBS)
    )
    wait: float = HIGHLIGHT_WAIT
    paint: Callable[[str, str, int], tuple[MdNode, ...]] = highlight
    # The chat's own cache is keyed by text and sized for turns, not whole files.
    render: Callable[[str], tuple[MdNode, ...]] = render_tree.__wrapped__
    _code: _Lru[Key, tuple[Line, ...]] = field(default_factory=lambda: _Lru(CACHE_ENTRIES))
    _prose: _Lru[Key, tuple[MdNode, ...]] = field(default_factory=lambda: _Lru(CACHE_ENTRIES))
    _pages: _Lru[Key, tuple[int, ...]] = field(default_factory=lambda: _Lru(CACHE_ENTRIES))

    def open(self, reader: Reader, real: Path, look: Look = FIRST_LOOK) -> FileView:
        """The view of the regular file ``real``; ``look.line`` picks the page of a paged file."""
        st = reader.stat(real)
        base = FileView("", real.name, st.size, st.version)
        # A file held behind the secret tap is never read, not even its first bytes.
        held = is_secret(real.name) and not look.reveal
        head = b"" if held or not st.size else reader.read_range(real, 0, SNIFF_BYTES)
        kind = _textless(base.name, st, head, held=held)
        if kind:
            return replace(base, kind=kind)
        return self._text(_Source(reader, real, st), base, look)

    def _text(self, src: "_Source", base: FileView, look: Look) -> FileView:
        line = look.line
        if src.stat.size > HIGHLIGHT_BYTES:
            return self._paged(src, base, line)
        text = src.reader.read_range(src.real, 0, src.stat.size).decode("utf-8", "replace")
        if look.source or PurePosixPath(base.name).suffix.lower() not in _MARKDOWN:
            return self._code_or_paged(src, base, text, line)
        if src.stat.size > MARKDOWN_BYTES:
            shown = self._code_or_paged(src, base, text, line)
            return replace(shown, note=shown.note or LONG_PROSE_NOTE)
        return self._markdown(src.key, base, text)

    def _code_or_paged(self, src: "_Source", base: FileView, text: str, line: int) -> FileView:
        lines = _lines_of(text)
        if len(lines) > HIGHLIGHT_LINES:
            return self._paged(src, base, line)
        return self._code_view(src.key, base, lines)

    def _markdown(self, key: Key, base: FileView, text: str) -> FileView:
        hit = self._prose.get(key)
        if hit is None:
            # Rendering takes a highlight slot too, so prose cannot crowd out the feed.
            if not self.jobs.acquire(timeout=self.wait):
                plain = _plain(_lines_of(text), 1)
                language = language_for(base.name)
                return replace(base, kind="code", language=language, lines=plain, note=BUSY_NOTE)
            try:
                hit = self.render(text)
            finally:
                self.jobs.release()
            self._prose.put(key, hit)
        return replace(base, kind="markdown", prose=hit)

    def _code_view(self, key: Key, base: FileView, lines: list[str]) -> FileView:
        language = language_for(base.name)
        hit = self._code.get(key)
        if hit is not None or not language:
            return replace(base, kind="code", language=language, lines=hit or _plain(lines, 1))
        if not self.jobs.acquire(timeout=self.wait):
            return replace(
                base, kind="code", language=language, lines=_plain(lines, 1), note=BUSY_NOTE
            )
        try:
            painted = self._paint(lines, language)
        finally:
            self.jobs.release()
        self._code.put(key, painted)
        return replace(base, kind="code", language=language, lines=painted)

    def _paint(self, lines: list[str], language: str) -> tuple[Line, ...]:
        cuts = [_cut(t) for t in lines]
        code = "\n".join(t for t, _ in cuts)
        runs = self.paint(code, language, len(code) + 1)
        if not runs:
            return _plain(lines, 1)
        split = split_lines(runs)
        return tuple(
            Line(i + 1, split[i] if i < len(split) else (), c) for i, (_, c) in enumerate(cuts)
        )

    def _paged(self, src: "_Source", base: FileView, line: int) -> FileView:
        reader, real, key = src.reader, src.real, src.key
        starts = self._pages.get(key)
        if starts is None:
            starts = page_starts(reader, real, key[2])
            self._pages.put(key, starts)
        page = min(max(0, (line - 1) // PAGE_LINES), len(starts) - 1) if line > 0 else 0
        end = starts[page + 1] if page + 1 < len(starts) else key[2]
        raw = reader.read_range(real, starts[page], min(end - starts[page], PAGE_BYTES))
        texts = _lines_of(raw.decode("utf-8", "replace"))
        lines = _plain(texts, page * PAGE_LINES + 1)
        note = PAGE_CUT_NOTE if end - starts[page] > PAGE_BYTES else ""
        return replace(base, kind="paged", lines=lines, page=page, pages=len(starts), note=note)


def page_starts(reader: Reader, real: Path, size: int) -> tuple[int, ...]:
    """The byte offset of the first line of every page of ``PAGE_LINES`` lines."""
    starts = [0]
    seen = 0
    offset = 0
    while offset < size:
        block = reader.read_range(real, offset, _SCAN)
        if not block:
            break
        at = block.find(b"\n")
        while at != -1:
            seen += 1
            if seen % PAGE_LINES == 0 and offset + at + 1 < size:
                starts.append(offset + at + 1)
            at = block.find(b"\n", at + 1)
        offset += len(block)
    return tuple(starts)


_UNITS: Final = ("B", "KB", "MB", "GB", "TB")
_STEP: Final = 1024
# Under this a size keeps one decimal: 3.2 MB, but 14 KB.
_FINE: Final = 10


def human_size(size: int) -> str:
    """A byte count as a phone reads it: 812 B, 14 KB, 3.2 MB."""
    value = float(size)
    unit = 0
    while value >= _STEP and unit < len(_UNITS) - 1:
        value /= _STEP
        unit += 1
    shown = f"{value:.1f}" if unit and value < _FINE else f"{value:.0f}"
    return f"{shown} {_UNITS[unit]}"
