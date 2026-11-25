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

4. Added 2026-11-25 at the operator's request (from the WM-INF-138 canary, where the research-prod cycle ran over 33 minutes): the reproducible result tables of the HRAM production database are no longer backed up with data. Measured on research-prod, read-only, 2026-11-25: `hram_db` is 26 GB; `results_teachingreferralflow` is 21 GB and `results_teachingtreatmentoutcome` 0.98 GB; the audit log table is 1.9 GB and `runs_run` 0.4 GB, everything else is below 0.1 GB. The dump keeps the table definitions and skips the data of exactly the tables the operator confirms. Confirmed by the operator in chat on 2026-11-25: (a) the exact table list is the two tables above (not the audit log, not `runs_run`); (b) the operator states that these results are reproducible, and the step was pulled forward for the WM-INF-138 canaries ("ja, mach die Aenderung"). Still to be recorded in the Completion: which restore tests, if any, expect those tables to be filled (the HRAM expert may add to this), and the measured dump size and cycle time before and after. The receivers keep the previous script commit until `webapp-management` moves its pin and installs `backup_db_policy.json` with it.

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

## B. Implementation map

- `backup.py` (`perform_db_dumps`, the `pg_dump` argv at the `docker exec` call, `discover_docker_targets`) — the dump path; `backup_volumes.json` and `volume_selection.py` — the existing declared-policy pattern and its strict validation, read from the file next to the script.
- Existing tests to follow: `test_backup.py`, `test_volume_backup.py`, `test_verify_backup.py`.
- Consumers that must keep working with empty excluded tables: `verify_backup.py`, `restore.py`, `stack_recovery.py` (read only; no change planned).
- Environment: the dump target is the container `hram_production-db-1`, database `hram_db`, on research-prod; the scripts run on the hosts as `deploy` with the standard library only; `webapp-management` installs the script files from a pinned commit (not part of this dispatch).
- Scope of this first dispatch: the exclusion of the data of exactly two tables (see Part A decision 4); no compression change, no retention change.

## Preamble

> You are the dispatched implementer, not the Orchestrator. Your first tool action is
> `codex-hook-canary-ws-gov-60`; continue on the expected denial and report unverified coverage
> once if it is not denied. Read the nearest governance, memory and applicable skill.
> Own only the paths in the dispatch state. Preserve parallel work. Edit source and tests and run
> only your own new tests. Do not access hosts, credentials or the Tailnet console, dispatch jobs,
> run reviewers or workers, edit the register, or write to git.
> Report progress and end with `RESULT: DONE` or `RESULT: BLOCKED`, naming test evidence.

# C. Orchestrator only — NOT ADDRESSED TO THE IMPLEMENTER

To be filled by the Orchestrator.

## Gate record
