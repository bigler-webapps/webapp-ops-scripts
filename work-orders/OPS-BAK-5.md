# OPS-BAK-5 — hram staging-set snapshot in the research-prod backup (part of WM-INF-165)

Pointer stub. The authoritative Envelope, Part B and Gate record live in `webapp-management/work-orders/WM-INF-165.md`.

This repository's part: the backup program writes a separate, size-capped, tagged staging-set snapshot of hram `pipeline_artifacts` (selected `infrastructure`/`runs` entries plus `jobs`, `scenarios`, `data`). A selector failure, an empty selection or an exceeded cap skips only that snapshot and reports a fixed code. The disaster-recovery include/exclude rules and the per-host size gate stay unchanged. Reviewed per its own WO ID.
