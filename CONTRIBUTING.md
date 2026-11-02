# Contributing

Thank you for helping. A few short rules keep the project easy to review and safe to publish.

## Before you start

- **Open an issue first for big changes**: a new service, a new guard script, a changed default, anything that changes what the installer asks or writes. Small fixes can go straight to a pull request.
- Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and, for guard scripts, [docs/AUTOMATIONS.md](docs/AUTOMATIONS.md).

## Run the checks locally

The same checks run in CI. Install the tools once (`pip install ruff pytest pyyaml`, and your distribution's `shellcheck` package), then run from the repository root:

```bash
ruff check .
pytest -q
shellcheck install.sh uninstall.sh scripts/*.sh scripts/gluetun/*.sh scripts/arr/*.sh tools/*.sh
python3 tools/block_scan.py moviebot/bot.py
bash tools/privacy_scan.sh
```

The tests need no network, no Docker and no running stack. Run them on Python 3.9 as well as a current Python if you can: CI tests both. With Docker installed, `docker compose --env-file .env.example config -q` checks that the compose file still parses, as CI does.

## Rules for code, tests and docs

- **No real titles.** Use only Big Buck Bunny (2008), Sintel (2010), Tears of Steel (2012), Elephants Dream (2006), Cosmos Laundromat (2015) and `ubuntu-24.04-desktop-amd64.iso`, with made-up release names such as `Big.Buck.Bunny.2008.1080p.WEB-DL.x264-GROUP`.
- **No indexers or trackers.** Do not name them or link to them anywhere, not even in a comment, and add no download links of any kind.
- **No personal data.** No secrets, tokens, host names, user names or personal paths. The only IP addresses allowed in examples are from 192.0.2.0/24, 198.51.100.0/24 and 203.0.113.0/24. Every value comes from `.env` through `scripts/settings.py`. A new setting goes into `.env.example`, in the right section, with a one-line comment. Never rename an existing setting.
- **Python versions.** The host-side code (`configure.py`, `scripts/`, `tools/`) must run on Python 3.9 with the standard library only. The bot runs on Python 3.12 in its container.
- **Guard scripts.** All work happens inside `main(argv)`, and importing a script does nothing. Every guard takes `--dry-run` (it changes and sends nothing) and exits 0 when healthy, 1 when it found a problem a monitor should show, and 2 when it failed itself. Keep the safety rules (budgets, cooldowns, "never delete a library file").
- **The bot.** Never call blocking code (network, sleep, subprocess, slow disks) directly inside an `async def`; use `await asyncio.to_thread(...)`. `tools/block_scan.py` checks this.
- **Comments** explain why (a design constraint, a failure mode), in plain words. No names of people, and no dated incident stories.
- **Docs** use short, plain sentences and no em-dashes.

## Commits and pull requests

- Commit messages: a short subject in the imperative ("Add a dry run to naming_guard", "Fix the disk alert for network shares"), then a blank line and the reason when it is not obvious.
- One topic per pull request. Say what you changed, why, and how you tested it.
- By contributing, you agree that your work is released under the [MIT license](LICENSE).
