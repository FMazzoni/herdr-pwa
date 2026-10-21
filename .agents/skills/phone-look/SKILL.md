---
name: phone-look
description: Look at herdr-pwa the way a phone shows it. Shoot catalogue states in WebKit as an iPhone, read the PNGs, judge them against the design rubric, write findings, and make a before/after contact sheet for any template or CSS change. Use before and after touching anything under src/herdr_pwa/web/templates or static, and when asked how a screen looks.
---

# phone-look

The geometry gate in `just check` measures (sideways scroll, 44px taps, spills). It
cannot tell you a screen is ugly, buries the question, or puts the composer out of
thumb reach. This loop is for that: pictures, read by you and by a person.

Screenshots are for eyes. They never gate anything; a pixel diff has no place here.

## The pieces

- **Catalogue**: `tests/catalogue.py`. Every named screen is a `State`: a path, a fake
  runner, and the `ready` selector that proves it drew. A screen reached by a tap has
  `armed` (wait for this) and `act` (the script that taps). The geometry gate runs
  every catalogue state, so a new state is also measured.
- **`just shots [KEYS]`**: WebKit with Safari's UA and touch, as three phones:

  | Folder | CSS px | Scale | Which phone |
  | --- | --- | --- | --- |
  | `iphone` | 390x844 | 3x | the phone the app is designed on |
  | `wide` | 430x932 | 3x | a large phone as a Home Screen app, with a 34px bottom safe-area inset |
  | `narrow` | 320x568 | 2x | the narrowest iPhone still in use |

  Writes `.agents/shots/current/<folder>/KEY.png` (the first viewport: what the phone
  shows without scrolling) and `KEY.full.png` (the whole page). `--out DIR` writes
  elsewhere. The geometry gate measures the same three widths.
- **`just sheet [KEYS] --left DIR [--embed --out PATH]`**: an HTML contact sheet under
  `.agents/sheets/`, one row per key: the shots saved in `DIR` beside this tree's.
  - `--left-label` and `--right-label` name the columns (default `before` | `after`).
  - `--embed --out PATH`: one self-contained file with the PNGs inlined at 1x, small
    enough to open on a phone.
  - `--device wide` (or `narrow`) draws both columns at that phone instead of `iphone`.
    A `--left` folder with no shots at that width says so in its column.
- **`--css FILE` and `--wire`** on `just shots`: lay CSS over the served `app.css` in the
  browser only (repeatable), or the wireframe overlay `scripts/wire.css`, which turns words
  into bars and colour grey so only layout reads. The tree is never edited, so a CSS
  variant needs no branch. A template or JS variant still needs its own tree: shoot it there
  with `--out` and point the compare page at the folder.
- **`just compare DATA.toml [--wire] [--no-shoot] [--out PATH]`**: the decision page. Shoots
  each option on the data file's states and writes `DATA.html` beside it: per state, the
  options side by side; then one card per option with its pros, cons, effort and whether it
  keeps, bends or breaks an assumption. One self-contained file (pictures inlined, no script,
  no font or file from outside, ASCII only), so it renders the same opened locally, on the
  phone or published as an artifact. Shots land in `DATA-shots/`.
- **`just webkit`**: installs the browser. Without it, shots and sheet print why and
  exit 0. If WebKit fails to launch over a missing system library (seen with
  `libavif.so.13` on Ubuntu 22.04 derivatives), install that library, or extract its
  package's `.so*` files into the `sys/lib/` folder of the Playwright WebKit build
  under `~/.cache/ms-playwright/`, which needs no root.

## The loop

1. Shoot what you are about to judge: `just shots KEY...`. Shoot everything when you
   cannot tell what a change touches.
2. Read the PNGs. Start with `iphone/KEY.png`, because the first viewport is the
   claim; then `wide/KEY.png` and `narrow/KEY.png`, then `.full.png` if the page
   scrolls. Check each one is the screen its key names: a blank or error page is a
   broken state, not a design finding.
3. Judge every screen against the rubric below, item by item. Write down only what
   fails, with the key and what you saw.
