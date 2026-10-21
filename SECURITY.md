# Security

herdr-pwa can type into terminals that run AI agents, so a hole in it is worth hearing
about. The short version is the README's [Security model](README.md#security-model).

## Reporting

<!-- OWNER: enable private vulnerability reporting in the repo settings once it's public. -->
Please report privately through GitHub's
[private vulnerability reporting](../../security/advisories/new), not in an issue. This
is a personal project: I handle reports when I can, with no SLA. Only `main` is supported.

Worth reporting: anyone but `HP_TAILNET_LOGIN` reading or driving an agent, a tap landing
on another screen than the one shown, a write with no audit record, agent text running as
markup, or `pair` exposing the app beyond the tailnet. Not a bug here: anything that needs
a shell on the machine, your tailnet's ACLs, or bugs in herdr, Tailscale or the agents.

## Threat model

The path is phone → `tailscale serve` (TLS, adds the login header) → the app on
`127.0.0.1:8772` → herdr's socket → an agent's terminal. The app also reads repos on disk
and posts to the browsers' push services.

| | threat | what stops it |
|---|---|---|
| S | another tailnet user, or the public internet | `HP_TAILNET_LOGIN`, compared in constant time; `pair` refuses Funnel, and no header is a 401 |
| S | a web page on the box rebinding a name to loopback | the header counts only on the node's MagicDNS Host (`auth.py`) |
| T | a cross-site write | Origin must equal Host on every write |
| T | a key landing on the wrong screen | session keying, a fresh read, only the keys that screen offers |
| T | herdr changing under the app | a pinned protocol; writes fail closed on a mismatch |
| R | "I didn't send that" | an audit record, with Host and Origin, before every send; no log, no write |
| I | agent text becoming markup | a closed node tree, autoescape, CSP `default-src 'none'`, htmx eval off |
| I | reading outside a repo, or its secrets | paths re-checked on the opened descriptor; `.git` and home credential stores fenced; secret-looking names behind a tap |
| D | big bodies, a stalled push service | a 64 KiB body cap; 10 devices, a 10 s send timeout, a 30 s fan-out budget |
| E | the box posting where a stranger points it | push goes only to Apple's, Google's, Mozilla's and Microsoft's services, with no redirects |

Accepted risks:
- Any local user or process can forge the header and is fully trusted.
- With `HP_TAILNET_LOGIN` unset, anyone your tailnet lets reach the node drives every agent. Startup warns.
- An authenticated user is a shell user: prompts go through verbatim, and agents run what they're told.
- The file fence is by name, so a hard link inside a repo is followed.
- opencode's first prompt is visible in `/proc/<pid>/cmdline` to local users.
- Push services see when an alert fires, not what it says. The audit log keeps up to 512 characters of each prompt, 0600.
