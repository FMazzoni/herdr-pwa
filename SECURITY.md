# Security

herdr-pwa lets a phone type into terminals where AI coding agents are running. A flaw in it
could let someone else read what your agents are doing or give them instructions, so I want
to hear about anything you find.

## Reporting a security problem

<!-- OWNER: enable private vulnerability reporting in the repo settings once it's public. -->
Please report it privately, through GitHub's
[private vulnerability reporting](../../security/advisories/new) form, rather than in a
public issue. A public issue shows everyone the flaw before there is a fix. This is a
personal project that I look after in my spare time, so I can't promise how quickly I'll
reply. Only the latest code on the `main` branch gets fixes; there are no older releases.

Please report it if any of these happen:

- Someone other than you can see or control an agent. "You" means the single Tailscale
  account named in the `HP_TAILNET_LOGIN` setting.
- A tap on your phone sends keys to a different screen from the one your phone showed.
- The app sends keys to an agent without first writing a record to its audit log.
- Text that an agent printed runs in your browser as code, instead of showing as text.
- The `pair` command, which publishes the app to your phone, makes it reachable from
  outside your tailnet (your private Tailscale network).

These are not bugs in herdr-pwa:

- Attacks that need a shell account on the machine running the app. Anyone with one is
  already fully trusted, as "Risks I accept" below explains.
- Your tailnet's access rules, which decide which of your devices can reach the machine.
  You set those in Tailscale, not in this app.
- Bugs in herdr, Tailscale or the agents themselves. Please report those to their projects.

## How the app is protected

This section is a threat model: a list of the ways someone might attack the app, and what
stops each one. It follows STRIDE, a common checklist that sorts threats into six kinds.
Each heading below names its kind in brackets:

- Spoofing is pretending to be someone else, such as you.
- Tampering is changing data or actions, such as the keys a tap sends.
- Repudiation is denying that you did something, such as sending a prompt.
- Disclosure is leaking data, such as your files or your agents' output.
- Denial of service is making the app unusable, such as by flooding it with requests.
- Elevation of privilege is gaining powers that you shouldn't have.

### The path a tap takes

1. Your phone sends the request over Tailscale, which encrypts it.
2. The Tailscale background service on the machine running the app receives it. The
   `tailscale serve` command set that service up to pass requests on to the app. Tailscale
   already knows which account sent the request, and adds a header naming that account.
3. The app receives it on `127.0.0.1:8772` (8772 is the default port; `HP_PORT` changes
   it). That is a loopback address, which only programs on the same machine can reach.
4. The app passes it to herdr, through a socket file that herdr keeps on that machine.
5. herdr types the keys into the agent's terminal.

The app also reads the git repositories your agents work in, to show you files and diffs.
And it sends notifications through the push services that phone browsers use.

### Someone pretending to be you (spoofing)

When `HP_TAILNET_LOGIN` is set, the app acts only on requests whose Tailscale header names
that account. It ignores case, and compares in constant time as routine hygiene, although
the login is not a secret. A request with no header is refused. The app also refuses to
listen on any address but loopback, so no other machine can send it a forged header.

The one exception is `HP_ASSUME_LOGIN`, a setting for scripts on the machine itself. It
lets a request with no header act as the login it names. It works only for requests
addressed to the machine by one of its local names, `127.0.0.1`, `::1` or `localhost`, and
never for ones that come in through Tailscale.

The `pair` command never uses Funnel, Tailscale's feature for sharing a machine with the
whole internet. It also refuses to add the app to a machine that already has Funnel on.

### A web page pretending to be the app (spoofing)

A web page open in a browser on the same machine could make its own web address point at
`127.0.0.1`, and then send requests to the app. This trick abuses the Domain Name System
(DNS), which turns names into addresses, so it is called DNS rebinding.
The app ignores the Tailscale header unless the request is addressed to the machine's own
Tailscale name. The browser sends the page's own web address with each request, never your
machine's `*.ts.net` Tailscale name, so the page can't pass this check.

