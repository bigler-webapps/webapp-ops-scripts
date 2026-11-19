# OPS-BAK-1 — Back up production volume data, fail loudly on gaps, prove restore

Prepared: 2026-11-19. Target repositories: `webapp-ops-scripts` (`main`) and `webapp-management` (`main`, Ansible). Origin: Cinevia go-live checklist, point 1 (backup and restore).

# A. Envelope — addressed to everyone

## Goal and expected outcome

The nightly backup (`webapp-management` `backup.yml` -> `backup.py` + `generate_paths.py` in this repository) stores the files that production apps keep in Docker volumes (uploads, photos, receipts, Excel files, stored assistant chats), not only the database dumps, on every managed production host. A volume that is selected for backup but cannot be read turns that host's backup run red instead of being skipped silently. A dated restore drill proves that a database and a media volume can be recovered from the off-site (B2) snapshot.

## Why

Measured 2026-11-19 by the operator with `sudo -u deploy python3 /srv/infrastructure/ops-scripts/generate_paths.py --infra-paths-file /dev/null`: on mhaas-prod and main-prod every non-database volume is reported as `Skipping unreadable path: /var/lib/docker/volumes/<name>/_data`. The script runs as `deploy`, which cannot read the root-owned `/var/lib/docker/volumes`. `backup.py` reduces the skips to one `WARN: generate_paths.py reported N warnings` line and the run stays green (run 44506158660: main-prod 2 paths / 84 warnings, mhaas-prod 2 / 11, research-prod 1 / 14, innoservice-prod 1 / 5). Database dumps are unaffected: they are taken via `docker exec pg_dump`, stored in B2 and checked with `gzip -t` daily. `restore.yml` has no successful run in the visible history (failures 2026-02-08 and 2026-08-10).

## Size basis (operator measurement 2026-11-19, `docker system df -v`)

Selected volumes per decision 2, mounted by a running production container:

| Host | Volumes | Size |
|---|---|---|
| mhaas-prod | `photogallery_production_media_volume` 11.63 GB, `kira_production_media_volume` 356.7 MB, `gustav_production_media_volume` 2.8 MB, `cinevia_production_media_volume` 0 B | ~12.0 GB |
| main-prod | `cockpit_production_open_webui_data` 1.117 GB, `reimbursements_production_media_volume` 63 MB, further production media volumes of 0–5 MB each (`bigler-consult`, `cockpit`, `spesix`, `webshop-guenter`, `jg-webapp`, `survey_contact_app`, `survey_app`, `fitness-monitor`, `queue`), `cockpit_production_assistant_cli_sessions` 0 B | ~1.2 GB |
| innoservice-prod | `innoservice_prod_media_volume` 436.7 MB | ~0.4 GB |
| research-prod | `hram_production_media_volume` 2 MB, `hram_production_excel_volume` 36.5 kB, `maana_production_media_volume` 1.6 MB, `hpc-bridge_production_media_volume` 1.6 MB, `hram_production_assistant_cli_sessions` 0 B | ~4 MB |
| **Total** | | **~13.6 GB** |

## Tier

Tier 3, infra lane: production backup on every production host.

## Operator decisions this Envelope records (2026-11-19)

