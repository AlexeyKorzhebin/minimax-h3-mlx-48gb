import { describe, expect, it } from "vitest";
import { stubFetch } from "@/test/fetchStub";
import { api, ApiError } from "./client";

describe("api", () => {
  it("sends JSON and returns the body", async () => {
    const calls = stubFetch({ "POST /api/studio/projects": { status: 201, body: { id: "p_1" } } });
    expect(await api("POST", "/projects", { title: "Лес" })).toEqual({ id: "p_1" });
    expect(calls).toEqual([{ key: "POST /api/studio/projects", body: { title: "Лес" } }]);
  });

  it("turns the §7 envelope into ApiError with code and detail", async () => {
    stubFetch({
      "PATCH /api/studio/projects/p_1": {
        status: 409,
        body: { error: { code: "revision_conflict", message: "Данные изменились", detail: { current: { revision: 2 } } } },
      },
    });
    const err = (await api("PATCH", "/projects/p_1", {}).catch((e: unknown) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect([err.status, err.code, err.message, err.detail]).toEqual([
      409, "revision_conflict", "Данные изменились", { current: { revision: 2 } },
    ]);
  });
});
