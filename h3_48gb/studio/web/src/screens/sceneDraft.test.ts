import { describe, expect, it } from "vitest";
import { scene } from "@/test/snapshot";
import { FakeSceneServer } from "@/test/sceneServer";
import {
  conflictPairs, dirtyFields, init, input, isStale, received, save, saveMine, sceneSeen, takeServer,
  type DraftState, type Request,
} from "./sceneDraft";

/** Deterministic regressions for every loss of input the reviews r2–r5 found, on the pure draft
 *  state. The model test (sceneDraft.model.test.ts) explores the interleavings at random; these
 *  pin the exact scenarios and the exact bodies. */

const INITIAL = scene({ id: "s_1", revision: 1, edit_version: 1, sound_text: "old sound", music_text: "none" });

/** Drive one request to the server and back: the body sent is what `state.inflight` says. */
function roundTrip(server: FakeSceneServer, state: DraftState) {
  const request = state.inflight!;
  const outcome = server.patch(request.body, "us");
  return { request, outcome, state: received(state, request, outcome) };
}
const bodies = (...requests: Request[]) => requests.map((r) => r.body);

describe("scene draft — regressions r2–r5", () => {
  it("r2 Н1: the base stays while the other window moves on; «Сохранить» meets a 409, not a silent overwrite", () => {
    const server = new FakeSceneServer(INITIAL);
    let s = input(init(INITIAL), "music_text", "mine");
    server.other("music_text", "other");
    s = sceneSeen(s, server.scene);                                   // SSE reload with rev 2
    expect([s.base, s.draft.music_text, isStale(s)]).toEqual([1, "mine", true]);
    s = save(s);
    expect(s.inflight!.body).toEqual({ expected_edit_version: 1, music_text: "mine" });
    const t = roundTrip(server, s);
    expect(t.outcome.kind).toBe("conflict");
    expect(server.scene.music_text).toBe("other");                     // nothing was overwritten
    expect(conflictPairs(t.state)).toEqual([{ field: "music_text", server: "other", mine: "mine" }]);
    const u = roundTrip(server, saveMine(t.state));
    expect(u.request.body).toEqual({ expected_edit_version: 2, music_text: "mine" });
    expect([server.scene.music_text, server.scene.edit_version, u.state.base, u.state.conflict, dirtyFields(u.state)])
      .toEqual(["mine", 3, 3, false, []]);
  });

  it("r3 Н3: a clean card follows the other window; a dirty card keeps its text and takes only the clean fields", () => {
    const server = new FakeSceneServer(INITIAL);
    const clean = sceneSeen(init(INITIAL), server.other("action_text", "Edited elsewhere."));
    expect([clean.draft.action_text, clean.base]).toEqual(["Edited elsewhere.", 2]);
    let dirty = input(init(INITIAL), "music_text", "none and a flute");
    dirty = sceneSeen(dirty, server.other("sound_text", "Wind."));
    expect([dirty.draft.music_text, dirty.draft.sound_text, dirty.base]).toEqual(["none and a flute", "Wind.", 1]);
  });

  it("r3 Б-2: text typed while the request is in flight survives and goes with the next save", () => {
    const server = new FakeSceneServer(INITIAL);
    let s = save(input(init(INITIAL), "music_text", "mine-v1"));
    const request = s.inflight!;
    s = input(s, "music_text", "mine-v1-v2");                          // typed while in flight
    s = received(s, request, server.patch(request.body, "us"));
    expect([s.draft.music_text, s.base, dirtyFields(s)]).toEqual(["mine-v1-v2", 2, ["music_text"]]);
    const t = roundTrip(server, save(s));
    expect(bodies(request, t.request)).toEqual([
      { expected_edit_version: 1, music_text: "mine-v1" },
      { expected_edit_version: 2, music_text: "mine-v1-v2" },
    ]);
    expect(server.scene.music_text).toBe("mine-v1-v2");
  });

  it("r4 №1: after a 409, a delayed «Сохранить моё» keeps the field the other window saved; the next PATCH carries only the new text", () => {
    const server = new FakeSceneServer(INITIAL);
    let s = input(init(INITIAL), "music_text", "mine");
    server.other("sound_text", "new remote sound");
    const first = roundTrip(server, save(s));                          // 409
    expect(first.outcome.kind).toBe("conflict");
    expect(first.state.draft.sound_text).toBe("new remote sound");    // the clean field shows the server's at once
    s = saveMine(first.state);
    const mine = s.inflight!;
    s = input(s, "action_text", "She walks to the lake. Then she sits.");   // typed while in flight
    s = received(s, mine, server.patch(mine.body, "us"));
    expect([s.base, s.draft.sound_text, dirtyFields(s)]).toEqual([3, "new remote sound", ["action_text"]]);
    const next = roundTrip(server, save(s));
    expect(bodies(first.request, mine, next.request)).toEqual([
      { expected_edit_version: 1, music_text: "mine" },
      { expected_edit_version: 2, music_text: "mine" },
      { expected_edit_version: 3, action_text: "She walks to the lake. Then she sits." },
    ]);
    expect([server.scene.sound_text, server.scene.music_text, server.scene.action_text])
      .toEqual(["new remote sound", "mine", "She walks to the lake. Then she sits."]);
  });

  it("r4: «Сохранить моё» sends only the owner's fields — both windows' edits end up on the server", () => {
    const server = new FakeSceneServer(INITIAL);
    const s = input(init(INITIAL), "action_text", "She walks to the lake. Then she sits.");
    server.other("sound_text", "Wind in the reeds.");
    const t = roundTrip(server, saveMine(roundTrip(server, save(s)).state));
    expect(t.request.body).toEqual({ expected_edit_version: 2, action_text: "She walks to the lake. Then she sits." });
    expect([server.scene.action_text, server.scene.sound_text]).toEqual(["She walks to the lake. Then she sits.", "Wind in the reeds."]);
  });

  it("r5: a second press while a request is in flight is queued, not sent — one request per card", () => {
    const server = new FakeSceneServer(INITIAL);
    let s = input(init(INITIAL), "music_text", "mine");
    server.other("sound_text", "remote sound");
    s = saveMine(roundTrip(server, save(s)).state);                     // 409 → «Сохранить моё»
    const mine = s.inflight!;
    s = saveMine(saveMine(s));                                         // pressed twice more while in flight
    expect([s.inflight, s.queued]).toEqual([mine, true]);
    s = received(s, mine, server.patch(mine.body, "us"));
    expect([s.inflight, s.queued, s.base, dirtyFields(s)]).toEqual([null, false, 3, []]);   // nothing left to send
    expect(server.scene.edit_version).toBe(3);
  });

  it("r5: a late answer below the base is ignored — a field edited after the send keeps its value, the base does not go back", () => {
    const server = new FakeSceneServer(INITIAL);
    let s = input(init(INITIAL), "music_text", "mine");
    server.other("sound_text", "remote sound");
    const early = save(s).inflight!;                                   // a request from the old base (rev 1)
    s = sceneSeen(s, server.scene);
    s = saveMine(received(s, early, { kind: "conflict", current: server.scene }));
    let t = roundTrip(server, s);                                      // rev 3
    t = roundTrip(server, save(input(t.state, "seed", "22")));         // rev 4
    s = input(input(t.state, "sound_text", "old sound"), "seed", "23"); // edited after everything was sent
    const stale = received(s, early, { kind: "ok", scene: { ...server.history.get(3)!, sound_text: "remote sound" } });
    expect([stale.draft.sound_text, stale.draft.seed, stale.base, dirtyFields(stale)]).toEqual(["old sound", "23", 4, ["sound_text", "seed"]]);
    expect(save(stale).inflight!.body).toEqual({ expected_edit_version: 4, sound_text: "old sound", seed: 23 });
  });

  it("r5 (mobile): every input counts in the conflict branch too", () => {
    const server = new FakeSceneServer(INITIAL);
    server.other("music_text", "other");
    const s = roundTrip(server, save(input(init(INITIAL), "music_text", "mine"))).state;
    expect([s.conflict, s.inputs]).toEqual([true, 1]);
    expect(input(s, "seed", "5").inputs).toBe(2);
  });
});

