# Invariants

What the app must do and refuse to do around the engine's decisions, which the
golden ledger cannot hold. Each rule is one line, followed by what was tried and
rejected, so it is not proposed again. How a rule is built belongs in the code's
comments, next to the code that keeps it.

## Write path

Anything that reaches a terminal has no undo. Every write follows this order, and
every step before the send may refuse.

1. **Key every request on the session, never the pane.** Pane ids are reused. A
   swapped session and pane id raises. An empty session matches nothing.
2. **Resolve against a snapshot taken in this request.** A failed refresh is a 503,
   `GONE` a 404, `MOVED` is followed. A session whose store says it started in a
   directory unrelated to the pane's cwd is doubted: 409.
3. **Check the session again on the wire**, ctrl-c included.
4. **The write guard lives in the lowest runner.** A cached "protocol ok" lasts about
   10s.
5. **Read the pane inside the request, before sending.** Refuse a prompt if the
   screen shows a question, if it is not the screen the composer was drawn against,
   or if it cannot be read.
   - Rejected: refusing whenever the agent is blocked. A menu's free-text row is
     reached by its digit, and a working agent queues prompts legitimately.
6. **The destructive-input confirm is asked on the server, after step 5.** A verb
   counts only at the start of a command, after any wrapper (`env`, `nohup`,
   `xargs`, ...) is stripped. There is no allow-list.
   - Rejected: matching a verb anywhere in a line. Prose to an agent says "the rm -rf
     in the Makefile", and a confirm that fires on prose gets tapped through.
7. **A screen heuristic may change what the app says and stores, never what it
   sends.**
   - Rejected: "it is a password prompt, so skip verification and press Enter".
   - Accepted exception: opencode's colour-only cursor may make a menu's rows live
     (rule 9). It never decides which key a row sends.
   - Rejected: a coloured row anywhere on the screen (inline code shares the
     colour), and colour from a second read (the two reads disagree).
8. **Read back after the send; never re-send.** An ack means herdr took the bytes.
   An unconfirmed prompt is "sent, unverified", and the same text is held until
   "Send again anyway". A dialog answer is believed gone after two reads, moved
   after two reads of the same other question. An unreadable read settles nothing
   before the 5s budget.
   - Rejected: treating "blocked, nothing parsed" as gone. A parse miss is not
     absence.
9. **Keys come only from what the screen offers, recomputed inside the request.**
   The allowed set is the generic pad plus the footer's hints, minus hand-off keys
   (`HANDOFF_VERBS`). Digits only on a corroborated single-choice menu printed
   whole. Every control carries the screen's fingerprint, with an unparsed
   screen's digits masked.
   - Rejected: dropping hand-off hints at parse time. Plan approval's only hint is
     `ctrl-g`, so it would stop counting as answerable.
   - Rejected: navigation computed from an observed focus (sending N Downs
     from one read). It runs blind after that read, and by rule 7 a heuristic
     never decides what is sent. The screen's own arrows reach the same rows one
     tap at a time.
10. **ctrl-c takes two taps.** It skips the fingerprint and `interactive_ready`
    checks, not the session check or `launch_pending`.
11. **After a key, read again before drawing the controls.**
12. **A close needs a confirm the route enforces.** A working agent needs a second
    acknowledgement. A workspace close's second tap carries a digest of every agent
    it was shown.
13. **Text in the terminal's input box is not the user's**, and is never shown as
    theirs.
14. **A GET never writes anything that matters.** Marking seen is a POST.
15. **Back and End are disabled while a send is in flight.**
16. **Every write takes the one-at-a-time lock**: by session, by pane for a first
    prompt, by workspace for workspace writes and starts, and one key for making a
    workspace. A second write is refused 409.
    - Rejected: locking prompts and keys only, keying a start on its pane or cwd, a
      replay ledger, and waiting for the lock.

## Auth and pairing

