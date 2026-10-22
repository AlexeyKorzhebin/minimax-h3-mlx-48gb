import { renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { FakeEventSource } from "@/test/fetchStub";
import { useEvents } from "./useEvents";

beforeEach(() => {
  FakeEventSource.last = null;
  vi.stubGlobal("EventSource", FakeEventSource);
});

describe("useEvents", () => {
  it("passes the kind and the event id of its own project's events, only for the listed kinds", () => {
    const seen: [string, number][] = [];
    renderHook(() => useEvents("p_1", 5, ["project_updated"], (kind, seq) => seen.push([kind, seq])));
    const es = FakeEventSource.last!;
    expect([...es.listeners.keys()]).toEqual(["project_updated"]);
    es.emit("project_updated", { project_id: "p_1" }, 7);
    expect(seen).toEqual([["project_updated", 7]]);
  });

  it("does not connect before the snapshot's last_seq is known", () => {
    renderHook(() => useEvents("p_1", null, ["project_updated"], () => {}));
    expect(FakeEventSource.last).toBeNull();
  });
});
