# OPS-REC-3 — webapp-ops-scripts side of WM-INF-129 (verified staging state across refresh failures)

Pointer stub. The work order is `webapp-management/work-orders/WM-INF-129.md` (Tier 3, infra lane); this repo carries only its
ops-scripts part so that review and landing have a register row here. Gate #1 and the calibration decision: recorded in that WO.

# A. Envelope (this repo's part)

- `stack_recovery.py`: a cutover journal in the recovery record, rollback to the last verified state (database, artifacts, service and
  restart state), a failed refresh recorded as `completed` + `refresh=failed` so ordinary writers are admitted, same-operation retries
  never answered as already completed, unresolved cutover rolled back first, public-root verification through the app's own router label.
- `restore.py`: database swap that keeps the live database as a recoverable copy, idempotent rollback, capacity decline, name hardening.
- `volume_selection.py` / `backup_volumes.json`: `include_exact` backs up exactly `hram_production_calibration_outputs_volume`
  (backup-only; exclusions otherwise keep precedence; running-mount requirement and the 17,000,000,000-byte host limit unchanged).
- Out of scope: the receiver, the sender and the workflow (webapp-management), HRAM's declaration (hram).

# C. Orchestrator only

Evidence and review: see `webapp-management/work-orders/WM-INF-129.md` -> Completion.
