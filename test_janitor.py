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
        # Comments are prose, not pipelines -- this guard used to flag its own
        # documentation (INF-51 added a comment naming `docker images | head`).
        if "| head" in line and "|| true" not in line and not line.strip().startswith("#")
    ]
    assert not offending, f"unguarded producer-into-head pipeline(s): {offending}"


def test_docker_images_listing_is_present_and_guarded():
    script = JANITOR.read_text(encoding="utf-8")
    # Comments are prose, not commands -- INF-51 added a comment naming this
    # very line, which the unfiltered scan counted as a second listing.
    lines = [
        ln for ln in script.splitlines()
        if "docker images --format" in ln and not ln.strip().startswith("#")
    ]
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


# --- INF-51: retention by count, and a threshold that can actually fail ------


def _stub_bin(bin_dir, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(body, encoding="utf-8", newline="\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _make_stub_env(tmp_path, docker_body: str, disk_pct: int = 40):
    """Build a PATH with `docker`, `df` and `unattended-upgrades` stubbed.

    `df` must be stubbed too: the script now READS the disk figure and fails
    above the threshold, so leaving the real df in place would make every one
    of these tests depend on whatever the machine running them happens to be
    sitting at.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _stub_bin(bin_dir, "docker", docker_body)
    # Mimics both shapes the script uses: `df -h /` (human) and `df -P /`
    # (parsed). Field 5 is Use%.
    _stub_bin(
        bin_dir,
        "df",
        "#!/usr/bin/env bash\n"
        "echo 'Filesystem     1024-blocks     Used Available Capacity Mounted on'\n"
        "echo '/dev/sda1        164802308 96000000  60000000 " + str(disk_pct) + "% /'\n",
    )
    _stub_bin(bin_dir, "unattended-upgrades", "#!/usr/bin/env bash\necho stub dry-run\n")

    env = os.environ.copy()
    env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]
    env["RM_LOG"] = str(tmp_path / "removed.txt")
    env.setdefault("RUNNING_IDS", "")
    return env


# Several repositories with more tags than the retention count. `image rm`
# calls are appended to $RM_LOG so the test can assert on the exact set.
# Deliberately NOT pre-sorted: the script's own sort establishes the order.
DOCKER_WITH_IMAGES = r"""#!/usr/bin/env bash
# id<space>created<space>comma-separated-tags ; $IMAGE_TABLE overrides the default
DEFAULT_TABLE='
sha256:a5 2026-08-20T08:00:00.0Z ghcr.io/x/app-a:t5
sha256:a4 2026-08-20T07:00:00.0Z ghcr.io/x/app-a:t4
sha256:a3 2026-08-20T06:00:00.0Z ghcr.io/x/app-a:t3
sha256:a2 2026-08-20T05:00:00.0Z ghcr.io/x/app-a:t2
sha256:a1 2026-08-20T04:00:00.0Z ghcr.io/x/app-a:t1
sha256:b4 2026-08-20T07:00:00.0Z ghcr.io/x/app-b:t4
sha256:b3 2026-08-20T06:00:00.0Z ghcr.io/x/app-b:t3
sha256:b2 2026-08-20T05:00:00.0Z ghcr.io/x/app-b:t2
sha256:b1 2026-08-20T04:00:00.0Z ghcr.io/x/app-b:t1
sha256:pg 2026-01-01T00:00:00.0Z postgres:18
sha256:rd 2026-01-01T00:00:00.0Z redis:alpine
sha256:dang 2026-08-20T03:00:00.0Z <none>:<none>
'
TABLE="${IMAGE_TABLE:-$DEFAULT_TABLE}"

if [ "$1 $2" = "ps -aq" ]; then
  printf '%s\n' $RUNNING_IDS
  exit 0
fi
case "$1" in
  inspect)
    # container inspect: id -> image id, via $REF_<id>-style RUNNING map
    for a in "$@"; do
      case "$a" in sha256:*|c[0-9]*) eval "printf '%s\n' \"\$IMG_$a\"" ;; esac
    done
    ;;
  images)
    if [ "${*#*-q}" != "$*" ]; then
      printf '%s\n' "$TABLE" | awk 'NF { print $1 }'
    else
      i=1
      while [ "$i" -le 200 ]; do echo "repo$i  tag$i  10MB"; i=$((i+1)); done
    fi
    ;;
  image)
    case "$2" in
      inspect)
        shift 3
        for id in "$@"; do
          printf '%s\n' "$TABLE" | awk -v want="$id" 'NF && $1 == want {
            tags = ""
            for (i = 3; i <= NF; i++) tags = tags $i " "
            print $2 "|" $1 "|" tags
          }'
        done
        ;;
      rm)
        echo "$3" >> "$RM_LOG"
        ;;
      prune) : ;;
    esac
    ;;
  *)
    exit 0
    ;;
