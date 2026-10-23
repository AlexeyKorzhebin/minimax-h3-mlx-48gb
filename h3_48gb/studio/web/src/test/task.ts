import type { Plan, Task } from "@/api/types";

export function task(over: Partial<Task> = {}): Task {
  return {
    id: "k_1", kind: "image", project_id: "p_1", status: "queued", template: "krea2-t2i",
    wait_reason: null, progress_step: null, progress_total: null, error: null, attempts: 0,
    engine_stop: null, engine_version: null, template_sha256: null, cancel_requested: false, depends_on_task_id: null, result_asset_id: null,
    result_url: null, created_at: "2026-10-22T10:00:00.000+00:00", updated_at: "2026-10-22T10:00:00.000+00:00",
    ...over,
  };
}

export function imagePlan(over: Partial<Plan> = {}): Plan {
  return {
    plan_id: "pl_1", expires_at: "2026-10-22T10:10:00.000+00:00", kind: "image",
    items: [{ kind: "image", scene_id: "s_1", template: "krea2-t2i", prompt: "A woman at the lake.",
              width: 1344, height: 768, seed: 7, steps: 8, estimate_s: 6, status: "ready", reasons: [] }],
    total_s: 23, checkpoint_switches: [{ from: "h3-fl2va", to: "krea2-t2i", seconds: 17 }],
    lint: {}, blocked: [], invalid: [], ...over,
  };
}
