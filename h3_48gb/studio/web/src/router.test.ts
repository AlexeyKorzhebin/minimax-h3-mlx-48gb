import { describe, expect, it } from "vitest";
import { parseHash } from "./router";

describe("parseHash", () => {
  it("maps #/p/<id> to the project and anything else to the list", () => {
    expect([parseHash("#/p/p_abc1"), parseHash("#/"), parseHash(""), parseHash("#/p/")]).toEqual([
      { name: "project", id: "p_abc1" }, { name: "projects" }, { name: "projects" }, { name: "projects" },
    ]);
  });
});