esac
"""


def _run_janitor(env):
    if not BASH:
        pytest.skip("no bash available on PATH")
    return subprocess.run(
        [BASH, str(JANITOR)], env=env, capture_output=True, text=True, timeout=60
    )


def _removed(env) -> set:
    log = Path(env["RM_LOG"])
    return set(log.read_text(encoding="utf-8").split()) if log.exists() else set()


def test_retention_keeps_exactly_n_newest_per_repository(tmp_path):
    """Requirement 1: N per REPOSITORY, independent of how many repos exist or
    how fast any one churns. The old `until=48h` filter could reach 26 of 262
    images on staging precisely because it keyed on age instead."""
    env = _make_stub_env(tmp_path, DOCKER_WITH_IMAGES)
    env["JANITOR_IMAGE_RETENTION"] = "2"

    result = _run_janitor(env)
    assert result.returncode == 0, result.stdout + result.stderr

    # app-a has 5 tags, keep the 2 newest (t5, t4) -> t3, t2, t1 go.
    # app-b has 4 tags, keep the 2 newest (t4, t3) -> t2, t1 go.
    assert _removed(env) == {
        "ghcr.io/x/app-a:t3",
        "ghcr.io/x/app-a:t2",
        "ghcr.io/x/app-a:t1",
        "ghcr.io/x/app-b:t2",
        "ghcr.io/x/app-b:t1",
    }


def test_retention_spares_newest_and_single_tag_repositories(tmp_path):
    """postgres:18 and redis:alpine have one tag each and must survive, as must
    the newest tag of every app repo. A retention pass that eats the image a
    container is about to need is worse than the full disk it prevents."""
    env = _make_stub_env(tmp_path, DOCKER_WITH_IMAGES)
    env["JANITOR_IMAGE_RETENTION"] = "2"
    _run_janitor(env)
    for survivor in ("postgres:18", "redis:alpine", "ghcr.io/x/app-a:t5", "ghcr.io/x/app-b:t4"):
        assert survivor not in _removed(env)


def test_retention_skips_images_in_use_by_a_container(tmp_path):
    """Live workloads outrank the retention count. `docker image rm` would fail
    on these anyway; the point is that the script decides deliberately rather
    than discovering it from an error mid-loop."""
    env = _make_stub_env(tmp_path, DOCKER_WITH_IMAGES)
    env["JANITOR_IMAGE_RETENTION"] = "2"
    env["RUNNING_IDS"] = "c1"
    env["IMG_c1"] = "sha256:a1"

    result = _run_janitor(env)
    assert result.returncode == 0, result.stdout + result.stderr
    removed = _removed(env)
    assert "ghcr.io/x/app-a:t1" not in removed, "in-use image was removed"
    assert "in use by a container" in result.stdout
    # One protected image must not abort the rest of the pass.
    assert "ghcr.io/x/app-a:t2" in removed


def test_disk_below_threshold_exits_zero(tmp_path):
    """Both sides of the threshold get a test. One that never passes is as
    useless as one that never fails."""
    env = _make_stub_env(tmp_path, DOCKER_WITH_IMAGES, disk_pct=40)
    result = _run_janitor(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "== Done ==" in result.stdout


def test_disk_above_threshold_exits_nonzero(tmp_path):
    """INF-51's core requirement, and the mutation proof for it: with a stubbed
    df reporting 93% the script must FAIL. Today's 10:08 run printed
    `Disk usage (after): 96%` and exited success -- nothing compared that
    number to anything."""
    env = _make_stub_env(tmp_path, DOCKER_WITH_IMAGES, disk_pct=93)
    result = _run_janitor(env)
    assert result.returncode != 0, result.stdout
    assert "93%" in result.stdout
    assert "== Done ==" not in result.stdout


def test_threshold_is_measured_after_the_prune_not_before(tmp_path):
    """A run that arrives at 95% and leaves at 40% is a janitor doing its job
    and must stay silent. Only the AFTER reading may decide."""
    env = _make_stub_env(tmp_path, DOCKER_WITH_IMAGES)
    bin_dir = tmp_path / "bin"
    counter = (tmp_path / "df.count").as_posix()
    # 95% on the first call (the "before" line), 40% on every later one.
    _stub_bin(
        bin_dir,
        "df",
        "#!/usr/bin/env bash\n"
        'n=$(cat "' + counter + '" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "' + counter + '"\n'
        'if [ "$n" -le 1 ]; then pct=95; else pct=40; fi\n'
        "echo 'Filesystem     1024-blocks     Used Available Capacity Mounted on'\n"
        'echo "/dev/sda1        164802308 96000000  60000000 ${pct}% /"\n',
    )
    result = _run_janitor(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "== Done ==" in result.stdout


def test_threshold_is_configurable_per_host(tmp_path):
    """`janitor.sh` is synced to every janitor-role host including the three
    prod boxes, whose disk sizes and churn differ from staging's -- one
    hardcoded number for all of them was a named risk in the WO."""
    env = _make_stub_env(tmp_path, DOCKER_WITH_IMAGES, disk_pct=93)
    env["JANITOR_DISK_THRESHOLD"] = "95"
    result = _run_janitor(env)
    assert result.returncode == 0, result.stdout + result.stderr


def test_prunes_are_no_longer_swallowed_by_or_true():
    """INF-51 requirement 4, structural half. Every command in this script used
    to end in `|| true`, so a genuinely failed prune was indistinguishable from
    a successful one."""
    script = JANITOR.read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for line in script.splitlines()
        if "prune" in line and line.strip().startswith("docker ") and "|| true" in line
    ]
    assert not offenders, "prune still guarded by || true: " + repr(offenders)


def test_diagnostic_lines_keep_their_or_true():
    """The other half, and the one easy to break while doing the first: INF-19
    added `|| true` for a real SIGPIPE abort on `docker images | head`.
    Removing it there re-breaks what that fix repaired."""
    script = JANITOR.read_text(encoding="utf-8")
    for diagnostic in ("docker system df -v", "docker images --format 'table"):
        lines = [
            ln for ln in script.splitlines()
            if diagnostic in ln and not ln.strip().startswith("#")
        ]
        assert lines, "diagnostic line vanished: " + diagnostic
        assert all("|| true" in ln for ln in lines), "diagnostic lost its guard: " + repr(lines)


def test_a_failing_prune_now_aborts_the_run(tmp_path):
    """Behavioural counterpart to the structural test above: with `|| true`
    gone, a prune that exits non-zero must stop the run."""
    docker = DOCKER_WITH_IMAGES.replace(
        "  *)\n    exit 0\n    ;;",
        "  container)\n    echo 'docker daemon hiccup' >&2\n    exit 1\n    ;;\n  *)\n    exit 0\n    ;;",
    )
    env = _make_stub_env(tmp_path, docker)
    result = _run_janitor(env)
    assert result.returncode != 0
    assert "== Done ==" not in result.stdout


def test_disk_reading_is_emitted_even_when_a_prune_aborts(tmp_path):
    """Named risk in the WO: removing `|| true` means a transient docker failure
    aborts the run BEFORE the diagnostics that would explain it."""
    docker = DOCKER_WITH_IMAGES.replace(
        "  *)\n    exit 0\n    ;;",
        "  container)\n    exit 1\n    ;;\n  *)\n    exit 0\n    ;;",
    )
    env = _make_stub_env(tmp_path, docker)
    result = _run_janitor(env)
    assert result.returncode != 0
    assert "Disk usage (after early exit)" in result.stdout, result.stdout


# --- INF-51 security review: adversarial values for the two tunables --------


@pytest.mark.parametrize("bad", ["abc", "3x", "-1", "0", "2.5", "1e3", " "])
def test_bad_retention_value_aborts_instead_of_deleting_everything(tmp_path, bad):
    """`${VAR:-default}` only rescues an UNSET or empty override; a non-empty
    nonsense value passes straight through to awk, which coerces it to 0 -- and
    `n[repo] > 0` is true for the FIRST image of every repository, silently
    turning "keep the newest 3" into "delete everything no container holds", on
    three production boxes, with no error. It must abort and remove nothing."""
    env = _make_stub_env(tmp_path, DOCKER_WITH_IMAGES)
    env["JANITOR_IMAGE_RETENTION"] = bad
    result = _run_janitor(env)
    assert result.returncode != 0, result.stdout
    assert "JANITOR_IMAGE_RETENTION" in result.stdout
    assert _removed(env) == set(), "images were removed despite an invalid retention count"


@pytest.mark.parametrize("bad", ["abc", "0", "101", "-5", "90%"])
def test_bad_threshold_value_aborts(tmp_path, bad):
    """The sibling tunable gets the same treatment, so the two behave alike
    rather than one failing loudly and the other silently."""
    env = _make_stub_env(tmp_path, DOCKER_WITH_IMAGES)
    env["JANITOR_DISK_THRESHOLD"] = bad
    result = _run_janitor(env)
    assert result.returncode != 0, result.stdout
    assert "JANITOR_DISK_THRESHOLD" in result.stdout


def test_validation_happens_before_anything_is_deleted(tmp_path):
    """Order matters: a bad config must be rejected before the prune, not
    discovered halfway through one."""
    env = _make_stub_env(tmp_path, DOCKER_WITH_IMAGES)
    env["JANITOR_IMAGE_RETENTION"] = "not-a-number"
    result = _run_janitor(env)
    assert result.returncode != 0
    assert "Docker container prune" not in result.stdout
    assert _removed(env) == set()


def test_valid_retention_and_threshold_still_run(tmp_path):
    """The validation must not reject the values actually in use."""
    env = _make_stub_env(tmp_path, DOCKER_WITH_IMAGES, disk_pct=40)
    env["JANITOR_IMAGE_RETENTION"] = "1"
    env["JANITOR_DISK_THRESHOLD"] = "100"
    result = _run_janitor(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "== Done ==" in result.stdout


@pytest.mark.parametrize("tunable", ["JANITOR_IMAGE_RETENTION", "JANITOR_DISK_THRESHOLD"])
def test_an_empty_override_falls_back_to_the_default(tmp_path, tunable):
    """An empty value is NOT nonsense -- `${VAR:-default}` rescuing it is the
    intended shell convention, and the validation must not turn a blank
    environment variable into a failed nightly run."""
    env = _make_stub_env(tmp_path, DOCKER_WITH_IMAGES, disk_pct=40)
    env[tunable] = ""
    result = _run_janitor(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "== Done ==" in result.stdout


def test_retention_order_survives_the_dst_fall_back(tmp_path):
    """Review finding R1. Docker's `{{.CreatedAt}}` is LOCAL time with the
    offset attached, and across the autumn fall-back the offset flips
    +0200 -> +0100. A lexicographic sort of that string ranks the bigger offset
    as newer, so the image built just AFTER the clock change looks older than
    one built just before -- and the retention cut keeps the wrong one. This
    fixture is that exact pair: `new` is chronologically later (02:30 +0100 =
    01:30 UTC) than `old` (02:30 +0200 = 00:30 UTC), while sorting their LOCAL
    renderings would invert them. Keeping 1, the NEW image must survive."""
    table = (
        "sha256:new 2026-10-25T01:30:00.0Z ghcr.io/x/dst:new\n"
        "sha256:old 2026-10-25T00:30:00.0Z ghcr.io/x/dst:old\n"
    )
    env = _make_stub_env(tmp_path, DOCKER_WITH_IMAGES)
    env["IMAGE_TABLE"] = table
    env["JANITOR_IMAGE_RETENTION"] = "1"
    result = _run_janitor(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _removed(env) == {"ghcr.io/x/dst:old"}


def test_a_multi_tagged_image_consumes_one_retention_slot_not_two(tmp_path):
    """Review finding R2. Retention is per distinct IMAGE, not per tag. An
    image carrying two tags in one repository used to eat two of the N slots,
    so "keep 3" silently meant "keep 2" and a genuinely older, distinct image
    was pushed out early."""
    table = (
        "sha256:m1 2026-08-20T09:00:00.0Z ghcr.io/x/multi:a ghcr.io/x/multi:b\n"
        "sha256:m2 2026-08-20T08:00:00.0Z ghcr.io/x/multi:c\n"
        "sha256:m3 2026-08-20T07:00:00.0Z ghcr.io/x/multi:d\n"
    )
    env = _make_stub_env(tmp_path, DOCKER_WITH_IMAGES)
    env["IMAGE_TABLE"] = table
    env["JANITOR_IMAGE_RETENTION"] = "2"
    result = _run_janitor(env)
    assert result.returncode == 0, result.stdout + result.stderr
    # Two distinct images kept (m1 with BOTH its tags, and m2); only m3 goes.
    assert _removed(env) == {"ghcr.io/x/multi:d"}


def test_all_tags_of_an_evicted_image_are_removed(tmp_path):
    """The other half of R2: once an image falls past the cut, every tag it
    holds must go, or the image survives by one of its names."""
    table = (
        "sha256:k1 2026-08-20T09:00:00.0Z ghcr.io/x/multi:keep\n"
        "sha256:k2 2026-08-20T08:00:00.0Z ghcr.io/x/multi:gone1 ghcr.io/x/multi:gone2\n"
    )
    env = _make_stub_env(tmp_path, DOCKER_WITH_IMAGES)
    env["IMAGE_TABLE"] = table
    env["JANITOR_IMAGE_RETENTION"] = "1"
    result = _run_janitor(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _removed(env) == {"ghcr.io/x/multi:gone1", "ghcr.io/x/multi:gone2"}


def test_registry_port_in_the_repository_name_is_not_split_on(tmp_path):
    """`localhost:5000/app:tag` -- the repository is everything before the LAST
    colon, so a registry port must not be mistaken for the tag separator."""
    table = (
        "sha256:p1 2026-08-20T09:00:00.0Z localhost:5000/app:t3\n"
        "sha256:p2 2026-08-20T08:00:00.0Z localhost:5000/app:t2\n"
        "sha256:p3 2026-08-20T07:00:00.0Z localhost:5000/app:t1\n"
    )
    env = _make_stub_env(tmp_path, DOCKER_WITH_IMAGES)
    env["IMAGE_TABLE"] = table
    env["JANITOR_IMAGE_RETENTION"] = "2"
    result = _run_janitor(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _removed(env) == {"localhost:5000/app:t1"}
