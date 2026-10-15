# Work orders - webapp-ops-scripts

Repo token: `OPS`. Stream `REC`: targeted data-recovery tooling.
Historical `WM-OPS-*` files retain their existing IDs; their index is in webapp-management.

| ID | Titel | Beschreibung | Datum | Status | Commit(s) | Notiz |
|---|---|---|---|---|---|---|
| OPS-REC-1 | Recover missing Innoservice media | Standalone dry-run-first script; copy only missing referenced files from old main-prod media, no overwrite, verified hashes. | 2026-10-15 | done | a156810 | [WO](work-orders/OPS-REC-1.md), [validation](work-orders/OPS-REC-1-validation.md). Tier 3. Gate #1 approved by the operator 2026-10-15 in this chat, script creation/local tests only. Initial native codex/gpt-6-luna/high; parent integrated hardening/fixes. review: codex/gpt-6-luna · lenses: envelope, regression, duplication, tests · 10 raised · 7 accepted · worst accepted: defect; 3 rejected with evidence, zero open, final reviewers verified read-only/medium. Local Linux unittest: 23 passed, zero skips; Windows CLI help green. Done means script ready: production copy not executed or approved; innoservice INNO-OPS-2 remains open. |
