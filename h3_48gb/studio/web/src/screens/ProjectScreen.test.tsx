import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Scene } from "@/api/types";
import { FakeEventSource, stubFetch } from "@/test/fetchStub";
import { scene, snapshot } from "@/test/snapshot";
import { ProjectScreen } from "./ProjectScreen";

/** waitFor with room: the model test runs in the same Vitest process and can starve a 1 s
 *  default under load — one such stall once read as a red mutation (r7 №23). */
const until = (fn: () => void) => vi.waitFor(fn, { timeout: 5000 });

const LINT = { "POST /api/studio/projects/p_1/lint": { body: { findings: {} } } };

beforeEach(() => {
  FakeEventSource.last = null;
  vi.stubGlobal("EventSource", FakeEventSource);
});

/** The card as it is in the document NOW — never a node remembered before an event (Н3). */
const card = (n: number) => screen.getByRole("article", { name: `Сцена ${n}` });
const field = (n: number, label: RegExp) => within(card(n)).getByLabelText<HTMLTextAreaElement>(label);

/** A server with state for scenes s_1 and s_2: GET answers the current snapshot, PATCH checks
 *  expected_edit_version exactly like ops.update_scene (И1a; 409 with the current scene). Two clients
 *  talk to it: the page under test, and `otherWindow` — plain fetch, as a second tab would. */
function fakeStudio() {
  // `hold()` makes the NEXT PATCH from the page wait until the returned function is called —
  // the owner keeps typing while the request is in flight (review r3, blocker 2).
  let gate: Promise<void> | null = null;
  const hold = () => {
    let open = () => {};
    gate = new Promise<void>((resolve) => { open = resolve; });
    return () => open();
  };
  const scenes: Record<string, Scene> = {
    s_1: scene({ id: "s_1", idx: 0, edit_version: 1, music_text: "none" }),
    s_2: scene({ id: "s_2", idx: 1, edit_version: 1, music_text: "none" }),
  };
  let seq = 5;
  const patch = (id: string) => async (body: unknown) => {
    if (gate) {
      const waiting = gate;
      gate = null;
      await waiting;
    }
    const { expected_edit_version, ...fields } = body as { expected_edit_version: number } & Partial<Scene>;
    const current = scenes[id];
    if (expected_edit_version !== current.edit_version) {
      return { status: 409, body: { error: { code: "revision_conflict", message: "x", detail: { current } } } };
    }
    scenes[id] = { ...current, ...fields, edit_version: current.edit_version + 1 };
    seq += 1;
    return { body: { scene: scenes[id], cascade: [], lint: [] } };
  };
  const calls = stubFetch({
    ...LINT,
    "GET /api/studio/projects/p_1": () => ({ body: snapshot({ scenes: [scenes.s_1, scenes.s_2], last_seq: seq }) }),
    "PATCH /api/studio/scenes/s_1": patch("s_1"),
    "PATCH /api/studio/scenes/s_2": patch("s_2"),
  });
  const otherWindow = (id: string, body: object) =>
    fetch(`/api/studio/scenes/${id}`, { method: "PATCH", body: JSON.stringify(body) });
  const event = (id: string) => FakeEventSource.last!.emit("scene_updated", { project_id: "p_1", scene_id: id }, seq);
  return { scenes, calls, otherWindow, event, hold };
}

