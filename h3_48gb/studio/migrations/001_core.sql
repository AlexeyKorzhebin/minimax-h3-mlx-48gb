CREATE TABLE project (
  id TEXT PRIMARY KEY,
  revision INTEGER NOT NULL DEFAULT 1,
  title TEXT NOT NULL,
  frame_format TEXT NOT NULL CHECK (frame_format IN ('16:9', '9:16')),
  idea_text TEXT NOT NULL DEFAULT '',
  style_block TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE TABLE asset (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('image', 'video', 'audio', 'frame', 'lora', 'scenario')),
  path TEXT NOT NULL UNIQUE,
  sha256 TEXT NOT NULL,
  bytes INTEGER NOT NULL,
  mime TEXT NOT NULL,
  origin_kind TEXT NOT NULL
    CHECK (origin_kind IN ('upload', 'take_output', 'extracted_frame', 'imported')),
  origin_take_id TEXT REFERENCES take(id),
  origin_asset_id TEXT REFERENCES asset(id),
  created_at TEXT NOT NULL
);

CREATE TRIGGER asset_is_immutable BEFORE UPDATE ON asset
BEGIN
  SELECT RAISE(ABORT, 'asset is immutable (И7)');
END;

CREATE TABLE scene (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES project(id),
  idx INTEGER NOT NULL,
  revision INTEGER NOT NULL DEFAULT 1,
  state_version INTEGER NOT NULL DEFAULT 1,
  -- И1a: optimistic lock for editing — +1 on every change of any editable field (the fingerprint
  -- of И1 included, and requested_s / state_in_text / state_out_text that И1 leaves out).
  edit_version INTEGER NOT NULL DEFAULT 0,
  mode TEXT NOT NULL CHECK (mode IN ('from_frame', 'refs', 'text')),
  action_text TEXT NOT NULL DEFAULT '',
  performance_text TEXT NOT NULL DEFAULT '',
  sound_text TEXT NOT NULL DEFAULT '',
  music_text TEXT NOT NULL DEFAULT '',
  state_in_text TEXT NOT NULL DEFAULT '',
  state_out_text TEXT NOT NULL DEFAULT '',
  prompt_final TEXT,
  requested_s REAL NOT NULL,
  frames INTEGER NOT NULL CHECK (frames BETWEEN 73 AND 345 AND (frames - 5) % 17 = 0),
  seed INTEGER NOT NULL,
  source_frame_kind TEXT NOT NULL DEFAULT 'none'
    CHECK (source_frame_kind IN ('none', 'prev_last_frame', 'asset')),
  source_scene_id TEXT REFERENCES scene(id),
  source_frame_asset_id TEXT REFERENCES asset(id),
  approved_sketch_take_id TEXT REFERENCES take(id),
  approved_final_take_id TEXT REFERENCES take(id),
  review_state TEXT NOT NULL DEFAULT 'none' CHECK (review_state IN ('none', 'needs_review', 'ok')),
  created_at TEXT NOT NULL,
  deleted_at TEXT
);
CREATE UNIQUE INDEX scene_live_order ON scene(project_id, idx) WHERE deleted_at IS NULL;

CREATE TABLE cast_member (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES project(id),
  tag TEXT NOT NULL,
  description_ru TEXT NOT NULL,
  description_en TEXT NOT NULL,
  portrait_phrase TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (project_id, tag)
);

CREATE TABLE cast_asset (
  cast_id TEXT NOT NULL REFERENCES cast_member(id),
  asset_id TEXT NOT NULL REFERENCES asset(id),
  role TEXT NOT NULL CHECK (role IN ('image', 'voice')),
  ord INTEGER NOT NULL,
  PRIMARY KEY (cast_id, asset_id)
);

CREATE TABLE scene_ref (
  scene_id TEXT NOT NULL REFERENCES scene(id),
  cast_id TEXT NOT NULL REFERENCES cast_member(id),
  ord INTEGER NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('person', 'place', 'style', 'voice')),
  PRIMARY KEY (scene_id, cast_id)
);