1. Read access: the `restic` binary on each backed-up host gets the Linux capability `cap_dac_read_search`, set through Ansible. It may read every file and write none. The backup keeps running as `deploy`, with no new sudo rule and no root process.
2. Selection: only user data of production. Included are volumes mounted by a running production container that hold data users or the app create (uploads/media, Excel files, stored assistant chats such as `open_webui_data` and `assistant_cli_sessions`). Excluded are: hram's `pipeline_artifacts` and `calibration_outputs` volumes (reproducible, operator 2026-11-19); caches and reproducible content (build caches, downloaded models, beat schedules, log positions); secrets volumes (secrets come from Proton via `sync-secrets`); anonymous hash-named volumes; staging/stage volumes; volumes no running container mounts. The selection is declared and readable in the repository, not derived from a deny list of name fragments alone.
3. Retention: volume data uses the existing retention (7 daily, 4 weekly, 6 monthly). No separate policy.
4. Size gate: before the first backup with volume data on a host, the selected size on that host is measured; if the total across hosts exceeds the size basis above by more than 25 % (about 17 GB), stop and report before enabling.
5. The restore drill is part of this order: the Cinevia production database and one non-empty production media volume (Cinevia's is empty today; use Photogallery's or Kira's) are restored from the newest B2 snapshot into a throwaway target, never into a live volume or database. Result and date are recorded in this WO.

## Prerequisite (operator, outside the repositories)

The B2 buckets `mhaas-backup` and `innoservice-backup` show the lifecycle "Keep all versions" (screenshot 2026-11-19); `webapp-backup-micha` shows "Keep prior versions for days". Under "Keep all versions" the files restic's retention removes stay as hidden versions and keep being billed, so the retention above does not bound storage. Before volume data is enabled on mhaas-prod and innoservice-prod, the operator sets those two buckets to the same lifecycle as `webapp-backup-micha`. The buckets are not managed in any repository.

## Scope

1. Selection per decision 2, applied on every host in the backup matrix.
2. Read access per decision 1 for the selected paths.
3. Gap detection: a selected volume that is missing or unreadable fails the host's backup job with a message naming the volume. The warning-only path is removed for selected volumes.
4. The backup summary reports the number and total size of volume paths backed up per host, so a future gap is visible in the log.
5. Size gate per decision 4.
6. Restore drill per decision 5, with the restored data checked (database: the dump loads and key tables have rows; media: file count and total size match the source volume at snapshot time).
7. The per-host include list as finally applied and the B2 bucket size per host before and after the first run with volume data are recorded in the Completion section.

## Non-goals

- No change to database dump logic, retention values, B2 buckets (beyond the operator prerequisite) or restic repository layout.
- No repair of `restore.yml` / `restore-cross-server.yml`; their failing state is recorded as a finding if the drill does not use them.
- No deletion or cleanup of stale volumes (e.g. `hram-webapp_*`, `infra_prod_*`, `template_prod_*`, unmounted `hram_production_*` and `innoservice_prod_*` on main-prod); they are listed as a finding.
- No change in any app repository and no backup change on the staging host.

## Risks

- The first run with volume data uploads ~13.6 GB at once (mostly Photogallery on mhaas-prod), and the local restic repository on each host grows by the same amount. Later runs add only changed data.
- `cap_dac_read_search` lets any process running the `restic` binary read every file on the host. `deploy` already has root-equivalent access through the Docker socket, so this widens nothing for `deploy`; the capability must not be granted to any other binary.
- A capability is lost when the binary is replaced (package update). The Ansible role reapplies it, and scope item 3 turns a lost capability into a red run instead of a silent gap.
- Files changing during the backup (uploads in progress) may be captured in an inconsistent state; acceptable for media.

## Required tests to write

- Selection: given a fixture volume and container list, only declared production user-data volumes that a running container mounts are selected; hram pipeline artifacts and calibration outputs, caches, models, secrets, anonymous, staging and unmounted volumes are not.
- Gap detection: a selected volume that is unreadable or missing makes the backup step exit non-zero with the volume named; an unselected unreadable volume does not.
- Summary: the log line reports count and total size of backed-up volume paths.
- Size gate: a selected total above the threshold stops before any upload.
- Ansible: the role sets `cap_dac_read_search` on the restic binary only, and a second apply changes nothing.

# B. Implementation map — addressed to the implementer

Infra lane: implemented by the Orchestrator (no dispatched implementer). Seams: `backup_volumes.json` (declared selection), `volume_selection.py`, `generate_paths.py`, `backup.py` (`check_volume_size_gate`, `report_volume_gaps`, summary), `test_volume_backup.py` here; `ansible/roles/srv_dirs` and `ansible/tests/test_ops_bak_1_restic_capability.py` in webapp-management.

# C. Orchestrator only — NOT ADDRESSED TO THE IMPLEMENTER

Review round 1 and closing round 2: codex/gpt-6.1-terra (envelope, regression, tests, sec_review) and codex/gpt-6-luna (duplication) on the webapp-ops-scripts diff; Ansible diff by a separate Claude sec_reviewer (sonnet) in both rounds.

## Completion

Status 2026-11-19: code landed, WO stays in-review. Not done (need prod/B2 access, which agents lack): restore drill (decision 5), real size measurement and B2 bucket sizes before/after (scope 7), Ansible apply on the prod hosts.

- Prerequisite met: operator 2026-11-19 "Lifecycle ist auf 30 Tage umgestellt" - `mhaas-backup` and `innoservice-backup` keep prior versions for 30 days.
- Built: declared selection in `backup_volumes.json` (include patterns for media/excel/assistant-session/open_webui volumes, excludes for pipeline_artifacts, calibration_outputs, caches, models, secrets, staging; only volumes a RUNNING container mounts). `generate_paths.py` emits volume paths without stat and writes a report (name, path, bytes; sizes from `docker system df -v`). `backup.py`: size gate before dumps/uploads, `VOLUME GAP: <name>` on a restic failure naming a selected path, `VOLUMES_BACKED_UP=<n> bytes=<b>` and summary line. Ansible `srv_dirs`: `cap_dac_read_search+ep` on `/usr/bin/restic` only, prod hosts only, exact-match guard, regular-file assert, post-set verify.
- Applied include list: the patterns above on every host that runs backup.py; no per-host list. An earlier `enabled_hosts` gate was removed after review (it backed up nothing until enabled, and a hostname mismatch would be a silent gap).
- Deviation: the size gate is per host (17 GB, basis 13.6 GB + 25 %); a host cannot see the others, so the combined-across-hosts check remains the operator's measurement before the first run. `ALLOW_VOLUME_FAILURE` no longer hides a failed volume selection.
- First-run caveats (unverified): the `docker system df -v --format json` Size parsing and restic's error text naming the path were not run against a real host; a parse failure turns the run red, never green. The first run uploads about 13.6 GB (mostly Photogallery on mhaas-prod).
- Review: 6 raised in total after dedup, accepted and fixed: enabled_hosts gate, report in /tmp, ALLOW_VOLUME_FAILURE, volumes dropped when Docker cannot resolve them, Ansible guard/check-mode/symlink/verify, test weaknesses. Rejected: combined-size gate at runtime (not possible), non-production-container check (names carry production), stopped containers (Envelope says running only), dedicated restic copy (operator decision 1). Carried nits: Ansible assert skipped in check mode.
- Later changes 2026-11-19 (operator): Photogallery volumes (`photogallery_*`) are excluded from the volume backup (operator request; about 11.6 GB less, mhaas-prod now about 0.4 GB). `jg-webapp_media_volume` (legacy name, 4 MB on main-prod) added to the include list: the operator confirmed with `docker ps --filter volume=...` that `jg_production-backend-1` mounts it, so it is production user data. Ansible provision dispatched per prod host after the operator's approval ("Ja, jetzt einspielen"); main-prod green (run 44533315351).
- Ansible provision applied on all four prod hosts 2026-11-19, all green: main-prod 44533315351, mhaas-prod 44534300428, innoservice-prod 44535361967, research-prod 44536456097 (the research-prod log shows the capability tasks incl. the verify task ran).
- First live evidence (operator-run manual backup.yml, mhaas-prod, run 44545676426, 2026-11-19 14:37 UTC): green. "Volumes selected: 3 paths, 0.36 GB", VOLUMES_BACKED_UP=3 bytes=359532000, dumps 4/4, local snapshot e44d50d4, B2 snapshot 35fcd3d5, verify SUCCESS (dumps only). generate_paths reported 1 warning; its text is not in the log without DEBUG=1 and was not inspected (volumes no longer emit "Skipping" lines, so it is most likely an infra-paths entry; unverified). Open: restore drill (Cinevia DB plus Kira media from B2 snapshot 35fcd3d5 or later), first runs of main-prod, innoservice-prod and research-prod, B2 bucket sizes before/after.
- Restore drill (decision 5, scope addition approved by the operator 2026-11-19, "Neuer Workflow"): the existing `restore-drill.yml` imports into the staging DB and handles dumps only, so a new workflow `restore-proof.yml` (+ `.github/scripts/restore_proof.py`, tests) was added in webapp-management (33a9bd5c). It restores into runner tmp and an ephemeral postgres:18 only, every restic call read-only and `--no-lock`. Review: Claude sec_reviewer + reviewer (sonnet) in round 1, sec_reviewer closing round; all accepted findings fixed (psql output no longer logged, psql minimal env, meta-command guard, line-anchored roles, restic ls limited and recursive, summary on failure). Carried: dump read as text (non-UTF8 fidelity), restic apt version unpinned, meta-command guard is best-effort, B2 key scope and environment protection are operator settings (the workflow would be safer with a read-only B2 key).
- RESULT, run 44547953641, 2026-11-19 14:50 UTC, mhaas-prod, snapshot 35fcd3d5 (host mhaas-prod): PASS. Cinevia production dump `cinevia_production-db-1_cinevia_2026-11-19T143736Z.sql.gz` loaded into the throwaway DB: 83 tables, 29 with rows, key table `django_migrations` 95 rows. Kira media volume: snapshot 136 files / 356698559 bytes, restored 136 files / 356698559 bytes (equal). Nothing live touched, nothing written to B2.
- Still open: first runs of main-prod, innoservice-prod and research-prod (volume selection there unverified live), B2 bucket sizes before/after. After those the WO can be set done.
- Findings for the register: stale volumes not touched (non-goal); `restore.yml` not repaired.

## Gate record

Gate #1 approved by the operator 2026-11-19 (chat message "Beide freigeben", covering OPS-BAK-1 and DCM-SEC-5).
