import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { stubFetch } from "@/test/fetchStub";
import { scene, snapshot } from "@/test/snapshot";
import { ScenarioStep } from "./ScenarioStep";

const LINT_EMPTY = { "POST /api/studio/projects/p_1/lint": { body: { findings: {} } } };
const CAST = [{ id: "c_1", tag: "anna", description_ru: "ж", description_en: "A woman.", portrait_phrase: "a woman", assets: [] }];
const PROPOSED = {
  mode: "refs", action_text: "She walks.", performance_text: "Her gaze drops.", sound_text: "Bare heels on planks.",
  music_text: "", state_in_text: "", state_out_text: "", requested_s: 8.2, seed: null,
  source_frame: { kind: "none" }, refs: ["anna"],
};

describe("ScenarioStep", () => {
  it("proposes, previews mode, sound and the snapped length, then applies with the project revision", async () => {
    const calls = stubFetch({
      ...LINT_EMPTY,
      "POST /api/studio/projects/p_1/director/propose": {
        body: { proposal_id: "r_1", scenes: [PROPOSED], durations: [{ frames: 192, duration_s: 8 }], dropped_tags: ["ghost"] },
      },
      "POST /api/studio/projects/p_1/director/apply": { body: { project: {}, scenes: [], diff: {}, lint: {} } },
    });
    const onChanged = vi.fn();
    // one scene already there: the proposal replaces it, so apply names it with its version (r7)
    render(<ScenarioStep snap={snapshot({ scenes: [scene({ edit_version: 3 })] })} onChanged={onChanged} />);
    const count = screen.getByLabelText("Сцен");
    await userEvent.clear(count);
    await userEvent.type(count, "1");
    await userEvent.click(screen.getByRole("button", { name: "Предложить сцены" }));
    const region = await screen.findByRole("region", { name: "Предложение сценариста" });
    const item = within(region).getByRole("listitem", { name: "Предложенная сцена 1" });
    expect(item.textContent).toBe("Сцена 1Референсы192 кадров · 8,000 сShe walks.Her gaze drops.Bare heels on planks.");
    expect(within(region).getByText(/вне кастинга/).textContent).toBe(
      "Сценарист назвал героев вне кастинга: @ghost — они не попали в сцены.");
    await userEvent.click(within(region).getByRole("button", { name: "Применить" }));
    await vi.waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
    expect(calls.filter((c) => !c.key.endsWith("/lint")).map(({ key, body }) => ({ key, body }))).toEqual([
      { key: "POST /api/studio/projects/p_1/director/propose", body: { idea_text: "девушка у озера", scenes_count: 1 } },
      { key: "POST /api/studio/projects/p_1/director/apply", body: { proposal_id: "r_1", expected_revision: 3, deleted: [{ scene_id: "s_1", expected_edit_version: 3 }] } },
    ]);
  });

  it("saves only the edited fields — text, mode and who is on screen — with the scene's own edit_version", async () => {
    const calls = stubFetch({
      ...LINT_EMPTY,
      "PATCH /api/studio/scenes/s_1": { body: { scene: scene({ edit_version: 1 }), cascade: [], lint: [] } },
    });
    const snap = snapshot({ cast: CAST, scenes: [scene({ mode: "from_frame", refs: [] })] });
    render(<ScenarioStep snap={snap} onChanged={() => {}} />);
    const card = screen.getByRole("article", { name: "Сцена 1" });
    const sound = within(card).getByLabelText(/Звук действий/);
    await userEvent.clear(sound);
    await userEvent.type(sound, "Wind in the reeds.");
    await userEvent.click(within(card).getByRole("button", { name: "Референсы" }));
    await userEvent.click(within(card).getByRole("button", { name: "@anna" }));
    await userEvent.click(within(card).getByRole("button", { name: "Сохранить сцену" }));
    await vi.waitFor(() => expect(calls.some((c) => c.key === "PATCH /api/studio/scenes/s_1")).toBe(true));
    expect(calls.find((c) => c.key === "PATCH /api/studio/scenes/s_1")!.body).toEqual({
      expected_edit_version: 0, sound_text: "Wind in the reeds.", mode: "refs", refs: ["anna"],
    });
  });

  it("shows lint findings under the field they belong to", async () => {
    stubFetch({
      "POST /api/studio/projects/p_1/lint": { body: { findings: { s_1: [
        { rule: "L4", severity: "error", scene_id: "s_1", field: "sound_text", message: "Кириллица в поле промпта: H3 ждёт английский" },
      ] } } },
    });
    render(<ScenarioStep snap={snapshot({ scenes: [scene()] })} onChanged={() => {}} />);
    const finding = await screen.findByText("Кириллица в поле промпта: H3 ждёт английский");
    const field = finding.closest("li")!.parentElement!.parentElement!;
    expect(within(field).getByLabelText(/Звук действий/)).toBeTruthy();
  });

  it("adds a scene that continues from the previous one, keeping existing ids", async () => {
    const calls = stubFetch({
      ...LINT_EMPTY,
      "PUT /api/studio/projects/p_1/scenes": { body: { project: {}, scenes: [], diff: {}, lint: {} } },
    });
    render(<ScenarioStep snap={snapshot({ scenes: [scene({ edit_version: 4 })] })} onChanged={() => {}} />);
    await userEvent.click(screen.getByRole("button", { name: "Сцена" }));
    await vi.waitFor(() => expect(calls.some((c) => c.key === "PUT /api/studio/projects/p_1/scenes")).toBe(true));
    expect(calls.find((c) => c.key === "PUT /api/studio/projects/p_1/scenes")!.body).toEqual({
      expected_revision: 3,
      scenes: [
        { scene_id: "s_1", expected_edit_version: 4, mode: "refs", action_text: "She walks to the lake.", performance_text: "Her gaze drops, then she smiles.", sound_text: "Bare heels on wet planks.", music_text: "none", state_in_text: "", state_out_text: "", requested_s: 8, seed: 11, source_frame: { kind: "none", scene_id: null, asset_id: null }, refs: ["anna"] },
        { mode: "from_frame", requested_s: 8, source_frame: { kind: "prev_last_frame" }, refs: [] },
      ],
      deleted: [],
    });
  });

  // ---- review r8: an operation on the set changes nothing in a scene it is not about -----------

  /** Three saved scenes; S3's frame comes from S1 (an explicit branch the owner chose, O8), S2's
   *  from an asset. Their O7 copies are written out by hand — every field as stored. */
  const SAVED = [
    scene({ id: "s_1", idx: 0, edit_version: 2, action_text: "One.", state_in_text: "in 1", state_out_text: "out 1" }),
    scene({ id: "s_2", idx: 1, edit_version: 5, mode: "from_frame", refs: [], requested_s: 8.1, seed: 22,
            source_frame: { kind: "asset", scene_id: null, asset_id: "a_9", auto: false } }),
    scene({ id: "s_3", idx: 2, edit_version: 7, mode: "from_frame", refs: [], music_text: "A cello.",
            source_frame: { kind: "prev_last_frame", scene_id: "s_1", asset_id: null, auto: false } }),
  ];
  const COPY: Record<string, object> = {
    s_1: { scene_id: "s_1", expected_edit_version: 2, mode: "refs", action_text: "One.", performance_text: "Her gaze drops, then she smiles.", sound_text: "Bare heels on wet planks.", music_text: "none", state_in_text: "in 1", state_out_text: "out 1", requested_s: 8, seed: 11, source_frame: { kind: "none", scene_id: null, asset_id: null }, refs: ["anna"] },
    s_2: { scene_id: "s_2", expected_edit_version: 5, mode: "from_frame", action_text: "She walks to the lake.", performance_text: "Her gaze drops, then she smiles.", sound_text: "Bare heels on wet planks.", music_text: "none", state_in_text: "", state_out_text: "", requested_s: 8.1, seed: 22, source_frame: { kind: "asset", scene_id: null, asset_id: "a_9" }, refs: [] },
    s_3: { scene_id: "s_3", expected_edit_version: 7, mode: "from_frame", action_text: "She walks to the lake.", performance_text: "Her gaze drops, then she smiles.", sound_text: "Bare heels on wet planks.", music_text: "A cello.", state_in_text: "", state_out_text: "", requested_s: 8, seed: 11, source_frame: { kind: "prev_last_frame", scene_id: "s_1", asset_id: null }, refs: [] },
  };
  const putBody = (calls: { key: string; body: unknown }[]) =>
    calls.find((c) => c.key === "PUT /api/studio/projects/p_1/scenes")!.body as { scenes: { scene_id?: string }[]; deleted: unknown };
  const sameBytes = (a: unknown, b: unknown) => expect(JSON.stringify(a)).toBe(JSON.stringify(b));

  it("«+ Сцена»: S3 keeps its frame source S1, every saved scene goes byte for byte as stored", async () => {
    const calls = stubFetch({ ...LINT_EMPTY, "PUT /api/studio/projects/p_1/scenes": { body: { project: {}, scenes: [], diff: {}, lint: {} } } });
    render(<ScenarioStep snap={snapshot({ scenes: SAVED })} onChanged={() => {}} />);
    await userEvent.click(screen.getByRole("button", { name: "Сцена" }));
    await vi.waitFor(() => expect(calls.some((c) => c.key === "PUT /api/studio/projects/p_1/scenes")).toBe(true));
    const body = putBody(calls);
    sameBytes(body.scenes.slice(0, 3), [COPY.s_1, COPY.s_2, COPY.s_3]);
    expect([body.scenes.length, body.deleted]).toEqual([4, []]);
  });

  it("«Удалить сцену»: the others go as stored, only a scene continuing the deleted one is re-linked", async () => {
    const calls = stubFetch({ ...LINT_EMPTY, "PUT /api/studio/projects/p_1/scenes": { body: { project: {}, scenes: [], diff: {}, lint: {} } } });
    render(<ScenarioStep snap={snapshot({ scenes: SAVED })} onChanged={() => {}} />);
    await userEvent.click(screen.getByRole("button", { name: "Удалить сцену 2" }));          // S2 is nobody's source
    await vi.waitFor(() => expect(calls.some((c) => c.key === "PUT /api/studio/projects/p_1/scenes")).toBe(true));
    const body = putBody(calls);
    sameBytes(body.scenes, [COPY.s_1, COPY.s_3]);
    expect(body.deleted).toEqual([{ scene_id: "s_2", expected_edit_version: 5 }]);
  });

  it("«Применить»: no scene fields at all, only the versions of the set it replaces", async () => {
    const calls = stubFetch({
      ...LINT_EMPTY,
      "POST /api/studio/projects/p_1/director/propose": { body: { proposal_id: "r_1", scenes: [PROPOSED], durations: [{ frames: 192, duration_s: 8 }], dropped_tags: [] } },
      "POST /api/studio/projects/p_1/director/apply": { body: { project: {}, scenes: [], diff: {}, lint: {} } },
    });
    render(<ScenarioStep snap={snapshot({ scenes: SAVED })} onChanged={() => {}} />);
    await userEvent.click(screen.getByRole("button", { name: "Предложить сцены" }));
    await userEvent.click(await screen.findByRole("button", { name: "Применить" }));
    await vi.waitFor(() => expect(calls.some((c) => c.key === "POST /api/studio/projects/p_1/director/apply")).toBe(true));
    sameBytes(calls.find((c) => c.key === "POST /api/studio/projects/p_1/director/apply")!.body, {
      proposal_id: "r_1", expected_revision: 3,
      deleted: [{ scene_id: "s_1", expected_edit_version: 2 }, { scene_id: "s_2", expected_edit_version: 5 }, { scene_id: "s_3", expected_edit_version: 7 }],
    });
  });

  it("deleting the middle of a chain re-links only the scene that continued it, explicitly", async () => {
    // review finding 10 (r1) and r8: S3 continued S2 → it now continues S1, named by id; S4 chose
    // S1 explicitly and goes exactly as stored
    const calls = stubFetch({ ...LINT_EMPTY, "PUT /api/studio/projects/p_1/scenes": { body: { project: {}, scenes: [], diff: {}, lint: {} } } });
    const chain = [
      scene({ id: "s_1", idx: 0 }),
      scene({ id: "s_2", idx: 1, source_frame: { kind: "prev_last_frame", scene_id: "s_1", asset_id: null, auto: false } }),
      scene({ id: "s_3", idx: 2, source_frame: { kind: "prev_last_frame", scene_id: "s_2", asset_id: null, auto: false } }),
      scene({ id: "s_4", idx: 3, source_frame: { kind: "prev_last_frame", scene_id: "s_1", asset_id: null, auto: false } }),
    ];
    render(<ScenarioStep snap={snapshot({ scenes: chain })} onChanged={() => {}} />);
    await userEvent.click(screen.getByRole("button", { name: "Удалить сцену 2" }));
    await vi.waitFor(() => expect(calls.some((c) => c.key === "PUT /api/studio/projects/p_1/scenes")).toBe(true));
    const body = putBody(calls) as unknown as { scenes: { scene_id: string; source_frame: unknown }[]; deleted: unknown };
    expect(body.deleted).toEqual([{ scene_id: "s_2", expected_edit_version: 0 }]);
    expect(body.scenes.map((s) => [s.scene_id, s.source_frame])).toEqual([
      ["s_1", { kind: "none", scene_id: null, asset_id: null }],
      ["s_3", { kind: "prev_last_frame", scene_id: "s_1", asset_id: null }],
      ["s_4", { kind: "prev_last_frame", scene_id: "s_1", asset_id: null }],
    ]);
  });

  // ---- the order-derived source (migration 002): O7 forbids `auto` on input (400) --------------

  const FRAME_ASSET = { kind: "asset", scene_id: null, asset_id: "a_9", auto: false };
  const derived = (id: string, idx: number, from: string) =>
    scene({ id, idx, source_frame: { kind: "prev_last_frame", scene_id: from, asset_id: null, auto: true } });

  it("a frame source derived from the order goes without scene_id and without `auto`; an explicit one keeps its scene_id", async () => {
    const calls = stubFetch({ ...LINT_EMPTY, "PUT /api/studio/projects/p_1/scenes": { body: { project: {}, scenes: [], diff: {}, lint: {} } } });
    const saved = [
      scene({ id: "s_1", idx: 0 }),
      derived("s_2", 1, "s_1"),
      scene({ id: "s_3", idx: 2, source_frame: { kind: "prev_last_frame", scene_id: "s_1", asset_id: null, auto: false } }),
      scene({ id: "s_4", idx: 3, source_frame: FRAME_ASSET as never }),
    ];
    render(<ScenarioStep snap={snapshot({ scenes: saved })} onChanged={() => {}} />);
    await userEvent.click(screen.getByRole("button", { name: "Сцена" }));
    await vi.waitFor(() => expect(calls.some((c) => c.key === "PUT /api/studio/projects/p_1/scenes")).toBe(true));
    const sent = putBody(calls) as unknown as { scenes: { source_frame: unknown }[] };
    sameBytes(sent.scenes.slice(0, 4).map((x) => x.source_frame), [
      { kind: "none", scene_id: null, asset_id: null },
      { kind: "prev_last_frame" },
      { kind: "prev_last_frame", scene_id: "s_1", asset_id: null },
      { kind: "asset", scene_id: null, asset_id: "a_9" },
    ]);
    expect(JSON.stringify(sent)).not.toContain('"auto"');
  });

  it("deleting the middle of an order-derived chain: the continuation stays derived (the server re-derives it), nothing is named", async () => {
    const calls = stubFetch({ ...LINT_EMPTY, "PUT /api/studio/projects/p_1/scenes": { body: { project: {}, scenes: [], diff: {}, lint: {} } } });
    render(<ScenarioStep snap={snapshot({ scenes: [scene({ id: "s_1", idx: 0 }), derived("s_2", 1, "s_1"), derived("s_3", 2, "s_2")] })} onChanged={() => {}} />);
    await userEvent.click(screen.getByRole("button", { name: "Удалить сцену 2" }));
    await vi.waitFor(() => expect(calls.some((c) => c.key === "PUT /api/studio/projects/p_1/scenes")).toBe(true));
    const sent = putBody(calls) as unknown as { scenes: { scene_id: string; source_frame: unknown }[] };
    sameBytes(sent.scenes.map((x) => [x.scene_id, x.source_frame]), [
      ["s_1", { kind: "none", scene_id: null, asset_id: null }],
      ["s_3", { kind: "prev_last_frame" }],
    ]);
  });

  it("the «от кадра сцены N» badge is not shown on the first scene, whose derived source has no scene before it", () => {
    stubFetch({ ...LINT_EMPTY });
    render(<ScenarioStep snap={snapshot({ scenes: [derived("s_1", 0, "s_0"), derived("s_2", 1, "s_1")] })} onChanged={() => {}} />);
    expect(screen.queryByText("от кадра сцены 0")).toBeNull();
    expect(screen.getByText("от кадра сцены 1")).toBeTruthy();
  });

  // ---- review r1 ---------------------------------------------------------------------------------

  const ERR = (status: number, code: string, message: string) => ({ status, body: { error: { code, message, detail: {} } } });
  const PUT_OK = { body: { project: {}, scenes: [], diff: {}, lint: {} } };

  it("r1 №7: «Предложить сцены» with the LLM down shows the server's message and leaves the button usable", async () => {
    stubFetch({ ...LINT_EMPTY, "POST /api/studio/projects/p_1/director/propose": ERR(503, "llm_not_configured", "Сценарист не настроен") });
    render(<ScenarioStep snap={snapshot()} onChanged={() => {}} />);
    await userEvent.click(screen.getByRole("button", { name: "Предложить сцены" }));
    expect((await screen.findByRole("alert")).textContent).toBe("Сценарист не настроен");
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "Предложить сцены" }).disabled).toBe(false);
  });

  it("r1 №7: a 409 on «+ Сцена» reloads the snapshot and shows the message; a 400 shows the message without a reload", async () => {
    stubFetch({
      ...LINT_EMPTY,
      "PUT /api/studio/projects/p_1/scenes": [ERR(409, "revision_conflict", "Проект изменили в другом окне"), ERR(400, "args_invalid", "Неверные данные")],
    });
    const onChanged = vi.fn();
    render(<ScenarioStep snap={snapshot({ scenes: [scene()] })} onChanged={onChanged} />);
    await userEvent.click(screen.getByRole("button", { name: "Сцена" }));
    expect((await screen.findByRole("alert")).textContent).toBe("Проект изменили в другом окне");
    expect(onChanged).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByRole("button", { name: "Сцена" }));
    await vi.waitFor(() => expect(screen.getByRole("alert").textContent).toBe("Неверные данные"));
    expect(onChanged).toHaveBeenCalledTimes(1);
  });

  it("r1 №7: a 409 on «Применить» reloads the snapshot; a successful apply hides the proposal", async () => {
    const calls = stubFetch({
      ...LINT_EMPTY,
      "POST /api/studio/projects/p_1/director/propose": { body: { proposal_id: "r_1", scenes: [PROPOSED], durations: [{ frames: 192, duration_s: 8 }], dropped_tags: [] } },
      "POST /api/studio/projects/p_1/director/apply": [ERR(409, "revision_conflict", "Проект изменили в другом окне"), PUT_OK],
    });
    const onChanged = vi.fn();
    render(<ScenarioStep snap={snapshot()} onChanged={onChanged} />);
    await userEvent.click(screen.getByRole("button", { name: "Предложить сцены" }));
    await userEvent.click(await screen.findByRole("button", { name: "Применить" }));
    await vi.waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
    expect(screen.getByRole("region", { name: "Предложение сценариста" })).toBeTruthy();      // not applied: still there
    await userEvent.click(screen.getByRole("button", { name: "Применить" }));
    await vi.waitFor(() => expect(onChanged).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("region", { name: "Предложение сценариста" })).toBeNull();
    expect(calls.filter((c) => c.key.endsWith("/apply"))).toHaveLength(2);
  });

  it("r1 №7: a failed save shows the server's message, and the button saves again", async () => {
    const calls = stubFetch({
      ...LINT_EMPTY,
      "PATCH /api/studio/scenes/s_1": [ERR(502, "llm_failed", "Сервер временно недоступен"), { body: { scene: scene({ edit_version: 1, music_text: "none!" }), cascade: [], lint: [] } }],
    });
    render(<ScenarioStep snap={snapshot({ scenes: [scene()] })} onChanged={() => {}} />);
    const card = screen.getByRole("article", { name: "Сцена 1" });
    await userEvent.type(within(card).getByLabelText(/Музыка/), "!");
    await userEvent.click(within(card).getByRole("button", { name: "Сохранить сцену" }));
    expect((await within(card).findByRole("alert")).textContent).toBe("Сервер временно недоступен");
    await userEvent.click(within(card).getByRole("button", { name: "Сохранить сцену" }));
    await vi.waitFor(() => expect(within(card).queryByRole("alert")).toBeNull());
    expect(calls.filter((c) => c.key === "PATCH /api/studio/scenes/s_1").map((c) => c.body)).toEqual([
      { expected_edit_version: 0, music_text: "none!" }, { expected_edit_version: 0, music_text: "none!" }]);
  });

  it("r1 №7: a new snapshot (last_seq) asks the linter again", async () => {
    const calls = stubFetch({ ...LINT_EMPTY });
    const { rerender } = render(<ScenarioStep snap={snapshot({ scenes: [scene()], last_seq: 5 })} onChanged={() => {}} />);
    await vi.waitFor(() => expect(calls).toHaveLength(1));
    rerender(<ScenarioStep snap={snapshot({ scenes: [scene()], last_seq: 6 })} onChanged={() => {}} />);
    await vi.waitFor(() => expect(calls).toHaveLength(2));
  });

  it("r1 №4: a late answer of an older lint request does not replace the newer one", async () => {
    let releaseOld!: () => void;
    const heldOld = new Promise<void>((r) => (releaseOld = r));
    const finding = (message: string) => ({ rule: "L4", severity: "error", scene_id: "s_1", field: "sound_text", message });
    stubFetch({
      "POST /api/studio/projects/p_1/lint": [
        async () => { await heldOld; return { body: { findings: { s_1: [finding("Старая находка")] } } }; },
        { body: { findings: { s_1: [finding("Новая находка")] } } },
      ],
    });
    const { rerender } = render(<ScenarioStep snap={snapshot({ scenes: [scene()], last_seq: 5 })} onChanged={() => {}} />);
    rerender(<ScenarioStep snap={snapshot({ scenes: [scene()], last_seq: 6 })} onChanged={() => {}} />);
    await screen.findByText("Новая находка");
    releaseOld();
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.getByText("Новая находка")).toBeTruthy();
    expect(screen.queryByText("Старая находка")).toBeNull();
  });

  it("r1 №1: a scene deleted in another window keeps its unsaved draft as a card; «Вернуть как новую сцену» adds it at the end", async () => {
    const calls = stubFetch({ ...LINT_EMPTY, "PUT /api/studio/projects/p_1/scenes": PUT_OK });
    const one = scene({ id: "s_1", idx: 0 });
    const two = scene({ id: "s_2", idx: 1 });
    const { rerender } = render(<ScenarioStep snap={snapshot({ scenes: [one, two], last_seq: 5 })} onChanged={() => {}} />);
    await userEvent.type(within(screen.getByRole("article", { name: "Сцена 2" })).getByLabelText(/Музыка/), "!");
    rerender(<ScenarioStep snap={snapshot({ scenes: [one], last_seq: 6 })} onChanged={() => {}} />);       // deleted elsewhere
    const card = screen.getByRole("article", { name: "Сцена 2" });
    expect(within(card).getByRole("status").textContent).toBe("Сцену удалили в другом окне. Ваш черновик сохранён здесь — верните его как новую сцену или уберите.");
    expect(within(card).getByLabelText<HTMLTextAreaElement>(/Музыка/).value).toBe("none!");
    expect(within(card).queryByRole("button", { name: "Сохранить сцену" })).toBeNull();
    await userEvent.click(within(card).getByRole("button", { name: "Вернуть как новую сцену" }));
    await vi.waitFor(() => expect(calls.some((c) => c.key === "PUT /api/studio/projects/p_1/scenes")).toBe(true));
    const body = putBody(calls) as unknown as { scenes: unknown[]; deleted: unknown[] };
    expect(body.scenes).toHaveLength(2);
    expect(body.scenes[1]).toEqual({
      mode: "refs", action_text: "She walks to the lake.", performance_text: "Her gaze drops, then she smiles.",
      sound_text: "Bare heels on wet planks.", music_text: "none!", requested_s: 8, seed: 11, refs: ["anna"],
      source_frame: { kind: "prev_last_frame" },
    });
    expect(body.deleted).toEqual([]);
    await vi.waitFor(() => expect(screen.queryByRole("article", { name: "Сцена 2" })).toBeNull());
  });

  it("r1 №1: «Убрать» drops the orphan card; a clean card of a deleted scene just disappears", async () => {
    stubFetch({ ...LINT_EMPTY });
    const [one, two, three] = [scene({ id: "s_1", idx: 0 }), scene({ id: "s_2", idx: 1 }), scene({ id: "s_3", idx: 2 })];
    const { rerender } = render(<ScenarioStep snap={snapshot({ scenes: [one, two, three], last_seq: 5 })} onChanged={() => {}} />);
    await userEvent.type(within(screen.getByRole("article", { name: "Сцена 3" })).getByLabelText(/Музыка/), "!");
    rerender(<ScenarioStep snap={snapshot({ scenes: [one], last_seq: 6 })} onChanged={() => {}} />);
    expect(screen.queryByRole("article", { name: "Сцена 2" })).toBeNull();                 // clean: gone
    const orphan = screen.getByRole("article", { name: "Сцена 3" });                        // dirty: stays
    await userEvent.click(within(orphan).getByRole("button", { name: "Убрать" }));
    expect(screen.queryByRole("article", { name: "Сцена 3" })).toBeNull();
  });

  it("r1 №2: the «от кадра сцены N» badge names the scene the frame comes from, not the previous position", () => {
    stubFetch({ ...LINT_EMPTY });
    const from = (id: string, idx: number, scene_id: string) =>
      scene({ id, idx, source_frame: { kind: "prev_last_frame", scene_id, asset_id: null, auto: false } });
    render(<ScenarioStep snap={snapshot({ scenes: [scene({ id: "s_1", idx: 0 }), from("s_2", 1, "s_1"), from("s_3", 2, "s_1"), from("s_4", 3, "s_2")] })} onChanged={() => {}} />);
    const badge = (n: number) => within(screen.getByRole("article", { name: `Сцена ${n}` })).queryByText(/от кадра сцены/)?.textContent;
    expect([badge(1), badge(2), badge(3), badge(4)]).toEqual([undefined, "от кадра сцены 1", "от кадра сцены 1", "от кадра сцены 2"]);
  });

  it("r1 Minor: while a request is in flight the delete buttons are disabled", async () => {
    let release!: () => void;
    const held = new Promise<void>((r) => (release = r));
    stubFetch({ ...LINT_EMPTY, "PUT /api/studio/projects/p_1/scenes": async () => { await held; return PUT_OK; } });
    render(<ScenarioStep snap={snapshot({ scenes: [scene({ id: "s_1", idx: 0 }), scene({ id: "s_2", idx: 1 })] })} onChanged={() => {}} />);
    await userEvent.click(screen.getByRole("button", { name: "Удалить сцену 1" }));
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "Удалить сцену 2" }).disabled).toBe(true);
    release();
    await vi.waitFor(() => expect(screen.getByRole<HTMLButtonElement>("button", { name: "Удалить сцену 2" }).disabled).toBe(false));
  });

  it("r1 Minor: deleting a card with unsaved edits asks first", async () => {
    const calls = stubFetch({ ...LINT_EMPTY, "PUT /api/studio/projects/p_1/scenes": PUT_OK });
    const confirm = vi.fn().mockReturnValueOnce(false).mockReturnValueOnce(true);
    vi.stubGlobal("confirm", confirm);
    render(<ScenarioStep snap={snapshot({ scenes: [scene()] })} onChanged={() => {}} />);
    await userEvent.type(within(screen.getByRole("article", { name: "Сцена 1" })).getByLabelText(/Музыка/), "!");
    await userEvent.click(screen.getByRole("button", { name: "Удалить сцену 1" }));
    expect([confirm.mock.calls.length, calls.some((c) => c.key.startsWith("PUT"))]).toEqual([1, false]);
    await userEvent.click(screen.getByRole("button", { name: "Удалить сцену 1" }));
    await vi.waitFor(() => expect(calls.some((c) => c.key === "PUT /api/studio/projects/p_1/scenes")).toBe(true));
    expect(confirm).toHaveBeenCalledWith("В сцене 1 есть несохранённые правки. Удалить сцену?");
  });

  it("r1 Minor: an empty seed or duration is not sent — the field says what is wrong", async () => {
    const calls = stubFetch({ ...LINT_EMPTY });
    render(<ScenarioStep snap={snapshot({ scenes: [scene()] })} onChanged={() => {}} />);
    const card = screen.getByRole("article", { name: "Сцена 1" });
    await userEvent.clear(within(card).getByLabelText(/Сид/));
    expect(within(card).getByText("Укажите число — пустое поле не сохраняется")).toBeTruthy();
    expect(within(card).getByRole<HTMLButtonElement>("button", { name: "Сохранить сцену" }).disabled).toBe(true);
    await userEvent.clear(within(card).getByLabelText(/Длительность/));
    expect(within(card).getAllByText("Укажите число — пустое поле не сохраняется")).toHaveLength(2);
    expect(calls.filter((c) => c.key.startsWith("PATCH"))).toEqual([]);
  });
});
