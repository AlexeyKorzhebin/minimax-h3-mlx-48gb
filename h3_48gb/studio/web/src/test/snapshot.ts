import type { Scene, Snapshot } from "@/api/types";

export function scene(over: Partial<Scene> = {}): Scene {
  return {
    id: "s_1", idx: 0, revision: 1, state_version: 1, edit_version: 0, mode: "refs",
    action_text: "She walks to the lake.", performance_text: "Her gaze drops, then she smiles.",
    sound_text: "Bare heels on wet planks.", music_text: "none", state_in_text: "", state_out_text: "",
    requested_s: 8, frames: 192, duration_s: 8, seed: 11,
    source_frame: { kind: "none", scene_id: null, asset_id: null, auto: false }, refs: ["anna"],
    review_state: "none", approved_sketch_take_id: null, approved_final_take_id: null, ...over,
  };
}

export function snapshot(over: Partial<Snapshot> = {}): Snapshot {
  return {
    project: { id: "p_1", revision: 3, title: "Лес", frame_format: "16:9", idea_text: "девушка у озера", style_block: "", created_at: "2026-10-21T10:00:00.000+00:00" },
    scenes: [], cast: [], takes: [], tasks: [], last_seq: 5, ...over,
  };
}
