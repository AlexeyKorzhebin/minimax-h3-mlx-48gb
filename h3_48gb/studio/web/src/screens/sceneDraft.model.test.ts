import { describe, expect, it } from "vitest";
import type { Scene } from "@/api/types";
import { FakeSceneServer } from "@/test/sceneServer";
import { scene as makeScene } from "@/test/snapshot";
import {
  conflictPairs, dirtyFields, fromScene, init, input, isDirty, isDirtyField, received, save, saveMine,
  sceneSeen, takeServer, type DraftState, type Field, type Outcome, type Request,
} from "./sceneDraft";

/** The fields a scene card edits, written out here — NOT imported from sceneDraft: a field dropped
 *  from the production list must turn this model red (review r6), not quietly shrink it. */
const FIELDS: readonly Field[] = ["action_text", "performance_text", "sound_text", "music_text", "requested_s", "seed", "mode", "refs"];

/** Model test of the draft sync: random interleavings of the owner's inputs, another window's
 *  saves, snapshots (taken and delivered out of order), answers (processed and delivered out of
 *  order, some failing), 409s and the owner's choices — against a server with state. After EVERY
 *  step the invariants below are checked; at the end the run is drained and everything the owner
 *  typed and did not discard must have reached the server. Seeds are fixed; a failure names the
 *  seed and prints the step log. `DRAFT_MODEL_RUNS=20000 npm test` for a longer campaign. */

const RUNS = Number(import.meta.env.DRAFT_MODEL_RUNS ?? 5000);
const STEPS = 80;

// Small alphabets: equal values recur, so a merge that compares values instead of counting
// edits (review r5) is exercised, not avoided.
const ALPHABET: Record<Field, string[]> = {
  action_text: ["a", "b", "c"], performance_text: ["p", "q"], sound_text: ["s", "t", "u"], music_text: ["m", "n", ""],
  // 8 and 8.1 snap to the same 192 frames: the revision stays (И1), only edit_version moves (И1a)
  requested_s: ["4", "8", "8.1"], seed: ["1", "2"], mode: ["refs", "from_frame"], refs: ["", "anna", "anna,bob"],
};

function mulberry32(seed: number) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

type Sent = { request: Request; sendStep: number; outcome?: Outcome; acceptStep?: number; delivered?: boolean };

class Run {
  rnd: ReturnType<typeof mulberry32>;
  server: FakeSceneServer;
  state: DraftState;
  log: string[] = [];
  step = 0;
  /** The last value the owner typed into a field, and when; cleared by «Взять с сервера». */
  lastOwner: Partial<Record<Field, { value: string; step: number }>> = {};
  sent = new Map<number, Sent>();
  pendingAnswers: number[] = [];
  pendingSnapshots: Scene[] = [];
  deliveredRevisions = new Set<number>([1]);
  /** edit_version of the value a field showed while clean (what the owner saw in the field). */
  cleanShown = Object.fromEntries(FIELDS.map((f) => [f, 1])) as Record<Field, number>;
  /** The owner's informed edit: edit_version of the value the field showed when the owner started
   *  typing over it; and the explicit choice: edit_version shown by the conflict box when the
   *  owner pressed «Сохранить моё» with this field among the pairs (review r6). */
  typedOver = Object.fromEntries(FIELDS.map((f) => [f, 0])) as Record<Field, number>;
  keptMine = Object.fromEntries(FIELDS.map((f) => [f, 0])) as Record<Field, number>;
  /** How often the generator touched each field (owner or other window) — every field must play. */
  played = Object.fromEntries(FIELDS.map((f) => [f, 0])) as Record<Field, number>;
  prevBase = 1;
  prevServer = 1;

  constructor(seed: number) {
    this.rnd = mulberry32(seed);
    const initial = makeScene({ id: "s_1", revision: 1, edit_version: 1, requested_s: 8, frames: 192 });
    this.server = new FakeSceneServer(initial);
    this.state = init(initial);
  }
  pick<T>(xs: readonly T[]): T { return xs[Math.floor(this.rnd() * xs.length)]; }
  chance(p: number) { return this.rnd() < p; }
  fail(what: string): never {
    const s = this.state;
    throw new Error(`${what}\n${this.log.join("\n")}\nstate: ${JSON.stringify({ draft: s.draft, gen: s.gen, cleanGen: s.cleanGen, base: s.base, server: s.server.edit_version, conflict: s.conflict })}`);
  }
  ok(cond: boolean, what: () => string) { if (!cond) this.fail(what()); }