4. Write findings to `.agents/phone-look/<date>-<topic>.md`, outside the tracked tree.
   Each finding names the state key, the rubric item, what the screen shows, and the
   fix if you have one.
5. For any template or CSS change, make a before/after sheet:
   1. Before editing: `just shots KEY... --out .agents/shots/before-<topic>`.
   2. Make the change.
   3. `just sheet KEY... --left .agents/shots/before-<topic> --embed --out .agents/sheets/<topic>.html`.
   4. Read the sheet: shoot it, or read the shots on each side.
   5. Hand the sheet's path to whoever asked.
6. `just check` still decides green; the geometry gate is in it.

## Drafting variants and a decision page

Never hand-write a comparison page or draw a wireframe in CSS: shoot the real thing.

1. Make a folder outside the tree: `.agents/variants/<topic>/`.
2. Per option that is CSS only, write `<option>.css`: rules laid over `app.css`, using its
   `:root` tokens. An option that needs a template or JS change is built in its own worktree
   and shot there with `just shots KEY... --out <folder>`.
3. Write `<topic>.toml`:

   ```toml
   title = "Status row"            # the page's heading
   intro = "What is being decided"  # optional
   device = "iphone"                # optional: iphone, wide or narrow

   [[area]]                         # one question; repeat for more
   id = "B"
   title = "The status row"
   note = "Why it matters"          # optional
   pick = "one"                     # or "any"
   states = ["chat-working-crowded", "chat-needs-you"]

   [[area.options]]
   id = "B0"
   name = "Today"                   # no css and no shots: this tree as it is

   [[area.options]]
   id = "B1"
   name = "Counts first"
   css = "b1.css"                   # or shots = "../../shots/b1" from another tree
   summary = "One line"
   pros = ["..."]
   cons = ["..."]
   effort = "S-M"
   assumption = "keeps"             # keeps, bends or breaks
   assumes = "Which assumption, if it bends or breaks"
   recommended = true
   ```

4. `just compare .agents/variants/<topic>/<topic>.toml`, read the page, then hand over its
   path or publish it. Re-run after editing the TOML only with `--no-shoot`.
5. The page has no picker: the owner answers with option ids.

## Rubric

Judge every phone screen against each of these:

- The thing that needs you is on the first screen.
- The composer is in thumb reach.
- Destructive actions are set apart from safe ones.
- Long tokens truncate with an ellipsis; they don't wrap across lines.
- Tap targets are at least 44px.
- There is no empty block above the fold.
- Back from anywhere lands on the feed.

Back behaviour and the iOS edge swipe cannot be seen in a still picture, and no Linux
engine reproduces the gesture. Judge back from the links on the page, and leave the
gesture to a device.

## What a shot cannot tell you

- **iOS chrome is not drawn.** Safari's URL bar, the keyboard, the home indicator and
  the standalone safe areas are not in the picture. The viewport is the full 844px
  (932px for `wide`), which is what a Home Screen app gets. The large phone's 34px
  bottom inset is not emulated: `env(safe-area-inset-bottom)` is 0 in the shots, so a
  bar fixed to the bottom is 34px shorter here than on the phone, where its content is
  lifted above the home indicator.
- **WebKit on Linux is not iOS Safari.** It is close for layout. It differs for native
  controls (a `<select>` is drawn in the platform style), fonts (system-ui falls back
  to a Linux face) and scrolling physics.
- **The rig answers a key, and nothing else.** A state stages what the terminal did
  with it through `prime` and `FakeWire.after` (see `_lands` in the catalogue). A
  screen that only exists after a delivered prompt cannot be catalogued until the fake
  runner answers one.

## Adding a state

1. Add a `State` to the right tuple in `tests/catalogue.py`, under a key no other
   state uses.
2. Make `ready` a selector that only this screen draws. `body` proves nothing.
3. Run `uv run pytest tests/test_catalogue.py`, then `just geometry-force`.
4. Run `just shots KEY`, then read the PNG.
