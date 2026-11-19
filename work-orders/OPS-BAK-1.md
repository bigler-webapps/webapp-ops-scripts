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

To be filled by the Orchestrator.

# C. Orchestrator only — NOT ADDRESSED TO THE IMPLEMENTER

To be filled by the Orchestrator.

## Gate record