describe("ProjectScreen — snapshot, SSE, unsaved edits, conflicts", () => {
  it("a clean card follows the other window; a dirty card keeps its text", async () => {
    const studio = fakeStudio();
    render(<ProjectScreen id="p_1" />);
    await screen.findByRole("article", { name: "Сцена 2" });
    expect(FakeEventSource.last!.url).toBe("/api/studio/events?after=5");
    await userEvent.type(field(2, /Музыка/), " and a flute");
    await studio.otherWindow("s_1", { expected_edit_version: 1, action_text: "Edited elsewhere." });
    studio.event("s_1");
    await until(() => expect(field(1, /Действие/).value).toBe("Edited elsewhere."));
    expect(card(2).isConnected).toBe(true);
    expect(field(2, /Музыка/).value).toBe("none and a flute");
  });

  it("ignores events of another project", async () => {
    const studio = fakeStudio();
    render(<ProjectScreen id="p_1" />);
    await screen.findByRole("article", { name: "Сцена 2" });
    FakeEventSource.last!.emit("scene_updated", { project_id: "p_2", scene_id: "s_9" }, 6);
    await new Promise((r) => setTimeout(r, 20));
    expect(studio.calls.filter((c) => c.key === "GET /api/studio/projects/p_1")).toHaveLength(1);
  });

  it("two clients edit the same field: no silent overwrite — a conflict «сервер / моё», then a choice", async () => {
    const studio = fakeStudio();
    render(<ProjectScreen id="p_1" />);
    await screen.findByRole("article", { name: "Сцена 1" });
    await userEvent.clear(field(1, /Музыка/));
    await userEvent.type(field(1, /Музыка/), "mine");
    // the other window saves the same field, the page learns about it over SSE
    await studio.otherWindow("s_1", { expected_edit_version: 1, music_text: "other" });
    studio.event("s_1");
    await until(() => expect(screen.getByText("Сцену изменили в другом окне — при сохранении покажу, что различается.")).toBeTruthy());
    expect(field(1, /Музыка/).value).toBe("mine");
    await userEvent.click(within(card(1)).getByRole("button", { name: "Сохранить сцену" }));
    const box = await within(card(1)).findByRole("group", { name: "Конфликт правок" });
    expect(within(box).getByRole("alert").textContent).toBe("Сцену изменили в другом окне. Сравните и выберите, что оставить.");
    expect(within(box).getByTestId("conflict-music_text").textContent).toBe("МузыкаСервер: other · Моё: mine");
    expect(studio.scenes.s_1.music_text).toBe("other");                 // nothing was overwritten
    expect(studio.calls.filter((c) => c.key === "PATCH /api/studio/scenes/s_1").map((c) => c.body)).toEqual([
      { expected_edit_version: 1, music_text: "other" },                      // the other window
      { expected_edit_version: 1, music_text: "mine" },                       // ours, from our base
    ]);
    // the conflict survives the reload that the 409 triggered
    await until(() => expect(studio.calls.filter((c) => c.key === "GET /api/studio/projects/p_1").length).toBeGreaterThanOrEqual(3));
    expect(within(card(1)).getByRole("group", { name: "Конфликт правок" })).toBeTruthy();
    await userEvent.click(within(card(1)).getByRole("button", { name: "Сохранить моё" }));
    await until(() => expect(studio.scenes.s_1.music_text).toBe("mine"));
    expect(studio.scenes.s_1.edit_version).toBe(3);
    expect(within(card(1)).queryByRole("group", { name: "Конфликт правок" })).toBeNull();
  });

  it("«Взять с сервера» drops my edit and shows the server's text", async () => {
    const studio = fakeStudio();
    render(<ProjectScreen id="p_1" />);
    await screen.findByRole("article", { name: "Сцена 1" });
    await userEvent.clear(field(1, /Музыка/));
    await userEvent.type(field(1, /Музыка/), "mine");
    await studio.otherWindow("s_1", { expected_edit_version: 1, music_text: "other" });
    await userEvent.click(within(card(1)).getByRole("button", { name: "Сохранить сцену" }));
    await userEvent.click(await within(card(1)).findByRole("button", { name: "Взять с сервера" }));
    expect(field(1, /Музыка/).value).toBe("other");
    expect([studio.scenes.s_1.music_text, studio.scenes.s_1.edit_version]).toEqual(["other", 2]);
  });

  it("a save answer applies only to what was sent: text typed meanwhile survives and goes next", async () => {
    const studio = fakeStudio();
    render(<ProjectScreen id="p_1" />);
    await screen.findByRole("article", { name: "Сцена 1" });
    await userEvent.clear(field(1, /Музыка/));
    await userEvent.type(field(1, /Музыка/), "mine-v1");
    const release = studio.hold();
    await userEvent.click(within(card(1)).getByRole("button", { name: "Сохранить сцену" }));
    await userEvent.type(field(1, /Музыка/), "-v2");                 // typed while in flight
    release();
    await until(() => expect(studio.scenes.s_1.edit_version).toBe(2));
    await until(() => expect(studio.calls.filter((c) => c.key === "GET /api/studio/projects/p_1").length).toBeGreaterThanOrEqual(2));
    expect(field(1, /Музыка/).value).toBe("mine-v1-v2");
    await userEvent.click(within(card(1)).getByRole("button", { name: "Сохранить сцену" }));
    await until(() => expect(studio.scenes.s_1.music_text).toBe("mine-v1-v2"));
    expect(studio.calls.filter((c) => c.key === "PATCH /api/studio/scenes/s_1").map((c) => c.body)).toEqual([
      { expected_edit_version: 1, music_text: "mine-v1" },
      { expected_edit_version: 2, music_text: "mine-v1-v2" },
    ]);
  });

  it("the same holds for «Сохранить моё» after a 409", async () => {
    const studio = fakeStudio();
    render(<ProjectScreen id="p_1" />);
    await screen.findByRole("article", { name: "Сцена 1" });
    await userEvent.clear(field(1, /Музыка/));
    await userEvent.type(field(1, /Музыка/), "mine");
    await studio.otherWindow("s_1", { expected_edit_version: 1, music_text: "other" });
    await userEvent.click(within(card(1)).getByRole("button", { name: "Сохранить сцену" }));
    await within(card(1)).findByRole("group", { name: "Конфликт правок" });
    const release = studio.hold();
    await userEvent.click(within(card(1)).getByRole("button", { name: "Сохранить моё" }));
    await userEvent.type(field(1, /Музыка/), "-v2");
    release();
    await until(() => expect(studio.scenes.s_1.edit_version).toBe(3));
    expect([studio.scenes.s_1.music_text, field(1, /Музыка/).value]).toEqual(["mine", "mine-v2"]);
    await userEvent.click(within(card(1)).getByRole("button", { name: "Сохранить сцену" }));
    await until(() => expect(studio.scenes.s_1.music_text).toBe("mine-v2"));
    expect(studio.calls.filter((c) => c.key === "PATCH /api/studio/scenes/s_1").map((c) => c.body).slice(1)).toEqual([
      { expected_edit_version: 1, music_text: "mine" },
      { expected_edit_version: 2, music_text: "mine" },
      { expected_edit_version: 3, music_text: "mine-v2" },
    ]);
  });

  it("after a 409, a delayed «Сохранить моё» keeps the field the other window saved", async () => {
    // review r4 blocker: the answer must not turn the other window's sound into a local edit
    const studio = fakeStudio();
    render(<ProjectScreen id="p_1" />);
    await screen.findByRole("article", { name: "Сцена 1" });
    await userEvent.clear(field(1, /Музыка/));
    await userEvent.type(field(1, /Музыка/), "mine");
    await studio.otherWindow("s_1", { expected_edit_version: 1, sound_text: "new remote sound" });
    await userEvent.click(within(card(1)).getByRole("button", { name: "Сохранить сцену" }));
    await within(card(1)).findByRole("group", { name: "Конфликт правок" });
    const release = studio.hold();
    await userEvent.click(within(card(1)).getByRole("button", { name: "Сохранить моё" }));
    await userEvent.type(field(1, /Действие/), " Then she sits.");        // typed while in flight
    release();
    await until(() => expect(studio.scenes.s_1.edit_version).toBe(3));
    await until(() => expect(field(1, /Звук действий/).value).toBe("new remote sound"));
    await userEvent.click(within(card(1)).getByRole("button", { name: "Сохранить сцену" }));
    await until(() => expect(studio.scenes.s_1.edit_version).toBe(4));
    expect(studio.calls.filter((c) => c.key === "PATCH /api/studio/scenes/s_1").map((c) => c.body).slice(1)).toEqual([
      { expected_edit_version: 1, music_text: "mine" },
      { expected_edit_version: 2, music_text: "mine" },
      { expected_edit_version: 3, action_text: "She walks to the lake. Then she sits." },
    ]);
    expect([studio.scenes.s_1.sound_text, studio.scenes.s_1.music_text, studio.scenes.s_1.action_text]).toEqual([
      "new remote sound", "mine", "She walks to the lake. Then she sits."]);
  });

  it("«Сохранить моё» sends only the owner's fields: both windows' edits end up on the server", async () => {
    const studio = fakeStudio();
    render(<ProjectScreen id="p_1" />);
    await screen.findByRole("article", { name: "Сцена 1" });
    await userEvent.type(field(1, /Действие/), " Then she sits.");
    await studio.otherWindow("s_1", { expected_edit_version: 1, sound_text: "Wind in the reeds." });
    await userEvent.click(within(card(1)).getByRole("button", { name: "Сохранить сцену" }));
    await userEvent.click(await within(card(1)).findByRole("button", { name: "Сохранить моё" }));
    await until(() => expect(studio.scenes.s_1.edit_version).toBe(3));
    expect(studio.calls.filter((c) => c.key === "PATCH /api/studio/scenes/s_1").map((c) => c.body).at(-1)).toEqual(
      { expected_edit_version: 2, action_text: "She walks to the lake. Then she sits." });
    expect([studio.scenes.s_1.action_text, studio.scenes.s_1.sound_text]).toEqual([
      "She walks to the lake. Then she sits.", "Wind in the reeds."]);
  });

  it("r5: a second «Сохранить» while a request is in flight is queued — one request per card, nothing lost", async () => {
    const studio = fakeStudio();
    render(<ProjectScreen id="p_1" />);
    await screen.findByRole("article", { name: "Сцена 1" });
    await userEvent.clear(field(1, /Музыка/));
    await userEvent.type(field(1, /Музыка/), "mine");
    const release = studio.hold();
    await userEvent.click(within(card(1)).getByRole("button", { name: "Сохранить сцену" }));
    await userEvent.type(field(1, /Действие/), " Then she sits.");        // typed while in flight …
    await userEvent.click(within(card(1)).getByRole("button", { name: "Сохранить сцену" }));   // … and pressed again
    expect(studio.calls.filter((c) => c.key === "PATCH /api/studio/scenes/s_1")).toHaveLength(1);
    release();
    await until(() => expect(studio.scenes.s_1.edit_version).toBe(3));
    expect(studio.calls.filter((c) => c.key === "PATCH /api/studio/scenes/s_1").map((c) => c.body)).toEqual([
      { expected_edit_version: 1, music_text: "mine" },
      { expected_edit_version: 2, action_text: "She walks to the lake. Then she sits." },
    ]);
    await until(() => expect(within(card(1)).getByRole<HTMLButtonElement>("button", { name: "Сохранить сцену" }).disabled).toBe(true));
    expect([field(1, /Музыка/).value, field(1, /Действие/).value]).toEqual(["mine", "She walks to the lake. Then she sits."]);
  });
});

