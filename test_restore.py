"""Restore call-site, schema integrity and event-observation contracts.

Real exclusion, restart inhibition and interruption tests live in
test_stack_recovery.py; the receiver integration also uses this actual module.
"""
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import restore

REAL_WATCHER = restore.ContainerStartWatcher


def completed_process(stdout=""):
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def test_import_database_is_db_only_and_records_completion(tmp_path, monkeypatch):
    class Tx:
        def __init__(self):
            self.events = []

        def mark_database_started(self):
            self.events.append("database:importing")

        def update_phase(self, phase, status):
            self.events.append(f"{phase}:{status}")

        def exec_psql(self, container_id, *args, **kwargs):
            self.events.append(("psql", container_id, *args))
            class Child:
                def wait(self):
                    return 0
            return Child()

    tx = Tx()
    monkeypatch.setattr(restore, "reset_database", lambda *args, **kwargs: tx.events.append("reset"))
    monkeypatch.setattr(restore, "stream_restore_from_local_file", lambda *args, **kwargs: tx.events.append("stream"))
    restore.import_database(tx, tmp_path / "dump.sql.gz", "trusted-db", "user", "database")

    assert tx.events == ["database:importing", "reset", "stream", "database:completed"]
    assert not any("migration" in str(event) or "artifact" in str(event) for event in tx.events)


def test_run_migrations_uses_transaction_one_shot_adapter(monkeypatch):
    class Tx:
        def __init__(self):
            self.calls = []

        def update_phase(self, phase, status, error_code=None):
            self.calls.append((phase, status))

        def run_migrations(self, *, stdout=None):
            self.calls.append(("one-shot", stdout))
            class Child:
                def wait(self):
                    return 0
            return Child()

    tx = Tx()
    monkeypatch.setattr(restore, "find_compose_container", lambda *args: pytest.fail("must not find a running backend"))
    restore.run_migrations("demo_staging", tx=tx)
    assert tx.calls == [
        ("migration", "running"),
        ("one-shot", subprocess.DEVNULL),
        ("migration", "completed"),
    ]


def test_assert_schema_healthy_passes_when_every_table_has_a_primary_key(monkeypatch):
    calls = []

    def fake_run_cmd(cmd, env=None, check=True):
        calls.append(cmd)
        return completed_process()

    monkeypatch.setattr(restore, "run_cmd", fake_run_cmd)

    restore.assert_schema_healthy("db-container", "app-user", "app-db")

    assert calls[0][:8] == [
        "docker", "exec", "db-container", "psql", "-U", "app-user", "-d", "app-db",
    ]
    assert "PRIMARY KEY" in calls[0][-1]


def test_assert_schema_healthy_exits_non_zero_and_names_tables_missing_primary_keys(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        restore,
        "run_cmd",
        lambda cmd, env=None, check=True: completed_process(
            "public.status_slurmqueuestate\npublic.status_workerstate\n"
        ),
    )

    with pytest.raises(SystemExit) as exc_info:
        restore.assert_schema_healthy("db-container", "app-user", "app-db")

    assert exc_info.value.code == 1
    output = capsys.readouterr().out
    assert "2 public base table(s) have no primary key" in output
    assert "public.status_slurmqueuestate" in output
    assert "public.status_workerstate" in output


def test_inf_94_event_watcher_streams_the_project_events_and_reads_them_back(monkeypatch):
    started = {}

    class FakePopen:
        returncode = None

        def __init__(self, cmd, stdout=None, stderr=None, text=None):
            started["cmd"] = cmd
            self.stdout = stdout
            stdout.write("abc123 resolved_staging-backend-run-1\n")
            stdout.flush()
            self.terminated = False

        def poll(self):
            return None if not self.terminated else -15

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return -15

    monkeypatch.setattr(restore.subprocess, "Popen", FakePopen)
    watcher = REAL_WATCHER("resolved_staging")
    try:
        assert watcher.started() == [("abc123", "resolved_staging-backend-run-1")]
    finally:
        watcher.close()

    cmd = started["cmd"]
    assert cmd[:2] == ["docker", "events"]
    assert "label=com.docker.compose.project=resolved_staging" in cmd
    assert "event=create" in cmd and "event=start" in cmd
    assert "{{.Actor.ID}} {{.Actor.Attributes.name}}" in cmd


