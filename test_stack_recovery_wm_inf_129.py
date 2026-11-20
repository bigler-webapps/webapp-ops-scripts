"""WM-INF-129: cutover journal, rollback to the last verified state, failed-refresh recording and public-root verification."""
import json
import subprocess
import sys

import pytest

import stack_recovery as recovery
from test_stack_recovery import FakeDocker, make_test_state

pytestmark = pytest.mark.skipif(recovery.fcntl is None, reason="Linux flock is required")

PROJECT, APP, SOURCE = "demo_staging", "demo", "refresh"


def transaction(root, lock, docker, operation="run-1-1", **kwargs):
    return recovery.RecoveryTransaction(PROJECT, APP, operation, SOURCE, lock_path=lock, state_root=root,
                                        runner=docker, origin_run_id="run-1", origin_attempt="1", **kwargs)


def start_everything(docker):
    """What a failed `complete` leaves behind: services started again (new StartedAt), policies restored."""
    for cid in ("be1111111111",):
        docker.items[cid]["State"]["Running"] = True
        docker.items[cid]["State"]["StartedAt"] = "2026-10-27T10:09:00Z"
        docker.items[cid]["HostConfig"]["RestartPolicy"]["Name"] = "unless-stopped"


def test_a_failed_cutover_is_rolled_back_and_the_application_serves_the_last_verified_state(tmp_path):
    root, lock = make_test_state(tmp_path)
    docker = FakeDocker()
    undone = []
    with transaction(root, lock, docker) as tx:
        assert docker.items["be1111111111"]["State"]["Running"] is False  # writer exclusion during the cutover
        tx.begin_cutover()
        tx.update_phase("database", "importing")
        result = tx.rollback_cutover(lambda: undone.append("undo"), failed_phase="import", check_backend_http=True)
    assert undone == ["undo"]
    assert result["status"] == "completed" and result["refresh"] == "failed" and result["rollback"] == "completed"
    assert result["cutover"]["state"] == "rolled_back" and result["cutover"]["failed_phase"] == "import"
    assert result["database"] == "not_attempted", "no phase of the failed refresh is claimed"
    assert docker.items["be1111111111"]["State"]["Running"] is True
    assert docker.items["be1111111111"]["HostConfig"]["RestartPolicy"]["Name"] == "unless-stopped"
    assert recovery.read_startup_gate(PROJECT, root)["gate"] == "open"
    recovery.admit_writer(PROJECT, root)  # ordinary writers are admitted again
    pending = recovery.pending_repairs(PROJECT, APP, SOURCE, root)
    assert pending and pending[0]["status"] == "pending", "the failed refresh stays a repair until a later refresh succeeds"
    shown = json.loads(recovery.recovery_result(result))["recovery_result"]
    assert shown["refresh"] == "failed" and shown["rollback"] == "completed" and shown["failed_phase"] == "import"


def test_a_health_failure_after_the_services_were_started_stops_them_again_before_the_restore(tmp_path):
    root, lock = make_test_state(tmp_path)
    docker = FakeDocker()
    order = []
    with transaction(root, lock, docker) as tx:
        tx.begin_cutover()
        start_everything(docker)

        def undo():
            order.append(docker.items["be1111111111"]["State"]["Running"])

        tx.rollback_cutover(undo, failed_phase="health")
    assert order == [False], "the application is quiesced before the data is restored"
    assert docker.items["be1111111111"]["State"]["Running"] is True
    assert recovery.read_status(PROJECT, root)["rollback"] == "completed"


def test_a_deliberately_stopped_application_stays_stopped_after_a_rollback(tmp_path):
    root, lock = make_test_state(tmp_path)
    docker = FakeDocker(backend_running=False)
    with transaction(root, lock, docker, expected_final="preserve") as tx:
        tx.begin_cutover()
        result = tx.rollback_cutover(lambda: None, failed_phase="import")
    assert result["expected_application"] == "stopped"
    assert docker.items["be1111111111"]["State"]["Running"] is False
    assert result["application"] == "stopped"


