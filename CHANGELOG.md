# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-10-31

### Added

- `install.sh`: a one-command installer. It checks the machine, asks its questions (or reads an answers file with `--answers` and `--yes`), writes `.env` with mode 600, creates the folders, downloads only missing images, starts the containers, reads the apps' API keys, starts the bot, connects the apps and installs the guard jobs. Safe to run again. Options for optional services (`--profiles`), test installs without a VPN (`--no-vpn`), installing Docker (`--install-docker`) and skipping cron (`--skip-cron`).
- `uninstall.sh`, with `--remove-images` and `--purge`. It never touches the media folder.
- `docker-compose.yml`: qBittorrent inside Gluetun's network namespace (VPN kill switch), Radarr, Sonarr, Prowlarr, FlareSolverr, Jellyseerr and the bot, plus the optional profiles `plex`, `monitoring` (Uptime Kuma, Tautulli) and `subtitles` (Bazarr). One media mount (`/data`) so imports can use hardlinks.
- `compose/test-no-vpn.override.yml` for throwaway test installs.
- `configure.py`: sets up qBittorrent, Radarr, Sonarr, Prowlarr, Jellyseerr and the cron jobs through the apps' own APIs. Idempotent, with `--dry-run` and `--only`. It never adds indexers.
- The Telegram bot: search and add titles, a grab check with a Cancel button, force-grab, closest-release questions, ready messages, a daily report, new-release cards, a missing-episode filler with a 720p offer, dead and fake download cleanup, disk alerts, and Yes/No questions for the host scripts. Access by allow-list only; webhooks on a secret path.
- Guard scripts with `scripts/run_guard.sh` and `scripts/cron.template`: `regrab_guard`, `link_orphans`, `vpn_watchdog`, `verify_imports`, `missing_sweep`, `library_guard`, `config_backup`, `integrity_scan`, `subtitle_sweep`, `update_check`, `restore_drill`, `dead_torrent_reaper`, `malware_guard`, `mount_guard`, `arr_health_guard`, `size_cap_watch` and `naming_guard`, plus the optional space-saver `shrink_oversized` and `resolve_shrink_imports` (off by default).
- The on-grab guard for Radarr and Sonarr, the Gluetun port script, and `downloads-on.sh` and `downloads-off.sh`.
- `tools/killswitch_test.sh`, `tools/block_scan.py` and `tools/privacy_scan.sh`, a test suite and CI.
- Documentation: README, setup, bot, automations, troubleshooting, Usenet, architecture, security and contributing guides.

[Unreleased]: https://github.com/signalgarage/media-stack/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/signalgarage/media-stack/releases/tag/v0.1.0
