# OPS-BAK-5 — hram staging-set snapshot in the research-prod backup (part of WM-INF-165)

Pointer stub. The authoritative Envelope, Part B and Gate record live in `webapp-management/work-orders/WM-INF-165.md`.

This repository's part: the backup program writes a separate, size-capped, tagged staging-set snapshot of hram `pipeline_artifacts` (selected `infrastructure`/`runs` entries plus `jobs`, `scenarios`, `data`). A selector failure, an empty selection or an exceeded cap skips only that snapshot and reports a fixed code. The disaster-recovery include/exclude rules and the per-host size gate stay unchanged. Reviewed per its own WO ID.

## Completion

Tier 3, infra lane, chunk 1 of WM-INF-165. RAN: codex/gpt-6.1-terra effort medium (implementation, one blocker fix dispatch, two defect fix dispatches).

Built: `staging_set_snapshot.py` (new) and its hook in `backup.py`, run on `research-prod` with a B2 repository after both regular backups, never failing them. Separate restic snapshot, tag `hram-staging-set`, `infrastructure`/`runs` entries named by `staging_artifact_selection` (run via docker exec in the production backend) plus `jobs`, `scenarios`, `data`; keeps the last 3 (grouped by host and tag), 10 GB cap measured with `du` in the backend, timeouts on every external call. Fixed verdict codes: `selector_failure`, `empty_selection`, `size_exceeded`, `unsafe_path`, `snapshot_failure`, `ok bytes=<n>`, logged as `STAGING_SET=<code>`. Host-side guard rejects missing paths, symlink components and paths resolving outside the volume `_data` root before anything is read (narrows, cannot remove, a time-of-check/time-of-use window). Disaster-recovery include/exclude rules, the per-host size gate and `backup_volumes.json` unchanged (tested).

Orchestrator findings, fixed before review: `verify_backup.py` and `restore.py` selected the newest snapshot of the host with no tag filter and would have chosen the staging-set snapshot (nightly verification failing, disaster-recovery restore using the wrong set). Both now filter on tag `daily` and refuse an explicit staging-set snapshot id; `backup.py` retention for daily snapshots is `--tag daily`. Part B: errors: omitted that snapshot consumers (`verify_backup.py`, `restore.py`) and the webapp-management receiver copy list and pinned commit are affected, found by the Orchestrator.

Tests (WSL, after the last edit): `ops-scripts` scope PASS 342, 1 skipped (pre-existing). Not host verified; the receiver in webapp-management still installs the previous pinned commit, so nothing runs on research-prod yet.

Review (infra lane, 2 rounds, closing pass 2): round 1 raised 7 (envelope 2, regression 1, tests 4), round 2 added 3 new (tests 2, sec_review 1: symlink escape). 10 raised, 10 accepted, 8 fixed, worst accepted: defect. Carried 2 (envelope lens, both rounds): the `daily` filter in restore, verification and retention ignores any disaster-recovery snapshot that has no `daily` tag; the tag has been set by `backup.py` since its first commit in this repo, so only snapshots from before that extraction could be untagged. duplication: no findings; sec_review round 1: none.
