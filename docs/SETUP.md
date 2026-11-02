# Setup

This guide installs media-stack step by step. Each step shows the command to run and what you should see. When something does not match, look in [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

Run every command on the server, as your normal user (not root), in the folder you cloned media-stack into, unless a step says otherwise.

1. [Prepare the host](#1-prepare-the-host)
2. [Make a Telegram bot and find your user id](#2-make-a-telegram-bot-and-find-your-user-id)
3. [Choose a VPN and collect the Gluetun values](#3-choose-a-vpn-and-collect-the-gluetun-values)
4. [Decide about Plex](#4-decide-about-plex)
5. [Pick the media folder layout](#5-pick-the-media-folder-layout)
6. [Run the installer](#6-run-the-installer)
7. [First-run checks](#7-first-run-checks)
8. [Change settings later](#8-change-settings-later)
9. [Re-run and upgrade](#9-re-run-and-upgrade)
10. [Test installs without a VPN](#10-test-installs-without-a-vpn)
11. [Check the kill switch](#11-check-the-kill-switch)
12. [Uninstall](#12-uninstall)

## 1. Prepare the host

You need a Linux machine that stays on. Install Docker Engine with the Compose plugin by following Docker's guide for your distribution: <https://docs.docker.com/engine/install/>

Let your user run Docker without sudo, then log out and back in:

```bash
sudo usermod -aG docker "$USER"
```

Check the versions:

```bash
docker compose version
python3 --version
```

Success looks like `Docker Compose version v2.24.0` or newer and `Python 3.9` or newer. The installer stops when Compose is older than 2.24.

Also check:

- `command -v curl crontab git` prints three paths. Without `crontab` the guard jobs are not installed.
- `ls -l /dev/net/tun` shows a line that starts with `c` (a character device). If the file is missing, run `sudo modprobe tun` and add a line `tun` to a file in `/etc/modules-load.d/` so it is loaded after a reboot.
- Recommended: install your distribution's `ffmpeg` package. Its `ffprobe` is how `verify_imports` and `integrity_scan` check that files play.

If Docker is not installed yet, `./install.sh --install-docker` offers to download Docker's official install script from get.docker.com and run it for you.

## 2. Make a Telegram bot and find your user id

1. In Telegram, open **@BotFather** (the official one has a blue check mark) and send `/newbot`. Choose a display name and a user name that ends in `bot`.
2. BotFather replies with a token that looks like `123456789:AA...`. This is `TELEGRAM_BOT_TOKEN`. Keep it secret: anyone who has it can run your bot.
3. Open your new bot and tap **Start**. Telegram only lets a bot write to people who wrote to it first, and the installer sends test messages.
4. Find your numeric Telegram user id. It is a number, not your @username. Two ways:
   - Ask any "get my id" bot in Telegram. It replies with your number.
   - Or use the bot's own log. Install with a placeholder such as `123`, send your bot a message, then run `docker logs moviebot 2>&1 | grep "unauthorised user id"`. The line ends with your id. Put it in `ALLOWED_TELEGRAM_USER_IDS` in `.env` and run `./install.sh` again (keep your `.env`).

More than one person can use the bot: list their ids separated by commas. Every listed user gets the bot's messages. The guard scripts' alerts go to `NOTIFY_CHAT_ID`, or to the first id in the list when that is empty.

## 3. Choose a VPN and collect the Gluetun values

qBittorrent only reaches the internet through Gluetun, so you need a VPN subscription that Gluetun supports. Gluetun's wiki lists every provider and the values each one needs: <https://github.com/qdm12/gluetun-wiki/tree/main/setup/providers>

The installer asks for these and writes them to `.env`:

| Setting | What it is |
|---|---|
| `VPN_SERVICE_PROVIDER` | The provider's name as Gluetun spells it (see the table below). |
| `VPN_TYPE` | `wireguard` (the default) or `openvpn`. |
| `WIREGUARD_PRIVATE_KEY` | For WireGuard: your private key, 44 characters ending in `=`. |
| `WIREGUARD_ADDRESSES` | For WireGuard: your interface address, for example `192.0.2.10/32`. Some providers do not need it. |
| `WIREGUARD_PRESHARED_KEY` | For WireGuard: only when your provider uses one. |
| `OPENVPN_USER`, `OPENVPN_PASSWORD` | For OpenVPN: usually special VPN credentials, not your account login. |
| `SERVER_COUNTRIES` | Optional: countries to choose servers from, comma separated. |
| `VPN_PORT_FORWARDING` | `on` only when your provider forwards a port (see the table). |
| `FIREWALL_OUTBOUND_SUBNETS` | Your home network, for example `192.0.2.0/24`, so you can open the qBittorrent web UI from other devices. The installer detects it. |

Common providers:

| Provider | `VPN_SERVICE_PROVIDER` | WireGuard needs | OpenVPN needs | Automatic port forwarding | Gluetun page |
|---|---|---|---|---|---|
| Mullvad | `mullvad` | `WIREGUARD_PRIVATE_KEY`, `WIREGUARD_ADDRESSES` | Mullvad is removing OpenVPN: use WireGuard | No | [mullvad](https://github.com/qdm12/gluetun-wiki/blob/main/setup/providers/mullvad.md) |
| ProtonVPN | `protonvpn` | `WIREGUARD_PRIVATE_KEY` | `OPENVPN_USER`, `OPENVPN_PASSWORD` (the OpenVPN credentials from your account page) | Yes | [protonvpn](https://github.com/qdm12/gluetun-wiki/blob/main/setup/providers/protonvpn.md) |
| Private Internet Access | `private internet access` | Not built into Gluetun: use OpenVPN | `OPENVPN_USER`, `OPENVPN_PASSWORD` | Yes | [private-internet-access](https://github.com/qdm12/gluetun-wiki/blob/main/setup/providers/private-internet-access.md) |
| Surfshark | `surfshark` | `WIREGUARD_PRIVATE_KEY`, `WIREGUARD_ADDRESSES` | `OPENVPN_USER`, `OPENVPN_PASSWORD` (from Surfshark's manual setup page) | No | [surfshark](https://github.com/qdm12/gluetun-wiki/blob/main/setup/providers/surfshark.md) |
| NordVPN | `nordvpn` | `WIREGUARD_PRIVATE_KEY` (from the manual setup section of your account) | `OPENVPN_USER`, `OPENVPN_PASSWORD` (service credentials, not your email and password) | No | [nordvpn](https://github.com/qdm12/gluetun-wiki/blob/main/setup/providers/nordvpn.md) |
| AirVPN | `airvpn` | `WIREGUARD_PRIVATE_KEY`, `WIREGUARD_PRESHARED_KEY`, `WIREGUARD_ADDRESSES` (all from AirVPN's config generator) | Needs a client key and certificate, which the installer does not ask for: use WireGuard | No (by hand, see below) | [airvpn](https://github.com/qdm12/gluetun-wiki/blob/main/setup/providers/airvpn.md) |
| Windscribe | `windscribe` | `WIREGUARD_PRIVATE_KEY`, `WIREGUARD_PRESHARED_KEY` (required), `WIREGUARD_ADDRESSES` | `OPENVPN_USER`, `OPENVPN_PASSWORD` (from a config you generate on Windscribe's site) | No (by hand, see below) | [windscribe](https://github.com/qdm12/gluetun-wiki/blob/main/setup/providers/windscribe.md) |
| Your own WireGuard server | `custom` | `WIREGUARD_PRIVATE_KEY`, `WIREGUARD_ADDRESSES`, optional `WIREGUARD_PRESHARED_KEY`, plus the server's address, port and public key in an override file (below) | Not covered here | No | [custom](https://github.com/qdm12/gluetun-wiki/blob/main/setup/providers/custom.md) |

Notes on the table:

- **Mullvad:** `WIREGUARD_ADDRESSES` is the IPv4 `Address` line of a WireGuard configuration file you generate on Mullvad's site.
- **ProtonVPN:** for port forwarding set `VPN_PORT_FORWARDING=on`, and either switch on NAT-PMP (port forwarding) when you generate the WireGuard configuration, or add `+pmp` to the end of `OPENVPN_USER`. Also set `PORT_FORWARD_ONLY=on` in `.env`, so Gluetun only uses servers that forward ports.
- **Private Internet Access:** for port forwarding set `VPN_PORT_FORWARDING=on` and `PORT_FORWARD_ONLY=on`. Gluetun picks PIA servers by region, not by country, so leave `SERVER_COUNTRIES` empty.
- **AirVPN:** if `WIREGUARD_ADDRESSES` from the config generator holds an IPv6 address, keep only the IPv4 one unless Docker on your server has IPv6.
- **AirVPN and Windscribe** let you reserve a port on their site, but Gluetun does not set it up for you. Keep `VPN_PORT_FORWARDING=off`. Their Gluetun pages explain the manual way.
- **Windscribe:** Gluetun picks Windscribe servers by region, not by country, so leave `SERVER_COUNTRIES` empty.

**What port forwarding changes.** With `VPN_PORT_FORWARDING=on`, Gluetun runs `scripts/gluetun/setport.sh` each time it gets a forwarded port, which sets qBittorrent's listening port to that port, and `vpn_watchdog` checks that the two stay equal. Without port forwarding qBittorrent still downloads. It shows as "firewalled" and reaches fewer peers, which is normal behind a VPN.

**Gluetun settings the installer does not ask for.** `docker-compose.yml` passes Gluetun only the settings above, `PORT_FORWARD_ONLY` and the time zone. For anything else Gluetun supports, such as a region filter or your own WireGuard server, create a file named `docker-compose.override.yml` next to `docker-compose.yml`. Docker Compose and the installer read it automatically. Example for your own WireGuard server (`VPN_SERVICE_PROVIDER=custom`):

```yaml
services:
  gluetun:
    environment:
      WIREGUARD_ENDPOINT_IP: "203.0.113.10"
      WIREGUARD_ENDPOINT_PORT: "51820"
      WIREGUARD_PUBLIC_KEY: "PASTE_THE_SERVER_PUBLIC_KEY_HERE"
```

Keep the private key and the address in `.env` as usual. A region filter goes in the same place, for example `SERVER_REGIONS: "a region name from the Gluetun page"`. Apply a change with `docker compose up -d`.

## 4. Decide about Plex

You have three choices.

**A. You already have a Plex server.** Give the installer its address (`PLEX_URL`, for example `http://192.0.2.10:32400`) and a token (`PLEX_TOKEN`). Plex explains how to find the token in [Finding an authentication token / X-Plex-Token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/). With a token, `configure.py` adds a Plex connection to Radarr and Sonarr, so Plex scans new files right after they are imported, and it does Jellyseerr's first-run setup for you. Plex says a token found that way may only be valid for a while. If the Plex connection stops working later, put a new token in `PLEX_TOKEN` and run `python3 configure.py --only radarr,sonarr`.

**B. Run Plex in this stack.** Install with `./install.sh --profiles plex`, or answer `plex` to the optional services question. Just before the containers start for the first time, the installer asks for a claim code: open <https://www.plex.tv/claim>, copy the code and paste it within 4 minutes. Press Enter instead to claim the server later at `http://<server>:32400/web`, then run `./install.sh` again. With this profile `PLEX_URL` is `http://plex:32400`, the installer reads the token from Plex's own settings once the server is claimed, and the host scripts reach Plex at `localhost:32400` by themselves. Plex sees the media read-only under `/data`: when you add libraries in Plex, choose folders such as `/data/movies` and `/data/tv`. If devices on your home network show up as remote in Plex, add your network (for example `192.0.2.0/24`) under Settings > Network > LAN Networks in Plex.

**C. No Plex.** Press Enter at the Plex question. Everything else works. Jellyseerr still needs one sign-in with a media server account before it is set up, and the bot's new-release cards need Jellyseerr.

## 5. Pick the media folder layout

Everything lives under one folder, `MEDIA_ROOT` (default `/srv/media`). Every container that handles media sees it as `/data`.

```
/srv/media            MEDIA_ROOT, seen as /data inside the containers
  downloads/          DOWNLOADS_DIR: qBittorrent saves here
    movies/           downloads for Radarr (qBittorrent category radarr)
    tv/               downloads for Sonarr (qBittorrent category sonarr)
  movies/             MOVIES_DIR: Radarr's library
  movies-fr/          optional extra library from MOVIE_LIBRARIES
  tv/                 TV_DIR: Sonarr's library
```

**Why one folder.** Radarr and Sonarr import a finished download by making a hardlink in the library. A hardlink is instant, uses no extra space, and the download keeps seeding. Hardlinks only work inside one filesystem, and only when the apps see the downloads and the library through one mount. That is why the stack mounts `MEDIA_ROOT` once and never mounts the sub-folders on their own.

- **One disk:** nothing to do. The installer creates the folders.
- **Several disks:** mount them under `MEDIA_ROOT`, not somewhere else, because the containers only see `MEDIA_ROOT`. A download on one disk cannot be hardlinked into a library on another disk: that import becomes a full copy, which is slower and uses the space twice while the torrent seeds. Keep the downloads on the same disk as the library they feed when you can. The installer warns when downloads, movies and TV are not on one filesystem.
- **Network shares:** mount them at or under `MEDIA_ROOT` before the stack starts, list each mount point in `MOUNT_PATHS` in `.env`, and read about [mount_guard](AUTOMATIONS.md#mount_guard). A container does not see a share that was mounted after it started.

**Extra movie libraries by language.** `MOVIE_LIBRARIES=fr=movies-fr,es=movies-es` sends movies whose original language is French to `movies-fr` and Spanish ones to `movies-es`. Everything else goes to `movies`.

The installer never moves, renames or deletes anything in your media folder. It only creates missing folders, and only the folders it created (or empty ones) are given to the user and group the apps run as (`PUID`, `PGID`).

## 6. Run the installer

```bash
git clone https://github.com/signalgarage/media-stack.git
cd media-stack
./install.sh
```

The questions, in order. Press Enter to accept the value in brackets. What you type for secrets is not shown.

| Question | Setting | Notes |
|---|---|---|
| Optional services: plex, monitoring, subtitles | `COMPOSE_PROFILES` | Comma separated. Enter for none. Not asked when you pass `--profiles`. |
| Media folder (downloads and libraries; the apps see it as /data) | `MEDIA_ROOT` | An absolute path such as `/srv/media`. Not a system folder and not the stack's own folder. |
| Downloads folder inside it | `DOWNLOADS_DIR` | Default `downloads`. |
| Movies folder inside it | `MOVIES_DIR` | Default `movies`. |
| TV folder inside it | `TV_DIR` | Default `tv`. |
| Extra movie libraries | `MOVIE_LIBRARIES` | For example `fr=movies-fr,es=movies-es`. Enter for none. |
| VPN provider (as gluetun names it) | `VPN_SERVICE_PROVIDER` | See section 3. |
| VPN type (wireguard or openvpn) | `VPN_TYPE` | Default `wireguard`. |
| WireGuard private key, address, preshared key | `WIREGUARD_PRIVATE_KEY`, `WIREGUARD_ADDRESSES`, `WIREGUARD_PRESHARED_KEY` | WireGuard only. The address and the preshared key may be empty. |
| OpenVPN user name, OpenVPN password | `OPENVPN_USER`, `OPENVPN_PASSWORD` | OpenVPN only. |
| VPN server countries | `SERVER_COUNTRIES` | Enter for any. |
| Port forwarding | `VPN_PORT_FORWARDING` | `on` or `off`. Default `off`. |
| Your LAN, so the qBittorrent web UI stays reachable | `FIREWALL_OUTBOUND_SUBNETS` | The detected network is the default. |
| Telegram bot token | `TELEGRAM_BOT_TOKEN` | From BotFather. |
| Telegram user id(s) allowed to use the bot | `ALLOWED_TELEGRAM_USER_IDS` | Numbers, comma separated. |
| Existing Plex server URL | `PLEX_URL` | Not asked with the `plex` profile. Enter for no Plex. |
| Plex token | `PLEX_TOKEN` | Only asked when you gave a Plex URL. Optional. |
| Time zone | `TZ` | Detected from the server, for example `Etc/UTC`. |
| User id the apps run as | `PUID` | Your user id (`id -u`). |
| Group id the apps run as | `PGID` | Your group id (`id -g`). |

The installer generates `WEBHOOK_SECRET` and `QBIT_PASSWORD` itself. When `.env` already exists, it first asks whether to keep it and only fill in what is missing (`k`, the default) or to answer every question again (`r`; the old file is saved as `.env.bak-<date>`).

After the questions it works through these steps, each under a `==` heading:

1. **Writing .env:** written with mode 600, so only you can read it.
2. **Folders:** creates the media folders, `config/<app>`, `state/`, `logs/` and `backups/`.
3. **Containers:** downloads the images that are not on the machine yet (the first run takes a while; later runs never update images) and asks for the Plex claim code when you use the `plex` profile. Then it starts the apps, waits up to 5 minutes for their first start, reads their API keys into `.env`, and builds and starts the bot. The bot starts before the next step because Radarr and Sonarr test its webhook when the connection is saved.
4. **Connecting the apps (configure.py):** sets up qBittorrent, Radarr, Sonarr, Prowlarr and Jellyseerr. Each line ends in `ok`, `created`, `updated` or `FAILED:` with a reason. On the first run you also get Telegram messages: "Radarr is connected to the bot", "Sonarr is connected to the bot", and one test message each from the "Health alerts" connection that Radarr, Sonarr and Prowlarr get.
5. **Guard jobs (cron):** copies the jobs from `scripts/cron.template` into your crontab, between the lines `# >>> media-stack >>>` and `# <<< media-stack <<<`.
6. **Done:** prints the web addresses and the next steps.

Success looks like this, with your server's address:

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
```

The list continues with what applies to you: the Jellyseerr sign-in, claiming Plex, connecting Bazarr, and where the logs are.

**Unattended installs.** Put the answers in a file of `KEY=VALUE` lines, with the names from `.env.example`, and run:

```bash
./install.sh --yes --answers answers.env
```

The answers without a default are `VPN_SERVICE_PROVIDER`, the WireGuard private key (or the OpenVPN user and password), `TELEGRAM_BOT_TOKEN` and `ALLOWED_TELEGRAM_USER_IDS`. Answers can also come from environment variables with the same names. The answers file holds secrets, so delete it when you are done.

All installer options:

| Option | What it does |
|---|---|
| `--answers FILE` | Reads answers from FILE instead of asking. |
| `--yes`, `-y` | Never asks: uses the answers file, environment variables and defaults, and stops with a message when a required answer is missing. |
| `--profiles LIST` | Optional services, comma separated: `plex`, `monitoring`, `subtitles`. It sets the whole list. |
| `--no-vpn` | Test installs only. See section 10. |
| `--install-docker` | When Docker is missing, offers to install it with Docker's official script. |
| `--skip-cron` | Does not install the guard jobs. |
| `-h`, `--help` | Shows the help. |

## 7. First-run checks

Work through these once after the first install.

1. **Open every web page** from the summary. Radarr, Sonarr and Prowlarr ask you to create a login the first time. The qBittorrent user is `admin` (`QBIT_USERNAME`) and the password is `QBIT_PASSWORD` in `.env`:

   ```bash
   grep '^QBIT_PASSWORD=' .env
   ```

2. **Check the VPN.** This prints `healthy`:

   ```bash
   docker inspect -f '{{.State.Health.Status}}' gluetun
   ```

   Then run the kill switch test in section 11.

3. **Add indexers in Prowlarr.** Go to Indexers > Add Indexer and add indexers of your choice. If an indexer is behind Cloudflare, give it the tag `flaresolverr`: `configure.py` set up a FlareSolverr proxy for that tag. Prowlarr copies the indexers to Radarr and Sonarr by itself (Settings > Apps lists both).

4. **Finish Jellyseerr if the summary asks you to.** That happens when there was no `PLEX_TOKEN`. Open `http://<server>:5055`, sign in with your media server account and finish the wizard (you can skip its Radarr and Sonarr step). Then run:

   ```bash
   python3 configure.py --only jellyseerr
   ```

   It links Radarr and Sonarr and prints `ok`, `created` or `updated` for each.

5. **Talk to the bot.** Send `/start`. It replies "Media bot ready." with a list of what it can do. Send a title to try a search. Then check that the host scripts can reach you too:

   ```bash
   python3 scripts/tg.py send "Test from media-stack"
   ```

   It prints `sent`, and the message arrives.

6. **Check the on-grab guard.** In Radarr and in Sonarr, Settings > Connect lists `moviebot`, `On-grab guard`, `Health alerts` and, when the stack has a Plex token, `Plex`. The on-grab guard cancels a grab straight away when the movie, or every episode in it, is already on disk. Its log is `config/radarr/ongrab/ongrab_guard.log` (and the same under `config/sonarr/`). To make it only log what it would cancel, set `ONGRAB_DRYRUN=true` in `.env` and run `python3 configure.py --only radarr`.

7. **Check the guard jobs.** `crontab -l` shows the media-stack block. A few minutes later, `logs/` holds a file per job, for example `logs/mount_guard.log`.

8. **Already have a library?** If your library folders already hold media, import it in Radarr and Sonarr (their Library Import pages), so they know what you have. Then record the naming problems that already exist, so the weekly naming check only reports new ones:

   ```bash
   python3 scripts/naming_guard.py --seed
   ```

9. **Optional services.** Nothing sets these up for you beyond starting them. With the `subtitles` profile, connect Bazarr to Sonarr (`sonarr`, port 8989) and Radarr (`radarr`, port 7878) in its settings; their API keys are in `.env`. With the `monitoring` profile, go through the first-run setup of Uptime Kuma (port 3001) and of Tautulli (port 8181, whose wizard connects it to your Plex server). Then add Uptime Kuma push monitors for the guard jobs you care about ([AUTOMATIONS.md](AUTOMATIONS.md#how-the-jobs-run)).

## 8. Change settings later

All settings live in `.env`, and `.env.example` explains each one. After you edit `.env`, the simplest way to apply everything is to run the installer again and keep your `.env`:

```bash
./install.sh
```

To apply only what changed:

| You changed | Run |
|---|---|
| Something a container uses: VPN settings, `TELEGRAM_BOT_TOKEN`, `ALLOWED_TELEGRAM_USER_IDS`, `WEBHOOK_SECRET`, the bot's schedule and limits (`REPORT_HOUR`, `NEW_RELEASES_*`, `FILL_*`, `DEAD_MINUTES`, `DISK_ALERT_PCT`, `FALLBACK_GRACE_SEC`), `PREFERRED_LANGUAGE`, `EPISODE_CAP_GB` | `docker compose up -d` |
| Something `configure.py` writes into the apps: `TV_SIZE_CAP_MB_PER_MIN`, `MOVIE_SIZE_CAP_MB_PER_MIN`, `ALLOW_UPGRADES`, `PREFERRED_LANGUAGE`, `BLOCK_MULTI_AUDIO`, `RENAME_FILES`, `MIN_FREE_SPACE_MB`, `ONGRAB_DRYRUN`, `PLEX_URL`, `PLEX_TOKEN`, `NOTIFY_CHAT_ID`, `ALLOWED_TELEGRAM_USER_IDS`, `WEBHOOK_SECRET`, `TELEGRAM_BOT_TOKEN` | `python3 configure.py` |
| `GUARDS_ENABLED`, `SHRINK_ENABLED` | `python3 configure.py --only cron` |
| `MOUNT_PATHS`, `MOUNT_CANARY_FILE`, `MOUNT_CANARY_MAX_AGE_S`, `INTEGRITY_EXCLUDE`, `VERIFY_IMPORTS_HEAL`, `KUMA_PUSH_*` | Nothing. The guards read `.env` on every run. |
| The optional services | `./install.sh --profiles <list>` |

Some settings appear twice because both a container and the apps use them; run both commands, or simply `./install.sh`. Turning a profile off does not stop its containers: remove them with `docker rm -f <name>`, for example `docker rm -f bazarr`.

To move your media to another disk, keep the same folder names below the new location, change `MEDIA_ROOT` and run `docker compose up -d`. The apps keep seeing the same `/data` paths, so nothing else changes.

## 9. Re-run and upgrade

Running `./install.sh` again is safe at any time. It keeps your answers and secrets, fills in new settings from `.env.example`, never updates images, rebuilds the bot, applies `configure.py` again and refreshes the cron block.

To upgrade media-stack itself:

```bash
git pull
./install.sh
```

The app images are never updated automatically. `update_check` tells you on Mondays when newer images exist. To update them:

```bash
docker compose pull
docker compose up -d
```

`docker compose pull` cannot pull the bot because it is built locally; it says so and carries on. `./install.sh` rebuilds the bot. Before a big update, take a fresh configuration backup with `python3 scripts/config_backup.py`.

## 10. Test installs without a VPN

`./install.sh --no-vpn` is for testing the installer on a throwaway machine without a VPN subscription. It replaces Gluetun with a dummy container that only holds a network namespace (`compose/test-no-vpn.override.yml`, added through `COMPOSE_FILE` in `.env`), sets `VPN_MODE=off`, and prints a TEST MODE banner. qBittorrent then sends all of its traffic over your normal connection. There is no VPN and no kill switch.

Never use it for real downloading. Every peer and tracker sees your home address, and nothing stops the traffic. `vpn_watchdog` and `tools/killswitch_test.sh` do nothing in this mode.

To go back, run `./install.sh` without `--no-vpn`. It switches the VPN back on, removes `COMPOSE_FILE` from `.env`, and asks for your VPN settings again, because a test install only holds the template's example values.

## 11. Check the kill switch

With the stack running:

```bash
tools/killswitch_test.sh
```

It shows qBittorrent's public address through the tunnel (it must differ from the server's own), stops Gluetun and checks that qBittorrent can reach nothing and has no network interface except loopback, then starts Gluetun again and restarts qBittorrent. Downloads pause for a minute or two. It contacts one public "what is my IP" service, from the server and from inside qBittorrent, and nothing else.

A good result ends in `PASS`:

```
1. Through the tunnel
   this server's public address : 198.51.100.7
   qbittorrent's public address : 203.0.113.25
2. VPN container stopped
   qbittorrent's public address : none (good)
   network interfaces           : lo
3. VPN back
restoring: starting gluetun and restarting qbittorrent...
   gluetun health               : healthy
   qbittorrent's public address : 203.0.113.25

PASS
```

`FAIL` comes with the reason, for example a leak with the VPN stopped. The script exits 0 on PASS.

## 12. Uninstall

```bash
./uninstall.sh
```

It asks for confirmation, removes the media-stack block from your crontab, and stops and removes the containers and their network. It keeps your media, `.env`, `config/`, `state/`, `logs/` and `backups/`, so `./install.sh` can bring everything back.

| Option | What it adds |
|---|---|
| `--remove-images` | Also deletes the images the stack downloaded or built. |
| `--purge` | Also deletes `.env`, `config/` (all app settings), `state/`, `logs/` and `backups/` in the stack folder. You confirm by typing `delete`. |
| `--yes` | Does not ask for confirmation. |

Your media folder is never touched, and nothing outside the stack folder is deleted.
