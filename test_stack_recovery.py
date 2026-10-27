import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import stack_recovery as recovery


def make_test_state(tmp_path):
    root = tmp_path / "state"
    (root / "apps").mkdir(parents=True)
    (root / "repairs").mkdir()
    (root / "auto_restarts").mkdir()
    os.chmod(root, 0o770)
    os.chmod(root / "auto_restarts", 0o770)
    os.chmod(root / "apps", 0o770)
    os.chmod(root / "repairs", 0o770)
    lock = tmp_path / "deploy-server.lock"
    lock.touch()
    os.chmod(lock, 0o660)
    return root, lock


def docker_item(cid, service, running=True, policy="unless-stopped"):
    return {
        "Id": cid + "0" * 40,
        "Config": {"Labels": {
            "com.docker.compose.project": "demo_staging",
            "com.docker.compose.service": service,
        }},
        "State": {"Running": running, "StartedAt": "2026-10-27T10:00:00Z"},
        "HostConfig": {"RestartPolicy": {"Name": policy}},
    }


class FakeDocker:
    def __init__(self, *, backend_running=True, backend_policy="unless-stopped"):
        self.items = {
            "db1111111111": docker_item("db1111111111", "db"),
            "be1111111111": docker_item("be1111111111", "backend", backend_running, backend_policy),
            "f01111111111": docker_item("f01111111111", "init-volumes", False, "no"),
        }
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        tail = cmd[1:]
        if tail[0] == "exec" and "curl" in tail:
            return subprocess.CompletedProcess(cmd, 0, "200", "")
        if "label=com.docker.compose.oneoff=True" in tail:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if tail[:4] == ["ps", "-a", "-q", "-f"]:
            return subprocess.CompletedProcess(cmd, 0, "db1111111111\nbe1111111111\nf01111111111\n", "")
        if tail and tail[0] == "inspect":
            requested = tail[1:]
            items = [self.items[cid] for cid in requested]
            return subprocess.CompletedProcess(cmd, 0, json.dumps(items), "")
        if tail and tail[0] == "update":
            policy = tail[1].split("=", 1)[1]
            for cid in tail[2:]:
                self.items[cid]["HostConfig"]["RestartPolicy"]["Name"] = policy
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if tail and tail[0] in {"stop", "start"}:
            running = tail[0] == "start"
            for cid in tail[1:]:
                self.items[cid]["State"]["Running"] = running
                if running:
                    self.items[cid]["State"]["StartedAt"] = "2026-10-27T10:01:00Z"
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")


@pytest.mark.skipif(recovery.fcntl is None, reason="Linux flock is required")
def test_incomplete_database_barrier_rejects_ordinary_writer_and_same_op_cannot_reimport(tmp_path):
    root, lock = make_test_state(tmp_path)
    docker = FakeDocker()
    tx = recovery.RecoveryTransaction(
        "demo_staging", "demo", "run-1-1", "refresh", lock_path=lock,
        state_root=root, runner=docker,
    )
    with pytest.raises(ValueError):
        with tx:
            tx.record_evidence(origin_run_id="run-1", origin_attempt="1")
            tx.mark_database_started()
            raise ValueError("synthetic import failure")
    record = recovery.read_status("demo_staging", root)
    assert record["status"] == "recovery-required"
    assert record["database"] == "importing"
    assert record["application"] == "stopped"
    with pytest.raises(recovery.RecoveryError, match="recovery"):
        recovery.admit_writer("demo_staging", root)
    assert recovery.pending_repairs("demo_staging", "demo", "refresh", root)[0]["attempt"] == "1"

    retry = recovery.RecoveryTransaction(
        "demo_staging", "demo", "run-1-1", "refresh", lock_path=lock,
        state_root=root, runner=docker,
    )
    with retry as resumed:
        assert resumed.reconcile_only
        with pytest.raises(recovery.RecoveryError, match="already recorded"):
            resumed.mark_database_started()


