# WM-OPS-7 — backup verification fails on research-prod because the dump filename is stamped once per run

Register row: `webapp-management/WORK_ORDERS.md` (this repo's work orders are indexed there;
`OPS-*` stream). Repo: `webapp-ops-scripts`.

# A. Envelope

## Goal & expected outcome

- Ziel: the nightly backup verification must judge dump freshness by when the dump was actually
  written, not by when the backup run started.
- Expected outcome: `Backup, Verify & Sync` is green on all five targets while the backups are in
  fact fresh and valid, and the gzip integrity check actually runs on research-prod's dumps again.
  A genuinely stale or missing dump must still fail the target.

## Why this is not cosmetic

`Backup, Verify & Sync` fails on the `research-prod` target every night since 2026-09-20 (green
through 2026-09-19). The **backup itself succeeds** — local snapshot, B2 snapshot, both retentions:

```
[08:18:38] Local: backup OK (snapshot=c2ce5bd3)
[08:19:02] B2: backup OK (snapshot=a06db300)
[08:19:08] SUMMARY: dumps ok, local=c2ce5bd3, b2=a06db300
[08:19:08] == Backup Success ==
```

Then verification rejects it:

```
-> Snapshot Date:      2026-09-21T08:18:42Z
-> Filtering for files within 0.2h of snapshot time
   Window: 2026-09-21 08:06:42 UTC  ..  08:23:42 UTC
-> Found 0 matching dumps out of 5 .sql.gz files.
[CRITICAL FAILURE] No matching .sql.gz dumps found for this snapshot window.
```

**Root cause.** `verify_backup.py:264` filters the snapshot's `.sql.gz` files by a timestamp parsed
out of the **filename** (`parse_timestamp_from_filename`, line 57), within
`MAX_SNAPSHOT_WINDOW_HOURS` (default `0.2`, i.e. 12 minutes) before the snapshot time.
`backup.py:216` takes **one** `utc_timestamp_str()` for the whole run, before the first dump
starts, and every dump of that run carries it (`backup.py:224`).

research-prod's hram database dump is now ~1.8 GB and takes about 12 minutes on its own. By the
time the B2 snapshot exists, the run-start timestamp in the filename is 12.3 to 12.6 minutes old
and falls outside a 12-minute window:

| date | dump timestamp | snapshot | window opens | missed by |
|---|---|---|---|---|
| 2026-09-20 | 07:52:14 | 08:04:30 | 07:52:30 | 16 s |
| 2026-09-21 | ~08:06:04 | 08:18:42 | 08:06:42 | 38 s |

This is a slow-growth threshold crossing, not a sudden fault, and the margin is still shrinking as
the database grows.

**What is actually lost.** Not the backup — the snapshots exist and the dumps are almost certainly
intact. What is lost is the **gzip integrity verification**, which now never runs on research-prod,
and the ability to tell this failure apart from a real one: the target job is red either way, so a
genuine backup failure on that host would now look exactly like this.

## Scope + non-goals

- In scope: making the freshness judgement reflect when each dump was written. The direction the
  Envelope asks for is a **per-dump timestamp** — stamped at the dump's completion, per file —
  rather than one timestamp for the run.
- In scope: whatever `verify_backup.py` needs so that it keeps working for dumps written by a
  previous version of `backup.py` (the snapshot it verifies may contain older files; the failing
  log shows 5 `.sql.gz` in one snapshot).
- Explicit non-goals / do-not-touch:
  - **Do NOT solve this by widening `MAX_SNAPSHOT_WINDOW_HOURS`.** That treats the symptom and
    moves the cliff to the next few GB of database growth. It is explicitly rejected.
  - Do NOT set `VERIFY_ALL_SQL_GZ=1` as the fix. Verifying every `.sql.gz` in the snapshot removes
    the freshness check altogether — the one property this script exists to assert.
  - Do NOT weaken or bypass the failure path. A snapshot with no fresh dump must still fail.
  - Do NOT change what gets backed up, the restic repositories, the retention policy, or
    `generate_paths.py`.
  - Do NOT touch `janitor.sh` (that is WM-OPS-8).
  - Do NOT change the backup workflow in `webapp-management`. If a change there turns out to be
    required, stop and report it — it is a different repo and a separate WO.

## Tier · precondition / gate

- Tier: **3** — backup and the verification that watches it. A fault here is silent: it does not
  break the backup, it removes the ability to notice that the backup broke.
- Precondition: none in this repo. Note the deployment path: these scripts are synced to
  `/srv/infrastructure/ops-scripts/` on each target by the backup workflow's sync step, so a
  change lands on the hosts the next time that workflow runs.

## Risks

- **The filename is a contract between two scripts.** `backup.py` writes the name,
  `verify_backup.py` parses it, and `restore.py` may also consume it — check before changing the
  shape. A rename that only one side understands makes verification silently pass or silently fail
  on everything.
- `parse_timestamp_from_filename` already accepts two historical formats
  (`...T103000Z` and `...T10:30:07Z`). Whatever is added must not break either, because a snapshot
  can hold files written by older versions.
- The dump filename is built from container/database names (`backup.py:223`). Do not make the
  timestamp position ambiguous against a database name that could itself contain the separator.
- `test_backup.py` and `test_verify_backup.py` already exist in this repo; the change must not
  leave them asserting the old single-timestamp behaviour as if it were still correct.

## Required tests to WRITE

In this repo's existing `unittest` style, beside `test_backup.py` / `test_verify_backup.py`:

1. A dump written at the END of a long run is judged fresh: simulate the measured case —
   run starts, a dump completes ~12.5 minutes later, snapshot taken right after — and assert the
   dump is selected as a candidate. **This must fail against the current code.**
2. Multiple dumps from the same run with different completion times are each judged on their own
   time.
3. A genuinely stale dump (well outside the window) is still rejected — the failure path must be
   shown to work, not assumed.
4. A snapshot containing only older-format filenames still parses (both legacy formats).
5. Round-trip: a filename produced by `backup.py` is parseable by
   `verify_backup.parse_timestamp_from_filename`. This is the contract that broke; assert it
   directly rather than testing each side in isolation.

---

# B. Implementation map — filled by the Orchestrator at dispatch time

PLACEHOLDER — not yet filled. This WO is registered, not dispatched. Do not invoke an implementer
on this file while this line is present.

---

# C. Orchestrator only

> **Implementer: STOP at this line.**

## Review routing

Tier 3: all four `review` lenses. No frontend, no auth surface. The Orchestrator's own pass covers
the backup/restore filename contract across all three consumers.

## Verification

1. This repo's `test_backup.py`, `test_verify_backup.py` and the new tests — the affected set.
2. The mutation check on test 1: it must go red against the current single-timestamp code.
3. A real `verify_backup.py` run against research-prod's existing B2 snapshot, read-only, before
   relying on the next scheduled run. Do NOT dispatch `backup.yml` to test — it is a
   prod-mutating workflow and needs the operator to name workflow and inputs.
4. Read the first scheduled run after the scripts sync to the hosts, on all five targets, not just
   research-prod: the window logic applies everywhere and the other four are green today.
