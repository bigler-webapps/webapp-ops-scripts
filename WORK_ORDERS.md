# Work-Order Register - Work orders - webapp-ops-scripts

Repo token: `OPS`. Stream `REC`: targeted data-recovery tooling. Stream `BAK`: nightly backup coverage and restore drills.
Historical `WM-OPS-*` files retain their existing IDs; their index is in webapp-management.

The register is `work-orders/register.jsonl` (one JSON object per work order); read and write it only
through `python -I .claude/scripts/wo.py` (`AGENTS.md` -> "Work-Order Register"). Completion details
live in the "Completion" section of each `work-orders/<ID>.md`. The register as it stood before the
migration is archived whole in `work-orders/archive/WORK_ORDERS-until-2026-11-16.md`.

**Repo token:** see the declaration above