@pytest.mark.skipif(recovery.fcntl is None, reason="Linux flock is required")
def test_deliberately_stopped_success_and_duplicate_finalization(tmp_path):
    root, lock = make_test_state(tmp_path)
    docker = FakeDocker()
    tx = recovery.RecoveryTransaction(
        "demo_staging", "demo", "run-2-1", "refresh", expected_final="stopped",
        auto_restart=False, lock_path=lock, state_root=root, runner=docker,
    )
    with tx as active:
        active.record_evidence(origin_run_id="run-2", origin_attempt="1")
        active.mark_database_started()
        active.update_phase("database", "completed")
        active.update_phase("migration", "completed")
        active.update_phase("checks", "completed")
        result = active.complete(artifacts="not_applicable", application="stopped")
        assert result["status"] == "completed"
        assert result["application"] == "stopped"
    assert docker.items["be1111111111"]["State"]["Running"] is False
    assert docker.items["f01111111111"]["State"]["Running"] is False

    repeat = recovery.RecoveryTransaction(
        "demo_staging", "demo", "run-2-1", "refresh", expected_final="stopped",
        auto_restart=False, lock_path=lock, state_root=root, runner=docker,
    )
    before = list(docker.calls)
    with repeat as replay:
        assert replay.already_completed
        assert replay.record["status"] == "completed"
    assert docker.calls == before, "a replay must not repeat Docker phases"


@pytest.mark.skipif(recovery.fcntl is None, reason="Linux flock is required")
def test_successful_retry_can_start_only_previously_stopped_long_running_services(tmp_path):
    root, lock = make_test_state(tmp_path)
    docker = FakeDocker(backend_running=False)
    tx = recovery.RecoveryTransaction(
        "demo_staging", "demo", "run-2-2", "refresh", expected_final="running",
        auto_restart=True, restart_left_stopped=True, lock_path=lock,
        state_root=root, runner=docker,
    )
    with tx as active:
        active.mark_database_started()
        active.update_phase("database", "completed")
        active.update_phase("migration", "completed")
        active.update_phase("checks", "completed")
        result = active.complete(artifacts="completed", application="running")
    assert result["application"] == "running"
    assert docker.items["be1111111111"]["State"]["Running"] is True
    assert docker.items["f01111111111"]["State"]["Running"] is False
    assert ["docker", "start", "be1111111111"] in docker.calls
    assert not any(call[:3] == ["docker", "start", "f01111111111"] for call in docker.calls)


@pytest.mark.skipif(recovery.fcntl is None, reason="Linux flock is required")
def test_retry_wrong_source_and_wrong_app_cannot_clear_barrier(tmp_path):
    root, lock = make_test_state(tmp_path)
    docker = FakeDocker()
    with pytest.raises(RuntimeError):
        with recovery.RecoveryTransaction("demo_staging", "demo", "op-3", "source-a",
                expected_final="stopped", auto_restart=False, lock_path=lock,
                state_root=root, runner=docker) as tx:
            tx.mark_database_started()
            raise RuntimeError("interrupted")
    for app, source in (("demo", "source-b"), ("other", "source-a")):
        with pytest.raises(recovery.RecoveryError, match="different or incomplete"):
            with recovery.RecoveryTransaction("demo_staging", app, "op-3", source,
                    expected_final="stopped", auto_restart=False, lock_path=lock,
                    state_root=root, runner=docker):
                pass


@pytest.mark.skipif(recovery.fcntl is None, reason="Linux flock is required")
def test_new_same_source_refresh_reconciles_failed_drill_and_satisfies_its_repair(tmp_path):
    root, lock = make_test_state(tmp_path)
    docker = FakeDocker()
    with pytest.raises(RuntimeError):
        with recovery.RecoveryTransaction(
            "demo_staging", "demo", "drill-5-1", "refresh", expected_final="stopped",
            auto_restart=False, lock_path=lock, state_root=root, runner=docker,
            origin_run_id="drill-5", origin_attempt="1",
        ) as drill:
            drill.mark_database_started()
            drill.update_phase("database", "failed")
            raise RuntimeError("drill import failed")

    repair = recovery.pending_repairs("demo_staging", "demo", "refresh", root)[0]
    refresh = recovery.RecoveryTransaction(
        "demo_staging", "demo", "refresh-6-1", "refresh", expected_final="stopped",
        auto_restart=False, lock_path=lock, state_root=root, runner=docker,
        origin_run_id="refresh-6", origin_attempt="1",
    )
    with refresh as tx:
        assert tx.record["supersedes_operation_id"] == "drill-5-1"
        assert tx.record["repair_ids"] == [repair["repair_id"]]
        tx.mark_database_started()
        tx.update_phase("database", "completed")
        tx.update_phase("migration", "completed")
        tx.update_phase("checks", "completed")
        tx.complete(artifacts="completed", application="stopped")

    assert recovery.pending_repairs("demo_staging", "demo", "refresh", root) == []
    saved = json.loads((root / "repairs" / f"{repair['repair_id']}.json").read_text(encoding="utf-8"))
    assert saved["status"] == "completed"
    assert saved["satisfied_by"] == "refresh-6-1"


