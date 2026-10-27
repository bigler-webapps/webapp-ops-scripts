"""Shared host-wide exclusion and durable application recovery state.

The production paths are deliberately fixed. Callers may pass temporary paths
when exercising the contract in isolated tests, but there is no environment
switch that can disable the production barrier.
"""

from __future__ import annotations

import contextlib
import hashlib
try:
    import fcntl
except ImportError:  # Production requires Linux flock; keep pure contract imports portable.
    fcntl = None
import json
import os
import re
import signal
import shlex
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterable

LOCK_PATH = Path("/tmp/deploy-server.lock")
STATE_ROOT = Path("/var/lib/webapp-recovery")
STATE_GROUP = "deploy"
SCHEMA_VERSION = 1
IDENTITY_PATH = Path("/usr/local/lib/webapp-recovery/identity.json")
GATE_TARGET = "/run/webapp-recovery"
GATE_MARKER = "webapp-recovery-gate-v1"
PHASES = ("database", "artifacts", "migration", "checks", "application")
STATUSES = {
    "database": {"not_attempted", "importing", "completed", "failed", "unknown"},
    "artifacts": {"not_applicable", "not_attempted", "completed", "partial", "skipped", "failed", "unknown"},
    "migration": {"not_attempted", "running", "completed", "failed", "unknown"},
    "checks": {"not_attempted", "running", "completed", "failed", "unknown"},
    "application": {"expected", "stopped", "running", "healthy", "unhealthy", "unknown", "failed"},
}
_local = threading.local()


class RecoveryError(RuntimeError):
    pass


