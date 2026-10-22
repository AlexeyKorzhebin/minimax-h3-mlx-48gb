import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FakeEventSource, stubFetch } from "@/test/fetchStub";
import { App } from "./main";

const PROJECT = (id: string, title: string) => ({
  id, revision: 1, title, frame_format: "16:9", idea_text: "", style_block: "",
  created_at: "2026-10-21T10:00:00.000+00:00",
});
const snap = (id: string, title: string) => ({
  project: PROJECT(id, title), scenes: [], cast: [], takes: [], tasks: [], last_seq: 1,
});

beforeEach(() => {
  location.hash = "";
  vi.stubGlobal("EventSource", FakeEventSource);
});
afterEach(() => {
  location.hash = "";
});

describe("App", () => {
  it("opens the project's screen after «Создать проект» (the URL and the screen change together)", async () => {
    stubFetch({
      "GET /api/studio/projects": { body: [] },
      "POST /api/studio/projects": { status: 201, body: { ...PROJECT("p_9", "Озеро"), scenes_total: 0, scenes_done: 0 } },
      "GET /api/studio/projects/p_9": { body: snap("p_9", "Озеро") },
    });
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "Новый проект" }));
    await userEvent.type(screen.getByLabelText("Название"), "Озеро");
    await userEvent.click(screen.getByRole("button", { name: "Создать проект" }));
    expect((await screen.findByRole("heading", { level: 1, name: "Озеро" })).textContent).toBe("Озеро");
    expect(location.hash).toBe("#/p/p_9");
    expect(screen.queryByRole("button", { name: "Новый проект" })).toBeNull();
  });

  it("shows the list at #/ and the project at #/p/<id>, and back", async () => {
    location.hash = "#/p/p_1";
    stubFetch({
      "GET /api/studio/projects/p_1": { body: snap("p_1", "Лес") },
      "GET /api/studio/projects": { body: [] },
    });
    render(<App />);
    await screen.findByRole("heading", { level: 1, name: "Лес" });
    expect(screen.queryByRole("button", { name: "Новый проект" })).toBeNull();
    location.hash = "#/";
    await screen.findByRole("button", { name: "Новый проект" });
    expect(screen.queryByRole("heading", { level: 1, name: "Лес" })).toBeNull();
  });

  it("does not show the previous project's snapshot while another project loads", async () => {
    location.hash = "#/p/p_1";
    let release!: () => void;
    const held = new Promise<void>((r) => (release = r));
    stubFetch({
      "GET /api/studio/projects/p_1": { body: snap("p_1", "Лес") },
      "GET /api/studio/projects/p_2": async () => {
        await held;
        return { body: snap("p_2", "Озеро") };
      },
    });
    render(<App />);
    await screen.findByRole("heading", { level: 1, name: "Лес" });
    location.hash = "#/p/p_2";
    await screen.findByText("Загрузка…");
    expect(screen.queryByRole("heading", { level: 1, name: "Лес" })).toBeNull();
    release();
    await waitFor(() => expect(screen.getByRole("heading", { level: 1, name: "Озеро" })).toBeTruthy());
  });
});