@pytest.mark.skipif(recovery.fcntl is None, reason="Linux flock is required")
def test_failed_required_artifact_cannot_complete_or_repair(tmp_path):
    root, lock = make_test_state(tmp_path)
    docker = FakeDocker()
    repair = recovery.record_repair("run4-1-demo", "demo_staging", "demo", "refresh", "run4", root, attempt="1")
    tx = recovery.RecoveryTransaction("demo_staging", "demo", "run4-1", "refresh",
            expected_final="stopped", auto_restart=False, lock_path=lock,
            state_root=root, runner=docker)
    with tx as active:
        active.mark_database_started()
        active.update_phase("database", "completed")
        with pytest.raises(recovery.RecoveryError, match="required artifacts"):
            active.complete(artifacts="partial", application="stopped", satisfy_repair_ids=[repair["repair_id"]])
    assert recovery.pending_repairs("demo_staging", "demo", "refresh", root)[0]["status"] == "pending"


@pytest.mark.skipif(recovery.fcntl is None, reason="Linux flock is required")
@pytest.mark.parametrize("termination", [signal.SIGKILL, signal.SIGTERM])
def test_inherited_flock_survives_killed_python_owner_until_child_exits(tmp_path, termination):
    root, lock = make_test_state(tmp_path)
    owner_script = r'''
import os, subprocess, sys, signal
sys.path.insert(0, sys.argv[2])
import stack_recovery as recovery
with recovery.writer_guard("demo_staging", lock_path=sys.argv[1], state_root=sys.argv[3]) as guard:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], pass_fds=(guard.fd,))
    print(child.pid, flush=True)
    os.kill(os.getpid(), int(sys.argv[4]))
'''
    owner = subprocess.Popen([sys.executable, "-c", owner_script, str(lock), str(Path(recovery.__file__).parent), str(root), str(int(termination))], stdout=subprocess.PIPE, text=True)
    child_pid = int(owner.stdout.readline().strip())
    owner.wait(timeout=5)
    assert owner.returncode == -termination
    probe_script = "import fcntl,os,sys; fd=os.open(sys.argv[1],os.O_RDWR);\ntry: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB); print('free')\nexcept BlockingIOError: print('held')"
    held = subprocess.run([sys.executable, "-c", probe_script, str(lock)], capture_output=True, text=True, check=True)
    assert held.stdout.strip() == "held"
    os.kill(child_pid, signal.SIGKILL)
    time.sleep(0.1)
    free = subprocess.run([sys.executable, "-c", probe_script, str(lock)], capture_output=True, text=True, check=True)
    assert free.stdout.strip() == "free"


def test_recovery_output_is_bounded_and_excludes_diagnostics():
    output = recovery.recovery_result({
        "schema": 1, "project": "demo_staging", "app": "demo", "operation_id": "op",
        "source": "refresh", "status": "recovery-required", "database": "failed",
        "artifacts": "unknown", "migration": "not_attempted", "checks": "not_attempted",
        "application": "stopped", "expected_application": "stopped", "updated_at": 1,
        "primary_error": "secret-token-value", "diagnostics": "large raw output",
    })
    assert len(output) < 1000
    assert "secret-token-value" not in output
    assert "large raw output" not in output
    assert json.loads(output)["recovery_result"]["database"] == "failed"


