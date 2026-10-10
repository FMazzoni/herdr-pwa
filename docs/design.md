# Design

How the phone screens are built, and what was tried and rejected. The tokens
are documented in `app.css`'s `:root`, and the macros in `_ui.html`.

## Rubric

Every phone screen is judged against all of these:

- The thing that needs you is on the first screen.
- The composer is in thumb reach.
- Destructive actions are separated from safe ones.
- Long tokens truncate with an ellipsis; they do not wrap across lines.
- Tap targets are at least 44px.
- There is no empty block above the fold.
- Back from anywhere lands on the feed (`docs/invariants.md`, Navigation and history).

## Rules

- **Every colour and length in `app.css` is a token in `:root`.**
  `scripts/css_tokens.py` fails any literal outside it, and an exception needs a
  line in `scripts/css_tokens.allow` with its reason.
- **Repeated pieces are macros in `_ui.html`, imported as `ui`.** No template
  writes a macro's classes by hand; the macro grows a parameter instead. A
  history rule travels as `attrs=replace()`, never as a flag, so the design check
  can read every link.
- **Type follows iPhone Text Size**: every size is in `rem`, and on iOS the root is
  `-apple-system-body`. The geometry gate reruns every state at 130% and 200%.
  - Rejected: bigger fixed pixel sizes, which still ignore the setting.
- **Every text colour reads at WCAG AA on every ground**, which `tests/test_a11y.py`
  computes from `:root`.
  - Rejected for now: light and high-contrast themes.
- **One live region, `#hp-live`, outside every redrawn region.** A redraw is
  `aria-busy` and keeps focus.
- **Reduce Motion stops all motion.** The status bubble also has a per-device
  "Stop the animation" switch (WCAG 2.2.2).

## The agent page

- **Chat | Pane | Files is in the top bar.**
  - Rejected: a second fixed row (about 52px on every page), icon buttons in the
    dock, and a fourth bar control.
- **The stats strip is one quiet line under the bar, and its 44px tap box reaches
  below it.**
  - Rejected: an exception to the 44px rule, stats above the prompt, a context ring
    with no strip, and a footer on each reply.
- **Tool calls fold into one quiet line; a failed call is never folded.**
  - Rejected: one line per call with no grouping, and hiding tools behind a toggle.
- **The composer rides the keyboard, measured against `clientHeight`.** WebKit
  reports `innerHeight` as the visible band. The composer's colour runs on under
  iOS's translucent pill.
  - Rejected: `--kb` on the composer (htmx swaps it), a backdrop `--kb` tall, a
    script-placed or fixed backdrop (it trails or is clipped), and one colour for
    `<html>` and `<body>`.
  - Not built, and the fallback if it keeps failing: a `100dvh` column with an
    inner scroller.
  - Outside our code: iOS 26 may leave `visualViewport` offset after the keyboard
    closes (WebKit bug 297779).
- **A status bubble says the agent is working**, naming the open tool call or else
  one verb per turn. It goes quiet after 90s with no tool call open. Palettes and
  motions are presets in `app.css` and `hp.js`, with the defaults named in
  `web/chat.py`.
  - Rejected: the first small rotating-verb line, and Pretext for measuring it
    (47KB to do what `measureText` already does for one word).

## Chat prose

- **Markdown becomes a node tree from closed sets (`TAGS`, `CLASSES`,
  `MATH_ATTRS`), never a markup string.** Raw HTML is off, and links are
  `http(s)` only.
- Rejected: KaTeX, MathJax, highlight.js and Prism (each builds `innerHTML`,
  against the CSP), Mermaid, raw HTML from model text, and footnotes.

## The file explorer

- **Roots are git work trees an agent stands in.** Secret directories are fenced,
  and URLs name a root by an opaque id.
- **Every path is resolved and checked again on the opened descriptor.**
- **A name that may hold a secret is not read until "Show it anyway".**
- **Nothing is cached, and there is no write route.**
- **git is read in-process through libgit2, so no command a repository names ever
  runs.** `tests/test_git_reads.py` holds it to the git CLI's output, and the
  package spawns no process.
  - Costs: no timeout or cancel, slower on huge trees, reads `/etc/gitconfig`.
  - Rejected: `Repository.status()` (no renames), `Repository.diff("HEAD")` (skips
    the index), and, with the CLI, `--attr-source`.
- Rejected: mirroring herdr's TUI file viewers, highlighting in the browser, every
  cwd as a root, inotify watching, ETag and 304 under `no-store`, editing from the
  phone, sideways-scrolling code by default, and line numbers as 44px links.

## Changing the design

1. Shoot before: `just shots KEY... --out .agents/shots/before-<topic>`.
2. Edit one token or one macro. Add a token rather than write a literal.
3. Make the sheet: `just sheet KEY... --left .agents/shots/before-<topic> --left-label
   before --right-label after --embed --out .agents/sheets/<topic>.html`, and judge
   it against the rubric.
4. A person looks at it on the phone. Nothing merges on a picture nobody has
   seen.
5. `just check` must exit 0. The sheet is for eyes and gates nothing.