- **Authentication is the `Tailscale-User-Login` header; there is no token.**
  - Rejected: pairing and device tokens, unless a non-Tailscale front door
    ever exists.
- **Fail closed on every path, reads and `/static` included**, in an outer
  middleware before routing. Only `/healthz` is open.
- **A request body is capped at 64 KiB**, counted as it streams.
  - Rejected: 16 KiB, which refuses a legitimate non-ASCII prompt.
- **`serve` refuses a non-loopback bind.**
- **A pinned login is compared case-insensitively in constant time**; a mismatch is
  a 403, logged to the operator only.
- **Never trust the header because the peer is loopback.** `tailscale serve`
  proxies from loopback. Any local uid is fully trusted.
- **Same-origin on every write method**: `Origin` host equals `Host`. No dev-mode
  exemption.
- **An assumed login is a named login confined to a literal loopback Host**, and
  refuses to start while `tailscale serve` is active.
- **Serve detection fails closed**: anything but no socket or a refused connection
  is undecidable.
- **Pairing uses tailscaled's LocalAPI with one writer, serve only, never Funnel,
  compare-and-swap and idempotent.** Never run `tailscale serve reset`.

## Audit log

- **Writes only (prompt, keys, start, close), as two records per write.** The
  attempt is flushed before the runner is called, and an unrecordable attempt
  refuses the write. The outcome fails loud, never closed.
- **A box that cannot record does not serve.** No setting turns it off.
- **Payloads are verbatim up to 512 characters; a no-echo prompt's is not stored.**
  This is decided from the screen, never by guessing.
- **0600, in a 0700 directory, two 5 MiB generations.**
  - Rejected: dated files, which need a reaper.

## Push and the notifier

- **Fire on needs-you and on finished-and-unread; clears never fire.** "Finished"
  is the app's own unread ledger, not only herdr's `done`.
- **Needs-you is more than herdr's `blocked`.** A stop on a full context (from the
  transcript's structured refusal) and a stop whose screen holds a corroborated
  dialog both count. `decisions.read_status` is the one place, so every surface
  and the push agree.
  - Rejected for the full context: matching the screen (forgeable by any agent
    printing the words), a herdr signal (none exists), reading working agents'
    logs, herdr's `working` as the clear (retries flap), and keying the push on
    the refusal's uuid.
- **A cold start primes silently.** A genuinely new blocked agent still fires.
- **One notification for the herd, on one tag**, buzzing only for a new alert.
- **Do-not-disturb cannot silence `blocked`, and a clear is never suppressed.**
  Preferences live on the box.
- **A managed agent's finish is not pushed**; its `blocked` always is.
- **The page being looked at is silenced in the service worker, not the server.**
  - Rejected: a server-side "I am looking" flag, which outlives the visibility.
- **The payload is hostile text**: escapes stripped, control and bidi characters
  mapped, URLs replaced, and lengths capped.
- **The VAPID key is created once and never re-minted.**
- **The notifier is a diff loop**: events only wake it early.

## Live updates

- **The event stream carries invalidations, never markup.** A region refetches its
  own route.
  - Rejected: WebSockets, pushing rendered HTML, and htmx's SSE swaps.
- **One watcher and one herdr connection however many pages**, diffing what each
  region shows. A transcript's change is its identity and size, not its mtime.
  - Rejected: tree-wide transcript watching, and a connection per page.
- **A lost stream costs latency, never staleness.** The page falls back to a
  20s poll.
- **Nothing on the way buffers the stream.**
- **An open stream must not cost the feed its back-forward cache**: the stream
  closes before a navigation starts.
- **Following the end is a pinned state, not a position.** Nothing is swapped
  under a finger or within 250ms of a scroll. The dock re-pins only when the
  keyboard's or the dock's height changes, never on a pan.
  - Rejected: a 120px "near the end" band, a smaller slack, unpinning only on an
    upward scroll, and loosening the walks' timing.
