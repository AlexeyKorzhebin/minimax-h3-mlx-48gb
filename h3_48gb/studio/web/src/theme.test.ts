import { afterEach, describe, expect, it, vi } from "vitest";
import { applyTheme, followSystem, readTheme } from "./theme";

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

afterEach(() => localStorage.clear());

describe("theme", () => {
  it("defaults to light, the main theme", () => {
    expect(readTheme()).toBe("light");
  });

  it("follows the system switching while the choice is «system», and stops after", () => {
    const system = stubSystem(false);
    applyTheme("system");
    const stop = followSystem("system");
    expect(document.documentElement.dataset.theme).toBe("light");
    system.flip(true);
    expect(document.documentElement.dataset.theme).toBe("dark");
    stop();
    expect(system.listeners).toHaveLength(0);
  });

  it("an explicit choice does not follow the system", () => {
    const system = stubSystem(false);
    applyTheme("light");
    followSystem("light");
    system.flip(true);
    expect(document.documentElement.dataset.theme).toBe("light");
  });
});

describe("theme storage", () => {
  it("applyTheme stores the choice and readTheme returns it; junk falls back to light", () => {
    applyTheme("dark");
    expect(localStorage.getItem("studio.theme")).toBe("dark");
    expect(readTheme()).toBe("dark");
    localStorage.setItem("studio.theme", "system");
    expect(readTheme()).toBe("system");
    localStorage.setItem("studio.theme", "sepia");
    expect(readTheme()).toBe("light");
  });
});