  /** A state transition of the page; afterwards a new request may be in flight. */
  page(label: string, next: DraftState) {
    this.log.push(`${this.step}: ${label}`);
    this.state = next;
    const r = next.inflight;
    if (r && !this.sent.has(r.id)) {
      this.sent.set(r.id, { request: r, sendStep: this.step });
      this.pendingAnswers.push(r.id);
      this.log.push(`${this.step}:   → PATCH ${JSON.stringify(r.body)}`);
      // invariant 4 (client side): never an edit_version the server has not reached
      this.ok(r.expected <= this.server.scene.edit_version, () => "expected_edit_version ahead of the server");
      // invariant 8: the body carries exactly the fields holding an unacknowledged owner's value
      for (const f of r.fields) this.ok(this.justified(f), () => `sent a field the owner did not edit: ${f}`);
      this.ok(JSON.stringify(Object.keys(r.body).filter((k) => k !== "expected_edit_version").sort()) === JSON.stringify([...r.fields].sort()),
        () => "body does not match the request's fields");
    }
  }

  /** The field holds an owner's value that no delivered 200 has acknowledged. */
  justified(f: Field): boolean {
    const own = this.lastOwner[f];
    if (!own) return false;
    for (const s of this.sent.values()) {
      if (s.delivered && s.outcome?.kind === "ok" && s.request.fields.includes(f) && s.sendStep > own.step) return false;
    }
    return true;
  }
  /** The server took this value of the field from this page after the owner typed it. */
  persisted(f: Field, own: { value: string; step: number }): boolean {
    return [...this.sent.values()].some((x) => x.outcome?.kind === "ok" && x.acceptStep! > own.step
      && f in x.request.body && fromScene(x.outcome.scene)[f] === own.value);
  }

  invariants() {
    const s = this.state;
    // invariant 2: base and the shown server state never go back, base never runs ahead of it
    this.ok(s.base >= this.prevBase, () => `base decreased ${this.prevBase} → ${s.base}`);
    this.ok(s.server.edit_version >= this.prevServer, () => "shown server state went back");
    this.ok(s.base <= s.server.edit_version, () => "base ahead of the shown server state");
    this.prevBase = s.base;
    this.prevServer = s.server.edit_version;
    // the shown server state is one the page actually received
    this.ok(this.deliveredRevisions.has(s.server.edit_version), () => `server state ${s.server.edit_version} was never delivered`);
    this.ok(s.server.edit_version === Math.max(...this.deliveredRevisions), () => `a newer server state (${Math.max(...this.deliveredRevisions)}) was delivered and ignored`);
    this.ok(s.server === this.server.history.get(s.server.edit_version), () => "shown server state is not the real one");
    const sv = fromScene(s.server);
    for (const f of FIELDS) {
      const own = this.lastOwner[f];
      // invariant 1: the owner's last value is in the draft, or the server took it after it was typed
      if (own && s.draft[f] !== own.value) {
        this.ok(this.persisted(f, own), () => `owner's ${f}=${JSON.stringify(own.value)} lost: draft has ${JSON.stringify(s.draft[f])}`);
      }
      // invariant 5: a clean field shows the newest server state; dirty ⇔ unacknowledged owner's value
      const dirty = isDirtyField(s, f);
      if (!dirty) {
        this.ok(s.draft[f] === sv[f], () => `clean ${f} shows ${JSON.stringify(s.draft[f])}, the server state shown has ${JSON.stringify(sv[f])}`);
        this.cleanShown[f] = s.server.edit_version;
      }
      this.ok(dirty === this.justified(f), () => `${f}: dirty=${dirty} but holds an unacknowledged owner's value=${this.justified(f)}`);
    }
    // invariant 9 (r1): a card that holds nothing unsaved and waits for nothing sits exactly on the
    // server state it shows — a base left behind would send the next edit with a stale version
    this.ok(isDirty(s) || s.inflight !== null || s.base === s.server.edit_version,
      () => `clean card, nothing in flight, base ${s.base} != shown server state ${s.server.edit_version}`);
    // invariant 6: a conflict is only shown while the server is really ahead of the base, and
    // there is something to choose about; its «Сервер» side is the state shown
    if (s.conflict) {
      this.ok(s.server.edit_version > s.base, () => "conflict shown at base");
      this.ok(dirtyFields(s).length > 0, () => "conflict without a dirty field");
      for (const p of conflictPairs(s)) {
        this.ok(p.server === sv[p.field], () => "conflict pair differs from the server state shown");
      }
    }
    // invariant 7: at most one request in flight
    const inFlight = this.pendingAnswers.length + [...this.sent.values()].filter((x) => x.outcome && !x.delivered).length;
    this.ok(inFlight <= 1, () => "more than one request in flight");
  }

