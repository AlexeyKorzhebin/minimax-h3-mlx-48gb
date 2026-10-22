import { describe, expect, it } from "vitest";
import { stubFetch } from "@/test/fetchStub";
import { api, ApiError } from "./client";

describe("api", () => {
  it("sends JSON and returns the body", async () => {
    const calls = stubFetch({ "POST /api/studio/projects": { status: 201, body: { id: "p_1" } } });
    expect(await api("POST", "/projects", { title: "Лес" })).toEqual({ id: "p_1" });
    // without Content-Type: application/json the real FastAPI answers 400 args_invalid
    expect(calls).toEqual([{ key: "POST /api/studio/projects", body: { title: "Лес" }, headers: { "Content-Type": "application/json" } }]);
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

  it("sends no Content-Type when there is no body", async () => {
    const calls = stubFetch({ "GET /api/studio/projects": { body: [] } });
    await api("GET", "/projects");
    expect(calls).toEqual([{ key: "GET /api/studio/projects", body: null, headers: {} }]);
  });

  it("accepts an empty 204 body as success", async () => {
    stubFetch({ "DELETE /api/studio/projects/p_1": { status: 204, body: null, raw: "" } });
    expect(await api("DELETE", "/projects/p_1")).toBeNull();
  });

  it("turns a non-envelope error (a proxy's HTML page) into an ApiError with the status", async () => {
    stubFetch({ "GET /api/studio/projects": { status: 502, body: null, raw: "<html>Bad Gateway</html>" } });
    const err = (await api("GET", "/projects").catch((e: unknown) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect([err.status, err.code, err.message, err.detail]).toEqual([502, "http_error", "Сервер ответил 502", {}]);
  });

  it("turns an empty error body into an ApiError too", async () => {
    stubFetch({ "GET /api/studio/projects": { status: 500, body: null, raw: "" } });
    const err = (await api("GET", "/projects").catch((e: unknown) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect([err.status, err.code, err.message]).toEqual([500, "http_error", "Сервер ответил 500"]);
  });

  it("turns a 200 that is not JSON into an ApiError, not a SyntaxError", async () => {
    stubFetch({ "GET /api/studio/projects": { status: 200, body: null, raw: "<html>login</html>" } });
    const err = (await api("GET", "/projects").catch((e: unknown) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect([err.status, err.code, err.message]).toEqual([200, "bad_response", "Сервер ответил не в том формате"]);
  });
});