@pytest.mark.skipif(recovery.fcntl is None, reason="Linux flock is required")
def test_host_provision_reservation_blocks_writers_until_matching_successful_finish(tmp_path):
    root, lock = make_test_state(tmp_path)
    reservation = recovery.begin_provisioning_reservation(
        "ansible-run-42", lock_path=lock, state_root=root,
    )
    assert reservation["status"] == "running"
    assert recovery.read_host_reservation(root)["run_id"] == "ansible-run-42"
    with pytest.raises(recovery.RecoveryError, match="provisioning reservation"):
        with recovery.writer_guard("demo_staging", lock_path=lock, state_root=root):
            pass
    with pytest.raises(recovery.RecoveryError, match="identity does not match"):
        recovery.finish_provisioning_reservation(
            "other-run", success=True, lock_path=lock, state_root=root,
        )
    failed = recovery.finish_provisioning_reservation(
        "ansible-run-42", success=False, lock_path=lock, state_root=root,
    )
    assert failed["status"] == "failed"
    with pytest.raises(recovery.RecoveryError, match="provisioning reservation"):
        with recovery.writer_guard(None, lock_path=lock, state_root=root):
            pass
    with pytest.raises(recovery.RecoveryError, match="already active"):
        recovery.begin_provisioning_reservation("new-ansible-run", lock_path=lock, state_root=root)
    resumed = recovery.begin_provisioning_reservation(
        "ansible-run-42", lock_path=lock, state_root=root,
    )
    assert resumed["previous_status"] == "failed"
    assert resumed["resume_count"] == 1
    recovery.finish_provisioning_reservation(
        "ansible-run-42", success=True, lock_path=lock, state_root=root,
    )
    assert recovery.read_host_reservation(root) is None
    with recovery.writer_guard("demo_staging", lock_path=lock, state_root=root):
        pass


@pytest.mark.skipif(recovery.fcntl is None, reason="Linux flock is required")
def test_auto_restart_marker_blocks_ordinary_writers_until_actual_restart_observation(tmp_path):
    root, lock = make_test_state(tmp_path)
    with recovery.automatic_restart_guard("demo_staging", lock_path=lock, state_root=root):
        recovery.begin_auto_restart("demo_staging", "a" * 64, "2026-10-27T10:00:00Z", root)
        with pytest.raises(recovery.RecoveryError, match="automatic restart completion"):
            recovery.admit_writer("demo_staging", root)
        with pytest.raises(recovery.RecoveryError, match="completion was not observed"):
            recovery.finish_auto_restart(
                "demo_staging", "a" * 64, "2026-10-27T10:00:00Z",
                running=True, health="healthy", state_root=root,
            )
        recovery.finish_auto_restart(
            "demo_staging", "a" * 64, "2026-10-27T10:01:00Z",
            running=True, health="healthy", state_root=root,
        )
    with recovery.writer_guard("demo_staging", lock_path=lock, state_root=root):
        pass


@pytest.mark.skipif(recovery.fcntl is None, reason="Linux flock is required")
def test_preserve_final_state_does_not_start_a_deliberately_stopped_app(tmp_path):
    root, lock = make_test_state(tmp_path)
    docker = FakeDocker(backend_running=False)
    tx = recovery.RecoveryTransaction(
        "demo_staging", "demo", "preserve-stopped", "refresh", expected_final="preserve",
        auto_restart=True, lock_path=lock, state_root=root, runner=docker,
    )
    with tx as active:
        assert active.expected_final == "stopped"
        active.mark_database_started()
        active.update_phase("database", "completed")
        active.update_phase("migration", "completed")
        active.update_phase("checks", "completed")
        result = active.complete(artifacts="completed", application="stopped")
    assert result["requested_application"] == "preserve"
    assert result["expected_application"] == "stopped"
    assert result["application"] == "stopped"
    assert docker.items["be1111111111"]["State"]["Running"] is False
    assert not any(call[:2] == ["docker", "start"] for call in docker.calls)


@pytest.mark.skipif(recovery.fcntl is None, reason="Linux flock is required")
def test_preserve_retry_uses_original_running_snapshot_after_interruption(tmp_path):
    root, lock = make_test_state(tmp_path)
    first_docker = FakeDocker(backend_running=True)
    with pytest.raises(RuntimeError):
        with recovery.RecoveryTransaction(
            "demo_staging", "demo", "preserve-1", "refresh", expected_final="preserve",
            auto_restart=True, lock_path=lock, state_root=root, runner=first_docker,
        ) as first:
            first.mark_database_started()
            raise RuntimeError("interrupted before the required artifact phase")

    retry_docker = FakeDocker(backend_running=False)
    with recovery.RecoveryTransaction(
        "demo_staging", "demo", "preserve-2", "refresh", expected_final="preserve",
        auto_restart=True, lock_path=lock, state_root=root, runner=retry_docker,
    ) as retry:
        assert retry.expected_final == "running"
        retry.mark_database_started()
        retry.update_phase("database", "completed")
        retry.update_phase("artifacts", "completed")
        retry.update_phase("migration", "completed")
        retry.update_phase("checks", "completed")
        result = retry.complete(artifacts="completed", application="running")
    assert result["application"] == "running"
    assert retry_docker.items["be1111111111"]["State"]["Running"] is True


