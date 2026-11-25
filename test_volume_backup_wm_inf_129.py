"""OPS-BAK-2: HRAM calibration outputs are excluded from backup; the exact-include mechanism and size gate stay.

Run: pytest test_volume_backup_wm_inf_129.py
"""

import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import backup  # noqa: E402
import volume_selection as vs  # noqa: E402

CFG = vs.load_config()
CALIBRATION = "hram_production_calibration_outputs_volume"


def test_the_calibration_volume_is_excluded_from_the_shipped_policy():
    assert any(__import__("fnmatch").fnmatchcase(CALIBRATION, pattern) for pattern in CFG["exclude"]), "the exclusion still matches it"
    assert vs.select_volumes({CALIBRATION, "hram_production_excel_volume"}, CFG) == ["hram_production_excel_volume"]


def test_the_name_is_what_compose_creates_for_the_production_project_and_the_declared_volume_key():
    # hram's compose key is `calibration_outputs_volume`; the production Compose project is `hram_production`
    assert CALIBRATION == "hram_production_calibration_outputs_volume"
    assert CFG["include_exact"] == []


@pytest.mark.parametrize("name", [
    "hram_production_calibration_outputs",            # a near name
    "hram_production_calibration_outputs_volume2",
    "hram_staging_calibration_outputs_volume",        # staging is never backed up
    "other_production_calibration_outputs_volume",    # another app's volume with the same key
    "hram_production_calibration_outputs_volume_old",
    "HRAM_PRODUCTION_CALIBRATION_OUTPUTS_VOLUME",     # exact, case-sensitive
])
def test_no_other_calibration_volume_is_selected(name):
    assert vs.select_volumes({name}, CFG) == []


def _fake_docker(mounts):
    """`docker ps -q` / `docker inspect` of running containers whose mounts are `mounts` (lists of volume names)."""
    def run(cmd, **kw):
        if cmd[-1] == "-q":
            return types.SimpleNamespace(stdout=" ".join(f"id{i}" for i in range(len(mounts))) + "\n")
        return types.SimpleNamespace(stdout=json.dumps([{"Mounts": [{"Type": "volume", "Name": n} for n in names]} for names in mounts]))
    return run


def test_an_unmounted_calibration_volume_is_not_selected_through_the_real_mount_discovery(monkeypatch):
    monkeypatch.setattr(vs.subprocess, "run", _fake_docker([["hram_production_excel_volume"], []]))
    mounted = vs.running_container_volumes(["docker"])
    assert CALIBRATION not in mounted and vs.select_volumes(mounted, CFG) == ["hram_production_excel_volume"]
    monkeypatch.setattr(vs.subprocess, "run", _fake_docker([["hram_production_excel_volume", CALIBRATION]]))
    assert vs.select_volumes(vs.running_container_volumes(["docker"]), CFG) == ["hram_production_excel_volume"]


def test_the_calibration_volume_is_absent_from_the_backup_path_list_and_report_through_the_generator(monkeypatch, tmp_path, capsys):
    import generate_paths as gp
    names = [CALIBRATION, "hram_production_pipeline_artifacts_volume", "hram_staging_calibration_outputs_volume"]
    monkeypatch.setattr(gp, "docker_cmd_prefix", lambda: ["docker"])
    monkeypatch.setattr(vs, "running_container_volumes", lambda p: set(names))
    monkeypatch.setattr(gp, "inspect_mountpoints", lambda p, v: {n: f"/var/lib/docker/volumes/{n}/_data" for n in v})
    monkeypatch.setattr(vs, "volume_sizes", lambda p: {n: 10 for n in names})
    report_file = tmp_path / "r.json"
    monkeypatch.setattr(sys, "argv", ["generate_paths.py", "--volumes-report", str(report_file)])
    assert gp.main() == 0
    assert [v["name"] for v in json.loads(report_file.read_text())] == []
    assert capsys.readouterr().out.split() == []


