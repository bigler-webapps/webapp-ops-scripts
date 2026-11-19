"""OPS-BAK-1: volume selection, gap detection, summary and size gate.

Run: pytest test_volume_backup.py
"""

import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import backup  # noqa: E402
import generate_paths as gp  # noqa: E402
import volume_selection as vs  # noqa: E402

CFG = vs.load_config()

ALL_NAMES = [
    "photogallery_production_media_volume",
    "kira_production_media_volume",
    "cockpit_production_open_webui_data",
    "hram_production_excel_volume",
    "hram_production_assistant_cli_sessions",
    "innoservice_prod_media_volume",
    # must NOT be selected
    "hram_production_pipeline_artifacts",
    "hram_production_calibration_outputs",
    "photogallery_production_postgres_data",
    "cockpit_production_hf_model_cache",
    "cockpit_production_secrets_volume",
    "photogallery_staging_media_volume",
    "kira_stage_media_volume",
    "a" * 64,
    "unmounted_production_media_volume",
]
MOUNTED = set(ALL_NAMES) - {"unmounted_production_media_volume"}
EXPECTED = sorted(ALL_NAMES[:6])


def test_selection_keeps_only_declared_mounted_production_user_data():
    assert vs.select_volumes(MOUNTED, CFG) == EXPECTED


def test_unmounted_declared_volume_is_not_selected():
    assert "unmounted_production_media_volume" not in vs.select_volumes(MOUNTED, CFG)