@pytest.mark.skipif(recovery.fcntl is None, reason="Linux flock is required")
def test_controlled_start_for_health_checks_stays_inside_recovery_transaction(tmp_path):
    root, lock = make_test_state(tmp_path)
    docker = FakeDocker(backend_running=True)
    tx = recovery.RecoveryTransaction(
        "demo_staging", "demo", "check-start", "pg-upgrade", expected_final="running",
        auto_restart=True, lock_path=lock, state_root=root, runner=docker,
    )
    with tx as active:
        active.mark_database_started()
        active.update_phase("database", "completed")
        active.update_phase("migration", "completed")
        active.update_phase("checks", "completed")
        assert docker.items["be1111111111"]["State"]["Running"] is False
        result = active.complete(artifacts="not_applicable", application="running", check_backend_http=True)
    assert result["status"] == "completed"
    assert result["application"] == "running"
    assert any("curl" in command for command in docker.calls)


@pytest.mark.skipif(recovery.fcntl is None, reason="Linux flock is required")
def test_actual_guard_times_out_and_reenters_without_deadlock(tmp_path):
    root, lock = make_test_state(tmp_path)
    with recovery.writer_guard("demo_staging", lock_path=lock, state_root=root) as guard:
        with recovery.writer_guard("demo_staging", lock_path=lock, state_root=root) as nested:
            assert nested.fd == guard.fd
        code = '''import sys
sys.path.insert(0, sys.argv[1])
import stack_recovery as r
try:
    with r.writer_guard("demo_staging", lock_path=sys.argv[2], state_root=sys.argv[3], lock_timeout=0.05):
        raise AssertionError("contending writer entered")
except r.RecoveryError:
    print("refused")
'''
        result = subprocess.run([sys.executable, "-c", code, str(Path(recovery.__file__).parent), str(lock), str(root)], capture_output=True, text=True, timeout=3, check=True)
        assert result.stdout.strip() == "refused"
    with recovery.writer_guard("demo_staging", lock_path=lock, state_root=root):
        pass


@pytest.mark.skipif(os.name != "posix", reason="Linux startup shell is required")
def test_installed_startup_gate_blocks_repeated_container_starts_until_open(tmp_path, monkeypatch):
    root, _ = make_test_state(tmp_path)
    project = "demo_staging"
    gate_dir = root / "gates" / project
    monkeypatch.setattr(recovery, "GATE_TARGET", str(gate_dir))
    output = tmp_path / "executed"
    item = docker_item("be1111111111", "backend")
    item.update(Path=sys.executable, Args=["-c", "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text(sys.argv[2])", str(output), "$literal; value"])
    override = recovery.gated_compose_override(project, [item], state_root=root)
    entrypoint = override["services"]["backend"]["entrypoint"]
    assert "$$gate_state" in entrypoint[2], "Compose must preserve shell variables"
    # Compose reduces $$ to $ before Docker receives the command.
    installed = [value.replace("$$", "$") for value in entrypoint]
    origin = {"project": project, "app": "demo", "source": "refresh", "operation_id": "failed-1", "origin_run_id": "run-1", "origin_attempt": "1"}
    recovery._write_startup_gate(project, "blocked", origin, root)
    for _ in range(2):
        process = subprocess.Popen(installed, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(0.1)
            assert process.poll() is None
            assert not output.exists()
        finally:
            process.terminate()
            process.wait(timeout=3)
    # A queued/cancelled newer run cannot erase the original durable obligation.
    for _delivery in ("cancelled", "rejected", "overflowed"):
        pending = recovery.pending_repairs(project, "demo", "refresh", root)
        assert pending and pending[0]["run_id"] == "run-1"
        with pytest.raises(recovery.RecoveryError):
            recovery.admit_writer(project, root)
    recovery._write_startup_gate(project, "open", origin, root)
    subprocess.run(installed, timeout=3, check=True)
    assert output.read_text() == "$literal; value"


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0, reason="Run explicitly as Linux root for synthetic UID permission test")
def test_provisioned_uid_can_lock_read_only_and_write_restart_evidence():
    import tempfile
    with tempfile.TemporaryDirectory(prefix="wmops11-uid-") as temporary:
        base = Path(temporary)
        base.chmod(0o755)
        root, lock = make_test_state(base)
        for directory in (root, root / "apps", root / "repairs", root / "auto_restarts"):
            os.chown(directory, 0, 65534)
            directory.chmod(0o2770)
        os.chown(lock, 0, 65534)
        identity = base / "identity.json"
        identity.write_text(json.dumps({"uid": 65534, "gid": 65534}))
        identity.chmod(0o644)
        code = '''import os,sys,pathlib
sys.path.insert(0, sys.argv[1])
import stack_recovery as r
r.LOCK_PATH=pathlib.Path(sys.argv[2]); r.IDENTITY_PATH=pathlib.Path(sys.argv[4]); r.STATE_GROUP="missing-wmops11-group"
os.setgroups([]); os.setgid(int(sys.argv[5])); os.setuid(int(sys.argv[5]))
with r.automatic_restart_guard("demo_staging", lock_path=r.LOCK_PATH, state_root=sys.argv[3]):
    r.begin_auto_restart("demo_staging", "a"*64, "before", sys.argv[3])
print("admitted")
'''
        argv = [sys.executable, "-c", code, str(Path(recovery.__file__).parent), str(lock), str(root), str(identity)]
        allowed = subprocess.run([*argv, "65534"], capture_output=True, text=True, timeout=5)
        assert allowed.returncode == 0, allowed.stderr
        assert allowed.stdout.strip() == "admitted"
        evidence = root / "auto_restarts" / "inflight.json"
        assert evidence.stat().st_uid == 65534
        denied = subprocess.run([*argv, "65533"], capture_output=True, text=True, timeout=5)
        assert denied.returncode != 0
        assert "admitted" not in denied.stdout


