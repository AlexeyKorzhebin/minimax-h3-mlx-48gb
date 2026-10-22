import type { Scene, ScenePatch } from "@/api/types";

/** The scene card's draft against the server (spec §7 O8 `expected_edit_version`, 409
 *  `revision_conflict`). Versions here are the scene's `edit_version` (И1a: +1 on every change of
 *  any editable field), never `revision`: И1 leaves `revision` alone when only `requested_s` or
 *  `state_in_text` changes, so equal revisions can carry different content (review r6).
 *
 *  Pure state + transitions; the card only renders it and runs the one request it names.
 *  «Edited» is never decided by comparing values: every input bumps the field's `gen`, and the
 *  field is dirty while `gen > cleanGen`. The invariants the model test pins:
 *   1. an owner's value never disappears from the draft unless the owner takes the server's;
 *   2. `base` (the edit_version the dirty fields are edited against) never decreases, nor does
 *      `server` (the newest server state shown); an answer below them is ignored;
 *   3. another window's saved value is only ever overwritten by «Сохранить моё», which goes with
 *      the edit_version whose values the conflict box showed;
 *   4. clean fields always show `server`; a field is dirty exactly while it holds an owner's
 *      value the server has not acknowledged;
 *   5. at most one request per card is in flight — a second press waits for the answer and goes
 *      with the base that answer sets. */

export const FIELDS = ["action_text", "performance_text", "sound_text", "music_text", "requested_s", "seed", "mode", "refs"] as const;
export type Field = (typeof FIELDS)[number];
export type Draft = {
  action_text: string; performance_text: string; sound_text: string; music_text: string;
  requested_s: string; seed: string; mode: Scene["mode"]; refs: string;
};
export type Gen = Record<Field, number>;
export type PatchBody = Partial<ScenePatch> & { expected_edit_version: number };
export type Request = {
  id: number;
  expected: number;
  fields: Field[];
  body: PatchBody;
  /** `gen` of every field at the moment of sending: the answer may only touch a field whose gen is still this. */
  sentGen: Gen;
  viaConflict: boolean;
};
export type Outcome =
  | { kind: "ok"; scene: Scene }
  | { kind: "conflict"; current: Scene }
  | { kind: "error"; message: string };

export type DraftState = {
  draft: Draft;
  gen: Gen;
  cleanGen: Gen;
  /** Revision the dirty fields are edited against — what a plain «Сохранить» sends. */
  base: number;
  /** Newest server state this card has seen: clean fields show it, the conflict box compares with it. */
  server: Scene;
  inflight: Request | null;
  queued: boolean;
  conflict: boolean;
  error: string | null;
  /** Every input, any branch: the phone bar picks the card that got input last. */
  inputs: number;
  nextId: number;
};

export function fromScene(s: Scene): Draft {
  return {
    action_text: s.action_text, performance_text: s.performance_text,
    sound_text: s.sound_text, music_text: s.music_text,
    requested_s: String(s.requested_s), seed: String(s.seed), mode: s.mode, refs: s.refs.join(","),
  };
}

const zeroGen = (): Gen => Object.fromEntries(FIELDS.map((f) => [f, 0])) as Gen;

export function init(scene: Scene): DraftState {
  return {
    draft: fromScene(scene), gen: zeroGen(), cleanGen: zeroGen(), base: scene.edit_version, server: scene,
    inflight: null, queued: false, conflict: false, error: null, inputs: 0, nextId: 1,
  };
}

export const isDirtyField = (s: DraftState, f: Field) => s.gen[f] > s.cleanGen[f];
export const dirtyFields = (s: DraftState): Field[] => FIELDS.filter((f) => isDirtyField(s, f));
export const isDirty = (s: DraftState) => dirtyFields(s).length > 0;
/** Another window moved the scene on while this card holds unsaved text (and no conflict is shown yet). */
export const isStale = (s: DraftState) => isDirty(s) && !s.conflict && s.server.edit_version > s.base;
/** «Сервер: … · Моё: …» for every dirty field — the server side is the newest state seen. */
export function conflictPairs(s: DraftState): { field: Field; server: string; mine: string }[] {
  const sv = fromScene(s.server);
  return dirtyFields(s).map((field) => ({ field, server: sv[field], mine: s.draft[field] }));
}

export function input(s: DraftState, field: Field, value: string): DraftState {
  return {
    ...s,
    draft: { ...s.draft, [field]: value },
    gen: { ...s.gen, [field]: s.gen[field] + 1 },
    inputs: s.inputs + 1,
  };
}

