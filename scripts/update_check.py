#!/usr/bin/env python3
"""update_check: say which of the stack's containers have newer images. NEVER apply them.

Why notify-only: an update can break a working stack (a changed default, a renamed setting, a
proxy that stops matching), and an unattended update applied at night is found broken in the
morning. Knowing what is available and choosing when to update is the right trade for a
server one person maintains.

Why not `docker pull`: pulling stages the new image locally, and then ANY later
`docker compose up -d` (including one a watchdog runs to repair something else) silently moves
containers onto versions nobody chose. So this compares the registry's manifest digest with
the digest of the image each container runs, and downloads nothing.

The registry digest comes from `docker buildx imagetools inspect`, which reports the digest of
the manifest INDEX, the same thing a local RepoDigest holds. Do not take element 0 of
`docker manifest inspect --verbose` instead: for a multi-arch image that is one platform's
digest, which can never equal the index digest, so every image would look out of date. When
buildx is missing, the fallback compares like with like: the per-platform digests listed
under the tag with those listed under the digest the container runs.

Containers built locally (moviebot) have no registry digest and are skipped, as are images
pinned by digest. Only containers of this compose project are checked.

Runs weekly from cron. It speaks only when the set of pending updates gains a new member, so
a container deliberately left on its version does not nag every week.
Flags: --dry-run  check and print; send nothing, keep the state file.
Exit:  0 checked (updates or not), 2 Docker unusable or the guard itself failed.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import subprocess
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import settings  # noqa: E402

JOB = "update_check"
TAG = "[update_check]"
STATE_NAME = ".update_check.json"

GAP_S = 1                  # politeness pause between registry queries
REGISTRY_TIMEOUT = 180
KNOWN_CONTAINERS = ("gluetun", "qbittorrent", "radarr", "sonarr", "prowlarr", "flaresolverr",
                    "jellyseerr", "moviebot", "plex", "uptime-kuma", "tautulli", "bazarr")


def sh(*args: str, timeout: int = 120):
    """docker <args> -> (returncode, stdout); returncode None when docker could not run."""
    env = dict(os.environ, DOCKER_CLI_EXPERIMENTAL="enabled")   # `docker manifest` on older CLIs
    try:
        r = subprocess.run(settings.docker_cmd() + list(args), capture_output=True, text=True,
                           timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        return None, str(e)
    return r.returncode, r.stdout


def stack_containers() -> list:
    project = settings.get("COMPOSE_PROJECT_NAME", "media-stack")
    rc, out = sh("ps", "--filter", "label=com.docker.compose.project=%s" % project, "--format", "{{.Names}}")
    if rc is None or rc != 0:
        raise RuntimeError("docker is not usable here (%s)" % (out or "no output").strip()[:120])
    names = out.split()
    if not names:                                     # project renamed: fall back to the known names
        rc, out = sh("ps", "--format", "{{.Names}}")
        names = [n for n in (out or "").split() if n in KNOWN_CONTAINERS]
    return sorted(names)


def split_ref(ref: str):
    """(repository, tag) of an image reference; tag defaults to latest."""
    last = ref.rsplit("/", 1)[-1]
    if ":" in last:
        repo, tag = ref.rsplit(":", 1)
        return repo, tag
    return ref, "latest"


def _norm_repo(repo: str) -> str:
    if repo.startswith("docker.io/"):
        repo = repo[len("docker.io/"):]
    if repo.startswith("library/"):
        repo = repo[len("library/"):]
    return repo


def local_digest(image_id: str, repo: str):
    """The registry digest of the image a container RUNS (not of whatever the tag points to now)."""
    rc, out = sh("image", "inspect", image_id, "-f", "{{json .RepoDigests}}")
    if rc != 0:
        return None
    try:
        digests = json.loads(out or "null") or []
    except ValueError:
        return None
    pick = [d for d in digests if "@sha256:" in d and _norm_repo(d.split("@", 1)[0]) == _norm_repo(repo)]
    pick = pick or [d for d in digests if "@sha256:" in d]
    return pick[0].split("@", 1)[1] if pick else None


def buildx_digest(ref: str):
    rc, out = sh("buildx", "imagetools", "inspect", ref, timeout=REGISTRY_TIMEOUT)
    if rc != 0:
        return None
    for line in (out or "").splitlines():
        if line.strip().lower().startswith("digest:"):
            return line.split(":", 1)[1].strip()
    return None


def _manifest_digests(ref: str):
    """Set of manifest digests `docker manifest inspect --verbose` lists for a reference."""
    rc, out = sh("manifest", "inspect", "--verbose", ref, timeout=REGISTRY_TIMEOUT)
    if rc != 0:
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return None
    items = data if isinstance(data, list) else [data]
    return {((i or {}).get("Descriptor") or {}).get("digest") for i in items} - {None}


def has_update(ref: str, repo: str, loc: str):
    """True / False, or None when the registry could not be asked."""
    reg = buildx_digest(ref)
    if reg:
        return reg != loc
    remote = _manifest_digests(ref)
    if remote is None:
        return None
    if loc in remote:
        return False                  # single-platform image: the tag still points at what runs
    current = _manifest_digests("%s@%s" % (repo, loc))
    if current is None:
        return None
    return remote != current


def run(dry: bool) -> int:
    updates, checked, skipped, unknown = [], 0, [], []
    for name in stack_containers():
        rc, out = sh("inspect", "-f", "{{.Config.Image}}|{{.Image}}", name)
        if rc != 0 or "|" not in out:
            continue
        ref, image_id = out.strip().split("|", 1)
        if "@sha256:" in ref:
            skipped.append(name)          # pinned by digest on purpose
            continue
        repo, tag = split_ref(ref)
        ref_t = "%s:%s" % (repo, tag)
        loc = local_digest(image_id, repo)
        if not loc:
            skipped.append(name)          # built locally: no registry to compare with
            continue
        checked += 1
        changed = has_update(ref_t, repo, loc)
        if changed is None:
            unknown.append(name)          # registry unreachable: not an update signal
        elif changed:
            updates.append((name, ref_t))
            settings.log(TAG, "update available: %-14s %s" % (name, ref_t))
        time.sleep(GAP_S)

    settings.log(TAG, "checked %d image(s): %d update(s), %d skipped (local or pinned), %d registry unreachable"
                 % (checked, len(updates), len(skipped), len(unknown)))

    st_path = settings.state_path(STATE_NAME)
    prev = set(settings.load_json(st_path, {}).get("pending") or [])
    now_set = {n for n, _r in updates}
    if not dry:
        try:
            settings.save_json(st_path, {"pending": sorted(now_set), "t": int(time.time())})
        except OSError as e:
            settings.log(TAG, "could not save state: %s" % e)
    new = now_set - prev
    if not new:
        settings.log(TAG, "nothing new since the last check")
        return 0
    body = ("Container updates available\n\n%d of the stack's containers have newer images:\n\n"
            % len(updates))
    body += "\n".join("• %s%s" % (html.escape(n, quote=False), "  (new)" if n in new else "") for n, _r in updates)
    body += ("\n\nNothing has been updated; this is only a heads-up. Updates are applied by hand on purpose, "
             "because an unattended update can break the stack at a bad moment. To update, run "
             "`docker compose pull` and then `docker compose up -d` in the stack folder.")
    if dry:
        settings.log(TAG, "[dry-run] would send: " + body.replace("\n", " | "))
    else:
        settings.notify(body)
    return 0


def main(argv) -> int:
    p = argparse.ArgumentParser(description="Report newer images for the stack's containers; never pull.")
    p.add_argument("--dry-run", "--dry", dest="dry_run", action="store_true",
                   help="check and print only; send nothing, keep the state file")
    args = p.parse_args(argv)
    dry = args.dry_run or settings.get_bool("DRY_RUN")
    if not dry:
        settings.heartbeat(JOB)
    try:
        return run(dry)
    except Exception as e:
        settings.log(TAG, "cannot check: %s" % e)
        if not isinstance(e, RuntimeError):
            traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
