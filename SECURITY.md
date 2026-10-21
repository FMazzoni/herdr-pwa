# Security

herdr-pwa can type into terminals that run AI agents, so a hole in it is worth hearing
about.

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
and posts to the browsers' push services. The rows follow STRIDE, the usual checklist.

| kind | threat | what stops it |
|---|---|---|
| Spoofing | another tailnet user, or the public internet | `HP_TAILNET_LOGIN`, compared in constant time; `pair` refuses Funnel; no header is a 401 unless `HP_ASSUME_LOGIN` is set, and then only on a loopback Host |
| Spoofing | a web page on the box pointing its name at loopback | the header counts only when addressed to the machine's Tailscale name (`auth.py`) |
| Tampering | a cross-site write | Origin must equal Host on every write |
| Tampering | a key landing on the wrong screen | session keying, a fresh read, only the keys that screen offers |
| Tampering | herdr changing under the app | a pinned protocol; writes fail closed on a mismatch |
| Repudiation | "I didn't send that" | an audit record, with Host and Origin, before every send; if it can't be written, nothing is sent |
| Disclosure | agent text becoming markup | a closed element tree, autoescape, CSP `default-src 'none'`, htmx eval off |
| Disclosure | reading outside a repo, or its secrets | paths re-checked on the opened descriptor; `.git` and home credential stores fenced; secret-looking names behind a tap |
| Denial of service | big bodies, a stalled push service | a 64 KiB body cap; 10 devices, a 10 s send timeout, a 30 s fan-out budget |
| Elevation | the box posting where a stranger points it | push goes only to Apple's, Google's, Mozilla's and Microsoft's services, with no redirects |

Accepted risks:
- Any local user or process can forge the header and is fully trusted.
- With `HP_TAILNET_LOGIN` unset, anyone your tailnet lets reach the node drives every agent. Startup warns.
- With `HP_ASSUME_LOGIN` set, any local request to a loopback Host is that login, header or not. `serve` refuses to start with it while `tailscale serve` is active.
- Whoever is signed in has the same power as a shell: prompts go through as typed, and agents do what they're told.
- The file fence is by name, so a hard link inside a repo is followed.
- opencode's first prompt is visible in `/proc/<pid>/cmdline` to local users.
- Push services see when an alert fires, not what it says. The audit log keeps up to 512 characters of each prompt, 0600.
