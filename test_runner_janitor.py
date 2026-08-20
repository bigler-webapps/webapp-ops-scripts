"""Structural safety guards for the CI-runner Docker GC script (INF-42).

Run: pytest test_runner_janitor.py
"""

import re

from pathlib import Path

RUNNER_JANITOR = Path(__file__).with_name("runner_janitor.sh")
JANITOR = Path(__file__).with_name("janitor.sh")

# Sibling repos on disk in this workspace layout (…/webapps/<repo>). Not
# guaranteed to exist in every checkout (e.g. a standalone CI clone of just
# this repo), so the cross-repo assertion below skips rather than fails when
# they're absent -- it must never silently pass by finding nothing to check.
WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
SERVER_MATRIX_WORKFLOW_DIRS = [
    WORKSPACE_ROOT / "webapp-management" / ".github",
    WORKSPACE_ROOT / "workflow-templates" / ".github",
]


def test_runner_janitor_prunes_volumes():
    """Deliberate inversion of janitor.sh::test_janitor_never_prunes_volumes
    -- this host's volumes are ephemeral per-job CI containers, not app data."""
    script = RUNNER_JANITOR.read_text(encoding="utf-8")
    assert "volume prune" in script


def test_runner_janitor_image_prune_window_is_under_24h():
    """INF-42 regression: runner-maintenance.yml's until=168h filtered on
    creation time and reclaimed 0B on a box that rebuilds per push."""
    script = RUNNER_JANITOR.read_text(encoding="utf-8")
    match = re.search(r'docker image prune -af --filter "until=(\d+)h"', script)
    assert match, "expected a docker image prune with an hour-denominated until filter"
    hours = int(match.group(1))
    assert 0 < hours < 24, f"until window is {hours}h, expected 0 < hours < 24"


def test_runner_janitor_build_cache_floor_is_well_under_janitor_sh():
    """janitor.sh's --keep-storage=20GB reclaimed almost nothing against this
    host's 21.49GB cache; the floor here must be materially smaller, or
    absent entirely (a bare `docker builder prune -af`)."""
    script = RUNNER_JANITOR.read_text(encoding="utf-8")
    match = re.search(r"docker builder prune -af(?: --keep-storage=(\d+)GB)?", script)
    assert match, "expected a docker builder prune -af line"
    floor_gb = match.group(1)
    if floor_gb is not None:
        assert int(floor_gb) < 20, f"build-cache floor is {floor_gb}GB, expected < 20GB"


def test_janitor_still_never_prunes_volumes():
    """Regression guard: this WO must not touch janitor.sh's volume-safety
    invariant, which protects real app-server data."""
    script = JANITOR.read_text(encoding="utf-8")
    assert "volume prune" not in script


def test_runner_janitor_not_referenced_by_any_server_matrix_workflow():
    """runner_janitor.sh must stay a runner-only, standalone job. If it ever
    gets pulled into a server-matrix workflow (janitor.yml, maintenance.yml,
    backup.yml, ...), the same script that prunes volumes here would run
    against app servers holding real data.

    "Server-matrix" is identified by use of resolve_inventory_targets.py,
    the shared resolver every fan-out-to-hosts workflow uses -- this
    deliberately excludes e.g. app-ci.yml, which mentions runner_janitor.sh
    only in an explanatory comment and never dispatches to a host matrix."""
    checked_any = False
    for gh_dir in SERVER_MATRIX_WORKFLOW_DIRS:
        if not gh_dir.is_dir():
            continue
        for path in gh_dir.rglob("*.yml"):
            if path.name == "runner-janitor.yml":
                continue
            text = path.read_text(encoding="utf-8")
            if "resolve_inventory_targets" not in text:
                continue
            checked_any = True
            assert "runner_janitor.sh" not in text, (
                f"{path} references runner_janitor.sh -- it must stay out of "
                "every server-matrix workflow"
            )
    if not checked_any:
        import pytest

        pytest.skip("sibling repos (webapp-management/workflow-templates) not on disk")
