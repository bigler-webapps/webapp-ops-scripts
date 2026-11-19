"""OPS-BAK-1: declared selection of production user-data Docker volumes for the restic backup.

Standard library only (runs on the managed hosts as `deploy`). The selection never touches the
volume files: `/var/lib/docker/volumes` is unreadable for `deploy`, and only the capability on the
restic binary reads it. Names, mountpoints and sizes come from the Docker daemon.
"""

import fnmatch
import json
import re
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

CONFIG_FILE = Path(__file__).with_name("backup_volumes.json")

_SIZE_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([A-Za-z]*)\s*$")
_UNITS = {
    "": 1, "b": 1,
    "kb": 1000, "mb": 1000 ** 2, "gb": 1000 ** 3, "tb": 1000 ** 4,
    "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3, "tib": 1024 ** 4,
}


def load_config(path: Path = CONFIG_FILE) -> dict:
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    for key in ("include", "exclude"):
        if not isinstance(cfg.get(key), list):
            raise ValueError(f"{path}: '{key}' must be a list")
    if not isinstance(cfg.get("max_total_bytes_per_host"), int):
        raise ValueError(f"{path}: 'max_total_bytes_per_host' must be an integer")
    return cfg


def is_declared(name: str, cfg: dict) -> bool:
    if any(fnmatch.fnmatchcase(name, pat) for pat in cfg["exclude"]):
        return False
    return any(fnmatch.fnmatchcase(name, pat) for pat in cfg["include"])


def select_volumes(mounted_by_running: Iterable[str], cfg: dict) -> List[str]:
    """Declared production user-data volumes that a running container mounts, sorted.

    Taken from the containers' mounts, not from `docker volume ls`: a declared volume that a
    container mounts but Docker cannot resolve fails later, named, instead of being dropped.
    """
    return sorted(n for n in set(mounted_by_running) if is_declared(n, cfg))


def parse_size(text) -> int:
    """Docker's human size ('11.63GB', '356.7MB', '36.5kB', '0B') in bytes (decimal units)."""
    if isinstance(text, (int, float)):
        return int(text)
    m = _SIZE_RE.match(str(text))
    if not m or m.group(2).lower() not in _UNITS:
        raise ValueError(f"unparsable volume size: {text!r}")
    return int(float(m.group(1)) * _UNITS[m.group(2).lower()])


def running_container_volumes(prefix: List[str]) -> Set[str]:
    ids = subprocess.run(prefix + ["ps", "-q"], capture_output=True, text=True, check=True).stdout.split()
    if not ids:
        return set()
    out = subprocess.run(prefix + ["inspect"] + ids, capture_output=True, text=True, check=True).stdout
    names: Set[str] = set()
    for container in json.loads(out):
        for mount in container.get("Mounts") or []:
            if mount.get("Type") == "volume" and mount.get("Name"):
                names.add(mount["Name"])
    return names


def volume_sizes(prefix: List[str]) -> Dict[str, int]:
    """Volume sizes from `docker system df -v` (daemon-side, needs no file access)."""
    out = subprocess.run(
        prefix + ["system", "df", "-v", "--format", "json"], capture_output=True, text=True, check=True
    ).stdout
    data = json.loads(out)
    return {v["Name"]: parse_size(v["Size"]) for v in data.get("Volumes") or []}


def build_report(selected: List[str], mountpoints: Dict[str, str], sizes: Dict[str, int]) -> List[dict]:
    """One entry per selected volume. A missing size or mountpoint is an error, never a silent 0."""
    report = []
    for name in selected:
        if not mountpoints.get(name):
            raise ValueError(f"selected volume has no mountpoint: {name}")
        if name not in sizes:
            raise ValueError(f"selected volume has no size from docker: {name}")
        report.append({"name": name, "path": mountpoints[name], "bytes": sizes[name]})
    return report


def total_bytes(report: List[dict]) -> int:
    return sum(v["bytes"] for v in report)


def format_bytes(n: int) -> str:
    return f"{n / 1e9:.2f} GB" if n >= 1e8 else f"{n / 1e6:.1f} MB"


def volumes_named_in(text: str, report: List[dict]) -> List[str]:
    """Names of the report's volumes whose path appears in a tool's error output."""
    return [v["name"] for v in report if v["path"] in (text or "")]


def load_report(path: Path) -> Optional[List[dict]]:
    p = Path(path)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))