def test_inf_94_event_watcher_that_cannot_run_is_reported_and_not_fatal(monkeypatch, capsys):
    def no_docker(*args, **kwargs):
        raise OSError("docker not found")

    monkeypatch.setattr(restore.subprocess, "Popen", no_docker)
    watcher = REAL_WATCHER("resolved_staging")
    try:
        assert watcher.started() == []
    finally:
        watcher.close()

    assert "checking container state only" in capsys.readouterr().out


def test_inf_94_event_watcher_that_dies_early_is_reported_once(monkeypatch, capsys):
    class DeadPopen:
        returncode = 1

        def __init__(self, *args, **kwargs):
            pass

        def poll(self):
            return 1

    monkeypatch.setattr(restore.subprocess, "Popen", DeadPopen)
    watcher = REAL_WATCHER("resolved_staging")
    try:
        assert watcher.started() == [] and watcher.started() == []
    finally:
        watcher.close()

    assert capsys.readouterr().out.count("exited early") == 1


def test_inf_33_s1_import_only_rejects_an_unknown_target_env(tmp_path, monkeypatch, capsys):
    """INF-33 S1: the compose project is f"{app}_{env}" and the separator is a
    plain underscore, so free-text env makes the boundary ambiguous --
    `--app foo --target-env bar_staging` names the same project as
    `--app foo_bar --target-env staging`. Constraining env removes the second
    spelling. It does NOT stop a caller naming another app directly; that needs
    an app allowlist this script cannot source."""
    dump_file = tmp_path / "dump.sql.gz"
    dump_file.write_bytes(b"placeholder")
    touched = []

    monkeypatch.setattr(
        restore, "find_db_container_id", lambda projects: touched.append(projects) or ("", "")
    )

    with pytest.raises(SystemExit) as exc_info:
        restore.mode_import_only(
            SimpleNamespace(app="example", target_env="bar_staging", dump_file=str(dump_file))
        )

    assert exc_info.value.code == 1
    assert touched == [], "no container lookup may happen for an unknown env"
    assert "--target-env must be one of" in capsys.readouterr().out


def test_inf_33_s1_known_envs_are_still_accepted(tmp_path, monkeypatch):
    """The guard must not break the real callers: every ENV_ALIASES key passes."""
    for env_name in sorted(restore.ENV_ALIASES):
        restore.require_known_env(env_name, "--target-env")


def test_inf_33_s1_dump_only_rejects_an_unknown_source_env(tmp_path, monkeypatch, capsys):
    """INF-33 R1 (delta review): the mode_dump_only call site had no coverage at
    all -- no test invoked that mode. A wrong attribute, a wrong flag string, or
    the guard drifting below the output_dir side effects would have gone
    unnoticed. This pins the call site, not just the helper."""
    monkeypatch.setenv("RESTIC_REPOSITORY", "b2:example")
    monkeypatch.setenv("RESTIC_PASSWORD", "irrelevant")
    touched = []
    monkeypatch.setattr(
        restore, "find_db_container_id", lambda projects: touched.append(projects) or ("", "")
    )

    with pytest.raises(SystemExit) as exc_info:
        restore.mode_dump_only(
            SimpleNamespace(
                app="example",
                source_env="bar_production",
                output_dir=str(tmp_path / "out"),
                snapshot="latest",
            )
        )

    assert exc_info.value.code == 1
    assert touched == [], "no container lookup may happen for an unknown env"
    assert not (tmp_path / "out").exists(), "no output directory may be created either"
    assert "--source-env must be one of" in capsys.readouterr().out