const project = (title: string, revision: number) => ({
  id: "p_1", revision, title, frame_format: "16:9", idea_text: "", style_block: "",
  created_at: "2026-10-21T10:00:00.000+00:00",
});
const snap = (title: string, revision: number, last_seq: number) => ({
  project: project(title, revision), scenes: [], cast: [], takes: [], tasks: [], last_seq,
});


describe("ProjectScreen — S0 (snapshot, events, errors)", () => {
  it("subscribes after the snapshot's last_seq and reloads on its own project's event", async () => {
    stubFetch({ ...LINT, "GET /api/studio/projects/p_1": [{ body: snap("Лес", 1, 5) }, { body: snap("Озеро", 2, 6) }] });
    render(<ProjectScreen id="p_1" />);
    await screen.findByRole("heading", { name: "Лес" });
    // the subscription is made in a passive effect, which may run just after the heading appears
    await waitFor(() => expect(FakeEventSource.last?.url).toBe("/api/studio/events?after=5"));
    FakeEventSource.last!.emit("project_updated", { project_id: "p_1" }, 6);
    await screen.findByRole("heading", { name: "Озеро" });
    await waitFor(() => expect(FakeEventSource.last?.url).toBe("/api/studio/events?after=6"));
  });

  it("ignores another project's events", async () => {
    const calls = stubFetch({ ...LINT, "GET /api/studio/projects/p_1": { body: snap("Лес", 1, 5) } });
    render(<ProjectScreen id="p_1" />);
    await screen.findByRole("heading", { name: "Лес" });
    await waitFor(() => expect(FakeEventSource.last).not.toBeNull());
    FakeEventSource.last!.emit("project_updated", { project_id: "p_2" }, 6);
    await new Promise((r) => setTimeout(r, 20));
    expect(calls.map((c) => c.key).filter((k) => k !== "POST /api/studio/projects/p_1/lint")).toEqual(["GET /api/studio/projects/p_1"]);
  });

  it("shows the project's format and idea", async () => {
    const withIdea = { ...snap("Лес", 1, 5), project: { ...project("Лес", 1), frame_format: "9:16", idea_text: "девушка у воды" } };
    stubFetch({ ...LINT, "GET /api/studio/projects/p_1": { body: withIdea } });
    render(<ProjectScreen id="p_1" />);
    await screen.findByRole("heading", { name: "Лес" });
    expect(screen.getByText("Проект · 9:16")).toBeTruthy();
    expect(screen.getByText("девушка у воды", { selector: "p" })).toBeTruthy();
  });

  it("shows the server's message when the project is not found", async () => {
    stubFetch({
      "GET /api/studio/projects/p_1": {
        status: 404,
        body: { error: { code: "project_not_found", message: "Проект не найден", detail: {} } },
      },
    });
    render(<ProjectScreen id="p_1" />);
    expect((await screen.findByRole("alert")).textContent).toBe("Проект не найден");
  });

  it("a late answer to an older request does not overwrite a newer snapshot", async () => {
    let releaseOld!: () => void;
    const heldOld = new Promise<void>((r) => (releaseOld = r));
    stubFetch({
      ...LINT,
      "GET /api/studio/projects/p_1": [
        { body: snap("Лес", 1, 5) },
        async () => {            // the reload started by the event: slow
          await heldOld;
          return { body: snap("Старое", 1, 5) };
        },
        { body: snap("Новое", 2, 6) },
      ],
    });
    render(<ProjectScreen id="p_1" />);
    await screen.findByRole("heading", { name: "Лес" });
    await waitFor(() => expect(FakeEventSource.last).not.toBeNull());
    const first = FakeEventSource.last!;
    first.emit("project_updated", { project_id: "p_1" }, 6);       // request 2 (held)
    first.emit("project_updated", { project_id: "p_1" }, 7);       // request 3 (fast, newer)
    await screen.findByRole("heading", { name: "Новое" });
    releaseOld();
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.getByRole("heading", { name: "Новое" })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Старое" })).toBeNull();
  });
});