- **A question waits at the end, by the thumb**, or with its top under the bar if
  it is taller than the room.
  - Rejected: a fixed bottom sheet, and the question at the top.

## Chat history

- **A page counts messages, not turns.** The window widens until a long record fits.
  - Rejected: raising the turn limit.
- **"Load earlier messages" holds the reader and unpins.**
- **A transcript is read once, then only what was appended**, sealed against
  rewrites.
- **The page arrives with its newest line drawn in.**
  - Rejected: a skeleton loader, `content-visibility` on turns (the end moves after
    landing), and caching rendered turns.
- **A restart warms the most urgent agents.** `HP_PREWARM=false` turns it off.

## Stats strip

- **Only what the harness recorded; a missing number is left out, never zero.**
  - Rejected: a price table for Claude, because a stale table is a confident wrong
    number.
- **It refreshes through the event stream**, and a Claude tally grows by appends.

## Navigation and history

- **Back always lands on the feed.** Only the feed's own links push; everything else
  replaces.
  - Rejected: `HX-Redirect` (it pushes), and history that mirrors depth.
- **Only the rowless feed shell may enter the back-forward cache.** Everything with
  agent state is `no-store`.
  - Rejected: making every page `no-cache`.
- **No sideways scroller starts inside the 16px gutter.**

## Service worker

- **Never cache agent content; precache only an allow-list.**
- **Cache keys drop their query string.**
- **Same-origin GETs only, put on 200 only.**
- **`skipWaiting` only from a message.**
- **Push is shown "on" only when the server confirms it.**

## CSP and escaping

- **One assigned policy on every response; only `'self'` or `'none'`.**
- **No inline script or style; htmx is vendored.**
- **htmx expression filters do nothing under this CSP**: stop polling on the server.
- **Autoescape everywhere; client JS never uses `innerHTML`.**
- **Markdown and ANSI never become markup.**
- **Artifacts have their own literal policy and an empty sandbox.**
  - Rejected: a second origin on a second port.

## herdr drift and a stale server

- **Pin the snapshot protocol.** On a mismatch reads warn and writes fail closed,
  and `doctor` fails rather than warns.
- **Bump the pin only after diffing the schema and driving one real write.**
  - Rejected: bumping the constant alone, and re-vendoring fixtures from the live
    herd.
- **The detection manifest moves on its own.** Grade the remote one.
- **A stale server is a hard failure.** Static files and templates are read once at
  startup. `doctor` fails when the content fingerprint of the tree the service
  started from has moved.
  - Rejected: templates reloading with detection only, a restart-on-change watcher,
    mtimes, a static-only stamp, and the fingerprint over loopback HTTP.
- **Never take herdr's `ok` on a session report as evidence it took effect.**
- **Under test, every destination is a throwaway**, enforced by walking
  `app.state`.

## Harness quirks

- **opencode needs `permission.bash: "ask"`**, or it never blocks.
- **A claude status is herdr's heuristic, not a report.**
- **opencode and hermes have no session until their first prompt.**
- **Never run a bare nested harness in an agent pane.**
- **Match background jobs by pid lineage, not `HERDR_PANE_ID`.**
- **hermes's approval dialog puts options mid-panel and a countdown on the footer**,
  and herdr can call it `done`.
  - Rejected: relaxing the trailing-menu rule.
- **Widen what the parser sees, never what it claims.**
- **opencode's first prompt goes on its command line, in a tab with `HISTFILE=''`.**
  Typing stays for later prompts and other harnesses.
  - Rejected: plain argv (the prompt lands in shell history), and typing (it
    refused under load).
  - Accepted: the prompt is visible in `/proc/<pid>/cmdline` for the agent's life.
- **opencode's models are declared in `HP_OPENCODE_MODELS`**; an undeclared model
  is refused.
  - Rejected: every file-based catalogue, since each mis-read and a dead model
    silently runs the paid default.
