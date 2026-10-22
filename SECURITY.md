# Security

<!-- OWNER: enable private vulnerability reporting in the repo settings once it's public. -->
If you find a security problem, report it through GitHub's
[private vulnerability reporting](../../security/advisories/new) form rather than a public
issue, because a public issue shows the flaw to everyone before it is fixed. This is a
personal project, so I can't promise how quickly I'll reply, and only `main` gets fixes.

The app protects your agents in four ways:

- Only the Tailscale account named in `HP_TAILNET_LOGIN` can read or drive your agents,
  unless you opt in to more with `HP_ALLOW_ANY_TAILNET_USER` or `HP_ASSUME_LOGIN`.
- The app listens only on the machine itself, so the only way in from your tailnet is
  through `tailscale serve`, which tells the app who sent each request.
- Every write to an agent is recorded in the audit log before it is sent, and the log
  never stores the text you typed.
- A notification sent with `herdr-pwa ping` always shows as "Ping from" and the sender's
  name, so it can't pass for an alert from an agent.

One limit is known: any program already running as you on the machine can reach the app's
port and drive your agents. That program could run commands as you anyway.
