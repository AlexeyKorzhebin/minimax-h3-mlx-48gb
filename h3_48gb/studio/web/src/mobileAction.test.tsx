import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { FakeEventSource, stubFetch } from "@/test/fetchStub";
import { scene, snapshot } from "@/test/snapshot";
import { ProjectScreen } from "@/screens/ProjectScreen";

/** The viewport: phone or not; the returned function turns the page (the AB layout listens). */
function phone(matches: boolean) {
  const listeners: (() => void)[] = [];
  const query = { matches, addEventListener: (_: string, fn: () => void) => { listeners.push(fn); }, removeEventListener: () => {} };
  vi.stubGlobal("matchMedia", (q: string) => {
    if (q !== "(max-width: 767px)") throw new Error(`unexpected media query: ${q}`);
    return query;
  });
  return (now: boolean) => act(() => { query.matches = now; listeners.forEach((fn) => fn()); });
}

beforeEach(() => {
  vi.stubGlobal("EventSource", FakeEventSource);
  stubFetch({
    "GET /api/studio/projects/p_1": { body: snapshot({ scenes: [scene()] }) },
    "POST /api/studio/projects/p_1/lint": { body: { findings: {} } },
  });
});

const barLabel = async (label: string) =>
  vi.waitFor(() => expect(within(screen.getByTestId("mobile-action-bar")).getByRole("button").textContent).toBe(label));

describe("phone bar: the main action of the step in hand", () => {
  it("follows the owner from step to step by priority", async () => {
    phone(true);
    render(<ProjectScreen id="p_1" />);
    await screen.findByRole("article", { name: "Сцена 1" });
    await barLabel("Предложить сцены");
    await userEvent.type(screen.getByLabelText(/Тег/), "anna");
    await barLabel("Сохранить героя");
    await userEvent.type(within(screen.getByRole("article", { name: "Сцена 1" })).getByLabelText(/Музыка/), "!");
    await barLabel("Сохранить сцену 1");
    expect(screen.queryByRole("button", { name: "Сохранить сцену" })).toBeNull();   // no inline twin
  });

  it("on a wide screen there is no bar, every step has its own button", async () => {
    phone(false);
    render(<ProjectScreen id="p_1" />);
    await screen.findByRole("article", { name: "Сцена 1" });
    expect(screen.queryByTestId("mobile-action-bar")).toBeNull();
    expect(screen.getByRole("button", { name: "Предложить сцены" })).toBeTruthy();
  });

  it("between two edited scenes the bar saves the one that got input last (1 → 2 → 1)", async () => {
    phone(true);
    stubFetch({
      "GET /api/studio/projects/p_1": { body: snapshot({ scenes: [scene({ id: "s_1", idx: 0 }), scene({ id: "s_2", idx: 1 })] }) },
      "POST /api/studio/projects/p_1/lint": { body: { findings: {} } },
    });
    render(<ProjectScreen id="p_1" />);
    const music = async (n: number) => within(await screen.findByRole("article", { name: `Сцена ${n}` })).getByLabelText(/Музыка/);
    await userEvent.type(await music(1), "a");
    await userEvent.type(await music(2), "b");
    await barLabel("Сохранить сцену 2");
    await userEvent.type(await music(1), "c");
    await barLabel("Сохранить сцену 1");
  });

  it("r5: between two cards in conflict the bar's «Сохранить моё» belongs to the one that got input last (1 → 2 → 1)", async () => {
    const turn = phone(false);                                          // wide: both cards reach a conflict by their own buttons
    const conflict = (id: string, idx: number) => ({ status: 409, body: { error: { code: "revision_conflict", message: "x",
      detail: { current: scene({ id, idx, edit_version: 1, music_text: "other" }) } } } });
    const calls = stubFetch({
      "GET /api/studio/projects/p_1": { body: snapshot({ scenes: [scene({ id: "s_1", idx: 0 }), scene({ id: "s_2", idx: 1 })] }) },
      "POST /api/studio/projects/p_1/lint": { body: { findings: {} } },
      "PATCH /api/studio/scenes/s_1": conflict("s_1", 0),
      "PATCH /api/studio/scenes/s_2": conflict("s_2", 1),
    });
    render(<ProjectScreen id="p_1" />);
    const card = async (n: number) => screen.findByRole("article", { name: `Сцена ${n}` });
    const music = async (n: number) => within(await card(n)).getByLabelText(/Музыка/);
    for (const n of [1, 2]) {
      await userEvent.type(await music(n), "x");
      await userEvent.click(within(await card(n)).getByRole("button", { name: "Сохранить сцену" }));
      await within(await card(n)).findByRole("group", { name: "Конфликт правок" });
    }
    turn(true);                                                         // phone: one bar for both conflicts
    const bar = () => within(screen.getByTestId("mobile-action-bar")).getByRole("button", { name: "Сохранить моё" });
    const patched = () => calls.filter((c) => c.key === "PATCH /api/studio/scenes/s_1" || c.key === "PATCH /api/studio/scenes/s_2").map((c) => c.key);
    for (const [n, expected] of [[1, "s_1"], [2, "s_2"], [1, "s_1"]] as const) {
      await userEvent.type(await music(n), "y");
      await userEvent.click(bar());
      await vi.waitFor(() => expect(patched().at(-1)).toBe(`PATCH /api/studio/scenes/${expected}`));
    }
    expect(patched()).toHaveLength(5);
  });

  it("r1 №5: after the scenes are reordered the bar still saves the card edited last, by scene id (S1, then S2; order becomes S2, S1)", async () => {
    phone(true);
    let scenes = [scene({ id: "s_1", idx: 0 }), scene({ id: "s_2", idx: 1 })];
    const calls = stubFetch({
      "GET /api/studio/projects/p_1": () => ({ body: snapshot({ scenes }) }),
      "POST /api/studio/projects/p_1/lint": { body: { findings: {} } },
      "PATCH /api/studio/scenes/s_1": { body: { scene: scene({ id: "s_1", edit_version: 1 }), cascade: [], lint: [] } },
      "PATCH /api/studio/scenes/s_2": { body: { scene: scene({ id: "s_2", edit_version: 1 }), cascade: [], lint: [] } },
    });
    render(<ProjectScreen id="p_1" />);
    const music = async (name: string) => within(await screen.findByRole("article", { name })).getByLabelText(/Музыка/);
    await userEvent.type(await music("Сцена 1"), "a");      // s_1
    await userEvent.type(await music("Сцена 2"), "b");      // s_2, edited last
    await barLabel("Сохранить сцену 2");
    scenes = [{ ...scenes[1], idx: 0 }, { ...scenes[0], idx: 1 }];
    FakeEventSource.last!.emit("scenes_replaced", { project_id: "p_1" }, 6);
    await barLabel("Сохранить сцену 1");                     // s_2 is now the first scene — and still the card in hand
    await userEvent.click(within(screen.getByTestId("mobile-action-bar")).getByRole("button"));
    await vi.waitFor(() => expect(calls.some((c) => c.key.startsWith("PATCH"))).toBe(true));
    expect(calls.filter((c) => c.key.startsWith("PATCH")).map((c) => c.key)).toEqual(["PATCH /api/studio/scenes/s_2"]);
  });
});