def _installed_identity() -> dict:
    fd = os.open(IDENTITY_PATH, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or info.st_size > 1024:
            raise RecoveryError("unsafe installed recovery identity")
        data = json.loads(os.read(fd, 1025))
        if not isinstance(data, dict) or any(type(data.get(key)) is not int or data[key] < 1 for key in ("uid", "gid")):
            raise RecoveryError("invalid installed recovery identity")
        return data
    finally:
        os.close(fd)


def _deployment_gid() -> int:
    import grp
    try:
        return grp.getgrnam(STATE_GROUP).gr_gid
    except KeyError:
        return _installed_identity()["gid"]


def _deployment_uid() -> int:
    import pwd
    try:
        return pwd.getpwnam("deploy").pw_uid
    except KeyError:
        return _installed_identity()["uid"]


def _gate_dir(project: str, state_root: Path | str, *, create=False) -> Path:
    root = Path(state_root)
    parent = root / "gates"
    if root != STATE_ROOT and create and not parent.exists():
        parent.mkdir(mode=0o770)
        parent.chmod(0o770)
    _mkdir_state_root(parent, test_path=root != STATE_ROOT)
    path = parent / _safe_component(project, "project")
    if create:
        try:
            path.mkdir(mode=0o2775)
            path.chmod(0o2775)
        except FileExistsError:
            pass
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o002:
        raise RecoveryError("unsafe application startup gate directory")
    if root == STATE_ROOT and (info.st_uid not in {0, _deployment_uid()} or info.st_gid != _deployment_gid()):
        raise RecoveryError("unsafe application startup gate ownership")
    return path


def read_startup_gate(project: str, state_root: Path | str = STATE_ROOT) -> dict | None:
    try:
        path = _gate_dir(project, state_root) / "gate"
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022 or info.st_size > 4096:
            raise RecoveryError("unsafe application startup gate")
        if Path(state_root) == STATE_ROOT and (info.st_uid not in {0, _deployment_uid()} or info.st_gid != _deployment_gid()):
            raise RecoveryError("unsafe application startup gate owner")
        raw = os.read(fd, 4097).decode("utf-8")
        state, metadata = raw.split("\n", 1)
        data = json.loads(metadata)
        if state not in {"open", "blocked"} or not isinstance(data, dict) or data.get("project") != project:
            raise RecoveryError("invalid application startup gate")
        return {**data, "gate": state}
    finally:
        os.close(fd)


def _write_startup_gate(project: str, state: str, record: dict, state_root: Path | str = STATE_ROOT) -> None:
    directory = _gate_dir(project, state_root, create=True)
    data = {key: record.get(key) for key in ("app", "source", "operation_id", "origin_run_id", "origin_attempt")}
    data["project"] = project
    fd, temporary = tempfile.mkstemp(prefix=".gate-", dir=directory)
    try:
        os.fchmod(fd, 0o644)
        payload = (state + "\n" + json.dumps(data, separators=(",", ":")) + "\n").encode("utf-8")
        os.write(fd, payload)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(temporary, directory / "gate")
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def gated_compose_override(project: str, containers: list[dict], *, state_root: Path | str = STATE_ROOT) -> dict:
    """Compile fixed existing container commands into app-local gated startup.

    This returns Compose data, never a host command executor. Original argv comes
    from the selected project's created containers and executes only inside them.
    """
    project = _safe_component(project, "project")
    if read_startup_gate(project, state_root) is None:
        _write_startup_gate(project, "open", {}, state_root)
    result = {"services": {}}
    for item in containers:
        labels = (item.get("Config") or {}).get("Labels") or {}
        if labels.get("com.docker.compose.project") != project:
            raise RecoveryError("startup gate container belongs to another project")
        service = labels.get("com.docker.compose.service")
        policy = ((item.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name", "no")
        if service == "db" or policy == "no" or labels.get("com.docker.compose.oneoff", "false").lower() == "true":
            continue
        if not isinstance(service, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}", service):
            raise RecoveryError("invalid startup gate service")
        argv = [item.get("Path"), *(item.get("Args") or [])]
        if not all(isinstance(arg, str) and arg and "\0" not in arg for arg in argv):
            raise RecoveryError("existing container startup command is unavailable")
        script = ("set -eu\ntrap 'exit 143' TERM INT\nwhile :; do\n"
                  "  gate_state=blocked\n"
                  f"  IFS= read -r gate_state < {GATE_TARGET}/gate || gate_state=blocked\n"
                  "  [ \"$gate_state\" = open ] && break\n  sleep 1\ndone\nexec " + shlex.join(argv))
        result["services"][service] = {
            "entrypoint": ["/bin/sh", "-c", script.replace("$", "$$"), GATE_MARKER], "command": [],
            "volumes": [{"type": "bind", "source": str(Path(state_root) / "gates" / project),
                         "target": GATE_TARGET, "read_only": True, "bind": {"create_host_path": False}}],
        }
    return result


class _ContainerStartWatcher:
    """Observe create/start events even when a foreign container is removed again."""

    def __init__(self, docker: str, project: str):
        self.started_ns = time.time_ns()
        self.output = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        self.proc = subprocess.Popen(
            [docker, "events", "--since", str(int(self.started_ns / 1_000_000_000)),
             "--filter", f"label=com.docker.compose.project={project}",
             "--filter", "type=container", "--filter", "event=create", "--filter", "event=start",
             "--format", '{{.TimeNano}}|{{.Actor.ID}}|{{.Actor.Attributes.name}}|{{index .Actor.Attributes "com.docker.compose.service"}}|{{index .Actor.Attributes "com.docker.compose.oneoff"}}'],
            stdout=self.output, stderr=subprocess.DEVNULL, text=True,
        )

    def unexpected(self, allowed_ids: set[str]) -> list[str]:
        if self.proc.poll() not in {None, 0}:
            raise RecoveryError("Docker event observation stopped unexpectedly")
        self.output.flush()
        self.output.seek(0)
        violations = []
        for line in self.output.read().splitlines():
            fields = line.split("|", 4)
            if len(fields) != 5:
                raise RecoveryError("Docker event observation returned an incomplete record")
            try:
                timestamp = int(fields[0])
            except ValueError as exc:
                raise RecoveryError("Docker event observation returned an invalid timestamp") from exc
            full_id, name, service, oneoff = fields[1:]
            if timestamp < self.started_ns or service == "db" or oneoff.lower() == "true":
                continue
            if any(full_id.startswith(cid) for cid in allowed_ids):
                continue
            violations.append(name or full_id[:12])
        self.output.seek(0, os.SEEK_END)
        return sorted(set(violations))

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self.output.close()


def _safe_component(value: str, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise RecoveryError(f"invalid {label}")
    if value in {".", ".."}:
        raise RecoveryError(f"invalid {label}")
    return value


def _repair_identifier(run_id: str, attempt: str, project: str, app: str) -> str:
    material = "\0".join((run_id, attempt, project, app)).encode("utf-8")
    return "repair-" + hashlib.sha256(material).hexdigest()[:48]


def _check_regular(path: Path, expected_uid: int | None = None) -> os.stat_result:
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        raise RecoveryError(f"unsafe non-regular file: {path.name}")
    if expected_uid is not None and st.st_uid != expected_uid:
        raise RecoveryError(f"unsafe file owner: {path.name}")
    if st.st_mode & 0o002:
        raise RecoveryError(f"unsafe writable file: {path.name}")
    return st


def _open_lock(path: Path, *, read_only: bool = False) -> int:
    if fcntl is None:
        raise RecoveryError("destination recovery locking requires Linux flock")
    # IaC/tmpfiles installs this root-owned inode before callers are enabled.
    # Refusing a missing lock avoids accidentally creating a second lock during
    # a mixed-version rollout.
    flags = (os.O_RDONLY if read_only else os.O_RDWR) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    st = os.fstat(fd)
    production = path == LOCK_PATH
    expected_gid = _deployment_gid() if production else os.getegid()
    expected_uid = 0 if production else os.geteuid()
    if (not stat.S_ISREG(st.st_mode) or st.st_uid != expected_uid
            or production and (st.st_gid != expected_gid or st.st_mode & 0o007 or not st.st_mode & stat.S_IWGRP)
            or st.st_mode & 0o002):
        os.close(fd)
        raise RecoveryError("unsafe shared recovery lock")
    return fd


def _mkdir_state_root(root: Path, *, test_path: bool = False) -> None:
    # Walk every component so a substituted parent cannot redirect atomic state
    # writes. Test-only roots may live under /tmp, but production may not.
    for part in (root, *root.parents):
        part_stat = os.lstat(part)
        if not stat.S_ISDIR(part_stat.st_mode):
            raise RecoveryError("unsafe recovery state directory component")
        if not test_path and part_stat.st_uid != 0:
            raise RecoveryError("recovery state directory component is not root-owned")
        if stat.S_ISLNK(part_stat.st_mode):
            raise RecoveryError("symlink in recovery state directory path")
        if part != root and not test_path and part != STATE_ROOT and part_stat.st_mode & 0o022:
            raise RecoveryError("writable parent in recovery state path")
        if not test_path and part == STATE_ROOT:
            trusted_gid = _deployment_gid()
            if part_stat.st_gid != trusted_gid or part_stat.st_mode & 0o007:
                raise RecoveryError("unsafe trusted recovery state ancestor")
    st = os.lstat(root)
    expected_uids = {0, os.geteuid()} if test_path else {0}
    if not stat.S_ISDIR(st.st_mode) or st.st_uid not in expected_uids or st.st_mode & 0o002:
        raise RecoveryError("unsafe recovery state directory")
    # The trusted group is provisioned by IaC for deploy and dbrecv identities.
    required_gid = st.st_gid if test_path else _deployment_gid()
    if (not test_path and st.st_gid != required_gid) or not (st.st_mode & stat.S_IWGRP) or st.st_mode & 0o007:
        raise RecoveryError("recovery state directory permissions are not provisioned")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o007:
            raise RecoveryError("unsafe recovery state file")
        if st.st_size > 65536:
            raise RecoveryError("recovery state file exceeds the bounded record size")
        if STATE_ROOT in path.parents:
            recovery_gid = _deployment_gid()
            if st.st_uid not in {0, _deployment_uid()} or st.st_gid != recovery_gid:
                raise RecoveryError("recovery state file has unexpected ownership")
        with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as stream:
            data = json.load(stream)
        if not isinstance(data, dict) or data.get("schema") != SCHEMA_VERSION:
            raise RecoveryError("invalid recovery state record")
        return data
    finally:
        os.close(fd)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    parent = path.parent
    test_path = STATE_ROOT not in parent.parents and parent != STATE_ROOT
    _mkdir_state_root(parent, test_path=test_path)
    payload = (json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix=".recovery-", dir=str(parent))
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, 0o660)
        try:
            expected_gid = os.getegid() if test_path else _deployment_gid()
            if os.geteuid() == 0 and os.fstat(fd).st_gid != expected_gid:
                os.fchown(fd, 0, expected_gid)
            elif os.fstat(fd).st_gid != expected_gid:
                raise RecoveryError("state file did not inherit the trusted recovery group")
        except (ImportError, KeyError, PermissionError):
            if not test_path:
                raise RecoveryError("could not set recovery state ownership")
        with os.fdopen(fd, "wb", closefd=True) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        dfd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def state_path(project: str, state_root: Path | str = STATE_ROOT) -> Path:
    return Path(state_root) / "apps" / f"{_safe_component(project, 'project')}.json"


def read_status(project: str, state_root: Path | str = STATE_ROOT) -> dict[str, Any] | None:
    root = Path(state_root)
    _mkdir_state_root(root / "apps", test_path=root != STATE_ROOT)
    return _read_json(state_path(project, root))


def _host_reservation_path(state_root: Path | str = STATE_ROOT) -> Path:
    return Path(state_root) / "host-maintenance.json"


def _auto_restart_path(state_root: Path | str = STATE_ROOT) -> Path:
    return Path(state_root) / "auto_restarts" / "inflight.json"


def read_host_reservation(state_root: Path | str = STATE_ROOT) -> dict[str, Any] | None:
    """Return the durable whole-host maintenance reservation, if any."""
    root = Path(state_root)
    _mkdir_state_root(root, test_path=root != STATE_ROOT)
    return _read_json(_host_reservation_path(root))


def read_auto_restart(state_root: Path | str = STATE_ROOT) -> dict[str, Any] | None:
    root = Path(state_root)
    _mkdir_state_root(root, test_path=root != STATE_ROOT)
    return _read_json(_auto_restart_path(root))


def begin_auto_restart(project: str, container_id: str, started_at: str,
                       state_root: Path | str = STATE_ROOT) -> dict[str, Any]:
    """Persist intent before a Docker API restart can outlive its client."""
    root = Path(state_root)
    _safe_component(project, "project")
    if not isinstance(container_id, str) or not re.fullmatch(r"[0-9a-f]{12,64}", container_id):
        raise RecoveryError("invalid automatic restart container identity")
    if not isinstance(started_at, str) or len(started_at) > 64:
        raise RecoveryError("invalid automatic restart observation")
    path = _auto_restart_path(root)
    existing = _read_json(path)
    if existing:
        raise RecoveryError("an automatic restart is awaiting reconciliation")
    record = {"schema": SCHEMA_VERSION, "project": project, "container_id": container_id,
              "previous_started_at": started_at, "status": "in_flight", "created_at": int(time.time())}
    _write_json(path, record)
    return record


def finish_auto_restart(project: str, container_id: str, started_at: str, *, running: bool,
                        health: str | None, state_root: Path | str = STATE_ROOT) -> None:
    """Clear only after the same container's restart is observed complete."""
    path = _auto_restart_path(state_root)
    current = _read_json(path)
    if not current or current.get("project") != project or current.get("container_id") != container_id:
        raise RecoveryError("automatic restart identity does not match its in-flight marker")
    if (not running or started_at == current.get("previous_started_at")
            or health in {"starting", "unhealthy"}):
        raise RecoveryError("automatic restart completion was not observed")
    path.unlink()


@contextlib.contextmanager
def _raw_host_lock(lock_path: Path | str = LOCK_PATH, lock_timeout: float = 300.0,
                   *, read_only: bool = False):
    """Take the common host lock without writer admission for reservation control."""
    fd = _open_lock(Path(lock_path), read_only=read_only)
    deadline = time.monotonic() + lock_timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RecoveryError("timed out waiting for destination recovery lock")
                time.sleep(min(0.1, max(deadline - time.monotonic(), 0)))
        yield fd
    finally:
        os.close(fd)


def begin_provisioning_reservation(run_id: str, *, lock_path: Path | str = LOCK_PATH,
                                  state_root: Path | str = STATE_ROOT,
                                  lock_timeout: float = 300.0) -> dict[str, Any]:
    """Reserve the whole host across a multi-task Ansible provisioning run.

    The caller must be the trusted root provisioning identity. A reservation
    is durable by design: an interrupted play remains visible and blocks all
    supported writers until the same run identity explicitly finishes it.
    """
    if Path(state_root) == STATE_ROOT and os.geteuid() != 0:
        raise RecoveryError("provisioning reservation requires root")
    identity = _safe_component(run_id, "provisioning run id")
    root = Path(state_root)
    with _raw_host_lock(lock_path, lock_timeout):
        _mkdir_state_root(root, test_path=root != STATE_ROOT)
        existing = _read_json(_host_reservation_path(root))
        if existing:
            if existing.get("status") != "failed" or existing.get("run_id") != identity:
                raise RecoveryError("a host provisioning reservation is already active")
        if read_auto_restart(root):
            raise RecoveryError("provisioning refused while an automatic restart is unresolved")
        apps = root / "apps"
        _mkdir_state_root(apps, test_path=root != STATE_ROOT)
        unresolved = [p.stem for p in apps.glob("*.json")
                      if (record := _read_json(p)) and record.get("status") != "completed"]
        if unresolved:
            raise RecoveryError("provisioning refused while application recovery is required")
        reservation = {"schema": SCHEMA_VERSION, "run_id": identity,
                       "status": "running", "started_at": int(time.time())}
        if existing:
            reservation.update(previous_status="failed", resumed_at=int(time.time()),
                               resume_count=int(existing.get("resume_count", 0)) + 1)
        _write_json(_host_reservation_path(root), reservation)
        return reservation


def finish_provisioning_reservation(run_id: str, *, success: bool,
                                    lock_path: Path | str = LOCK_PATH,
                                    state_root: Path | str = STATE_ROOT,
                                    lock_timeout: float = 300.0) -> dict[str, Any] | None:
    """Finish only the matching reservation; failures remain fail-closed."""
    if Path(state_root) == STATE_ROOT and os.geteuid() != 0:
        raise RecoveryError("provisioning reservation requires root")
    identity = _safe_component(run_id, "provisioning run id")
    root = Path(state_root)
    path = _host_reservation_path(root)
    with _raw_host_lock(lock_path, lock_timeout):
        current = _read_json(path)
        if not current:
            raise RecoveryError("no host provisioning reservation exists")
        if current.get("run_id") != identity:
            raise RecoveryError("provisioning reservation identity does not match")
        if success:
            apps = root / "apps"
            unresolved = [p.stem for p in apps.glob("*.json")
                          if (record := _read_json(p)) and record.get("status") != "completed"]
            if unresolved or read_auto_restart(root):
                raise RecoveryError("provisioning cannot finish while a recovery or automatic restart is unresolved")
            path.unlink()
            return None
        failed = {**current, "status": "failed", "finished_at": int(time.time())}
        _write_json(path, failed)
        return failed


def admit_writer(project: str | None, state_root: Path | str = STATE_ROOT, *,
                 _allow_auto_restart_marker: bool = False) -> None:
    """Fail closed while a durable recovery barrier is present.

    This is an ordinary-writer check and has no recovery-ID bypass. Authorized
    recovery uses RecoveryTransaction, whose receiver boundary authenticates
    source and operation independently.
    """
    reservation = read_host_reservation(state_root)
    if reservation:
        raise RecoveryError("host provisioning reservation is active; ordinary writer refused")
    if not _allow_auto_restart_marker and read_auto_restart(state_root):
        raise RecoveryError("automatic restart completion is unresolved; ordinary writer refused")
    gate_root = Path(state_root) / "gates"
    projects = [project] if project is not None else (
        [path.name for path in gate_root.iterdir()] if gate_root.exists() else [])
    for candidate in projects:
        gate = read_startup_gate(candidate, state_root)
        if gate and gate["gate"] == "blocked":
            raise RecoveryError("application startup gate requires recovery; ordinary writer refused")
    if project is None:
        apps = Path(state_root) / "apps"
        _mkdir_state_root(apps, test_path=Path(state_root) != STATE_ROOT)
        try:
            entries = sorted(apps.glob("*.json"))
        except OSError as exc:
            raise RecoveryError("recovery state inventory is unavailable") from exc
        records = [(entry.stem, _read_json(entry)) for entry in entries]
    else:
        records = [(project, read_status(project, state_root))]
    for app_project, record in records:
        if record and record.get("status") != "completed":
            raise RecoveryError(f"application recovery is required for {app_project}; ordinary writer refused")


class WriterGuard:
    def __init__(self, project: str | None, lock_path: Path | str, state_root: Path | str,
                 lock_timeout: float):
        self.project = _safe_component(project, "project") if project is not None else None
        self.lock_path = Path(lock_path)
        self.state_root = Path(state_root)
        self.lock_timeout = lock_timeout
        self.fd: int | None = None
        self._nested = False

    def __enter__(self):
        current = getattr(_local, "transaction", None) or getattr(_local, "writer", None)
        if current is not None:
            if (current.lock_path != self.lock_path or current.state_root != self.state_root
                    or current.project != self.project):
                raise RecoveryError("nested writer identity does not match the held contract")
            self._nested = True
            self.fd = current.fd
        else:
            self.fd = _open_lock(self.lock_path)
            deadline = time.monotonic() + self.lock_timeout
            while True:
                try:
                    fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        os.close(self.fd)
                        self.fd = None
                        raise RecoveryError("timed out waiting for destination recovery lock")
                    time.sleep(min(0.1, max(deadline - time.monotonic(), 0)))
        try:
            admit_writer(self.project, self.state_root)
        except BaseException:
            if not self._nested and self.fd is not None:
                os.close(self.fd)
                self.fd = None
            raise
        if not self._nested:
            _local.writer = self
        return self

    def __exit__(self, exc_type, exc, tb):
        if not self._nested and self.fd is not None:
            # Closing the owner's descriptor is essential: LOCK_UN would also
            # release the inherited open-file-description lock held by children.
            os.close(self.fd)
            self.fd = None
            if getattr(_local, "writer", None) is self:
                del _local.writer
        return False


def writer_guard(project: str | None, *, lock_path: Path | str = LOCK_PATH,
                 state_root: Path | str = STATE_ROOT, lock_timeout: float = 300.0) -> WriterGuard:
    """Guard one project, or all projects for host-wide maintenance/reboot."""
    return WriterGuard(project, lock_path, state_root, lock_timeout)


def _boot_id() -> str:
    value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[a-f0-9-]{36}", value):
        raise RecoveryError("host boot identity is unavailable")
    return value


def reconcile_completed_maintenance_reboot(*, lock_path: Path | str = LOCK_PATH,
                                          state_root: Path | str = STATE_ROOT) -> None:
    """A different kernel boot proves the old reboot requester has terminated."""
    with _raw_host_lock(lock_path):
        current = read_host_reservation(state_root)
        if (current and current.get("kind") == "maintenance-reboot"
                and current.get("boot_id") != _boot_id()):
            _host_reservation_path(state_root).unlink()


@contextlib.contextmanager
def maintenance_guard(operation: str, *, lock_path: Path | str = LOCK_PATH,
                      state_root: Path | str = STATE_ROOT):
    """Keep durable exclusion when sudo closes inherited file descriptors.

    Failed/interrupted apt work requires explicit operator/IaC reconciliation;
    it is never cleared merely because its unprivileged requester disappeared.
    """
    if operation not in {"apt", "reboot"}:
        raise RecoveryError("unsupported fixed maintenance operation")
    reconcile_completed_maintenance_reboot(lock_path=lock_path, state_root=state_root)
    with writer_guard(None, lock_path=lock_path, state_root=state_root) as guard:
        record = {"schema": SCHEMA_VERSION, "kind": f"maintenance-{operation}",
                  "run_id": str(time.time_ns()), "status": "running", "boot_id": _boot_id(),
                  "started_at": int(time.time())}
        path = _host_reservation_path(state_root)
        _write_json(path, record)
        try:
            yield guard
        except BaseException:
            record.update(status="failed", finished_at=int(time.time()))
            with contextlib.suppress(Exception):
                _write_json(path, record)
            raise
        else:
            if operation != "apt":
                raise RecoveryError("reboot completion requires an observed new kernel boot")
            path.unlink()


@contextlib.contextmanager
def automatic_restart_guard(project: str, *, lock_path: Path | str = LOCK_PATH,
                            state_root: Path | str = STATE_ROOT, lock_timeout: float = 30.0):
    """Lock-aware fixed adapter admission that can reconcile its own marker."""
    with _raw_host_lock(lock_path, lock_timeout, read_only=True) as fd:
        admit_writer(project, state_root, _allow_auto_restart_marker=True)
        yield fd


def record_repair(repair_id: str, project: str, app: str, source: str, run_id: str,
                  state_root: Path | str = STATE_ROOT, *, attempt: str = "1") -> dict[str, Any]:
    """Durably create one repair item; repeated IDs cannot replace its origin."""
    repairs = Path(state_root) / "repairs"
    rid = _safe_component(repair_id, "repair id")
    target = repairs / f"{rid}.json"
    existing = _read_json(target)
    if existing:
        if any(existing.get(k) != v for k, v in {"project": project, "app": app, "source": source,
                                                 "run_id": run_id, "attempt": attempt}.items()):
            raise RecoveryError("repair identity collision")
        return existing
    item = {"schema": SCHEMA_VERSION, "repair_id": rid, "project": _safe_component(project, "project"),
            "app": _safe_component(app, "app"), "source": _safe_component(source, "source"),
            "run_id": _safe_component(run_id, "run id"), "attempt": _safe_component(attempt, "attempt"),
            "status": "pending", "satisfied_by": None}
    _write_json(target, item)
    return item


def pending_repairs(project: str, app: str, source: str | None = None,
                    state_root: Path | str = STATE_ROOT) -> list[dict[str, Any]]:
    repairs = Path(state_root) / "repairs"
    _mkdir_state_root(repairs, test_path=Path(state_root) != STATE_ROOT)
    found = []
    for item_path in sorted(repairs.glob("*.json")):
        item = _read_json(item_path)
        if (item and item.get("status") == "pending" and item.get("project") == project
                and item.get("app") == app and (source is None or item.get("source") == source)):
            found.append(item)
    unresolved = read_status(project, state_root)
    gate = read_startup_gate(project, state_root)
    if gate and gate["gate"] == "blocked" and (not unresolved or gate.get("operation_id") != unresolved.get("operation_id")):
        unresolved = {**gate, "status": "recovery-required"}
    if (unresolved and unresolved.get("status") != "completed" and unresolved.get("app") == app
            and unresolved.get("origin_run_id") and unresolved.get("origin_attempt")
            and (source is None or unresolved.get("source") == source)):
        repair_id = _repair_identifier(unresolved["origin_run_id"], unresolved["origin_attempt"], project, app)
        if not any(item.get("repair_id") == repair_id for item in found):
            found.append({
                "repair_id": repair_id, "project": project, "app": app,
                "source": unresolved["source"], "run_id": unresolved["origin_run_id"],
                "attempt": unresolved["origin_attempt"], "status": "interrupted",
                "satisfied_by": None,
            })
    return found


class RecoveryTransaction:
    """One host lock and one per-app durable barrier spanning all recovery phases.

    Fixed Docker children inherit the flock descriptor. Thus they retain
    exclusion if this Python owner is killed while an import or migration lives.
    """

    def __init__(self, project: str, app: str, operation_id: str, source: str = "manual",
                 *, expected_final: str = "running", auto_restart: bool = True,
                 restart_left_stopped: bool = False, lock_path: Path | str = LOCK_PATH,
                 state_root: Path | str = STATE_ROOT, docker: str = "docker",
                 runner=None, lock_timeout: float = 300.0,
                 origin_run_id: str | None = None, origin_attempt: str | None = None,
                 repair_ids: Iterable[str] = (), require_startup_gate: bool | None = None):
        self.project = _safe_component(project, "project")
        self.app = _safe_component(app, "app")
        self.operation_id = _safe_component(operation_id, "operation id")
        self.source = _safe_component(source, "source")
        if expected_final not in {"running", "stopped", "preserve"}:
            raise RecoveryError("invalid expected final application state")
        if expected_final != "preserve" and (expected_final == "running") != bool(auto_restart):
            raise RecoveryError("auto_restart must agree with the expected final application state")
        self.requested_final = expected_final
        self.expected_final = expected_final
        self.auto_restart = auto_restart
        self.restart_left_stopped = restart_left_stopped
        self.lock_path = Path(lock_path)
        self.state_root = Path(state_root)
        self.path = state_path(self.project, self.state_root)
        self.require_startup_gate = self.state_root == STATE_ROOT if require_startup_gate is None else require_startup_gate
        self.docker = docker
        self.runner = runner or subprocess.run
        self.lock_timeout = lock_timeout
        if bool(origin_run_id) != bool(origin_attempt):
            raise RecoveryError("origin run ID and attempt must be supplied together")
        self.origin_run_id = _safe_component(origin_run_id, "origin run id") if origin_run_id else None
        self.origin_attempt = _safe_component(origin_attempt, "origin attempt") if origin_attempt else None
        self.request_repair_ids = [_safe_component(value, "repair id") for value in repair_ids]
        self.fd: int | None = None
        self.record: dict[str, Any] | None = None
        self.services: list[dict[str, Any]] = []
        self.db_container_id: str | None = None
        self.db_running = False
        self.db_container_id: str | None = None
        self._nested = False
        self.reconcile_only = False
        self.already_completed = False
        self._previous_handler = None
        self._entered = False
        self.event_watcher: _ContainerStartWatcher | None = None

    def _docker(self, *args: str, check: bool = True):
        if not args or args[0] not in {"ps", "inspect", "update", "stop", "start", "top"}:
            raise RecoveryError("unsupported recovery Docker control")
        command = [self.docker, *args]
        if self.runner is not subprocess.run:
            return self.runner(command, capture_output=True, text=True, check=check)
        proc = subprocess.Popen(command, pass_fds=(self.fd,), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        stdout, stderr = proc.communicate()
        result = subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)
        if check and result.returncode:
            raise RecoveryError(f"Docker recovery control failed ({result.returncode})")
        return result

    def _acquire(self) -> None:
        current = getattr(_local, "transaction", None)
        if current is not None:
            if (current.project != self.project or current.app != self.app
                    or current.operation_id != self.operation_id or current.source != self.source
                    or current.state_root != self.state_root):
                raise RecoveryError("nested recovery must use the active destination transaction")
            self._nested = True
            self.fd = current.fd
            self.record = current.record
            self.services = current.services
            return
        self.fd = _open_lock(self.lock_path)
        deadline = time.monotonic() + self.lock_timeout
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(self.fd)
                    self.fd = None
                    raise RecoveryError("timed out waiting for destination recovery lock")
                time.sleep(min(0.1, max(deadline - time.monotonic(), 0)))

    def _load_services(self) -> list[dict[str, Any]]:
        ps = self._docker("ps", "-a", "-q", "-f", f"label=com.docker.compose.project={self.project}")
        ids = [line.strip() for line in ps.stdout.splitlines() if line.strip()]
        if not ids:
            raise RecoveryError("no labelled containers found for recovery project")
        inspected = self._docker("inspect", *ids)
        result = []
        for item in json.loads(inspected.stdout):
            labels = (item.get("Config") or {}).get("Labels") or {}
            service = labels.get("com.docker.compose.service")
            if service == "db":
                self.db_container_id = (item.get("Id") or "")[:12]
                self.db_running = bool((item.get("State") or {}).get("Running"))
                continue
            if not service or str(labels.get("com.docker.compose.oneoff", "false")).lower() == "true":
                continue
            policy = (((item.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name") or "no")
            result.append({"id": (item.get("Id") or "")[:12], "service": service,
                           "startup_gated": ((item.get("Config") or {}).get("Entrypoint") or [None])[-1] == GATE_MARKER
                           and any(mount.get("Source") == str(self.state_root / "gates" / self.project)
                                   and mount.get("Destination") == GATE_TARGET and mount.get("RW") is False
                                   for mount in item.get("Mounts", [])),
                           "running": bool((item.get("State") or {}).get("Running")),
                           "started_at": (item.get("State") or {}).get("StartedAt"),
                           "restart_policy": policy,
                           "working_dir": labels.get("com.docker.compose.project.working_dir"),
                           "config_files": labels.get("com.docker.compose.project.config_files")})
        return result

    def __enter__(self):
        self._acquire()
        if self._nested:
            self._entered = True
            return self
        try:
            if read_host_reservation(self.state_root):
                raise RecoveryError("host provisioning reservation is active; recovery refused")
            if read_auto_restart(self.state_root):
                raise RecoveryError("automatic restart completion is unresolved; recovery refused")
            _mkdir_state_root(self.path.parent, test_path=self.state_root != STATE_ROOT)
            old = _read_json(self.path)
            gate = read_startup_gate(self.project, self.state_root)
            if gate and gate["gate"] == "blocked" and (not old or gate.get("operation_id") != old.get("operation_id")):
                if gate.get("source") != self.source or gate.get("app") != self.app:
                    raise RecoveryError("orphan startup gate belongs to another application/source")
                if gate.get("origin_run_id") and gate.get("origin_attempt"):
                    repair_id = _repair_identifier(gate["origin_run_id"], gate["origin_attempt"], self.project, self.app)
                    record_repair(repair_id, self.project, self.app, self.source, gate["origin_run_id"],
                                  self.state_root, attempt=gate["origin_attempt"])
                old = None
            if old and old.get("status") == "completed" and old.get("operation_id") == self.operation_id:
                if old.get("source") != self.source or old.get("app") != self.app:
                    raise RecoveryError("completed operation identity does not match this application/source")
                self.record = old
                self.already_completed = True
                for repair_id in old.get("satisfied_repair_ids", []):
                    target = Path(self.state_root) / "repairs" / f"{_safe_component(repair_id, 'repair id')}.json"
                    item = _read_json(target)
                    if (item and item.get("status") == "pending" and item.get("project") == self.project
                            and item.get("app") == self.app and item.get("source") == self.source):
                        item.update(status="completed", satisfied_by=self.operation_id)
                        _write_json(target, item)
                _local.transaction = self
                self._entered = True
                return self
            superseding = False
            if old and old.get("status") != "completed":
                if old.get("source") != self.source or old.get("app") != self.app:
                    raise RecoveryError("a different or incomplete recovery already owns this application")
                superseding = old.get("operation_id") != self.operation_id
                self.reconcile_only = not superseding and old.get("database") in {"completed", "importing", "unknown"}
                if old.get("origin_run_id") and old.get("origin_attempt"):
                    old_repair_id = _repair_identifier(old["origin_run_id"], old["origin_attempt"], self.project, self.app)
                    record_repair(old_repair_id, self.project, self.app, self.source,
                                  old["origin_run_id"], self.state_root,
                                  attempt=old["origin_attempt"])
            self.services = self._load_services()
            if self.require_startup_gate and any(
                    service["restart_policy"] != "no" and not service["startup_gated"] for service in self.services):
                raise RecoveryError("application startup gate has not been adopted; recovery refused")
            if superseding:
                self._assert_no_active_recovery_children()
            if old and (self.reconcile_only or superseding):
                previous = {item.get("service"): item for item in old.get("services", [])}
                current = {item.get("service"): item for item in self.services}
                if set(previous) != set(current):
                    raise RecoveryError("recovery service inventory changed; manual reconciliation required")
                self.services = [
                    {**current[name], "running": previous[name]["running"],
                     "restart_policy": previous[name]["restart_policy"]}
                    for name in sorted(current)
                ]
            if self.requested_final == "preserve":
                preserved = old.get("services", self.services) if old else self.services
                should_restart = self.auto_restart and any(
                    service["running"] and service["restart_policy"] != "no"
                    for service in preserved
                )
                self.expected_final = "running" if should_restart else "stopped"
            self.record = {
                "schema": SCHEMA_VERSION, "project": self.project, "app": self.app,
                "operation_id": self.operation_id, "source": self.source,
                "status": "recovery-required", "created_at": old.get("created_at") if old else int(time.time()),
                "updated_at": int(time.time()), "expected_application": self.expected_final,
                "requested_application": self.requested_final,
                "database": old.get("database", "not_attempted") if old and self.reconcile_only else "not_attempted",
                "artifacts": old.get("artifacts", "not_attempted") if old and self.reconcile_only else "not_attempted",
                "migration": old.get("migration", "not_attempted") if old and self.reconcile_only else "not_attempted",
                "checks": old.get("checks", "not_attempted") if old and self.reconcile_only else "not_attempted",
                "application": "unknown", "observed_application": "unknown",
                "repairs": old.get("repairs", []) if old else [],
                "origin_run_id": self.origin_run_id or (old.get("origin_run_id") if old else None),
                "origin_attempt": self.origin_attempt or (old.get("origin_attempt") if old else None),
                "repair_ids": sorted(set((old.get("repair_ids", []) if old and old.get("status") != "completed" else []) + self.request_repair_ids)),
                "supersedes_operation_id": old.get("operation_id") if superseding else None,
                "services": self.services,
            }
            if len(self.record["repair_ids"]) > 100:
                raise RecoveryError("too many repair IDs in one recovery record")
            _write_startup_gate(self.project, "blocked", self.record, self.state_root)
            _write_json(self.path, self.record)
            if self.runner is subprocess.run:
                self.event_watcher = _ContainerStartWatcher(self.docker, self.project)
            # The startup gate also covers interruption midway through policy
            # inhibition, including a Docker or host restart.
            for service in self.services:
                if service["restart_policy"] != "no":
                    self._docker("update", "--restart=no", service["id"])
            running_ids = [s["id"] for s in self.services if s["running"]]
            if running_ids:
                self._docker("stop", *running_ids)
            self._previous_handler = signal.getsignal(signal.SIGTERM)
            try:
                signal.signal(signal.SIGTERM, self._sigterm)
            except ValueError:
                self._previous_handler = None
            _local.transaction = self
            self._entered = True
            return self
        except BaseException:
            if self.event_watcher is not None:
                with contextlib.suppress(Exception):
                    self.event_watcher.close()
                self.event_watcher = None
            if self.record is not None:
                self.record["status"] = "recovery-required"
                self.record["updated_at"] = int(time.time())
                with contextlib.suppress(Exception):
                    ids = [service["id"] for service in self.services]
                    if ids:
                        self._docker("stop", *ids)
                        states = json.loads(self._docker("inspect", *ids).stdout)
                        self.record["application"] = (
                            "stopped" if all(not bool((item.get("State") or {}).get("Running")) for item in states)
                            else "unknown"
                        )
                        self.record["observed_application"] = self.record["application"]
                with contextlib.suppress(Exception):
                    self._record_failed_repair()
                with contextlib.suppress(Exception):
                    _write_json(self.path, self.record)
            self._release()
            raise

    @staticmethod
    def _sigterm(signum, frame):
        raise RecoveryError(f"received signal {signum}; durable recovery barrier retained")

    def exec_psql(self, container_id: str, *args: str, **kwargs):
        """Run the fixed psql container entrypoint with the recovery lock inherited."""
        if self.fd is None:
            raise RecoveryError("no active destination lock")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", container_id):
            raise RecoveryError("invalid database container identity")
        kwargs.setdefault("stdout", subprocess.DEVNULL)
        kwargs.setdefault("stderr", subprocess.DEVNULL)
        command = [self.docker, "exec", *args[:1], container_id, "psql", *args[1:]] if args[:1] == ("-i",) else [self.docker, "exec", container_id, "psql", *args]
        return subprocess.Popen(command, pass_fds=(self.fd,), **kwargs)

    def run_migrations(self, *, stdout=None, check_only: bool = False):
        """Run migrations in a one-shot compose container while normal backend stays stopped."""
        if self.fd is None:
            raise RecoveryError("no active destination lock")
        backend = next((service for service in self.services if service["service"] == "backend"), None)
        if not backend or not backend.get("working_dir") or not backend.get("config_files"):
            raise RecoveryError("compose metadata for the controlled migration is unavailable")
        app_ids = [service["id"] for service in self.services]
        if app_ids:
            observed = json.loads(self._docker("inspect", *app_ids).stdout)
            if any(bool((item.get("State") or {}).get("Running")) for item in observed):
                raise RecoveryError("ordinary application services must remain stopped for migration")
        working_dir = Path(backend["working_dir"])
        files = [Path(value) for value in backend["config_files"].split(",") if value]
        if (not working_dir.is_absolute() or not working_dir.is_dir() or not files
                or any(not path.is_absolute() or not path.is_file() for path in files)):
            raise RecoveryError("compose metadata for the controlled migration is invalid")
        command = [self.docker, "compose", "--project-directory", str(working_dir)]
        for compose_file in files:
            command.extend(("-f", str(compose_file)))
        command.extend(("--project-name", self.project, "run", "--rm", "--no-deps", "-T",
                        "--entrypoint", "python", "backend", "manage.py", "migrate", "--check" if check_only else "--noinput"))
        return subprocess.Popen(
            command,
            pass_fds=(self.fd,), stdout=stdout if stdout is not None else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _assert_no_active_recovery_children(self) -> None:
        if not self.db_container_id:
            raise RecoveryError("database container is unknown; escalation is required")
        if self.db_running:
            top = self._docker("top", self.db_container_id, check=False)
            if top.returncode != 0:
                raise RecoveryError("could not observe active database writers; escalation is required")
            if re.search(r"\bpsql\b", top.stdout, flags=re.IGNORECASE):
                raise RecoveryError("a database import process is still active")
        oneoffs = self._docker("ps", "-a", "-q", "-f", f"label=com.docker.compose.project={self.project}",
                               "-f", "label=com.docker.compose.oneoff=True", check=False)
        if oneoffs.returncode != 0:
            raise RecoveryError("could not observe recovery containers; escalation is required")
        oneoff_ids = [line.strip() for line in oneoffs.stdout.splitlines() if line.strip()]
        if oneoff_ids:
            inspected = self._docker("inspect", *oneoff_ids, check=False)
            if inspected.returncode != 0:
                raise RecoveryError("could not inspect recovery containers; escalation is required")
            if any(bool((item.get("State") or {}).get("Running")) for item in json.loads(inspected.stdout)):
                raise RecoveryError("a controlled recovery container is still active")

    def record_evidence(self, *, origin_run_id: str, origin_attempt: str,
                        repair_ids: Iterable[str] = ()) -> None:
        if self.record is None:
            raise RecoveryError("no active recovery record")
        ids = [_safe_component(value, "repair id") for value in repair_ids]
        if len(ids) > 100:
            raise RecoveryError("too many repair IDs in one recovery record")
        self.record["origin_run_id"] = _safe_component(origin_run_id, "origin run id")
        self.record["origin_attempt"] = _safe_component(origin_attempt, "origin attempt")
        self.record["repair_ids"] = sorted(set(self.record.get("repair_ids", []) + ids))
        if len(self.record["repair_ids"]) > 100:
            raise RecoveryError("too many repair IDs in one recovery record")
        self.record["updated_at"] = int(time.time())
        _write_json(self.path, self.record)

    def _record_failed_repair(self) -> None:
        if not self.record or not self.record.get("origin_run_id") or not self.record.get("origin_attempt"):
            return
        run_id = self.record["origin_run_id"]
        attempt = self.record["origin_attempt"]
        repair_id = _repair_identifier(run_id, attempt, self.project, self.app)
        item = record_repair(repair_id, self.project, self.app, self.source, run_id,
                             self.state_root, attempt=attempt)
        self.record["repair_ids"] = sorted(set(self.record.get("repair_ids", []) + [item["repair_id"]]))

    def update_phase(self, phase: str, status: str, error_code: str | None = None) -> None:
        if phase not in STATUSES or status not in STATUSES[phase]:
            raise RecoveryError("invalid recovery phase status")
        if self.record is None:
            raise RecoveryError("no active recovery record")
        self.record[phase] = status
        if phase == "application":
            self.record["observed_application"] = status
        if error_code:
            self.record["primary_error"] = _safe_component(error_code, "error code")
        self.record["updated_at"] = int(time.time())
        _write_json(self.path, self.record)

    def mark_database_started(self):
        if self.already_completed:
            raise RecoveryError("operation is already complete; duplicate destructive work is refused")
        if self.reconcile_only:
            raise RecoveryError("database phase is already recorded; reconcile without repeating import")
        self.update_phase("database", "importing")

    def reconcile_database_retry(self) -> None:
        """Authorize same-source/op retry only after no DB or migration child remains."""
        if not self.reconcile_only or not self.record or self.record.get("database") not in {"importing", "unknown"}:
            raise RecoveryError("database does not need interrupted-phase reconciliation")
        self._assert_no_active_recovery_children()
        self.update_phase("database", "failed", error_code="interrupted-reconciled")
        self.reconcile_only = False

    def complete(self, *, artifacts: str = "not_applicable", migration: str = "completed",
                 checks: str = "completed", application: str | None = None,
                 health_check_passed: bool = False,
                 satisfy_repair_ids: Iterable[str] = (), check_backend_http: bool = False) -> dict[str, Any]:
        if self.already_completed:
            return self.record
        if self.record is None:
            raise RecoveryError("no active recovery record")
        if self.record.get("database") != "completed":
            raise RecoveryError("database phase is not recorded as started")
        if artifacts not in STATUSES["artifacts"] or migration != "completed" or checks != "completed":
            raise RecoveryError("required recovery phase is incomplete")
        if artifacts not in {"completed", "not_applicable"}:
            raise RecoveryError("required artifacts are incomplete")
        if self.record.get("migration") != "completed" or self.record.get("checks") != "completed":
            raise RecoveryError("migration and health-check completion must be recorded")
        if application is not None and application != self.expected_final:
            raise RecoveryError("reported application state does not match the expected final state")
        final = "unknown"
        # Validate every requested repair before making the record successful.
        repairs_to_complete = []
        repair_ids = sorted(set(self.record.get("repair_ids", []) + list(satisfy_repair_ids)))
        for repair_id in repair_ids:
            target = Path(self.state_root) / "repairs" / f"{_safe_component(repair_id, 'repair id')}.json"
            item = _read_json(target)
            if (not item or item.get("status") != "pending" or item.get("project") != self.project
                    or item.get("app") != self.app or item.get("source") != self.source):
                raise RecoveryError("repair identity does not match successful recovery")
            repairs_to_complete.append((target, item))
        start_services = [s for s in self.services
                          if (s["running"] and s["restart_policy"] != "no")
                          or self.restart_left_stopped and s["restart_policy"] != "no"]
        start_ids = sorted({s["id"] for s in start_services})
        if self.expected_final == "running" and self.auto_restart and start_ids:
            ids = [service["id"] for service in self.services]
            before = json.loads(self._docker("inspect", *ids).stdout) if ids else []
            running_before = {service["id"] for service, item in zip(self.services, before)
                              if bool((item.get("State") or {}).get("Running"))}
            if running_before:
                self.update_phase("application", "unhealthy")
                raise RecoveryError("application service started during the recovery window")
            for service, item in zip(self.services, before):
                started_at = (item.get("State") or {}).get("StartedAt")
                if started_at != service.get("started_at"):
                    self.update_phase("application", "unhealthy")
                    raise RecoveryError("application container changed during the recovery window")
            if self.event_watcher and self.event_watcher.unexpected(set()):
                self.update_phase("application", "unhealthy")
                raise RecoveryError("application container started during the recovery window")
            remaining_start_ids = start_ids
            _write_startup_gate(self.project, "open", self.record, self.state_root)
            if remaining_start_ids:
                self._docker("start", *remaining_start_ids)
            deadline = time.monotonic() + 120
            states = []
            while True:
                observed = self._docker("inspect", *start_ids)
                states = json.loads(observed.stdout)
                if not all(bool((item.get("State") or {}).get("Running")) for item in states):
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(1)
                    continue
                health = [(item.get("State") or {}).get("Health", {}).get("Status") for item in states]
                if any(value == "unhealthy" for value in health):
                    break
                if any(value == "starting" for value in health):
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(1)
                    continue
                break
            if not all(bool((item.get("State") or {}).get("Running")) for item in states):
                self.update_phase("application", "unhealthy")
                raise RecoveryError("application start was not observed")
            health = [(item.get("State") or {}).get("Health", {}).get("Status") for item in states]
            if any(value == "unhealthy" for value in health) or any(value == "starting" for value in health):
                self.update_phase("application", "unhealthy")
                raise RecoveryError("application health check failed or timed out")
            if self.event_watcher and self.event_watcher.unexpected(set(start_ids)):
                self.update_phase("application", "unhealthy")
                raise RecoveryError("unexpected application container start was observed")
            if check_backend_http:
                backend = next((service for service in start_services if service["service"] == "backend"), None)
                if backend is None:
                    raise RecoveryError("required backend health target was not started")
                deadline = time.monotonic() + 120
                while True:
                    command = [self.docker, "exec", backend["id"], "curl", "--max-time", "10", "-sS", "-o", "/dev/null",
                               "-w", "%{http_code}", "-H", "X-Forwarded-Proto: https", "http://localhost:8000/api/healthz"]
                    options = {"capture_output": True, "text": True, "check": False}
                    if self.runner is subprocess.run:
                        options.update(pass_fds=(self.fd,), timeout=15)
                    response = self.runner(command, **options)
                    if response.returncode == 0 and response.stdout.strip() == "200":
                        break
                    if time.monotonic() >= deadline:
                        raise RecoveryError("required backend health check failed")
                    time.sleep(2)
            for service in start_services:
                if service["restart_policy"] != "no":
                    self._docker("update", f"--restart={service['restart_policy']}", service["id"])
            final = "healthy" if health and all(value == "healthy" for value in health) else "running"
        elif self.expected_final == "running" and self.auto_restart and not start_ids:
            self.update_phase("application", "stopped")
            raise RecoveryError("no previously running service is eligible to restart")
        else:
            all_ids = [service["id"] for service in self.services]
            states = json.loads(self._docker("inspect", *all_ids).stdout) if all_ids else []
            if any(bool((item.get("State") or {}).get("Running")) for item in states):
                self.update_phase("application", "running")
                raise RecoveryError("application was expected to remain stopped")
            if self.event_watcher and self.event_watcher.unexpected(set()):
                self.update_phase("application", "unhealthy")
                raise RecoveryError("application container started during the recovery window")
            final = "stopped"
        self.update_phase("artifacts", artifacts)
        self.update_phase("migration", migration)
        self.update_phase("checks", checks)
        self.update_phase("application", final)
        if self.expected_final == "running" and final not in {"running", "healthy"}:
            raise RecoveryError("expected running application was not observed")
        self.record["satisfied_repair_ids"] = sorted({item[0].stem for item in repairs_to_complete})
        self.record.update(status="completed", updated_at=int(time.time()))
        _write_json(self.path, self.record)
        _write_startup_gate(self.project, "open", self.record, self.state_root)
        for target, item in repairs_to_complete:
            item.update(status="completed", satisfied_by=self.operation_id)
            _write_json(target, item)
        return self.record

    def _release(self):
        if self.fd is not None:
            # Children retain the same flock open-file description after the
            # parent dies; do not explicitly unlock it while any child lives.
            os.close(self.fd)
            self.fd = None

    def __exit__(self, exc_type, exc, tb):
        if self._nested:
            self._entered = False
            return False
        if self.already_completed:
            if getattr(_local, "transaction", None) is self:
                del _local.transaction
            self._release()
            self._entered = False
            return False
        if self.record is not None and self.record.get("status") != "completed":
            with contextlib.suppress(Exception):
                _write_startup_gate(self.project, "blocked", self.record, self.state_root)
            self.record["status"] = "recovery-required"
            self.record["updated_at"] = int(time.time())
            if exc is not None:
                # Record only a bounded class code; diagnostics may contain secrets.
                self.record["primary_error"] = type(exc).__name__[:64]
            with contextlib.suppress(Exception):
                self._record_failed_repair()
            try:
                ids = [s["id"] for s in self.services]
                if ids:
                    self._docker("stop", *ids)
                    states = json.loads(self._docker("inspect", *ids).stdout)
                    self.record["application"] = (
                        "stopped" if all(not bool((item.get("State") or {}).get("Running")) for item in states)
                        else "unknown"
                    )
                    self.record["observed_application"] = self.record["application"]
            except Exception:
                self.record["cleanup_error"] = "application-stop-or-observation-failed"
                self.record["application"] = "unknown"
                self.record["observed_application"] = "unknown"
            with contextlib.suppress(Exception):
                _write_json(self.path, self.record)
        if self._previous_handler is not None:
            with contextlib.suppress(ValueError):
                signal.signal(signal.SIGTERM, self._previous_handler)
        if self.event_watcher is not None:
            with contextlib.suppress(Exception):
                self.event_watcher.close()
            self.event_watcher = None
        if getattr(_local, "transaction", None) is self:
            del _local.transaction
        self._release()
        self._entered = False
        return False


def recovery_result(record: dict[str, Any]) -> str:
    """Render bounded, machine-readable evidence without diagnostics or secrets."""
    keys = ("project", "app", "operation_id", "source", "status", "database", "artifacts",
            "migration", "checks", "application", "observed_application",
            "expected_application", "requested_application", "origin_run_id", "origin_attempt", "repair_ids",
            "satisfied_repair_ids", "supersedes_operation_id", "primary_error",
            "cleanup_error", "updated_at")
    safe = {key: record.get(key) for key in keys}
    error_classes = {"RecoveryError", "RuntimeError", "SystemExit", "KeyboardInterrupt", "OSError",
                     "ValueError", "TimeoutError", "BrokenPipeError", "PermissionError",
                     "FileNotFoundError", "CalledProcessError", "TimeoutExpired", "InterruptedError"}
    if safe.get("primary_error") not in error_classes:
        safe["primary_error"] = "unknown_error" if safe.get("primary_error") else None
    if safe.get("cleanup_error") not in {None, "application-stop-or-observation-failed"}:
        safe["cleanup_error"] = "cleanup_failed"
    return json.dumps({"recovery_result": safe}, separators=(",", ":"), ensure_ascii=True)