@pytest.mark.skipif(recovery.fcntl is None, reason="Linux flock is required")
def test_maintenance_reservation_survives_killed_requester_when_sudo_closes_fds(tmp_path):
    root, lock = make_test_state(tmp_path)
    code = '''import os,sys,subprocess,signal
sys.path.insert(0,sys.argv[1])
import stack_recovery as r
with r.maintenance_guard("apt", lock_path=sys.argv[2], state_root=sys.argv[3]):
    # Model sudo closing descriptors before its fixed privileged child starts.
    child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"], close_fds=True)
    print(child.pid,flush=True)
    os.kill(os.getpid(),signal.SIGKILL)
'''
    owner = subprocess.Popen([sys.executable, "-c", code, str(Path(recovery.__file__).parent), str(lock), str(root)], stdout=subprocess.PIPE, text=True)
    child_pid = int(owner.stdout.readline())
    try:
        owner.wait(timeout=3)
        assert owner.returncode == -signal.SIGKILL
        assert recovery.read_host_reservation(root)["kind"] == "maintenance-apt"
        with pytest.raises(recovery.RecoveryError, match="reservation"):
            with recovery.writer_guard(None, lock_path=lock, state_root=root, lock_timeout=0.1):
                pytest.fail("ordinary writer entered during orphaned privileged maintenance")
    finally:
        os.kill(child_pid, signal.SIGKILL)
    # Child exit alone is not proof that interrupted package state is repaired.
    with pytest.raises(recovery.RecoveryError, match="reservation"):
        with recovery.maintenance_guard("apt", lock_path=lock, state_root=root):
            pass


@pytest.mark.skipif(recovery.fcntl is None, reason="Linux flock is required")
def test_maintenance_success_and_reboot_reconciliation_require_actual_completion(tmp_path, monkeypatch):
    root, lock = make_test_state(tmp_path)
    with recovery.maintenance_guard("apt", lock_path=lock, state_root=root):
        assert recovery.read_host_reservation(root)["status"] == "running"
    assert recovery.read_host_reservation(root) is None
    monkeypatch.setattr(recovery, "_boot_id", lambda: "old-boot")
    with pytest.raises(RuntimeError):
        with recovery.maintenance_guard("reboot", lock_path=lock, state_root=root):
            raise RuntimeError("requester interrupted")
    recovery.reconcile_completed_maintenance_reboot(lock_path=lock, state_root=root)
    assert recovery.read_host_reservation(root)
    monkeypatch.setattr(recovery, "_boot_id", lambda: "new-boot")
    recovery.reconcile_completed_maintenance_reboot(lock_path=lock, state_root=root)
    assert recovery.read_host_reservation(root) is None
    with recovery.writer_guard(None, lock_path=lock, state_root=root):
        pass
