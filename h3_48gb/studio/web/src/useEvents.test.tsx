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

  it("closes the previous connection when it re-subscribes after a new last_seq, and on unmount", () => {
    const { rerender, unmount } = renderHook(
      ({ seq }) => useEvents("p_1", seq, ["project_updated"], () => {}),
      { initialProps: { seq: 5 } },
    );
    const first = FakeEventSource.last!;
    expect([first.url, first.closed]).toEqual(["/api/studio/events?after=5", false]);
    rerender({ seq: 6 });
    const second = FakeEventSource.last!;
    expect(second).not.toBe(first);
    expect([first.closed, second.url, second.closed]).toEqual([true, "/api/studio/events?after=6", false]);
    unmount();
    expect(second.closed).toBe(true);
  });
});
