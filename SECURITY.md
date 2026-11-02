# Security policy

## Reporting a vulnerability

Please report security problems privately, not in a public issue.

Use GitHub's private vulnerability reporting: open this repository on GitHub, go to its Security tab (called "Security and quality" on newer GitHub pages), and choose **Report a vulnerability**. GitHub explains the steps in [Privately reporting a security vulnerability](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability).

Please include what you found, how to reproduce it, what an attacker could do with it, and the commit or release you tested.

If you cannot find the button, open a normal issue that only says you have a security report and asks for a private channel. Do not put any details in that issue.

## Supported versions

Only the latest release and the default branch get security fixes.

## What is in scope

The code in this repository: `install.sh`, `uninstall.sh`, `configure.py`, `docker-compose.yml`, the scripts in `scripts/`, the bot in `moviebot/` and the tools in `tools/`. For example:

- a way for someone who is not on the allow-list to make the bot do anything;
- the webhook accepting calls without the right secret;
- secrets ending up in logs, in backups that others can read, or in files that git would commit;
- a guard script deleting or changing files beyond what [docs/AUTOMATIONS.md](docs/AUTOMATIONS.md) says it does;
- qBittorrent traffic leaving the server outside the VPN tunnel while the VPN is on.

## What is out of scope

- Problems in the apps the stack runs (Radarr, Sonarr, Prowlarr, qBittorrent, Gluetun, FlareSolverr, Jellyseerr, Plex, Uptime Kuma, Tautulli, Bazarr). Please report those to their own projects.
- Attacks that need a shell on the server, or read access to `.env`, to begin with.
- The `--no-vpn` test mode having no VPN. That is its documented purpose.
- Settings you chose that weaken the stack, for example publishing the web pages to the internet.

## What to expect

This is a small project maintained by its repository owner in spare time. You should get a first reply within 14 days. Confirmed problems are fixed as soon as possible, and the fix is announced in a security advisory and in [CHANGELOG.md](CHANGELOG.md). You are credited in the advisory unless you ask not to be.

There is no bug bounty. Thank you for reporting responsibly.
