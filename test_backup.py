"""Tests for backup.py's UTC timestamp generation (INF-17).

Run: pytest test_backup.py
"""

import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import backup  # noqa: E402


def test_utc_timestamp_str_converts_non_utc_local_time():
    # Wall-clock 05:18:37 on a host whose local zone is +02:00 is 03:18:37 UTC.
    # Against the pre-fix code (naive `datetime.now()`, no conversion) this
    # would have produced "051837" instead of "031837".
    local_plus2 = datetime(2026, 8, 17, 5, 18, 37, tzinfo=timezone(timedelta(hours=2)))
    assert backup.utc_timestamp_str(local_plus2) == "2026-08-17T031837Z"


def test_utc_timestamp_str_defaults_to_real_utc_now():
    result = backup.utc_timestamp_str()
    parsed = datetime.strptime(result, "%Y-%m-%dT%H%M%SZ")
    now_utc_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    assert abs((now_utc_naive - parsed).total_seconds()) < 5


# --- B2 snapshot marker survives a retention failure (INF-24) -----------------

def test_run_restic_preserves_snapshot_id_when_retention_fails(monkeypatch):
    # Reproduces the observed collision verbatim: backup succeeds, forget
    # --prune then hits another target's live lock on the shared B2 repo.
    # run_restic must still report the snapshot id it already created --
    # losing it here is what let the marker go stale on a retention failure.
    calls = []

    def fake_run_cmd(cmd, env=None, cwd=None, show_on_success=False):
        calls.append(cmd)
        if "backup" in cmd:
            return True, "snapshot abc12345 saved", ""
        if "forget" in cmd:
            return False, "", (
                "unable to create lock in backend: repository is already "
                "locked by PID 699822 on research-prod by deploy"
            )
        return True, "", ""  # snapshots (repo-ready check)

    monkeypatch.setattr(backup, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(backup, "RESTIC_PASSWORD", "x")
    monkeypatch.setattr(backup.os, "uname", lambda: types.SimpleNamespace(nodename="test-host"), raising=False)

    ok, snap_id = backup.run_restic("s3:example/repo", "B2")

    assert ok is False
    assert snap_id == "abc12345"


def test_record_b2_snapshot_writes_marker_and_prints_greppable_line(tmp_path, monkeypatch, capsys):
    marker = tmp_path / ".last_b2_snapshot_id"
    monkeypatch.setattr(backup, "LAST_B2_SNAPSHOT_ID_FILE", marker)

    backup.record_b2_snapshot("abc12345")

    assert marker.read_text(encoding="utf-8") == "abc12345\n"
    assert "B2_SNAPSHOT_CREATED=abc12345" in capsys.readouterr().out


def test_record_b2_snapshot_prints_marker_even_if_the_file_write_fails(tmp_path, monkeypatch, capsys):
    # A workflow-level consumer (INF-24) reads the stdout marker, not the
    # file -- the file write is a best-effort convenience for verify_backup.py
    # and must not be a single point of failure for the workflow-level signal.
    unwritable_dir = tmp_path / "no-such-dir"  # parent doesn't exist -> OSError
    monkeypatch.setattr(backup, "LAST_B2_SNAPSHOT_ID_FILE", unwritable_dir / "marker")

    backup.record_b2_snapshot("abc12345")

    assert "B2_SNAPSHOT_CREATED=abc12345" in capsys.readouterr().out


def test_record_b2_snapshot_no_marker_line_when_no_snapshot_was_created(capsys):
    backup.record_b2_snapshot(None)

    out = capsys.readouterr().out
    assert "B2_SNAPSHOT_CREATED=" not in out
    assert "WARN: no B2 snapshot id captured" in out


def test_main_records_marker_before_the_retention_failure_exit(monkeypatch, tmp_path):
    # End-to-end ordering guard: main() must call record_b2_snapshot() before
    # the ok_local/ok_b2 fail() gate. Against the pre-INF-24 ordering, fail()'s
    # sys.exit(1) would abort main() before this ever ran -- the marker would
    # never update on a retention-only failure. This test simulates that exact
    # shape end to end (no live restic/B2/docker).
    marker = tmp_path / ".last_b2_snapshot_id"
    monkeypatch.setattr(backup, "LAST_B2_SNAPSHOT_ID_FILE", marker)
    monkeypatch.setattr(backup, "determine_docker_command", lambda: ["docker"])
    monkeypatch.setattr(backup, "ensure_restic_available", lambda: None)
    monkeypatch.setattr(backup, "generate_paths_file", lambda: None)
    monkeypatch.setattr(backup, "perform_db_dumps", lambda: None)

    def fake_run_restic(repo_url, repo_name, *, unlock_stale=False):
        if repo_name == "Local":
            return True, "local0001"
        return False, "b2snap001"  # B2 backup succeeded, retention failed

    monkeypatch.setattr(backup, "run_restic", fake_run_restic)

    with pytest.raises(SystemExit):
        backup.main()

    assert marker.read_text(encoding="utf-8") == "b2snap001\n"