  // ---- steps ---------------------------------------------------------------------------------

  input() {
    const f = this.pick(FIELDS);
    const v = this.pick(ALPHABET[f]);
    this.played[f]++;
    // typing into a clean field = typing over the value the owner sees there
    if (!isDirtyField(this.state, f)) this.typedOver[f] = Math.max(this.typedOver[f], this.cleanShown[f]);
    this.lastOwner[f] = { value: v, step: this.step };
    this.page(`owner types ${f}=${JSON.stringify(v)}`, input(this.state, f, v));
  }
  otherWrites() {
    const f = this.pick(FIELDS);
    const v = this.pick(ALPHABET[f]);
    this.played[f]++;
    const after = this.server.other(f, v);
    this.log.push(`${this.step}: other window saves ${f}=${JSON.stringify(v)} → edit_version ${after.edit_version} (revision ${after.revision})`);
    if (this.chance(0.7)) this.pendingSnapshots.push(this.server.scene);
  }
  process() {
    if (this.pendingAnswers.length === 0) return;
    const id = this.pendingAnswers.splice(Math.floor(this.rnd() * this.pendingAnswers.length), 1)[0];
    const entry = this.sent.get(id)!;
    const r = entry.request;
    const before = this.server.scene;
    let outcome: Outcome;
    if (this.chance(0.08)) outcome = { kind: "error", message: "Сервер не ответил" };
    else {
      outcome = this.server.patch(r.body, "us");
      // invariant 4 (server side): accepted ⇔ the expected edit_version was current
      this.ok((outcome.kind === "ok") === (r.expected === before.edit_version), () => "server took a stale expected_edit_version");
      if (outcome.kind === "ok") {
        entry.acceptStep = this.step;
        // invariant 3: a value another window saved is only overwritten by the owner's own
        // decision — either the owner typed over it after seeing it in the field, or pressed
        // «Сохранить моё» with this field in the conflict box showing it (review r6: having been
        // merely shown the value is not a decision)
        for (const f of r.fields) {
          const prev = fromScene(before)[f];
          if (fromScene(outcome.scene)[f] === prev) continue;
          const writer = this.server.writerOf(before.edit_version, f);
          if (writer.by === "other") {
            this.ok(this.typedOver[f] >= writer.rev || this.keptMine[f] >= writer.rev,
              () => `overwrote the other window's ${f}=${JSON.stringify(prev)} (edit_version ${writer.rev}) without the owner's decision (typed over ${this.typedOver[f]}, «Сохранить моё» at ${this.keptMine[f]})`);
          }
        }
      }
    }
    entry.outcome = outcome;
    this.log.push(`${this.step}: server answers #${id}: ${outcome.kind}${outcome.kind === "ok" ? ` edit_version ${outcome.scene.edit_version}` : outcome.kind === "conflict" ? ` current edit_version ${outcome.current.edit_version}` : ""}`);
    if (outcome.kind === "ok" && this.chance(0.7)) this.pendingSnapshots.push(this.server.scene);
  }
  deliver() {
    const ready = [...this.sent.values()].filter((x) => x.outcome && !x.delivered);
    if (ready.length === 0) return;
    const entry = this.pick(ready);
    entry.delivered = true;
    const o = entry.outcome!;
    if (o.kind === "ok") this.deliveredRevisions.add(o.scene.edit_version);
    if (o.kind === "conflict") this.deliveredRevisions.add(o.current.edit_version);
    this.page(`page receives answer #${entry.request.id} (${o.kind})`, received(this.state, entry.request, o));
  }
  snapshot() {
    this.pendingSnapshots.push(this.server.scene);
    this.log.push(`${this.step}: snapshot taken at edit_version ${this.server.scene.edit_version}`);
  }
  deliverSnapshot() {
    if (this.pendingSnapshots.length === 0) return;
    const snap = this.pendingSnapshots.splice(Math.floor(this.rnd() * this.pendingSnapshots.length), 1)[0];
    this.deliveredRevisions.add(snap.edit_version);
    this.page(`page receives snapshot edit_version ${snap.edit_version}`, sceneSeen(this.state, snap));
  }
  pressSave() { this.page("owner presses «Сохранить сцену»", save(this.state)); }
  pressSaveMine() {
    // the explicit choice: every field in the conflict box, at the edit_version the box shows
    for (const p of conflictPairs(this.state)) this.keptMine[p.field] = Math.max(this.keptMine[p.field], this.state.server.edit_version);
    this.page("owner presses «Сохранить моё»", saveMine(this.state));
  }
  pressTakeServer() {
    for (const f of FIELDS) delete this.lastOwner[f];
    this.page("owner presses «Взять с сервера»", takeServer(this.state));
  }