describe("scene draft — queue, stale snapshots, the owner's choice", () => {
  it("a press during a request goes out once the answer is in, with the fresh base and only the new field", () => {
    const server = new FakeSceneServer(INITIAL);
    let s = save(input(init(INITIAL), "music_text", "mine"));
    const first = s.inflight!;
    s = save(input(s, "seed", "7"));                                   // typed and pressed while in flight
    expect([s.inflight, s.queued]).toEqual([first, true]);
    s = received(s, first, server.patch(first.body, "us"));
    expect(s.inflight!.body).toEqual({ expected_edit_version: 2, seed: 7 });
    expect(s.queued).toBe(false);
  });

  it("a snapshot older than the one shown is ignored; a newer one updates the «Сервер» side of an open conflict", () => {
    const server = new FakeSceneServer(INITIAL);
    let s = input(init(INITIAL), "music_text", "mine");
    const rev2 = server.other("music_text", "other");
    const rev3 = server.other("music_text", "newer");
    s = sceneSeen(sceneSeen(s, rev3), rev2);
    expect(s.server.edit_version).toBe(3);
    const t = roundTrip(server, save(s)).state;                        // 409 against rev 3
    expect(conflictPairs(t)).toEqual([{ field: "music_text", server: "newer", mine: "mine" }]);
    const rev4 = server.other("music_text", "newest");
    expect(conflictPairs(sceneSeen(t, rev4))).toEqual([{ field: "music_text", server: "newest", mine: "mine" }]);
    expect(sceneSeen(t, rev4).base).toBe(1);                           // still the owner's to decide
  });

  it("«Взять с сервера» takes the newest state seen and makes the card clean at that revision", () => {
    const server = new FakeSceneServer(INITIAL);
    let s = input(init(INITIAL), "music_text", "mine");
    server.other("music_text", "other");
    s = roundTrip(server, save(s)).state;
    s = sceneSeen(s, server.other("sound_text", "later"));
    s = takeServer(s);
    expect([s.draft.music_text, s.draft.sound_text, s.base, s.conflict, dirtyFields(s)]).toEqual(["other", "later", 3, false, []]);
  });

  it("a 409 for a request whose fields the owner has since discarded is not a conflict (found by the model)", () => {
    const server = new FakeSceneServer(INITIAL);
    let s = input(init(INITIAL), "music_text", "mine");
    server.other("music_text", "other");
    s = saveMine(roundTrip(server, save(s)).state);
    const mine = s.inflight!;
    server.other("music_text", "other again");                         // moves on while «Сохранить моё» flies
    s = sceneSeen(s, server.scene);
    s = takeServer(s);                                                 // the owner gives up on «mine»
    s = received(s, mine, server.patch(mine.body, "us"));              // 409
    expect([s.conflict, dirtyFields(s), s.base, s.draft.music_text]).toEqual([false, [], 3, "other again"]);
  });

  it("after «Взять с сервера» during a request, the 200 brings every clean field to the newest state (found by the model)", () => {
    const server = new FakeSceneServer(INITIAL);
    let s = input(init(INITIAL), "sound_text", "t");
    server.other("music_text", "other");
    s = saveMine(roundTrip(server, save(s)).state);
    const mine = s.inflight!;
    s = takeServer(input(s, "sound_text", "s"));                       // typed, then discarded, while in flight
    const ok = server.patch(mine.body, "us");                          // the server took "t" meanwhile
    s = received(s, mine, ok);
    expect([s.draft.sound_text, s.base, dirtyFields(s)]).toEqual(["t", 3, []]);
  });

  it("a failed request keeps the draft dirty and drops the queued press; a stale 409 is ignored", () => {
    const server = new FakeSceneServer(INITIAL);
    let s = save(input(init(INITIAL), "music_text", "mine"));
    const request = s.inflight!;
    s = received(save(s), request, { kind: "error", message: "Сервер не ответил" });
    expect([s.error, s.inflight, s.queued, dirtyFields(s)]).toEqual(["Сервер не ответил", null, false, ["music_text"]]);
    const t = roundTrip(server, save(s)).state;                        // rev 2, base 2
    const u = received(t, request, { kind: "conflict", current: server.history.get(2)! });
    expect([u.conflict, u.base]).toEqual([false, 2]);
  });

  it("r6: states are ordered by edit_version — a late older snapshot never rolls a saved 8.1 back to 8", () => {
    // requested_s 8 → 8.1 keeps 192 frames, so the revision stays 1 (И1); edit_version goes 1 → 2 (И1a)
    const server = new FakeSceneServer(INITIAL);
    let s = save(input(init(INITIAL), "requested_s", "8.1"));
    s = received(s, s.inflight!, server.patch(s.inflight!.body, "us"));
    expect([server.scene.revision, server.scene.edit_version]).toEqual([1, 2]);
    expect([s.draft.requested_s, s.base, dirtyFields(s)]).toEqual(["8.1", 2, []]);
    const late = sceneSeen(s, INITIAL);                                // the snapshot taken before the save
    expect([late.draft.requested_s, late.server.edit_version, late.base]).toEqual(["8.1", 2, 2]);
  });

  it("r6: another tab's requested_s at the same revision is a newer state, and a stale save meets a 409", () => {
    const server = new FakeSceneServer(INITIAL);
    let s = input(init(INITIAL), "requested_s", "8.2");
    const b = server.other("requested_s", "8.1");                      // revision stays 1, edit_version 2
    expect([b.revision, b.edit_version]).toEqual([1, 2]);
    s = sceneSeen(s, b);
    expect([isStale(s), s.base]).toEqual([true, 1]);
    const t = roundTrip(server, save(s));
    expect([t.outcome.kind, t.request.body]).toEqual(["conflict", { expected_edit_version: 1, requested_s: 8.2 }]);
    expect([server.scene.requested_s, conflictPairs(t.state)]).toEqual([8.1, [{ field: "requested_s", server: "8.1", mine: "8.2" }]]);
  });
});