def test_an_undo_that_cannot_complete_keeps_the_barrier_and_the_next_entry_repeats_it(tmp_path):
    root, lock = make_test_state(tmp_path)
    docker = FakeDocker()

    def broken():
        raise RuntimeError("the previous copy is missing")

    with pytest.raises(RuntimeError):
        with transaction(root, lock, docker) as tx:
            tx.begin_cutover()
            tx.update_cutover(artifacts_journal="/var/tmp/dbrecv-spool-demo-run-1-1", database_previous="demo__prev_1")
            tx.rollback_cutover(broken, failed_phase="import")
    record = recovery.read_status(PROJECT, root)
    assert record["status"] == "recovery-required" and record["cutover"]["state"] == "rollback_failed"
    assert recovery.read_startup_gate(PROJECT, root)["gate"] == "blocked"
    with pytest.raises(recovery.RecoveryError):
        recovery.admit_writer(PROJECT, root)
    shown = json.loads(recovery.recovery_result(record))["recovery_result"]
    assert shown["cutover_state"] == "rollback_failed"

    # another operation arrives: the unresolved cutover is detected, the journal is carried, the services' former state is kept
    undone = []
    with transaction(root, lock, docker, operation="run-2-1") as again:
        assert again.pending_rollback is True
        assert again.record["cutover"]["artifacts_journal"] == "/var/tmp/dbrecv-spool-demo-run-1-1"
        assert [s["running"] for s in again.services if s["service"] == "backend"] == [True]
        again.rollback_cutover(lambda: undone.append(1))
    assert undone == [1]
    assert docker.items["be1111111111"]["State"]["Running"] is True
    recovery.admit_writer(PROJECT, root)


def test_repeating_the_recovery_after_it_completed_is_a_no_op(tmp_path):
    root, lock = make_test_state(tmp_path)
    docker = FakeDocker()
    with transaction(root, lock, docker) as tx:
        tx.begin_cutover()
        tx.rollback_cutover(lambda: None, failed_phase="artifacts")
    with transaction(root, lock, docker, operation="run-2-1") as again:
        assert again.pending_rollback is False
        assert again.already_completed is False
        assert docker.items["be1111111111"]["State"]["Running"] is False  # a fresh transaction starts its own cutover window


def test_a_retry_of_the_same_operation_after_a_rolled_back_failure_is_not_already_completed(tmp_path):
    root, lock = make_test_state(tmp_path)
    docker = FakeDocker()
    with transaction(root, lock, docker) as tx:
        tx.begin_cutover()
        tx.rollback_cutover(lambda: None, failed_phase="import")
    with transaction(root, lock, docker) as retry:
        assert retry.already_completed is False, "a failed refresh must never answer the retry as a success"


def test_complete_after_rollback_is_refused_unless_the_state_was_restored(tmp_path):
    root, lock = make_test_state(tmp_path)
    docker = FakeDocker()
    with pytest.raises(recovery.RecoveryError, match="last verified state"):
        with transaction(root, lock, docker) as tx:
            tx.begin_cutover()
            tx.complete(after_rollback=True)


def test_a_verified_cutover_is_recorded_and_its_unfinished_cleanup_is_visible_to_the_next_entry(tmp_path):
    root, lock = make_test_state(tmp_path)
    docker = FakeDocker()
    with transaction(root, lock, docker) as tx:
        tx.begin_cutover()
        tx.update_cutover(database_previous="demo__prev_abc", database_swap="imported")
        tx.update_phase("database", "completed")
        tx.update_phase("migration", "completed")
        tx.update_phase("checks", "completed")
        record = tx.complete(artifacts="not_applicable")
    assert record["refresh"] == "succeeded" and record["rollback"] == "not_needed"
    assert record["cutover"]["state"] == "verified"
    with transaction(root, lock, docker, operation="run-2-1") as next_entry:
        assert next_entry.leftover_cutover and next_entry.leftover_cutover["database_previous"] == "demo__prev_abc"
        assert next_entry.pending_rollback is False


