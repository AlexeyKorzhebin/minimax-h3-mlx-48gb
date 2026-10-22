import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { stubFetch } from "@/test/fetchStub";
import { plural, ProjectsScreen } from "./ProjectsScreen";

const ITEM = {
  id: "p_1", revision: 1, title: "Лес", frame_format: "16:9", idea_text: "", style_block: "",
  created_at: "2026-10-21T10:00:00.000+00:00", scenes_total: 2, scenes_done: 0,
};

describe("plural", () => {
  it("agrees сцена with the number", () => {
    expect([1, 2, 4, 5, 11, 12, 14, 21, 22, 25].map((n) => `${n} ${plural(n)}`)).toEqual([
      "1 сцена", "2 сцены", "4 сцены", "5 сцен", "11 сцен", "12 сцен", "14 сцен", "21 сцена", "22 сцены", "25 сцен",
    ]);
  });
});

describe("ProjectsScreen", () => {
  it("lists projects from O1 as cards linking to the project", async () => {
    stubFetch({ "GET /api/studio/projects": { body: [ITEM] } });
    render(<ProjectsScreen />);
    const link = await screen.findByRole("link", { name: /Лес/ });
    expect(link.getAttribute("href")).toBe("#/p/p_1");
    expect(link.querySelector("[data-testid=card-meta]")!.textContent).toBe("2 сцены · 16:9");
    expect(screen.getByText("Проекты · 1")).toBeTruthy();
  });

  it("creates a project with the chosen format and opens it", async () => {
    const calls = stubFetch({
      "GET /api/studio/projects": { body: [] },
      "POST /api/studio/projects": { status: 201, body: { ...ITEM, id: "p_9", frame_format: "9:16" } },
    });
    render(<ProjectsScreen />);
    await userEvent.click(await screen.findByRole("button", { name: "Новый проект" }));
    await userEvent.type(screen.getByLabelText("Название"), "Озеро");
    await userEvent.click(screen.getByRole("button", { name: "9:16" }));
    await userEvent.type(screen.getByLabelText(/Идея/), "девушка у воды");
    await userEvent.click(screen.getByRole("button", { name: "Создать проект" }));
    await screen.findByRole("form", { name: "Новый проект" });
    expect(calls[1]).toEqual({
      key: "POST /api/studio/projects",
      body: { title: "Озеро", frame_format: "9:16", idea_text: "девушка у воды" },
      headers: { "Content-Type": "application/json" },
    });
    await new Promise((r) => setTimeout(r, 0));
    expect(location.hash).toBe("#/p/p_9");
  });

  it("shows the server's message when creation is refused", async () => {
    stubFetch({
      "GET /api/studio/projects": { body: [] },
      "POST /api/studio/projects": {
        status: 400,
        body: { error: { code: "args_invalid", message: "Неверные данные запроса", detail: {} } },
      },
    });
    render(<ProjectsScreen />);
    await userEvent.click(await screen.findByRole("button", { name: "Новый проект" }));
    await userEvent.type(screen.getByLabelText("Название"), "X");
    await userEvent.click(screen.getByRole("button", { name: "Создать проект" }));
    expect((await screen.findByRole("alert")).textContent).toBe("Неверные данные запроса");
  });

  it("shows each card's own scene count and format", async () => {
    stubFetch({ "GET /api/studio/projects": { body: [{ ...ITEM, id: "p_2", title: "Озеро", frame_format: "9:16", scenes_total: 5 }] } });
    render(<ProjectsScreen />);
    const link = await screen.findByRole("link", { name: /Озеро/ });
    expect(link.querySelector("[data-testid=card-meta]")!.textContent).toBe("5 сцен · 9:16");
  });

  it("sends the title trimmed, and keeps «Создать проект» disabled while the title is blank", async () => {
    const calls = stubFetch({
      "GET /api/studio/projects": { body: [] },
      "POST /api/studio/projects": { status: 201, body: { ...ITEM, id: "p_3" } },
    });
    render(<ProjectsScreen />);
    await userEvent.click(await screen.findByRole("button", { name: "Новый проект" }));
    const create = screen.getByRole("button", { name: "Создать проект" }) as HTMLButtonElement;
    expect(create.disabled).toBe(true);
    await userEvent.type(screen.getByLabelText("Название"), "   ");
    expect(create.disabled).toBe(true);
    await userEvent.type(screen.getByLabelText("Название"), " Озеро ");
    expect(create.disabled).toBe(false);
    await userEvent.click(create);
    await screen.findByRole("form", { name: "Новый проект" });
    expect(calls[1]).toEqual({
      key: "POST /api/studio/projects",
      body: { title: "Озеро", frame_format: "16:9", idea_text: "" },
      headers: { "Content-Type": "application/json" },
    });
  });

  it("a refusal can be fixed: the error goes away on the next successful send, and the button is usable again", async () => {
    const refusal = {
      status: 400,
      body: { error: { code: "args_invalid", message: "Неверные данные запроса", detail: {} } },
    };
    const calls = stubFetch({
      "GET /api/studio/projects": { body: [] },
      "POST /api/studio/projects": [refusal, { status: 201, body: { ...ITEM, id: "p_4" } }],
    });
    render(<ProjectsScreen />);
    await userEvent.click(await screen.findByRole("button", { name: "Новый проект" }));
    await userEvent.type(screen.getByLabelText("Название"), "X");
    const create = screen.getByRole("button", { name: "Создать проект" }) as HTMLButtonElement;
    await userEvent.click(create);
    expect((await screen.findByRole("alert")).textContent).toBe("Неверные данные запроса");
    await waitFor(() => expect(create.disabled).toBe(false));   // refused: not stuck in «busy»
    await userEvent.click(create);
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
    expect(calls.filter((c) => c.key === "POST /api/studio/projects")).toHaveLength(2);
  });

  it("a double click sends one request, and the button is disabled while it is in flight", async () => {
    let release!: () => void;
    const held = new Promise<void>((r) => (release = r));
    const calls = stubFetch({
      "GET /api/studio/projects": { body: [] },
      "POST /api/studio/projects": async () => {
        await held;
        return { status: 201, body: { ...ITEM, id: "p_5" } };
      },
    });
    render(<ProjectsScreen />);
    await userEvent.click(await screen.findByRole("button", { name: "Новый проект" }));
    await userEvent.type(screen.getByLabelText("Название"), "X");
    const create = screen.getByRole("button", { name: "Создать проект" }) as HTMLButtonElement;
    await userEvent.click(create);
    await waitFor(() => expect(create.disabled).toBe(true));
    await userEvent.click(create);
    release();
    await waitFor(() => expect(create.disabled).toBe(false));
    expect(calls.filter((c) => c.key === "POST /api/studio/projects")).toHaveLength(1);
  });

  it("shows the server's own message when the list cannot be loaded", async () => {
    stubFetch({
      "GET /api/studio/projects": {
        status: 500,
        body: { error: { code: "internal", message: "База недоступна", detail: {} } },
      },
    });
    render(<ProjectsScreen />);
    expect((await screen.findByRole("alert")).textContent).toBe("База недоступна");
  });

  it("says «Сервер не ответил» when the request fails outright", async () => {
    vi.stubGlobal("fetch", async () => { throw new TypeError("Failed to fetch"); });
    render(<ProjectsScreen />);
    expect((await screen.findByRole("alert")).textContent).toBe("Сервер не ответил");
  });

  it("a project without scenes says so instead of «0 сцен»", async () => {
    stubFetch({ "GET /api/studio/projects": { body: [{ ...ITEM, scenes_total: 0 }] } });
    render(<ProjectsScreen />);
    const link = await screen.findByRole("link", { name: /Лес/ });
    expect(link.querySelector("[data-testid=card-meta]")!.textContent).toBe("сцен пока нет · 16:9");
  });

  it("counts the projects, numbers the cards and shows nothing as empty when there are some", async () => {
    stubFetch({
      "GET /api/studio/projects": {
        body: [ITEM, { ...ITEM, id: "p_2", title: "Озеро" }, { ...ITEM, id: "p_3", title: "Горы" }],
      },
    });
    render(<ProjectsScreen />);
    await screen.findByRole("link", { name: /Горы/ });
    expect(screen.getByText("Проекты · 3")).toBeTruthy();
    expect(screen.getAllByText(/^№ \d\d$/).map((e) => e.textContent)).toEqual(["№ 01", "№ 02", "№ 03"]);
    expect(screen.queryByText(/Проектов пока нет/)).toBeNull();
  });

  it("an empty list says so, with the counter at 0", async () => {
    stubFetch({ "GET /api/studio/projects": { body: [] } });
    render(<ProjectsScreen />);
    expect((await screen.findByText(/Проектов пока нет/)).textContent).toBe("Проектов пока нет — начните с «Новый проект».");
    expect(screen.getByText("Проекты · 0")).toBeTruthy();
  });
});
