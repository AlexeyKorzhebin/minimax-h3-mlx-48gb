import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { taskText } from "@/components/TaskLine";
import { MobileActionProvider } from "@/mobileAction";
import { stubFetch } from "@/test/fetchStub";
import { scene, snapshot } from "@/test/snapshot";
import { imagePlan, task } from "@/test/task";
import { FrameStep } from "./FrameStep";

const fromFrame = scene({ id: "s_1", mode: "from_frame", edit_version: 4 });

describe("FrameStep", () => {
  it("plans, shows time and the model switch, confirms with a fresh Idempotency-Key", async () => {
    const calls = stubFetch({
      "POST /api/studio/projects/p_1/plan": { body: imagePlan() },
      "POST /api/studio/projects/p_1/image": { status: 201, body: { task_id: "k_1", status: "queued" } },
    });
    const onChanged = vi.fn();
    render(<FrameStep snap={snapshot({ scenes: [fromFrame] })} onChanged={onChanged} newKey={() => "key-1"} />);
    await userEvent.click(screen.getByRole("button", { name: "Сделать кадр" }));
    const region = await screen.findByRole("region", { name: "План кадра" });
    expect(region.querySelector("[aria-label='План']")!.textContent).toBe(
      "Время ≈ 23 с · смена модели h3-fl2va → krea2-t2i: +17 с");
    expect(screen.getByText("1344×768 · Krea 2")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Подтвердить" }));
    await vi.waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
    expect(calls.map((c) => [c.key, c.body])).toEqual([
      ["POST /api/studio/projects/p_1/plan", { kind: "image", scene_id: "s_1" }],
      ["POST /api/studio/projects/p_1/image", { plan_id: "pl_1" }],
    ]);
    expect(calls[1].headers).toEqual({ "Content-Type": "application/json", "Idempotency-Key": "key-1" });
  });

  it("shows why the task waits and frees the card from Qwen", async () => {
    const calls = stubFetch({ "POST /api/studio/gpu/qwen/unload": { body: { ok: true } } });
    const onChanged = vi.fn();
    const waiting = task({ status: "waiting_gpu", wait_reason: "ждём GPU: Qwen держит карту" });
    render(<FrameStep snap={snapshot({ scenes: [fromFrame], tasks: [waiting] })} onChanged={onChanged} />);
    expect(screen.getByRole("status").textContent).toBe("ждём GPU: Qwen держит карту");
    await userEvent.click(screen.getByRole("button", { name: "Освободить" }));
    await vi.waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
    expect(calls.map((c) => c.key)).toEqual(["POST /api/studio/gpu/qwen/unload"]);
  });

  it("cancels a running task", async () => {
    const calls = stubFetch({ "POST /api/studio/tasks/k_1/cancel": { body: task({ status: "cancelled" }) } });
    const running = task({ status: "running", progress_step: 3, progress_total: 8 });
    render(<FrameStep snap={snapshot({ scenes: [fromFrame], tasks: [running] })} onChanged={() => {}} />);
    expect(screen.getByRole("status").textContent).toBe("Шаг 3 из 8");
    await userEvent.click(screen.getByRole("button", { name: "Отменить" }));
    await vi.waitFor(() => expect(calls.length).toBe(1));
    expect([calls[0].key, calls[0].body]).toEqual(["POST /api/studio/tasks/k_1/cancel", { reason: "владелец" }]);
  });

  it("takes the finished frame into scene 1 with the scene's edit_version", async () => {
    const calls = stubFetch({ "PATCH /api/studio/scenes/s_1": { body: { scene: fromFrame, cascade: [], lint: [] } } });
    const onChanged = vi.fn();
    const done = task({ status: "succeeded", result_asset_id: "a_9", result_url: "/api/studio/assets/a_9/file" });
    render(<FrameStep snap={snapshot({ scenes: [fromFrame], tasks: [done] })} onChanged={onChanged} />);
    expect((screen.getByAltText("Новый кадр") as HTMLImageElement).getAttribute("src")).toBe("/api/studio/assets/a_9/file");
    await userEvent.click(screen.getByRole("button", { name: "Взять в сцену 1" }));
    await vi.waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
    expect(calls.map((c) => [c.key, c.body])).toEqual([
      ["PATCH /api/studio/scenes/s_1", { expected_edit_version: 4, source_frame: { kind: "asset", asset_id: "a_9" } }],
    ]);
  });

  it("an attached frame is shown and not offered again", () => {
    stubFetch({});
    const attached = scene({ id: "s_1", mode: "from_frame", source_frame: { kind: "asset", scene_id: null, asset_id: "a_9", auto: false } });
    const done = task({ status: "succeeded", result_asset_id: "a_9", result_url: "/api/studio/assets/a_9/file" });
    render(<FrameStep snap={snapshot({ scenes: [attached], tasks: [done] })} onChanged={() => {}} />);
    expect((screen.getByAltText("Кадр сцены 1") as HTMLImageElement).getAttribute("src")).toBe("/api/studio/assets/a_9/file");
    expect(screen.queryByRole("button", { name: "Взять в сцену 1" })).toBeNull();
    expect(screen.getByRole("button", { name: "Сделать другой кадр" })).toBeTruthy();
  });

  it("a refs scene needs no frame", () => {
    stubFetch({});
    render(<FrameStep snap={snapshot({ scenes: [scene({ mode: "refs" })] })} onChanged={() => {}} />);
    expect(screen.getByText("Сцена 1 снимается по фото героя (режим «Референсы») — кадр не нужен.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Сделать кадр" })).toBeNull();
  });

  it("a blocked plan shows its reasons and cannot be confirmed", async () => {
    const blocked = imagePlan();
    blocked.items[0] = { ...blocked.items[0], status: "blocked", reasons: ["в ComfyUI нет шаблона krea2-t2i"] };
    const calls = stubFetch({ "POST /api/studio/projects/p_1/plan": { body: blocked } });
    render(<FrameStep snap={snapshot({ scenes: [fromFrame] })} onChanged={() => {}} />);
    await userEvent.click(screen.getByRole("button", { name: "Сделать кадр" }));
    expect((await screen.findByRole("alert")).textContent).toBe("в ComfyUI нет шаблона krea2-t2i");
    expect((screen.getByRole("button", { name: "Подтвердить" }) as HTMLButtonElement).disabled).toBe(true);
    expect(calls.length).toBe(1);
  });

  it("while a task is active a second plan is not offered", () => {
    stubFetch({});
    const running = task({ status: "running", progress_step: 1, progress_total: 8 });
    render(<FrameStep snap={snapshot({ scenes: [fromFrame], tasks: [running] })} onChanged={() => {}} />);
    expect((screen.getByRole("button", { name: "Сделать кадр" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("one Idempotency-Key per plan: a retry after a network failure reuses it and sees the task", async () => {
    const newKey = vi.fn().mockReturnValueOnce("key-1").mockReturnValueOnce("key-2");
    const calls = stubFetch({
      "POST /api/studio/projects/p_1/plan": { body: imagePlan() },
      "POST /api/studio/projects/p_1/image": [
        () => { throw new TypeError("network down"); },
        { status: 200, body: { task_id: "k_1", status: "queued" } },
      ],
    });
    const onChanged = vi.fn();
    render(<FrameStep snap={snapshot({ scenes: [fromFrame] })} onChanged={onChanged} newKey={newKey} />);
    await userEvent.click(screen.getByRole("button", { name: "Сделать кадр" }));
    await userEvent.click(await screen.findByRole("button", { name: "Подтвердить" }));
    expect((await screen.findByRole("alert")).textContent).toBe("Сервер не ответил");
    expect(onChanged).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Подтвердить" }));
    await vi.waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
    expect(calls.filter((c) => c.key === "POST /api/studio/projects/p_1/image").map((c) => [c.body, c.headers["Idempotency-Key"]]))
      .toEqual([[{ plan_id: "pl_1" }, "key-1"], [{ plan_id: "pl_1" }, "key-1"]]);
    expect(newKey).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["plan_used", "Этот план уже подтверждён"],
    ["plan_expired", "План устарел (живёт 10 минут) — постройте заново"],
  ])("a spent plan (%s) is dropped, the snapshot is reloaded, the words are shown", async (code, message) => {
    stubFetch({
      "POST /api/studio/projects/p_1/plan": { body: imagePlan() },
      "POST /api/studio/projects/p_1/image": { status: 409, body: { error: { code, message, detail: {} } } },
    });
    const onChanged = vi.fn();
    render(<FrameStep snap={snapshot({ scenes: [fromFrame] })} onChanged={onChanged} />);
    await userEvent.click(screen.getByRole("button", { name: "Сделать кадр" }));
    await userEvent.click(await screen.findByRole("button", { name: "Подтвердить" }));
    expect((await screen.findByRole("alert")).textContent).toBe(message);
    expect(screen.queryByRole("region", { name: "План кадра" })).toBeNull();
    expect(onChanged).toHaveBeenCalledTimes(1);
  });

  it("a 409 on «Взять в сцену 1» reloads; the owner's retry carries the new edit_version", async () => {
    const calls = stubFetch({
      "PATCH /api/studio/scenes/s_1": [
        { status: 409, body: { error: { code: "revision_conflict", message: "Сцену изменили в другом окне", detail: {} } } },
        { body: { scene: fromFrame, cascade: [], lint: [] } },
      ],
    });
    const onChanged = vi.fn();
    const done = task({ status: "succeeded", result_asset_id: "a_9", result_url: "/api/studio/assets/a_9/file" });
    const view = render(<FrameStep snap={snapshot({ scenes: [fromFrame], tasks: [done] })} onChanged={onChanged} />);
    await userEvent.click(screen.getByRole("button", { name: "Взять в сцену 1" }));
    expect((await screen.findByRole("alert")).textContent).toBe("Сцену изменили в другом окне");
    expect(onChanged).toHaveBeenCalledTimes(1);
    expect(calls.length).toBe(1);                       // no automatic retry
    view.rerender(<FrameStep snap={snapshot({ scenes: [{ ...fromFrame, edit_version: 5 }], tasks: [done] })} onChanged={onChanged} />);
    await userEvent.click(screen.getByRole("button", { name: "Взять в сцену 1" }));
    await vi.waitFor(() => expect(calls.length).toBe(2));
    expect(calls.map((c) => [c.key, c.body])).toEqual([
      ["PATCH /api/studio/scenes/s_1", { expected_edit_version: 4, source_frame: { kind: "asset", asset_id: "a_9" } }],
      ["PATCH /api/studio/scenes/s_1", { expected_edit_version: 5, source_frame: { kind: "asset", asset_id: "a_9" } }],
    ]);
  });

  it("a plan is dropped when scene 1 changes after it was built", async () => {
    stubFetch({ "POST /api/studio/projects/p_1/plan": { body: imagePlan() } });
    const view = render(<FrameStep snap={snapshot({ scenes: [fromFrame] })} onChanged={() => {}} />);
    await userEvent.click(screen.getByRole("button", { name: "Сделать кадр" }));
    await screen.findByRole("region", { name: "План кадра" });
    view.rerender(<FrameStep snap={snapshot({ scenes: [{ ...fromFrame, revision: 2 }] })} onChanged={() => {}} />);
    expect(screen.queryByRole("region", { name: "План кадра" })).toBeNull();
    expect(screen.getByRole("button", { name: "Сделать кадр" })).toBeTruthy();
  });

  it("a frame already taken leaves no task line behind", () => {
    stubFetch({});
    const attached = scene({ id: "s_1", mode: "from_frame", source_frame: { kind: "asset", scene_id: null, asset_id: "a_9", auto: false } });
    const done = task({ status: "succeeded", result_asset_id: "a_9", result_url: "/api/studio/assets/a_9/file" });
    render(<FrameStep snap={snapshot({ scenes: [attached], tasks: [done] })} onChanged={() => {}} />);
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("the latest image task speaks, not the first", () => {
    stubFetch({});
    const first = task({ id: "k_1", status: "succeeded", result_asset_id: "a_1", result_url: "/api/studio/assets/a_1/file" });
    const second = task({ id: "k_2", status: "running", progress_step: 2, progress_total: 8 });
    render(<FrameStep snap={snapshot({ scenes: [fromFrame], tasks: [first, second] })} onChanged={() => {}} />);
    expect(screen.getByRole("status").textContent).toBe("Шаг 2 из 8");
    expect(screen.queryByAltText("Новый кадр")).toBeNull();
  });

  it("a requested cancel says so and offers no second «Отменить»", () => {
    stubFetch({});
    const asked = task({ status: "running", progress_step: 3, progress_total: 8, cancel_requested: true });
    render(<FrameStep snap={snapshot({ scenes: [fromFrame], tasks: [asked] })} onChanged={() => {}} />);
    expect(screen.getByRole("status").textContent).toBe("Отмена запрошена");
    expect(screen.queryByRole("button", { name: "Отменить" })).toBeNull();
  });

  it("after a failed task a new frame can be planned at once", () => {
    stubFetch({});
    const failed = task({ status: "failed", error: "ComfyUI: OOM" });
    render(<FrameStep snap={snapshot({ scenes: [fromFrame], tasks: [failed] })} onChanged={() => {}} />);
    expect((screen.getByRole("button", { name: "Сделать кадр" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("a finished frame: «Взять в сцену 1» is the main button, «Сделать другой кадр» the secondary", () => {
    stubFetch({});
    const done = task({ status: "succeeded", result_asset_id: "a_9", result_url: "/api/studio/assets/a_9/file" });
    render(<FrameStep snap={snapshot({ scenes: [fromFrame], tasks: [done] })} onChanged={() => {}} />);
    expect(screen.getByRole("button", { name: "Взять в сцену 1" }).getAttribute("data-variant")).toBe("default");
    expect(screen.getByRole("button", { name: "Сделать другой кадр" }).getAttribute("data-variant")).toBe("outline");
  });

  it("«Подтвердить» is not pressed twice while the request is in flight", async () => {
    let release!: () => void;
    const held = new Promise<void>((r) => { release = r; });
    const calls = stubFetch({
      "POST /api/studio/projects/p_1/plan": { body: imagePlan() },
      "POST /api/studio/projects/p_1/image": async () => { await held; return { status: 201, body: { task_id: "k_1", status: "queued" } }; },
    });
    render(<FrameStep snap={snapshot({ scenes: [fromFrame] })} onChanged={() => {}} />);
    await userEvent.click(screen.getByRole("button", { name: "Сделать кадр" }));
    const confirmBtn = await screen.findByRole("button", { name: "Подтвердить" });
    await userEvent.click(confirmBtn);
    await vi.waitFor(() => expect((confirmBtn as HTMLButtonElement).disabled).toBe(true));
    await userEvent.click(confirmBtn);
    release();
    await vi.waitFor(() => expect(screen.queryByRole("region", { name: "План кадра" })).toBeNull());
    expect(calls.filter((c) => c.key === "POST /api/studio/projects/p_1/image").length).toBe(1);
  });

  it("«Отменить» is not pressed twice while the request is in flight", async () => {
    let release!: () => void;
    const held = new Promise<void>((r) => { release = r; });
    const calls = stubFetch({ "POST /api/studio/tasks/k_1/cancel": async () => { await held; return { body: task({ status: "cancelled" }) }; } });
    const running = task({ status: "running", progress_step: 3, progress_total: 8 });
    render(<FrameStep snap={snapshot({ scenes: [fromFrame], tasks: [running] })} onChanged={() => {}} />);
    const btn = screen.getByRole("button", { name: "Отменить" });
    await userEvent.click(btn);
    await vi.waitFor(() => expect((btn as HTMLButtonElement).disabled).toBe(true));
    await userEvent.click(btn);
    release();
    await vi.waitFor(() => expect((btn as HTMLButtonElement).disabled).toBe(false));
    expect(calls.length).toBe(1);
  });

  it("phone: the bar cannot confirm a blocked plan", async () => {
    const query = { matches: true, addEventListener: () => {}, removeEventListener: () => {} };
    vi.stubGlobal("matchMedia", () => query);
    const blocked = imagePlan();
    blocked.items[0] = { ...blocked.items[0], status: "blocked", reasons: ["в ComfyUI нет шаблона krea2-t2i"] };
    stubFetch({ "POST /api/studio/projects/p_1/plan": { body: blocked } });
    render(<MobileActionProvider><FrameStep snap={snapshot({ scenes: [fromFrame] })} onChanged={() => {}} /></MobileActionProvider>);
    await userEvent.click(screen.getByRole("button", { name: "Сделать кадр" }));
    await screen.findByRole("region", { name: "План кадра" });
    const bar = await screen.findByTestId("mobile-action-bar");
    expect((within(bar).getByRole("button", { name: "Подтвердить" }) as HTMLButtonElement).disabled).toBe(true);
    vi.unstubAllGlobals();
  });

  it("names every task state in words", () => {
    expect([
      taskText(task({ status: "queued" })), taskText(task({ status: "blocked" })),
      taskText(task({ status: "waiting_ready", wait_reason: "ждём ComfyUI: в ComfyUI нет узлов: X" })),
      taskText(task({ status: "running" })), taskText(task({ status: "failed", error: "ComfyUI: OOM" })),
      taskText(task({ status: "submit_unknown", wait_reason: null })), taskText(task({ status: "cancelled" })),
    ]).toEqual(["В очереди", "Ждёт кадр предыдущей сцены", "ждём ComfyUI: в ComfyUI нет узлов: X", "Идёт",
                "Ошибка: ComfyUI: OOM", "Не знаю, принял ли ComfyUI задачу", "Отменено"]);
  });
});
