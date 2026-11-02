# Adding Usenet

media-stack downloads with qBittorrent out of the box. This page adds a Usenet download client, SABnzbd, next to it. Radarr and Sonarr then use both and pick per release. Nothing here is required, and no provider or indexer is named or included: you choose your own.

## What Usenet adds

- A second source next to torrents. Usenet posts are stored on your provider's servers, so a download comes from those servers, not from other users. There are no seeders, no peers and nothing to seed back.
- Older posts often stay available for years. How far back depends on your provider's retention.
- The connection to your provider is encrypted (SSL/TLS), and you only ever connect to your provider.

What it does not do: a listing on an indexer is not a guarantee. Takedowns can remove parts of a post after it was listed, and a download then fails or needs repair.

## What you need

- **One Usenet provider.** A paid subscription that gives you a server address, a port, a user name, a password and a number of connections. Providers differ in retention and in the network (backbone) behind them. Several brands resell the same backbone, so two providers on one backbone give you the same posts twice: if you ever add a second provider, pick one on a different backbone.
- **One Usenet indexer** of your choice. It finds the posts. Most need an account and give you an API key.
- Some free Usenet search sites let you check whether posts for a title exist before you pay. They work poorly as automatic indexers, so use them for a quick check only. Downloading always needs a provider.

## 1. Add the SABnzbd container

Create the folders SABnzbd works in, under your media folder (use your `MEDIA_ROOT` instead of `/srv/media`), as the user the apps run as:

```bash
mkdir -p /srv/media/usenet/incomplete /srv/media/usenet/complete
```

They sit inside `MEDIA_ROOT`, so SABnzbd sees them through the same `/data` mount as Radarr and Sonarr, and an import is a quick move within one filesystem. They also sit outside the torrent downloads folder on purpose: `malware_guard` and `link_orphans` watch that folder, and a Usenet download in progress is full of archive parts that `malware_guard` would report one by one.

Then create `docker-compose.override.yml` next to `docker-compose.yml` (or add the `sabnzbd` service to the one you have). Docker Compose and the installer read it automatically.

```yaml
services:
  sabnzbd:
    image: lscr.io/linuxserver/sabnzbd:latest
    container_name: sabnzbd
    restart: unless-stopped
    security_opt:
      - no-new-privileges:true
    networks:
      - media
    ports:
      - "8085:8080"           # qBittorrent already uses 8080 on the server
    environment:
      PUID: ${PUID:-1000}
      PGID: ${PGID:-1000}
      UMASK: ${UMASK:-002}
      TZ: ${TZ}
    volumes:
      - ./config/sabnzbd:/config
      - ${MEDIA_ROOT:?run ./install.sh first}:/data
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
```

Start it:

```bash
docker compose up -d sabnzbd
```

Success: `docker compose ps sabnzbd` shows it running, and `http://<server>:8085` opens SABnzbd's setup wizard.

SABnzbd does not use the VPN, and it should not: the connection to your provider is already encrypted, and routing it through Gluetun adds one more thing that can break. qBittorrent stays behind Gluetun exactly as before.

## 2. Set up SABnzbd

In SABnzbd's web page:

1. **Server:** in the wizard, or Config > Servers, add your provider's server with its address, user name, password and number of connections. Turn **SSL on** and use the SSL port your provider lists (563 is the standard one). Leave certificate verification on Strict.
2. **Folders:** in Config > Folders, set the Temporary Download Folder to `/data/usenet/incomplete` and the Completed Download Folder to `/data/usenet/complete`.
3. **Categories:** in Config > Categories, make sure there are categories named `movies` and `tv`. A relative folder is placed inside the completed folder, so `movies` ends up in `/data/usenet/complete/movies`.
4. **Sorting:** leave sorting off (Config > Sorting). Radarr and Sonarr rename and file the downloads themselves.
5. **Programs:** in Config > Switches, list program extensions (for example `exe, scr, bat, cmd, com, lnk, msi, ps1, vbs, js, jar, pif`) as unwanted extensions and set the action when one is detected to Abort. `malware_guard` does not watch the Usenet folders, so this is the protection there.
6. **Host name:** Radarr and Sonarr reach SABnzbd by its container name, `sabnzbd`. If they report "hostname verification failed", add `sabnzbd` to `host_whitelist` in Config > Special.
7. Copy the **API key** from Config > General. Radarr and Sonarr need it.

## 3. Add the indexer in Prowlarr

In Prowlarr, Indexers > Add Indexer: pick your Usenet indexer, or the generic Newznab entry when it is not listed, and enter its address and API key. Test, then save. Prowlarr copies it to Radarr and Sonarr by itself, because `configure.py` set both apps to full sync. Prowlarr does not need a download client for this.

## 4. Add SABnzbd to Radarr and Sonarr

In Radarr and in Sonarr, Settings > Download Clients > add **SABnzbd**:

| Field | Value |
|---|---|
| Host | `sabnzbd` |
| Port | `8080` (the port inside the stack's network, not 8085) |
| API Key | the key from SABnzbd |
| Category | `movies` in Radarr, `tv` in Sonarr |
| Use SSL | off (the traffic stays inside the server) |

Test, then save. `configure.py` only manages the client named "qBittorrent" and leaves this one alone.

On the same page, keep **Completed Download Handling** on (it is on by default): the apps then import finished downloads by themselves. Because SABnzbd and the apps see the same `/data` paths, no remote path mapping is needed. Keep the option that downloads another release after a failed one switched on as well, so a broken post is replaced.

Radarr and Sonarr now choose between Usenet and torrents per release. The delay profile (Settings > Profiles > Delay Profiles) decides which protocol they prefer when both have a suitable release.

## 5. Check it

Pick a missing title in Radarr and use Interactive Search. Results from the Usenet indexer appear next to the torrent results. Grab one and watch it appear in SABnzbd's queue, then in Radarr's Activity, then in the library.

## What the guards and the bot cover

Covered for Usenet downloads too, because they work through Radarr's and Sonarr's own queues and libraries:

- the on-grab guard and `regrab_guard` (downloads of things you already own);
- `missing_sweep`, `library_guard`, `verify_imports`, `integrity_scan`, `naming_guard` and `arr_health_guard` (which also sees the apps' download client health checks);
- the bot's ready messages, and its fake-download cleanup when the app's status says "unsupported extension";
- `update_check`, which also reports newer SABnzbd images, because the override file belongs to the same compose project;
- `config_backup`, which keeps `docker-compose.override.yml` together with `.env`.

Not covered:

- `vpn_watchdog`, `dead_torrent_reaper` and `malware_guard` only look at qBittorrent and the torrent downloads folder.
- The bot's grab check with its Cancel button, its dead-torrent cleanup and `/downloading` only cover qBittorrent.
- `mount_guard` does not restart SABnzbd after a share is mounted late. Restart it yourself: `docker restart sabnzbd`.
- SABnzbd's own settings in `config/sabnzbd` are not part of the nightly backup. Copy that folder yourself.

## Remove it again

Delete the `sabnzbd` client in Radarr and Sonarr and the indexer in Prowlarr. Remove the `sabnzbd` service from `docker-compose.override.yml` (or the whole file, if it held nothing else), then remove the container:

```bash
docker rm -f sabnzbd
```

Its settings stay in `config/sabnzbd`, and the folders under `usenet/` stay in your media folder until you delete them yourself.
