# Troubleshooting

Each entry is a symptom, what to check, and how to fix it. Run the commands on the server, in the stack folder. After any fix, `./install.sh` (keeping your `.env`) is always a safe way to put everything back in line.

- [Where the logs are, and the self-check](#where-the-logs-are-and-the-self-check)
- [Run a guard by hand, as a dry run](#run-a-guard-by-hand-as-a-dry-run)
- [Containers are unhealthy or keep restarting](#containers-are-unhealthy-or-keep-restarting)
- [Gluetun is not healthy and qBittorrent does not start](#gluetun-is-not-healthy-and-qbittorrent-does-not-start)
- [The qBittorrent web UI does not open from another device](#the-qbittorrent-web-ui-does-not-open-from-another-device)
- [qBittorrent shows "firewalled" or few DHT nodes](#qbittorrent-shows-firewalled-or-few-dht-nodes)
- [Prowlarr's Radarr or Sonarr test fails](#prowlarrs-radarr-or-sonarr-test-fails)
- [Radarr or Sonarr cannot reach qBittorrent](#radarr-or-sonarr-cannot-reach-qbittorrent)
- [Imports fail](#imports-fail)
- [Nothing downloads](#nothing-downloads)
- [Hardlinks do not work](#hardlinks-do-not-work)
- [The bot does not answer](#the-bot-does-not-answer)
- [The webhook returns 403](#the-webhook-returns-403)
- [Jellyseerr is not set up](#jellyseerr-is-not-set-up)
- [The guard jobs do not run](#the-guard-jobs-do-not-run)

## Where the logs are, and the self-check

- **Guard jobs:** `logs/<job>.log` (in `LOG_DIR`). Each run starts with a `=====` line and ends with `----- exit <code> after <n> s`. For example: `tail -n 40 logs/vpn_watchdog.log`.
- **The bot:** `docker logs moviebot`.
- **The apps:** `docker logs <name>` (for example `docker logs --tail 100 gluetun`), and each app's own log page in its web UI.
- **The on-grab guard:** `config/radarr/ongrab/ongrab_guard.log` and `config/sonarr/ongrab/ongrab_guard.log`.
- **Telegram messages that could not be delivered:** `state/tg_undelivered.log`. They go out with the next message that gets through.
- **What the scripts see:**

  ```bash
  python3 scripts/settings.py
  ```

  It prints the stack folder, the media folders, the state and log folders, each app's address with `key=set` or `key=missing`, how Docker is called (`docker` or `sudo -n docker`), and whether alerts have a Telegram destination. A `key=missing` usually means the app has not finished its first start, or its `config/<app>` folder cannot be read by your user.
- **What configure.py would change:** `python3 configure.py --dry-run`. It reads from the apps and prints, step by step, what it would create or update, without changing anything.

## Run a guard by hand, as a dry run

Every guard takes `--dry-run`: it prints what it would do, changes nothing and sends nothing. Run it with Python to see the output on screen:

```bash
python3 scripts/malware_guard.py --dry-run
```

`scripts/run_guard.sh <job> --dry-run` works too, but it writes the output to `logs/<job>.log` instead of the screen. The list of jobs and their flags is in [AUTOMATIONS.md](AUTOMATIONS.md#all-jobs-at-a-glance).

## Containers are unhealthy or keep restarting

**Check:**

```bash
docker compose ps
docker logs --tail 50 <name>
```

The STATUS column shows `Up`, `Up (healthy)`, `Restarting` or `Exited`. The last lines of the log usually name the cause.

**Fix:**

- **Permission denied** on `/config` or `/data`: the folder does not belong to `PUID`:`PGID`. Check with `ls -ln config/` and fix the folder the error names, for example `sudo chown -R 1000:1000 config/radarr` with your own ids.
- **Port already in use:** another program on the server uses 8080, 7878, 8989, 9696, 5055 or 6881 (or a port of an optional service). Stop that program or move it to another port. The stack's ports are fixed in `docker-compose.yml`, and the host scripts reach the apps on them through the `*_URL` settings.
- **No space left on device:** free space on the disk that holds the stack folder or Docker's storage.
- Then start everything again with `docker compose up -d`.

## Gluetun is not healthy and qBittorrent does not start

qBittorrent only starts once Gluetun reports healthy, so a VPN problem looks like a qBittorrent problem.

**Check:**

```bash
docker inspect -f '{{.State.Health.Status}}' gluetun
docker logs --tail 100 gluetun
```

**Fix, by what the log shows:**

- **Wrong credentials or keys:** authentication errors, or a WireGuard tunnel that never passes traffic. Use the values from your provider's VPN configuration page, not your account login (OpenVPN providers usually have separate service credentials). The WireGuard private key is 44 characters ending in `=`. Correct `.env`, then `docker compose up -d`.
- **`/dev/net/tun` is missing:** the installer refuses to continue without it. Run `sudo modprobe tun`, and add a line `tun` to a file in `/etc/modules-load.d/` so it survives a reboot.
- **Unknown provider:** `VPN_SERVICE_PROVIDER` must be spelled the way Gluetun spells it, for example `private internet access` with spaces. See the table in [SETUP.md](SETUP.md#3-choose-a-vpn-and-collect-the-gluetun-values).
- **Port forwarding errors:** set `VPN_PORT_FORWARDING=off` unless your provider has "Yes" for automatic port forwarding in that table.
- **The connection is blocked:** the provider is down, or your network blocks VPN traffic. Try other countries in `SERVER_COUNTRIES` (or leave it empty), or check your provider's status page.

## The qBittorrent web UI does not open from another device

**Check:** it answers on the server itself:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8080/api/v2/app/version
```

Any number other than `000` means qBittorrent answers. Then look at `FIREWALL_OUTBOUND_SUBNETS` in `.env`.

**Fix:** Gluetun's firewall only lets qBittorrent answer the networks listed in `FIREWALL_OUTBOUND_SUBNETS`. Set it to your home network, for example `FIREWALL_OUTBOUND_SUBNETS=192.0.2.0/24` (several networks separated by commas), then run `docker compose up -d`. If it still does not open, check the server's own firewall for port 8080.

## qBittorrent shows "firewalled" or few DHT nodes

**Check:** qBittorrent's status bar shows the connection state and the number of DHT nodes. `tail -n 40 logs/vpn_watchdog.log` shows what the watchdog measured. With port forwarding on, compare the forwarded port with qBittorrent's listening port (Tools > Options > Connection):

```bash
docker exec gluetun cat /tmp/gluetun/forwarded_port
```

**Fix:**

- **Without port forwarding**, "firewalled" is normal behind a VPN. Downloads still work, with fewer peers. Only some providers forward a port (see [SETUP.md](SETUP.md#3-choose-a-vpn-and-collect-the-gluetun-values)).
- **With port forwarding**, `vpn_watchdog` pushes the forwarded port into qBittorrent when they differ. To do it by hand:

  ```bash
  docker exec gluetun /scripts/setport.sh "$(docker exec gluetun cat /tmp/gluetun/forwarded_port)"
  ```

- **Few DHT nodes for a long time, or no peers at all:** `vpn_watchdog` restarts the tunnel after 15 minutes of bad checks. It never does that while downloads run faster than 100 KB/s, and never more than once an hour. To restart it yourself:

  ```bash
  docker compose restart gluetun
  docker restart qbittorrent
  ```

## Prowlarr's Radarr or Sonarr test fails

**Check:** in Prowlarr, Settings > Apps: Radarr uses `http://radarr:7878` and Sonarr `http://sonarr:8989`, with `http://prowlarr:9696` as the Prowlarr server. Then:

```bash
python3 configure.py --only prowlarr --dry-run
```

**Fix:** most often an API key changed, for example after you regenerated one in Radarr. Run `./install.sh`: it reads the current keys from `config/<app>/` into `.env`, restarts the bot with them and runs `configure.py` again. If `configure.py` reports "the API key was rejected", a key in `.env` is out of date; `./install.sh` refreshes it.

## Radarr or Sonarr cannot reach qBittorrent

**Check:** in Radarr and Sonarr, Settings > Download Clients > qBittorrent. The host must be `gluetun` and the port `8080`, because qBittorrent lives inside Gluetun's network. Then check that qBittorrent runs and Gluetun is healthy:

```bash
docker compose ps qbittorrent gluetun
```

**Fix:**

- `python3 configure.py --only radarr,sonarr` writes the download client again and tests it.
- **Login refused:** `QBIT_PASSWORD` in `.env` must match qBittorrent's web UI password. If you changed the password in the web UI, put the new one in `.env`, then run `python3 configure.py --only radarr,sonarr` and `docker compose up -d moviebot`.
- **qBittorrent refuses logins after failed attempts:** wait an hour, or run `docker restart qbittorrent`.
- **qBittorrent stopped:** `scripts/downloads-off.sh` stops it on purpose. Start it with `scripts/downloads-on.sh` or `docker compose start qbittorrent`.

## Imports fail

**Check:** in Radarr or Sonarr, Activity > Queue: hover over the item to read the message. Then check the owner of the folders and the free space:

```bash
ls -ln /srv/media
df -h /srv/media
```

(Use your `MEDIA_ROOT` instead of `/srv/media`.)

**Fix:**

- **Permission denied:** the folders must belong to `PUID`:`PGID` from `.env`. Fix the folder the message names, for example `sudo chown -R 1000:1000 /srv/media/movies` with your own ids and path.
- **Not enough free space:** the apps refuse to import when less than `MIN_FREE_SPACE_MB` (25000 MB by default) would remain. Free some space, or lower the setting and run `python3 configure.py --only radarr,sonarr`. `arr_health_guard` warns when a root folder has less than 50 GB free.
- **"Not an upgrade":** with `ALLOW_UPGRADES=false` (the default), a title you already have is never replaced by another copy. That is on purpose.
- **A file that does not play:** `verify_imports` reports it. In the app's History, mark the download as failed so it is fetched again.

## Nothing downloads

**Check, in this order:**

1. Prowlarr has at least one working indexer: Indexers, then Test. `library_guard` alerts when none works.
2. The indexers reached Radarr and Sonarr: their Settings > Indexers lists the indexers Prowlarr synced.
3. Search by hand: in Radarr, open the movie and use Interactive Search. The rejection reasons next to each release say why it is refused.
4. qBittorrent runs and Gluetun is healthy (see above).

**Fix, by the rejection reason:**

- **"larger than maximum allowed":** the size cap per minute of runtime. See [the per-minute cap trap](AUTOMATIONS.md#the-per-minute-cap-trap). Raise `MOVIE_SIZE_CAP_MB_PER_MIN` or `TV_SIZE_CAP_MB_PER_MIN` and run `python3 configure.py --only radarr,sonarr`, or let the episode filler and `size_cap_watch` handle TV.
- **A custom format score below the minimum:** the `HD-1080p` profile scores low-quality release groups, multi-audio releases (`BLOCK_MULTI_AUDIO=true`) and releases in a language other than `PREFERRED_LANGUAGE` or the original language at -10000. That is on purpose. Change `PREFERRED_LANGUAGE` or `BLOCK_MULTI_AUDIO` and run `python3 configure.py --only radarr,sonarr` if the rules do not suit you.
- **The quality is not wanted in the profile:** the `HD-1080p` profile allows only 1080p qualities for TV, and 1080p plus some 720p qualities for movies.
- **Only cinema recordings exist:** the bot never offers CAM or TS copies. Radarr gets a proper copy once it is released.
- For one title, the bot's force-grab (the ⚡ button) lets you pick any release yourself, past every rule.

Missing older episodes and movies are searched a few at a time each day by `missing_sweep`, so the indexers are not flooded.

## Hardlinks do not work

Imports take long and use the space twice.

**Check:** the downloads and the libraries must be on one filesystem. The first number must be the same on every line:

```bash
stat -c '%d %n' /srv/media/downloads /srv/media/movies /srv/media/tv
```

The installer warns about this, and `link_orphans` exits 1 and says so in its log. In Radarr and Sonarr, Settings > Media Management, "Use Hardlinks instead of Copy" must be on (`configure.py` turns it on).

**Fix:** keep the downloads and the libraries on the same disk (one filesystem) under `MEDIA_ROOT`. Do not add separate volume mounts for the sub-folders in an override file: the apps must see everything through the single `/data` mount for hardlinks to work.

## The bot does not answer

**Check:**

```bash
docker compose ps moviebot
docker logs --tail 50 moviebot
```

**Fix, by what the log shows:**

- **"... is empty"** (`TELEGRAM_BOT_TOKEN`, `ALLOWED_TELEGRAM_USER_IDS`, `WEBHOOK_SECRET` or `RADARR_API_KEY`): the bot refuses to start without these. Fill them in `.env`, or run `./install.sh` (it reads `RADARR_API_KEY` from Radarr), then `docker compose up -d moviebot`.
- **`unauthorised user id=<id>`:** your id is not on the list, or you wrote from another account. Add the number to `ALLOWED_TELEGRAM_USER_IDS` and run `docker compose up -d moviebot`.
- **A token error from Telegram:** the token is wrong or was revoked. Get the current one from BotFather, put it in `.env` and run `docker compose up -d moviebot`.
- **A "Conflict" error:** another program reads the same bot's messages, for example a second copy of the bot on another server. Telegram allows only one. Stop the other one, or give each its own bot.
- **No log line at all when you write:** you are writing in a group. The bot only answers in a private chat.

To test the alert path of the host scripts: `python3 scripts/tg.py send "test"` prints `sent` or `not sent`.

## The webhook returns 403

Symptoms: no "is ready to watch" messages, and the Test button of the `moviebot` connection in Radarr or Sonarr fails.

**Check:**

```bash
docker logs moviebot 2>&1 | grep webhook
```

A line such as `webhook /radarr: wrong or missing secret (403)` means the secret in the app's connection URL does not match `WEBHOOK_SECRET`. In Radarr or Sonarr, Settings > Connect > moviebot, the URL must be `http://moviebot:8090/radarr/<WEBHOOK_SECRET>` (or `/sonarr/`). A "connection refused" error instead means the bot is not running.

**Fix:** run `./install.sh`. It restarts the bot with the secret from `.env` and `configure.py` writes the same secret into both apps. By hand: `docker compose up -d moviebot`, then `python3 configure.py --only radarr,sonarr`.

## Jellyseerr is not set up

Symptoms: Jellyseerr shows its setup page, the installer's summary asks you to sign in, or `/new` fails.

**Check:**

```bash
python3 configure.py --only jellyseerr --dry-run
```

It prints `first-run setup ... done` when Jellyseerr is set up.

**Fix:** open `http://<server>:5055`, sign in with your media server account and finish the wizard (you can skip its Radarr and Sonarr step). Then run `python3 configure.py --only jellyseerr`, which links Radarr and Sonarr. If `/new` says new releases are off, `JELLYSEERR_API_KEY` is empty: run `./install.sh` so it reads the key from `config/jellyseerr/settings.json` and restarts the bot with it.

## The guard jobs do not run

**Check:**

```bash
crontab -l
ls logs/
```

The crontab must hold a block between `# >>> media-stack >>>` and `# <<< media-stack <<<`, and `logs/` should hold a `.log` file per job after a few minutes. Also check that the cron service runs (`systemctl status cron`, or `crond` on some distributions).

**Fix:**

- **No block:** install cron with your package manager if it is missing, then run `python3 configure.py --only cron`. With `GUARDS_ENABLED=false` in `.env` the block is removed on purpose.
- **Wrong user:** the jobs run as the user whose crontab holds them. Run the installer as the user who should own them, not as root.
- **Your edits disappeared:** the block in the crontab is rewritten from `scripts/cron.template`. Change the schedule there, then run `python3 configure.py --only cron`.
- **A job runs but fails:** read its log in `logs/`, then run it with `--dry-run` (see above).
