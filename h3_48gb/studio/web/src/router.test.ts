import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { parseHash, useRoute } from "./router";

afterEach(() => {
  vi.restoreAllMocks();
  location.hash = "";
});

describe("parseHash", () => {
  it("maps #/p/<id> to the project and anything else to the list", () => {
    expect([parseHash("#/p/p_abc1"), parseHash("#/"), parseHash(""), parseHash("#/p/")]).toEqual([
      { name: "project", id: "p_abc1" }, { name: "projects" }, { name: "projects" }, { name: "projects" },
    ]);
  });

  it("does not take a tail after the id for a project", () => {
    expect([parseHash("#/p/p_abc1/extra"), parseHash("#/p/p_abc1?x=1")]).toEqual([
      { name: "projects" }, { name: "projects" },
    ]);
  });
});

describe("useRoute", () => {
  it("follows the hash when it changes by hand, both ways", async () => {
    location.hash = "";
    const { result } = renderHook(() => useRoute());
    expect(result.current).toEqual({ name: "projects" });
    location.hash = "#/p/p_7";
    await waitFor(() => expect(result.current).toEqual({ name: "project", id: "p_7" }));
    location.hash = "#/";
    await waitFor(() => expect(result.current).toEqual({ name: "projects" }));
  });

  it("starts from the current hash", () => {
    location.hash = "#/p/p_3";
    const { result } = renderHook(() => useRoute());
    expect(result.current).toEqual({ name: "project", id: "p_3" });
  });

  it("removes the very listener it added when unmounted", () => {
    const add = vi.spyOn(globalThis, "addEventListener");
    const remove = vi.spyOn(globalThis, "removeEventListener");
    const { unmount } = renderHook(() => useRoute());
    const added = add.mock.calls.filter((c) => c[0] === "hashchange");
    expect(added).toHaveLength(1);
    unmount();
    expect(remove.mock.calls.filter((c) => c[0] === "hashchange")).toEqual([["hashchange", added[0][1]]]);
  });
});