def test_cutover_fields_are_bounded_and_unknown_fields_are_refused(tmp_path):
    root, lock = make_test_state(tmp_path)
    with transaction(root, lock, FakeDocker()) as tx:
        with pytest.raises(recovery.RecoveryError, match="unsupported"):
            tx.update_cutover(password="x")
        with pytest.raises(recovery.RecoveryError, match="invalid"):
            tx.update_cutover(artifacts_journal="x" * 300)
        with pytest.raises(recovery.RecoveryError, match="invalid"):
            tx.update_cutover(database_previous=["not", "scalar"])
        tx.rollback_cutover(lambda: None)


def test_public_root_hosts_come_only_from_the_applications_own_router_labels():
    items = [
        {"Config": {"Labels": {"traefik.http.routers.demo.rule": "Host(`staging.example.org`) || Host(`www.example.org`)",
                               "traefik.http.routers.demo.tls": "true", "other": "Host(`ignored.example.org`)"}}},
        {"Config": {"Labels": {}}},
        {"Config": {"Labels": {"traefik.http.routers.bad name.rule": "Host(`bad.example.org`)"}}},
    ]
    assert recovery.public_root_hosts(items) == ["staging.example.org", "www.example.org"]


def _serve(status, seen):
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get("X-Forwarded-Proto")))
            self.send_response(status)
            self.send_header("Location", "https://elsewhere.invalid/")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.mark.parametrize("status", [200, 302, 503])
def test_the_backend_probe_runs_with_the_interpreter_and_reports_the_status_without_curl(status):
    seen = []
    server = _serve(status, seen)
    try:
        source = recovery.BACKEND_HEALTH_PROBE.replace("'localhost', 8000", f"'127.0.0.1', {server.server_address[1]}")
        result = subprocess.run([sys.executable, "-I", "-c", source], capture_output=True, text=True, timeout=30, check=False)
    finally:
        server.shutdown()
        server.server_close()
    assert result.returncode == 0 and result.stdout == str(status)
    assert seen == [("/api/healthz", "https")]


def test_the_backend_probe_fails_when_nothing_listens():
    source = recovery.BACKEND_HEALTH_PROBE.replace("8000", "1")
    result = subprocess.run([sys.executable, "-I", "-c", source], capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode != 0 and result.stdout == ""


class CurlDocker(FakeDocker):
    def __init__(self, status="200", **kwargs):
        super().__init__(**kwargs)
        self.status = status
        self.items["be1111111111"]["Config"]["Labels"]["traefik.http.routers.demo.rule"] = "Host(`staging.example.org`)"

    def __call__(self, cmd, **kwargs):
        if cmd[0] == "curl":
            self.calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0 if self.status != "000" else 7, self.status, "")
        return super().__call__(cmd, **kwargs)


def _complete_ready(tx):
    tx.update_phase("database", "completed")
    tx.update_phase("migration", "completed")
    tx.update_phase("checks", "completed")


def test_the_public_root_is_checked_through_the_applications_own_router_host(tmp_path):
    root, lock = make_test_state(tmp_path)
    docker = CurlDocker("200")
    with transaction(root, lock, docker) as tx:
        _complete_ready(tx)
        tx.complete(artifacts="not_applicable", check_backend_http=True, check_public_root=True)
    curl = [call for call in docker.calls if call[0] == "curl"]
    assert curl and "staging.example.org:443:127.0.0.1" in curl[0] and curl[0][-1] == "https://staging.example.org/"


