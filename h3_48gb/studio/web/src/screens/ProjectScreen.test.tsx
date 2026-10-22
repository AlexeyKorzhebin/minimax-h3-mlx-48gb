import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { FakeEventSource, stubFetch } from "@/test/fetchStub";
import { ProjectScreen } from "./ProjectScreen";

const project = (title: string, revision: number) => ({
  id: "p_1", revision, title, frame_format: "16:9", idea_text: "", style_block: "",
  created_at: "2026-10-21T10:00:00.000+00:00",
});
const snap = (title: string, revision: number, last_seq: number) => ({
  project: project(title, revision), scenes: [], cast: [], takes: [], tasks: [], last_seq,
});

beforeEach(() => {
  FakeEventSource.last = null;
  vi.stubGlobal("EventSource", FakeEventSource);
});

describe("ProjectScreen (S0)", () => {
  it("subscribes after the snapshot's last_seq and reloads on its own project's event", async () => {
    stubFetch({ "GET /api/studio/projects/p_1": [{ body: snap("Лес", 1, 5) }, { body: snap("Озеро", 2, 6) }] });
    render(<ProjectScreen id="p_1" />);
    await screen.findByRole("heading", { name: "Лес" });
    // the subscription is made in a passive effect, which may run just after the heading appears
    await waitFor(() => expect(FakeEventSource.last?.url).toBe("/api/studio/events?after=5"));
    FakeEventSource.last!.emit("project_updated", { project_id: "p_1" }, 6);
    await screen.findByRole("heading", { name: "Озеро" });
    await waitFor(() => expect(FakeEventSource.last?.url).toBe("/api/studio/events?after=6"));
  });

  it("ignores another project's events", async () => {
    const calls = stubFetch({ "GET /api/studio/projects/p_1": { body: snap("Лес", 1, 5) } });
    render(<ProjectScreen id="p_1" />);
    await screen.findByRole("heading", { name: "Лес" });
    await waitFor(() => expect(FakeEventSource.last).not.toBeNull());
    FakeEventSource.last!.emit("project_updated", { project_id: "p_2" }, 6);
    await new Promise((r) => setTimeout(r, 20));
    expect(calls.map((c) => c.key)).toEqual(["GET /api/studio/projects/p_1"]);
  });

  it("shows the project's format and idea", async () => {
    const withIdea = { ...snap("Лес", 1, 5), project: { ...project("Лес", 1), frame_format: "9:16", idea_text: "девушка у воды" } };
    stubFetch({ "GET /api/studio/projects/p_1": { body: withIdea } });
    render(<ProjectScreen id="p_1" />);
    await screen.findByRole("heading", { name: "Лес" });
    expect(screen.getByText("Проект · 9:16")).toBeTruthy();
    expect(screen.getByText("девушка у воды")).toBeTruthy();
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