def test_inf_33_s3_env_alias_values_are_all_keys_too():
    """INF-33 S3 (delta review): require_known_env tests membership against the
    KEYS of ENV_ALIASES. That is deliberately the stricter side -- widening it to
    the values would let an underscore-bearing alias back in and reopen the
    collision. It is correct only while every alias value is also a key, and
    nothing else enforces that. Adding an alias value without its own key would
    silently start rejecting a legitimate environment; this names the reason."""
    for key, aliases in restore.ENV_ALIASES.items():
        for alias in aliases:
            assert alias in restore.ENV_ALIASES, (
                f"'{alias}' is an alias of '{key}' but not a key of ENV_ALIASES, "
                f"so require_known_env would reject it"
            )

@pytest.fixture
def restore_flow(tmp_path, monkeypatch):
    events = []
    instances = []
    active = {"value": False}

    class Transaction:
        def __init__(self, project, app, operation, source, **options):
            self.options = options
            self.already_completed = False
            self.record = {"project": project, "app": app, "source": source,
                           "operation_id": operation, "status": "recovery-required"}
            instances.append(self)

        def __enter__(self):
            active["value"] = True
            events.append("guard-enter")
            return self

        def __exit__(self, exc_type, exc, tb):
            events.append("guard-exit")
            active["value"] = False
            self.error = exc

        def mark_database_started(self):
            assert active["value"]
            events.append("database:importing")

        def update_phase(self, phase, status, **kwargs):
            assert active["value"]
            self.record[phase] = status
            events.append(f"{phase}:{status}")

        def complete(self, **kwargs):
            assert active["value"]
            assert events[-1] == "schema-check"
            events.append("finalize")
            self.record.update(status="completed", application="stopped")

    def step(name):
        def run(*args, **kwargs):
            assert active["value"], f"{name} escaped recovery exclusion"
            events.append(name)
        return run

    monkeypatch.setattr(restore.stack_recovery, "RecoveryTransaction", Transaction)
    monkeypatch.setattr(restore, "find_db_container_id", lambda projects: ("db-id", "example_staging"))
    monkeypatch.setattr(restore, "get_db_credentials", lambda cid: ("app-user", "app-db"))
    monkeypatch.setattr(restore, "reset_database", step("reset"))
    monkeypatch.setattr(restore, "stream_restore_from_local_file", step("import"))
    monkeypatch.setattr(restore, "stream_restore_into_psql", step("snapshot-import"))
    monkeypatch.setattr(restore, "run_migrations", step("one-shot-migration"))
    monkeypatch.setattr(restore, "assert_schema_healthy", step("schema-check"))
    dump = tmp_path / "synthetic.sql.gz"
    dump.write_bytes(b"synthetic input; the import is stubbed")
    args = SimpleNamespace(app="example", target_env="staging", dump_file=str(dump),
                           operation_id="run-123", recovery_source="main-prod",
                           origin_run_id="123", origin_attempt="2", auto_restart=False)
    return events, instances, active, args


def test_import_holds_transaction_through_migration_checks_and_finalization(restore_flow):
    events, instances, active, args = restore_flow
    assert restore.mode_import_only(args) == 0
    assert events == ["guard-enter", "database:importing", "reset", "import",
                      "database:completed", "one-shot-migration", "schema-check",
                      "finalize", "guard-exit"]
    assert not active["value"]
    tx = instances[0]
    assert tx.record["project"] == "example_staging"
    assert tx.record["app"] == "example"
    assert tx.record["source"] == "main-prod"
    assert tx.options["expected_final"] == "stopped"
    assert tx.options["auto_restart"] is False
    assert tx.options["origin_run_id"] == "123"
    assert tx.options["origin_attempt"] == "2"


@pytest.mark.parametrize("failed_step,expected_prefix", [
    ("stream_restore_from_local_file", ["guard-enter", "database:importing", "reset"]),
    ("run_migrations", ["guard-enter", "database:importing", "reset", "import", "database:completed"]),
    ("assert_schema_healthy", ["guard-enter", "database:importing", "reset", "import",
                               "database:completed", "one-shot-migration"]),
])
def test_import_failure_never_finalizes_or_starts(restore_flow, monkeypatch, failed_step, expected_prefix):
    events, instances, active, args = restore_flow
    def fail(*a, **kw):
        raise RuntimeError("synthetic-primary-failure")
    monkeypatch.setattr(restore, failed_step, fail)
    with pytest.raises(SystemExit) as failure:
        restore.mode_import_only(args)
    assert failure.value.code == 1
    assert events == expected_prefix + ["guard-exit"]
    assert "finalize" not in events
    assert instances[0].record["status"] == "recovery-required"
    assert not active["value"]


