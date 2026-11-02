# Automations

The guard scripts in `scripts/` run on the server from cron. They watch for failures that the apps do not report, or report too late, and fix the ones that are safe to fix. This page covers every job: what it does, why it exists, when it runs, what it never does, its files, its flags and its alerts. The [table at the end](#all-jobs-at-a-glance) sums it up.

- [How the jobs run](#how-the-jobs-run)
- [The per-minute cap trap](#the-per-minute-cap-trap)
- The jobs: [regrab_guard](#regrab_guard), [link_orphans](#link_orphans), [vpn_watchdog](#vpn_watchdog), [verify_imports](#verify_imports), [missing_sweep](#missing_sweep), [library_guard](#library_guard), [config_backup](#config_backup), [integrity_scan](#integrity_scan), [subtitle_sweep](#subtitle_sweep), [update_check](#update_check), [restore_drill](#restore_drill), [dead_torrent_reaper](#dead_torrent_reaper), [malware_guard](#malware_guard), [mount_guard](#mount_guard), [arr_health_guard](#arr_health_guard), [size_cap_watch](#size_cap_watch), [naming_guard](#naming_guard)
- [WARNING: the space-saver (shrink_oversized and resolve_shrink_imports)](#warning-the-space-saver)
- [Helpers that are not cron jobs](#helpers-that-are-not-cron-jobs)
- [All jobs at a glance](#all-jobs-at-a-glance)

## How the jobs run

- **Installing the schedule.** `python3 configure.py --only cron` (the installer runs it) copies `scripts/cron.template` into your crontab, between the lines `# >>> media-stack >>>` and `# <<< media-stack <<<`, with the stack folder filled in. To change a schedule, edit `scripts/cron.template` and run that command again; edits inside the block in the crontab itself are overwritten. `GUARDS_ENABLED=false` in `.env` removes the block. `subtitle_sweep` is only installed with the `subtitles` profile.
- **The wrapper.** Each line calls `scripts/run_guard.sh <job>`. It runs `scripts/<job>.py` (or `scripts/<job>.sh`) from the stack folder and appends the output to `logs/<job>.log`, under a `===== <date and time> <job>` header and a `----- exit <code> after <n> s` footer. It keeps each log under about 5 MB by dropping its oldest half, and it skips a run while the previous run of the same job is still going.
- **Uptime Kuma.** When a job exits 0 and `KUMA_PUSH_<JOB>` is set in `.env`, the wrapper calls that push URL. Create a Push monitor in Uptime Kuma for a job, copy the push URL Uptime Kuma shows into the matching setting (for example `KUMA_PUSH_MOUNT_GUARD`), and the monitor turns red when the job fails or stops running.
- **Exit codes.** 0 means healthy, 1 means the job found a problem it wants a monitor to show, 2 means the job itself failed (often because an app it needs did not answer). The table at the end gives each job's meaning.
- **Dry runs.** Every job takes `--dry-run`: it reports what it would do, changes nothing and sends nothing. Run it directly to see the output on screen:

  ```bash
  python3 scripts/regrab_guard.py --dry-run
  ```

- **Settings.** The jobs read `.env` through `scripts/settings.py` on every run, so a change applies to the next run. `python3 scripts/settings.py` prints what they see.
- **State.** Each job keeps its memory in `STATE_DIR` (default `state/`), in small files whose names start with a dot. Deleting one is safe; at worst an alert is sent again.
- **Alerts.** They go to Telegram, to `NOTIFY_CHAT_ID` or else the first id in `ALLOWED_TELEGRAM_USER_IDS`: a title line, short sections, bullet points. Most jobs send a problem once, when it first appears, and summarise problems that last at most once a day. When Telegram cannot be reached, the text waits in `state/tg_undelivered.log` and goes out after the next message that gets through.
- **The mount canary.** A job that reads the media folders first checks that `MEDIA_ROOT` and every `MOUNT_PATHS` entry exist. If one is missing, it skips the run and says so at most once a day. A missing share must never look like an empty library.
- **The shared lock.** The jobs that change the downloads folder (`regrab_guard`, `link_orphans`, `dead_torrent_reaper`, `malware_guard`, `resolve_shrink_imports`) share one lock, so two of them never work on the same files at the same time.

## The per-minute cap trap

Radarr and Sonarr do not store a size limit per file. Their quality definitions hold a minimum, preferred and maximum size in MB per minute of runtime, and the limit for a release is that number times the runtime.

Say you want "at most 2.5 GB per episode". To turn that into a number per minute you have to pick a runtime, and the only safe choice is the longest show you follow, or its episodes get refused. For a 65-minute drama that is 2.5 GB / 65 minutes, about 38 MB per minute (`TV_SIZE_CAP_MB_PER_MIN=38`). The same cap now applies to a 22-minute sitcom: 38 MB x 22 minutes is about 0.84 GB. Normal 1080p copies of short episodes are often bigger than that, so Sonarr refuses all of them for size, says nothing, and the episodes stay missing.

A higher per-minute cap would let oversized copies of long episodes through. A custom format with a size condition cannot fix it either: it measures the whole release, and for a season pack that is the whole season, so any limit low enough for single episodes would block every pack.

So media-stack keeps the per-minute cap for automatic grabs and adds two helpers that think in whole episodes:

- `EPISODE_CAP_GB` (default 2.5) is the real limit per episode.
- The bot's daily missing-episode filler takes copies that Sonarr refused for size alone, as long as each episode stays within `EPISODE_CAP_GB` (for a pack: its size divided by the number of episodes in the season). See [BOT.md](BOT.md#the-missing-episode-filler).
- [size_cap_watch](#size_cap_watch) looks at recently aired episodes and asks you on Telegram before it downloads one that only the size cap blocks.

`configure.py` writes the caps into the apps: `TV_SIZE_CAP_MB_PER_MIN` (default 38) into Sonarr's 1080p qualities, and `MOVIE_SIZE_CAP_MB_PER_MIN` (default 25) into Radarr's 720p, 1080p and 2160p qualities. After you change one in `.env`, run `python3 configure.py --only radarr,sonarr`.

## regrab_guard

Cancels downloads of things the library already has.

- **Why:** Radarr and Sonarr decide what is missing from their own database, not from the disk. A file they cannot match (a folder named after the release, a different episode numbering, a library imported by hand) looks missing, so they download it again, and again.
- **What it does:** it looks at what is really in the library folders. When an app is downloading something that is already there, it cancels that download, blocklists the release and rescans, so the app adopts the copy you have. It also clears two dead ends that are safe to resolve. A Radarr movie that is "owned but missing" gets a rescan, and when its file sits in another folder inside a Radarr root folder, the movie is pointed at that folder; nothing moves on disk. An import that is blocked only because the release name differs from the title is finished with the id the app recorded, but only when the download holds exactly one main video file.
- **When:** every 10 minutes, holding the shared lock.
- **Never:** deletes or moves a library file. Counts a file in the downloads folder as owned. Cancels a season pack unless every episode in it is owned. Cancels an upgrade of a file the app rates below the profile cutoff when `ALLOW_UPGRADES=true`. Runs while the media folder is missing.
- **Files:** `.libindex.json` (the library index, rebuilt about once an hour; a match is checked on disk again before anything is cancelled) and `.regrab_guard_state.json`. It leaves alone the episodes listed in `.shrink_pending.json` by the optional space-saver.
- **Flags:** `--dry-run`, `--rebuild-index` (ignore the cached index).
- **Alerts:** "Re-download guard", with what it fixed and what needs your attention. Open issues are summarised once a day.
- **Exit:** 0 ran; 2 neither Radarr nor Sonarr answered.

## link_orphans

Files movies that are stranded in the downloads folder into the library.

- **Why:** a collection or pack that Radarr imported only in part, or a movie you downloaded by hand, sits in the downloads folder, where no media server looks.
- **What it does:** it looks for video files over 300 MB in the downloads folder that are older than two hours and not part of an active Radarr download. The name must carry a year and no episode number, and the movie must not be in the library yet: no file with the same name, no library folder with the same title and year that holds a video, and no earlier hardlink of the very same file. Each one it finds is hardlinked into the library. When Radarr has the movie, the link goes into Radarr's own folder for it and Radarr rescans. Otherwise Radarr's lookup gives the title and year, and the link goes into "Title (Year)" in the library for the movie's original language. When it filed something and Plex is set up, it asks Plex to scan.
- **When:** every 4 hours, at 25 past, holding the shared lock.
- **Never:** copies, moves or deletes anything; it only creates folders and hardlinks. Matches by size alone (two different movies can have the same size). Guesses: with no confident match it leaves the file and says so.
- **Files:** none of its own.
- **Flags:** `--dry-run`.
- **Alerts:** "Movie filing report": what it filed, what it could not file, and what it was unsure about.
- **Exit:** 0 ran; 1 hardlinks are impossible here (the downloads and the library are on different filesystems); 2 Radarr did not answer.

## vpn_watchdog

Repairs qBittorrent when it stops reaching peers after the VPN reconnects.

- **Why:** Gluetun sometimes builds a new tunnel: after a health restart, a provider drop or a forwarded port that lapsed. qBittorrent can then keep its sockets on the old tunnel and silently stop reaching peers, while everything Gluetun does itself keeps working. It looks as if the internet is fine.
- **What it checks:** qBittorrent's connection status is "disconnected"; it is "firewalled" (checked only with `VPN_PORT_FORWARDING=on`); it has fewer than 10 DHT nodes (checked only when DHT is on in qBittorrent); the forwarded port differs from qBittorrent's listening port (with port forwarding on).
- **What it does:** a wrong port is fixed at once, by running `setport.sh` inside Gluetun. For everything else it waits for 3 bad checks in a row (15 minutes), then runs `docker compose restart gluetun`, restarts the containers that share Gluetun's network (qbittorrent), and checks for up to 5 minutes whether that worked.
- **It also reports**, once each: the media folder disappearing, the server's root disk under 10 GB free (at most daily), and magnet links stuck fetching metadata for more than a day.
- **When:** every 5 minutes. It does nothing when `VPN_MODE=off`.
- **Never:** acts on a single bad check; acts while downloads run faster than 100 KB/s; restarts the tunnel more than once an hour; starts a qBittorrent you stopped on purpose (`downloads-off.sh`).
- **Files:** `.vpn_watchdog_state.json`.
- **Flags:** `--dry-run`.
- **Alerts:** "VPN watchdog" after a repair, whether it worked or not, and an all-clear when a failed repair recovers later. "Media watchdog" for the extra checks.
- **Exit:** 0 healthy or recovering; 1 still broken after the threshold, or the repair did not work; 2 the job failed.

## verify_imports

Checks that every newly imported file actually plays.

- **Why:** Radarr and Sonarr import on name and size. A zero-filled or cut-off file can sit in the library for weeks until someone presses play.
- **What it does:** it reads both apps' import history and runs `ffprobe` on each new file, up to 100 per run. A failed probe is tried again after a minute, because a file on a network share may still be settling.
- **Optional self-heal:** `VERIFY_IMPORTS_HEAL=true` (off by default). Sometimes a library file cannot be read while the download copy in the history has the same size and plays. The library file is then rebuilt from that copy (written next to it, checked, then swapped in), and the app rescans. On any doubt it does nothing and the normal alert goes out. The copy uses extra disk space.
- **When:** at 7 and 37 minutes past every hour.
- **Never:** deletes anything.
- **Needs:** `ffprobe`. It uses the one on the server (install the `ffmpeg` package), else the one inside the Bazarr container (`subtitles` profile), else the `lscr.io/linuxserver/ffmpeg` image if it is already on the machine. It never downloads one. Without any of them it logs that the imports stay unchecked and exits 2, so a push monitor shows it; nothing is reported as corrupt.
- **Files:** `.verify_imports_state.json` (the history entries it has checked) and its own lock, `.verify_imports.lock`.
- **Flags:** `--dry-run`.
- **Alerts:** "Corrupt import found", with the files and how to get them again (mark the download as failed in the app's History). "Self-healed an unreadable import" after a repair.
- **Exit:** 0 every new import plays; 1 at least one does not; 2 neither app answered, or no ffprobe is available.

## missing_sweep

Searches, on a small budget, for monitored episodes and movies that are still missing.

- **Why:** Radarr and Sonarr watch the release feeds only for things released after you add them. The earlier episodes of a show added mid-season, or an older movie whose first search found nothing, stay missing until someone searches by hand.
- **What it does:** each run starts at most 6 season searches, 10 episode searches (one per season with gaps) and 8 movie searches, 20 seconds apart. A season that is missing completely gets one season search; a season you partly have gets an episode search, so a pack never downloads episodes you already own. Each item is searched again at most every 21 days.
- **It stands down** when 40 or more items are already queued, unless nothing was started for 3 days (then it pushes a minimal batch through), and when no indexer is enabled. When some indexers are failing, it spends a third of its budget.
- **When:** daily at 05:15.
- **Never:** fires hundreds of searches at once. Bursts like that get a server rate-limited or banned by indexers, and every search of an indexer behind Cloudflare costs a slow FlareSolverr solve.
- **Files:** `.missing_sweep_state.json`, plus a heartbeat on every run, including runs that stand down, which [library_guard](#library_guard) watches.
- **Flags:** `--dry-run` (lists what it would search).
- **Alerts:** "Back-catalogue sweep", with the searches it started.
- **Exit:** 0 ran (also when it stood down); 1 no indexer is enabled; 2 Sonarr or Radarr did not answer.

## library_guard

Watches the search plumbing and how complete the library is.

- **Why:** when searching breaks, nothing new arrives and the library quietly stops filling. When the sweep stops running, the gaps quietly stay.
- **What it checks:** indexers in Prowlarr that are switched off or failing, and no working indexer at all; FlareSolverr, when an indexer uses it (probed from inside the Prowlarr container, because its port is not published); the heartbeat of `missing_sweep` (more than 48 hours old means the job has stopped); Python files in `scripts/` that share a name with a standard-library module and would break every guard.
- **Weekly report (Sundays):** series with missing aired episodes and released movies that are still missing.
- **When:** daily at 09:40.
- **Never:** changes anything.
- **Files:** `.library_guard_state.json` (the date of its first run, for a grace period).
- **Flags:** `--dry-run`.
- **Alerts:** "Library watchdog" for new problems only; "Weekly library check" on Sundays.
- **Exit:** 0 healthy; 1 a serious problem (no working indexer, FlareSolverr down, Prowlarr unreachable, the sweep not running, an app scan failing).

## config_backup

Backs up the stack's configuration every night.

- **Why:** the media can be downloaded again; the configuration cannot. Losing it means rebuilding series and their monitoring, root folders, indexers, profiles, custom formats and the bot's state by hand.
- **What it does:** it asks Radarr, Sonarr and Prowlarr for their own backup (a consistent zip of their database), waits for it and copies it. It archives the settings of Jellyseerr and qBittorrent (its settings, not its list of torrents), those of Bazarr, Tautulli and Uptime Kuma when their profile is on, the `state/` folder, and `.env` (with `docker-compose.override.yml` when you have one). Caches and logs are skipped, and databases are copied in a way that is safe while the apps run. Every archive is opened and read in full before it counts. The newest 7 per app are kept.
- **Where:** `BACKUP_DIR` (default `backups/`), as `radarr-YYYYMMDD.zip`, `jellyseerr-YYYYMMDD.tgz`, `env-YYYYMMDD.tgz` and so on. The folder is mode 700 and the files mode 600, because they hold API keys and `.env`. Point `BACKUP_DIR` at a different disk from `config/`, or a single disk failure takes both.
- **When:** nightly at 03:30.
- **Never:** touches media. Accepts an app backup older than 30 minutes (that would be old data under today's date). Deletes a file it did not name.
- **Flags:** `--dry-run` (lists what it would back up and asks no app for a backup).
- **Alerts:** "Config backup incomplete" when a backup is missing or stale. Success is silent.
- **Exit:** 0 all backups fresh and verified; 1 at least one is missing or stale; 2 the job failed.
- **Restoring:** Radarr, Sonarr and Prowlarr restore their zip themselves, from System > Backup in their web page. Each `.tgz` holds one folder named after the app: stop the container, move its current folder aside, and unpack the archive into `config/`, for example `docker compose stop jellyseerr`, `mv config/jellyseerr config/jellyseerr.old`, `tar -xzf backups/jellyseerr-YYYYMMDD.tgz -C config`, `docker compose start jellyseerr`. A `state-*.tgz` unpacks into the stack folder (`-C .`), and `env-*.tgz` holds `.env`.

## integrity_scan

A rolling, read-only health check of the whole library.

- **Why:** files rot, imports go wrong, and network shares sometimes hand back garbage. A broken file is usually found when someone sits down to watch it. Finding damage early, while the title can still be downloaded again, is the only defence when there is no second copy.
- **What it does:** it runs `ffprobe` on every file that is due and records the result. Every file is checked again weekly, and at once when its size or date changes. The first run covers the whole library; later runs handle about a seventh of it. `ffprobe` reads headers, not whole files, and a short pause between files lets playback win any competition for the disk. A run stops after 40 minutes at most; the rest rolls over.
- **Scope:** the movie and TV libraries, minus `INTEGRITY_EXCLUDE` (comma separated: absolute paths, paths relative to `MEDIA_ROOT`, or folder names matched anywhere). The downloads and backup folders, hidden folders, NAS system folders and extras or sample folders are never scanned.
- **When:** daily at 07:15.
- **Never:** moves, renames or deletes anything.
- **Needs:** `ffprobe`, found the same way as for [verify_imports](#verify_imports). Without it, it sends "Library check cannot run" once and stops.
- **Files:** `.integrity_state.json`.
- **Flags:** `--dry-run` (scans and prints, sends nothing, keeps the state file as it was).
- **Alerts:** "Damaged files found", once per file.
- **Exit:** 0 normal (damaged files are reported by message); 2 the job failed.

## subtitle_sweep

Chases missing subtitles the way `missing_sweep` chases missing episodes. Only installed with the `subtitles` profile (Bazarr).

- **Why:** Bazarr knows which subtitles are missing, but its own retries are conservative, and a title that had no subtitle on release day is rarely looked at again once one appears.
- **What it does:** it starts at most 12 episode and 4 movie subtitle searches per run, 6 seconds apart, and searches the same item at most every 14 days. Bazarr writes the subtitle next to the video, as it normally does.
- **When:** daily at 08:10.
- **Never:** moves, renames or deletes anything. Without the `subtitles` profile it logs that and exits 0.
- **Files:** `.subtitle_sweep_state.json`.
- **Flags:** `--dry-run`.
- **Alerts:** "Subtitle sweep", with the titles it searched.
- **Exit:** 0 ran (or nothing to do); 1 Bazarr runs but no API key can be found.

## update_check

Says which containers have newer images. It never applies them.

- **Why notify only:** an update can break a working stack (a changed default, a renamed setting), and an update applied unattended at night is found broken in the morning. Knowing what is available and choosing when to update is the safer trade.
- **What it does:** for each container of this compose project, it compares the digest of the image tag in the registry with the digest of the image the container runs. It downloads nothing: pulling an image would make any later `docker compose up -d` (even one a guard runs to repair something) move containers onto versions nobody chose. The bot (built locally) and images pinned by digest are skipped.
- **When:** Mondays at 09:00.
- **Never:** pulls an image or restarts a container.
- **Files:** `.update_check.json` (the updates it already told you about).
- **Flags:** `--dry-run`.
- **Alerts:** "Container updates available", only when a container joins the list, so one you deliberately keep on its version does not nag every week. The message says how to update (`docker compose pull`, then `docker compose up -d`).
- **Exit:** 0 checked (with or without updates); 2 Docker is not usable, or the job failed.

## restore_drill

Proves that the configuration backups actually restore.

- **Why:** a backup that was never restored is only a hope.
- **What it does:** it takes the newest Sonarr backup (`sonarr-YYYYMMDD.zip` in `BACKUP_DIR`), unpacks it into a scratch folder and checks the database's integrity. Then it starts a throwaway Sonarr container (`sonarr-restoredrill`) on the copy and compares its series count with the live Sonarr (a difference of up to 2 is fine).
- **Isolation:** the throwaway container runs on its own Docker network (`restoredrill-net`), so it cannot reach qBittorrent, Prowlarr or anything else by name. It publishes its port only on localhost (8991). It gets a copy of the configuration, never the real `config/sonarr`. It uses the image the live Sonarr already runs and never pulls one. The container, the network and the scratch folder are removed afterwards, even on failure.
- **When:** Sundays at 11:00.
- **Never:** touches the live Sonarr, its settings or any media file.
- **Flags:** `--dry-run` (unpacks and checks the database only; starts no container).
- **Alerts:** "Backup restore test FAILED", with what went wrong. Success is silent.
- **Exit:** 0 the drill passed; 1 it failed; 2 the job itself failed.

## dead_torrent_reaper

Clears torrents that can never finish, before they freeze the download queue.

- **Why:** qBittorrent counts a stalled download as active. A torrent whose swarm has no seeders never finishes and never leaves that state, so it holds an active slot forever. They pile up until the active limit is reached, and then the whole queue freezes: speed zero, healthy torrents waiting behind dead ones. Nothing in qBittorrent or the apps cleans them up in time.
- **The test:** qBittorrent reports how many seeders exist in the swarm and how many it is connected to. Only "no seeder exists" means dead. Seeders that exist while qBittorrent reaches none point at your own connection (the VPN or the forwarded port), so those torrents are left alone and reported instead.
- **What it does:** a torrent that looks dead for 45 minutes is removed through Sonarr or Radarr with a blocklist entry, so the same release is never grabbed again, and a search for a replacement starts. A dead torrent that no app tracks is deleted in qBittorrent; its files are deleted only when they are inside the download folders. Stopped or paused downloads report no swarm information, so they are never judged in that state: they are resumed (once a day each) and judged an hour later on real numbers. Magnet links that are still fetching metadata after 3 hours count as dead too.
- **The pause flag:** create `state/.downloads_paused` (for example `touch state/.downloads_paused`) to stop it resuming stopped downloads. The flag counts for 24 hours after it was last touched.
- **When:** every 30 minutes, holding the shared lock.
- **Never:** removes more than 25 torrents in one run (a tracker outage cannot empty the queue); removes a torrent an app tracks without that app's blocklist (it retries next run instead); deletes a library file; judges a stopped torrent. A stopped qBittorrent (downloads switched off) is not an error.
- **Files:** `.dead_reaper_state.json`.
- **Flags:** `--dry-run`.
- **Alerts:** "Download cleanup": what it restarted, what it cleared, and, at most daily, when 3 or more downloads have seeders but cannot reach any (usually the forwarded port).
- **Exit:** 0 normal; 1 qBittorrent did not answer; 2 the job failed.

The bot has a quicker, narrower cleanup of its own (torrents with no seeders and no peers that downloaded nothing): see [BOT.md](BOT.md#dead-torrents).

## malware_guard

Makes sure nothing executable stays in the download folders.

- **Why:** fake releases are a common way to spread malware: a "video" that is really a program, or a real video with a program next to it. A media library never needs a program, and one sitting in a download folder is a double-click away from trouble on any device that opens the share.
- **Three layers:**
  1. **Prevention:** qBittorrent's "excluded file names" list holds the program, script and shortcut extensions, so those files are never downloaded. `configure.py` sets the list, and this job sets it again on every run in case it was switched off.
  2. **Detection:** every file in the download folders (and in qBittorrent's incomplete folder, when one is set) is judged by its first bytes, not its name, because payloads often have no extension: Windows, Linux and Mac programs, Java classes, Windows shortcuts, scripts, and zip archives that contain programs. Text scripts and web shortcuts with no binary signature are caught by their extension.
  3. **Removal, per torrent:** when the program is the whole download (no real video or audio left), it is a fake release, removed through Sonarr or Radarr with a blocklist entry so a replacement is searched. When the program is an extra inside a real release, that one file is set to "do not download" and deleted, and the video keeps downloading and seeding. A leftover that no torrent owns any more is deleted.
- **When:** every 10 minutes, holding the shared lock.
- **Never:** deletes anything outside the download folders (checked by real path, so a symbolic link cannot lead elsewhere). Runs when a download folder is the media folder itself or contains a library folder. Looks at the library. Deletes a file a torrent still owns unless qBittorrent lists that exact path with that exact size. Plain-deletes a fake whose app queue cannot be read (it would escape the blocklist and come back): it waits instead. Opens RAR or 7z archives: those are reported once and left in place. Acts on more than 25 torrents and 50 leftover files per run.
- **Files:** `.malware_guard_state.json` (files already found clean, by path, size and date, so each is read once).
- **Flags:** `--dry-run`.
- **Alerts:** "Download safety: programs blocked" for what it removed, "Download safety: needs a look" for problems (once each), and "Download safety check refused to run" when the folders overlap.
- **Exit:** 0 normal; 1 qBittorrent did not answer, or an action failed; 2 the job failed, or it refused to run because the folders overlap.

## mount_guard

Keeps the media storage mounted, responsive and visible inside every container.

- **Why:** after a power cut, a network share or USB disk can come back minutes after the server. If its one mount attempt fails, nothing retries it, and the apps run on an empty folder (root folders missing, downloads at zero) while every web page still loads, so a page monitor stays green.
- **What it does:**
  1. **Mounted:** every path in `MOUNT_PATHS` must be a mount point. It reads the kernel's mount table, because looking at a hung share can block forever. A missing one that is listed in `/etc/fstab` gets its systemd mount unit started; otherwise it is reported. With `MOUNT_PATHS` empty, `MEDIA_ROOT` only has to exist.
  2. **Alive:** each path (and `MEDIA_ROOT`) must list its contents within 25 seconds and report its free space. A stale share connection can still list folders but fail the free-space query, and the apps then skip their free-space checks before importing. The checks run in a child process that is abandoned on a timeout. Optional canary: `MOUNT_CANARY_FILE` must have changed within `MOUNT_CANARY_MAX_AGE_S` seconds (default 900). Let a job on the storage side touch it every few minutes; a stale canary means the storage stopped taking writes, or that job died. mount_guard only alerts on it.
  3. **Visible inside:** a share mounted after a container started stays invisible inside that container until it restarts. For each running container that mounts `MEDIA_ROOT` at `/data` (radarr, sonarr, bazarr, moviebot, plex, qbittorrent), it compares the mount behind `/data` with the server's. A container with a stale view is restarted, one at a time with a pause, qBittorrent last, each at most once per 30 minutes and 3 times a day. After a qBittorrent restart the forwarded port is pushed again.
- **Remounting needs sudo.** When the guards run as your normal user, starting a mount unit needs `sudo -n systemctl start <unit>` to be allowed without a password. Find the unit name and the path of `systemctl`:

  ```bash
  systemd-escape -p --suffix=mount /srv/media
  command -v systemctl
  ```

  The first command prints, for example, `srv-media.mount`. Then add one line per mount point with `sudo visudo -f /etc/sudoers.d/media-stack`, using your user name and the paths printed above:

  ```
  youruser ALL=(root) NOPASSWD: /usr/bin/systemctl start srv-media.mount
  ```

  Without this line, a missing mount is still reported; it is just not remounted.
- **When:** every 2 minutes.
- **Never:** unmounts anything, touches a media file, or restarts a container while the storage itself is unhealthy (a restart cannot fix that and can hang on a dead mount). A run lock keeps runs from piling up behind a hung share.
- **Files:** `.mount_guard_state.json`.
- **Flags:** `--dry-run` (mounts nothing, restarts nothing, sends nothing).
- **Alerts:** "Media storage: repaired automatically", "Media storage: PROBLEM" and "Media storage: resolved". Repairs and recoveries are always reported; a problem once.
- **Exit:** 0 only when every path is mounted, alive and correctly visible everywhere; 1 anything else; 2 the job failed.

## arr_health_guard

Turns the apps' own health reports into alarms.

- **Why:** an uptime monitor that only loads each web page stays green while Radarr's own health page says "root folder missing" or "download client unavailable" and downloads sit at zero for hours. The apps know when the pipeline is broken; this job listens to them.
- **What counts as a problem** (exit 1 and one message per new problem): any health item of type "error" in Radarr, Sonarr or Prowlarr; a health item from a source that breaks the pipeline even when the app calls it a warning (root folders missing, download client unreachable, remote path mapping broken, no indexer available for RSS or search, import mechanism broken); a root folder with less than 50 GB free, or one whose free space cannot be read; an app that does not answer; qBittorrent not answering, or 5 or more torrents losing their files at once (a share or disk has gone away).
- **Alert only** (exit stays 0): a queue item stuck at the import step for more than 6 hours, and a torrent in qBittorrent's error state.
- **Ignored:** update notices, and Prowlarr warnings that some indexers are failing (one indexer being down is normal; "no indexer available" shows up in Radarr and Sonarr instead). Bazarr has no comparable health API and is not checked.
- **When:** every 10 minutes.
- **Never:** changes anything in any app. A check that could not run this time keeps its last known state: a failed call never reads as "resolved".
- **Files:** `.arr_health_state.json`.
- **Flags:** `--dry-run`.
- **Alerts:** "Download pipeline: PROBLEM", "Download pipeline: needs a look", and "Download pipeline: resolved" when a problem clears.
- **Exit:** 0 healthy (alert-only items allowed); 1 a pipeline problem; 2 the job failed.

## size_cap_watch

Asks on Telegram before downloading an episode that only Sonarr's size cap blocks. This is one of the two helpers for [the per-minute cap trap](#the-per-minute-cap-trap).

- **Why:** a longer than usual episode (a premiere, a finale) can have every good copy rejected as too large while the rest of the season downloads fine, and Sonarr says nothing.
- **What it does:** on every run it first reads your answers to earlier questions. Yes: it searches that episode again and grabs the copy it offered (a grab you request is not subject to the cap). No: it never asks about that episode again. At most every 2 hours it then looks at up to 3 recently aired episodes (aired between 4 hours and 14 days ago, monitored, missing, not downloading) and runs Sonarr's interactive search. It flags an episode only when the best copy Sonarr turned down was turned down for size alone, is not bigger than `EPISODE_CAP_GB`, has a custom-format score of zero or more, and beats anything Sonarr would accept. Findings become one question per show and season, through the bot's [Yes/No questions](BOT.md#yesno-questions-from-the-host-scripts).
- **When:** every 10 minutes (the searches themselves at most every 2 hours).
- **Never:** downloads anything without your Yes; offers season packs; offers copies in a language other than `PREFERRED_LANGUAGE` or the show's original language; searches the same episode more than once a day. A question nobody answers is dropped after 7 days.
- **Files:** `.size_cap_state.json` and its own lock, `.size_cap_watch.lock`.
- **Flags:** `--dry-run` (searches and prints; asks and grabs nothing), `--probe <episode id>` (classifies one episode and prints the verdict; ignores the queue, cooldown and state).
- **Alerts:** the questions themselves, and a short result after a Yes.
- **Exit:** 0 ran; 2 Sonarr did not answer.

## naming_guard

A weekly, read-only check of the library's file names.

- **Why:** a file whose name the apps cannot read is how content you already have gets downloaded again: Sonarr or Radarr think it is missing and fetch it, or the media server never shows it. Typical causes: a streaming service's own episode numbering, episodes named "Part 1" or "Season 1 Episode 1", a file with a hash-like name, a movie folder without a year, a show matched to the wrong series in Plex.
- **What it flags:** files in a monitored series folder that Sonarr does not know (no episode number, a number Sonarr's episode list does not have, or an episode Sonarr thinks is missing); monitored movies that Radarr thinks are missing while their folder holds a video; TV files without an `SxxEyy` number; hash-like or random file names; movie files outside a movie folder; movie folders without a year. With `PLEX_URL` and `PLEX_TOKEN` set, also files Plex does not list (older than a day, so Plex had time to scan them) and series folders Plex matched to another show.
- **When:** Sundays at 10:15.
- **Never:** renames, moves or deletes anything.
- **Files:** none of its own; it only reports problems that are new since the last run.
- **Flags:** `--dry-run`, `--seed` (records today's problems as known and sends nothing; run it once after installing on an existing library).
- **Alerts:** "Weekly naming check: new problems", grouped by kind.
- **Exit:** 0 ran.

## WARNING: the space-saver

`shrink_oversized` and `resolve_shrink_imports` replace TV episode files that are bigger than their size cap with smaller releases. **They download again episodes you already own, only to save disk space.** They are off by default, and most people should leave them off: every swap spends indexer queries and bandwidth and replaces a file that was working fine. Turn them on only when disk space matters more to you than that.

**How they work.** `shrink_oversized` finds episode files bigger than the series runtime times `TV_SIZE_CAP_MB_PER_MIN` and grabs a smaller 1080p release for each, biggest savings first. Its safety rule is to never delete a file before its replacement is in hand: the grab goes through Sonarr's manual grab, and the old file is only replaced when the new one imports, so a failed download costs nothing. A replacement must be the largest release under the cap and at most 85 % of the current size. It must have at least 8 MB per minute, must not be multi-language, in another language, scored below zero or blocklisted, and must come from a swarm with enough seeders. The script looks up at most 50 episodes per run and stands down while the download queue is busy. An episode with nothing suitable is looked at again only after 30 days. `resolve_shrink_imports` then lands the replacements that Sonarr parks as "Not an upgrade". A downloaded file that is meaningfully smaller than the library's is imported. One that is not smaller is removed with a blocklist entry. When either size is unknown, it does nothing and says so.

**Enable them:**

1. Set `SHRINK_ENABLED=true` in `.env`.
2. Run `python3 configure.py --only cron`. The two lines in `scripts/cron.template` that start with `#SHRINK` are then installed: `shrink_oversized` daily at 06:00 and `resolve_shrink_imports` every hour at 25 past.

**Pause them:** create `state/.shrink_paused` (`touch state/.shrink_paused`). No new swaps start; swaps in flight still finish. Delete the file to resume.

**Keep some episodes as they are:** list them in `state/.shrink_keep.json`, as `{"<Sonarr episode id>": {"title": "...", "why": "..."}}`. They are never shrunk. If that file exists but is not a valid JSON object, both scripts refuse to run.

**Turn them off again:** set `SHRINK_ENABLED=false` and run `python3 configure.py --only cron`. With the setting off, both scripts do nothing even when started by hand.

**Their files:** `.shrink_pending.json` (episodes with a replacement in flight; `regrab_guard` leaves exactly these alone; an entry leaves when the swap landed, or after 2 days, when its stalled grab is cancelled with a blocklist entry), `.shrink_keep.json`, `.shrink_paused`, `.shrink_state.json` (the 30-day cooldowns) and `.shrink_moved.json` (torrents `resolve_shrink_imports` moved, so only those are tidied up later).

**Flags:** `--dry-run` on both. A dry run of `shrink_oversized` looks up only a few candidates, so it does not spend the day's indexer queries.

**Exit:** `shrink_oversized`: 0 normal (also when disabled), 1 Sonarr or the keep-list could not be read, 2 the script failed. `resolve_shrink_imports`: 0 normal (also when disabled), 1 a Sonarr call failed or the keep-list could not be read, 2 the script failed.

## Helpers that are not cron jobs

### The on-grab guard

`scripts/arr/ongrab_guard.sh` runs inside the Radarr and Sonarr containers each time they grab something (Settings > Connect > "On-grab guard", set up by `configure.py`). When the grabbed movie, or every episode in a grabbed pack, is already on disk, it cancels the download and blocklists the release at once, before a second copy downloads. A movie counts as owned when one video file name holds the release year and every significant word of the title. A title without a significant word is never judged, a grab that upgrades a file below the profile cutoff is allowed, and a pack with even one missing episode goes through. When it is unsure it stays silent; `regrab_guard` is the backstop. `ONGRAB_DRYRUN=true` in `.env` (applied with `python3 configure.py --only radarr`) makes it log only. Its log and cache are in `config/radarr/ongrab/` and `config/sonarr/ongrab/`, and it reads its folder names from `scripts/arr/arr.env`, which `configure.py` writes.

### downloads-off.sh and downloads-on.sh

Optional switches, for example to keep the disks and the connection free for streaming in the daytime.

- `scripts/downloads-off.sh` stops only qBittorrent, giving it 30 seconds to save its state. The VPN keeps running. The guards treat a stopped qBittorrent as "downloads switched off", not as a fault.
- `scripts/downloads-on.sh` starts Gluetun (waiting up to 3 minutes for it to be healthy) and qBittorrent, and resumes every torrent. It sets the "night" limits: 40 MB/s down, 2 MB/s up, and 8 active torrents, of which 6 download and 3 upload (edit the numbers at the top of the script). Last, it asks Radarr and Sonarr to search for everything they are missing. On a big library, that last step is a lot of indexer queries at once.

They are not in the default schedule. To use them, add your own lines to your crontab, outside the media-stack block, for example:

```
0 1 * * * /bin/bash "/path/to/media-stack/scripts/run_guard.sh" downloads-on
0 9 * * * /bin/bash "/path/to/media-stack/scripts/run_guard.sh" downloads-off
```

Both exit 0 when done, 1 when a step failed, 2 when Docker is not usable.

### setport.sh

`scripts/gluetun/setport.sh` runs inside the Gluetun container. Gluetun calls it each time the forwarded port changes, and `vpn_watchdog` and `mount_guard` call it for repairs. It sets qBittorrent's listening port and retries until qBittorrent reports the new port. It talks to qBittorrent on localhost inside the shared network namespace, where `configure.py` lets local requests in without a password, so no password is stored for it.

### tg.py and settings.py

- `python3 scripts/tg.py send "text"` sends a Telegram message the way the guards do. `python3 scripts/tg.py ask "question" 120` asks a Yes/No question through the bot and prints the answer.
- `python3 scripts/settings.py` prints the settings the guards resolve: the folders, each app's address and whether its API key was found, how Docker is called, and whether alerts have a destination.

## All jobs at a glance

Schedules are from `scripts/cron.template`, in the server's local time. The push variable is the `.env` setting for an Uptime Kuma push monitor.

| Job | Schedule | Exit 0 | Exit 1 | Exit 2 | Push variable |
|---|---|---|---|---|---|
| `regrab_guard` | `*/10 * * * *` | ran | | neither Radarr nor Sonarr answered | `KUMA_PUSH_REGRAB_GUARD` |
| `link_orphans` | `25 */4 * * *` | ran | hardlinks impossible (different filesystems) | Radarr did not answer | `KUMA_PUSH_LINK_ORPHANS` |
| `vpn_watchdog` | `*/5 * * * *` | healthy or recovering | still broken, or the repair failed | job failed | `KUMA_PUSH_VPN_WATCHDOG` |
| `verify_imports` | `7,37 * * * *` | new imports play | a new import does not play | no app answered, or no ffprobe is available | `KUMA_PUSH_VERIFY_IMPORTS` |
| `missing_sweep` | `15 5 * * *` | ran (also standing down) | no indexer enabled | Sonarr or Radarr did not answer | `KUMA_PUSH_MISSING_SWEEP` |
| `library_guard` | `40 9 * * *` | healthy | serious problem | | `KUMA_PUSH_LIBRARY_GUARD` |
| `config_backup` | `30 3 * * *` | all backups fresh and verified | a backup is missing or stale | job failed | `KUMA_PUSH_CONFIG_BACKUP` |
| `integrity_scan` | `15 7 * * *` | normal | | job failed | `KUMA_PUSH_INTEGRITY_SCAN` |
| `subtitle_sweep` | `10 8 * * *` (`subtitles` profile) | ran | no Bazarr API key | | `KUMA_PUSH_SUBTITLE_SWEEP` |
| `update_check` | `0 9 * * 1` | checked | | Docker not usable, or job failed | `KUMA_PUSH_UPDATE_CHECK` |
| `restore_drill` | `0 11 * * 0` | drill passed | drill failed | job failed | `KUMA_PUSH_RESTORE_DRILL` |
| `dead_torrent_reaper` | `*/30 * * * *` | normal | qBittorrent did not answer | job failed | `KUMA_PUSH_DEAD_TORRENT_REAPER` |
| `malware_guard` | `*/10 * * * *` | normal | qBittorrent did not answer, or an action failed | job failed, or folders overlap | `KUMA_PUSH_MALWARE_GUARD` |
| `mount_guard` | `*/2 * * * *` | all mounted, alive and visible | anything else | job failed | `KUMA_PUSH_MOUNT_GUARD` |
| `arr_health_guard` | `*/10 * * * *` | healthy | pipeline problem | job failed | `KUMA_PUSH_ARR_HEALTH_GUARD` |
| `size_cap_watch` | `*/10 * * * *` | ran | | Sonarr did not answer | `KUMA_PUSH_SIZE_CAP_WATCH` |
| `naming_guard` | `15 10 * * 0` | ran | | | `KUMA_PUSH_NAMING_GUARD` |
| `shrink_oversized` | `0 6 * * *`, only with `SHRINK_ENABLED=true` | normal or disabled | Sonarr or keep-list unreadable | job failed | none in `.env.example`; add one named like the others |
| `resolve_shrink_imports` | `25 * * * *`, only with `SHRINK_ENABLED=true` | normal or disabled | a Sonarr call failed, or keep-list unreadable | job failed | none in `.env.example`; add one named like the others |

An empty cell means the job does not use that exit code. A job that crashes with a Python error also exits non-zero, which turns its push monitor red.