def test_a_failing_public_root_fails_the_cutover_so_it_can_be_rolled_back(tmp_path, monkeypatch):
    root, lock = make_test_state(tmp_path)
    docker = CurlDocker("503")
    ticks = iter(range(0, 10_000, 40))
    monkeypatch.setattr(recovery.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(recovery.time, "sleep", lambda _s: None)
    with transaction(root, lock, docker) as tx:
        tx.begin_cutover()
        _complete_ready(tx)
        with pytest.raises(recovery.RecoveryError, match="public root"):
            tx.complete(artifacts="not_applicable", check_backend_http=True, check_public_root=True)
        # the failed health check is rolled back like any other cutover failure
        result = tx.rollback_cutover(lambda: None, failed_phase="health")
    assert result["refresh"] == "failed" and result["rollback"] == "completed"
    assert docker.items["be1111111111"]["State"]["Running"] is True
    assert docker.items["be1111111111"]["HostConfig"]["RestartPolicy"]["Name"] == "unless-stopped"


def test_a_missing_router_host_cannot_pass_a_required_public_root_check(tmp_path):
    root, lock = make_test_state(tmp_path)
    docker = FakeDocker()  # no traefik label
    with transaction(root, lock, docker) as tx:
        _complete_ready(tx)
        with pytest.raises(recovery.RecoveryError, match="public root host"):
            tx.complete(artifacts="not_applicable", check_backend_http=True, check_public_root=True)


def test_the_neighbouring_application_is_untouched_by_a_rollback(tmp_path):
    root, lock = make_test_state(tmp_path)
    docker = FakeDocker()
    other = recovery.RecoveryTransaction("other_staging", "other", "run-9-1", SOURCE, lock_path=lock, state_root=root,
                                         runner=FakeDocker())
    with other:
        pass
    before = recovery.read_status("other_staging", root)
    with transaction(root, lock, docker) as tx:
        tx.begin_cutover()
        tx.rollback_cutover(lambda: None, failed_phase="import")
    assert recovery.read_status("other_staging", root) == before


def test_a_restored_state_whose_verification_failed_stays_unresolved_and_is_repeated_by_the_next_entry(tmp_path):
    root, lock = make_test_state(tmp_path)
    docker = FakeDocker()
    real_complete = recovery.RecoveryTransaction.complete

    def failing_complete(self, **kwargs):
        if kwargs.get("after_rollback"):
            raise recovery.RecoveryError("restored application did not become healthy")
        return real_complete(self, **kwargs)

    recovery.RecoveryTransaction.complete = failing_complete
    try:
        with pytest.raises(recovery.RecoveryError, match="did not become healthy"):
            with transaction(root, lock, docker) as tx:
                tx.begin_cutover()
                tx.rollback_cutover(lambda: None, failed_phase="import")
    finally:
        recovery.RecoveryTransaction.complete = real_complete
    record = recovery.read_status(PROJECT, root)
    assert record["status"] == "recovery-required" and record["cutover"]["state"] == "restored"
    assert recovery.read_startup_gate(PROJECT, root)["gate"] == "blocked"
    with transaction(root, lock, docker, operation="run-2-1") as again:
        assert again.pending_rollback is True, "no new cutover starts on an unverified restore"
        result = again.rollback_cutover(lambda: None)
    assert result["cutover"]["state"] == "rolled_back" and result["refresh"] == "failed"
    recovery.admit_writer(PROJECT, root)


def test_a_404_from_the_router_is_not_a_served_public_root(tmp_path, monkeypatch):
    root, lock = make_test_state(tmp_path)
    docker = CurlDocker("404")
    ticks = iter(range(0, 10_000, 40))
    monkeypatch.setattr(recovery.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(recovery.time, "sleep", lambda _s: None)
    with transaction(root, lock, docker) as tx:
        _complete_ready(tx)
        with pytest.raises(recovery.RecoveryError, match="public root"):
            tx.complete(artifacts="not_applicable", check_backend_http=True, check_public_root=True)
        tx.rollback_cutover(lambda: None)


@pytest.mark.parametrize("status", ["200", "204", "301", "302", "401", "403"])
def test_served_statuses_pass_the_public_root_check(tmp_path, status):
    root, lock = make_test_state(tmp_path)
    docker = CurlDocker(status)
    with transaction(root, lock, docker) as tx:
        _complete_ready(tx)
        tx.complete(artifacts="not_applicable", check_backend_http=True, check_public_root=True)
