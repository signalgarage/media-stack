# media-stack

media-stack is a self-hosted media server automation stack (Plex plus Radarr, Sonarr, Prowlarr and qBittorrent behind a VPN, with a Telegram assistant) with a one-command installer. It runs on one Linux machine with Docker. The installer starts the containers and connects the apps to each other through their own APIs. Scheduled guard scripts then watch for the problems these setups tend to have: downloads of files you already own, dead or fake torrents, a VPN tunnel that stops passing traffic, a network share that did not mount. Plex is optional. The stack contains no indexers and no content. You choose your own sources, and you are responsible for what you download.

## What you get

- **qBittorrent behind a VPN.** qBittorrent shares the network of a Gluetun VPN container. When the tunnel is down, qBittorrent has no network at all (a kill switch). `tools/killswitch_test.sh` proves it on your machine.
- **The usual apps, already connected.** Radarr (movies), Sonarr (TV), Prowlarr (indexers), FlareSolverr (for indexers behind Cloudflare) and Jellyseerr (requests and new-release data). `configure.py` sets up the download client, root folders, a 1080p quality profile with size limits, rules that block low-quality and wrong-language releases, hardlink imports, file naming and the connections between the apps.
- **A Telegram assistant.** Send the bot a title and tap a number to add it. It tells you when a download starts and when it is ready to watch, lets you cancel a download or pick a release yourself, and sends a daily report, new-release suggestions and offers for missing episodes. Only the Telegram user ids you list can use it. See [docs/BOT.md](docs/BOT.md).
- **Guard scripts on a schedule.** Cron jobs catch re-downloads, a stuck VPN, imports that do not play, dead and fake downloads, missing mounts, unhealthy apps and more, and fix what is safe to fix. Every job has a dry-run mode. See [docs/AUTOMATIONS.md](docs/AUTOMATIONS.md).
- **Backups.** Every night the stack backs up the settings of Radarr, Sonarr, Prowlarr, Jellyseerr and qBittorrent (and of Bazarr, Tautulli and Uptime Kuma when you run them), the bot's state and `.env`. Once a week it restores one of the backups into a throwaway container to prove that it works.
- **Optional services** through Docker Compose profiles: `plex`, `monitoring` (Uptime Kuma and Tautulli) and `subtitles` (Bazarr).
- **An installer you can run again.** It keeps your answers and secrets, fills in what is missing, and never moves, renames or deletes anything in your media folder.

## Requirements

- A Linux machine with Docker Engine and Docker Compose 2.24 or newer.
- On the host: bash 4 or newer, Python 3.9 or newer, curl, cron and git. The `ffmpeg` package is recommended: its `ffprobe` checks that files play.
- `/dev/net/tun` on the host. The VPN container needs it.
- A VPN subscription that Gluetun supports (WireGuard or OpenVPN). [docs/SETUP.md](docs/SETUP.md) has a table of common providers.
- A Telegram account, a bot token from BotFather, and your numeric Telegram user id.
- Optional: Plex. Point the stack at your existing server, let it run one for you, or do without.
- Disk space for your media. The installer stops when less than 1 GB is free where the stack lives, and warns below 5 GB.

## Quick start

```bash
git clone https://github.com/signalgarage/media-stack.git
cd media-stack
./install.sh
```

**What it asks:** the optional services you want, the media folder and its sub-folders, and extra movie libraries by language. Then your VPN provider and its keys, your home network, the Telegram bot token and your user id, and your Plex server, if any. Last, the time zone and the user and group ids the apps run as. It generates the webhook secret and the qBittorrent password itself. [docs/SETUP.md](docs/SETUP.md) goes through every question.

**What it does:** checks the machine, writes `.env` (readable by you only), creates the folders, downloads the images that are missing, starts the containers, reads the apps' API keys, starts the bot, connects the apps with `configure.py`, and installs the guard jobs in your crontab.

**What it prints at the end:**

```
== Done
  qBittorrent   http://192.0.2.10:8080
  Radarr        http://192.0.2.10:7878
  Sonarr        http://192.0.2.10:8989
  Prowlarr      http://192.0.2.10:9696
  Jellyseerr    http://192.0.2.10:5055

Next steps
  - qBittorrent login: user admin, password: QBIT_PASSWORD in .env
  - Radarr, Sonarr and Prowlarr ask you to create a login the first time you open them.
  - Add your indexers in Prowlarr; tag the ones behind Cloudflare with 'flaresolverr'.
  - Send /start to your Telegram bot.
  ...
```

Then work through the first-run checks in [docs/SETUP.md](docs/SETUP.md#7-first-run-checks).

## How it fits together

qBittorrent runs inside the Gluetun container's network, so all of its traffic goes through the VPN. The other apps share a private Docker network called `media` and reach qBittorrent at `gluetun:8080`. One media folder (`MEDIA_ROOT`) is mounted as `/data` in every container that handles files, which lets Radarr and Sonarr import with hardlinks instead of copies. Radarr and Sonarr report grabs and imports to the bot through webhooks on a secret path, and the bot's port is never published. The guard scripts run on the host from cron and talk to the apps on their local ports. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) has the diagram, the data flow and the security model.

## Documentation

- [docs/SETUP.md](docs/SETUP.md): installation step by step, VPN providers, Plex, folder layout, changing settings, upgrades, uninstall.
- [docs/BOT.md](docs/BOT.md): every command, button and background job of the Telegram bot.
- [docs/AUTOMATIONS.md](docs/AUTOMATIONS.md): every guard script, its schedule, what it does and what it never does.
- [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md): symptom, check, fix.
- [docs/ADDING-USENET.md](docs/ADDING-USENET.md): add a Usenet download client next to qBittorrent.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): containers, networks, data flow, where state lives, security model.
- [.env.example](.env.example): every setting with a short comment.

## What it does not do

- **No indexers.** Nothing in this repository adds an indexer, tracker or download source. You add indexers of your choice in Prowlarr.
- **No music or books.** It handles movies and TV only.
- **No Windows or macOS.** The installer runs on Linux only.
- **No automatic updates.** `update_check` tells you when newer images exist. You decide when to apply them.
- **No remote access.** The web pages are plain HTTP for your own network. Do not publish them to the internet.

## Contributing, security and license

- [CONTRIBUTING.md](CONTRIBUTING.md): how to propose changes and run the checks.
- [SECURITY.md](SECURITY.md): how to report a vulnerability privately.
- [CHANGELOG.md](CHANGELOG.md): what changed in each release.
- [LICENSE](LICENSE): MIT.

## Disclaimer

media-stack is for personal use with content you have the rights to. Follow your local laws and the terms of your VPN provider and your indexers. The authors are not responsible for misuse. The software is provided as is, with no warranty of any kind, under the MIT license.
