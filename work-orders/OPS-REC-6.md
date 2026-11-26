# OPS-REC-6 — Durable HRAM chat activation barrier

Companion to HRAM-CHT-11 and WFT-CI-59. The operator approved the HRAM deployment blocker fix and its forward-repair rollback rule on 2026-11-27.

## A. Envelope

**Tier 3, infrastructure lane.** Extend the existing recovery and admission controls only as necessary for the one-time HRAM chat permission activation. Existing application recovery, backup, restore, admission and deploy behavior for other applications stays unchanged.

The trusted HRAM activation must hold exclusive recovery admission, persistently block automatic starts, identify and drain the authoritative old authorization writers, and run migrations and the fixed `initial_chat_catalogue_cutover` command against the verified new release under one stable deployment identity. A crash or ambiguous step remains blocked and permits safe reconciliation. Before releasing compatible services, durably record the activation and enforce a compatibility fence across deploy, restart, restore and recovery entry points: old or unknown incompatible writers cannot start against the activated database. After activation, recover forward with compatible code only. An old-image rollback needs an independently verified restoration of the matching database state and a separate operator approval; no automatic old-image rollback is authorized here. Never accept commands, service inventory or compatibility claims from release artifacts.

Preserve the existing recovery journal, startup gate, restore and health guarantees. Fail closed if target, writer inventory, image identity, deployment identity or recovery state is incomplete or inconsistent. Coordinate the fixed trusted API and its tests with WFT-CI-59; do not activate on a server during implementation. Test all interruption points, retries, concurrent admission, inventory mismatches, old and unknown images, recovery/restore paths and non-HRAM regression behavior through the repository's declared scopes.

## B. Implementation map

- `webapp-ops-scripts` main: `stack_recovery.py`, `test_stack_recovery*.py`, existing callers in `restore.py` and related tests.
- Existing barriers: `RecoveryTransaction`, `writer_guard`, startup gate and recovery journal. WFT-CI-59 specifies its proposed fixed coordinator contract in `workflow-templates/.github/scripts/hram_cutover.py`.
- HRAM writers in the trusted staging Compose policy are backend, celery_worker and celery_beat; the complete external writer boundary and installed target state still require verification.
- Callers and policy are managed in workflow-templates and webapp-management; this worker does not modify those repositories or access servers.

## Preamble

> You are the dispatched implementer. Run `codex-hook-canary-ws-gov-60` first; continue on its expected denial, otherwise report unverified coverage. Read this work order and the recovery skill. Implement only the named repository portion and focused tests. Preserve foreign changes. Do not access hosts, secrets or `.env`, use Git, edit work orders or register, run tests or delegate. If the safety preconditions cannot be enforced, leave an explicitly inert interface and report the precise dependency; never weaken an existing guard.

## C. Orchestrator only

Coordinate reviews, evidence, delivery and managed-server applies with HRAM-CHT-11 and WFT-CI-59. No production activation is approved here.

## Gate record

Gate #1 approved by the operator 2026-11-27 (resolve the activation blockers; after activation recover forward with compatible code and require a separate approval plus verified matching database restoration for old-image rollback).
