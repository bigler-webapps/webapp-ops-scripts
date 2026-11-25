# OPS-BAK-2 — Longer backup retention with deduplicating database dumps

Prepared: 2026-11-19. Target repository: `webapp-ops-scripts` (`main`). Prerequisite: OPS-BAK-1 done (volume backup, gap detection, restore drill, B2 bucket lifecycle).

# A. Envelope — addressed to everyone

## Goal and expected outcome

Backups can be reached further back: restic keeps 14 daily, 8 weekly and 12 monthly snapshots on every host instead of 7 / 4 / 6. To keep the extra storage small, database dumps are written in a form restic can deduplicate between days, so a retained snapshot no longer carries a nearly complete copy of every dump.

## Why

Today every dump is a plain `gzip` stream (`backup.py` `perform_db_dumps`). A small change in the database changes almost the whole compressed file, so restic stores each daily dump nearly in full. Doubling the retained snapshots would then roughly double the dump share of each bucket. `gzip --rsyncable` keeps the `.sql.gz` format, the `gzip -t` integrity check and every reader unchanged, while letting unchanged parts of the dump compress to identical blocks.

## Tier

Tier 3, infra lane: production backup on every production host.

## Operator decisions this Envelope records (2026-11-19)

1. Retention 14 daily, 8 weekly, 12 monthly, the same for all hosts and for database dumps and volume data.
2. Dumps keep the file name pattern and the `.sql.gz` format; the change is the rsyncable compression mode. No change of dump format (plain SQL), no `pg_dump -Fc`.
3. Order: first measure, then extend. The dump change lands first; the retention change lands only after the gain is measured over at least 7 nightly runs on every host and recorded in this WO.

4. Added 2026-11-25 at the operator's request (from the WM-INF-138 canary, where the research-prod cycle ran over 33 minutes): the reproducible result tables of the HRAM production database are no longer backed up with data. Measured on research-prod, read-only, 2026-11-25: `hram_db` is 26 GB; `results_teachingreferralflow` is 21 GB and `results_teachingtreatmentoutcome` 0.98 GB; the audit log table is 1.9 GB and `runs_run` 0.4 GB, everything else is below 0.1 GB. The dump keeps the table definitions and skips the data of exactly the tables the operator confirms. Open before the build, both to be confirmed in this WO: (a) the exact table list (the two tables above, not the audit log, not `runs_run`); (b) the HRAM expert confirms the results can be recomputed after a restore and which restore tests expect those tables to be filled.

## Scope

0. Per decision 4, the per-database list of tables whose data is excluded from the dump (a reviewed declaration read by `perform_db_dumps`, `pg_dump --exclude-table-data`), the pin move on the receivers (`webapp-management`) and the restore, refresh and upgrade consumers listed in scope item 1 keep working with empty excluded tables. Measure the dump size and cycle time on research-prod before and after, and record the numbers in the Completion section; do this before the 7-night retention measurement of decision 3, so that its baseline is the reduced dump.
1. Dumps are written with rsyncable compression; the output stays a valid gzip stream readable by every existing consumer (`verify_backup.py`, `restore.py`, the restore actions in `workflow-templates`, `restore-drill.yml`, `restore-cross-server.yml`, `pg-upgrade.yml`, the `stack_recovery` and `dbrecv` roles in `webapp-management`).
2. Measurement: B2 bucket size and the data added per run (restic's "Added to the repository") before and after the dump change, per host, recorded in the Completion section.
3. Retention per decision 1, applied after scope item 2.
4. A search of every app's privacy statement for a stated backup retention period; each hit that conflicts with 12 months is reported as a finding with its location.

## Non-goals

- No change to dump consumers, volume selection, restic repository layout or B2 bucket settings.
- No edit of any app's privacy statement in this order.

## Risks

- `gzip --rsyncable` needs a gzip that supports it on each host (or an equivalent in the Python dump path); a host without support must fail the run, not silently fall back.
- Rsyncable output is slightly larger per file; acceptable against the deduplication gain.
- Longer retention keeps deleted personal data in backups for up to 12 months; scope item 4 surfaces any statement that promises less.

## Required tests to write

- A dump written by the new path passes `gzip -t` and decompresses to the same SQL as the old path.
- Two dumps of databases that differ in one row share most restic-relevant blocks (or, at the unit level, the compression is invoked in rsyncable mode).
- The `forget` command carries 14 / 8 / 12.
- A host whose gzip lacks rsyncable support fails the dump step with a clear message.

# B. Implementation map — addressed to the implementer

To be filled by the Orchestrator.

# C. Orchestrator only — NOT ADDRESSED TO THE IMPLEMENTER

To be filled by the Orchestrator.

## Gate record