  run() {
    this.invariants();
    for (this.step = 1; this.step <= STEPS; this.step++) {
      const x = this.rnd();
      if (x < 0.25) this.input();
      else if (x < 0.35) this.otherWrites();
      else if (x < 0.47) this.pressSave();
      else if (x < 0.53) { if (this.state.conflict) this.pressSaveMine(); }
      else if (x < 0.56) { if (this.state.conflict) this.pressTakeServer(); }
      else if (x < 0.70) this.process();
      else if (x < 0.84) this.deliver();
      else if (x < 0.92) this.snapshot();
      else this.deliverSnapshot();
      this.invariants();
    }
    // drain: the owner keeps choosing «Сохранить моё» / saving until the card is clean
    for (let i = 0; i < 40; i++) {
      this.step++;
      while (this.pendingAnswers.length) this.process();
      while ([...this.sent.values()].some((x) => x.outcome && !x.delivered)) this.deliver();
      while (this.pendingSnapshots.length) this.deliverSnapshot();
      this.invariants();
      if (this.state.conflict) this.pressSaveMine();
      else if (dirtyFields(this.state).length) this.pressSave();
      else break;
      this.invariants();
    }
    this.ok(dirtyFields(this.state).length === 0 && !this.state.conflict, () => "could not settle");
    for (const f of FIELDS) {
      const own = this.lastOwner[f];
      if (own) this.ok(this.persisted(f, own), () => `owner's ${f}=${JSON.stringify(own.value)} never reached the server`);
    }
  }
}

describe("scene draft model", () => {
  it(`keeps the invariants over ${RUNS} random runs of ${STEPS} steps`, () => {
    const counts = { conflicts: 0, saves: 0, errors: 0 };
    const played = Object.fromEntries(FIELDS.map((f) => [f, 0])) as Record<Field, number>;
    for (let seed = 1; seed <= RUNS; seed++) {
      const run = new Run(seed);
      try {
        run.run();
      } catch (err) {
        throw new Error(`seed ${seed}: ${(err as Error).message}`);
      }
      for (const f of FIELDS) played[f] += run.played[f];
      for (const s of run.sent.values()) {
        if (s.outcome?.kind === "ok") counts.saves++;
        if (s.outcome?.kind === "conflict") counts.conflicts++;
        if (s.outcome?.kind === "error") counts.errors++;
      }
    }
    console.log(`draft model: ${RUNS} runs × ${STEPS} steps — ${JSON.stringify(counts)}`);
    // the runs really exercise saves, conflicts and failures
    expect(counts.saves).toBeGreaterThanOrEqual(RUNS);
    expect(counts.conflicts).toBeGreaterThanOrEqual(RUNS / 4);
    expect(counts.errors).toBeGreaterThanOrEqual(Math.floor(RUNS / 40));
    // every field of the literal list takes part in the generator (no field is silently skipped)
    expect(FIELDS.filter((f) => played[f] < RUNS)).toEqual([]);
  }, 120_000);
});
