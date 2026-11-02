# The Telegram bot

The bot (container `moviebot`, built from `moviebot/`) is the stack's Telegram assistant. It searches and adds titles, tells you what is downloading and what is ready, cleans up bad downloads, and shows the questions the guard scripts ask. This page describes everything it does.

- [Who can use it](#who-can-use-it)
- [Commands](#commands)
- [Searching and adding](#searching-and-adding)
- [When Radarr grabs a release](#when-radarr-grabs-a-release)
- [Ready messages](#ready-messages)
- [Buttons](#buttons)
- [Background jobs](#background-jobs)
- [The webhooks](#the-webhooks)
- [Settings](#settings)
- [Logs and state](#logs-and-state)

## Who can use it

- Only the Telegram user ids in `ALLOWED_TELEGRAM_USER_IDS` (numbers, comma separated). `ALLOWED_CHAT_IDS` is an older name for the same list; leave it empty.
- Only in a private chat with the bot. It ignores groups, even for allowed users, so results never land where other people can read them.
- Anyone else gets no reply at all. Their id is written to the bot's log as `unauthorised user id=<id>` (at most once every 10 minutes per id), so you can add someone you know. A stranger who taps a button gets an empty answer and nothing happens.
- Every allowed user receives every message the bot sends. For a maintenance question or a Cancel button, the first tap counts and the other copies lose their buttons.
- The bot refuses to start, and logs the reason, when `TELEGRAM_BOT_TOKEN`, `ALLOWED_TELEGRAM_USER_IDS`, `WEBHOOK_SECRET` or `RADARR_API_KEY` is empty. It never runs open or without its webhook secret.
- The token, the webhook secret, the API keys and the qBittorrent password are blanked out of its log lines.

## Commands

| Command | What it does |
|---|---|
| `/start`, `/help` | Shows what the bot can do, with the times of the daily jobs. It also says when new releases or the episode filler are off. |
| any other text | Searches Radarr (movies) and Sonarr (shows) for that title. See [Searching and adding](#searching-and-adding). |
| `/report` | Sends the report of the last 24 hours now. It also comes every day at `REPORT_HOUR`. |
| `/downloading`, `/status`, `/dl` | Live progress of up to 12 active qBittorrent downloads: a progress bar, percent, size, speed and time left. |
| `/new` | Checks the streaming services for new releases now. Needs Jellyseerr. |
| `/fill` | Runs the missing-episode filler now. Needs Sonarr. |

## Searching and adding

Send a title, for example `Big Buck Bunny`. The bot asks Radarr and Sonarr and shows up to four movies and up to four shows, numbered, with a button per number.

- A movie that is already in Radarr is marked "in library", and its button is a ⚡ to pick a release yourself (see [Buttons](#buttons)). A show that is already in Sonarr is marked with a check mark.
- **Add a movie:** tap its number. Radarr adds it, monitored, with minimum availability "released", and starts searching. The quality profile is `HD-1080p`; when that profile does not exist, the first profile with 1080 in its name, else the first profile. The library folder follows the movie's original language (`MOVIE_LIBRARIES`), and buttons let you file it in one of your other movie libraries instead. A "Not downloading? Force-grab a release" button lets you pick a release yourself.
- **If nothing was grabbed** `FALLBACK_GRACE_SEC` seconds after you added a movie (default 300), the bot sends up to three of the closest releases to approve. Or it tells you that only cinema recordings (CAM or TS copies) exist so far, or that your indexers have no release yet.
- **Add a show:** tap its number. Sonarr adds it with every season monitored. For each finished season the bot grabs one complete season pack: the healthiest one with at least 3 seeders that your custom formats do not score below zero. A season still airing is searched episode by episode, and new episodes come week by week. When a finished season has no suitable pack, the bot sends the closest releases to choose from (at most four such questions per show).

## When Radarr grabs a release

Radarr reports every grab to the bot through its webhook. For qBittorrent downloads, the bot checks the release against the movie's rules as they are set in Radarr: the qualities the quality profile allows, its minimum custom-format score, and the size limits per minute of runtime. Radarr applies these rules before an automatic grab, so the check catches grabs that bypassed them, such as a manual override in Radarr's own web page. The rules are read from Radarr each time; the bot keeps no copy of them.

- **The release meets the rules:** you get a "Downloading" message with the title, the release name, size, quality and release group, and a **Cancel this download** button. A note says when it is bigger than usual for its runtime. There is nothing to approve.
- **The release breaks a rule:** the bot stops the torrent at once and removes it through Radarr with a blocklist entry, so Radarr searches for a different release. It tells you which rule the release broke, with a **Pick a release myself** button. If Radarr will not remove it, the bot deletes that one torrent in qBittorrent and tells you that it is not blocklisted. If that fails too, the torrent stays stopped and the bot asks you to delete it by hand.
- Releases you picked in the bot (from the force-grab list or a closest-release question) pass without the check. A pick that never turns into a grab expires after 6 hours.
- The bot does not check Sonarr grabs. Sonarr applies its own rules, and the on-grab guard inside Sonarr cancels grabs of episodes you already have ([AUTOMATIONS.md](AUTOMATIONS.md#the-on-grab-guard)).

## Ready messages

- **Movies:** when Radarr imports a movie, the bot says "*Title* (*year*) is ready to watch!", or that it was upgraded to a better copy. The Cancel button on the download message goes away.
- **Episodes:** Sonarr imports are collected per show for 45 seconds, so a season pack gives one message, such as "*Show* S01E01-E10 ready to watch (WEBDL-1080p)".
- **Import lists:** when Radarr adds a movie from an import list (a watchlist, for example), the bot files it in the library for its language, announces it, and adds the library buttons and a force-grab button.

## Buttons

Every button sends a short code (the callback data) back to the bot. The code starts with a prefix that tells the bot what to do.

| Button | Callback data | What it does |
|---|---|---|
| A number in search results, **Add & download** on a new-release card | `addm:<TMDB id>` | Adds the movie to Radarr and starts the search (see [Searching and adding](#searching-and-adding)). |
| A number in search results, **Add (all seasons)** on a new-release card | `adds:<TVDB id>` | Adds the show to Sonarr with every season and starts the season-pack grabs. |
| ⚡ next to a movie in your library, **Not downloading? Force-grab a release**, **Pick a release myself** | `force:<Radarr movie id>` | Searches every indexer for the movie (up to two minutes) and lists the six releases with the most seeders, without cinema recordings. Each line shows size, quality, seeders and the release name, marked as meeting your settings, having objections, or 🚫 scored below zero by your custom formats. |
| A number in the force-grab list | `fg:<movie id>:<n>` | Grabs that release now, past every filter. The list is kept in memory: after a bot restart, search again. |
| A number in a closest-release question | `fba:<token>:<n>` | Grabs that release (for a movie, it passes the grab check as your pick). |
| **Skip** in a closest-release question | `fbx:<token>` | Leaves it for now. Tap ⚡ later to try again. |
| **Cancel this download** | `cancel:<torrent hash>` | Removes the download through Radarr with a blocklist entry, and Radarr looks for a different release. When Radarr cannot do it, the bot deletes the torrent in qBittorrent only, and says it is not blocklisted. A download that already finished or was imported is never deleted. If qBittorrent does not answer, the button stays so you can try again. |
| A library folder name (📁) | `mvroot:<movie id>:<language code or default>` | Moves the movie to that movie library. Radarr moves the files. |
| **Not interested** on a new-release card | `nrskip` | Marks the card as skipped. The title is not suggested again. |
| **Get 720p** | `g720:<series id>:<season>` | Grabs the 720p copies the episode filler offered for that season. |
| **Leave it** | `g720no:<series id>:<season>` | Keeps the season as it is. The filler looks for 1080p again in about three months. |
| **Yes**, **No** on "A maintenance job asks" | `cok:<id>`, `cno:<id>` | Answers a host script's question. The first answer wins. See [Yes/No questions from the host scripts](#yesno-questions-from-the-host-scripts). |

## Background jobs

| Job | When | Settings | What it does |
|---|---|---|---|
| App defaults | At start-up, retried every 20 seconds until Radarr answers | | Reads the quality profile and root folders for new titles. The bot answers commands meanwhile. |
| Maintenance questions | Every 4 seconds | `STATE_DIR` | Shows the Yes/No questions of the host scripts. |
| Dead-torrent cleanup | Every 10 minutes | `DEAD_MINUTES` (30) | Removes torrents that nobody in the swarm has. |
| Fake-download cleanup | 3 minutes after start, then every 15 minutes | | Removes downloads that are archives or programs instead of video. |
| Disk watch | 5 minutes after start, then every 6 hours | `DISK_ALERT_PCT` (80) | Warns when a disk is that full, at most once a day per disk. |
| Daily report | Every day at `REPORT_HOUR`:00 (9:00) | `REPORT_HOUR` | Sends the report of the last 24 hours. |
| New releases | Every day at `NEW_RELEASES_HOUR`:00 (18:00) | `NEW_RELEASES_*`. Needs `JELLYSEERR_API_KEY`. | Sends new-release cards. |
| Missing-episode filler | Every day at `FILL_HOUR`:30 (7:30) | `FILL_SEASONS_PER_DAY` (4), `EPISODE_CAP_GB` (2.5). Needs `SONARR_API_KEY`. | Fills episodes that Sonarr's size cap left missing. |

Times are in your time zone (`TZ`). When a daily job fails, the bot logs it and runs it again at its next scheduled time. Without `JELLYSEERR_API_KEY` the new-release job is off; without `SONARR_API_KEY` the filler and the TV features are off.

### Dead torrents

A torrent counts as dead when it is stalled, nobody in the swarm has the file (no seeders) or wants it (no peers), it has downloaded nothing, and it was added more than `DEAD_MINUTES` minutes ago. The bot removes it through Radarr or Sonarr with a blocklist entry and without an immediate new search. A dead torrent that no app tracks is deleted in qBittorrent only when its category is `radarr` or `sonarr`, so a torrent you added by hand is never touched. You get a "Cleaned up dead torrents" message.

The host script `dead_torrent_reaper` is a second, broader check that also handles partly downloaded and stopped torrents ([AUTOMATIONS.md](AUTOMATIONS.md#dead_torrent_reaper)).

### Fake downloads

For each item in the Radarr and Sonarr queues, the bot reads the torrent's file list from qBittorrent:

1. A video file larger than 50 MB means the download is real, even when a stray program sits next to it. Some release groups ship one; the video is what counts.
2. Otherwise a program (`.exe`, `.scr`, `.bat`, `.cmd`, `.lnk`, `.msi`, `.js`, `.vbs`, `.ps1`, `.jar`, `.pif`) makes it a fake: "had a program file instead of a video, likely malware".
3. Otherwise an archive (`.rar`, `.zip`, `.7z`, split parts such as `.r00` or `.001`, `.iso`, `.tar`, `.gz`) makes it a fake: "was a packed archive, not a video".
4. The app's own status message "unsupported extension" for `.7z`, `.rar`, `.zip`, `.iso`, `.exe`, `.scr` or `.lnk` counts too.

A fake is removed through the app with a blocklist entry, and the app searches for a real copy. A torrent whose file list is not known yet is checked again next time. You get a "Removed fake downloads" message.

### Disk alerts

The bot checks the disks behind the downloads, movies, TV and extra library folders (as it sees them under `/data`), plus the container's own disk, which is Docker's storage on the server ("System disk"). Folders on the same disk are reported once. At or above `DISK_ALERT_PCT` percent, it sends at most one alert per disk per day, for example "Media disk (downloads, movies, tv) is 85% full: 1.2 TB left." The daily report shows the same percentages.

### Daily report

"Daily report: last 24 hours" lists:

- movies imported: how many, their total size, and up to 10 titles;
- TV episodes imported: how many, their total size, and the number of episodes per show (up to 12 shows);
- the total size;
- the movie that took longest and the one that was fastest from grab to import, with their sizes;
- how many torrents are still downloading;
- the disk use of each disk.

When nothing finished, it says "Nothing finished downloading", with the still-downloading count and the disk line.

### New-release cards

At `NEW_RELEASES_HOUR`, and when you send `/new`, the bot asks Jellyseerr for the most popular titles that recently came out on streaming services.

- `NEW_RELEASES_REGIONS`: the countries whose services count, as two-letter codes (`US,GB,CA,AU` in `.env.example`).
- `NEW_RELEASES_SERVICES`: `all`, or a comma-separated list of service names as TMDB spells them.
- `NEW_RELEASES_DAYS_MOVIES` (90) and `NEW_RELEASES_DAYS_TV` (45): how far back a movie's release date or a show's first air date may be.
- `NEW_RELEASES_LANG_CAPS`: picks per original language and day, as `<language>:<movies>:<shows>`, for example `en:5:3,fr:2:1`. The first language only gets titles with at least 10 votes on TMDB, which keeps obscure filler out; the other languages have no such floor.

Titles already in Radarr or Sonarr, or already requested in Jellyseerr, are skipped. Each title is suggested once: the bot remembers its suggestions for a year in `state/new_releases_seen.json`.

A card shows:

- the picture, the title and year, the genres, and the runtime (or the number of seasons);
- where it streams (services and countries), and where to rent or buy it;
- up to five cast members and the story, shortened to fit;
- the published critic and audience scores that Jellyseerr provides.

Its buttons are **Add & download** (movies) or **Add (all seasons)** (shows), and **Not interested**.

### The missing-episode filler

Sonarr stores its size limit per minute of runtime, so a limit per episode cannot be set there, and short episodes often have every copy refused for size ([AUTOMATIONS.md](AUTOMATIONS.md#the-per-minute-cap-trap) explains why). The filler fills those gaps.

1. It looks at monitored series with missing episodes. In each season it counts the episodes that aired more than a day ago, are monitored, have no file and are not downloading.
2. It handles up to `FILL_SEASONS_PER_DAY` seasons per run, seasons it never tried first. A season it tried is left alone for 21 days.
3. It runs one season search per season (every indexer is asked, which can take minutes), with 10 seconds between seasons.
4. It only takes 1080p copies with at least 5 seeders that are not cinema recordings. Each episode must stay within `EPISODE_CAP_GB` (for a pack, its size is divided by the number of episodes in the season). Sonarr must accept the copy, or refuse it only because of its size cap. For a pack, refusals because of episodes you already have are fine too: those files stay as they are.
5. **The half-season pack rule.** When the whole season is missing, it takes the best pack with at least 10 seeders. Otherwise it picks single episodes, one by one. A pack is used only when no single copy exists and at least half of the season is missing, so a season you mostly have is never downloaded again for one or two gaps.
6. **The 720p offer.** When episodes are still missing and a 720p copy exists, the bot asks whether to get it, with the number of seeders and the size, and the buttons **Get 720p** and **Leave it**. 720p is never downloaded without your tap. After **Leave it**, the season is looked at again in about three months.

What it grabbed comes as "Filled missing episodes (healthy copies under 2.5 GB)", one line per season.

### Yes/No questions from the host scripts

Telegram lets only one program read a bot's messages, and that is the bot. A host script that needs an answer therefore asks through files in `state/confirms/` (the bot sees this folder as `/state/confirms`):

1. The script writes `<id>.ask`, holding JSON such as `{"text": "..."}` or plain text.
2. Every 4 seconds the bot looks for new `.ask` files, deletes them, and sends the question as "A maintenance job asks" with **Yes** and **No** to every allowed user.
3. The first tap writes `<id>.reply` with `yes` or `no`. The other copies lose their buttons, and a second tap only shows "Already answered".
4. The script reads the reply. Anything other than an explicit `yes` counts as no. Replies nobody collected are removed after 7 days.

`size_cap_watch` uses this to ask before it downloads an episode that only Sonarr's size cap blocks. You can try the exchange yourself:

```bash
python3 scripts/tg.py ask "Test question from media-stack" 120
```

It prints `yes` or `no`, or `__TIMEOUT__` when nobody answered within 120 seconds. The bot and the host scripts must run as the same user (`PUID` and `PGID`), so the bot can delete and write files in `state/confirms/`. The bot logs a warning at start-up when it cannot write there.

## The webhooks

Radarr and Sonarr call the bot at `http://moviebot:8090/radarr/<WEBHOOK_SECRET>` and `http://moviebot:8090/sonarr/<WEBHOOK_SECRET>`. `configure.py` sets this up as the `moviebot` connection in each app: on grab, import and upgrade, and in Radarr also when a movie is added.

- The secret is part of the path. A call with a wrong or missing secret gets HTTP 403, and the bot logs it at most once every 10 minutes per app.
- The port (`WEBHOOK_PORT`, default 8090) is not published on the server. Only containers on the stack's `media` network can reach it.
- Radarr events: Test ("Radarr is connected to the bot."), Grab (the check above), Download and MovieFileImported (ready message), MovieAdded from an import list. Sonarr events: Test, Download and EpisodeFileImported. Every other event is accepted and ignored.
- Any other path gets 404. The container's health check counts any answer as "alive".

## Settings

All in `.env`. After a change, run `docker compose up -d moviebot`.

| Setting | Default | Used for |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | | The bot's token from BotFather. Required. |
| `ALLOWED_TELEGRAM_USER_IDS` | | Who may use the bot. Required. |
| `ALLOWED_CHAT_IDS` | | Older name of the list above; leave empty. |
| `WEBHOOK_SECRET` | generated | The secret webhook path. Required. |
| `WEBHOOK_PORT` | 8090 | The webhook port inside the stack's network. |
| `RADARR_API_KEY` | read by the installer | Required. |
| `SONARR_API_KEY` | read by the installer | Shows and the filler. |
| `JELLYSEERR_API_KEY` | read by the installer | New-release cards. |
| `QBIT_USERNAME`, `QBIT_PASSWORD` | `admin`, generated | qBittorrent's web UI login. |
| `PREFERRED_LANGUAGE` | English | Release lists mark releases that carry other languages. |
| `REPORT_HOUR` | 9 | The daily report. |
| `NEW_RELEASES_HOUR` | 18 | The new-release cards. |
| `NEW_RELEASES_REGIONS`, `NEW_RELEASES_SERVICES`, `NEW_RELEASES_LANG_CAPS`, `NEW_RELEASES_DAYS_MOVIES`, `NEW_RELEASES_DAYS_TV` | see [New-release cards](#new-release-cards) | Which titles qualify. |
| `FILL_HOUR`, `FILL_SEASONS_PER_DAY` | 7, 4 | The missing-episode filler. |
| `EPISODE_CAP_GB` | 2.5 | The filler's per-episode size limit. |
| `DEAD_MINUTES` | 30 | How old a dead torrent must be before it is removed. |
| `DISK_ALERT_PCT` | 80 | The disk alert threshold. |
| `FALLBACK_GRACE_SEC` | 300 | How long after an add the bot waits before it offers the closest releases. |
| `MOVIE_LIBRARIES`, `DOWNLOADS_DIR`, `MOVIES_DIR`, `TV_DIR` | | Folder names below `/data`. |
| `STATE_DIR` | `./state` | The host folder mounted as `/state` in the bot. |
| `TZ` | | The time zone of the daily jobs. |

The bot reaches the apps by their container names (`radarr:7878`, `sonarr:8989`, `gluetun:8080`, `jellyseerr:5055`). The `*_URL` settings in `.env` are for the host scripts only.

## Logs and state

- Log: `docker logs moviebot`, or `docker compose logs -f moviebot` to follow it.
- State in `state/`: `new_releases_seen.json` (suggested titles), `missing_fill.json` (when each season was last tried), `disk_alerts.json` (when each disk last alerted) and `confirms/` (the questions and answers).
