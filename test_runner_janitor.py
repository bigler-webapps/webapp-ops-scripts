"""Structural safety guards for the CI-runner Docker GC script (INF-42).

Run: pytest test_runner_janitor.py
"""

import os
import re
import shutil
import stat
import subprocess

from pathlib import Path

import pytest

RUNNER_JANITOR = Path(__file__).with_name("runner_janitor.sh")
JANITOR = Path(__file__).with_name("janitor.sh")
BASH = shutil.which("bash")

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


def test_runner_janitor_buildx_cleanup_uses_buildx_rm():
    """INF-46 regression: `docker rm` on the orphaned builder CONTAINER alone
    (the pre-INF-46 approach) leaves the builder registered and its state
    volume behind. `docker buildx rm` removes builder + container + volume
    together -- this is now the only removal path for these entries."""
    script = RUNNER_JANITOR.read_text(encoding="utf-8")
    assert "docker buildx rm" in script
    assert 'docker ps -a --filter "name=buildx_buildkit_builder-"' not in script


def test_runner_janitor_buildx_builder_name_comes_from_buildx_ls():
    """INF-46: the builder name must be read from `docker buildx ls` itself,
    never derived from a container/volume name -- those carry a different
    prefix (buildx_buildkit_builder- vs builder-) plus a trailing node index
    (.../builder-<uuid>0_state) that a substring-derived name would silently
    get wrong, per the WO's own documented trap. This also pins that the
    node-index pitfall is actually documented in the script, not just avoided
    by accident."""
    script = RUNNER_JANITOR.read_text(encoding="utf-8")
    assert "docker buildx ls" in script
    assert "node index" in script


def test_runner_janitor_no_blanket_volume_prune_all():
    """Non-goal, INF-46: `docker volume prune -af`/`--all` would hit every
    unused named volume on the host, widening exactly the blast radius the
    INF-42 review already had to narrow (this script's own rsync exposure
    to app-server disks). Cleanup here must stay targeted at the specific
    buildx naming pattern instead.

    Checks actual flag TOKENS, not a bare "-a" substring: a naive substring
    check would miss a combined short-flag ordering like `-fa` (still
    "force + all", the same forbidden semantics as `-af`) because the
    literal characters "-a" never appear in "-fa"."""
    script = RUNNER_JANITOR.read_text(encoding="utf-8")
    for line in script.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "volume prune" not in stripped:
            continue
        command = stripped.split("#", 1)[0]
        flags = command.split("volume prune", 1)[1]
        for token in flags.split():
            if token.startswith("--"):
                assert token != "--all", line
            elif token.startswith("-"):
                assert "a" not in token[1:], line


