#!/usr/bin/env python3
"""Safely prepare or recover DB-referenced Innoservice media from main-prod."""

from __future__ import annotations

import argparse
import base64
import hashlib
import inspect
import json
import os
import posixpath
import re
import shlex
import stat
import subprocess
import sys
from pathlib import Path
from typing import Iterable

SOURCE_ALIAS = "main-prod"
TARGET_ALIAS = "innoservice-prod"
SOURCE_VOLUME = "innoservice_prod_media_volume"
TARGET_CONTAINER = "innoservice_production-backend-1"
APPS = ("measurements", "inspection", "structures", "protocols", "bills", "people")
JSON_MARKER = "__RECOVER_MEDIA_JSON__:"
SSH_BASE = ("ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "RemoteCommand=none")


class RecoveryError(Exception):
    """A safe, user-facing recovery failure."""


def validate_relative_path(value: str) -> str:
    """Accept only ordinary POSIX media names, never host paths or secrets."""
    if not isinstance(value, str) or not value or "\\" in value or any(ord(c) < 32 for c in value):
        raise RecoveryError("invalid media path")
    if value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise RecoveryError("absolute media path rejected")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise RecoveryError("traversal or empty path component rejected")
    if parts[0] not in ("private", "public"):
        raise RecoveryError("unexpected media prefix")
    if any(part.casefold() in {"secret", "secrets"} or part.casefold().startswith(".env") for part in parts):
        raise RecoveryError("secret path rejected")
    if posixpath.normpath(value) != value:
        raise RecoveryError("non-canonical media path rejected")
    return value


def open_parent(root, relative, *, create=False):
    """Pin each Linux directory with O_NOFOLLOW, including root ancestors."""
    relative = validate_relative_path(relative)
    root = os.fspath(root)
    if not root.startswith("/") or any(p in (".", "..") for p in root.split("/")):
        raise RecoveryError("media root is not an absolute Linux path")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open("/", flags)
    try:
        for component in filter(None, root.split("/")):
            child = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        for component in relative.split("/")[:-1]:
            try:
                child = os.open(component, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    os.close(fd)
                    return None, relative.split("/")[-1]
                try:
                    os.mkdir(component, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd, relative.split("/")[-1]
    except OSError as exc:
        os.close(fd)
        raise RecoveryError("unsafe or inaccessible media directory") from exc


def checked_path(root, relative, *, allow_missing=False):
    """Diagnostic validation only; actual IO uses pinned descriptors."""
    parent, leaf = open_parent(root, relative)
    if parent is None:
        if allow_missing:
            return Path(root) / relative
        raise RecoveryError("media file is missing")
    try:
        try:
            mode = os.stat(leaf, dir_fd=parent, follow_symlinks=False).st_mode
        except FileNotFoundError:
            if allow_missing:
                return Path(root) / relative
            raise RecoveryError("media file is missing")
        if not stat.S_ISREG(mode):
            raise RecoveryError("media file is not a regular file")
        return Path(root) / relative
    finally:
        os.close(parent)


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def select_missing(references, target_root):
    unique = sorted({validate_relative_path(value) for value in references if value})
    missing = []
    for relative in unique:
        parent, leaf = open_parent(target_root, relative)
        if parent is None:
            missing.append(relative)
            continue
        try:
            try:
                mode = os.stat(leaf, dir_fd=parent, follow_symlinks=False).st_mode
            except FileNotFoundError:
                missing.append(relative)
                continue
            if not stat.S_ISREG(mode):
                raise RecoveryError("target media path is not a regular file")
        finally:
            os.close(parent)
    return missing, len(unique) - len(missing)


def read_source_file(root, relative):
    parent, leaf = open_parent(root, relative)
    if parent is None:
        raise RecoveryError("source file is missing")
    try:
        try:
            fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise RecoveryError("source is not a regular file")
                data = stream.read()
        except OSError as exc:
            raise RecoveryError("source file is unreadable") from exc
        if not data:
            raise RecoveryError("source file is empty")
        return data
    finally:
        os.close(parent)


def source_preflight(root, paths):
    result = []
    for relative in paths:
        data = read_source_file(root, relative)
        result.append({"path": relative, "size": len(data), "sha256": sha256_bytes(data)})
    return result


def publish_verified(root, relative, data, expected_size, expected_sha256):
    """Publish a verified regular file atomically without replacing another inode."""
    if len(data) != expected_size or sha256_bytes(data) != expected_sha256:
        raise RecoveryError("transferred file failed size or checksum validation")
    parent, final = open_parent(root, relative, create=True)
    temp = ".recover-media-" + os.urandom(12).hex() + ".part"
    created = False
    try:
        try:
            mode = os.stat(final, dir_fd=parent, follow_symlinks=False).st_mode
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(mode):
                raise RecoveryError("existing target is not a regular file")
            return "skipped"
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=parent)
        created = True
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temp, final, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
        except FileExistsError:
            if not stat.S_ISREG(os.stat(final, dir_fd=parent, follow_symlinks=False).st_mode):
                raise RecoveryError("racing target is not a regular file")
            return "skipped"
        return "copied"
    finally:
        try:
            if created:
                os.unlink(temp, dir_fd=parent)
        finally:
            os.close(parent)


def _b64_code(source: str) -> str:
    encoded = base64.b64encode(source.encode("utf-8")).decode("ascii")
    return "exec(__import__('base64').b64decode('" + encoded + "').decode('utf-8'))"


# Ship exactly the filesystem functions exercised by the local Linux tests.
REMOTE_COMMON_CODE = (
    "from __future__ import annotations\nimport base64, hashlib, json, os, posixpath, re, stat, subprocess, sys\nfrom pathlib import Path\n"
    + "\n\n".join(inspect.getsource(function) for function in (
        RecoveryError, validate_relative_path, open_parent, checked_path, sha256_bytes,
        select_missing, read_source_file, source_preflight, publish_verified,
    )) + "\n"
)

TARGET_QUERY_CODE = REMOTE_COMMON_CODE + r'''
from django.apps import apps
from django.conf import settings
from django.db.models import FileField
refs = set()
for label in ("measurements", "inspection", "structures", "protocols", "bills", "people"):
    for model in apps.get_app_config(label).get_models():
        for field in model._meta.fields:
            if isinstance(field, FileField):
                refs.update(value for value in model.objects.values_list(field.name, flat=True) if value)
root = os.path.abspath(settings.MEDIA_ROOT)
missing, present = select_missing(refs, root)
print("__RECOVER_MEDIA_JSON__:" + json.dumps({"media_root": root, "references": len(refs), "missing": missing}))
'''

SOURCE_MOUNT_CODE = r'''
mount = json.loads(subprocess.run(["docker", "volume", "inspect", sys.argv[1]], check=True, capture_output=True, text=True).stdout)[0]["Mountpoint"]
'''
SOURCE_PREFLIGHT_CODE = REMOTE_COMMON_CODE + SOURCE_MOUNT_CODE + r'''
print(json.dumps(source_preflight(mount, json.load(sys.stdin))))
'''
SOURCE_READ_CODE = REMOTE_COMMON_CODE + SOURCE_MOUNT_CODE + r'''
sys.stdout.buffer.write(read_source_file(mount, sys.argv[2]))
'''
TARGET_PUBLISH_CODE = REMOTE_COMMON_CODE + r'''
meta = json.loads(base64.b64decode(sys.argv[1]).decode("utf-8"))
data = sys.stdin.buffer.read()
print(publish_verified(meta["media_root"], meta["path"], data, meta["size"], meta["sha256"]))
'''


def ssh_command(alias: str, remote: str) -> list[str]:
    return [*SSH_BASE, alias, remote]


def _run_json(cmd: list[str], *, input_bytes: bytes | None = None, marker: bool = False):
    try:
        result = subprocess.run(cmd, input=input_bytes, capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RecoveryError("remote command could not be completed") from exc
    if result.returncode:
        raise RecoveryError("remote preflight failed")
    output = result.stdout.decode("utf-8", errors="replace")
    try:
        if marker:
            lines = [line[len(JSON_MARKER):] for line in output.splitlines() if line.startswith(JSON_MARKER)]
            if len(lines) != 1:
                raise ValueError()
            return json.loads(lines[0])
        return json.loads(output)
    except (ValueError, json.JSONDecodeError) as exc:
        raise RecoveryError("remote response was invalid") from exc


def query_target() -> dict:
    remote = "docker exec " + shlex.quote(TARGET_CONTAINER) + " python manage.py shell -c " + shlex.quote(_b64_code(TARGET_QUERY_CODE))
    return _run_json(ssh_command(TARGET_ALIAS, remote), marker=True)


def preflight_source(paths: list[str]) -> list[dict[str, object]]:
    remote = "sudo -n python3 -c " + shlex.quote(_b64_code(SOURCE_PREFLIGHT_CODE)) + " " + shlex.quote(SOURCE_VOLUME)
    return _run_json(ssh_command(SOURCE_ALIAS, remote), input_bytes=json.dumps(paths).encode("utf-8"))


def read_source(relative: str) -> bytes:
    remote = "sudo -n python3 -c " + shlex.quote(_b64_code(SOURCE_READ_CODE)) + " " + shlex.quote(SOURCE_VOLUME) + " " + shlex.quote(relative)
    try:
        result = subprocess.run(ssh_command(SOURCE_ALIAS, remote), capture_output=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RecoveryError("source transfer failed") from exc
    if result.returncode:
        raise RecoveryError("source transfer failed")
    return result.stdout


def publish_target(item: dict[str, object], media_root: str) -> str:
    command = target_publish_command(item, media_root)
    data = read_source(str(item["path"]))
    if len(data) != item["size"] or sha256_bytes(data) != item["sha256"]:
        raise RecoveryError("source changed after preflight")
    try:
        result = subprocess.run(command, input=data, capture_output=True, timeout=180, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RecoveryError("destination publish failed") from exc
    if result.returncode:
        raise RecoveryError("destination publish failed")
    outcome = result.stdout.decode("utf-8", errors="replace").strip().splitlines()
    if outcome not in (["copied"], ["skipped"]):
        raise RecoveryError("destination response was invalid")
    return outcome[0]


def target_publish_command(item: dict[str, object], media_root: str) -> list[str]:
    """Build a fixed remote helper invocation; metadata travels base64 encoded."""
    meta = dict(item, media_root=media_root)
    encoded = base64.b64encode(json.dumps(meta, separators=(",", ":")).encode("utf-8")).decode("ascii")
    remote_code = _b64_code(TARGET_PUBLISH_CODE)
    remote = "docker exec -i " + shlex.quote(TARGET_CONTAINER) + " python3 -c " + shlex.quote(remote_code) + " " + shlex.quote(encoded)
    return ssh_command(TARGET_ALIAS, remote)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Recover only currently missing, DB-referenced Innoservice media.")
    parser.add_argument("--apply", action="store_true", help="transfer the preflighted missing files; default is read-only")
    args = parser.parse_args(argv)
    copied = skipped = failures = 0
    try:
        target = query_target()
        if not isinstance(target, dict):
            raise RecoveryError("target inventory was invalid")
        if (not isinstance(target.get("media_root"), str)
                or not target["media_root"].startswith("/")
                or type(target.get("references")) is not int
                or not isinstance(target.get("missing"), list)):
            raise RecoveryError("target inventory was invalid")
        media_root = target["media_root"]
        paths = [validate_relative_path(path) for path in target["missing"]]
        if len(paths) != len(set(paths)) or target["references"] < len(paths):
            raise RecoveryError("target inventory counts were invalid")
        print(f"DB references: {target['references']}; currently missing: {len(paths)}; mode: {'APPLY' if args.apply else 'DRY RUN'}")
        if not paths:
            print("Copied: 0; skipped: 0; failures: 0")
            return 0
        manifest = preflight_source(paths)
        if (not isinstance(manifest, list)
                or any(not isinstance(entry, dict) for entry in manifest)
                or [entry.get("path") for entry in manifest] != paths):
            raise RecoveryError("source preflight did not match selected paths")
        for entry in manifest:
            if (type(entry.get("size")) is not int or entry["size"] <= 0
                    or not isinstance(entry.get("sha256"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])):
                raise RecoveryError("source preflight metadata was invalid")
        for item in manifest:
            print(f"  {item['path']} ({item['size']} bytes, sha256 {item['sha256']})")
        if args.apply:
            for item in manifest:
                try:
                    outcome = publish_target(item, media_root)
                    if outcome == "copied":
                        copied += 1
                    else:
                        skipped += 1
                except RecoveryError as exc:
                    failures += 1
                    print(f"FAILED {item['path']}: {exc}", file=sys.stderr)
                    break
        print(f"Copied: {copied}; skipped: {skipped}; failures: {failures}")
        return 1 if failures else 0
    except RecoveryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print(f"Copied: {copied}; skipped: {skipped}; failures: {failures + 1}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
