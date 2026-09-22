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

# B. Implementation map — ADDRESSED TO THE IMPLEMENTER

## Context package

**Named files to change:** `backup.py` (repo root), function `perform_db_dumps()` (currently
lines ~202-260+); `verify_backup.py` (repo root), function `parse_timestamp_from_filename()`
(lines ~57-80) and the filtering loop in `main()`/its caller (lines ~230-270).

**Current shape in `backup.py` — verified against the landed code:**

```python
def utc_timestamp_str(now: Optional[datetime.datetime] = None) -> str:
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    return now.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")

def perform_db_dumps() -> None:
    ...
    timestamp = utc_timestamp_str()          # <-- ONE timestamp for the whole run
    targets = discover_docker_targets()
    ...
    for t in targets:
        safe_name = t["name"].replace("@", "_").replace("/", "_")
        outfile = DUMP_DIR / f"{safe_name}_{t['db']}_{timestamp}.sql.gz"   # every dump reuses it
        tmpfile = outfile.with_suffix(outfile.suffix + ".tmp")
        ... pg_dump piped into gzip into tmpfile ...
        ok, _, stderr = run_cmd(["gzip", "-t", str(tmpfile)])
        ...
        os.replace(tmpfile, outfile)
        os.chmod(outfile, 0o600)
```

**The fix:** the outfile name (hence its embedded timestamp) must be decided AFTER the dump +
gzip-integrity-check succeed, not before the loop starts. Concretely:

1. Build `tmpfile` from a name that does NOT embed the final timestamp — e.g.
   `DUMP_DIR / f"{safe_name}_{t['db']}.sql.gz.tmp"` (adjust if two targets could share
   `safe_name`+`db` — check `discover_docker_targets()` for whether that pair is already unique
   per target; if not, add the container id/index to the tmp name only, it never reaches the final
   filename or the contract).
2. Run the dump + gzip pipe into that tmpfile exactly as now.
3. Only once `gzip -t` on the tmpfile has passed (i.e. right where `os.replace` is currently
   called), call `utc_timestamp_str()` **again, per dump**, to get that dump's OWN completion
   timestamp, build `outfile = DUMP_DIR / f"{safe_name}_{t['db']}_{completion_timestamp}.sql.gz"`
   at that point, then `os.replace(tmpfile, outfile)`.
