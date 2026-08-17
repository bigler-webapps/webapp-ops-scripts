"""Tests for verify_backup.py's snapshot selection logic (INF-17).

All pure decision logic — no live restic, no B2, per the WO's "narrow tests"
requirement. Run: pytest test_verify_backup.py
"""

import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import backup  # noqa: E402
import verify_backup as vb  # noqa: E402


def test_snapshot_ordering_across_mixed_offsets_uses_parsed_instant():
    # research-prod's snapshot string sorts above main-prod's even though it is
    # the earlier instant (05:18:29+02:00 == 03:18:29 UTC < 03:19:27 UTC). This
    # is the exact shape of the run-31990601185 defect (INF-17 defect 2).
    snapshots = [
        {"id": "research0002", "time": "2026-08-17T05:18:29.000000000+02:00"},
        {"id": "mainprod0001", "time": "2026-08-17T03:19:27.000000000+00:00"},
    ]
    chosen = vb.choose_snapshot(snapshots)
    assert chosen["id"] == "mainprod0001"


def test_explicit_snapshot_id_wins_over_a_newer_one():
    snapshots = [
        {"id": "own_snapshot_id", "time": "2026-08-17T03:19:27+00:00"},
        {"id": "newer_foreign_id", "time": "2026-08-17T05:18:29+02:00"},
    ]
    chosen = vb.choose_snapshot(snapshots, explicit_id="own_snapshot_id")
    assert chosen["id"] == "own_snapshot_id"


def test_explicit_snapshot_id_supports_short_prefix():
    snapshots = [{"id": "abcdef1234567890", "time": "2026-08-17T03:19:27+00:00"}]
    chosen = vb.choose_snapshot(snapshots, explicit_id="abcdef12")
    assert chosen["id"] == "abcdef1234567890"


def test_explicit_snapshot_id_ambiguous_prefix_raises():
    snapshots = [
        {"id": "abcdef1111111111", "time": "2026-08-17T03:19:27+00:00"},
        {"id": "abcdef2222222222", "time": "2026-08-17T03:19:27+00:00"},
    ]
    with pytest.raises(RuntimeError):
        vb.choose_snapshot(snapshots, explicit_id="abcdef")


def test_explicit_snapshot_id_not_found_raises():
    with pytest.raises(RuntimeError):
        vb.choose_snapshot([{"id": "other", "time": "2026-08-17T03:19:27+00:00"}], explicit_id="missing")


def test_empty_snapshot_list_raises_without_silently_passing():
    with pytest.raises(RuntimeError):
        vb.choose_snapshot([], explicit_id=None)


def test_filename_timestamp_from_non_utc_host_falls_inside_freshness_window():
    # A dump named by the *fixed* backup.py on a +02:00 host must land inside
    # the freshness window computed around its own (UTC) snapshot time.
    local_plus2 = datetime(2026, 8, 17, 5, 18, 37, tzinfo=timezone(timedelta(hours=2)))
    filename = f"main-prod_appdb_{backup.utc_timestamp_str(local_plus2)}.sql.gz"

    ts = vb.parse_timestamp_from_filename(filename)
    assert ts is not None

    snap_time = datetime(2026, 8, 17, 3, 18, 40, tzinfo=timezone.utc)
    min_time = snap_time - timedelta(hours=vb.MAX_SNAPSHOT_WINDOW_HOURS)
    max_time = snap_time + timedelta(minutes=5)
    assert min_time <= ts <= max_time


def test_legacy_colon_timestamp_format_still_parses():
    ts = vb.parse_timestamp_from_filename("app_db_2025-11-20T22:06:07Z.sql.gz")
    assert ts == datetime(2025, 11, 20, 22, 6, 7, tzinfo=timezone.utc)


def test_read_snapshot_id_file_missing_returns_none(tmp_path):
    assert vb.read_snapshot_id_file(tmp_path / "does-not-exist") is None


def test_read_snapshot_id_file_reads_and_strips(tmp_path):
    f = tmp_path / "snap_id"
    f.write_text("abc123\n", encoding="utf-8")
    assert vb.read_snapshot_id_file(f) == "abc123"


def test_no_matching_dumps_still_exits_non_zero(monkeypatch):
    # Guard against regression while the selection logic was rewritten: an
    # empty candidate set must never silently pass.
    monkeypatch.setenv("RESTIC_REPO_B2", "s3:example/repo")
    monkeypatch.setenv("RESTIC_PASSWORD", "x")
    monkeypatch.setattr(vb, "REPO_URL", "s3:example/repo")
    monkeypatch.setattr(vb, "REPO_PWD", "x")
    monkeypatch.setattr(vb.os, "uname", lambda: types.SimpleNamespace(nodename="test-host"), raising=False)
    monkeypatch.setattr(vb, "read_snapshot_id_file", lambda path: "snap123")
    monkeypatch.setattr(
        vb, "get_target_snapshot",
        lambda env, host, explicit_id: ("snap123", "2026-08-17T03:19:27+00:00"),
    )
    monkeypatch.setattr(vb, "list_sql_gz_files", lambda env, snapshot_id: [])

    assert vb.main() == 1
