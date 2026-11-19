#!/usr/bin/env python3
import argparse
import os
import subprocess
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import volume_selection as vs  # noqa: E402

ALLOW_VOLUME_FAILURE = os.environ.get("ALLOW_VOLUME_FAILURE") == "1"


def docker_cmd_prefix():
    """Choose docker invocation, either direct or via sudo -n."""
    try:
        subprocess.run(
            ["docker", "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        return ["docker"]
    except subprocess.CalledProcessError:
        return ["sudo", "-n", "docker"]


def emit_existing(path: str, seen: set) -> None:
    """Print a path only if it exists, and only once."""
    p = Path(path)
    try:
        exists = p.exists()
    except PermissionError:
        sys.stderr.write(f"Skipping unreadable path: {path}\n")
        return

    if exists:
        s = str(p)
        if s not in seen:
            seen.add(s)
            print(s)
    else:
        sys.stderr.write(f"Skipping missing path: {path}\n")


def inspect_mountpoints(prefix: list, volumes: list) -> dict:
    """Return {volume name: mountpoint} from the Docker daemon (no file access)."""
    mountpoints = {}
    chunk_size = 200
    for i in range(0, len(volumes), chunk_size):
        cmd = prefix + ["volume", "inspect"] + volumes[i:i + chunk_size]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        for vol in json.loads(res.stdout):
            if vol.get("Name") and vol.get("Mountpoint"):
                mountpoints[vol["Name"]] = vol["Mountpoint"]
    return mountpoints


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate restic include-paths list.")
    parser.add_argument(
        "--infra-paths-file",
        default=os.environ.get("INFRA_PATHS_FILE", ""),
        help="File with static infra paths to back up (one per line, # = comment).",
    )
    parser.add_argument(
        "--volumes-report",
        default="",
        help="Write the selected volumes (name, path, bytes) as JSON to this file.",
    )
    args = parser.parse_args()

    seen: set = set()

    # Infra-specific static paths from the calling infra repo
    if args.infra_paths_file:
        infra_file = Path(args.infra_paths_file)
        if infra_file.exists():
            for line in infra_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    emit_existing(line, seen)
        else:
            sys.stderr.write(f"Infra paths file not found: {args.infra_paths_file}\n")

    report = []
    try:
        cfg = vs.load_config()
        prefix = docker_cmd_prefix()
        mounted = vs.running_container_volumes(prefix)
        selected = vs.select_volumes(mounted, cfg)
        report = vs.build_report(selected, inspect_mountpoints(prefix, selected), vs.volume_sizes(prefix))
    except (OSError, subprocess.CalledProcessError, ValueError, KeyError) as e:
        sys.stderr.write(f"Volume selection failed: {getattr(e, 'stderr', None) or e}\n")
        # No override: a failed selection must never become a green run without volume data.
        return 1

    # Volume paths are NOT stat()ed: `deploy` cannot read them; restic's capability does.
    for v in report:
        if v["path"] not in seen:
            seen.add(v["path"])
            print(v["path"])

    if args.volumes_report:
        Path(args.volumes_report).write_text(json.dumps(report), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
