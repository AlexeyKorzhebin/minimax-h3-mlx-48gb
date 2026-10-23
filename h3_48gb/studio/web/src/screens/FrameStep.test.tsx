import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { taskText } from "@/components/TaskLine";
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

  it("a server refusal on confirm is shown in words, no task line appears", async () => {
    stubFetch({
      "POST /api/studio/projects/p_1/plan": { body: imagePlan() },
      "POST /api/studio/projects/p_1/image": { status: 409, body: { error: { code: "plan_stale", message: "Сцена изменилась — постройте план заново", detail: {} } } },
    });
    const onChanged = vi.fn();
    render(<FrameStep snap={snapshot({ scenes: [fromFrame] })} onChanged={onChanged} />);
    await userEvent.click(screen.getByRole("button", { name: "Сделать кадр" }));
    await userEvent.click(await screen.findByRole("button", { name: "Подтвердить" }));
    expect((await screen.findByRole("alert")).textContent).toBe("Сцена изменилась — постройте план заново");
    expect(onChanged).not.toHaveBeenCalled();
  });

  it("a release refused while the render runs (409 task_running) is shown", async () => {
    stubFetch({ "POST /api/studio/gpu/qwen/unload": { status: 409, body: { error: { code: "task_running", message: "Идёт рендер — карту не освободить", detail: {} } } } });
    const waiting = task({ status: "waiting_gpu", wait_reason: "ждём GPU: Qwen держит карту" });
    render(<FrameStep snap={snapshot({ scenes: [fromFrame], tasks: [waiting] })} onChanged={() => {}} />);
    await userEvent.click(screen.getByRole("button", { name: "Освободить" }));
    expect((await screen.findByRole("alert")).textContent).toBe("Идёт рендер — карту не освободить");
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