def test_hrams_compose_creates_exactly_that_production_volume_name():
    compose = Path(__file__).resolve().parents[1] / "hram" / "docker-compose.yml"
    if not compose.is_file():
        pytest.skip("sibling hram checkout is absent")
    lines = compose.read_text(encoding="utf-8").splitlines()
    top = max(i for i, line in enumerate(lines) if line == "volumes:")  # the top-level block is the last one
    block = []
    for line in lines[top + 1:]:
        if line and not line.startswith(" "):
            break
        block.append(line)
    key = block.index("  calibration_outputs_volume:")
    own = []
    for line in block[key + 1:]:
        if line.startswith("  ") and not line.startswith("   "):
            break  # the next volume key
        own.append(line)
    assert not any(line.strip().startswith("name:") for line in own), "an explicit name would change the Docker volume name"


def test_the_size_gate_stops_the_backup_before_any_dump_when_the_calibration_volume_tips_the_host_over(monkeypatch, tmp_path):
    report_file = tmp_path / "r.json"
    script = tmp_path / "gen.py"
    script.write_text("")
    monkeypatch.setattr(backup, "GENERATE_PATHS_SCRIPT", script)
    monkeypatch.setattr(backup, "VOLUMES_REPORT_FILE", report_file)
    monkeypatch.setattr(backup, "PATHS_FILE", tmp_path / "paths.txt")
    over = [{"name": "hram_production_excel_volume", "path": "/p", "bytes": 100},
            {"name": CALIBRATION, "path": "/q", "bytes": CFG["max_total_bytes_per_host"]}]

    def fake_generate(cmd, **kw):
        report_file.write_text(json.dumps(over))
        return True, "/p\n/q\n", ""

    monkeypatch.setattr(backup, "run_cmd", fake_generate)
    called = []
    monkeypatch.setattr(backup, "perform_db_dumps", lambda: called.append("dump") or [])
    with pytest.raises(SystemExit):
        backup.generate_paths_file()
    assert called == [], "no dump and no upload starts when the gate trips"


@pytest.mark.parametrize("name", [
    "hram_production_pipeline_artifacts_volume",
    "hram_production_hf_model_cache",
    "cockpit_production_secrets_volume",
    "photogallery_production_media_volume",
    "hram_staging_media_volume",
    "hram_stage_excel_volume",
])
def test_the_other_production_artifact_exclusions_remain(name):
    assert vs.select_volumes({name}, CFG) == []


def test_include_exact_overrides_exclusion_in_a_local_policy(tmp_path):
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps({"include": [], "exclude": ["*calibration_outputs*"],
                                "include_exact": [CALIBRATION], "max_total_bytes_per_host": 1}), encoding="utf-8")
    assert vs.select_volumes({CALIBRATION, "other_calibration_outputs_volume"}, vs.load_config(path)) == [CALIBRATION]


def test_include_exact_accepts_names_only_never_patterns(tmp_path):
    for bad in (["*calibration*"], ["hram_*"], ["a b"], [1], "hram_production_calibration_outputs_volume", [""]):
        path = tmp_path / "cfg.json"
        path.write_text(json.dumps({"include": [], "exclude": [], "include_exact": bad, "max_total_bytes_per_host": 1}), encoding="utf-8")
        with pytest.raises(ValueError, match="include_exact"):
            vs.load_config(path)


def test_a_config_without_include_exact_behaves_as_before(tmp_path):
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps({"include": ["*_media_volume"], "exclude": ["*calibration*"], "max_total_bytes_per_host": 1}), encoding="utf-8")
    cfg = vs.load_config(path)
    assert vs.select_volumes({"a_media_volume", CALIBRATION}, cfg) == ["a_media_volume"]


def test_the_host_size_limit_is_unchanged_and_still_stops_before_any_upload(capsys):
    assert CFG["max_total_bytes_per_host"] == 17_000_000_000
    # the six volumes measured on 2026-11-20 (13,665,198,515 apparent bytes) fit; the same host growing past the limit does not
    within = [{"name": CALIBRATION, "path": "/p", "bytes": 13_665_198_515}]
    backup.check_volume_size_gate(within)
    beyond = [{"name": CALIBRATION, "path": "/p", "bytes": 17_000_000_001}]
    with pytest.raises(SystemExit):
        backup.check_volume_size_gate(beyond)
    assert "size gate" in capsys.readouterr().out
