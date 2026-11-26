"""Fail closed before opening remote restic repositories."""

import functools
import re
import subprocess

MIN_RESTIC = (0, 16)


class ResticTooOld(RuntimeError):
    """The restic version cannot safely open a B2 repository."""


@functools.lru_cache(maxsize=None)
def require_b2_version(binary):
    try:
        result = subprocess.run([binary, "version"], capture_output=True, text=True, check=False)
    except (OSError, UnicodeError) as exc:
        raise ResticTooOld("restic_version_floor: cannot determine restic version, refusing to open a B2 repository") from exc
    match = re.fullmatch(r"\s*restic\s+(\d+)\.(\d+)\.(\d+)(?:\s+[^\r\n]*)?\s*", result.stdout) if result.returncode == 0 else None
    if not match:
        raise ResticTooOld("restic_version_floor: cannot determine restic version, refusing to open a B2 repository")
    version = tuple(map(int, match.groups()))
    if version[:2] < MIN_RESTIC:
        found = ".".join(match.groups())
        raise ResticTooOld(f"restic_version_floor: restic {found} is below 0.16, refusing to open a B2 repository")


def is_remote_repository(repository):
    return repository.lower().startswith(("b2:", "s3:"))
