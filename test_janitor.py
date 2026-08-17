"""Structural safety guards for the production janitor script.

Run: pytest test_janitor.py
"""

import os
import shutil
import stat
import subprocess

import pytest

from pathlib import Path


JANITOR = Path(__file__).with_name("janitor.sh")
# Resolve bash explicitly rather than relying on subprocess's own PATH search:
# on Windows a bare "bash" can hit the WSL launcher shim in System32 instead
# of a real bash (e.g. Git Bash), which fails outright with no WSL installed.
BASH = shutil.which("bash")


def test_build_cache_prune_keeps_a_20gb_floor():
    script = JANITOR.read_text(encoding="utf-8")
    assert "docker builder prune -af --keep-storage=20GB" in script


def test_janitor_never_prunes_volumes():
    script = JANITOR.read_text(encoding="utf-8")
    assert "volume prune" not in script


def test_janitor_reports_docker_and_containerd_storage_for_diagnosis():
    script = JANITOR.read_text(encoding="utf-8")
    assert "docker system df -v" in script
    assert "/var/lib/docker/*/ /var/lib/containerd/*/" in script


def test_no_unguarded_producer_into_head_pipeline():
    """INF-19: `docker images | head -n 50` used to abort the whole script on
    SIGPIPE once a host had more than 50 images (staging, exit 141, red every
    night since 2026-08-12) -- `head` closes the pipe early and `set -o
    pipefail` + `set -e` propagate the producer's SIGPIPE death as a script
    abort. Every producer-into-`head` pipeline in this script must be
    guarded the same way the pre-existing `unattended-upgrades | head -n 80`
    line already was."""
    script = JANITOR.read_text(encoding="utf-8")
    offending = [
        line for line in script.splitlines()
        if "| head" in line and "|| true" not in line
    ]
    assert not offending, f"unguarded producer-into-head pipeline(s): {offending}"


def test_docker_images_listing_is_present_and_guarded():
    script = JANITOR.read_text(encoding="utf-8")
    lines = [ln for ln in script.splitlines() if "docker images --format" in ln]
    assert len(lines) == 1, "expected exactly one docker images listing line"
    assert lines[0].rstrip().endswith("|| true"), lines[0]


def test_janitor_survives_docker_images_output_over_50_lines(tmp_path):
    """Behavioural regression test for the actual defect (the structural
    guards above pin text, not behaviour, and would not have caught this):
    execute the script with `docker` stubbed to emit far more than 50 image
    lines, and assert the script still exits 0 and reaches '== Done =='.
    Against today's pre-fix script this fails with exit 141 (128 + SIGPIPE)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    def _stub(name: str, body: str) -> None:
        path = bin_dir / name
        path.write_text(body, encoding="utf-8", newline="\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    _stub(
        "docker",
        "#!/usr/bin/env bash\n"
        "case \"$1\" in\n"
        "  images)\n"
        "    for i in $(seq 1 200); do echo \"repo$i  tag$i  10MB\"; done\n"
        "    ;;\n"
        "  *)\n"
        "    exit 0\n"
        "    ;;\n"
        "esac\n",
    )
    # Shadow any real unattended-upgrades on PATH so the test stays hermetic
    # (it would otherwise try to touch the real system's apt state).
    _stub("unattended-upgrades", "#!/usr/bin/env bash\necho stub dry-run\n")

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"

    if not BASH:
        pytest.skip("no bash available on PATH")

    result = subprocess.run(
        [BASH, str(JANITOR)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, (
        f"janitor.sh exited {result.returncode} (expected 0); "
        f"stdout tail:\n{result.stdout[-2000:]}\nstderr tail:\n{result.stderr[-2000:]}"
    )
    assert "== Done ==" in result.stdout