def test_running_container_volumes_parses_docker_inspect(monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        out = "id1 id2\n" if cmd[-1] == "-q" else json.dumps([
            {"Mounts": [{"Type": "volume", "Name": "a_production_media_volume"}, {"Type": "bind", "Name": "x"}]},
            {"Mounts": None},
        ])
        return types.SimpleNamespace(stdout=out)

    monkeypatch.setattr(vs.subprocess, "run", fake_run)
    assert vs.running_container_volumes(["docker"]) == {"a_production_media_volume"}


def test_volume_sizes_parses_docker_system_df(monkeypatch):
    out = json.dumps({"Volumes": [{"Name": "a", "Size": "11.63GB"}, {"Name": "b", "Size": "0B"}]})
    monkeypatch.setattr(vs.subprocess, "run", lambda cmd, **kw: types.SimpleNamespace(stdout=out))
    assert vs.volume_sizes(["docker"]) == {"a": 11_630_000_000, "b": 0}


def test_declared_volume_that_docker_cannot_resolve_fails_named():
    with pytest.raises(ValueError, match="a_production_media_volume"):
        vs.build_report(["a_production_media_volume"], {}, {"a_production_media_volume": 1})


@pytest.mark.parametrize("text,expected", [("11.63GB", 11_630_000_000), ("356.7MB", 356_700_000),
                                           ("36.5kB", 36_500), ("0B", 0), ("2.8MB", 2_800_000)])
def test_parse_size(text, expected):
    assert vs.parse_size(text) == expected


def test_unparsable_size_raises():
    with pytest.raises(ValueError):
        vs.parse_size("lots")


def test_report_requires_size_and_mountpoint():
    with pytest.raises(ValueError):
        vs.build_report(["v"], {"v": "/p"}, {})
    with pytest.raises(ValueError):
        vs.build_report(["v"], {}, {"v": 1})


def _report():
    return [
        {"name": "a_production_media_volume", "path": "/var/lib/docker/volumes/a/_data", "bytes": 1_000_000_000},
        {"name": "b_production_media_volume", "path": "/var/lib/docker/volumes/b/_data", "bytes": 500_000_000},
    ]


def _patch_restic(monkeypatch, stderr, ok=False):
    monkeypatch.setattr(backup, "RESTIC_PASSWORD", "pw")
    monkeypatch.setattr(backup.os, "uname", lambda: types.SimpleNamespace(nodename="h1"), raising=False)
    monkeypatch.setattr(backup, "restic_repo_ready", lambda env, name: None)
    calls = []

    def fake_run_cmd(cmd, env=None, cwd=None, show_on_success=False):
        calls.append(cmd)
        return ok, "snapshot abcdef12 saved" if ok else "", stderr

    monkeypatch.setattr(backup, "run_cmd", fake_run_cmd)
    return calls


def test_unreadable_selected_volume_fails_and_is_named(monkeypatch, capsys):
    monkeypatch.setattr(backup, "VOLUME_REPORT", _report())
    _patch_restic(monkeypatch, "open /var/lib/docker/volumes/b/_data: permission denied")
    ok, snap = backup.run_restic("repo", "B2")
    out = capsys.readouterr().out
    assert (ok, snap) == (False, None)
    assert "VOLUME GAP: b_production_media_volume" in out
    assert "a_production_media_volume" not in out


def test_unselected_unreadable_path_is_not_named_as_a_volume_gap(monkeypatch, capsys):
    monkeypatch.setattr(backup, "VOLUME_REPORT", _report())
    _patch_restic(monkeypatch, "open /var/lib/docker/volumes/other/_data: permission denied")
    backup.run_restic("repo", "B2")
    assert "VOLUME GAP" not in capsys.readouterr().out


def test_summary_reports_count_and_size(monkeypatch, capsys):
    monkeypatch.setattr(backup, "VOLUME_REPORT", _report())
    monkeypatch.setattr(backup, "determine_docker_command", lambda: ["docker"])
    monkeypatch.setattr(backup, "ensure_restic_available", lambda: None)
    monkeypatch.setattr(backup, "generate_paths_file", lambda: None)
    monkeypatch.setattr(backup, "perform_db_dumps", lambda: [])
    monkeypatch.setattr(backup, "run_restic", lambda url, name, unlock_stale=False: (True, "snap1"))
    monkeypatch.setattr(backup, "record_b2_snapshot", lambda s: None)
    monkeypatch.setattr(backup, "record_run_dumps", lambda d: None)
    backup.main()
    out = capsys.readouterr().out
    assert "VOLUMES_BACKED_UP=2 bytes=1500000000" in out
    assert "volumes=2 paths/1.50 GB" in out


def test_size_gate_stops_before_any_upload(monkeypatch, capsys):
    big = [{"name": "x_production_media_volume", "path": "/p", "bytes": CFG["max_total_bytes_per_host"] + 1}]
    with pytest.raises(SystemExit):
        backup.check_volume_size_gate(big)
    assert "size gate" in capsys.readouterr().out


def test_size_gate_passes_within_limit():
    backup.check_volume_size_gate(_report())


def _patch_generate(monkeypatch, sizes=None):
    monkeypatch.setattr(gp, "docker_cmd_prefix", lambda: ["docker"])
    monkeypatch.setattr(vs, "running_container_volumes", lambda p: MOUNTED)
    monkeypatch.setattr(gp, "inspect_mountpoints", lambda p, v: {n: f"/var/lib/docker/volumes/{n}/_data" for n in v})
    monkeypatch.setattr(vs, "volume_sizes", lambda p: {n: 10 for n in ALL_NAMES} if sizes is None else sizes)


def test_generate_paths_emits_selected_volumes_without_stat_and_writes_report(monkeypatch, tmp_path, capsys):
    _patch_generate(monkeypatch)
    report_file = tmp_path / "r.json"
    monkeypatch.setattr(sys, "argv", ["generate_paths.py", "--volumes-report", str(report_file)])
    assert gp.main() == 0
    out = capsys.readouterr()
    assert sorted(Path(p).parent.name for p in out.out.split()) == EXPECTED
    assert "Skipping" not in out.err
    assert [v["name"] for v in json.loads(report_file.read_text())] == EXPECTED


def test_generate_paths_fails_when_a_selected_volume_has_no_size(monkeypatch, tmp_path):
    _patch_generate(monkeypatch, sizes={})
    monkeypatch.setattr(sys, "argv", ["generate_paths.py", "--volumes-report", str(tmp_path / "r.json")])
    assert gp.main() == 1


def test_allow_volume_failure_does_not_hide_a_failed_selection(monkeypatch, tmp_path):
    _patch_generate(monkeypatch, sizes={})
    monkeypatch.setattr(gp, "ALLOW_VOLUME_FAILURE", True)
    monkeypatch.setattr(sys, "argv", ["generate_paths.py", "--volumes-report", str(tmp_path / "r.json")])
    assert gp.main() == 1


def test_generate_paths_file_runs_the_size_gate_before_dumps(monkeypatch, tmp_path):
    big = [{"name": "x_production_media_volume", "path": "/p", "bytes": CFG["max_total_bytes_per_host"] + 1}]
    report_file = tmp_path / "r.json"
    script = tmp_path / "gen.py"
    script.write_text("")
    monkeypatch.setattr(backup, "GENERATE_PATHS_SCRIPT", script)
    monkeypatch.setattr(backup, "VOLUMES_REPORT_FILE", report_file)
    monkeypatch.setattr(backup, "PATHS_FILE", tmp_path / "paths.txt")

    def fake_generate(cmd, **kw):  # the real generator writes the report after backup.py cleared it
        report_file.write_text(json.dumps(big))
        return True, "/p\n", ""

    monkeypatch.setattr(backup, "run_cmd", fake_generate)
    called = []
    monkeypatch.setattr(backup, "perform_db_dumps", lambda: called.append("dump") or [])
    with pytest.raises(SystemExit):
        backup.generate_paths_file()
    assert called == []