def test_import_interruption_unwinds_transaction_without_success(restore_flow, monkeypatch):
    events, instances, active, args = restore_flow
    def interrupt(*a, **kw):
        raise KeyboardInterrupt()
    monkeypatch.setattr(restore, "stream_restore_from_local_file", interrupt)
    with pytest.raises(KeyboardInterrupt):
        restore.mode_import_only(args)
    assert isinstance(instances[0].error, KeyboardInterrupt)
    assert events[-1] == "guard-exit" and "finalize" not in events
    assert not active["value"]


def test_import_completed_operation_replay_does_no_destructive_work(restore_flow, monkeypatch):
    events, instances, active, args = restore_flow
    original = restore.stack_recovery.RecoveryTransaction
    def replay(*a, **kw):
        tx = original(*a, **kw)
        tx.already_completed = True
        tx.record["status"] = "completed"
        return tx
    monkeypatch.setattr(restore.stack_recovery, "RecoveryTransaction", replay)
    assert restore.mode_import_only(args) == 0
    assert events == ["guard-enter", "guard-exit"]


def test_explicit_restart_does_not_enable_starting_previously_stopped_services(restore_flow):
    events, instances, active, args = restore_flow
    args.auto_restart = True
    restore.mode_import_only(args)
    assert instances[0].options["auto_restart"] is True
    assert instances[0].options["restart_left_stopped"] is False


def test_same_host_import_holds_guard_through_snapshot_migrations_and_checks(restore_flow, monkeypatch):
    events, instances, active, args = restore_flow
    monkeypatch.setenv("RESTIC_REPOSITORY", "synthetic-repository")
    monkeypatch.setenv("RESTIC_PASSWORD", "synthetic-test-value")
    monkeypatch.setattr(restore, "resolve_snapshot", lambda *a: ("snapshot", "2026-10-27T00:00:00Z"))
    monkeypatch.setattr(restore, "list_sql_gz", lambda *a: ["selected.sql.gz"])
    monkeypatch.setattr(restore, "choose_dump_for_db", lambda *a: "selected.sql.gz")
    args.snapshot = "latest"
    args.auto_restart = True
    assert restore.mode_in_place(args) == 0
    assert events == ["guard-enter", "database:importing", "reset", "snapshot-import",
                      "database:completed", "one-shot-migration", "schema-check",
                      "finalize", "guard-exit"]
    assert instances[0].record["source"] == "main-prod"


def test_missing_host_contract_refuses_before_database_reset(tmp_path, monkeypatch):
    events = []
    dump = tmp_path / "dump.sql.gz"
    dump.write_bytes(b"synthetic")
    monkeypatch.setattr(restore, "find_db_container_id", lambda projects: ("db-id", "example_staging"))
    monkeypatch.setattr(restore, "get_db_credentials", lambda cid: ("user", "db"))
    class Refused:
        def __init__(self, *a, **kw):
            pass
        def __enter__(self):
            raise restore.stack_recovery.RecoveryError("contract-unavailable")
        def __exit__(self, *a):
            pass
    monkeypatch.setattr(restore.stack_recovery, "RecoveryTransaction", Refused)
    monkeypatch.setattr(restore, "reset_database", lambda *a, **kw: events.append("reset"))
    with pytest.raises(SystemExit):
        restore.mode_import_only(SimpleNamespace(app="example", target_env="staging", dump_file=str(dump)))
    assert events == []


def test_wm_inf_112_staging_import_waits_45_minutes_for_the_recovery_lock(restore_flow):
    events, instances, active, args = restore_flow
    assert restore.mode_import_only(args) == 0
    assert instances[0].options["lock_timeout"] == 2700.0