4. Remove the single pre-loop `timestamp = utc_timestamp_str()` call (or keep it unused only if
   something else in the function still needs a run-level timestamp for logging — check before
   deleting; if it's only used for the filename, delete it).
5. `utc_timestamp_str()` itself is correct and untouched — it already returns a real UTC instant.
   You are changing WHEN it's called (per dump, at completion), not what it computes.

**Current shape in `verify_backup.py`:**

`parse_timestamp_from_filename(path)` splits on `_` and takes the last part as the timestamp —
this is unaffected by the backup.py change (the filename SHAPE is unchanged:
`<name>_<db>_<timestamp>.sql.gz`, only which timestamp value ends up there). The filtering loop
in `main()`:

```python
min_time = snap_time - timedelta(hours=MAX_SNAPSHOT_WINDOW_HOURS)
max_time = snap_time + timedelta(minutes=5)
...
for f in all_files:
    ts = parse_timestamp_from_filename(f)
    if ts and (min_time <= ts <= max_time):
        candidates.append(f)
```

This logic itself needs NO change once each dump's filename carries its own completion time: a
dump that finished 12 minutes into a run will now be timestamped at ~08:18 (its own completion),
not ~08:06 (the run start), and `08:18` falls inside a window computed against the ~08:18 snapshot
time. **Do not widen `MAX_SNAPSHOT_WINDOW_HOURS` or change `min_time`/`max_time` math** — the
Envelope explicitly rejects that; the fix is entirely on the `backup.py` side. If, after making the
per-dump change, you find `verify_backup.py` still cannot pass for some remaining reason, STOP and
report it as `RESULT: BLOCKED <reason>` rather than loosening the window — that would be exactly
the rejected fix.

**Invariants / do-not-touch / pitfalls:**

- The filename SHAPE (`<name>_<db>_<timestamp>.sql.gz`) must stay exactly parseable by
  `parse_timestamp_from_filename` in both existing formats it already accepts
  (`...T103000Z` and legacy `...T10:30:07Z`) — you are not introducing a new format, just changing
  which instant fills the same slot.
- `restore.py` may also parse this filename shape — grep this repo for other consumers of the
  `.sql.gz` naming before assuming only these two files care.
- Do not touch `janitor.sh` (WM-OPS-8's surface) or `generate_paths.py`.
- Do not change `VERIFY_ALL_SQL_GZ`, the restic repos, or retention policy.
- Two dumps in the same run now legitimately get two DIFFERENT timestamps (previously identical) —
  this is intended; do not "fix" it back to one shared value.
- Nothing in `webapp-management` is in scope. If a change there turns out to be required, stop and
  report it rather than editing that repo.

## Required tests to WRITE

In this repo's existing `unittest` style, beside `test_backup.py` / `test_verify_backup.py` — the
five cases already listed in Part A's "Required tests to WRITE" section above. In particular:
- Case 1 (a dump finishing ~12.5 min into a run is judged fresh) **must fail against the current
  single-timestamp code** — write it against the CURRENT `perform_db_dumps`/timestamp logic first
  and confirm it goes red, to prove the regression is real, before making your fix.
- Case 5 (round-trip: a filename `backup.py` produces is parseable by
  `verify_backup.parse_timestamp_from_filename`) exercises both files together — import both
  modules in the one test.

Work from this package; do not explore broadly from scratch beyond the grep for other `.sql.gz`
filename consumers called out above.

## Target repo working directory (absolute)

`C:\Users\biglmi\Documents\webapps\webapp-ops-scripts`

## Preamble

> The text above is the COMPLETE spec — the committed WO file's content, not a plan to refine;
> there is no separate plan file. Read the nearest `AGENTS.md` and the relevant
> `.codex/skills/<role>/SKILL.md` ONLY for conventions. Stay in scope; do not touch
> `janitor.sh`, `generate_paths.py`, restic config, retention policy, or any file in
> `webapp-management`; do not update `MEMORY.md`. **Do NOT edit `WORK_ORDERS.md`** (it lives in
> `webapp-management`, not this repo, and is the orchestrator's alone regardless). **Your tools are
> for editing source and test files and for running the tests you wrote — nothing else.** Do NOT
> install dependencies, touch a lockfile, run a package manager, or tidy up stray files; if
> something in the repo state blocks you, stop and report it as `RESULT: BLOCKED <reason>` instead
> of fixing it. Do NOT `git add`/`commit`/`push` — leave every change uncommitted in the working
> tree for the orchestrator's independent review. WRITE the tests the `Required tests` section
> calls for AND **RUN the tests you just wrote** to confirm they execute and pass, and that the
> regression case (case 1) genuinely FAILS against the pre-change code before your fix — that is
> the ONLY test run you do (NOT the repo's other tests, NOT any review). The orchestrator re-runs
> the authoritative set and does the independent review after you finish — those are the gate;
> your own run does not count.
>
> Narrate continuously: a `PLAN: <step1> | <step2> | …` line up front, then a single-line
> `PROGRESS: [<n>/<total>] <present-tense action>` before every relevant action (and `… done` on
> completion), spaced so no gap exceeds ~2 min, stdout unbuffered, plus exactly one final
> `RESULT: DONE|BLOCKED <reason>`.

---

# C2. Orchestrator only — execution directive

> **If you are the implementer reading this as your specification: STOP, this section is not
> yours.**

`codex exec` in the background, invoked directly via Bash with both `--skip-git-repo-check` and
`--dangerously-bypass-approvals-and-sandbox`, `-m` per `.claude/models.local.json` →
`implementation`, WO passed on stdin. Fallback to direct Claude implementation only on Codex
quota/rate-limit/non-zero exit; the fallback flips authorship, so the independent review becomes
mandatory either way (it already is, Tier 3).

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
