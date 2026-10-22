import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AppHeader } from "./AppHeader";

afterEach(() => {
  localStorage.clear();
  document.documentElement.dataset.theme = "light";
});

/** A controllable OS: `flip` changes prefers-color-scheme and fires «change» like a real one. */
function stubSystem(dark: boolean) {
  const listeners: (() => void)[] = [];
  const query = {
    matches: dark,
    addEventListener: (kind: string, fn: () => void) => { if (kind === "change") listeners.push(fn); },
    removeEventListener: (kind: string, fn: () => void) => {
      if (kind === "change") listeners.splice(listeners.indexOf(fn), 1);
    },
  };
  vi.stubGlobal("matchMedia", (q: string) => {
    if (q !== "(prefers-color-scheme: dark)") throw new Error(`unexpected media query: ${q}`);
    return query;
  });
  return { flip(next: boolean) { query.matches = next; listeners.forEach((fn) => fn()); }, listeners };
}

describe("AppHeader theme button", () => {
  it("cycles light → dark → system → light, applying and remembering each choice", async () => {
    stubSystem(true);
    render(<AppHeader />);
    const seen: string[] = [];
    for (const label of ["Тема: Светлая", "Тема: Тёмная", "Тема: Как в системе"]) {
      await userEvent.click(screen.getByRole("button", { name: label }));
      seen.push(`${localStorage.getItem("studio.theme")}/${document.documentElement.dataset.theme}`);
    }
    // the OS is dark, so «system» renders dark but is stored as «system»
    expect(seen).toEqual(["dark/dark", "system/dark", "light/light"]);
    expect(screen.getByRole("button", { name: "Тема: Светлая" })).toBeTruthy();
  });

  it("each variant of the button shows its own label and the click goes to the next one", async () => {
    const next = { light: "Тёмная", dark: "Как в системе", system: "Светлая" } as const;
    const label = { light: "Светлая", dark: "Тёмная", system: "Как в системе" } as const;
    for (const saved of ["light", "dark", "system"] as const) {
      stubSystem(false);
      localStorage.setItem("studio.theme", saved);
      const { unmount } = render(<AppHeader />);
      await userEvent.click(screen.getByRole("button", { name: `Тема: ${label[saved]}` }));
      expect(screen.getByRole("button", { name: `Тема: ${next[saved]}` })).toBeTruthy();
      unmount();
      localStorage.clear();
    }
  });

  it("after a reload with a saved dark theme the button says dark, not light", () => {
    stubSystem(false);
    localStorage.setItem("studio.theme", "dark");
    render(<AppHeader />);
    expect(screen.getByRole("button", { name: "Тема: Тёмная" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Тема: Светлая" })).toBeNull();
  });

  it("on «system» it follows the OS both ways, keeps «system» stored, and stops when the choice changes", async () => {
    const system = stubSystem(false);
    localStorage.setItem("studio.theme", "system");
    render(<AppHeader />);
    expect(system.listeners).toHaveLength(1);
    system.flip(true);
    expect([document.documentElement.dataset.theme, localStorage.getItem("studio.theme")]).toEqual(["dark", "system"]);
    system.flip(false);
    expect([document.documentElement.dataset.theme, localStorage.getItem("studio.theme")]).toEqual(["light", "system"]);
    await userEvent.click(screen.getByRole("button", { name: "Тема: Как в системе" }));   // → light
    expect(system.listeners).toHaveLength(0);
    system.flip(true);
    expect(document.documentElement.dataset.theme).toBe("light");
  });
});
