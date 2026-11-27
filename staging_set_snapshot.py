"""B2 tag hram-staging-set: absolute Docker volume _data paths contain
infrastructure/<selected>, runs/<selected>, jobs, scenarios and data.
This snapshot is for staging, not disaster recovery.
unsafe_path indicates a missing, symlinked, or escaping host path.
"""

import json
import os
import stat
import re
import subprocess
import tempfile

TAG = "hram-staging-set"
KEEP_LAST = "3"
MAX_BYTES = 10_000_000_000
TIMEOUT_SECONDS = 120
BACKUP_TIMEOUT_SECONDS = 3600
FORGET_TIMEOUT_SECONDS = 1800
UNSAFE_PATH = "unsafe_path"
SELECTOR_FAILURE = "selector_failure"
EMPTY_SELECTION = "empty_selection"
SIZE_EXCEEDED = "size_exceeded"
SNAPSHOT_FAILURE = "snapshot_failure"
SUCCESS = "ok"
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z", re.ASCII)
DESTINATION = "/app/hram_artifacts"
VOLUME = "hram_production_pipeline_artifacts_volume"


def command(args):
    return subprocess.run(args, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=True).stdout


def selection(docker):
    ids = command(docker + ["ps", "-q"]).split()
    if not ids:
        raise ValueError("backend unavailable")
    containers = json.loads(command(docker + ["inspect"] + ids))
    matches = []
    for container in containers:
        labels = (container.get("Config") or {}).get("Labels") or {}
        if (labels.get("com.docker.compose.project") == "hram_production"
                and labels.get("com.docker.compose.service") == "backend"
                and labels.get("dbrecv.enable") != "true"
                and not any(k.startswith("backup.pg.") for k in labels)):
            matches.append(container)
    if len(matches) != 1:
        raise ValueError("backend ambiguous or unavailable")
    container = matches[0]
    mounts = [m for m in container.get("Mounts", []) if m.get("Type") == "volume"
              and m.get("Name") == VOLUME and m.get("Destination") == DESTINATION]
    if len(mounts) != 1 or not re.fullmatch(r"/var/lib/docker/volumes/[^/]+/_data", mounts[0].get("Source", "")):
        raise ValueError("volume unavailable")
    relative = []
    for subtree in ("infrastructure", "runs"):
        names = json.loads(command(docker + ["exec", container["Id"], "python", "manage.py",
                                             "staging_artifact_selection", subtree]))
        if not isinstance(names, list) or not all(isinstance(n, str) and NAME.fullmatch(n) for n in names):
            raise ValueError("invalid selection")
        if not names:
            return None
        relative.extend(subtree + "/" + name for name in sorted(set(names)))
    relative.extend(("jobs", "scenarios", "data"))
    return container["Id"], mounts[0]["Source"], relative


def safe_paths(root, relative):
    """Check only the selected paths and their components below the volume root."""
    try:
        real_root = os.path.realpath(root)
        for path in relative:
            current = root
            for component in path.split("/"):
                if component in ("", ".", ".."):
                    return False
                current = os.path.join(current, component)
                if stat.S_ISLNK(os.lstat(current).st_mode):
                    return False
            if os.path.commonpath((real_root, os.path.realpath(current))) != real_root:
                return False
        return True
    except (OSError, ValueError):
        return False


def backup(docker, restic, repository, host):
    try:
        selected = selection(docker)
        if selected is None:
            return EMPTY_SELECTION
        container, root, relative = selected
        # This narrows the TOCTOU window; restic reads the paths later.
        if not safe_paths(root, relative):
            return UNSAFE_PATH
        # du runs as the backend user, which has access to its mounted artifacts;
        # do not follow symlinks, matching restic's default behaviour.
        paths = [DESTINATION + "/" + path for path in relative]
        output = command(docker + ["exec", container, "du", "-sb", "--total", "--"] + paths)
        size = int(output.strip().splitlines()[-1].split()[0])
        if size > MAX_BYTES:
            return SIZE_EXCEEDED
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, IndexError):
        return SELECTOR_FAILURE

    env = os.environ.copy()
    env["RESTIC_REPOSITORY"] = repository
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n") as path_file:
            path_file.write("".join(root + "/" + path + "\n" for path in relative))
            path_file.flush()
            result = subprocess.run([restic, "backup", "--host", host, "--tag", TAG,
                                     "--files-from-raw", path_file.name], env=env, capture_output=True, text=True,
                                    timeout=BACKUP_TIMEOUT_SECONDS)
        if result.returncode:
            return SNAPSHOT_FAILURE
        result = subprocess.run([restic, "forget", "--prune", "--host", host, "--tag", TAG,
                                 "--group-by", "host,tag", "--keep-last", KEEP_LAST],
                                env=env, capture_output=True, text=True, timeout=FORGET_TIMEOUT_SECONDS)
        if result.returncode:
            return SNAPSHOT_FAILURE
    except (OSError, subprocess.SubprocessError):
        return SNAPSHOT_FAILURE
    return SUCCESS + f" bytes={size}"
