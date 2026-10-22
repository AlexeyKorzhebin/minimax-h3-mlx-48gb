import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AppHeader } from "./AppHeader";

afterEach(() => {
  localStorage.clear();
  document.documentElement.dataset.theme = "light";
});

describe("AppHeader theme button", () => {
  it("cycles light → dark → system → light, applying and remembering each choice", async () => {
    vi.stubGlobal("matchMedia", () => ({ matches: true, addEventListener() {}, removeEventListener() {} }));
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
});