### Another website sending requests in your name (tampering)

A site you visit could try to make your browser submit a form to the app. Every request
that changes anything must carry an `Origin` header, which browsers set to the site that
started the request. The app refuses the request unless that header names the app's own
address.

### A tap landing on the wrong screen (tampering)

An agent's screen can change while you are reading it. Before typing anything, the app
finds the agent by its conversation rather than by its terminal pane, because herdr reuses
pane numbers. It then reads the screen again and sends only the keys that screen offers.
If the screen has changed, the app refuses the tap.

### herdr changing under the app (tampering)

herdr may change how it talks to other programs in a new version. The app knows exactly
one version of that protocol, and it refuses to type into a herdr that speaks another.
A misread screen could send the wrong keys, so refusing is the safer failure.

### "I didn't send that" (repudiation)

Before it sends anything to an agent, the app writes a record to its audit log: who sent
it, from which address, and what was sent. If the record can't be written, nothing is
sent. So every keystroke the app typed has a record, made before the keystroke. A prompt's
record holds only its length until the app sees the text appear on the pane. Text you type
into a prompt that hides your typing, such as a password prompt, never appears, so it is
never stored.

### Agent text running as code (disclosure)

Agents print whatever their tools and models produce, and some of that could be HTML or
script. The app never inserts agent text into a page as HTML. It builds every page from a
fixed set of elements and escapes all text, so `<script>` shows as those characters. As a
second layer, every page tells the browser to run only the app's own scripts and to load
nothing from any other site.

### Reading files outside a repository (disclosure)

The file browser shows only the git repositories your agents work in. It resolves every
path before opening it, and checks again on the file it actually opened, so a symbolic link
swapped in between can't lead outside the repository. It never shows the `.git` folder or the
places in your home folder where tools keep passwords and keys, such as `~/.ssh`.
A file whose name suggests a secret, such as `.env` or `id_rsa`, stays hidden until you tap
to show it.

### Making the app unusable (denial of service)

The app refuses any request body over 64 KiB, so a huge upload can't exhaust its memory.
It sends notifications to at most 10 devices, and gives up on any push service that takes
over 10 seconds, so one slow service can't hold up the rest for long. One round of
notifications stops after 30 seconds in total and skips any devices left, so the part of
the app that sends notifications never stalls.

### Making the machine send requests for a stranger (elevation of privilege)

When your phone turns on notifications, it gives the app an address to post them to. A
forged address could make your machine send requests anywhere, an attack known as
server-side request forgery. The app posts only to the push services of Apple, Google,
Mozilla and Microsoft, and it doesn't follow redirects.

## Risks I accept

These follow from the design, and I've chosen not to fix them:

- Any user or program on the machine can forge the Tailscale header, because the app
  can't tell `tailscale serve` apart from another local program. They are fully trusted.
- If you leave `HP_TAILNET_LOGIN` unset, anyone your tailnet lets reach the machine can
  control every agent. The app prints a warning when it starts.
- If you set `HP_ASSUME_LOGIN`, any request from the machine itself that has no Tailscale
  header, addressed to `127.0.0.1`, `::1` or `localhost`, acts as that login. As a second
  safeguard, the app refuses to start with it while `tailscale serve` is publishing
  anything. It checks only at startup, so it won't notice if you run `pair` afterwards.
- Whoever the app accepts, meaning your Tailscale account, has the same power as a shell
  on the machine. The app passes prompts through as typed, and agents do what they're told.
- The file browser's limits go by path name, so a hard link inside a repository to a file
  outside it is still shown.
- When the app starts opencode with a first prompt, the prompt goes on opencode's command
  line, where other users on the machine can read it.
- Push services can see when a notification is sent, but not what it says, because the
  content is encrypted.
- The audit log keeps up to 512 characters of each prompt the pane showed back, in a file
  only your user can read.