CREATE TABLE task (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('render', 'image', 'assemble', 'extract_frame')),
  executor TEXT NOT NULL,
  gpu_scope TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  idempotency_key TEXT UNIQUE,
  status TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 0,
  wait_reason TEXT,
  engine_ref TEXT,
  engine_stop TEXT,
  submit_attempt_at REAL,
  engine_submitted_at REAL,
  cancel_requested_at REAL,
  attempts INTEGER NOT NULL DEFAULT 0,
  progress_step INTEGER,
  progress_total INTEGER,
  error TEXT,
  depends_on_task_id TEXT REFERENCES task(id),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE take (
  id TEXT PRIMARY KEY,
  scene_id TEXT NOT NULL REFERENCES scene(id),
  scene_revision INTEGER NOT NULL,
  tier TEXT NOT NULL CHECK (tier IN ('sketch', 'final')),
  executor TEXT NOT NULL,
  template TEXT NOT NULL,
  upstream_take_id TEXT REFERENCES take(id),
  inputs_json TEXT,
  inputs_sha256 TEXT,
  keyframe_sha256 TEXT,
  status TEXT NOT NULL
    CHECK (status IN ('pending_input', 'queued', 'running', 'done', 'failed', 'cancelled', 'stale')),
  task_id TEXT REFERENCES task(id),
  output_asset_id TEXT REFERENCES asset(id),
  last_frame_asset_id TEXT REFERENCES asset(id),
  created_at TEXT NOT NULL
);

-- `INSERT OR REPLACE` (and REPLACE conflict clauses) on `take` and `asset` is forbidden by the `ops`
-- convention: REPLACE deletes and re-inserts, which dodges the UPDATE triggers below. Spec: UPDATE.
-- И2: the only allowed UPDATE of these columns is NULL -> value (materialization). A value never
-- changes, and NULL -> NULL is refused too, so no code path "touches" an unmaterialized take.
CREATE TRIGGER take_inputs_write_once BEFORE UPDATE OF inputs_json, inputs_sha256 ON take
WHEN OLD.inputs_json IS NOT NULL OR NEW.inputs_json IS NULL OR NEW.inputs_sha256 IS NULL
BEGIN
  SELECT RAISE(ABORT, 'take.inputs_json is write-once (И2)');
END;

CREATE TABLE proposal (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES project(id),
  project_revision INTEGER NOT NULL,
  idea_text TEXT NOT NULL,
  scenes_json TEXT NOT NULL,
  llm_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  applied_at TEXT
);

CREATE TABLE render_plan (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES project(id),
  tier TEXT,
  items_json TEXT NOT NULL,
  versions_json TEXT NOT NULL,
  expires_at TEXT NOT NULL
);

CREATE TABLE estimate_sample (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  executor TEXT NOT NULL,
  template TEXT NOT NULL,
  machine TEXT NOT NULL,
  width INTEGER NOT NULL,
  height INTEGER NOT NULL,
  frames INTEGER NOT NULL,
  steps INTEGER NOT NULL,
  wall_s REAL NOT NULL
);

CREATE TABLE assembly (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES project(id),
  inputs_json TEXT NOT NULL,
  dir TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL,
  output_asset_id TEXT REFERENCES asset(id),
  created_at TEXT NOT NULL
);

CREATE TABLE workflow_template (
  id TEXT PRIMARY KEY,
  executor TEXT NOT NULL,
  name TEXT NOT NULL,
  path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  bindings_json TEXT NOT NULL,
  requires_json TEXT NOT NULL,
  verified_on TEXT
);

CREATE TABLE event (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  project_id TEXT,
  scene_id TEXT,
  task_id TEXT,
  payload_json TEXT NOT NULL
);

CREATE TABLE setting (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE op_log (
  idempotency_key TEXT PRIMARY KEY,
  request_sha256 TEXT NOT NULL,
  response_json TEXT NOT NULL,
  http_status INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
