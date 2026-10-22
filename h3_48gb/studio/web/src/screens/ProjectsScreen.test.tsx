import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
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
    });
  });
});
