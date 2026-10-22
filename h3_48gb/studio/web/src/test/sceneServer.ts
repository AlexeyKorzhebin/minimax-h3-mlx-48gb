import type { Scene } from "@/api/types";
import { fromScene, type Field, type Outcome, type PatchBody } from "@/screens/sceneDraft";

export type Writer = "init" | "us" | "other";

/** ops.update_scene in miniature, for the draft tests (spec §6.1 И1, И1a; §7 O8):
 *   - `expected_edit_version` must equal the current `edit_version`, else 409 with the scene;
 *   - an accepted write that changes something bumps `edit_version` by exactly 1; a write that
 *     changes nothing is accepted and bumps nothing (И1a, like the real server — review r7);
 *   - `revision` follows И1: +1 only when the fingerprint changes — `requested_s` counts through
 *     the frames it snaps to, so 8 → 8.1 keeps the revision (the r6 blocker lives exactly there).
 *  Every edit_version and its writer are kept, so a snapshot of any age can be served and an
 *  overwrite can be attributed. */
export class FakeSceneServer {
  scene: Scene;
  history = new Map<number, Scene>();
  writers = new Map<number, Writer>();
  constructor(initial: Scene) {
    this.scene = initial;
    this.history.set(initial.edit_version, initial);
    this.writers.set(initial.edit_version, "init");
  }
  patch(body: PatchBody, by: Writer): Outcome {
    const { expected_edit_version, ...fields } = body;
    if (expected_edit_version !== this.scene.edit_version) return { kind: "conflict", current: this.scene };
    const next = { ...this.scene, ...(fields as Partial<Scene>) };
    next.frames = snapFrames(next.requested_s);
    if (JSON.stringify(fromScene(next)) === JSON.stringify(fromScene(this.scene))) return { kind: "ok", scene: this.scene };
    const fingerprintChanged = fingerprint(next) !== fingerprint(this.scene);
    this.scene = {
      ...next,
      revision: this.scene.revision + (fingerprintChanged ? 1 : 0),
      edit_version: this.scene.edit_version + 1,
    };
    this.history.set(this.scene.edit_version, this.scene);
    this.writers.set(this.scene.edit_version, by);
    return { kind: "ok", scene: this.scene };
  }
  /** The other window saves one field: it always reads first, so it always has the current version. */
  other(f: Field, v: string): Scene {
    const out = this.patch({ expected_edit_version: this.scene.edit_version, ...fieldValue(f, v) } as PatchBody, "other");
    return (out as { scene: Scene }).scene;
  }
  /** Who wrote the value a field has at `version`, and at which edit_version. */
  writerOf(version: number, f: Field) {
    let v = version;
    const first = Math.min(...this.history.keys());
    const value = fromScene(this.history.get(version)!)[f];
    while (v > first && fromScene(this.history.get(v - 1)!)[f] === value) v -= 1;
    return { rev: v, by: this.writers.get(v)! };
  }
}

/** И1's fingerprint, restricted to what a scene card edits (+ frames instead of requested_s). */
function fingerprint(s: Scene): string {
  return JSON.stringify([s.mode, s.action_text, s.performance_text, s.sound_text, s.music_text, s.frames, s.seed, s.refs]);
}

/** duration.snap_frames: nearest point of 17n+5 at 24 fps, ties up, clamped to [73, 345]. */
export function snapFrames(seconds: number): number {
  const target = seconds * 24;
  const low = 5 + 17 * Math.floor((target - 5) / 17);
  const frames = target - low < low + 17 - target ? low : low + 17;
  return Math.min(Math.max(frames, 73), 345);
}

export function fieldValue(f: Field, v: string): Partial<Scene> {
  if (f === "requested_s" || f === "seed") return { [f]: Number(v) };
  if (f === "refs") return { refs: v ? v.split(",") : [] };
  if (f === "mode") return { mode: v as Scene["mode"] };
  return { [f]: v };
}
