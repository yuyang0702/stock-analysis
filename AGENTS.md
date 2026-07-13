# Repository guidance

- `docs/project_roadmap.md` is the unique main project document. If another document conflicts with it, follow the roadmap.
- The roadmap's "当前有效从文档索引" is the authoritative list of active subordinate documents. Files under `docs/archive/` are historical references and are not default required reading.
- For a new machine or conversation, read `docs/project_handoff.md` after the roadmap; treat it as a time-point snapshot and re-verify external state. Then read only the active subordinate documents relevant to the task.
- Before changing business behavior, read the roadmap, `docs/live_trading_execution_plan.md`, and the active specification or plan named by the roadmap index.
- Follow `docs/data_storage_policy.md` for every new or changed persistent file, log, cache, snapshot, report, JSONL stream, or database table.
- A persistence feature is incomplete unless it defines bounded growth, retention, rotation or compaction, efficient reads, backup/recovery, privacy, and tests.
- Do not introduce unbounded append-only files, full-history scans in recurring jobs, or permanent per-run output files without an approved retention design.
- Keep runtime data under `cache/` or `output/`, not in the repository root, and do not commit secrets or runtime state.
- Preserve strategy, trading, risk, and document semantics unless the user explicitly authorizes a business-logic change.