@pytest.mark.parametrize("target_env", ["production", "prod"])
def test_wm_inf_112_other_import_targets_keep_the_default_lock_wait(restore_flow, target_env):
    events, instances, active, args = restore_flow
    args.target_env = target_env
    assert restore.mode_import_only(args) == 0
    assert "lock_timeout" not in instances[0].options


def test_wm_inf_112_same_host_restore_keeps_the_default_lock_wait(restore_flow, monkeypatch):
    events, instances, active, args = restore_flow
    monkeypatch.setenv("RESTIC_REPOSITORY", "synthetic-repository")
    monkeypatch.setenv("RESTIC_PASSWORD", "synthetic-test-value")
    monkeypatch.setattr(restore, "resolve_snapshot", lambda *a: ("snapshot", "2026-10-27T00:00:00Z"))
    monkeypatch.setattr(restore, "list_sql_gz", lambda *a: ["selected.sql.gz"])
    monkeypatch.setattr(restore, "choose_dump_for_db", lambda *a: "selected.sql.gz")
    args.snapshot = "latest"
    assert restore.mode_in_place(args) == 0
    assert "lock_timeout" not in instances[0].options


def _failing_enter(message):
    class Refused:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            raise restore.stack_recovery.RecoveryError(message)

        def __exit__(self, *a):
            pass
    return Refused


@pytest.fixture
def import_args(tmp_path, monkeypatch):
    dump = tmp_path / "dump.sql.gz"
    dump.write_bytes(b"synthetic")
    monkeypatch.setattr(restore, "find_db_container_id", lambda projects: ("db-id", "example_staging"))
    monkeypatch.setattr(restore, "get_db_credentials", lambda cid: ("user", "db"))
    return SimpleNamespace(app="example", target_env="staging", dump_file=str(dump))


def test_wm_inf_112_lock_timeout_is_named_in_the_import_failure_line(import_args, monkeypatch, capsys):
    monkeypatch.setattr(restore.stack_recovery, "RecoveryTransaction", _failing_enter(restore.LOCK_TIMEOUT_MESSAGE))
    with pytest.raises(SystemExit) as failure:
        restore.mode_import_only(import_args)
    assert failure.value.code == 1
    assert "[RESTORE] ERROR: Import failed (RecoveryError: recovery_lock_timeout)." in capsys.readouterr().out


def test_wm_inf_112_other_recovery_error_logs_its_fixed_message(import_args, monkeypatch, capsys):
    monkeypatch.setattr(restore.stack_recovery, "RecoveryTransaction",
                        _failing_enter("no labelled containers found for recovery project"))
    with pytest.raises(SystemExit):
        restore.mode_import_only(import_args)
    out = capsys.readouterr().out
    assert "Import failed (RecoveryError: no labelled containers found for recovery project)." in out
    assert "recovery_lock_timeout" not in out


def test_wm_inf_112_failure_label_is_single_line_bounded_and_class_only_for_other_errors():
    error = restore.stack_recovery.RecoveryError
    assert restore.failure_label(RuntimeError("secret detail")) == "RuntimeError"
    assert restore.failure_label(error("")) == "RecoveryError"
    label = restore.failure_label(error("first line\nsecond   line " + "x" * 400))
    assert "\n" not in label and "first line second line" in label
    assert len(label) <= len("RecoveryError: ") + 200
    hostile = restore.failure_label(error("unsafe file: \x1b[31mred\x1b[0m‮ name"))
    assert all(" " <= ch <= "~" for ch in hostile) and "\x1b" not in hostile


def test_wm_inf_112_stack_recovery_defaults_and_message_are_unchanged():
    """The named code depends on stack_recovery's exact message; the other callers rely on its 300 s default."""
    import inspect
    source = inspect.getsource(restore.stack_recovery)
    assert f'RecoveryError("{restore.LOCK_TIMEOUT_MESSAGE}")' in source
    for target in (restore.stack_recovery.RecoveryTransaction.__init__, restore.stack_recovery.writer_guard,
                   restore.stack_recovery._raw_host_lock):
        assert inspect.signature(target).parameters["lock_timeout"].default == 300.0
