-- Slice S2 (plan 2026-10-22, R-S2-1; file 003 — 002 is S1's scene_source_auto): what the worker
-- and O12/O13 need that 001 did not carry.
-- A task belongs to a project (the SSE filter, the snapshot's task list, O13); an image task's
-- result is an asset (O13 and the event name it, §7 O12); an asset made by a task names it.
ALTER TABLE task ADD COLUMN project_id TEXT REFERENCES project(id);
ALTER TABLE task ADD COLUMN result_asset_id TEXT REFERENCES asset(id);
-- What actually ran (plan revision 3, Codex r2 finding 10): the ComfyUI version from
-- /system_stats at submit and the template version the graph was built from.
ALTER TABLE task ADD COLUMN engine_version TEXT;
ALTER TABLE task ADD COLUMN template_sha256 TEXT;
ALTER TABLE asset ADD COLUMN origin_task_id TEXT REFERENCES task(id);
CREATE INDEX task_by_status ON task(status, priority, created_at);
CREATE INDEX task_by_project ON task(project_id, created_at);
-- A plan is confirmed once (O11/O12): a second press with a new Idempotency-Key must not queue
-- the same work twice.
ALTER TABLE render_plan ADD COLUMN used_at TEXT;