export function toBody(draft: Draft, fields: Field[], expected: number): PatchBody {
  const out: PatchBody = { expected_edit_version: expected };
  for (const k of fields) {
    if (k === "requested_s" || k === "seed") out[k] = Number(draft[k]);
    else if (k === "refs") out.refs = draft.refs ? draft.refs.split(",") : [];
    else if (k === "mode") out.mode = draft.mode;
    else out[k] = draft[k];
  }
  return out;
}

function start(s: DraftState, expected: number, viaConflict: boolean): DraftState {
  if (s.inflight) return { ...s, queued: true };
  const fields = dirtyFields(s);
  if (fields.length === 0) return { ...s, queued: false };
  const request: Request = {
    id: s.nextId, expected, fields, body: toBody(s.draft, fields, expected), sentGen: { ...s.gen }, viaConflict,
  };
  return { ...s, inflight: request, queued: false, error: null, nextId: s.nextId + 1 };
}

/** «Сохранить сцену»: the dirty fields against `base`. In a conflict the owner must choose instead. */
export const save = (s: DraftState): DraftState => (s.conflict ? s : start(s, s.base, false));
/** «Сохранить моё»: the dirty fields against the edit_version whose values the conflict box shows. */
export const saveMine = (s: DraftState): DraftState => (s.conflict ? start(s, s.server.edit_version, true) : s);

/** «Взять с сервера»: the owner drops every unsaved value for the newest server state. */
export function takeServer(s: DraftState): DraftState {
  return { ...s, draft: fromScene(s.server), cleanGen: { ...s.gen }, base: s.server.edit_version, conflict: false, queued: false, error: null };
}

/** States are strictly ordered by edit_version (И1a): equal version ⇒ equal content, so the one
 *  already held is kept and a late copy can never roll the card back (review r6, «Важно» 1). */
const newer = (a: Scene, b: Scene): Scene => (b.edit_version > a.edit_version ? b : a);

/** Clean fields show the newest server state. */
function adoptClean(s: DraftState): DraftState {
  const sv = fromScene(s.server);
  const draft = { ...s.draft };
  for (const f of FIELDS) if (!isDirtyField(s, f)) draft[f] = sv[f] as never;
  return { ...s, draft };
}

/** A queued press goes out once the answer is in and no conflict asks the owner to choose. */
const flush = (s: DraftState): DraftState => (s.queued && !s.inflight && !s.conflict ? start(s, s.base, false) : s);

/** A new snapshot of the scene (SSE reload or the reload after a save). */
export function sceneSeen(s: DraftState, scene: Scene): DraftState {
  if (scene.edit_version <= s.server.edit_version) return s;               // not newer than what is shown
  const next = adoptClean({ ...s, server: scene });
  return isDirty(next) ? next : { ...next, base: scene.edit_version };
}

/** The answer to `request`. A 200 settles every field whose gen is still what was sent: it takes
 *  the server's value and is clean. A field typed into since keeps its text and stays dirty.
 *  Answers below `base` / `server` are stale and ignored. */
export function received(s: DraftState, request: Request, outcome: Outcome): DraftState {
  const inflight = s.inflight?.id === request.id ? null : s.inflight;
  s = { ...s, inflight };
  if (outcome.kind === "error") return { ...s, error: outcome.message, queued: false };
  if (outcome.kind === "conflict") {
    if (outcome.current.edit_version <= s.base) return flush(s);               // stale answer to an older request
    const next = adoptClean({ ...s, server: newer(s.server, outcome.current) });
    // nothing left to choose about (the owner took the server's while this was in flight)
    if (!isDirty(next)) return flush({ ...next, base: next.server.edit_version, conflict: false, error: null });
    return { ...next, conflict: true, queued: false, error: null };
  }
  const { scene } = outcome;
  if (scene.edit_version < s.base) return flush(s);
  const server = newer(s.server, scene);
  const sv = fromScene(server);
  const draft = { ...s.draft };
  const cleanGen = { ...s.cleanGen };
  for (const f of FIELDS) {
    if (s.gen[f] === request.sentGen[f]) {
      draft[f] = sv[f] as never;
      cleanGen[f] = s.gen[f];
    }
  }
  const next = adoptClean({ ...s, draft, cleanGen, server, conflict: false, error: null });
  return flush({ ...next, base: isDirty(next) ? scene.edit_version : server.edit_version });
}
