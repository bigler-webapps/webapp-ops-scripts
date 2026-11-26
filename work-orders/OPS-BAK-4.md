# OPS-BAK-4 — research-prod restore paths for its own B2 repository (part of WM-INF-146)

Script and workflow part of `webapp-management/work-orders/WM-INF-146.md`; the Envelope, risks, tests and Gate record live there.

## A. Envelope

In this repository: research-prod's backup and every script here that opens its B2 repository use the new repository for snapshots after the switch and can still reach its old snapshots in the shared repository; main-prod's paths stay unchanged. The restore, check and init workflows live in webapp-management (WM-INF-146).

**Tier 3** (production backups, credentials, infra lane).

### Required tests

- research-prod resolves to the new repository after the switch and to the shared one before it; main-prod unchanged.
- A missing new credential refuses before restic opens a repository.

## B. Implementation map

- `backup.py` (`RESTIC_REPO_B2` `:28`), `test_backup.py`; `stack_recovery.py` if it opens research-prod's B2 repository.

## Preamble

> You are the dispatched implementer. Run `codex-hook-canary-ws-gov-60` as the first tool action.
> Continue on the expected denial. Otherwise report unverified coverage once and continue under the existing gates.
> Read this Envelope, WM-INF-146 and its Gate record, the nearest governance and the relevant role skill.
> Edit only the named scripts and their tests and run only the new tests you wrote. Preserve parallel work.
> Do not access hosts or credentials, dispatch jobs or agents, run reviews, edit the register or write to git.
> Report progress and end with `RESULT: DONE` or `RESULT: BLOCKED` with the test evidence.

## C. Orchestrator only

Coordinate with WM-INF-146.

## Completion (2026-11-26, Orchestrator)

No script change needed; the Envelope is met by the existing design. Evidence (read-only inspection of this repository at e30a951 and of webapp-management at f9687a83):
- No production repository is hard-coded in `backup.py`, `verify_backup.py`, `restore.py`, `restic_version.py` or `stack_recovery.py`; every repository URL comes from the delivered `RESTIC_REPO_B2` / `RESTIC_REPOSITORY` (the `s3:`/`b2:` strings are test fixtures). research-prod therefore uses the new repository as soon as its credentials point there; main-prod's inputs are untouched.
- A missing or mismatched repository is refused before restic opens anything by the receiver (`ops_backup_receiver/files/receiver.py` `repository_approved`, reason `repository_policy_mismatch`), pinned by `agent-applies.json` and `test_ops_backup_cutover.py` (WM-INF-146 backup chunk, 052186d7).
- Old snapshots: restore workflows choose the shared repository through `research_repository=shared` (WM-INF-146, 679a1747 and 6f92bd07); `restore.py` takes whatever repository the job delivers.
- Known behaviour, not changed: `backup.py` skips B2 when no URL is delivered at all (`run_restic` "SKIP (no repo url)"); for research-prod this cannot occur through the receiver because the empty value fails the pin.
Review: none (no diff). Part B: accurate. Acceptance: with WM-INF-146 (first research-prod snapshot in the new repository, drill with `shared` on an old snapshot).