def _run_janitor_with_docker_stub(
    tmp_path,
    buildx_ls_output="",
    volume_ls_output="",
    volume_rm_exit=0,
):
    """Runs the real runner_janitor.sh against a stubbed `docker` on PATH.
    Returns (result, calls) where `calls` is the stub's own invocation log
    (one line per buildx rm / volume rm call), letting each test assert on
    exactly what the script tried to remove without needing a real Docker
    daemon or a real buildx state."""
    if not BASH:
        pytest.skip("no bash available on PATH")

    bin_dir = tmp_path / "bin"
    # exist_ok: defensive only. Every current caller passes a fresh path (a
    # bare `tmp_path`, or its own per-iteration subdirectory when looping
    # over fixtures -- see test_runner_janitor_default_never_removed_either_ordering),
    # so this never actually re-creates an existing directory today.
    bin_dir.mkdir(exist_ok=True)
    log_path = tmp_path / "docker_calls.log"
    buildx_ls_path = tmp_path / "buildx_ls_output.txt"
    volume_ls_path = tmp_path / "volume_ls_output.txt"
    buildx_ls_path.write_text(buildx_ls_output, encoding="utf-8", newline="\n")
    volume_ls_path.write_text(volume_ls_output, encoding="utf-8", newline="\n")

    docker_stub = f"""#!/usr/bin/env bash
LOG="{log_path!s}"
case "$1 $2" in
  "buildx ls")
    cat "{buildx_ls_path!s}"
    ;;
  "buildx rm")
    echo "buildx rm $3" >> "$LOG"
    ;;
  "volume ls")
    cat "{volume_ls_path!s}"
    ;;
  "volume rm")
    echo "volume rm $3" >> "$LOG"
    exit {volume_rm_exit}
    ;;
  *)
    true
    ;;
esac
exit 0
"""
    stub_path = bin_dir / "docker"
    stub_path.write_text(docker_stub, encoding="utf-8", newline="\n")
    stub_path.chmod(stub_path.stat().st_mode | stat.S_IEXEC)

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"

    result = subprocess.run(
        [BASH, str(RUNNER_JANITOR)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    calls = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    return result, calls


def _removed(calls, kind, name):
    """Exact-line membership, not substring: `"buildx rm builder-X" in calls`
    would ALSO match a call that actually removed the node-index-contaminated
    "builder-X0" (X's own name is a string-prefix of X0) -- exactly the bug
    class INF-46/INF-50 exist to catch. Each stub call is logged as
    "<kind> <name>\\n" (see `_run_janitor_with_docker_stub`'s docker_stub),
    so anchoring on the trailing newline makes this an exact-name check."""
    return f"{kind} {name}\n" in calls


# INF-50: reconstructed from the WO's own Part A two-builder walk-through
# (already verified there against the real box), WITH the header row every
# real `docker buildx ls` invocation prints added back in -- review finding:
# an earlier draft of this fixture omitted it (harmless to the assertions,
# since a header row never matches `^builder-` either way, but inaccurate to
# label "literal real output" while missing a row real output always has).
# Byte-exact WO Part B quote lives separately below, in
# REAL_BUILDX_LS_ONE_BUILDER_DEFAULT_LAST_VERBATIM_FROM_WO. Preserves the
# real shape that matters here: ` \_ ` node prefixes, the node-index digit,
# `default*`'s trailing `*`, STATUS on the NODE line (not the header), and
# `default` LAST -- INF-46's own fixture put `default` FIRST, which is why
# 17/17 tests stayed green
# while the live script silently dropped the real last builder every run --
# a test against self-authored output confirms the author's own assumption,
# not reality (this is the review question INF-50 was routed on).
REAL_BUILDX_LS_TWO_INACTIVE_BUILDERS_DEFAULT_LAST = (
    "NAME/NODE                                           DRIVER/ENDPOINT                   STATUS     BUILDKIT   PLATFORMS\n"
    "builder-08591bd2-...        docker-container\n"
    " \\_ builder-08591bd2-...0    \\_ unix:///var/run/docker.sock   inactive\n"
    "builder-af6064dc-...        docker-container\n"
    " \\_ builder-af6064dc-...0    \\_ unix:///var/run/docker.sock   inactive\n"
    "default*                                            docker\n"
    " \\_ default                  \\_ default                       running   v0.32.2   linux/amd64 (+3)\n"
)

# Character-for-character from work-orders/INF-50.md Part B ("wörtlich als
# Fixture verwenden"), captured live on the box on the run that surfaced
# this defect -- the single-builder case the WO cites by name, kept
# separate from the two-builder fixtures above (which are transcribed from
# the WO's own two-builder walk-through in Part A, not Part B) so that at
# least one test traces to the WO's literal quoted bytes, not a
# reconstruction of them.
REAL_BUILDX_LS_ONE_BUILDER_DEFAULT_LAST_VERBATIM_FROM_WO = (
    "NAME/NODE                                           DRIVER/ENDPOINT                   STATUS     BUILDKIT   PLATFORMS\n"
    "builder-af6064dc-4c64-48f6-adba-9aa5364d00e3        docker-container                                        \n"
    " \\_ builder-af6064dc-4c64-48f6-adba-9aa5364d00e30    \\_ unix:///var/run/docker.sock   inactive              \n"
    "default*                                            docker                                                  \n"
    " \\_ default                                          \\_ default                       running    v0.32.2    linux/amd64 (+3)\n"
)


def test_runner_janitor_removes_single_builder_wo_verbatim_fixture(tmp_path):
    """INF-50: the exact fixture quoted in work-orders/INF-50.md Part B,
    reproducing the live incident (one leftover builder, af6064dc, silently
    never collected by any GC run since it was always the list's last
    entry). Transcription error is the risk this test guards against that
    the other fixtures above cannot: they're built from the WO's Part A
    prose walk-through, not copied byte-for-byte from its Part B quote."""
    result, calls = _run_janitor_with_docker_stub(
        tmp_path, buildx_ls_output=REAL_BUILDX_LS_ONE_BUILDER_DEFAULT_LAST_VERBATIM_FROM_WO
    )
    assert result.returncode == 0
    assert _removed(calls, "buildx rm", "builder-af6064dc-4c64-48f6-adba-9aa5364d00e3"), calls
    assert "default" not in calls


REAL_BUILDX_LS_ONE_ACTIVE_ONE_INACTIVE_DEFAULT_LAST = (
    "NAME/NODE                                           DRIVER/ENDPOINT                   STATUS     BUILDKIT   PLATFORMS\n"
    "builder-08591bd2-...        docker-container\n"
    " \\_ builder-08591bd2-...0    \\_ unix:///var/run/docker.sock   inactive\n"
    "builder-af6064dc-...        docker-container\n"
    " \\_ builder-af6064dc-...0    \\_ unix:///var/run/docker.sock   running\n"
    "default*                                            docker\n"
    " \\_ default                  \\_ default                       running   v0.32.2   linux/amd64 (+3)\n"
)

# Legacy ordering (`default` FIRST) -- not real `docker buildx ls` output,
# but required test 4 (INF-50) demands the enumeration survive BOTH
# orderings, not swap a fix for one against a regression in the other.
LEGACY_BUILDX_LS_ONE_ACTIVE_ONE_INACTIVE_DEFAULT_FIRST = """\
NAME/NODE                                     DRIVER/ENDPOINT       STATUS
default                                       docker
  default                                     running
builder-08591bd2-6d51-431e-a0a6-63f2f5fca89d  docker-container
  builder-08591bd2-6d51-431e-a0a6-63f2f5fca89d0  running
builder-4fa805b0-0f65-4a9b-aa3f-89f436e5ae0d  docker-container
  builder-4fa805b0-0f65-4a9b-aa3f-89f436e5ae0d0  inactive
"""


def test_runner_janitor_removes_last_builder_before_default_real_output(tmp_path):
    """INF-50 regression -- the actual defect: `docker buildx ls` always ends
    with `default`, and `default` is always `running`. The pre-fix awk used
    `/^builder-/` as BOTH the record boundary and the output test, so
    `default`'s own line never flushed the prior (real, last) builder before
    overwriting `name` -- its `running` node then got attributed to whatever
    builder `name` still held, and `END{flush()}` suppressed it. Reproduced
    live: one leftover builder survived every GC run because it was always
    the list's last entry. Against the REAL two-builder output (default
    last), BOTH builders must be enumerated -- not just the first."""
    result, calls = _run_janitor_with_docker_stub(
        tmp_path, buildx_ls_output=REAL_BUILDX_LS_TWO_INACTIVE_BUILDERS_DEFAULT_LAST
    )
    assert result.returncode == 0, (
        f"runner_janitor.sh exited {result.returncode}; "
        f"stdout tail:\n{result.stdout[-2000:]}\nstderr tail:\n{result.stderr[-2000:]}"
    )
    assert _removed(calls, "buildx rm", "builder-08591bd2-..."), calls
    assert _removed(calls, "buildx rm", "builder-af6064dc-..."), calls


def test_runner_janitor_default_never_removed_either_ordering(tmp_path):
    """Required test 2 (INF-50): `default` must never appear in a removal
    call, whether it sits first (legacy fixture) or last (real output)."""
    for index, fixture in enumerate(
        (
            REAL_BUILDX_LS_TWO_INACTIVE_BUILDERS_DEFAULT_LAST,
            LEGACY_BUILDX_LS_ONE_ACTIVE_ONE_INACTIVE_DEFAULT_FIRST,
        )
    ):
        case_dir = tmp_path / f"case{index}"
        case_dir.mkdir()
        _, calls = _run_janitor_with_docker_stub(case_dir, buildx_ls_output=fixture)
        assert "default" not in calls, (fixture, calls)


def test_runner_janitor_skips_running_builder_that_is_last_before_default(tmp_path):
    """Required test 3 (INF-50): an active builder must stay skipped even
    when it is the LAST `builder-*` entry immediately before `default` --
    exactly the position the original defect silently mishandled. Must not
    "fix" INF-50 by dropping the running-check altogether."""
    result, calls = _run_janitor_with_docker_stub(
        tmp_path, buildx_ls_output=REAL_BUILDX_LS_ONE_ACTIVE_ONE_INACTIVE_DEFAULT_LAST
    )
    assert result.returncode == 0
    assert _removed(calls, "buildx rm", "builder-08591bd2-..."), calls
    assert "builder-af6064dc" not in calls, (
        f"the ACTIVE (running) builder, last before default, must never be removed: {calls}"
    )


def test_runner_janitor_removes_inactive_builder_keeps_active_one_legacy_ordering(tmp_path):
    """Required test 4 (INF-50): the legacy `default`-first fixture stays a
    covered case -- the enumeration must survive both orderings, not trade
    one for the other."""
    result, calls = _run_janitor_with_docker_stub(
        tmp_path, buildx_ls_output=LEGACY_BUILDX_LS_ONE_ACTIVE_ONE_INACTIVE_DEFAULT_FIRST
    )
    assert result.returncode == 0, (
        f"runner_janitor.sh exited {result.returncode}; "
        f"stdout tail:\n{result.stdout[-2000:]}\nstderr tail:\n{result.stderr[-2000:]}"
    )
    assert _removed(calls, "buildx rm", "builder-4fa805b0-0f65-4a9b-aa3f-89f436e5ae0d"), calls
    assert "builder-08591bd2" not in calls, (
        f"the ACTIVE builder must never be removed: {calls}"
    )


def test_runner_janitor_fallback_removes_volume_with_no_buildx_entry(tmp_path):
    """R1 review finding (INF-46): the fallback volume-cleanup path (for a
    state volume whose builder entry is already gone from buildx's own
    registry -- WO Part B, second seam) had zero test coverage. `buildx ls`
    knows nothing here; the orphaned volume must be picked up and removed
    directly, by name pattern, via the fallback loop."""
    result, calls = _run_janitor_with_docker_stub(
        tmp_path,
        buildx_ls_output="NAME/NODE   DRIVER/ENDPOINT\ndefault     docker\n",
        volume_ls_output="buildx_buildkit_builder-08591bd2-6d51-431e-a0a6-63f2f5fca89d0_state\n",
    )
    assert result.returncode == 0
    assert _removed(calls, "volume rm", "buildx_buildkit_builder-08591bd2-6d51-431e-a0a6-63f2f5fca89d0_state"), calls
    assert "buildx rm" not in calls, "buildx ls listed no such builder -- nothing for buildx rm to remove"


def test_runner_janitor_fallback_volume_rm_failure_does_not_abort_script(tmp_path):
    """R1 review finding (INF-46): the WO's own Risks section says an active
    builder's volume must never be removed -- the actual backstop for that
    is Docker itself refusing to remove a volume still mounted by a
    container, not a check in this script. What IS this script's own
    responsibility is surviving that refusal: `docker volume rm` failing
    (simulated here as exit 1, the real "volume is in use" case) must not
    abort the run via `set -euo pipefail` -- the `|| true` after it must
    hold, and the script must still reach its own end."""
    result, calls = _run_janitor_with_docker_stub(
        tmp_path,
        buildx_ls_output="NAME/NODE   DRIVER/ENDPOINT\ndefault     docker\n",
        volume_ls_output="buildx_buildkit_builder-08591bd2-6d51-431e-a0a6-63f2f5fca89d0_state\n",
        volume_rm_exit=1,
    )
    assert result.returncode == 0, (
        f"a failed docker volume rm must not abort the script; "
        f"stdout tail:\n{result.stdout[-2000:]}\nstderr tail:\n{result.stderr[-2000:]}"
    )
    assert "== Done ==" in result.stdout
    assert _removed(calls, "volume rm", "buildx_buildkit_builder-08591bd2-6d51-431e-a0a6-63f2f5fca89d0_state"), calls


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
